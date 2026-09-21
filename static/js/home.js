/* PSh Panel - 首页（任务列表 + 详情 + 预览 + 配置弹窗 + 历史弹窗） */
(function() {
  let currentTaskId = null;
  let detailTerm = null;
  let detailFit = null;
  let previewRO = null;

  const listEl = document.getElementById('task-list');
  const detailEl = document.getElementById('task-detail');
  if (!listEl || !detailEl) return;

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  /* ============================================================
   * 任务列表
   * ============================================================ */
  async function refreshList() {
    const r = await fetch('/api/tasks');
    const tasks = await r.json();
    listEl.innerHTML = '';
    tasks.forEach(t => {
      const el = document.createElement('div');
      el.className = 'task-item' + (t.id === currentTaskId ? ' active' : '');
      const statusClass = t.status === 'running' ? 'status-running' : 'status-stopped';
      const statusText = t.status === 'running' ? '运行中'
                       : (t.status === 'exited' ? '已退出' : '已停止');
      el.innerHTML = `
        <div class="name"><span>${escapeHtml(t.name)}</span>
          <span class="status-badge ${statusClass}">${statusText}</span></div>
        <div class="cmd">${escapeHtml(t.command)} · ${t.mode.toUpperCase()}</div>
      `;
      el.onclick = () => selectTask(t.id);
      listEl.appendChild(el);
    });
  }

  /* ============================================================
   * 预览终端生命周期
   * ============================================================ */
  function unregisterPreview() {
    if (previewRO) { try { previewRO.disconnect(); } catch(e){} previewRO = null; }
    if (detailTerm) { try { detailTerm.dispose(); } catch(e){} detailTerm = null; }
    if (currentTaskId) {
      if (window.__taskTerms[currentTaskId]) {
        delete window.__taskTerms[currentTaskId];
      }
      try { window.__socket.emit('task:unsubscribe', {task_id: currentTaskId}); }
      catch(e) {}
    }
  }

  function sendPreviewResize(taskId, mode) {
    if (!detailFit || mode !== 'pty') return;
    let dims = null;
    try { dims = detailFit.proposeDimensions(); } catch(e) { return; }
    if (!dims) return;
    const cols = Number(dims.cols);
    const rows = Number(dims.rows);
    if (!Number.isFinite(cols) || !Number.isFinite(rows)) return;
    if (cols < 2 || rows < 2) return;
    try {
      window.__socket.emit('task:resize', {
        task_id: taskId,
        cols: Math.floor(cols),
        rows: Math.floor(rows),
      });
    } catch(e) {}
  }

  /* ============================================================
   * 选中任务 → 渲染详情
   * ============================================================ */
  async function selectTask(id) {
    unregisterPreview();
    currentTaskId = id;
    await refreshList();

    const r = await fetch(`/api/tasks/${id}`);
    const t = await r.json();
    const statusText = t.status === 'running' ? '运行中'
                     : (t.status === 'exited' ? '已退出' : '已停止');
    detailEl.innerHTML = `
      <div class="panel-header">
        <h2>${escapeHtml(t.name)}</h2>
        <span class="status-badge ${t.status === 'running' ? 'status-running' : 'status-stopped'}">${statusText}</span>
      </div>
      <div class="detail-section">
        <h3>启动命令</h3>
        <div class="value">${escapeHtml(t.command)}</div>
      </div>
      <div class="detail-section">
        <h3>工作目录 · 模式</h3>
        <div class="value">${escapeHtml(t.cwd)} · ${t.mode.toUpperCase()}</div>
      </div>
      <div class="detail-section">
        <h3>Shell 预览（点击进入全屏）</h3>
        <div class="shell-preview" id="detail-term"></div>
      </div>
      <div class="detail-section">
        <h3>操作</h3>
        <div class="detail-actions">
          ${t.status !== 'running'
            ? `<button data-act="start">启动</button>`
            : `<button data-act="stop" class="danger">停止</button>`}
          <button data-act="restart">重启</button>
          <button data-act="toggle-enable">${t.enabled ? '禁用自启' : '启用自启'}</button>
          <button data-act="edit">编辑</button>
          <button data-act="history">历史</button>
          <button data-act="delete" class="danger">删除</button>
        </div>
      </div>
    `;

    const termEl = detailEl.querySelector('#detail-term');
    detailTerm = new Terminal(window.XTERM_OPTS(window.__settings?.font_size || 14));
    detailFit = new FitAddon.FitAddon();
    detailTerm.loadAddon(detailFit);
    detailTerm.open(termEl);
    requestAnimationFrame(() => { try { detailFit.fit(); } catch(e) {} });

    window.__taskTerms[id] = {
      term: detailTerm,
      onStatus: () => { refreshList(); },
    };

    try { window.__socket.emit('task:subscribe', {task_id: id}); } catch(e) {}

    if (t.mode === 'pty') {
      detailTerm.onData(d => {
        try { window.__socket.emit('task:input', {task_id: id, data: d}); }
        catch(e) {}
      });
    }

    setTimeout(() => {
      try { detailFit.fit(); } catch(e) {}
      sendPreviewResize(id, t.mode);
    }, 100);

    previewRO = new ResizeObserver(() => {
      if (!detailFit) return;
      try { detailFit.fit(); } catch(e) {}
      sendPreviewResize(id, t.mode);
    });
    previewRO.observe(termEl);

    termEl.onclick = () => {
      window.__pendingTaskFullscreen = id;
      unregisterPreview();
      navigate('terminal');
    };

    detailEl.querySelectorAll('[data-act]').forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === 'edit') return openTaskModal(t);
        if (act === 'history') return showHistory(t);
        if (act === 'toggle-enable') {
          await fetch(`/api/tasks/${id}`, {method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: !t.enabled})});
          return selectTask(id);
        }
        if (act === 'delete') {
          if (!confirm('确定删除该任务？')) return;
          unregisterPreview();
          await fetch(`/api/tasks/${id}`, {method: 'DELETE'});
          currentTaskId = null;
          detailEl.innerHTML = '<div class="empty-hint">选择一个任务查看详情</div>';
          return refreshList();
        }
        await fetch(`/api/tasks/${id}/${act}`, {method: 'POST'});
        setTimeout(() => selectTask(id), 300);
      };
    });
  }

  /* ============================================================
   * 任务配置弹窗
   * ============================================================ */
  const taskModal = document.getElementById('task-modal');
  const taskMsg = document.getElementById('tm-msg');

  function setMsg(text, isErr) {
    taskMsg.textContent = text || '';
    taskMsg.className = 'form-msg' + (isErr ? ' err' : (text ? ' ok' : ''));
  }

  function openTaskModal(task) {
    const isEdit = !!task;
    document.getElementById('task-modal-title').textContent =
      isEdit ? '编辑任务' : '新建任务';
    document.getElementById('tm-name').value = task?.name || '';
    document.getElementById('tm-command').value = task?.command || '';
    document.getElementById('tm-cwd').value = task?.cwd || '';
    document.getElementById('tm-mode').value = task?.mode || 'pipe';
    document.getElementById('tm-enabled').checked = !!task?.enabled;
    setMsg('');
    taskModal.dataset.editing = task?.id || '';
    taskModal.hidden = false;
    setTimeout(() => document.getElementById('tm-name').focus(), 50);
  }

  function closeTaskModal() {
    taskModal.hidden = true;
    taskModal.dataset.editing = '';
  }

  async function saveTaskModal() {
    const name = document.getElementById('tm-name').value.trim();
    const command = document.getElementById('tm-command').value.trim();
    const cwd = document.getElementById('tm-cwd').value.trim();
    const mode = document.getElementById('tm-mode').value;
    const enabled = document.getElementById('tm-enabled').checked;

    if (!name) { setMsg('请填写任务名称', true); return; }
    if (!command) { setMsg('请填写启动命令', true); return; }

    const editing = taskModal.dataset.editing;
    const payload = {name, command, mode, enabled};
    if (cwd) payload.cwd = cwd;

    const url = editing ? `/api/tasks/${editing}` : '/api/tasks';
    const method = editing ? 'PUT' : 'POST';

    let r;
    try {
      r = await fetch(url, {
        method,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
    } catch (e) {
      setMsg('网络错误：' + e, true);
      return;
    }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      setMsg(d.error || '保存失败', true);
      return;
    }
    const saved = await r.json();
    closeTaskModal();
    await refreshList();
    const id = saved.id || editing;
    if (id) selectTask(id);
  }

  document.getElementById('btn-new-task').onclick = () => openTaskModal(null);
  document.getElementById('task-modal-close').onclick = closeTaskModal;
  document.getElementById('task-modal-cancel').onclick = closeTaskModal;
  document.getElementById('task-modal-save').onclick = saveTaskModal;

  taskModal.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') {
      e.preventDefault();
      saveTaskModal();
    }
    if (e.key === 'Escape') closeTaskModal();
  });

  /* ============================================================
   * 目录选择弹窗
   * ============================================================ */
  const dirModal = document.getElementById('dir-modal');
  const dirList = document.getElementById('dir-list');
  const dirPathInput = document.getElementById('dir-path-input');
  let dirCurrentPath = '';
  let dirTargetInputId = null;

  async function openDirModal(targetInputId, startPath) {
    dirTargetInputId = targetInputId;
    dirModal.hidden = false;
    let path = startPath || '';
    if (!path) {
      try {
        const r = await fetch('/api/files/roots');
        const d = await r.json();
        if (d.roots && d.roots.length) path = d.roots[0].path;
      } catch(e) {}
    }
    await dirBrowse(path);
  }

  async function dirBrowse(path) {
    if (!path) return;
    dirList.innerHTML = '<div class="dir-empty">加载中…</div>';
    let data;
    try {
      const r = await fetch(`/api/files/list?path=${encodeURIComponent(path)}`);
      if (!r.ok) {
        dirList.innerHTML = '<div class="dir-empty">无法访问该目录</div>';
        return;
      }
      data = await r.json();
    } catch (e) {
      dirList.innerHTML = '<div class="dir-empty">网络错误</div>';
      return;
    }
    if (data.file) {
      const parent = data.file.path.replace(/[/\\][^/\\]+$/, '') || '/';
      return dirBrowse(parent);
    }
    dirCurrentPath = data.path;
    dirPathInput.value = data.path;
    dirList.innerHTML = '';
    const dirs = (data.entries || []).filter(e => e.is_dir);
    if (!dirs.length) {
      dirList.innerHTML = '<div class="dir-empty">此目录下没有子文件夹</div>';
      return;
    }
    dirs.forEach(e => {
      const el = document.createElement('div');
      el.className = 'dir-item';
      el.innerHTML = `<span class="dir-icon">📁</span><span>${escapeHtml(e.name)}</span>`;
      el.onclick = () => dirBrowse(e.path);
      dirList.appendChild(el);
    });
  }

  function closeDirModal() {
    dirModal.hidden = true;
    dirTargetInputId = null;
  }

  document.getElementById('tm-browse').onclick = () => {
    const cur = document.getElementById('tm-cwd').value.trim();
    openDirModal('tm-cwd', cur);
  };
  document.getElementById('dir-modal-close').onclick = closeDirModal;
  document.getElementById('dir-cancel').onclick = closeDirModal;
  document.getElementById('dir-up').onclick = () => {
    if (!dirCurrentPath) return;
    const parent = dirCurrentPath.replace(/[/\\][^/\\]+$/, '') || '/';
    dirBrowse(parent);
  };
  document.getElementById('dir-go').onclick = () => {
    dirBrowse(dirPathInput.value.trim());
  };
  dirPathInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') dirBrowse(dirPathInput.value.trim());
    if (e.key === 'Escape') closeDirModal();
  });
  document.getElementById('dir-choose').onclick = () => {
    if (dirTargetInputId && dirCurrentPath) {
      const el = document.getElementById(dirTargetInputId);
      if (el) el.value = dirCurrentPath;
    }
    closeDirModal();
  };

  /* ============================================================
   * 历史记录弹窗
   * ============================================================ */
  const historyModal = document.getElementById('history-modal');
  const historyList = document.getElementById('history-list');

  async function showHistory(t) {
    historyList.innerHTML = '<div class="history-empty">加载中…</div>';
    historyModal.hidden = false;

    let hist = [];
    try {
      const r = await fetch(`/api/tasks/${t.id}/history`);
      hist = await r.json();
    } catch (e) {
      historyList.innerHTML = '<div class="history-empty">加载失败</div>';
      return;
    }

    if (!hist.length) {
      historyList.innerHTML = '<div class="history-empty">暂无历史记录</div>';
      return;
    }

    // 倒序：最近一次在最上面
    const reversed = hist.slice().reverse();
    const total = hist.length;

    historyList.innerHTML = '';
    reversed.forEach((h, idx) => {
      const dur = (h.ended_at || 0) - (h.started_at || 0);
      const exit = h.exit_code;
      const exitClass = (exit === 0) ? 'ok' : 'err';
      const lines = (h.tail || []).map(x => x.data).join('');
      const lineCount = (h.tail || []).length;

      const card = document.createElement('div');
      card.className = 'history-card';

      const head = document.createElement('div');
      head.className = 'history-card-head';
      head.innerHTML = `
        <span class="history-index">#${total - idx}</span>
        <span class="history-time">${fmt.time(h.started_at)}</span>
        <span class="history-arrow">→</span>
        <span class="history-time">${fmt.time(h.ended_at)}</span>
        <span class="history-dur">${fmt.duration(dur)}</span>
        <span class="history-exit ${exitClass}">退出码 ${exit}</span>
        <span class="history-toggle">展开</span>
      `;

      const body = document.createElement('pre');
      body.className = 'history-body';
      body.textContent = lineCount
        ? lines
        : '(该次运行没有保存输出)';

      head.onclick = () => {
        const open = card.classList.toggle('expanded');
        head.querySelector('.history-toggle').textContent = open ? '收起' : '展开';
      };

      card.appendChild(head);
      card.appendChild(body);
      historyList.appendChild(card);
    });
  }

  document.getElementById('history-modal-close').onclick = () => {
    historyModal.hidden = true;
  };
  document.getElementById('history-modal-ok').onclick = () => {
    historyModal.hidden = true;
  };
  historyModal.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') historyModal.hidden = true;
  });

  /* ============================================================
   * 页面清理
   * ============================================================ */
  window.__registerCleanup && window.__registerCleanup(() => {
    unregisterPreview();
  });

  refreshList();
})();