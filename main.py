#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PSh Panel - 跨平台服务器控制面板"""
import os, sys, json, uuid, time, hmac, hashlib, secrets, threading
import subprocess, shutil, signal, string, socket as _socket
import re as _re
from collections import deque
from functools import wraps
from pathlib import Path

from flask import (Flask, render_template, request, jsonify, session,
                   send_file, abort, redirect, url_for)
from flask_socketio import SocketIO, emit, join_room, leave_room

try:
    import psutil
except ImportError:
    psutil = None

IS_WINDOWS = os.name == 'nt'
IS_POSIX = not IS_WINDOWS
BASE_DIR = Path(__file__).parent.absolute()
STORE_FILE = BASE_DIR / 'psh_data.json'

if IS_WINDOWS:
    try:
        from pywinpty import PtyProcess
    except ImportError:
        try:
            from winpty import PtyProcess
        except ImportError:
            PtyProcess = None
else:
    import pty, select, fcntl, termios, struct


# ============================================================
# PTY 辅助
# ============================================================
def pty_read(proc, size=4096):
    if IS_WINDOWS:
        try: return proc.read(size)
        except TypeError: return proc.read()
    return os.read(proc['fd'], size)


def pty_write(proc, data):
    if IS_WINDOWS:
        if isinstance(data, bytes): data = data.decode('utf-8', errors='replace')
        proc.write(data)
    else:
        if isinstance(data, str): data = data.encode('utf-8')
        os.write(proc['fd'], data)


def pty_resize(proc, rows, cols):
    if IS_WINDOWS:
        proc.setwinsize(rows, cols)
    else:
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(proc['fd'], termios.TIOCSWINSZ, winsize)


def default_shell():
    return 'cmd.exe' if IS_WINDOWS else '/bin/bash'


def find_free_port(start=5000):
    for p in range(start, start + 100):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', p)); return p
            except OSError: continue
    return start


def _safe_int(v, default, lo, hi):
    try: n = int(v)
    except (TypeError, ValueError): return default
    return max(lo, min(hi, n))


def rewrite_python(cmd, python_path):
    if not python_path or not cmd: return cmd
    s = cmd.lstrip()
    if not s: return cmd
    i = 0
    while i < len(s) and not s[i].isspace(): i += 1
    first = s[:i]
    rest = s[i:]
    if first in ('python', 'python3', 'py', 'py.exe') or first.startswith('python3.'):
        return f'"{python_path}"{rest}'
    return cmd


# ============================================================
# 环境变量解析：只认 set/export 前缀
# ============================================================
_RE_SET_DECL    = _re.compile(r'^\s*set\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*$', _re.IGNORECASE)
_RE_EXPORT_DECL = _re.compile(r'^\s*export\s+([A-Za-z_]\w*)\s*=\s*(.*?)\s*$')


def _strip_quotes(v):
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    return v


def _parse_env_decl(line):
    if not line: return None
    m = _RE_EXPORT_DECL.match(line) or _RE_SET_DECL.match(line)
    if not m: return None
    return m.group(1), _strip_quotes(m.group(2))


# ============================================================
# 存储
# ============================================================
class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return self._defaults()

    def _defaults(self):
        return {
            "auth": None,
            "secret_key": secrets.token_hex(32),
            "settings": {
                "scrollback": 5000, "font_size": 14, "max_history": 10,
                "panel_title": "PSh Panel", "perf_interval": 5000,
                "stop_timeout": 5, "python_path": "",
                "min_password_len": 8, "session_days": 7,
                "max_login_attempts": 5, "lockout_minutes": 5,
            },
            "tasks": {},
            "history": {},
        }

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def get_setting(self, key, default=None):
        with self._lock:
            return self._data['settings'].get(key, default)

    @property
    def data(self): return self._data
    @property
    def lock(self): return self._lock


store = Store(STORE_FILE)
with store.lock:
    for k, v in Store(STORE_FILE)._defaults()['settings'].items():
        store.data['settings'].setdefault(k, v)
    store.data.setdefault('login_fails', {})
    store.data.setdefault('locked_ips', {})


# ============================================================
# 登录守卫
# ============================================================
class LoginGuard:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._fails = {}
        self._locked = {}
        self._load()

    def _load(self):
        try:
            with self.store.lock:
                for ip, info in self.store.data.get('login_fails', {}).items():
                    self._fails[ip] = info
                for ip, ts in self.store.data.get('locked_ips', {}).items():
                    if ts > time.time(): self._locked[ip] = ts
        except Exception: pass

    def _persist(self):
        try:
            with self.store.lock:
                self.store.data['login_fails'] = self._fails
                self.store.data['locked_ips'] = {k: v for k, v in self._locked.items() if v > time.time()}
                self.store.save()
        except Exception: pass

    def get_client_ip(self):
        fwd = request.headers.get('X-Forwarded-For', '')
        if fwd: return fwd.split(',')[0].strip()
        real = request.headers.get('X-Real-IP', '')
        if real: return real.strip()
        return request.remote_addr or 'unknown'

    def is_locked(self, ip):
        with self._lock:
            unlock = self._locked.get(ip)
            if unlock and unlock > time.time(): return True, unlock
            if unlock: self._locked.pop(ip, None)
            return False, 0

    def record_fail(self, ip):
        max_attempts = int(self.store.get_setting('max_login_attempts', 5))
        lock_min = int(self.store.get_setting('lockout_minutes', 5))
        with self._lock:
            info = self._fails.get(ip, {"count": 0, "last": 0})
            now = time.time()
            if now - info.get("last", 0) > 600: info["count"] = 0
            info["count"] += 1
            info["last"] = now
            self._fails[ip] = info
            if info["count"] >= max_attempts:
                self._locked[ip] = now + lock_min * 60
                info["count"] = 0
                self._persist()
                return True, self._locked[ip]
            self._persist()
            return False, 0

    def record_success(self, ip):
        with self._lock:
            self._fails.pop(ip, None)
            self._locked.pop(ip, None)
            self._persist()

    def backoff_delay(self, ip):
        with self._lock:
            info = self._fails.get(ip)
            if not info: return 0
            count = info.get("count", 0)
            if count <= 0: return 0
            return min(30, 2 ** (count - 1))


login_guard = LoginGuard(store)


# ============================================================
# 认证
# ============================================================
PBKDF2_ITER = 260_000


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITER)
    return {"salt": salt.hex(), "hash": h.hex(), "iterations": PBKDF2_ITER}


def verify_password(password, record):
    if not record: return False
    salt = bytes.fromhex(record["salt"])
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                            salt, record.get("iterations", PBKDF2_ITER))
    return hmac.compare_digest(h.hex(), record["hash"])


def password_strength(pwd, min_len=8):
    if not isinstance(pwd, str): return False, '密码格式错误'
    if len(pwd) < min_len: return False, f'密码长度至少 {min_len} 位'
    kinds = 0
    if any(c.islower() for c in pwd): kinds += 1
    if any(c.isupper() for c in pwd): kinds += 1
    if any(c.isdigit() for c in pwd): kinds += 1
    if any(not c.isalnum() for c in pwd): kinds += 1
    if kinds < 2:
        return False, '密码需包含至少两类字符（大小写字母/数字/符号）'
    return True, ''


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('auth'):
            if request.path.startswith('/api/'):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# Flask
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = store.data.get('secret_key') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * int(store.get_setting('session_days', 7))

socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*',
                    max_http_buffer_size=50 * 1024 * 1024)


# ============================================================
# 任务运行时
# ============================================================
class TaskRuntime:
    def __init__(self, task_id):
        self.task_id = task_id
        self.process = None
        self.mode = None
        self.buffer = deque(maxlen=5000)
        self.seq = 0
        self.lock = threading.RLock()
        self.reader = None
        self.status = 'stopped'
        self.exit_code = None
        self.started_at = None
        self.stopping = False
        self.env_buffer = deque(maxlen=500)
        self.env_seq = 0
        self.env_status = 'none'
        self.env_vars = {}
        self.env_exit_code = None


# ============================================================
# 任务管理器
# ============================================================
class TaskManager:
    FLUSH_INTERVAL = 0.1
    FLUSH_MAX_BYTES = 32 * 1024

    def __init__(self, store, socketio):
        self.store = store
        self.socketio = socketio
        self.runtimes = {}
        self.lock = threading.RLock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._restart_locks = {}
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _flush_loop(self):
        while True:
            time.sleep(self.FLUSH_INTERVAL)
            with self._pending_lock:
                pending = self._pending
                self._pending = {}
            for task_id, items in pending.items():
                if not items: continue
                merged, total, latest_seq = [], 0, items[-1][0]
                for _, t in items:
                    merged.append(t); total += len(t)
                    if total >= self.FLUSH_MAX_BYTES: break
                self.socketio.emit('task:output', {
                    'task_id': task_id, 'seq': latest_seq, 'data': ''.join(merged),
                }, room=f"task:{task_id}")

    def list_tasks(self):
        with self.store.lock:
            return [self._public(t) for t in self.store.data.get("tasks", {}).values()]

    def get_task(self, tid):
        with self.store.lock:
            return self.store.data.get("tasks", {}).get(tid)

    def _public(self, task):
        rt = self.runtimes.get(task["id"])
        return {
            **task,
            "status": rt.status if rt else "stopped",
            "exit_code": rt.exit_code if rt else None,
            "started_at": rt.started_at if rt else None,
            "pid": self._pid(task["id"]),
        }

    def _pid(self, tid):
        rt = self.runtimes.get(tid)
        if not rt or rt.status != 'running' or not rt.process: return None
        if rt.mode == 'pipe': return rt.process.pid
        if IS_WINDOWS: return getattr(rt.process, 'pid', None)
        return rt.process["pid"]

    def create_task(self, data):
        tid = str(uuid.uuid4())[:8]
        task = {
            "id": tid, "name": data.get("name", "Untitled"),
            "command": data.get("command", ""),
            "cwd": data.get("cwd") or str(BASE_DIR),
            "mode": data.get("mode", "pipe"),
            "enabled": bool(data.get("enabled", False)),
            "env_script": data.get("env_script", ""),
            "created_at": time.time(),
        }
        with self.store.lock:
            self.store.data["tasks"][tid] = task
            self.store.save()
        return task

    def update_task(self, tid, data):
        with self.store.lock:
            task = self.store.data["tasks"].get(tid)
            if not task: return None
            for k in ("name", "command", "cwd", "mode", "enabled", "env_script"):
                if k in data: task[k] = data[k]
            self.store.save()
            return task

    def delete_task(self, tid):
        self.stop(tid)
        with self.store.lock:
            self.store.data["tasks"].pop(tid, None)
            self.store.data.get("history", {}).pop(tid, None)
            self.store.save()
        _clear_task_io_state(tid)

    def start(self, tid):
        task = self.get_task(tid)
        if not task: return {"error": "task not found"}
        with self.lock:
            rt = self.runtimes.get(tid)
            if rt and rt.status == 'running':
                return {"error": "already running"}
            rt = TaskRuntime(tid)
            rt.mode = task["mode"]
            rt.buffer = deque(maxlen=self.store.get_setting('scrollback', 5000))
            self.runtimes[tid] = rt
            try:
                env_extra = {}
                script = (task.get("env_script") or "").strip()
                if script:
                    env_extra = self._run_env_script(rt, task)
                self._spawn(rt, task, env_extra)
                rt.status = 'running'
                rt.started_at = time.time()
                self.socketio.emit('task:status', {
                    'task_id': tid, 'status': 'running',
                    'started_at': rt.started_at, 'pid': self._pid(tid),
                })
                return {"ok": True}
            except Exception as e:
                rt.status = 'exited'; rt.exit_code = -1
                self._emit_output(tid, f"\r\n[启动失败] {e}\r\n")
                self.socketio.emit('task:status', {
                    'task_id': tid, 'status': 'exited', 'exit_code': -1,
                })
                return {"error": str(e)}

    def _spawn(self, rt, task, env_extra=None):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["TERM"] = "xterm-256color"
        if env_extra:
            for k, v in env_extra.items(): env[str(k)] = str(v)
        py = self.store.get_setting('python_path', '') or ''
        if py: env['PYTHON'] = py
        cwd = task.get("cwd") or str(BASE_DIR)
        cmd = rewrite_python(task["command"], py)
        if not cmd: raise RuntimeError("命令为空")

        if task["mode"] == "pty":
            self._spawn_pty(rt, cmd, cwd, env)
        else:
            self._spawn_pipe(rt, cmd, cwd, env)
        rt.reader.start()

    def _spawn_pty(self, rt, cmd, cwd, env):
        if IS_WINDOWS:
            if PtyProcess is None: raise RuntimeError("pywinpty 未安装")
            try:
                proc = PtyProcess.spawn(cmd, dimensions=(30, 120), env=env, cwd=cwd)
            except TypeError:
                proc = PtyProcess.spawn(cmd, dimensions=(30, 120), env=env)
            rt.process = proc
            rt.reader = threading.Thread(target=self._read_pty_win, args=(rt,), daemon=True)
        else:
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(cwd)
                    os.execvpe('/bin/sh', ['/bin/sh', '-c', cmd], env)
                except Exception: os._exit(127)
            rt.process = {"pid": pid, "fd": fd}
            rt.reader = threading.Thread(target=self._read_pty_posix, args=(rt,), daemon=True)

    def _spawn_pipe(self, rt, cmd, cwd, env):
        kwargs = dict(shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      stdin=subprocess.DEVNULL, cwd=cwd, env=env, bufsize=0)
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["preexec_fn"] = os.setsid
        rt.process = subprocess.Popen(cmd, **kwargs)
        rt.reader = threading.Thread(target=self._read_pipe, args=(rt,), daemon=True)

    def stop(self, tid, timeout=None, force=False):
        with self.lock:
            rt = self.runtimes.get(tid)
            if not rt or rt.status != 'running': return {"ok": True}
            rt.stopping = True
            if timeout is None:
                timeout = float(self.store.get_setting('stop_timeout', 5))
            try:
                if rt.mode == 'pty': self._kill_pty(rt, timeout, force)
                else: self._kill_pipe(rt, timeout, force)
            finally:
                rt.status = 'exited'
                self.socketio.emit('task:status', {
                    'task_id': tid, 'status': 'exited', 'exit_code': rt.exit_code,
                })
            return {"ok": True}

    def _kill_pty(self, rt, timeout, force):
        proc = rt.process
        if IS_WINDOWS:
            if not force:
                try: proc.write('\x03')
                except Exception: pass
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        if not proc.isalive(): break
                    except Exception: break
                    time.sleep(0.1)
            try: getattr(proc, 'kill', proc.close)()
            except Exception:
                try: proc.close()
                except Exception: pass
        else:
            pid = proc["pid"]
            if not force:
                try: os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try: os.kill(pid, signal.SIGTERM)
                    except Exception: pass
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        wpid, _ = os.waitpid(pid, os.WNOHANG)
                        if wpid == pid: return
                    except ChildProcessError: return
                    time.sleep(0.1)
            try: os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                try: os.kill(pid, signal.SIGKILL)
                except Exception: pass

    def _kill_pipe(self, rt, timeout, force):
        proc = rt.process
        if IS_WINDOWS:
            if not force:
                try: proc.send_signal(signal.CTRL_BREAK_EVENT)
                except Exception: pass
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if proc.poll() is not None: return
                    time.sleep(0.1)
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except Exception:
                try: proc.kill()
                except Exception: pass
        else:
            if not force:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try: proc.terminate()
                    except Exception: pass
                try:
                    proc.wait(timeout=timeout); return
                except Exception: pass
            try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try: proc.kill()
                except Exception: pass

    def restart(self, tid, timeout=None, force=False):
        with self.lock:
            if self._restart_locks.get(tid):
                return {"error": "restart already in progress"}
            self._restart_locks[tid] = True

        def _do():
            try:
                self.socketio.emit('task:status', {'task_id': tid, 'status': 'restarting'})
                self.stop(tid, timeout=timeout, force=force)
                rt = self.runtimes.get(tid)
                if rt and rt.reader and rt.reader.is_alive():
                    rt.reader.join(timeout=2.0)
                if IS_WINDOWS:
                    time.sleep(0.3)
                self.start(tid)
            except Exception as e:
                print(f"[restart] {tid}: {e}")
            finally:
                self._restart_locks[tid] = False

        threading.Thread(target=_do, daemon=True).start()
        return {"ok": True, "async": True}

    # ---------- 环境脚本 ----------
    def _emit_env(self, rt, stream, text):
        with rt.lock:
            rt.env_seq += 1
            rt.env_buffer.append((rt.env_seq, stream, text))
        self.socketio.emit('task:env-output', {
            'task_id': rt.task_id, 'stream': stream, 'data': text,
        }, room=f"task-env:{rt.task_id}")

    def _set_env_status(self, rt, status, **extra):
        rt.env_status = status
        self.socketio.emit('task:env-status',
                           {'task_id': rt.task_id, 'status': status, **extra},
                           room=f"task-env:{rt.task_id}")

    def _run_env_script(self, rt, task, timeout=15):
        script = (task.get("env_script") or "").strip()
        if not script: return {}
        cwd = task.get("cwd") or str(BASE_DIR)
        py = self.store.get_setting('python_path', '') or ''
        exec_script = rewrite_python(script, py)

        self._set_env_status(rt, 'running')
        self._emit_env(rt, 'stdout', f"$ {exec_script}\n# cwd: {cwd}\n\n")

        try:
            exec_cmd = exec_script
            try:
                first = exec_script.split()[0]
                if first.startswith(('./', '../', '.\\', '..\\')):
                    abs_first = str((Path(cwd) / first).resolve())
                    exec_cmd = exec_script.replace(first, abs_first, 1)
            except Exception: pass

            env_for_script = os.environ.copy()
            if py: env_for_script['PYTHON'] = py

            kwargs = dict(shell=True, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          env=env_for_script, bufsize=0)
            if IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["preexec_fn"] = os.setsid
            proc = subprocess.Popen(exec_cmd, **kwargs)

            def pump(stream, name):
                try:
                    for line in iter(stream.readline, b''):
                        if not line: break
                        self._emit_env(rt, name, line.decode('utf-8', errors='replace'))
                except Exception as e:
                    self._emit_env(rt, 'stderr', f"[read error] {e}\n")
                finally:
                    try: stream.close()
                    except Exception: pass

            t_out = threading.Thread(target=pump, args=(proc.stdout, 'stdout'), daemon=True)
            t_err = threading.Thread(target=pump, args=(proc.stderr, 'stderr'), daemon=True)
            t_out.start(); t_err.start()

            timed_out = False
            try: proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                # 先温和 SIGTERM，再强杀
                try:
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/T", "/PID", str(proc.pid)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception: pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        if IS_WINDOWS:
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                        else:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try: proc.kill()
                        except Exception: pass
                self._emit_env(rt, 'stderr', f"\n[env script timed out after {timeout}s]\n")

            t_out.join(timeout=1); t_err.join(timeout=1)
            rt.env_exit_code = proc.returncode

            env_vars = {}
            for _, stream, text in rt.env_buffer:
                if stream != 'stdout': continue
                for raw in text.splitlines():
                    line = raw.strip()
                    if not line: continue
                    if line.startswith('$ ') or line.startswith('# '): continue
                    if line.startswith('# cwd:'): continue
                    parsed = _parse_env_decl(line)
                    if parsed: env_vars[parsed[0]] = parsed[1]
            for raw in exec_script.splitlines():
                line = raw.strip()
                if not line or line.startswith('#') or line.startswith('//'): continue
                parsed = _parse_env_decl(line)
                if parsed and parsed[0] not in env_vars:
                    env_vars[parsed[0]] = parsed[1]
            rt.env_vars = env_vars

            if timed_out:
                self._set_env_status(rt, 'error', exit_code=proc.returncode,
                                     vars=env_vars, reason='timeout')
            elif proc.returncode != 0:
                self._set_env_status(rt, 'error', exit_code=proc.returncode,
                                     vars=env_vars, reason='nonzero_exit')
                self._emit_env(rt, 'stderr', f"\n[env script exited with code {proc.returncode}]\n")
            else:
                self._set_env_status(rt, 'ok', exit_code=0, vars=env_vars)
                self._emit_env(rt, 'stdout', f"\n[env script ok: {len(env_vars)} variable(s) parsed]\n")
            return env_vars
        except FileNotFoundError as e:
            self._set_env_status(rt, 'error', error=f'command not found: {e}')
            self._emit_env(rt, 'stderr', f"\n[env script error] 命令未找到：{exec_script.split()[0]}\n")
            return {}
        except Exception as e:
            self._set_env_status(rt, 'error', error=str(e))
            self._emit_env(rt, 'stderr', f"\n[env script exception] {e}\n")
            return {}

    def _emit_output(self, tid, text):
        rt = self.runtimes.get(tid)
        if not rt: return
        with rt.lock:
            rt.seq += 1
            seq = rt.seq
            rt.buffer.append((seq, text))
        with self._pending_lock:
            self._pending.setdefault(tid, []).append((seq, text))

    def _read_pty_win(self, rt):
        proc = rt.process
        try:
            while True:
                try:
                    data = pty_read(proc, 4096)
                    if data:
                        if isinstance(data, bytes): data = data.decode('utf-8', errors='replace')
                        self._emit_output(rt.task_id, data)
                    elif not proc.isalive(): break
                except EOFError: break
                except Exception as e:
                    if not rt.stopping:
                        self._emit_output(rt.task_id, f"\r\n[读取错误] {e}\r\n")
                    break
        finally:
            code = -1
            try:
                proc.wait(); code = proc.exitstatus
            except Exception: pass
            try: proc.close()
            except Exception: pass
            self._finalize(rt, code)

    def _read_pty_posix(self, rt):
        fd = rt.process["fd"]
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try: data = os.read(fd, 4096)
                    except OSError: break
                    if not data: break
                    self._emit_output(rt.task_id, data.decode('utf-8', errors='replace'))
        finally:
            try: os.close(fd)
            except Exception: pass
            code = -1
            try:
                _, status = os.waitpid(rt.process["pid"], 0)
                if hasattr(os, 'waitstatus_to_exitcode'):
                    code = os.waitstatus_to_exitcode(status)
                elif os.WIFEXITED(status): code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status): code = -os.WTERMSIG(status)
            except Exception: pass
            self._finalize(rt, code)

    def _read_pipe(self, rt):
        proc = rt.process
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk: break
                self._emit_output(rt.task_id, chunk.decode('utf-8', errors='replace'))
        finally:
            code = -1
            try:
                proc.wait(timeout=2); code = proc.returncode
            except Exception: pass
            self._finalize(rt, code)

    def _finalize(self, rt, code):
        rt.exit_code = code
        rt.status = 'exited'
        with self._pending_lock:
            pending_items = self._pending.pop(rt.task_id, None)
        if pending_items:
            merged = ''.join(t for _, t in pending_items)
            if merged:
                self.socketio.emit('task:output', {
                    'task_id': rt.task_id,
                    'seq': pending_items[-1][0],
                    'data': merged,
                }, room=f"task:{rt.task_id}")
        self.socketio.emit('task:status', {
            'task_id': rt.task_id, 'status': 'exited', 'exit_code': code,
        })
        if rt.started_at:
            try:
                with self.store.lock:
                    hist = self.store.data.setdefault("history", {}).setdefault(rt.task_id, [])
                    max_h = self.store.get_setting('max_history', 10)
                    tail = [(s, d) for s, d in rt.buffer][-500:]
                    hist.append({
                        "started_at": rt.started_at, "ended_at": time.time(),
                        "exit_code": code,
                        "tail": [{"seq": s, "data": d} for s, d in tail],
                    })
                    if len(hist) > max_h: hist[:] = hist[-max_h:]
                    self.store.save()
            except Exception: pass

    def get_logs(self, tid, tail=1000, after=None):
        rt = self.runtimes.get(tid)
        if not rt:
            with self.store.lock:
                hist = self.store.data.get("history", {}).get(tid, [])
                if hist:
                    last = hist[-1]
                    return {"lines": last["tail"],
                            "latest_seq": last["tail"][-1]["seq"] if last["tail"] else 0}
            return {"lines": [], "latest_seq": 0}
        with rt.lock:
            if after is not None:
                lines = [(s, d) for s, d in rt.buffer if s > after]
            else:
                lines = list(rt.buffer)[-tail:]
            return {"lines": [{"seq": s, "data": d} for s, d in lines], "latest_seq": rt.seq}


task_manager = TaskManager(store, socketio)


# ============================================================
# Git Pull 会话（PTY）
# ============================================================
_git_sessions = {}
_git_lock = threading.RLock()


def _git_emit(task_id, text):
    socketio.emit('git:output', {'task_id': task_id, 'data': text}, room=f"git:{task_id}")


def _git_reader_win(task_id, proc):
    try:
        while True:
            try:
                data = pty_read(proc, 4096)
                if data:
                    if isinstance(data, bytes): data = data.decode('utf-8', errors='replace')
                    _git_emit(task_id, data)
                elif not proc.isalive(): break
            except EOFError: break
            except Exception as e:
                _git_emit(task_id, f"\r\n[读取错误] {e}\r\n"); break
    finally:
        code = -1
        try:
            proc.wait(); code = proc.exitstatus
        except Exception: pass
        try: proc.close()
        except Exception: pass
        _git_finalize(task_id, code)


def _git_reader_posix(task_id, proc):
    fd = proc["fd"]
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                try: data = os.read(fd, 4096)
                except OSError: break
                if not data: break
                _git_emit(task_id, data.decode('utf-8', errors='replace'))
    finally:
        try: os.close(fd)
        except Exception: pass
        code = -1
        try:
            _, status = os.waitpid(proc["pid"], 0)
            if hasattr(os, 'waitstatus_to_exitcode'):
                code = os.waitstatus_to_exitcode(status)
            elif os.WIFEXITED(status): code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status): code = -os.WTERMSIG(status)
        except Exception: pass
        _git_finalize(task_id, code)


def _git_finalize(task_id, code):
    socketio.emit('git:done', {'task_id': task_id, 'exit_code': code}, room=f"git:{task_id}")
    with _git_lock:
        _git_sessions.pop(task_id, None)


# ============================================================
# 磁盘 / 网络采样
# ============================================================
_disk_state = {"lock": threading.RLock(), "last": {}}
_net_alloc_state = {"lock": threading.RLock(), "ts": 0.0, "conns": {}, "total": 0}
_NET_ALLOC_INTERVAL = 5.0
_NET_ESTIMATED = IS_POSIX
_RATE_RESET_THRESHOLD = 10.0


def _conn_count(pid):
    if not psutil or not pid: return 0
    try: return len(psutil.Process(pid).connections(kind='inet'))
    except Exception: return 0


def _refresh_net_alloc(task_pids):
    now = time.time()
    with _net_alloc_state["lock"]:
        if now - _net_alloc_state["ts"] < _NET_ALLOC_INTERVAL: return
    conns, total = {}, 0
    for tid, pid in task_pids.items():
        n = _conn_count(pid); conns[tid] = n; total += n
    with _net_alloc_state["lock"]:
        _net_alloc_state["conns"] = conns
        _net_alloc_state["total"] = total
        _net_alloc_state["ts"] = now


def _task_disk_rate(pid, tid):
    if not psutil or not pid: return 0, 0
    try:
        io = psutil.Process(pid).io_counters()
        cur_read, cur_write = io.read_bytes, io.write_bytes
    except Exception: return 0, 0
    now = time.time()
    with _disk_state["lock"]:
        prev = _disk_state["last"].get(tid)
        _disk_state["last"][tid] = {"ts": now, "read": cur_read, "write": cur_write}
    if not prev: return 0, 0
    dt = now - prev["ts"]
    if dt <= 0 or dt > _RATE_RESET_THRESHOLD: return 0, 0
    if cur_read < prev["read"] or cur_write < prev["write"]: return 0, 0
    return int((cur_read - prev["read"]) / dt), int((cur_write - prev["write"]) / dt)


def _clear_task_io_state(tid):
    with _disk_state["lock"]:
        _disk_state["last"].pop(tid, None)


# ============================================================
# 性能广播（惰性）
# ============================================================
_perf_last = {"net": None, "time": None}
_perf_call_lock = threading.Lock()


def _get_cpu_temperature():
    """尝试获取 CPU 温度（°C）。返回 None 表示不可用。"""
    # 1) psutil.sensors_temperatures（Linux / macOS）
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ('coretemp', 'cpu_thermal', 'soc_thermal',
                         'k10temp', 'zenpower', 'acpitz'):
                if name in temps and temps[name]:
                    return round(float(temps[name][0].current), 1)
            for entries in temps.values():
                if entries:
                    return round(float(entries[0].current), 1)
    except Exception:
        pass

    # 2) 直接读 sysfs（ARM 常见）
    if IS_POSIX:
        for path in ('/sys/class/thermal/thermal_zone0/temp',
                     '/sys/class/hwmon/hwmon0/temp1_input'):
            try:
                with open(path, 'r') as f:
                    raw = f.read().strip()
                val = float(raw)
                if val > 1000:
                    val /= 1000.0
                return round(val, 1)
            except Exception:
                continue

    return None


class PerfBroadcaster:
    def __init__(self, socketio, store):
        self.socketio = socketio
        self.store = store
        self._lock = threading.Lock()
        self._subscribers = set()
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set(); self._wake.set()

    def subscribe(self, sid):
        with self._lock: self._subscribers.add(sid)
        self._wake.set()

    def unsubscribe(self, sid):
        with self._lock: self._subscribers.discard(sid)

    def _is_empty(self):
        with self._lock: return not self._subscribers

    def _loop(self):
        while not self._stop.is_set():
            if self._is_empty():
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                _perf_last["net"] = None
                _perf_last["time"] = None
                continue
            try:
                interval = float(self.store.get_setting('perf_interval', 5000)) / 1000.0
            except Exception: interval = 5.0
            interval = max(0.5, min(interval, 30.0))
            if _perf_last["time"] is None:
                try:
                    io = psutil.net_io_counters() if psutil else None
                    if io:
                        _perf_last["net"] = io
                        _perf_last["time"] = time.time()
                except Exception: pass
                with _disk_state["lock"]:
                    _disk_state["last"].clear()
                time.sleep(0.2); continue
            try:
                with _perf_call_lock:
                    data = get_perf()
                self.socketio.emit('perf:data', data, room='perf')
            except Exception as e:
                print(f"[perf] {e}")
            self._stop.wait(interval)


# ============================================================
# 临时终端
# ============================================================
class TerminalSession:
    def __init__(self, term_id):
        self.term_id = term_id
        self.proc = None
        self.thread = None
        self.buffer = deque(maxlen=5000)
        self.seq = 0
        self.lock = threading.RLock()
        self.alive = True
        self._pending = []
        self._pending_lock = threading.Lock()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _flush_loop(self):
        while self.alive:
            time.sleep(0.1)
            with self._pending_lock:
                if not self._pending: continue
                merged = ''.join(self._pending); self._pending = []
            if merged and self.alive:
                socketio.emit('term:output',
                              {'term_id': self.term_id, 'data': merged},
                              room=f"term:{self.term_id}")

    def spawn(self, command, cwd=None):
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        cwd = cwd or str(BASE_DIR)
        py = store.get_setting('python_path', '') or ''
        if py: env['PYTHON'] = py
        command = rewrite_python(command, py)
        if IS_WINDOWS:
            if PtyProcess is None: raise RuntimeError("pywinpty 未安装")
            try: self.proc = PtyProcess.spawn(command, dimensions=(30, 120), env=env, cwd=cwd)
            except TypeError: self.proc = PtyProcess.spawn(command, dimensions=(30, 120), env=env)
            self.thread = threading.Thread(target=self._read_win, daemon=True)
        else:
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(cwd)
                    os.execvpe('/bin/sh', ['/bin/sh', '-c', command], env)
                except Exception: os._exit(127)
            self.proc = {"pid": pid, "fd": fd}
            self.thread = threading.Thread(target=self._read_posix, daemon=True)
        self.thread.start()

    def _emit(self, data):
        with self.lock:
            self.seq += 1
            self.buffer.append((self.seq, data))
        with self._pending_lock:
            self._pending.append(data)

    def _read_win(self):
        try:
            while True:
                try:
                    data = pty_read(self.proc, 4096)
                    if data:
                        if isinstance(data, bytes): data = data.decode('utf-8', errors='replace')
                        self._emit(data)
                    elif not self.proc.isalive(): break
                except EOFError: break
                except Exception: break
        finally:
            code = -1
            try:
                self.proc.wait(); code = self.proc.exitstatus
            except Exception: pass
            try: self.proc.close()
            except Exception: pass
            self._finalize(code)

    def _read_posix(self):
        fd = self.proc["fd"]
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try: data = os.read(fd, 4096)
                    except OSError: break
                    if not data: break
                    self._emit(data.decode('utf-8', errors='replace'))
        finally:
            try: os.close(fd)
            except Exception: pass
            code = -1
            try:
                _, status = os.waitpid(self.proc["pid"], 0)
                if hasattr(os, 'waitstatus_to_exitcode'):
                    code = os.waitstatus_to_exitcode(status)
                elif os.WIFEXITED(status): code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status): code = -os.WTERMSIG(status)
            except Exception: pass
            self._finalize(code)

    def _finalize(self, code):
        if self.alive:
            socketio.emit('term:exit', {'term_id': self.term_id, 'code': code},
                          room=f"term:{self.term_id}")
        sessions.pop(self.term_id, None)

    def write(self, data):
        if not self.proc: return
        try: pty_write(self.proc, data)
        except Exception: pass

    def resize(self, rows, cols):
        if not self.proc: return
        try: pty_resize(self.proc, rows, cols)
        except Exception: pass

    def close(self):
        self.alive = False
        timeout = float(store.get_setting('stop_timeout', 5))
        if IS_WINDOWS:
            try: self.proc.write('\x03')
            except Exception: pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    if not self.proc.isalive(): break
                except Exception: break
                time.sleep(0.1)
            try: getattr(self.proc, 'kill', self.proc.close)()
            except Exception:
                try: self.proc.close()
                except Exception: pass
        else:
            pid = self.proc["pid"]
            try: os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception:
                try: os.kill(pid, signal.SIGTERM)
                except Exception: pass
            deadline = time.time() + timeout
            exited = False
            while time.time() < deadline:
                try:
                    wpid, _ = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid: exited = True; break
                except ChildProcessError: exited = True; break
                time.sleep(0.1)
            if not exited:
                try: os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:
                    try: os.kill(pid, signal.SIGKILL)
                    except Exception: pass


sessions = {}


# ============================================================
# 文件管理辅助
# ============================================================
def list_roots():
    roots = []
    if IS_WINDOWS:
        if psutil:
            try:
                for p in psutil.disk_partitions(all=False):
                    roots.append({"name": p.device, "path": p.device,
                                  "type": p.fstype or "drive", "is_root": True})
            except Exception: pass
        if not roots:
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    roots.append({"name": drive, "path": drive, "type": "drive", "is_root": True})
    else:
        home = str(Path.home())
        roots.append({"name": "~", "path": home, "type": "home", "is_root": True})
        roots.append({"name": "/", "path": "/", "type": "root", "is_root": True})
        seen = {"~", "/", home}
        if psutil:
            try:
                for p in psutil.disk_partitions(all=False):
                    mp = p.mountpoint
                    if mp in seen: continue
                    seen.add(mp)
                    roots.append({"name": mp, "path": mp, "type": p.fstype or "mount", "is_root": True})
            except Exception: pass
    return roots


def resolve_path(path):
    if not path: return None
    try: return str(Path(os.path.expanduser(path)).resolve())
    except Exception: return None


def file_info(path):
    try:
        st = path.stat(); is_dir = path.is_dir()
        return {"name": path.name or str(path), "path": str(path),
                "is_dir": is_dir, "size": 0 if is_dir else st.st_size,
                "mtime": st.st_mtime, "mode": oct(st.st_mode & 0o777)}
    except Exception as e:
        return {"name": path.name, "path": str(path), "is_dir": False,
                "size": 0, "mtime": 0, "mode": "000", "error": str(e)}


def list_dir(path):
    p = Path(path)
    if not p.exists() or not p.is_dir(): return None
    entries = []
    try:
        for child in p.iterdir(): entries.append(file_info(child))
    except PermissionError:
        return {"error": "permission denied", "entries": []}
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "entries": entries}


def protected_paths():
    if IS_WINDOWS:
        return {"C:\\", "C:/", "C:", "C:\\Windows", "C:/Windows",
                "C:\\Program Files", "C:/Program Files",
                "C:\\Program Files (x86)", "C:/Program Files (x86)"}
    return {"/", "/home", "/root", "/etc", "/usr", "/var", "/bin",
            "/sbin", "/boot", "/lib", "/lib64", "/opt", "/proc", "/sys",
            "/dev", "/run", "/tmp"}


# ============================================================
# 性能采集
# ============================================================
def get_perf():
    result = {
        "cpu": {"percent": 0, "per_cpu": [], "count": 0, "freq": None,
                "freq_max": None, "load": None, "temperature": None},
        "memory": {"total": 0, "used": 0, "percent": 0,
                   "swap_total": 0, "swap_used": 0},
        "net": {"sent_rate": 0, "recv_rate": 0, "sent_total": 0, "recv_total": 0},
        "disk": {"total": 0, "used": 0, "percent": 0},
        "uptime": 0, "tasks": [],
    }
    if not psutil: return result

    result["cpu"]["percent"] = psutil.cpu_percent(interval=None)
    result["cpu"]["per_cpu"] = psutil.cpu_percent(interval=None, percpu=True)
    result["cpu"]["count"] = psutil.cpu_count()
    try:
        f = psutil.cpu_freq()
        if f:
            result["cpu"]["freq"] = f.current
            result["cpu"]["freq_max"] = f.max
    except Exception:
        pass

    # ---- 系统负载（Linux/macOS；Windows 返回 None）----
    try:
        load = os.getloadavg()
        result["cpu"]["load"] = {"1m": load[0], "5m": load[1], "15m": load[2]}
    except Exception:
        result["cpu"]["load"] = None

    # ---- CPU 温度 ----
    result["cpu"]["temperature"] = _get_cpu_temperature()

    m = psutil.virtual_memory()
    result["memory"] = {"total": m.total, "used": m.used, "percent": m.percent,
                        "available": m.available}
    try:
        s = psutil.swap_memory()
        result["memory"]["swap_total"] = s.total
        result["memory"]["swap_used"] = s.used
    except Exception: pass

    try:
        io = psutil.net_io_counters()
        now = time.time()
        prev_net, prev_time = _perf_last["net"], _perf_last["time"]
        if prev_net and prev_time:
            dt = now - prev_time
            if 0 < dt <= _RATE_RESET_THRESHOLD:
                result["net"]["sent_rate"] = max(0, io.bytes_sent - prev_net.bytes_sent) / dt
                result["net"]["recv_rate"] = max(0, io.bytes_recv - prev_net.bytes_recv) / dt
        _perf_last["net"] = io; _perf_last["time"] = now
        result["net"]["sent_total"] = io.bytes_sent
        result["net"]["recv_total"] = io.bytes_recv
    except Exception: pass

    try:
        root = "C:\\" if IS_WINDOWS else "/"
        du = psutil.disk_usage(root)
        result["disk"] = {"total": du.total, "used": du.used, "percent": du.percent}
    except Exception: pass

    try: result["uptime"] = time.time() - psutil.boot_time()
    except Exception: pass

    all_tasks = task_manager.list_tasks()
    task_pids = {t["id"]: t["pid"] for t in all_tasks if t.get("pid")}
    if task_pids: _refresh_net_alloc(task_pids)
    with _net_alloc_state["lock"]:
        conns_map = dict(_net_alloc_state["conns"])
        total_conns = _net_alloc_state["total"]

    for task in all_tasks:
        tid = task["id"]; pid = task.get("pid")
        entry = {"id": tid, "name": task["name"], "status": task["status"],
                 "pid": pid, "cpu": 0, "memory": 0,
                 "disk_read": 0, "disk_write": 0,
                 "net_sent": 0, "net_recv": 0, "connections": 0,
                 "net_supported": _NET_ESTIMATED, "net_estimated": _NET_ESTIMATED}
        if pid:
            try:
                p = psutil.Process(pid)
                with p.oneshot():
                    entry["cpu"] = p.cpu_percent(interval=None)
                    entry["memory"] = p.memory_info().rss
                try:
                    for c in p.children(recursive=True):
                        try:
                            entry["cpu"] += c.cpu_percent(interval=None)
                            entry["memory"] += c.memory_info().rss
                        except Exception: pass
                except Exception: pass
            except Exception: pass
            r_rate, w_rate = _task_disk_rate(pid, tid)
            entry["disk_read"], entry["disk_write"] = r_rate, w_rate
            conns = conns_map.get(tid, 0)
            entry["connections"] = conns
            if _NET_ESTIMATED and total_conns > 0 and conns > 0:
                ratio = conns / total_conns
                entry["net_sent"] = int(result["net"]["sent_rate"] * ratio)
                entry["net_recv"] = int(result["net"]["recv_rate"] * ratio)
        result["tasks"].append(entry)
    return result


perf_broadcaster = PerfBroadcaster(socketio, store)


# ============================================================
# 路由
# ============================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        is_first = store.data.get('auth') is None
        return render_template('login.html', is_first=is_first,
                               title=store.get_setting('panel_title', 'PSh Panel'))
    ip = login_guard.get_client_ip()
    locked, unlock_ts = login_guard.is_locked(ip)
    if locked:
        wait = int(unlock_ts - time.time())
        return jsonify({"error": f"尝试次数过多，请等待 {wait} 秒后重试"}), 429
    delay = login_guard.backoff_delay(ip)
    if delay > 0: time.sleep(min(delay, 3))

    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    min_len = int(store.get_setting('min_password_len', 8))

    if store.data.get('auth') is None:
        ok, msg = password_strength(password, min_len)
        if not ok: return jsonify({"error": msg}), 400
        with store.lock:
            store.data['auth'] = hash_password(password)
            store.save()
        session.permanent = True; session['auth'] = True
        login_guard.record_success(ip)
        return jsonify({"ok": True, "is_first": True})

    if verify_password(password, store.data['auth']):
        session.permanent = True; session['auth'] = True
        login_guard.record_success(ip)
        return jsonify({"ok": True})

    just_locked, unlock_ts = login_guard.record_fail(ip)
    if just_locked:
        wait = int(unlock_ts - time.time())
        return jsonify({"error": f"尝试次数过多，账号已锁定 {wait} 秒"}), 429
    return jsonify({"error": "密码错误"}), 401


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route('/')
@login_required
def index():
    return render_template('base.html', title=store.get_setting('panel_title', 'PSh Panel'))


@app.route('/page/<name>')
@login_required
def page(name):
    allowed = {'home', 'terminal', 'files', 'performance', 'settings'}
    if name not in allowed: abort(404)
    return render_template(f'{name}.html')


# ---------- 任务 API ----------
@app.route('/api/tasks', methods=['GET', 'POST'])
@login_required
def api_tasks():
    if request.method == 'GET':
        return jsonify(task_manager.list_tasks())
    data = request.get_json() or {}
    if not data.get('command'):
        return jsonify({"error": "command required"}), 400
    return jsonify(task_manager.create_task(data))


@app.route('/api/tasks/<tid>', methods=['PUT', 'DELETE', 'GET'])
@login_required
def api_task(tid):
    if request.method == 'GET':
        t = task_manager.get_task(tid)
        if not t: abort(404)
        return jsonify(task_manager._public(t))
    if request.method == 'PUT':
        t = task_manager.update_task(tid, request.get_json() or {})
        if not t: abort(404)
        return jsonify(t)
    task_manager.delete_task(tid)
    return jsonify({"ok": True})


@app.route('/api/tasks/<tid>/<action>', methods=['POST'])
@login_required
def api_task_action(tid, action):
    if action == 'start':       return jsonify(task_manager.start(tid))
    if action == 'stop':        return jsonify(task_manager.stop(tid, force=False))
    if action == 'force-stop':  return jsonify(task_manager.stop(tid, force=True))
    if action == 'restart':     return jsonify(task_manager.restart(tid, force=False))
    if action == 'force-restart': return jsonify(task_manager.restart(tid, force=True))
    abort(404)


@app.route('/api/tasks/<tid>/logs')
@login_required
def api_task_logs(tid):
    tail = int(request.args.get('tail', 1000))
    after = request.args.get('after')
    after = int(after) if after is not None else None
    return jsonify(task_manager.get_logs(tid, tail=tail, after=after))


@app.route('/api/tasks/<tid>/history')
@login_required
def api_task_history(tid):
    with store.lock:
        hist = store.data.get("history", {}).get(tid, [])
    return jsonify(hist)


@app.route('/api/tasks/<tid>/env-log')
@login_required
def api_task_env_log(tid):
    rt = task_manager.runtimes.get(tid)
    if not rt:
        return jsonify({"status": "none", "vars": {}, "lines": [],
                        "exit_code": None, "latest_seq": 0})
    with rt.lock:
        lines = [{"seq": s, "stream": st, "data": d} for s, st, d in rt.env_buffer]
    return jsonify({"status": rt.env_status, "vars": rt.env_vars,
                    "exit_code": rt.env_exit_code, "lines": lines,
                    "latest_seq": rt.env_seq})


@app.route('/api/tasks/<tid>/git-pull', methods=['POST'])
@login_required
def api_task_git_pull(tid):
    task = task_manager.get_task(tid)
    if not task: return jsonify({"error": "task not found"}), 404
    rt = task_manager.runtimes.get(tid)
    if rt and rt.status == 'running':
        return jsonify({"error": "任务正在运行，请先停止"}), 400
    cwd = task.get("cwd") or str(BASE_DIR)
    if not os.path.isdir(cwd):
        return jsonify({"error": f"工作目录不存在: {cwd}"}), 400
    if not os.path.isdir(os.path.join(cwd, '.git')):
        return jsonify({"error": "该目录不是 git 仓库"}), 400

    with _git_lock:
        if tid in _git_sessions:
            return jsonify({"error": "已有 git pull 在执行"}), 409
        _git_sessions[tid] = {"running": True}

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["TERM"] = "xterm-256color"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"

    try:
        if IS_WINDOWS:
            if PtyProcess is None:
                with _git_lock: _git_sessions.pop(tid, None)
                return jsonify({"error": "pywinpty 未安装"}), 500
            wrapped = f'cmd.exe /c "cd /d "{cwd}" && git pull"'
            proc = PtyProcess.spawn(wrapped, dimensions=(30, 120), env=env)
            threading.Thread(target=_git_reader_win, args=(tid, proc), daemon=True).start()
        else:
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(cwd)
                    os.execvpe('/bin/sh', ['/bin/sh', '-c', 'git pull'], env)
                except Exception: os._exit(127)
            proc = {"pid": pid, "fd": fd}
            threading.Thread(target=_git_reader_posix, args=(tid, proc), daemon=True).start()
        return jsonify({"ok": True, "started": True})
    except FileNotFoundError as e:
        with _git_lock: _git_sessions.pop(tid, None)
        return jsonify({"error": f"系统未安装 git: {e}"}), 500
    except Exception as e:
        with _git_lock: _git_sessions.pop(tid, None)
        return jsonify({"error": str(e)}), 500


@app.route('/api/tasks/<tid>/git-kill', methods=['POST'])
@login_required
def api_task_git_kill(tid):
    with _git_lock:
        sess = _git_sessions.get(tid)
        if not sess: return jsonify({"ok": True})
        proc = sess.get("proc")
    if not proc: return jsonify({"ok": True})
    try:
        if IS_WINDOWS:
            try: proc.write('\x03')
            except Exception: pass
            time.sleep(0.3)
            try: getattr(proc, 'kill', proc.close)()
            except Exception: pass
        else:
            try: os.killpg(os.getpgid(proc["pid"]), signal.SIGTERM)
            except Exception: pass
    except Exception: pass
    return jsonify({"ok": True})


# ---------- 文件 API ----------
@app.route('/api/files/roots')
@login_required
def api_file_roots():
    return jsonify({"roots": list_roots(),
                    "platform": "windows" if IS_WINDOWS else "linux",
                    "home": str(Path.home())})


@app.route('/api/files/list')
@login_required
def api_file_list():
    resolved = resolve_path(request.args.get('path', ''))
    if not resolved: return jsonify({"error": "invalid path"}), 400
    if not os.path.exists(resolved): return jsonify({"error": "not found"}), 404
    if os.path.isfile(resolved):
        return jsonify({"file": file_info(Path(resolved))})
    result = list_dir(resolved)
    if not result: return jsonify({"error": "not a directory"}), 400
    return jsonify(result)


@app.route('/api/files/download')
@login_required
def api_file_download():
    path = resolve_path(request.args.get('path', ''))
    if not path or not os.path.isfile(path): abort(404)
    return send_file(path, as_attachment=True)


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_file_upload():
    directory = resolve_path(request.form.get('path', ''))
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "invalid dir"}), 400
    results = []
    for f in request.files.getlist('files'):
        if not f.filename: continue
        target = Path(directory) / Path(f.filename).name
        f.save(str(target)); results.append(str(target))
    return jsonify({"ok": True, "files": results})


@app.route('/api/files/mkdir', methods=['POST'])
@login_required
def api_file_mkdir():
    data = request.get_json() or {}
    parent = resolve_path(data.get('path', '')); name = data.get('name', '').strip()
    if not parent or not name or '/' in name or '\\' in name:
        return jsonify({"error": "invalid"}), 400
    target = Path(parent) / name
    if target.exists(): return jsonify({"error": "exists"}), 400
    target.mkdir(parents=True)
    return jsonify({"ok": True, "path": str(target)})


@app.route('/api/files/newfile', methods=['POST'])
@login_required
def api_file_newfile():
    data = request.get_json() or {}
    parent = resolve_path(data.get('path', '')); name = data.get('name', '').strip()
    if not parent or not name or '/' in name or '\\' in name:
        return jsonify({"error": "invalid"}), 400
    target = Path(parent) / name
    if target.exists(): return jsonify({"error": "exists"}), 400
    target.touch()
    return jsonify({"ok": True, "path": str(target)})


@app.route('/api/files/rename', methods=['POST'])
@login_required
def api_file_rename():
    data = request.get_json() or {}
    src = resolve_path(data.get('path', '')); new_name = data.get('name', '').strip()
    if not src or not new_name or '/' in new_name or '\\' in new_name:
        return jsonify({"error": "invalid"}), 400
    src_p = Path(src); dst = src_p.parent / new_name
    if dst.exists(): return jsonify({"error": "exists"}), 400
    src_p.rename(dst)
    return jsonify({"ok": True, "path": str(dst)})


@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_file_delete():
    data = request.get_json() or {}
    path = resolve_path(data.get('path', ''))
    if not path: return jsonify({"error": "invalid"}), 400
    norm = path.rstrip('/\\') or path
    for p in protected_paths():
        if norm.lower() == p.lower().rstrip('/\\'):
            return jsonify({"error": "refused: protected path"}), 403
    if len(norm) <= 3 and not IS_WINDOWS:
        return jsonify({"error": "refused"}), 403
    p = Path(path)
    if not p.exists(): return jsonify({"error": "not found"}), 404
    if p.is_dir(): shutil.rmtree(str(p))
    else: p.unlink()
    return jsonify({"ok": True})


@app.route('/api/files/read')
@login_required
def api_file_read():
    path = resolve_path(request.args.get('path', ''))
    if not path or not os.path.isfile(path): abort(404)
    if os.path.getsize(path) > 2 * 1024 * 1024:
        return jsonify({"error": "file too large"}), 413
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return jsonify({"content": f.read(), "path": path})
    except UnicodeDecodeError:
        return jsonify({"error": "binary file"}), 415


@app.route('/api/files/write', methods=['POST'])
@login_required
def api_file_write():
    data = request.get_json() or {}
    path = resolve_path(data.get('path', ''))
    if not path: return jsonify({"error": "invalid"}), 400
    with open(path, 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({"ok": True})


# ---------- 性能 / 设置 / 密码 ----------
@app.route('/api/perf')
@login_required
def api_perf():
    with _perf_call_lock:
        return jsonify(get_perf())


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    if request.method == 'GET':
        return jsonify(store.data['settings'])
    data = request.get_json() or {}
    with store.lock:
        for k in ('scrollback', 'font_size', 'max_history', 'panel_title',
                  'perf_interval', 'stop_timeout', 'python_path',
                  'session_days', 'max_login_attempts', 'lockout_minutes'):
            if k in data: store.data['settings'][k] = data[k]
        store.save()
    app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * int(store.get_setting('session_days', 7))
    return jsonify(store.data['settings'])


@app.route('/api/password', methods=['POST'])
@login_required
def api_password():
    data = request.get_json() or {}
    old, new = data.get('old', ''), data.get('new', '')
    if not verify_password(old, store.data.get('auth')):
        return jsonify({"error": "旧密码错误"}), 401
    min_len = int(store.get_setting('min_password_len', 8))
    ok, msg = password_strength(new, min_len)
    if not ok: return jsonify({"error": msg}), 400
    with store.lock:
        store.data['auth'] = hash_password(new)
        store.save()
    return jsonify({"ok": True})


@app.route('/api/python-check', methods=['POST'])
@login_required
def api_python_check():
    data = request.get_json() or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({"ok": True, "version": "", "message": "已清空"})
    try:
        r = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
        out = (r.stdout or r.stderr or '').strip()
        if r.returncode == 0:
            return jsonify({"ok": True, "version": out})
        return jsonify({"ok": False, "error": out or '非零退出码'})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "路径不存在或不可执行"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "执行超时"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route('/api/system')
@login_required
def api_system():
    try:
        import socketio as _pysio
        py_sio_ver = getattr(_pysio, '__version__', '?')
    except Exception: py_sio_ver = '?'
    try:
        import flask_socketio as _fsio
        flask_sio_ver = getattr(_fsio, '__version__', '?')
    except Exception: flask_sio_ver = '?'
    return jsonify({
        "platform": "windows" if IS_WINDOWS else ("darwin" if sys.platform == "darwin" else "linux"),
        "python": sys.version.split()[0],
        "default_shell": default_shell(),
        "has_pty": (PtyProcess is not None) if IS_WINDOWS else True,
        "has_psutil": psutil is not None,
        "py_socketio": py_sio_ver,
        "flask_socketio": flask_sio_ver,
    })


@app.route('/api/login-status')
def api_login_status():
    ip = login_guard.get_client_ip()
    locked, unlock = login_guard.is_locked(ip)
    return jsonify({"locked": locked,
                    "unlock_in": max(0, int(unlock - time.time())) if locked else 0})


# ============================================================
# SocketIO 事件
# ============================================================
@socketio.on('connect')
def sio_connect():
    if not session.get('auth'): return False


@socketio.on('task:subscribe')
def sio_task_subscribe(data):
    tid = data.get('task_id')
    if not tid: return
    join_room(f"task:{tid}")
    snap = task_manager.get_logs(tid)
    emit('task:snapshot', {'task_id': tid, **snap})


@socketio.on('task:unsubscribe')
def sio_task_unsubscribe(data):
    tid = data.get('task_id')
    if tid: leave_room(f"task:{tid}")


@socketio.on('task:input')
def sio_task_input(data):
    tid = data.get('task_id'); text = data.get('data', '')
    rt = task_manager.runtimes.get(tid)
    if not rt or rt.status != 'running' or rt.mode != 'pty': return
    try: pty_write(rt.process, text)
    except Exception: pass


@socketio.on('task:resize')
def sio_task_resize(data):
    tid = data.get('task_id')
    rows = _safe_int(data.get('rows'), 30, 2, 500)
    cols = _safe_int(data.get('cols'), 120, 2, 1000)
    rt = task_manager.runtimes.get(tid)
    if not rt or rt.mode != 'pty' or not rt.process: return
    try: pty_resize(rt.process, rows, cols)
    except Exception: pass


@socketio.on('task:env-subscribe')
def sio_task_env_subscribe(data):
    tid = data.get('task_id')
    if not tid: return
    join_room(f"task-env:{tid}")
    rt = task_manager.runtimes.get(tid)
    if not rt:
        emit('task:env-snapshot', {'task_id': tid, 'status': 'none',
                                    'vars': {}, 'lines': [], 'latest_seq': 0})
        return
    with rt.lock:
        lines = [{"seq": s, "stream": st, "data": d} for s, st, d in rt.env_buffer]
    emit('task:env-snapshot', {
        'task_id': tid, 'status': rt.env_status, 'vars': rt.env_vars,
        'exit_code': rt.env_exit_code, 'lines': lines, 'latest_seq': rt.env_seq,
    })


@socketio.on('task:env-unsubscribe')
def sio_task_env_unsubscribe(data):
    tid = data.get('task_id')
    if tid: leave_room(f"task-env:{tid}")


@socketio.on('git:subscribe')
def sio_git_subscribe(data):
    tid = data.get('task_id')
    if tid: join_room(f"git:{tid}")


@socketio.on('git:unsubscribe')
def sio_git_unsubscribe(data):
    tid = data.get('task_id')
    if tid: leave_room(f"git:{tid}")


@socketio.on('perf:subscribe')
def sio_perf_subscribe():
    join_room('perf')
    perf_broadcaster.subscribe(request.sid)
    try: emit('perf:data', get_perf())
    except Exception: pass


@socketio.on('perf:unsubscribe')
def sio_perf_unsubscribe():
    leave_room('perf')
    perf_broadcaster.unsubscribe(request.sid)


@socketio.on('term:create')
def sio_term_create(data):
    term_id = uuid.uuid4().hex[:12]
    command = data.get('command') or default_shell()
    cwd = data.get('cwd') or None
    sess = TerminalSession(term_id)
    try: sess.spawn(command, cwd=cwd)
    except Exception as e:
        emit('term:error', {'error': str(e), 'term_id': term_id}); return
    sessions[term_id] = sess
    join_room(f"term:{term_id}")
    emit('term:created', {'term_id': term_id, 'command': command,
                          'cwd': cwd or str(BASE_DIR)})


@socketio.on('term:join')
def sio_term_join(data):
    tid = data.get('term_id')
    if tid in sessions:
        join_room(f"term:{tid}")
        with sessions[tid].lock:
            for seq, chunk in sessions[tid].buffer:
                emit('term:output', {'term_id': tid, 'data': chunk})


@socketio.on('term:leave')
def sio_term_leave(data):
    tid = data.get('term_id')
    if tid: leave_room(f"term:{tid}")


@socketio.on('term:input')
def sio_term_input(data):
    sess = sessions.get(data.get('term_id'))
    if sess: sess.write(data.get('data', ''))


@socketio.on('term:resize')
def sio_term_resize(data):
    sess = sessions.get(data.get('term_id'))
    if sess:
        sess.resize(_safe_int(data.get('rows'), 30, 2, 500),
                    _safe_int(data.get('cols'), 120, 2, 1000))


@socketio.on('term:close')
def sio_term_close(data):
    tid = data.get('term_id')
    sess = sessions.pop(tid, None)
    if sess: threading.Thread(target=sess.close, daemon=True).start()


@socketio.on('disconnect')
def sio_disconnect():
    perf_broadcaster.unsubscribe(request.sid)


# ============================================================
# 自动启动
# ============================================================
def autostart_tasks():
    for task in task_manager.list_tasks():
        if task.get('enabled') and task['status'] != 'running':
            try: task_manager.start(task['id'])
            except Exception as e: print(f"[autostart] {task['name']}: {e}")


# ============================================================
# 入口
# ============================================================
if __name__ == '__main__':
    port = find_free_port(5000)
    print("=" * 56)
    print("  PSh Panel")
    print(f"  Platform: {'Windows' if IS_WINDOWS else sys.platform}")
    print(f"  PTY backend: {'pywinpty' if IS_WINDOWS else 'pty (stdlib)'}")
    print(f"  http://0.0.0.0:{port}")
    print(f"  Data: {STORE_FILE}")
    print("=" * 56)
    perf_broadcaster.start()
    def _delayed_start():
        time.sleep(2)
        autostart_tasks()
    threading.Thread(target=_delayed_start, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=port, debug=False,
                 allow_unsafe_werkzeug=True)