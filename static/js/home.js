(function() {
  const rootsEl = document.getElementById('roots-list');
  const listEl = document.getElementById('files-list');
  const pathEl = document.getElementById('path-input');
  const editorModal = document.getElementById('editor-modal');
  const editorTitle = document.getElementById('editor-title');
  const editorContent = document.getElementById('editor-content');
  let currentPath = '/';
  let currentEditPath = null;

  function afetch(url, options = {}) {
    if (window.accessFetch) return window.accessFetch(url, options);
    options.credentials = 'same-origin';
    options.redirect = 'manual';
    return fetch(url, options);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function escapeAttr(s) { return String(s).replace(/"/g, '&quot;').replace(/</g, '&lt;'); }
  function isText(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    return ['txt','md','py','js','ts','json','html','css','yml','yaml','ini','conf',
            'log','sh','bat','ps1','xml','csv','toml','env','gitignore','cfg'].includes(ext);
  }

  async function loadRoots() {
    const r = await afetch('/api/files/roots');
    const data = await r.json();
    rootsEl.innerHTML = '';
    data.roots.forEach(root => {
      const el = document.createElement('div');
      el.className = 'root-item';
      el.innerHTML = `<span>${escapeHtml(root.name)}</span><span class="type">${escapeHtml(root.type)}</span>`;
      el.onclick = () => browse(root.path);
      rootsEl.appendChild(el);
    });
    if (data.roots.length) browse(data.roots[0].path);
  }

  async function browse(path) {
    if (!path) return;
    let data;
    try {
      const r = await afetch(`/api/files/list?path=${encodeURIComponent(path)}`);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert('无法访问: ' + (err.error || r.status));
        return;
      }
      data = await r.json();
    } catch (e) { return; }
    if (data.file) return openEditor(data.file.path);
    currentPath = data.path;
    pathEl.value = data.path;
    listEl.innerHTML = data.entries.map(renderRow).join('');
  }

  function renderRow(e) {
    const icon = e.is_dir ? '📁' : (isText(e.name) ? '📄' : '📦');
    return `<div class="file-row" data-path="${escapeAttr(e.path)}" data-is-dir="${e.is_dir ? 1 : 0}" data-name="${escapeAttr(e.name)}">
      <div class="name">${icon} ${escapeHtml(e.name)}</div>
      <div class="size">${e.is_dir ? '-' : fmt.bytes(e.size)}</div>
      <div class="mode">${e.mode}</div>
      <div class="actions">
        ${e.is_dir ? '' : '<button data-act="download">下载</button>'}
        ${e.is_dir ? '' : '<button data-act="edit">编辑</button>'}
        <button data-act="rename">重命名</button>
        <button data-act="delete">删除</button>
      </div>
    </div>`;
  }

  listEl.addEventListener('click', (ev) => {
    const row = ev.target.closest('.file-row');
    if (!row) return;
    const path = row.dataset.path;
    const isDir = row.dataset.isDir === '1';
    const name = row.dataset.name;
    const btn = ev.target.closest('button[data-act]');
    if (btn) {
      ev.stopPropagation();
      handleAction(btn.dataset.act, {path, is_dir: isDir, name});
      return;
    }
    if (isDir) browse(path);
    else openEditor(path);
  });

  async function handleAction(act, e) {
    if (act === 'download') {
      window.location.href = `/api/files/download?path=${encodeURIComponent(e.path)}`;
      return;
    }
    if (act === 'edit') return openEditor(e.path);
    if (act === 'rename') {
      const name = prompt('新名称', e.name);
      if (!name || name === e.name) return;
      await afetch('/api/files/rename', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: e.path, name})});
      return browse(currentPath);
    }
    if (act === 'delete') {
      if (!confirm(`确定删除 ${e.name}？`)) return;
      await afetch('/api/files/delete', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: e.path})});
      return browse(currentPath);
    }
  }

  async function openEditor(path) {
    const r = await afetch(`/api/files/read?path=${encodeURIComponent(path)}`);
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
    await afetch('/api/files/write', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
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
    await afetch('/api/files/mkdir', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: currentPath, name})});
    browse(currentPath);
  };
  document.getElementById('btn-newfile').onclick = async () => {
    const name = prompt('文件名称');
    if (!name) return;
    await afetch('/api/files/newfile', {method: 'POST',
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
    await afetch('/api/files/upload', {method: 'POST', body: fd});
    e.target.value = '';
    browse(currentPath);
  };

  loadRoots();
})();