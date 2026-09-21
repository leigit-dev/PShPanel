#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PSh Panel - 跨平台服务器控制面板"""
import os, sys, json, uuid, time, hmac, hashlib, secrets, threading
import subprocess, shutil, signal, string, socket as _socket
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
# PTY 跨平台辅助
# ============================================================
def pty_read(proc, size=4096):
    if IS_WINDOWS:
        try:
            return proc.read(size)
        except TypeError:
            return proc.read()
    return os.read(proc['fd'], size)


def pty_write(proc, data):
    if IS_WINDOWS:
        if isinstance(data, bytes):
            data = data.decode('utf-8', errors='replace')
        proc.write(data)
    else:
        if isinstance(data, str):
            data = data.encode('utf-8')
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
                s.bind(('0.0.0.0', p))
                return p
            except OSError:
                continue
    return start


def _safe_int(v, default, lo, hi):
    """把前端传来的任意值安全转成 [lo, hi] 内的 int。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# ============================================================
# 存储层
# ============================================================
class Store:
    def __init__(self, path: Path):
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
                "scrollback": 5000,
                "font_size": 14,
                "max_history": 10,
                "panel_title": "PSh Panel",
                "perf_interval": 2000,
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

    @property
    def data(self): return self._data

    @property
    def lock(self): return self._lock


store = Store(STORE_FILE)


# ============================================================
# 认证
# ============================================================
PBKDF2_ITER = 260_000


def hash_password(password: str):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITER)
    return {"salt": salt.hex(), "hash": h.hex(), "iterations": PBKDF2_ITER}


def verify_password(password: str, record: dict) -> bool:
    if not record:
        return False
    salt = bytes.fromhex(record["salt"])
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                            salt, record.get("iterations", PBKDF2_ITER))
    return hmac.compare_digest(h.hex(), record["hash"])


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
# Flask / SocketIO
# ============================================================
app = Flask(__name__)
app.config['SECRET_KEY'] = store.data.get('secret_key') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*',
                    max_http_buffer_size=50 * 1024 * 1024)


# ============================================================
# 任务管理器
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


class TaskManager:
    def __init__(self, store, socketio):
        self.store = store
        self.socketio = socketio
        self.runtimes = {}
        self.lock = threading.RLock()

    def list_tasks(self):
        with self.store.lock:
            tasks = self.store.data.get("tasks", {})
            return [self._public(t) for t in tasks.values()]

    def get_task(self, task_id):
        with self.store.lock:
            return self.store.data.get("tasks", {}).get(task_id)

    def _public(self, task):
        rt = self.runtimes.get(task["id"])
        return {
            **task,
            "status": rt.status if rt else "stopped",
            "exit_code": rt.exit_code if rt else None,
            "started_at": rt.started_at if rt else None,
            "pid": self._pid(task["id"]),
        }

    def _pid(self, task_id):
        rt = self.runtimes.get(task_id)
        if not rt or rt.status != 'running' or not rt.process:
            return None
        if rt.mode == 'pipe':
            return rt.process.pid
        if IS_WINDOWS:
            return getattr(rt.process, 'pid', None)
        return rt.process["pid"]

    def create_task(self, data):
        task_id = str(uuid.uuid4())[:8]
        task = {
            "id": task_id,
            "name": data.get("name", "Untitled"),
            "command": data.get("command", ""),
            "cwd": data.get("cwd") or str(BASE_DIR),
            "mode": data.get("mode", "pipe"),
            "enabled": bool(data.get("enabled", False)),
            "created_at": time.time(),
        }
        with self.store.lock:
            self.store.data["tasks"][task_id] = task
            self.store.save()
        return task

    def update_task(self, task_id, data):
        with self.store.lock:
            task = self.store.data["tasks"].get(task_id)
            if not task:
                return None
            for k in ("name", "command", "cwd", "mode", "enabled"):
                if k in data:
                    task[k] = data[k]
            self.store.save()
            return task

    def delete_task(self, task_id):
        self.stop(task_id)
        with self.store.lock:
            self.store.data["tasks"].pop(task_id, None)
            self.store.data.get("history", {}).pop(task_id, None)
            self.store.save()

    def start(self, task_id):
        task = self.get_task(task_id)
        if not task:
            return {"error": "task not found"}
        with self.lock:
            rt = self.runtimes.get(task_id)
            if rt and rt.status == 'running':
                return {"error": "already running"}
            rt = TaskRuntime(task_id)
            rt.mode = task["mode"]
            rt.buffer = deque(maxlen=self.store.data["settings"]["scrollback"])
            self.runtimes[task_id] = rt
            try:
                self._spawn(rt, task)
                rt.status = 'running'
                rt.started_at = time.time()
                self.socketio.emit('task:status', {
                    'task_id': task_id, 'status': 'running',
                    'started_at': rt.started_at, 'pid': self._pid(task_id),
                })
                return {"ok": True}
            except Exception as e:
                rt.status = 'exited'
                rt.exit_code = -1
                self._emit_output(task_id, f"\r\n[启动失败] {e}\r\n")
                self.socketio.emit('task:status', {
                    'task_id': task_id, 'status': 'exited', 'exit_code': -1,
                })
                return {"error": str(e)}

    def _spawn(self, rt, task):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["TERM"] = "xterm-256color"
        cwd = task.get("cwd") or str(BASE_DIR)
        cmd = task["command"]
        if not cmd:
            raise RuntimeError("命令为空")

        if task["mode"] == "pty":
            self._spawn_pty(rt, cmd, cwd, env)
        else:
            self._spawn_pipe(rt, cmd, cwd, env)
        rt.reader.start()

    def _spawn_pty(self, rt, cmd, cwd, env):
        if IS_WINDOWS:
            if PtyProcess is None:
                raise RuntimeError("pywinpty 未安装")
            try:
                proc = PtyProcess.spawn(cmd, dimensions=(30, 120),
                                        env=env, cwd=cwd)
            except TypeError:
                proc = PtyProcess.spawn(cmd, dimensions=(30, 120), env=env)
            rt.process = proc
            rt.reader = threading.Thread(
                target=self._read_pty_win, args=(rt,), daemon=True)
        else:
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(cwd)
                    os.execvpe('/bin/sh', ['/bin/sh', '-c', cmd], env)
                except Exception:
                    os._exit(127)
            rt.process = {"pid": pid, "fd": fd}
            rt.reader = threading.Thread(
                target=self._read_pty_posix, args=(rt,), daemon=True)

    def _spawn_pipe(self, rt, cmd, cwd, env):
        kwargs = dict(
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            bufsize=0,
        )
        if IS_POSIX:
            kwargs["preexec_fn"] = os.setsid
        proc = subprocess.Popen(cmd, **kwargs)
        rt.process = proc
        rt.reader = threading.Thread(
            target=self._read_pipe, args=(rt,), daemon=True)

    def stop(self, task_id, timeout=5):
        with self.lock:
            rt = self.runtimes.get(task_id)
            if not rt or rt.status != 'running':
                return {"ok": True}
            rt.stopping = True
            try:
                if rt.mode == 'pty':
                    self._kill_pty(rt)
                else:
                    self._kill_pipe(rt)
            finally:
                rt.status = 'exited'
                self.socketio.emit('task:status', {
                    'task_id': task_id, 'status': 'exited',
                    'exit_code': rt.exit_code,
                })
            return {"ok": True}

    def _kill_pty(self, rt):
        if IS_WINDOWS:
            try: rt.process.write('\x03')
            except Exception: pass
            time.sleep(0.3)
            try: rt.process.close()
            except Exception: pass
        else:
            try: os.kill(rt.process["pid"], signal.SIGTERM)
            except Exception: pass
            time.sleep(0.3)
            try: os.kill(rt.process["pid"], signal.SIGKILL)
            except Exception: pass

    def _kill_pipe(self, rt):
        proc = rt.process
        if IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except Exception:
                try: proc.kill()
                except Exception: pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try: proc.terminate()
                except Exception: pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try: proc.kill()
                    except Exception: pass

    def restart(self, task_id):
        self.stop(task_id)
        time.sleep(0.5)
        return self.start(task_id)

    def _emit_output(self, task_id, text):
        rt = self.runtimes.get(task_id)
        if not rt:
            return
        with rt.lock:
            rt.seq += 1
            seq = rt.seq
            rt.buffer.append((seq, text))
        self.socketio.emit('task:output', {
            'task_id': task_id, 'seq': seq, 'data': text,
        }, room=f"task:{task_id}")

    def _read_pty_win(self, rt):
        proc = rt.process
        try:
            while True:
                try:
                    data = pty_read(proc, 4096)
                    if data:
                        if isinstance(data, bytes):
                            data = data.decode('utf-8', errors='replace')
                        self._emit_output(rt.task_id, data)
                    elif not proc.isalive():
                        break
                except EOFError:
                    break
                except Exception as e:
                    if not rt.stopping:
                        self._emit_output(rt.task_id,
                                          f"\r\n[读取错误] {e}\r\n")
                    break
        finally:
            code = -1
            try:
                proc.wait()
                code = proc.exitstatus
            except Exception:
                pass
            try: proc.close()
            except Exception: pass
            self._finalize(rt, code)

    def _read_pty_posix(self, rt):
        fd = rt.process["fd"]
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    self._emit_output(rt.task_id,
                                      data.decode('utf-8', errors='replace'))
        finally:
            try: os.close(fd)
            except Exception: pass
            code = -1
            try:
                _, status = os.waitpid(rt.process["pid"], 0)
                if hasattr(os, 'waitstatus_to_exitcode'):
                    code = os.waitstatus_to_exitcode(status)
                elif os.WIFEXITED(status):
                    code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    code = -os.WTERMSIG(status)
            except Exception:
                pass
            self._finalize(rt, code)

    def _read_pipe(self, rt):
        proc = rt.process
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self._emit_output(rt.task_id,
                                  chunk.decode('utf-8', errors='replace'))
        finally:
            code = -1
            try:
                proc.wait(timeout=2)
                code = proc.returncode
            except Exception:
                pass
            self._finalize(rt, code)

    def _finalize(self, rt, code):
        rt.exit_code = code
        rt.status = 'exited'
        self.socketio.emit('task:status', {
            'task_id': rt.task_id, 'status': 'exited', 'exit_code': code,
        })
        if rt.started_at:
            try:
                with self.store.lock:
                    hist = self.store.data.setdefault("history", {}) \
                        .setdefault(rt.task_id, [])
                    max_h = self.store.data["settings"]["max_history"]
                    tail = [(s, d) for s, d in rt.buffer][-500:]
                    hist.append({
                        "started_at": rt.started_at,
                        "ended_at": time.time(),
                        "exit_code": code,
                        "tail": [{"seq": s, "data": d} for s, d in tail],
                    })
                    if len(hist) > max_h:
                        hist[:] = hist[-max_h:]
                    self.store.save()
            except Exception:
                pass

    def get_logs(self, task_id, tail=1000, after=None):
        rt = self.runtimes.get(task_id)
        if not rt:
            with self.store.lock:
                hist = self.store.data.get("history", {}).get(task_id, [])
                if hist:
                    last = hist[-1]
                    return {
                        "lines": last["tail"],
                        "latest_seq": last["tail"][-1]["seq"] if last["tail"] else 0,
                    }
            return {"lines": [], "latest_seq": 0}
        with rt.lock:
            if after is not None:
                lines = [(s, d) for s, d in rt.buffer if s > after]
            else:
                lines = list(rt.buffer)[-tail:]
            return {
                "lines": [{"seq": s, "data": d} for s, d in lines],
                "latest_seq": rt.seq,
            }


task_manager = TaskManager(store, socketio)
# ============================================================
# 性能广播（WS 推送）
# ============================================================
class PerfBroadcaster:
    def __init__(self, socketio, store):
        self.socketio = socketio
        self.store = store
        self._lock = threading.Lock()
        self._subscribers = set()
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def subscribe(self, sid):
        with self._lock:
            self._subscribers.add(sid)

    def unsubscribe(self, sid):
        with self._lock:
            self._subscribers.discard(sid)

    def _is_empty(self):
        with self._lock:
            return not self._subscribers

    def _loop(self):
        while not self._stop.is_set():
            try:
                interval = float(self.store.data['settings'].get('perf_interval', 2000)) / 1000.0
            except Exception:
                interval = 2.0
            interval = max(0.5, min(interval, 30.0))

            if self._is_empty():
                # 无订阅者：低频空转，避免浪费
                self._stop.wait(1.0)
                continue

            try:
                data = get_perf()
                self.socketio.emit('perf:data', data, room='perf')
            except Exception as e:
                print(f"[perf] {e}")
            self._stop.wait(interval)


perf_broadcaster = PerfBroadcaster(socketio, store)

# ============================================================
# 临时终端会话
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

    def spawn(self, command, cwd=None):
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        cwd = cwd or str(BASE_DIR)

        if IS_WINDOWS:
            if PtyProcess is None:
                raise RuntimeError("pywinpty 未安装")
            try:
                self.proc = PtyProcess.spawn(command, dimensions=(30, 120),
                                             env=env, cwd=cwd)
            except TypeError:
                self.proc = PtyProcess.spawn(command, dimensions=(30, 120),
                                             env=env)
            self.thread = threading.Thread(target=self._read_win, daemon=True)
        else:
            pid, fd = pty.fork()
            if pid == 0:
                try:
                    os.chdir(cwd)
                    os.execvpe('/bin/sh', ['/bin/sh', '-c', command], env)
                except Exception:
                    os._exit(127)
            self.proc = {"pid": pid, "fd": fd}
            self.thread = threading.Thread(target=self._read_posix, daemon=True)
        self.thread.start()

    def _emit(self, data):
        with self.lock:
            self.seq += 1
            self.buffer.append((self.seq, data))
        if self.alive:
            socketio.emit('term:output',
                          {'term_id': self.term_id, 'data': data},
                          room=f"term:{self.term_id}")

    def _read_win(self):
        try:
            while True:
                try:
                    data = pty_read(self.proc, 4096)
                    if data:
                        if isinstance(data, bytes):
                            data = data.decode('utf-8', errors='replace')
                        self._emit(data)
                    elif not self.proc.isalive():
                        break
                except EOFError:
                    break
                except Exception:
                    break
        finally:
            code = -1
            try:
                self.proc.wait()
                code = self.proc.exitstatus
            except Exception:
                pass
            try: self.proc.close()
            except Exception: pass
            self._finalize(code)

    def _read_posix(self):
        fd = self.proc["fd"]
        try:
            while True:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    self._emit(data.decode('utf-8', errors='replace'))
        finally:
            try: os.close(fd)
            except Exception: pass
            code = -1
            try:
                _, status = os.waitpid(self.proc["pid"], 0)
                if hasattr(os, 'waitstatus_to_exitcode'):
                    code = os.waitstatus_to_exitcode(status)
                elif os.WIFEXITED(status):
                    code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    code = -os.WTERMSIG(status)
            except Exception:
                pass
            self._finalize(code)

    def _finalize(self, code):
        if self.alive:
            socketio.emit('term:exit',
                          {'term_id': self.term_id, 'code': code},
                          room=f"term:{self.term_id}")
        sessions.pop(self.term_id, None)

    def write(self, data):
        if not self.proc:
            return
        try: pty_write(self.proc, data)
        except Exception: pass

    def resize(self, rows, cols):
        if not self.proc:
            return
        try: pty_resize(self.proc, rows, cols)
        except Exception: pass

    def close(self):
        self.alive = False
        try:
            if IS_WINDOWS:
                try: self.proc.close()
                except Exception: pass
            else:
                try: os.kill(self.proc["pid"], signal.SIGTERM)
                except Exception: pass
        except Exception:
            pass


sessions = {}


# ============================================================
# 文件管理器
# ============================================================
def list_roots():
    roots = []
    if IS_WINDOWS:
        if psutil:
            try:
                for p in psutil.disk_partitions(all=False):
                    roots.append({
                        "name": p.device, "path": p.device,
                        "type": p.fstype or "drive", "is_root": True,
                    })
            except Exception:
                pass
        if not roots:
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    roots.append({"name": drive, "path": drive,
                                  "type": "drive", "is_root": True})
    else:
        home = str(Path.home())
        roots.append({"name": "~", "path": home, "type": "home", "is_root": True})
        roots.append({"name": "/", "path": "/", "type": "root", "is_root": True})
        seen = {"~", "/", home}
        if psutil:
            try:
                for p in psutil.disk_partitions(all=False):
                    mp = p.mountpoint
                    if mp in seen:
                        continue
                    seen.add(mp)
                    roots.append({
                        "name": mp, "path": mp,
                        "type": p.fstype or "mount", "is_root": True,
                    })
            except Exception:
                pass
    return roots


def resolve_path(path):
    if not path:
        return None
    try:
        p = os.path.expanduser(path)
        return str(Path(p).resolve())
    except Exception:
        return None


def file_info(path: Path):
    try:
        st = path.stat()
        is_dir = path.is_dir()
        return {
            "name": path.name or str(path),
            "path": str(path),
            "is_dir": is_dir,
            "size": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
            "mode": oct(st.st_mode & 0o777),
        }
    except Exception as e:
        return {
            "name": path.name, "path": str(path), "is_dir": False,
            "size": 0, "mtime": 0, "mode": "000", "error": str(e),
        }


def list_dir(path):
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return None
    entries = []
    try:
        for child in p.iterdir():
            entries.append(file_info(child))
    except PermissionError:
        return {"error": "permission denied", "entries": []}
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "entries": entries,
    }


def protected_paths():
    if IS_WINDOWS:
        return {
            "C:\\", "C:/", "C:",
            "C:\\Windows", "C:/Windows",
            "C:\\Program Files", "C:/Program Files",
            "C:\\Program Files (x86)", "C:/Program Files (x86)",
        }
    return {
        "/", "/home", "/root", "/etc", "/usr", "/var", "/bin",
        "/sbin", "/boot", "/lib", "/lib64", "/opt", "/proc", "/sys",
        "/dev", "/run", "/tmp",
    }


# ============================================================
# 性能监控
# ============================================================
_perf_last = {"net": None, "time": None}


def get_perf():
    result = {
        "cpu": {"percent": 0, "per_cpu": [], "count": 0, "freq": None},
        "memory": {"total": 0, "used": 0, "percent": 0,
                   "swap_total": 0, "swap_used": 0},
        "net": {"sent_rate": 0, "recv_rate": 0,
                "sent_total": 0, "recv_total": 0},
        "disk": {"total": 0, "used": 0, "percent": 0},
        "uptime": 0,
        "tasks": [],
    }
    if not psutil:
        return result

    result["cpu"]["percent"] = psutil.cpu_percent(interval=None)
    result["cpu"]["per_cpu"] = psutil.cpu_percent(interval=None, percpu=True)
    result["cpu"]["count"] = psutil.cpu_count()
    try:
        f = psutil.cpu_freq()
        if f: result["cpu"]["freq"] = f.current
    except Exception: pass

    m = psutil.virtual_memory()
    result["memory"] = {
        "total": m.total, "used": m.used, "percent": m.percent,
        "available": m.available,
    }
    try:
        s = psutil.swap_memory()
        result["memory"]["swap_total"] = s.total
        result["memory"]["swap_used"] = s.used
    except Exception: pass

    try:
        io = psutil.net_io_counters()
        now = time.time()
        if _perf_last["net"] and _perf_last["time"]:
            dt = now - _perf_last["time"]
            if dt > 0:
                result["net"]["sent_rate"] = (io.bytes_sent - _perf_last["net"].bytes_sent) / dt
                result["net"]["recv_rate"] = (io.bytes_recv - _perf_last["net"].bytes_recv) / dt
        _perf_last["net"] = io
        _perf_last["time"] = now
        result["net"]["sent_total"] = io.bytes_sent
        result["net"]["recv_total"] = io.bytes_recv
    except Exception: pass

    try:
        root = "C:\\" if IS_WINDOWS else "/"
        du = psutil.disk_usage(root)
        result["disk"] = {"total": du.total, "used": du.used,
                          "percent": du.percent}
    except Exception: pass

    try:
        result["uptime"] = time.time() - psutil.boot_time()
    except Exception: pass

    for task in task_manager.list_tasks():
        pid = task.get("pid")
        entry = {"id": task["id"], "name": task["name"],
                 "status": task["status"], "cpu": 0, "memory": 0, "pid": pid}
        if pid:
            try:
                p = psutil.Process(pid)
                with p.oneshot():
                    entry["cpu"] = p.cpu_percent(interval=None)
                    entry["memory"] = p.memory_info().rss
                try:
                    children = p.children(recursive=True)
                    for c in children:
                        try:
                            entry["cpu"] += c.cpu_percent(interval=None)
                            entry["memory"] += c.memory_info().rss
                        except Exception: pass
                except Exception: pass
            except Exception: pass
        result["tasks"].append(entry)
    return result


# ============================================================
# 路由
# ============================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        is_first = store.data.get('auth') is None
        return render_template(
            'login.html', is_first=is_first,
            title=store.data['settings'].get('panel_title', 'PSh Panel'))
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')
    if len(password) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400
    if store.data.get('auth') is None:
        with store.lock:
            store.data['auth'] = hash_password(password)
            store.save()
        session['auth'] = True
        return jsonify({"ok": True, "is_first": True})
    if verify_password(password, store.data['auth']):
        session['auth'] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route('/')
@login_required
def index():
    return render_template(
        'base.html',
        title=store.data['settings'].get('panel_title', 'PSh Panel'))


@app.route('/page/<name>')
@login_required
def page(name):
    allowed = {'home', 'terminal', 'files', 'performance', 'settings'}
    if name not in allowed:
        abort(404)
    return render_template(f'{name}.html')


@app.route('/api/tasks', methods=['GET', 'POST'])
@login_required
def api_tasks():
    if request.method == 'GET':
        return jsonify(task_manager.list_tasks())
    data = request.get_json() or {}
    if not data.get('command'):
        return jsonify({"error": "command required"}), 400
    return jsonify(task_manager.create_task(data))


@app.route('/api/tasks/<task_id>', methods=['PUT', 'DELETE', 'GET'])
@login_required
def api_task(task_id):
    if request.method == 'GET':
        t = task_manager.get_task(task_id)
        if not t:
            abort(404)
        return jsonify(task_manager._public(t))
    if request.method == 'PUT':
        t = task_manager.update_task(task_id, request.get_json() or {})
        if not t: abort(404)
        return jsonify(t)
    task_manager.delete_task(task_id)
    return jsonify({"ok": True})


@app.route('/api/tasks/<task_id>/<action>', methods=['POST'])
@login_required
def api_task_action(task_id, action):
    if action == 'start':
        return jsonify(task_manager.start(task_id))
    if action == 'stop':
        return jsonify(task_manager.stop(task_id))
    if action == 'restart':
        return jsonify(task_manager.restart(task_id))
    abort(404)


@app.route('/api/tasks/<task_id>/logs')
@login_required
def api_task_logs(task_id):
    tail = int(request.args.get('tail', 1000))
    after = request.args.get('after')
    after = int(after) if after is not None else None
    return jsonify(task_manager.get_logs(task_id, tail=tail, after=after))


@app.route('/api/tasks/<task_id>/history')
@login_required
def api_task_history(task_id):
    with store.lock:
        hist = store.data.get("history", {}).get(task_id, [])
    return jsonify(hist)


@app.route('/api/files/roots')
@login_required
def api_file_roots():
    return jsonify({
        "roots": list_roots(),
        "platform": "windows" if IS_WINDOWS else "linux",
        "home": str(Path.home()),
    })


@app.route('/api/files/list')
@login_required
def api_file_list():
    path = request.args.get('path', '')
    resolved = resolve_path(path)
    if not resolved:
        return jsonify({"error": "invalid path"}), 400
    if not os.path.exists(resolved):
        return jsonify({"error": "not found"}), 404
    if os.path.isfile(resolved):
        return jsonify({"file": file_info(Path(resolved))})
    result = list_dir(resolved)
    if not result:
        return jsonify({"error": "not a directory"}), 400
    return jsonify(result)


@app.route('/api/files/download')
@login_required
def api_file_download():
    path = resolve_path(request.args.get('path', ''))
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True)


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_file_upload():
    directory = resolve_path(request.form.get('path', ''))
    if not directory or not os.path.isdir(directory):
        return jsonify({"error": "invalid dir"}), 400
    uploaded = request.files.getlist('files')
    results = []
    for f in uploaded:
        if not f.filename:
            continue
        target = Path(directory) / Path(f.filename).name
        f.save(str(target))
        results.append(str(target))
    return jsonify({"ok": True, "files": results})


@app.route('/api/files/mkdir', methods=['POST'])
@login_required
def api_file_mkdir():
    data = request.get_json() or {}
    parent = resolve_path(data.get('path', ''))
    name = data.get('name', '').strip()
    if not parent or not name or '/' in name or '\\' in name:
        return jsonify({"error": "invalid"}), 400
    target = Path(parent) / name
    if target.exists():
        return jsonify({"error": "exists"}), 400
    target.mkdir(parents=True)
    return jsonify({"ok": True, "path": str(target)})


@app.route('/api/files/newfile', methods=['POST'])
@login_required
def api_file_newfile():
    data = request.get_json() or {}
    parent = resolve_path(data.get('path', ''))
    name = data.get('name', '').strip()
    if not parent or not name or '/' in name or '\\' in name:
        return jsonify({"error": "invalid"}), 400
    target = Path(parent) / name
    if target.exists():
        return jsonify({"error": "exists"}), 400
    target.touch()
    return jsonify({"ok": True, "path": str(target)})


@app.route('/api/files/rename', methods=['POST'])
@login_required
def api_file_rename():
    data = request.get_json() or {}
    src = resolve_path(data.get('path', ''))
    new_name = data.get('name', '').strip()
    if not src or not new_name or '/' in new_name or '\\' in new_name:
        return jsonify({"error": "invalid"}), 400
    src_p = Path(src)
    dst = src_p.parent / new_name
    if dst.exists():
        return jsonify({"error": "exists"}), 400
    src_p.rename(dst)
    return jsonify({"ok": True, "path": str(dst)})


@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_file_delete():
    data = request.get_json() or {}
    path = resolve_path(data.get('path', ''))
    if not path:
        return jsonify({"error": "invalid"}), 400

    protected = protected_paths()
    norm = path.rstrip('/\\') or path
    for p in protected:
        if norm.lower() == p.lower().rstrip('/\\'):
            return jsonify({"error": "refused: protected path"}), 403
    if len(norm) <= 3 and not IS_WINDOWS:
        return jsonify({"error": "refused"}), 403

    p = Path(path)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    if p.is_dir():
        shutil.rmtree(str(p))
    else:
        p.unlink()
    return jsonify({"ok": True})


@app.route('/api/files/read')
@login_required
def api_file_read():
    path = resolve_path(request.args.get('path', ''))
    if not path or not os.path.isfile(path):
        abort(404)
    size = os.path.getsize(path)
    if size > 2 * 1024 * 1024:
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
    if not path:
        return jsonify({"error": "invalid"}), 400
    with open(path, 'w', encoding='utf-8') as f:
        f.write(data.get('content', ''))
    return jsonify({"ok": True})


@app.route('/api/perf')
@login_required
def api_perf():
    return jsonify(get_perf())


@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    if request.method == 'GET':
        return jsonify(store.data['settings'])
    data = request.get_json() or {}
    with store.lock:
        for k in ('scrollback', 'font_size', 'max_history',
                  'panel_title', 'perf_interval'):
            if k in data:
                store.data['settings'][k] = data[k]
        store.save()
    return jsonify(store.data['settings'])


@app.route('/api/password', methods=['POST'])
@login_required
def api_password():
    data = request.get_json() or {}
    old = data.get('old', '')
    new = data.get('new', '')
    if not verify_password(old, store.data.get('auth')):
        return jsonify({"error": "旧密码错误"}), 401
    if len(new) < 4:
        return jsonify({"error": "新密码太短"}), 400
    with store.lock:
        store.data['auth'] = hash_password(new)
        store.save()
    return jsonify({"ok": True})


@app.route('/api/system')
@login_required
def api_system():
    return jsonify({
        "platform": "windows" if IS_WINDOWS else ("darwin" if sys.platform == "darwin" else "linux"),
        "python": sys.version.split()[0],
        "default_shell": default_shell(),
        "has_pty": (PtyProcess is not None) if IS_WINDOWS else True,
        "has_psutil": psutil is not None,
    })


# ============================================================
# SocketIO 事件
# ============================================================
@socketio.on('connect')
def sio_connect():
    if not session.get('auth'):
        return False


@socketio.on('task:subscribe')
def sio_task_subscribe(data):
    task_id = data.get('task_id')
    if not task_id:
        return
    join_room(f"task:{task_id}")
    snap = task_manager.get_logs(task_id)
    emit('task:snapshot', {'task_id': task_id, **snap})


@socketio.on('task:unsubscribe')
def sio_task_unsubscribe(data):
    task_id = data.get('task_id')
    if task_id:
        leave_room(f"task:{task_id}")


@socketio.on('task:input')
def sio_task_input(data):
    task_id = data.get('task_id')
    text = data.get('data', '')
    rt = task_manager.runtimes.get(task_id)
    if not rt or rt.status != 'running' or rt.mode != 'pty':
        return
    try:
        pty_write(rt.process, text)
    except Exception:
        pass


@socketio.on('task:resize')
def sio_task_resize(data):
    task_id = data.get('task_id')
    rows = _safe_int(data.get('rows'), 30, 2, 500)
    cols = _safe_int(data.get('cols'), 120, 2, 1000)
    rt = task_manager.runtimes.get(task_id)
    if not rt or rt.mode != 'pty' or not rt.process:
        return
    try:
        pty_resize(rt.process, rows, cols)
    except Exception:
        pass


@socketio.on('term:create')
def sio_term_create(data):
    term_id = uuid.uuid4().hex[:12]
    command = data.get('command') or default_shell()
    cwd = data.get('cwd') or None
    sess = TerminalSession(term_id)
    try:
        sess.spawn(command, cwd=cwd)
    except Exception as e:
        emit('term:error', {'error': str(e), 'term_id': term_id})
        return
    sessions[term_id] = sess
    join_room(f"term:{term_id}")
    emit('term:created', {
        'term_id': term_id, 'command': command, 'cwd': cwd or str(BASE_DIR),
    })


@socketio.on('term:join')
def sio_term_join(data):
    term_id = data.get('term_id')
    if term_id in sessions:
        join_room(f"term:{term_id}")
        sess = sessions[term_id]
        with sess.lock:
            for seq, chunk in sess.buffer:
                emit('term:output', {'term_id': term_id, 'data': chunk})


@socketio.on('term:leave')
def sio_term_leave(data):
    term_id = data.get('term_id')
    if term_id:
        leave_room(f"term:{term_id}")


@socketio.on('term:input')
def sio_term_input(data):
    term_id = data.get('term_id')
    sess = sessions.get(term_id)
    if sess:
        sess.write(data.get('data', ''))


@socketio.on('term:resize')
def sio_term_resize(data):
    term_id = data.get('term_id')
    rows = _safe_int(data.get('rows'), 30, 2, 500)
    cols = _safe_int(data.get('cols'), 120, 2, 1000)
    sess = sessions.get(term_id)
    if sess:
        sess.resize(rows, cols)


@socketio.on('term:close')
def sio_term_close(data):
    term_id = data.get('term_id')
    sess = sessions.pop(term_id, None)
    if sess:
        sess.close()

@socketio.on('perf:subscribe')
def sio_perf_subscribe():
    join_room('perf')
    perf_broadcaster.subscribe(request.sid)
    # 立刻推一次，避免等一个周期
    try:
        emit('perf:data', get_perf())
    except Exception:
        pass


@socketio.on('perf:unsubscribe')
def sio_perf_unsubscribe():
    leave_room('perf')
    perf_broadcaster.unsubscribe(request.sid)

@socketio.on('disconnect')
def sio_disconnect():
    perf_broadcaster.unsubscribe(request.sid)


# ============================================================
# 自动启动
# ============================================================
def autostart_tasks():
    for task in task_manager.list_tasks():
        if task.get('enabled') and task['status'] != 'running':
            try:
                task_manager.start(task['id'])
            except Exception as e:
                print(f"[autostart] {task['name']}: {e}")


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

    def _delayed_start():
        time.sleep(2)
        autostart_tasks()

    threading.Thread(target=_delayed_start, daemon=True).start()
    socketio.run(app, host='0.0.0.0', port=port, debug=False,
                 allow_unsafe_werkzeug=True)