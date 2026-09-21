/* PSh Panel - 通用工具 */

window.fmt = {
  bytes(n) {
    if (n === undefined || n === null) return '-';
    const units = ['B','KB','MB','GB','TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  },
  rate(n) { return window.fmt.bytes(n) + '/s'; },
  time(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    return d.toLocaleString('zh-CN', {hour12: false});
  },
  duration(sec) {
    if (!sec && sec !== 0) return '-';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (d) return `${d}天 ${h}时`;
    if (h) return `${h}时 ${m}分`;
    if (m) return `${m}分 ${s}秒`;
    return `${s}秒`;
  }
};

window.XTERM_THEME = {
  background: '#05070d',
  foreground: '#e6edf7',
  cursor: '#6ee7ff',
  cursorAccent: '#05070d',
  selectionBackground: 'rgba(79,158,255,0.35)',
  black: '#1a2236', red: '#ff5c7c', green: '#4fd18b', yellow: '#ffb45c',
  blue: '#4f9eff', magenta: '#c792ea', cyan: '#6ee7ff', white: '#e6edf7',
  brightBlack: '#5c6b85', brightRed: '#ff8aa3', brightGreen: '#7ce8b0',
  brightYellow: '#ffd28f', brightBlue: '#82b8ff', brightMagenta: '#dcb3ff',
  brightCyan: '#a3f0ff', brightWhite: '#ffffff',
};

window.XTERM_OPTS = (fontSize = 14) => ({
  cursorBlink: true,
  fontSize,
  fontFamily: '"SF Mono", Consolas, Menlo, monospace',
  theme: window.XTERM_THEME,
  scrollback: (window.__settings?.scrollback) || 5000,
  allowProposedApi: true,
  convertEol: false,
});

window.SparkChart = class {
  constructor(canvas, color = '#4f9eff', maxPoints = 60) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.color = color;
    this.points = [];
    this.maxPoints = maxPoints;
    this.maxValue = 100;
    this._resize();
    window.addEventListener('resize', () => this._resize());
  }
  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = rect.width * dpr;
    this.canvas.height = rect.height * dpr;
    this.ctx.scale(dpr, dpr);
    this.w = rect.width;
    this.h = rect.height;
  }
  push(v) {
    this.points.push(Math.max(0, v));
    if (this.points.length > this.maxPoints) this.points.shift();
    if (v > this.maxValue) this.maxValue = v;
    this.draw();
  }
  draw() {
    const {ctx, w, h, points} = this;
    ctx.clearRect(0, 0, w, h);
    if (points.length < 2) return;
    const step = w / (this.maxPoints - 1);
    const norm = (v) => h - (v / this.maxValue) * (h - 4) - 2;
    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < points.length; i++) {
      ctx.lineTo(i * step, norm(points[i]));
    }
    ctx.lineTo((points.length - 1) * step, h);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, this.color + '55');
    grad.addColorStop(1, this.color + '00');
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < points.length; i++) {
      const x = i * step, y = norm(points[i]);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }
};

/* ============================================================
 * 全局任务输出总线
 * 所有 task:* 事件只在这里注册一次，各页面通过
 * window.__taskTerms[task_id] = { term, onStatus } 接收
 * ============================================================ */
window.__taskTerms = {};

window.initGlobalSocketHandlers = function(socket) {
  socket.on('task:output', (data) => {
    const entry = window.__taskTerms[data.task_id];
    if (entry && entry.term) {
      try { entry.term.write(data.data); } catch(e) {}
    }
  });

  socket.on('task:snapshot', (data) => {
    const entry = window.__taskTerms[data.task_id];
    if (!entry || !entry.term) return;
    (data.lines || []).forEach(l => {
      try { entry.term.write(l.data); } catch(e) {}
    });
  });

  socket.on('task:status', (data) => {
    const entry = window.__taskTerms[data.task_id];
    if (entry && entry.onStatus) {
      try { entry.onStatus(data); } catch(e) {}
    }
  });
};