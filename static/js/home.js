/* PSh Panel - 首页 */
(function() {
  'use strict';

  function $(id) { return document.getElementById(id); }
  function escapeHtml(s) {
    return String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, function(c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function afetch(url, options) {
    if (window.accessFetch) return window.accessFetch(url, options);
    options = options || {};
    options.credentials = 'same-origin';
    options.redirect = 'manual';
    return fetch(url, options);
  }
  function safeEmit(event, data) {
    try { if (window.__socket) window.__socket.emit(event, data); } catch (e) {}
  }
  function waitFor(ids, timeout, cb) {
    timeout = timeout || 3000;
    var start = Date.now();
    var tick = function() {
      var all = true;
      for (var i = 0; i < ids.length; i++) {
        if (!$(ids[i])) { all = false; break; }
      }
      if (all) { cb(); return; }
      if (Date.now() - start > timeout) {
        var missing = [];
        for (var j = 0; j < ids.length; j++) if (!$(ids[j])) missing.push(ids[j]);
        console.error('[home] 等待超时，缺失元素:', missing);
        cb(); return;
      }
      requestAnimationFrame(tick);
    };
    tick();
  }

  var currentTaskId = null;
  var detailTerm = null, detailFit = null, previewRO = null;
  var gitTerm = null, gitFit = null, gitCurrentTaskId = null, gitRunning = false;
  var envCurrentTaskId = null;
  var dirCurrentPath = '';
  var dirTargetInputId = null;

  /* ============================================================
     任务列表
     ============================================================ */
  async function refreshList() {
    var listEl = $('task-list');
    if (!listEl) return;
    var tasks;
    try {
      var r = await afetch('/api/tasks');
      tasks = await r.json();
    } catch (e) { return; }
    listEl.innerHTML = '';
    tasks.forEach(function(t) {
      var el = document.createElement('div');
      el.className = 'task-item' + (t.id === currentTaskId ? ' active' : '');
      var statusClass = t.status === 'running' ? 'status-running' : 'status-stopped';
      var statusText = t.status === 'running' ? '运行中' : (t.status === 'exited' ? '已退出' : '已停止');
      el.innerHTML =
        '<div class="name"><span>' + escapeHtml(t.name) + '</span>' +
        '<span class="status-badge ' + statusClass + '">' + statusText + '</span></div>' +
        '<div class="cmd">' + escapeHtml(t.command) + ' · ' + String(t.mode || '').toUpperCase() + '</div>';
      el.onclick = function() { selectTask(t.id); };
      listEl.appendChild(el);
    });
  }

  function unregisterPreview() {
    if (previewRO) { try { previewRO.disconnect(); } catch (e) {} previewRO = null; }
    if (detailTerm) { try { detailTerm.dispose(); } catch (e) {} detailTerm = null; }
    if (currentTaskId) {
      if (window.__taskTerms && window.__taskTerms[currentTaskId]) {
        delete window.__taskTerms[currentTaskId];
      }
      safeEmit('task:unsubscribe', {task_id: currentTaskId});
    }
  }

  function sendPreviewResize(taskId, mode) {
    if (!detailFit || mode !== 'pty') return;
    var dims = null;
    try { dims = detailFit.proposeDimensions(); } catch (e) { return; }
    if (!dims) return;
    var cols = Number(dims.cols), rows = Number(dims.rows);
    if (!isFinite(cols) || !isFinite(rows)) return;
    if (cols < 2 || rows < 2) return;
    safeEmit('task:resize', {task_id: taskId, cols: Math.floor(cols), rows: Math.floor(rows)});
  }

  /* ============================================================
     选中任务
     ============================================================ */
  async function selectTask(id) {
    unregisterPreview();
    currentTaskId = id;
    await refreshList();

    var detailEl = $('task-detail');
    if (!detailEl) return;

    var t;
    try {
      var r = await afetch('/api/tasks/' + id);
      t = await r.json();
    } catch (e) { return; }

    var statusText = t.status === 'running' ? '运行中' : (t.status === 'exited' ? '已退出' : '已停止');
    var runningAttr = t.status === 'running' ? ' disabled' : '';
    detailEl.innerHTML =
      '<div class="panel-header"><h2>' + escapeHtml(t.name) + '</h2>' +
        '<span class="status-badge ' + (t.status === 'running' ? 'status-running' : 'status-stopped') + '">' + statusText + '</span></div>' +
      '<div class="detail-section"><h3>启动命令</h3><div class="value">' + escapeHtml(t.command) + '</div></div>' +
      '<div class="detail-section"><h3>工作目录 · 模式</h3><div class="value">' +
        escapeHtml(t.cwd) + ' · ' + String(t.mode || '').toUpperCase() + '</div></div>' +
      '<div class="detail-section"><h3>Shell 预览（点击进入全屏）</h3>' +
        '<div class="shell-preview" id="detail-term"></div></div>' +
      '<div class="detail-section"><h3>操作</h3><div class="detail-actions">' +
        (t.status !== 'running'
          ? '<button data-act="start">启动</button>'
          : '<button data-act="stop" class="danger">停止</button>') +
        '<button data-act="restart">重启</button>' +
        '<button data-act="toggle-enable">' + (t.enabled ? '禁用自启' : '启用自启') + '</button>' +
        '<button data-act="edit">编辑</button>' +
        '<button data-act="history">历史</button>' +
        '<button data-act="env">环境脚本</button>' +
        '<button data-act="git-pull"' + runningAttr + '>Git Pull</button>' +
        '<button data-act="open-terminal"' + runningAttr + '>在此打开终端</button>' +
        '<button data-act="delete" class="danger">删除</button>' +
      '</div></div>';

    var termEl = detailEl.querySelector('#detail-term');
    if (!termEl || !window.Terminal || !window.FitAddon) return;

    detailTerm = new window.Terminal(window.XTERM_OPTS(window.__settings && window.__settings.font_size || 14));
    detailFit = new window.FitAddon.FitAddon();
    detailTerm.loadAddon(detailFit);
    detailTerm.open(termEl);
    requestAnimationFrame(function() { try { detailFit.fit(); } catch (e) {} });

    window.__taskTerms = window.__taskTerms || {};
    window.__taskTerms[id] = { term: detailTerm, onStatus: function() { refreshList(); } };
    safeEmit('task:subscribe', {task_id: id});

    if (t.mode === 'pty') {
      detailTerm.onData(function(d) { safeEmit('task:input', {task_id: id, data: d}); });
    }

    setTimeout(function() {
      try { detailFit.fit(); } catch (e) {}
      sendPreviewResize(id, t.mode);
    }, 100);

    previewRO = new ResizeObserver(function() {
      if (!detailFit) return;
      try { detailFit.fit(); } catch (e) {}
      sendPreviewResize(id, t.mode);
    });
    previewRO.observe(termEl);

    termEl.onclick = function() {
      window.__pendingTaskFullscreen = id;
      unregisterPreview();
      navigate('terminal');
    };

    detailEl.querySelectorAll('[data-act]').forEach(function(btn) {
      btn.onclick = async function(e) {
        e.stopPropagation();
        var act = btn.dataset.act;
        if (act === 'edit') return openTaskModal(t);
        if (act === 'history') return showHistory(t);
        if (act === 'env') return openEnvModal(t);
        if (act === 'git-pull') return doGitPull(t);
        if (act === 'open-terminal') return openTerminalInDir(t);
        if (act === 'toggle-enable') {
          await afetch('/api/tasks/' + id, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: !t.enabled}),
          });
          return selectTask(id);
        }
        if (act === 'delete') {
          if (!confirm('确定删除该任务？')) return;
          unregisterPreview();
          await afetch('/api/tasks/' + id, {method: 'DELETE'});
          currentTaskId = null;
          detailEl.innerHTML = '<div class="empty-hint">选择一个任务查看详情</div>';
          return refreshList();
        }
        await afetch('/api/tasks/' + id + '/' + act, {method: 'POST'});
        setTimeout(function() { selectTask(id); }, 300);
      };
    });
  }

  function updateActionButtons(status) {
    var detailEl = $('task-detail');
    if (!detailEl) return;
    var running = status === 'running' || status === 'restarting';
    ['git-pull', 'open-terminal'].forEach(function(act) {
      var btn = detailEl.querySelector('[data-act="' + act + '"]');
      if (btn) btn.disabled = running;
    });
  }

  /* ============================================================
     任务配置弹窗
     ============================================================ */
  function setMsg(text, isErr) {
    var el = $('tm-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'form-msg' + (isErr ? ' err' : (text ? ' ok' : ''));
  }

  function openTaskModal(task) {
    var modal = $('task-modal');
    if (!modal) return;
    if ($('task-modal-title')) $('task-modal-title').textContent = task ? '编辑任务' : '新建任务';
    if ($('tm-name')) $('tm-name').value = (task && task.name) || '';
    if ($('tm-command')) $('tm-command').value = (task && task.command) || '';
    if ($('tm-cwd')) $('tm-cwd').value = (task && task.cwd) || '';
    if ($('tm-mode')) $('tm-mode').value = (task && task.mode) || 'pipe';
    if ($('tm-enabled')) $('tm-enabled').checked = !!(task && task.enabled);
    if ($('tm-env-script')) $('tm-env-script').value = (task && task.env_script) || '';
    setMsg('');
    modal.dataset.editing = (task && task.id) || '';
    modal.hidden = false;
    setTimeout(function() { if ($('tm-name')) $('tm-name').focus(); }, 50);
  }

  function closeTaskModal() {
    var modal = $('task-modal');
    if (!modal) return;
    modal.hidden = true;
    modal.dataset.editing = '';
  }

  async function saveTaskModal() {
    var modal = $('task-modal');
    if (!modal) return;
    var name = ($('tm-name') && $('tm-name').value || '').trim();
    var command = ($('tm-command') && $('tm-command').value || '').trim();
    var cwd = ($('tm-cwd') && $('tm-cwd').value || '').trim();
    var mode = $('tm-mode') && $('tm-mode').value || 'pipe';
    var enabled = $('tm-enabled') && $('tm-enabled').checked;
    var env_script = $('tm-env-script') && $('tm-env-script').value || '';

    if (!name) { setMsg('请填写任务名称', true); return; }
    if (!command) { setMsg('请填写启动命令', true); return; }

    var editing = modal.dataset.editing;
    var payload = {name: name, command: command, mode: mode, enabled: enabled, env_script: env_script};
    if (cwd) payload.cwd = cwd;

    var url = editing ? '/api/tasks/' + editing : '/api/tasks';
    var method = editing ? 'PUT' : 'POST';

    var r;
    try {
      r = await afetch(url, {
        method: method, headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
    } catch (e) { setMsg('网络错误：' + e, true); return; }
    if (!r.ok) {
      var d = await r.json().catch(function() { return {}; });
      setMsg(d.error || '保存失败', true); return;
    }
    var saved = await r.json();
    closeTaskModal();
    await refreshList();
    var id = saved.id || editing;
    if (id) selectTask(id);
  }

  /* ============================================================
     目录选择弹窗
     ============================================================ */
  async function openDirModal(targetInputId, startPath) {
    var modal = $('dir-modal');
    if (!modal) return;
    dirTargetInputId = targetInputId;
    modal.hidden = false;
    var path = startPath || '';
    if (!path) {
      try {
        var r = await afetch('/api/files/roots');
        var d = await r.json();
        if (d.roots && d.roots.length) path = d.roots[0].path;
      } catch (e) {}
    }
    await dirBrowse(path);
  }

  async function dirBrowse(path) {
    var dirList = $('dir-list');
    if (!dirList || !path) return;
    dirList.innerHTML = '<div class="dir-empty">加载中…</div>';
    var data;
    try {
      var r = await afetch('/api/files/list?path=' + encodeURIComponent(path));
      if (!r.ok) { dirList.innerHTML = '<div class="dir-empty">无法访问该目录</div>'; return; }
      data = await r.json();
    } catch (e) { dirList.innerHTML = '<div class="dir-empty">网络错误</div>'; return; }
    if (data.file) {
      var parent = data.file.path.replace(/[/\\][^/\\]+$/, '') || '/';
      return dirBrowse(parent);
    }
    dirCurrentPath = data.path;
    if ($('dir-path-input')) $('dir-path-input').value = data.path;
    dirList.innerHTML = '';
    var dirs = (data.entries || []).filter(function(e) { return e.is_dir; });
    if (!dirs.length) { dirList.innerHTML = '<div class="dir-empty">此目录下没有子文件夹</div>'; return; }
    dirs.forEach(function(e) {
      var el = document.createElement('div');
      el.className = 'dir-item';
      el.innerHTML = '<span class="dir-icon">📁</span><span>' + escapeHtml(e.name) + '</span>';
      el.onclick = function() { dirBrowse(e.path); };
      dirList.appendChild(el);
    });
  }

  function closeDirModal() {
    var modal = $('dir-modal');
    if (modal) modal.hidden = true;
    dirTargetInputId = null;
  }

  /* ============================================================
     历史弹窗
     ============================================================ */
  async function showHistory(t) {
    var modal = $('history-modal'), list = $('history-list');
    if (!modal || !list) return;
    list.innerHTML = '<div class="history-empty">加载中…</div>';
    modal.hidden = false;
    var hist = [];
    try {
      var r = await afetch('/api/tasks/' + t.id + '/history');
      hist = await r.json();
    } catch (e) { list.innerHTML = '<div class="history-empty">加载失败</div>'; return; }
    if (!hist.length) { list.innerHTML = '<div class="history-empty">暂无历史记录</div>'; return; }
    var reversed = hist.slice().reverse(), total = hist.length;
    list.innerHTML = '';
    reversed.forEach(function(h, idx) {
      var dur = (h.ended_at || 0) - (h.started_at || 0);
      var exit = h.exit_code;
      var exitClass = (exit === 0) ? 'ok' : 'err';
      var raw = (h.tail || []).map(function(x) { return x.data; }).join('');
      var clean = raw.replace(/\x1b\[[\?]?[0-9;]*[a-zA-Z]/g, '').replace(/\x1b\][^\x07]*\x07/g, '');
      var card = document.createElement('div');
      card.className = 'history-card';
      var head = document.createElement('div');
      head.className = 'history-card-head';
      head.innerHTML =
        '<span class="history-index">#' + (total - idx) + '</span>' +
        '<span class="history-time">' + fmt.time(h.started_at) + '</span>' +
        '<span class="history-arrow">→</span>' +
        '<span class="history-time">' + fmt.time(h.ended_at) + '</span>' +
        '<span class="history-dur">' + fmt.duration(dur) + '</span>' +
        '<span class="history-exit ' + exitClass + '">退出码 ' + exit + '</span>' +
        '<span class="history-toggle">展开</span>';
      var body = document.createElement('pre');
      body.className = 'history-body';
      body.textContent = clean || '(该次运行没有保存输出)';
      head.onclick = function() {
        var open = card.classList.toggle('expanded');
        var tgl = head.querySelector('.history-toggle');
        if (tgl) tgl.textContent = open ? '收起' : '展开';
      };
      card.appendChild(head); card.appendChild(body);
      list.appendChild(card);
    });
  }

  /* ============================================================
     环境脚本弹窗
     ============================================================ */
  var ENV_STATUS_MAP = {
    'none': { text: '未运行', cls: '' },
    'running': { text: '运行中', cls: 'running' },
    'ok': { text: '成功', cls: 'ok' },
    'error': { text: '失败', cls: 'err' }
  };

  function renderEnvStatus(status, exitCode, vars) {
    var badge = $('env-status-badge'), exitEl = $('env-exit'), varCountEl = $('env-var-count');
    var varsBlock = $('env-vars-block'), varsList = $('env-vars-list');
    var info = ENV_STATUS_MAP[status] || ENV_STATUS_MAP['none'];
    if (badge) { badge.textContent = info.text; badge.className = 'env-status-badge ' + (info.cls || ''); }
    if (exitEl) {
      if (exitCode === null || exitCode === undefined) { exitEl.textContent = ''; exitEl.style.color = ''; }
      else { exitEl.textContent = '退出码 ' + exitCode; exitEl.style.color = exitCode === 0 ? 'var(--success)' : 'var(--danger)'; }
    }
    var varCount = vars ? Object.keys(vars).length : 0;
    if (varCountEl) varCountEl.textContent = varCount ? (varCount + ' 个变量') : '';
    if (!varsBlock || !varsList) return;
    if (varCount) {
      varsBlock.hidden = false; varsList.innerHTML = '';
      Object.keys(vars).forEach(function(k) {
        var row = document.createElement('div');
        row.className = 'env-var-row';
        row.innerHTML = '<span class="env-var-key">' + escapeHtml(k) + '</span>' +
                        '<span class="env-var-eq">=</span>' +
                        '<span class="env-var-val">' + escapeHtml(vars[k]) + '</span>';
        varsList.appendChild(row);
      });
    } else { varsBlock.hidden = true; varsList.innerHTML = ''; }
  }

  function appendEnvLines(lines) {
    var body = $('env-body');
    if (!body) return;
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i];
      var span = document.createElement('span');
      span.className = l.stream === 'stderr' ? 'env-stderr' : 'env-stdout';
      span.textContent = l.data;
      body.appendChild(span);
    }
    body.scrollTop = body.scrollHeight;
  }

  async function openEnvModal(t) {
    var modal = $('env-modal'), body = $('env-body');
    if (!modal || !body) return;
    envCurrentTaskId = t.id;
    modal.hidden = false;
    body.innerHTML = '<span class="env-muted">加载中…</span>';
    safeEmit('task:env-subscribe', {task_id: t.id});
    try {
      var r = await afetch('/api/tasks/' + t.id + '/env-log');
      var d = await r.json();
      body.innerHTML = '';
      renderEnvStatus(d.status, d.exit_code, d.vars);
      if (d.lines && d.lines.length) appendEnvLines(d.lines);
      else body.innerHTML = '<span class="env-muted">(暂无输出)</span>';
    } catch (e) {
      body.innerHTML = '<span class="env-stderr">[加载失败] ' + e + '</span>';
    }
  }

  function closeEnvModal() {
    if (envCurrentTaskId) safeEmit('task:env-unsubscribe', {task_id: envCurrentTaskId});
    envCurrentTaskId = null;
    var modal = $('env-modal');
    if (modal) modal.hidden = true;
  }

  /* ============================================================
     Git Pull
     ============================================================ */
  function initGitTerm() {
    var wrap = $('git-term-wrap');
    if (!wrap) return;
    if (gitTerm) { try { gitTerm.dispose(); } catch (e) {} gitTerm = null; }
    gitTerm = new window.Terminal(window.XTERM_OPTS(12));
    gitFit = new window.FitAddon.FitAddon();
    gitTerm.loadAddon(gitFit);
    gitTerm.open(wrap);
    requestAnimationFrame(function() { try { gitFit.fit(); } catch (e) {} });
  }

  function setGitBadge(text, cls) {
    var badge = $('git-status-badge');
    if (!badge) return;
    badge.textContent = text;
    badge.className = 'env-status-badge ' + (cls || '');
  }
  function setGitExit(code) {
    var el = $('git-exit');
    if (!el) return;
    if (code === null || code === undefined) { el.textContent = ''; el.style.color = ''; }
    else { el.textContent = '退出码 ' + code; el.style.color = code === 0 ? 'var(--success)' : 'var(--danger)'; }
  }

  async function doGitPull(t) {
    var modal = $('git-modal');
    if (!modal) return;
    if (gitCurrentTaskId && gitCurrentTaskId !== t.id) {
      safeEmit('git:unsubscribe', {task_id: gitCurrentTaskId});
    }
    gitCurrentTaskId = t.id;
    gitRunning = false;
    modal.hidden = false;
    if ($('git-cwd')) $('git-cwd').textContent = t.cwd || '';
    setGitBadge('就绪', ''); setGitExit(null);
    initGitTerm();
    if (gitTerm) {
      gitTerm.clear();
      gitTerm.write('\x1b[36m# git pull @ ' + (t.cwd || '') + '\x1b[0m\r\n');
    }
    safeEmit('git:subscribe', {task_id: t.id});
    try {
      var r = await afetch('/api/tasks/' + t.id + '/git-pull', {method: 'POST'});
      var d = await r.json().catch(function() { return {}; });
      if (d.error) {
        if (gitTerm) gitTerm.write('\r\n\x1b[31m[错误] ' + d.error + '\x1b[0m\r\n');
        setGitBadge('失败', 'err'); setGitExit(-1); return;
      }
      gitRunning = true;
      setGitBadge('执行中', 'running');
    } catch (e) {
      if (gitTerm) gitTerm.write('\r\n\x1b[31m[网络错误] ' + e + '\x1b[0m\r\n');
      setGitBadge('失败', 'err');
    }
  }

  function closeGitModal() {
    if (gitCurrentTaskId) safeEmit('git:unsubscribe', {task_id: gitCurrentTaskId});
    gitCurrentTaskId = null;
    if (gitTerm) { try { gitTerm.dispose(); } catch (e) {} gitTerm = null; }
    var modal = $('git-modal');
    if (modal) modal.hidden = true;
  }

  /* ============================================================
     在此处打开终端
     ============================================================ */
  function openTerminalInDir(t) {
    var modal = $('term-picker-modal');
    if (!modal) return;
    var cwdEl = $('term-picker-cwd'), cmdEl = $('term-picker-cmd'), msg = $('term-picker-msg');
    if (cwdEl) cwdEl.value = t.cwd || '';
    if (cmdEl) {
      var isWin = /Windows/i.test(navigator.userAgent);
      cmdEl.value = isWin ? 'powershell.exe' : '/bin/bash';
    }
    if (msg) { msg.textContent = ''; msg.className = 'form-msg'; }
    modal.dataset.taskId = t.id;
    modal.dataset.cwd = t.cwd || '';
    modal.hidden = false;
    setTimeout(function() { if (cmdEl) cmdEl.focus(); }, 50);
  }

  function closeTermPicker() {
    var modal = $('term-picker-modal');
    if (modal) modal.hidden = true;
  }

  function confirmOpenTerminal() {
    var modal = $('term-picker-modal');
    if (!modal) return;
    var cwd = modal.dataset.cwd || '';
    var command = ($('term-picker-cmd') && $('term-picker-cmd').value || '').trim();
    var msg = $('term-picker-msg');
    if (!command) { if (msg) { msg.textContent = '请输入终端命令'; msg.className = 'form-msg err'; } return; }
    window.__pendingTermCreate = {command: command, cwd: cwd};
    closeTermPicker();
    navigate('terminal');
  }

  /* ============================================================
     Socket 事件
     ============================================================ */
  function bindSocketEvents() {
    if (!window.__socket) return;

    window.__socket.on('task:env-snapshot', function(data) {
      if (data.task_id !== envCurrentTaskId) return;
      var body = $('env-body');
      if (body) body.innerHTML = '';
      renderEnvStatus(data.status, data.exit_code, data.vars);
      if (data.lines && data.lines.length) appendEnvLines(data.lines);
      else if (body) body.innerHTML = '<span class="env-muted">(暂无输出)</span>';
    });
    window.__socket.on('task:env-output', function(data) {
      if (data.task_id !== envCurrentTaskId) return;
      var body = $('env-body');
      if (body && body.querySelector('.env-muted')) body.innerHTML = '';
      appendEnvLines([data]);
    });
    window.__socket.on('task:env-status', function(data) {
      if (data.task_id !== envCurrentTaskId) return;
      renderEnvStatus(data.status, data.exit_code, data.vars);
    });

    window.__socket.on('task:status', function(data) {
      if (data.task_id !== currentTaskId) return;
      updateActionButtons(data.status);
    });

    window.__socket.on('git:output', function(data) {
      if (!gitTerm) return;
      if (data.task_id !== gitCurrentTaskId) return;
      try { gitTerm.write(data.data); } catch (e) {}
    });
    window.__socket.on('git:done', function(data) {
      if (data.task_id !== gitCurrentTaskId) return;
      gitRunning = false;
      var code = data.exit_code;
      if (code === 0) {
        setGitBadge('成功', 'ok');
        if (gitTerm) gitTerm.write('\r\n\x1b[32m[git pull 完成]\x1b[0m\r\n');
      } else if (code === -1) {
        setGitBadge('已中止', 'err');
        if (gitTerm) gitTerm.write('\r\n\x1b[33m[已中止]\x1b[0m\r\n');
      } else {
        setGitBadge('失败', 'err');
        if (gitTerm) gitTerm.write('\r\n\x1b[31m[git pull 失败: 退出码 ' + code + ']\x1b[0m\r\n');
      }
      setGitExit(code);
    });
  }

  /* ============================================================
     启动
     ============================================================ */
  function boot() {
    waitFor(['task-list', 'task-detail', 'btn-new-task'], 3000, function() {
      var btnNew = $('btn-new-task');
      if (btnNew) btnNew.onclick = function() { openTaskModal(null); };

      var tmClose = $('task-modal-close'), tmCancel = $('task-modal-cancel'), tmSave = $('task-modal-save');
      if (tmClose) tmClose.onclick = closeTaskModal;
      if (tmCancel) tmCancel.onclick = closeTaskModal;
      if (tmSave) tmSave.onclick = saveTaskModal;

      var tmBrowse = $('tm-browse');
      if (tmBrowse) tmBrowse.onclick = function() {
        var cur = ($('tm-cwd') && $('tm-cwd').value || '').trim();
        openDirModal('tm-cwd', cur);
      };

      var dirClose = $('dir-modal-close'), dirCancel = $('dir-cancel'), dirUp = $('dir-up');
      var dirGo = $('dir-go'), dirChoose = $('dir-choose'), dirPathInput = $('dir-path-input');
      if (dirClose) dirClose.onclick = closeDirModal;
      if (dirCancel) dirCancel.onclick = closeDirModal;
      if (dirUp) dirUp.onclick = function() {
        if (!dirCurrentPath) return;
        var parent = dirCurrentPath.replace(/[/\\][^/\\]+$/, '') || '/';
        dirBrowse(parent);
      };
      if (dirGo) dirGo.onclick = function() { if (dirPathInput) dirBrowse(dirPathInput.value.trim()); };
      if (dirPathInput) dirPathInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') dirBrowse(dirPathInput.value.trim());
        if (e.key === 'Escape') closeDirModal();
      });
      if (dirChoose) dirChoose.onclick = function() {
        if (dirTargetInputId && dirCurrentPath) {
          var el = $(dirTargetInputId);
          if (el) el.value = dirCurrentPath;
        }
        closeDirModal();
      };

      var hClose = $('history-modal-close'), hOk = $('history-modal-ok');
      if (hClose) hClose.onclick = function() { if ($('history-modal')) $('history-modal').hidden = true; };
      if (hOk) hOk.onclick = function() { if ($('history-modal')) $('history-modal').hidden = true; };

      var eClose = $('env-modal-close'), eOk = $('env-modal-ok'), eRefresh = $('env-refresh');
      if (eClose) eClose.onclick = closeEnvModal;
      if (eOk) eOk.onclick = closeEnvModal;
      if (eRefresh) eRefresh.onclick = function() {
        if (envCurrentTaskId) openEnvModal({id: envCurrentTaskId});
      };

      var gClose = $('git-modal-close'), gOk = $('git-modal-ok'), gKill = $('git-kill'), gitModal = $('git-modal');
      if (gClose) gClose.onclick = closeGitModal;
      if (gOk) gOk.onclick = closeGitModal;
      if (gKill) gKill.onclick = async function() {
        if (!gitCurrentTaskId) return;
        try { await afetch('/api/tasks/' + gitCurrentTaskId + '/git-kill', {method: 'POST'}); } catch (e) {}
      };
      if (gitModal) gitModal.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeGitModal(); });

      var tpClose = $('term-picker-close'), tpCancel = $('term-picker-cancel'), tpOpen = $('term-picker-open');
      var tpCmd = $('term-picker-cmd'), tpModal = $('term-picker-modal');
      if (tpClose) tpClose.onclick = closeTermPicker;
      if (tpCancel) tpCancel.onclick = closeTermPicker;
      if (tpOpen) tpOpen.onclick = confirmOpenTerminal;
      if (tpCmd) tpCmd.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); confirmOpenTerminal(); }
        if (e.key === 'Escape') closeTermPicker();
      });
      if (tpModal) tpModal.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeTermPicker(); });

      var taskModal = $('task-modal');
      if (taskModal) taskModal.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') { e.preventDefault(); saveTaskModal(); }
        if (e.key === 'Escape') closeTaskModal();
      });
      var historyModal = $('history-modal');
      if (historyModal) historyModal.addEventListener('keydown', function(e) { if (e.key === 'Escape') historyModal.hidden = true; });
      var envModal = $('env-modal');
      if (envModal) envModal.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeEnvModal(); });

      bindSocketEvents();
      refreshList();
    });
  }

  window.__registerCleanup && window.__registerCleanup(function() {
    unregisterPreview();
    if (gitTerm) { try { gitTerm.dispose(); } catch (e) {} gitTerm = null; }
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();