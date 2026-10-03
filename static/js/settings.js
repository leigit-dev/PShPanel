(function() {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function setText(el, t) { if (el) el.textContent = t; }
  function setVal(el, v) { if (el) el.value = v; }
  function setColor(el, c) { if (el) el.style.color = c; }
  function setClass(el, c) { if (el) el.className = c; }

  function afetch(url, options = {}) {
    if (window.accessFetch) return window.accessFetch(url, options);
    options.credentials = 'same-origin';
    options.redirect = 'manual';
    return fetch(url, options);
  }

  let connectStart = Date.now();
  let uptimeTimer = null;

  function updateTransport() {
    try {
      const socket = window.__socket;
      let name = '-';
      if (socket && socket.io && socket.io.engine && socket.io.engine.transport) {
        name = socket.io.engine.transport.name || '-';
      }
      const el = $('conn-transport');
      if (!el) return;
      if (name === 'websocket') { el.textContent = 'WebSocket'; el.className = 'conn-badge ok'; }
      else if (name === 'polling') { el.textContent = 'HTTP Long-Polling'; el.className = 'conn-badge warn'; }
      else { el.textContent = '-'; el.className = 'conn-badge'; }
    } catch (e) {}
  }
  function updateState() {
    try {
      const socket = window.__socket;
      const connected = !!(socket && socket.connected);
      const stateEl = $('conn-state'), dotEl = $('conn-dot'), sidEl = $('conn-sid');
      setText(stateEl, connected ? '已连接' : '已断开');
      setColor(stateEl, connected ? 'var(--success)' : 'var(--danger)');
      setClass(dotEl, 'conn-dot ' + (connected ? 'ok' : 'err'));
      setText(sidEl, (socket && socket.id) || '-');
    } catch (e) {}
  }
  function updateUptime() {
    try {
      const el = $('conn-uptime');
      if (!el) return;
      const socket = window.__socket;
      if (!socket || !socket.connected) { el.textContent = '-'; return; }
      const sec = Math.floor((Date.now() - connectStart) / 1000);
      el.textContent = (window.fmt && fmt.duration) ? fmt.duration(sec) : (sec + ' 秒');
    } catch (e) {}
  }
  function initConnection() {
    connectStart = Date.now();
    updateTransport(); updateState(); updateUptime();
    if (uptimeTimer) clearInterval(uptimeTimer);
    uptimeTimer = setInterval(() => { updateTransport(); updateState(); updateUptime(); }, 1000);
    const socket = window.__socket;
    if (socket && socket.on) {
      socket.on('connect', () => {
        connectStart = Date.now(); updateTransport(); updateState();
      });
      socket.on('disconnect', updateState);
      try {
        if (socket.io && socket.io.engine && socket.io.engine.on) {
          socket.io.engine.on('upgrade', updateTransport);
        }
      } catch (e) {}
    }
  }

  function fillSettings(s) {
    s = s || {};
    setVal($('set-title'), s.panel_title || 'PSh Panel');
    setVal($('set-scrollback'), s.scrollback || 5000);
    setVal($('set-font'), s.font_size || 14);
    setVal($('set-history'), s.max_history || 10);
    setVal($('set-perf'), s.perf_interval || 5000);
    setVal($('set-stop-timeout'), s.stop_timeout || 5);
    setVal($('set-python-path'), s.python_path || '');
    setVal($('set-max-attempts'), s.max_login_attempts || 5);
    setVal($('set-lockout-min'), s.lockout_minutes || 5);
    setVal($('set-session-days'), s.session_days || 7);
  }
  function setMsg(id, text, isErr) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'form-msg ' + (isErr ? 'err' : (text ? 'ok' : ''));
  }

  async function saveSettings() {
    try {
      const payload = {
        panel_title: (($('set-title') && $('set-title').value) || 'PSh Panel').trim(),
        scrollback: parseInt($('set-scrollback') && $('set-scrollback').value) || 5000,
        font_size: parseInt($('set-font') && $('set-font').value) || 14,
        max_history: parseInt($('set-history') && $('set-history').value) || 10,
        perf_interval: parseInt($('set-perf') && $('set-perf').value) || 5000,
        stop_timeout: parseInt($('set-stop-timeout') && $('set-stop-timeout').value) || 5,
        python_path: (($('set-python-path') && $('set-python-path').value) || '').trim(),
        max_login_attempts: parseInt($('set-max-attempts') && $('set-max-attempts').value) || 5,
        lockout_minutes: parseInt($('set-lockout-min') && $('set-lockout-min').value) || 5,
        session_days: parseInt($('set-session-days') && $('set-session-days').value) || 7,
      };
      const r = await afetch('/api/settings', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (!r.ok) { setMsg('settings-msg', '保存失败', true); return; }
      window.__settings = await r.json();
      fillSettings(window.__settings);
      setMsg('settings-msg', '已保存', false);
      setTimeout(() => setMsg('settings-msg', ''), 2000);
    } catch (e) {
      setMsg('settings-msg', '网络错误: ' + e.message, true);
    }
  }

  async function checkPython() {
    const path = (($('set-python-path') && $('set-python-path').value) || '').trim();
    setMsg('python-msg', '检测中…', false);
    try {
      const r = await afetch('/api/python-check', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path}),
      });
      const d = await r.json();
      if (d.ok) setMsg('python-msg', d.version ? ('✔ ' + d.version) : (d.message || '✔ 已清空'), false);
      else setMsg('python-msg', '✘ ' + (d.error || '检测失败'), true);
    } catch (e) { setMsg('python-msg', '网络错误', true); }
  }

  async function savePassword() {
    try {
      const oldPwd = ($('pwd-old') && $('pwd-old').value) || '';
      const newPwd = ($('pwd-new') && $('pwd-new').value) || '';
      const newPwd2 = ($('pwd-new2') && $('pwd-new2').value) || '';
      setMsg('pwd-msg', '');
      if (!oldPwd) { setMsg('pwd-msg', '请输入旧密码', true); return; }
      if (newPwd !== newPwd2) { setMsg('pwd-msg', '两次输入不一致', true); return; }
      const r = await afetch('/api/password', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({old: oldPwd, new: newPwd}),
      });
      const d = await r.json().catch(() => ({}));
      if (d.ok) {
        setMsg('pwd-msg', '修改成功', false);
        setVal($('pwd-old'), ''); setVal($('pwd-new'), ''); setVal($('pwd-new2'), '');
        setTimeout(() => setMsg('pwd-msg', ''), 2000);
      } else setMsg('pwd-msg', d.error || '修改失败', true);
    } catch (e) { setMsg('pwd-msg', '网络错误', true); }
  }

  async function loadSystemInfo() {
    try {
      const r = await afetch('/api/system');
      const d = await r.json();
      const platformMap = {windows: 'Windows', linux: 'Linux', darwin: 'macOS'};
      setText($('sys-platform'), platformMap[d.platform] || d.platform || '-');
      setText($('sys-python'), d.python || '-');
      setText($('sys-shell'), d.default_shell || '-');
      const pty = $('sys-pty');
      setText(pty, d.has_pty ? '可用' : '不可用');
      setColor(pty, d.has_pty ? 'var(--success)' : 'var(--danger)');
      const ps = $('sys-psutil');
      setText(ps, d.has_psutil ? '可用' : '未安装');
      setColor(ps, d.has_psutil ? 'var(--success)' : 'var(--warn)');
      let ver = '-';
      if (d.py_socketio && d.py_socketio !== '?') {
        ver = 'py ' + d.py_socketio;
        if (d.flask_socketio && d.flask_socketio !== '?') ver += ' / flask ' + d.flask_socketio;
      } else if (window.io && window.io.protocol) ver = 'protocol ' + window.io.protocol;
      setText($('sys-socketio'), ver);
    } catch (e) {}
  }

  function initSettingsPanel() {
    fillSettings(window.__settings || {});
    const btn = $('btn-save-settings');
    if (btn) btn.onclick = saveSettings;
    const btnCheck = $('btn-check-python');
    if (btnCheck) btnCheck.onclick = checkPython;
    const btnPwd = $('btn-save-pwd');
    if (btnPwd) btnPwd.onclick = savePassword;
    ['pwd-old', 'pwd-new', 'pwd-new2'].forEach(id => {
      const el = $(id);
      if (el) el.addEventListener('keydown', e => { if (e.key === 'Enter') savePassword(); });
    });
  }

  function boot() {
    try { initConnection(); } catch (e) {}
    try { initSettingsPanel(); } catch (e) {}
    try { loadSystemInfo(); } catch (e) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else setTimeout(boot, 0);

  window.__registerCleanup && window.__registerCleanup(() => {
    if (uptimeTimer) clearInterval(uptimeTimer);
  });
})();