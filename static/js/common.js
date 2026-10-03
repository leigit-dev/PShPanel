/* PSh Panel - common.js */
window.fmt = {
  bytes: function(n) {
    if (n === undefined || n === null) return '-';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
  },
  rate: function(n) { return window.fmt.bytes(n) + '/s'; },
  time: function(ts) {
    if (!ts) return '-';
    return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
  },
  duration: function(sec) {
    if (!sec && sec !== 0) return '-';
    sec = Math.floor(sec);
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    if (d) return d + '\u5929 ' + h + '\u65f6';
    if (h) return h + '\u65f6 ' + m + '\u5206';
    if (m) return m + '\u5206 ' + s + '\u79d2';
    return s + '\u79d2';
  }
};

window.XTERM_THEME = {
  background: '#05070d',
  foreground: '#e6edf7',
  cursor: '#6ee7ff',
  cursorAccent: '#05070d',
  selectionBackground: 'rgba(79,158,255,0.35)',
  black: '#1a2236',
  red: '#ff5c7c',
  green: '#4fd18b',
  yellow: '#ffb45c',
  blue: '#4f9eff',
  magenta: '#c792ea',
  cyan: '#6ee7ff',
  white: '#e6edf7',
  brightBlack: '#5c6b85',
  brightRed: '#ff8aa3',
  brightGreen: '#7ce8b0',
  brightYellow: '#ffd28f',
  brightBlue: '#82b8ff',
  brightMagenta: '#dcb3ff',
  brightCyan: '#a3f0ff',
  brightWhite: '#ffffff'
};

window.XTERM_OPTS = function(fontSize) {
  if (fontSize === undefined) fontSize = 14;
  var sb = 5000;
  try {
    if (window.__settings && window.__settings.scrollback) sb = window.__settings.scrollback;
  } catch (e) {}
  return {
    cursorBlink: true,
    fontSize: fontSize,
    fontFamily: '"SF Mono", Consolas, Menlo, monospace',
    theme: window.XTERM_THEME,
    scrollback: sb,
    allowProposedApi: true,
    convertEol: false
  };
};

window.SparkChart = function(canvas, color, maxPoints) {
  this.canvas = canvas;
  this.ctx = canvas.getContext('2d');
  this.color = color || '#4f9eff';
  this.points = [];
  this.maxPoints = maxPoints || 60;
  this.maxValue = 100;
  this._resize();
  var self = this;
  window.addEventListener('resize', function() { self._resize(); });
};
window.SparkChart.prototype._resize = function() {
  var dpr = window.devicePixelRatio || 1;
  var rect = this.canvas.getBoundingClientRect();
  this.canvas.width = rect.width * dpr;
  this.canvas.height = rect.height * dpr;
  this.ctx.scale(dpr, dpr);
  this.w = rect.width;
  this.h = rect.height;
};
window.SparkChart.prototype.push = function(v) {
  this.points.push(Math.max(0, v));
  if (this.points.length > this.maxPoints) this.points.shift();
  if (v > this.maxValue) this.maxValue = v;
  this.draw();
};
window.SparkChart.prototype.draw = function() {
  var ctx = this.ctx, w = this.w, h = this.h, points = this.points;
  ctx.clearRect(0, 0, w, h);
  if (points.length < 2) return;
  var step = w / (this.maxPoints - 1);
  var self = this;
  var norm = function(v) { return h - (v / self.maxValue) * (h - 4) - 2; };
  ctx.beginPath();
  ctx.moveTo(0, h);
  for (var i = 0; i < points.length; i++) ctx.lineTo(i * step, norm(points[i]));
  ctx.lineTo((points.length - 1) * step, h);
  ctx.closePath();
  var grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, this.color + '55');
  grad.addColorStop(1, this.color + '00');
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  for (var j = 0; j < points.length; j++) {
    var x = j * step, y = norm(points[j]);
    if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = this.color;
  ctx.lineWidth = 1.5;
  ctx.stroke();
};

window.__taskTerms = {};

window.initGlobalSocketHandlers = function(socket) {
  socket.on('task:output', function(data) {
    var entry = window.__taskTerms[data.task_id];
    if (entry && entry.term) {
      try { entry.term.write(data.data); } catch (e) {}
    }
  });
  socket.on('task:snapshot', function(data) {
    var entry = window.__taskTerms[data.task_id];
    if (!entry || !entry.term) return;
    var lines = data.lines || [];
    for (var i = 0; i < lines.length; i++) {
      try { entry.term.write(lines[i].data); } catch (e) {}
    }
  });
  socket.on('task:status', function(data) {
    var entry = window.__taskTerms[data.task_id];
    if (entry && entry.onStatus) {
      try { entry.onStatus(data); } catch (e) {}
    }
  });
};