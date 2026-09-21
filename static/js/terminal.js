/* PSh Panel - 终端页：Shell 会话 + 任务全屏（PTY 可交互，PIPE 只读） */
(function() {
  const tabsEl = document.getElementById('term-tabs');
  const wrapEl = document.getElementById('term-wrap');
  const cmdEl = document.getElementById('term-cmd');
  if (!tabsEl || !wrapEl || !cmdEl) return;

  const terminals = {};   // key -> entry
  let activeKey = null;

  function defaultShell() {
    return navigator.userAgent.includes('Windows') ? 'powershell.exe' : '/bin/bash';
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  /* ---------- 新建 Shell ---------- */
  document.getElementById('term-new').onclick = () => {
    const command = (cmdEl.value || '').trim() || defaultShell();
    window.__socket.emit('term:create', {command});
  };
  cmdEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('term-new').click();
  });

  /* ---------- Shell 事件 ---------- */
  window.__socket.on('term:created', (data) => {
    createShellTab(data.term_id, data.command);
  });
  window.__socket.on('term:output', (data) => {
    const t = terminals['shell:' + data.term_id];
    if (t && t.term) t.term.write(data.data);
  });
  window.__socket.on('term:exit', (data) => {
    const t = terminals['shell:' + data.term_id];
    if (!t) return;
    t.term.write(`\r\n\x1b[33m[进程已退出，代码: ${data.code}]\x1b[0m\r\n`);
    const titleEl = t.tab.querySelector('.tab-title');
    if (titleEl && !titleEl.textContent.includes('[已退出]')) {
      titleEl.textContent += ' [已退出]';
    }
  });
  window.__socket.on('term:error', (data) => {
    const msg = data.error || 'unknown';
    if (data.term_id && terminals['shell:' + data.term_id]) {
      terminals['shell:' + data.term_id].term.write(`\r\n\x1b[31m[错误] ${msg}\x1b[0m\r\n`);
    } else {
      alert('终端创建失败: ' + msg);
    }
  });

  /* ---------- 打开任务全屏 ---------- */
  function openTaskTab(taskId) {
    const key = 'task:' + taskId;
    if (terminals[key]) { activate(key); return; }
    fetch(`/api/tasks/${taskId}`)
      .then(r => r.json())
      .then(task => createTaskTab(task))
      .catch(e => alert('加载任务失败: ' + e));
  }

  function createTaskTab(task) {
    const key = 'task:' + task.id;
    const term = new Terminal(window.XTERM_OPTS(window.__settings?.font_size || 14));
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);

    const container = document.createElement('div');
    container.className = 'term-instance';
    wrapEl.appendChild(container);
    term.open(container);
    requestAnimationFrame(() => { try { fit.fit(); } catch(e) {} });

    const readonly = task.mode !== 'pty';
    const tab = document.createElement('div');
    tab.className = 'tab';
    tab.innerHTML = `
      <span class="tab-title">${escapeHtml(task.name)}</span>
      <span class="mode-tag ${readonly ? 'readonly' : ''}">${task.mode.toUpperCase()}</span>
      <span class="close">✕</span>`;
    tab.addEventListener('click', (e) => {
      if (e.target.classList.contains('close')) closeTab(key);
      else activate(key);
    });
    tabsEl.appendChild(tab);

    // PTY 允许输入
    if (task.mode === 'pty') {
      term.onData(d => {
        try {
          window.__socket.emit('task:input', {task_id: task.id, data: d});
        } catch(e) {}
      });
    }

    const ro = new ResizeObserver(() => {
      if (activeKey === key) sendTaskResize(task.id, fit);
    });
    ro.observe(container);

    terminals[key] = {
      type: 'task', key, taskId: task.id, mode: task.mode,
      term, fit, container, tab, ro,
    };

    // 注册到全局总线
    window.__taskTerms = window.__taskTerms || {};
    window.__taskTerms[task.id] = {
      term,
      onStatus: (d) => {
        const t = terminals[key];
        if (!t) return;
        if (d.status === 'exited') {
          const titleEl = t.tab.querySelector('.tab-title');
          if (titleEl && !titleEl.textContent.includes('[已退出]')) {
            titleEl.textContent += ' [已退出]';
          }
        }
      },
    };

    window.__socket.emit('task:subscribe', {task_id: task.id});
    activate(key);
  }

  function createShellTab(termId, command) {
    const key = 'shell:' + termId;
    const term = new Terminal(window.XTERM_OPTS(window.__settings?.font_size || 14));
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);

    const container = document.createElement('div');
    container.className = 'term-instance';
    wrapEl.appendChild(container);
    term.open(container);
    requestAnimationFrame(() => { try { fit.fit(); } catch(e) {} });

    const tab = document.createElement('div');
    tab.className = 'tab';
    tab.innerHTML = `
      <span class="tab-title">${escapeHtml(command || 'shell')}</span>
      <span class="mode-tag">SHELL</span>
      <span class="close">✕</span>`;
    tab.addEventListener('click', (e) => {
      if (e.target.classList.contains('close')) closeTab(key);
      else activate(key);
    });
    tabsEl.appendChild(tab);

    term.onData(d => {
      try {
        window.__socket.emit('term:input', {term_id: termId, data: d});
      } catch(e) {}
    });

    const ro = new ResizeObserver(() => {
      if (activeKey === key) sendShellResize(termId, fit);
    });
    ro.observe(container);

    terminals[key] = {
      type: 'shell', key, termId, term, fit, container, tab, ro,
    };
    activate(key);
  }

  /* ---------- 切换 / 关闭 ---------- */
  function activate(key) {
    const t = terminals[key];
    if (!t) return;
    if (activeKey === key) {
      setTimeout(() => {
        t.term.focus();
        if (t.type === 'shell') sendShellResize(t.termId, t.fit);
        else sendTaskResize(t.taskId, t.fit);
      }, 30);
      return;
    }
    Object.entries(terminals).forEach(([k, v]) => {
      v.container.classList.toggle('active', k === key);
      v.tab.classList.toggle('active', k === key);
    });
    activeKey = key;
    setTimeout(() => {
      try { t.fit.fit(); } catch(e) {}
      t.term.focus();
      if (t.type === 'shell') sendShellResize(t.termId, t.fit);
      else sendTaskResize(t.taskId, t.fit);
    }, 30);
  }

  function closeTab(key) {
    const t = terminals[key];
    if (!t) return;
    if (t.type === 'shell') {
      try { window.__socket.emit('term:close', {term_id: t.termId}); } catch(e) {}
    } else if (t.type === 'task') {
      try { window.__socket.emit('task:unsubscribe', {task_id: t.taskId}); } catch(e) {}
      if (window.__taskTerms) delete window.__taskTerms[t.taskId];
    }
    try { t.ro && t.ro.disconnect(); } catch(e) {}
    try { t.term.dispose(); } catch(e) {}
    t.container.remove();
    t.tab.remove();
    delete terminals[key];
    if (activeKey === key) {
      const keys = Object.keys(terminals);
      activeKey = null;
      if (keys.length) activate(keys[0]);
    }
  }

  /* ---------- resize 辅助 ---------- */
  function _dims(fit) {
    let dims = null;
    try { dims = fit.proposeDimensions(); } catch(e) { return null; }
    if (!dims) return null;
    const cols = Number(dims.cols);
    const rows = Number(dims.rows);
    if (!Number.isFinite(cols) || !Number.isFinite(rows)) return null;
    if (cols < 2 || rows < 2) return null;
    return {cols: Math.floor(cols), rows: Math.floor(rows)};
  }

  function sendShellResize(termId, fit) {
    const d = _dims(fit);
    if (!d) return;
    try {
      window.__socket.emit('term:resize', {
        term_id: termId, cols: d.cols, rows: d.rows,
      });
    } catch(e) {}
  }

  function sendTaskResize(taskId, fit) {
    const t = terminals['task:' + taskId];
    if (!t || t.mode !== 'pty') return;
    const d = _dims(fit);
    if (!d) return;
    try {
      window.__socket.emit('task:resize', {
        task_id: taskId, cols: d.cols, rows: d.rows,
      });
    } catch(e) {}
  }

  /* ---------- 页面加载后检查待打开任务 ---------- */
  setTimeout(() => {
    if (window.__pendingTaskFullscreen) {
      const taskId = window.__pendingTaskFullscreen;
      window.__pendingTaskFullscreen = null;
      openTaskTab(taskId);
    }
  }, 100);

  /* ---------- 窗口 resize ---------- */
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!activeKey) return;
      const t = terminals[activeKey];
      if (!t) return;
      try { t.fit.fit(); } catch(e) {}
      if (t.type === 'shell') sendShellResize(t.termId, t.fit);
      else sendTaskResize(t.taskId, t.fit);
    }, 120);
  });

  /* ---------- 离开页面时清理 ---------- */
  window.__registerCleanup && window.__registerCleanup(() => {
    Object.keys(terminals).forEach(closeTab);
  });
})();