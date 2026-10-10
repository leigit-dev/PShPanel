(function() {
  const cpuCanvas = document.getElementById('cpu-chart');
  const memCanvas = document.getElementById('mem-chart');
  const netCanvas = document.getElementById('net-chart');
  if (!cpuCanvas) return;

  const charts = {
    cpu: new SparkChart(cpuCanvas, '#4f9eff'),
    mem: new SparkChart(memCanvas, '#c792ea'),
    net: new SparkChart(netCanvas, '#4fd18b'),
  };
  const cpuEl = document.getElementById('perf-cpu');
  const memEl = document.getElementById('perf-mem');
  const netEl = document.getElementById('perf-net-rate');
  const diskEl = document.getElementById('perf-disk');
  const cpuInfo = document.getElementById('perf-cpu-info');
  const memInfo = document.getElementById('perf-mem-info');
  const netTotal = document.getElementById('perf-net-total');
  const diskInfo = document.getElementById('perf-disk-info');
  const taskBody = document.getElementById('task-perf-body');

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  function render(d) {
    if (!d) return;

    // ---- CPU 主数值 ----
    cpuEl.textContent = (d.cpu.percent || 0).toFixed(1);

    // ---- CPU 副信息：核心数 · 频率 · 负载 · 温度 · 运行时长 ----
    var infoParts = [];
    infoParts.push((d.cpu.count || 0) + ' 核');
    if (d.cpu.freq) {
      var freqStr = d.cpu.freq.toFixed(0) + ' MHz';
      if (d.cpu.freq_max && d.cpu.freq_max > 0 &&
          Math.abs(d.cpu.freq_max - d.cpu.freq) > 50) {
        freqStr += ' / ' + d.cpu.freq_max.toFixed(0) + ' MHz';
      }
      infoParts.push(freqStr);
    }
    if (d.cpu.load && typeof d.cpu.load['1m'] === 'number') {
      infoParts.push('负载 ' + d.cpu.load['1m'].toFixed(2));
    }
    if (d.cpu.temperature !== null && d.cpu.temperature !== undefined) {
      infoParts.push(d.cpu.temperature.toFixed(1) + '°C');
    }
    infoParts.push('运行 ' + fmt.duration(d.uptime));
    cpuInfo.textContent = infoParts.join(' · ');

    // ---- 内存 ----
    memEl.textContent = (d.memory.percent || 0).toFixed(1);
    memInfo.textContent = `${fmt.bytes(d.memory.used)} / ${fmt.bytes(d.memory.total)}` +
      (d.memory.swap_total ? ` · Swap ${fmt.bytes(d.memory.swap_used)}/${fmt.bytes(d.memory.swap_total)}` : '');

    // ---- 网络 ----
    const down = d.net.recv_rate || 0, up = d.net.sent_rate || 0;
    netEl.textContent = `↓ ${fmt.rate(down)} / ↑ ${fmt.rate(up)}`;
    netTotal.textContent = `累计 ↓ ${fmt.bytes(d.net.recv_total)} · ↑ ${fmt.bytes(d.net.sent_total)}`;

    // ---- 磁盘 ----
    diskEl.textContent = (d.disk.percent || 0).toFixed(1);
    diskInfo.textContent = `${fmt.bytes(d.disk.used)} / ${fmt.bytes(d.disk.total)}`;

    // ---- 图表 ----
    charts.cpu.push(d.cpu.percent || 0);
    charts.mem.push(d.memory.percent || 0);
    charts.net.push((down + up) / 1024);
    if (charts.net.maxValue < 100) charts.net.maxValue = 100;

    // ---- 任务表 ----
    const tasks = d.tasks || [];
    taskBody.innerHTML = '';
    if (!tasks.length) {
      taskBody.innerHTML = '<tr><td colspan="7" style="color:var(--text-2);text-align:center;">暂无任务</td></tr>';
    } else {
      tasks.forEach(t => {
        const tr = document.createElement('tr');
        const disk = `${fmt.rate(t.disk_read || 0)} / ${fmt.rate(t.disk_write || 0)}`;
        let net;
        if (t.net_supported) {
          net = `↓ ${fmt.rate(t.net_recv || 0)} / ↑ ${fmt.rate(t.net_sent || 0)}`;
          if (t.net_estimated) net += ' <span class="perf-tag">估</span>';
        } else net = '<span class="perf-na">—</span>';
        tr.innerHTML = `
          <td>${escapeHtml(t.name)}</td>
          <td>${t.status}</td>
          <td>${t.pid || '-'}</td>
          <td>${(t.cpu || 0).toFixed(1)}%</td>
          <td>${fmt.bytes(t.memory)}</td>
          <td class="cell-io">${disk}</td>
          <td class="cell-io">${net}</td>
        `;
        taskBody.appendChild(tr);
      });
    }
  }

  function onPerfData(d) { render(d); }
  window.__socket.on('perf:data', onPerfData);

  async function bootstrap() {
    try {
      const r = await (window.accessFetch || fetch)('/api/perf');
      render(await r.json());
    } catch (e) {}
    try { window.__socket.emit('perf:subscribe'); } catch (e) {}
  }
  bootstrap();

  window.__registerCleanup && window.__registerCleanup(() => {
    try { window.__socket.emit('perf:unsubscribe'); } catch (e) {}
    try { window.__socket.off('perf:data', onPerfData); } catch (e) {}
  });
})();