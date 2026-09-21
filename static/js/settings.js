(function() {
  const s = window.__settings || {};
  document.getElementById('set-title').value = s.panel_title || 'PSh Panel';
  document.getElementById('set-scrollback').value = s.scrollback || 5000;
  document.getElementById('set-font').value = s.font_size || 14;
  document.getElementById('set-history').value = s.max_history || 10;
  document.getElementById('set-perf').value = s.perf_interval || 2000;

  document.getElementById('btn-save-settings').onclick = async () => {
    const payload = {
      panel_title: document.getElementById('set-title').value,
      scrollback: parseInt(document.getElementById('set-scrollback').value),
      font_size: parseInt(document.getElementById('set-font').value),
      max_history: parseInt(document.getElementById('set-history').value),
      perf_interval: parseInt(document.getElementById('set-perf').value),
    };
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    window.__settings = await r.json();
    alert('已保存');
  };

  document.getElementById('btn-save-pwd').onclick = async () => {
    const oldPwd = document.getElementById('pwd-old').value;
    const newPwd = document.getElementById('pwd-new').value;
    const newPwd2 = document.getElementById('pwd-new2').value;
    const msg = document.getElementById('pwd-msg');
    msg.className = 'form-msg';
    if (newPwd !== newPwd2) { msg.textContent = '两次输入不一致'; msg.classList.add('err'); return; }
    if (newPwd.length < 4) { msg.textContent = '新密码太短'; msg.classList.add('err'); return; }
    const r = await fetch('/api/password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({old: oldPwd, new: newPwd}),
    });
    const d = await r.json();
    if (d.ok) {
      msg.textContent = '修改成功';
      msg.classList.add('ok');
      document.getElementById('pwd-old').value = '';
      document.getElementById('pwd-new').value = '';
      document.getElementById('pwd-new2').value = '';
    } else {
      msg.textContent = d.error || '失败';
      msg.classList.add('err');
    }
  };
})();