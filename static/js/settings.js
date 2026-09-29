/* PSh Panel - 设置页（全防御版） */
(function() {
  'use strict';

  /* ---------- 小工具 ---------- */
  function $(id) { return document.getElementById(id); }
  function setText(el, t) { if (el) el.textContent = t; }
  function setVal(el, v) { if (el) el.value = v; }
  function setColor(el, c) { if (el) el.style.color = c; }
  function setClass(el, c) { if (el) el.className = c; }

  /* ============================================================
   * 1. 连接状态
   * ============================================================ */
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
      if (name === 'websocket') {
        el.textContent = 'WebSocket';
        el.className = 'conn-badge ok';
      } else if (name === 'polling') {
        el.textContent = 'HTTP Long-Polling';
        el.className = 'conn-badge warn';
      } else {
        el.textContent = '-';
        el.className = 'conn-badge';
      }
    } catch (e) {
      console.warn('[settings] updateTransport', e);
    }
  }

  function updateState() {
    try {
      const socket = window.__socket;
      const connected = !!(socket && socket.connected);
      const stateEl = $('conn-state');
      const dotEl = $('conn-dot');
      const sidEl = $('conn-sid');
      setText(stateEl, connected ? '已连接' : '已断开');
      setColor(stateEl, connected ? 'var(--success)' : 'var(--danger)');
      setClass(dotEl, 'conn-dot ' + (connected ? 'ok' : 'err'));
      setText(sidEl, (socket && socket.id) || '-');
    } catch (e) {
      console.warn('[settings] updateState', e);
    }
  }

  function updateUptime() {
    try {
      const el = $('conn-uptime');
      if (!el) return;
      const socket = window.__socket;
      if (!socket || !socket.connected) {
        el.textContent = '-';
        return;
      }
      const sec = Math.floor((Date.now() - connectStart) / 1000);
      el.textContent = (window.fmt && fmt.duration) ? fmt.duration(sec) : (sec + ' 秒');
    } catch (e) {}
  }

  function initConnection() {
    connectStart = Date.now();
    updateTransport();
    updateState();
    updateUptime();

    if (uptimeTimer) clearInterval(uptimeTimer);
    // 每秒刷新一次 —— 升级（polling → websocket）是异步发生的
    uptimeTimer = setInterval(() => {
      updateTransport();
      updateState();
      updateUptime();
    }, 1000);

    const socket = window.__socket;
    if (socket && socket.on) {
      socket.on('connect', () => {
        connectStart = Date.now();
        updateTransport();
        updateState();
      });
      socket.on('disconnect', updateState);
      try {
        if (socket.io && socket.io.engine && socket.io.engine.on) {
          socket.io.engine.on('upgrade', updateTransport);
        }
      } catch (e) {}
    }
  }

  /* ============================================================
   * 2. 面板设置
   * ============================================================ */
  function fillSettings(s) {
    s = s || {};
    setVal($('set-title'), s.panel_title || 'PSh Panel');
    setVal($('set-scrollback'), s.scrollback || 5000);
    setVal($('set-font'), s.font_size || 14);
    setVal($('set-history'), s.max_history || 10);
    setVal($('set-perf'), s.perf_interval || 2000);
    setVal($('set-stop-timeout'), s.stop_timeout || 5);
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
        panel_title: ($('set-title') && $('set-title').value || 'PSh Panel').trim(),
        scrollback: parseInt($('set-scrollback') && $('set-scrollback').value) || 5000,
        font_size: parseInt($('set-font') && $('set-font').value) || 14,
        max_history: parseInt($('set-history') && $('set-history').value) || 10,
        perf_interval: parseInt($('set-perf') && $('set-perf').value) || 2000,
        stop_timeout: parseInt($('set-stop-timeout') && $('set-stop-timeout').value) || 5,
      };
      // 夹紧到安全范围
      payload.scrollback = Math.max(500, Math.min(100000, payload.scrollback));
      payload.font_size = Math.max(10, Math.min(24, payload.font_size));
      payload.max_history = Math.max(1, Math.min(100, payload.max_history));
      payload.perf_interval = Math.max(500, Math.min(10000, payload.perf_interval));
      payload.stop_timeout = Math.max(1, Math.min(60, payload.stop_timeout));

      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
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

  function initSettingsPanel() {
    fillSettings(window.__settings || {});
    const btn = $('btn-save-settings');
    if (btn) btn.onclick = saveSettings;
  }

  /* ============================================================
   * 3. 修改密码
   * ============================================================ */
  async function savePassword() {
    try {
      const oldPwd = ($('pwd-old') && $('pwd-old').value) || '';
      const newPwd = ($('pwd-new') && $('pwd-new').value) || '';
      const newPwd2 = ($('pwd-new2') && $('pwd-new2').value) || '';

      setMsg('pwd-msg', '');
      if (!oldPwd) { setMsg('pwd-msg', '请输入旧密码', true); return; }
      if (newPwd.length < 4) { setMsg('pwd-msg', '新密码至少 4 位', true); return; }
      if (newPwd !== newPwd2) { setMsg('pwd-msg', '两次输入不一致', true); return; }

      const r = await fetch('/api/password', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({old: oldPwd, new: newPwd}),
      });
      const d = await r.json().catch(() => ({}));
      if (d.ok) {
        setMsg('pwd-msg', '修改成功', false);
        setVal($('pwd-old'), '');
        setVal($('pwd-new'), '');
        setVal($('pwd-new2'), '');
        setTimeout(() => setMsg('pwd-msg', ''), 2000);
      } else {
        setMsg('pwd-msg', d.error || '修改失败', true);
      }
    } catch (e) {
      setMsg('pwd-msg', '网络错误', true);
    }
  }

  function initPassword() {
    const btn = $('btn-save-pwd');
    if (btn) btn.onclick = savePassword;
    ['pwd-old', 'pwd-new', 'pwd-new2'].forEach(id => {
      const el = $(id);
      if (el) el.addEventListener('keydown', e => {
        if (e.key === 'Enter') savePassword();
      });
    });
  }

  /* ============================================================
   * 4. 系统信息
   * ============================================================ */
  async function loadSystemInfo() {
    try {
      const r = await fetch('/api/system');
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

      const ver = (window.io && window.io.protocol) || '-';
      setText($('sys-socketio'), String(ver));
    } catch (e) {
      console.warn('[settings] loadSystemInfo', e);
    }
  }

  /* ============================================================
   * 5. 启动
   * ============================================================ */
  function boot() {
    try { initConnection(); } catch (e) { console.error('[settings] initConnection', e); }
    try { initSettingsPanel(); } catch (e) { console.error('[settings] initSettingsPanel', e); }
    try { initPassword(); } catch (e) { console.error('[settings] initPassword', e); }
    try { loadSystemInfo(); } catch (e) { console.error('[settings] loadSystemInfo', e); }
  }

  // 等 DOM 完全就绪再执行
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    setTimeout(boot, 0);
  }

  /* 清理 */
  if (window.__registerCleanup) {
    window.__registerCleanup(() => {
      if (uptimeTimer) clearInterval(uptimeTimer);
    });
  }
})();