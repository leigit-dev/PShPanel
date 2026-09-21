(function() {
  const rootsEl = document.getElementById('roots-list');
  const listEl = document.getElementById('files-list');
  const pathEl = document.getElementById('path-input');
  const editorModal = document.getElementById('editor-modal');
  const editorTitle = document.getElementById('editor-title');
  const editorContent = document.getElementById('editor-content');
  let currentPath = '/';
  let currentEditPath = null;

  async function loadRoots() {
    const r = await fetch('/api/files/roots');
    const data = await r.json();
    rootsEl.innerHTML = '';
    data.roots.forEach(root => {
      const el = document.createElement('div');
      el.className = 'root-item';
      el.innerHTML = `<span>${root.name}</span><span class="type">${root.type}</span>`;
      el.onclick = () => browse(root.path);
      rootsEl.appendChild(el);
    });
    // 默认浏览第一个
    if (data.roots.length) browse(data.roots[0].path);
  }

  async function browse(path) {
    if (!path) return;
    const r = await fetch(`/api/files/list?path=${encodeURIComponent(path)}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert('无法访问: ' + (err.error || r.status));
      return;
    }
    const data = await r.json();
    if (data.file) {
      // 是文件，弹出编辑
      return openEditor(data.file.path);
    }
    currentPath = data.path;
    pathEl.value = data.path;
    listEl.innerHTML = '';
    data.entries.forEach(e => listEl.appendChild(renderRow(e)));
  }

  function renderRow(e) {
    const row = document.createElement('div');
    row.className = 'file-row';
    const icon = e.is_dir ? '📁' : (isText(e.name) ? '📄' : '📦');
    row.innerHTML = `
      <div class="name">${icon} ${escapeHtml(e.name)}</div>
      <div class="size">${e.is_dir ? '-' : fmt.bytes(e.size)}</div>
      <div class="mode">${e.mode}</div>
      <div class="actions">
        ${e.is_dir ? '' : `<button data-act="download">下载</button>`}
        ${e.is_dir ? '' : `<button data-act="edit">编辑</button>`}
        <button data-act="rename">重命名</button>
        <button data-act="delete">删除</button>
      </div>
    `;
    row.onclick = (ev) => {
      if (ev.target.tagName === 'BUTTON') return;
      if (e.is_dir) browse(e.path);
      else openEditor(e.path);
    };
    row.querySelectorAll('button').forEach(btn => {
      btn.onclick = (ev) => {
        ev.stopPropagation();
        handleAction(btn.dataset.act, e);
      };
    });
    return row;
  }

  function isText(name) {
    const ext = name.split('.').pop().toLowerCase();
    return ['txt','md','py','js','ts','json','html','css','yml','yaml','ini','conf',
            'log','sh','bat','ps1','xml','csv','toml','env','gitignore','cfg'].includes(ext);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  async function handleAction(act, e) {
    if (act === 'download') {
      window.location.href = `/api/files/download?path=${encodeURIComponent(e.path)}`;
      return;
    }
    if (act === 'edit') return openEditor(e.path);
    if (act === 'rename') {
      const name = prompt('新名称', e.name);
      if (!name || name === e.name) return;
      await fetch('/api/files/rename', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: e.path, name})});
      return browse(currentPath);
    }
    if (act === 'delete') {
      if (!confirm(`确定删除 ${e.name}？`)) return;
      await fetch('/api/files/delete', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: e.path})});
      return browse(currentPath);
    }
  }

  async function openEditor(path) {
    const r = await fetch(`/api/files/read?path=${encodeURIComponent(path)}`);
    if (!r.ok) { alert('无法打开文件'); return; }
    const data = await r.json();
    currentEditPath = path;
    editorTitle.textContent = path;
    editorContent.value = data.content;
    editorModal.hidden = false;
  }

  document.getElementById('editor-close').onclick = () => {
    editorModal.hidden = true;
    currentEditPath = null;
  };
  document.getElementById('editor-save').onclick = async () => {
    if (!currentEditPath) return;
    await fetch('/api/files/write', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: currentEditPath, content: editorContent.value}),
    });
    editorModal.hidden = true;
  };

  document.getElementById('btn-up').onclick = () => {
    const parent = currentPath.replace(/[/\\][^/\\]+$/, '') || '/';
    browse(parent || '/');
  };
  document.getElementById('btn-refresh').onclick = () => browse(currentPath);
  pathEl.onkeydown = (e) => { if (e.key === 'Enter') browse(pathEl.value); };

  document.getElementById('btn-mkdir').onclick = async () => {
    const name = prompt('文件夹名称');
    if (!name) return;
    await fetch('/api/files/mkdir', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: currentPath, name})});
    browse(currentPath);
  };
  document.getElementById('btn-newfile').onclick = async () => {
    const name = prompt('文件名称');
    if (!name) return;
    await fetch('/api/files/newfile', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: currentPath, name})});
    browse(currentPath);
  };
  document.getElementById('file-input').onchange = async (e) => {
    const files = e.target.files;
    if (!files.length) return;
    const fd = new FormData();
    fd.append('path', currentPath);
    for (const f of files) fd.append('files', f);
    await fetch('/api/files/upload', {method: 'POST', body: fd});
    e.target.value = '';
    browse(currentPath);
  };

  loadRoots();
})();