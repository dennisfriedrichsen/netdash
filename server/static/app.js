/* netdash dashboard. No libraries -- plain DOM + inline SVG sparklines. */
'use strict';

var REFRESH_MS = 30000;
var root = document.getElementById('root');
var metaEl = document.getElementById('meta');
var titleEl = document.getElementById('title');
var tipEl = document.getElementById('tip');
var timer = null;

/* Status is never colour alone: every chip carries a glyph and a word too. */
var STATUS = {
  ok:       { css: 'var(--st-ok)',   glyph: '✓', label: 'OK' },
  warning:  { css: 'var(--st-warn)', glyph: '▲', label: 'Warn' },
  critical: { css: 'var(--st-crit)', glyph: '✕', label: 'Crit' },
  stale:    { css: 'var(--st-none)', glyph: '⋯', label: 'Stale' },
  unknown:  { css: 'var(--st-none)', glyph: '–', label: 'n/a' }
};
function st(k) { return STATUS[k] || STATUS.unknown; }

function fmtBytes(b) {
  if (b === null || b === undefined) return '–';
  var u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'], i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)) + ' ' + u[i];
}
function fmtPct(p) { return (p === null || p === undefined) ? '–' : p.toFixed(0) + '%'; }
function fmtAge(s) {
  if (s === null || s === undefined) return '';
  if (s < 90) return s + 's ago';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  return Math.round(s / 3600) + 'h ago';
}
function fmtUptime(s) {
  if (!s) return '';
  var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d > 0 ? ('up ' + d + 'd ' + h + 'h') : ('up ' + h + 'h');
}
function el(tag, cls, txt) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined) e.textContent = txt;
  return e;
}

/* A labelled meter: name on the left, value on the right, bar below. */
function meter(name, pct, status, subtext) {
  var m = el('div', 'meter');
  var lbl = el('div', 'lbl');
  lbl.appendChild(el('span', null, name));
  lbl.appendChild(el('span', 'val', fmtPct(pct)));
  m.appendChild(lbl);
  var track = el('div', 'track');
  var fill = el('div', 'fill');
  fill.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
  fill.style.background = st(status).css;
  track.appendChild(fill);
  m.appendChild(track);
  if (subtext) m.appendChild(el('div', 'sub', subtext));
  return m;
}


/* ---------- OS icons ----------
   Inline SVG only: the dashboard must not fetch anything off the LAN.
   Drawn in muted ink rather than brand colours -- green/amber/red are reserved
   for status here, and a red BSD mark beside a green status dot reads as an
   alert. Shape carries identity, colour stays meaningful. The icon is a
   redundant cue (the OS string is printed underneath), so it is aria-hidden. */
var NS_SVG = 'http://www.w3.org/2000/svg';

var OS_ICONS = {
  apple: [['path', { d: 'M12.9 4.2c.6-.8 1-1.8.9-2.9-.9.05-2 .6-2.6 1.4-.6.7-1.1 1.7-.9 2.7 1 .08 2-.5 2.6-1.2zM16.5 12.6c-.02-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.85-1.45-.15-2.8.85-3.5.85-.72 0-1.85-.83-3.05-.8-1.56.02-3 .9-3.8 2.3-1.62 2.8-.42 7 1.15 9.3.77 1.13 1.68 2.4 2.88 2.35 1.15-.05 1.6-.75 3-.75s1.8.75 3.05.72c1.26-.02 2.05-1.15 2.8-2.28.88-1.3 1.25-2.56 1.27-2.63-.03-.01-2.4-.93-2.4-3.7z' }]],
  ubuntu: [['circle', { cx: 12, cy: 12, r: 6.6, fill: 'none', 'stroke-width': 1.7 }],
           ['circle', { cx: 12, cy: 4.6, r: 2.1 }],
           ['circle', { cx: 5.5, cy: 15.7, r: 2.1 }],
           ['circle', { cx: 18.5, cy: 15.7, r: 2.1 }]],
  debian: [['path', { d: 'M14.6 6.9a6 6 0 1 0 3.1 8.6A7.2 7.2 0 1 1 14.6 6.9z' }],
           ['circle', { cx: 12.4, cy: 12.2, r: 3.4, fill: 'none', 'stroke-width': 1.5 }]],
  raspberry: [['circle', { cx: 8.6, cy: 14.4, r: 2.5 }],
              ['circle', { cx: 15.4, cy: 14.4, r: 2.5 }],
              ['circle', { cx: 12, cy: 17.6, r: 2.5 }],
              ['circle', { cx: 12, cy: 11.2, r: 2.5 }],
              ['path', { d: 'M9.6 7.2c.9-1.6 2.6-2.5 4.6-2.4', fill: 'none', 'stroke-width': 1.7 }]],
  bsd: [['path', { d: 'M6.3 4.6l3.3 2.6M17.7 4.6l-3.3 2.6', fill: 'none', 'stroke-width': 1.8 }],
        ['circle', { cx: 12, cy: 13.6, r: 6.2, fill: 'none', 'stroke-width': 1.8 }],
        ['circle', { cx: 9.7, cy: 12.6, r: 1.1 }],
        ['circle', { cx: 14.3, cy: 12.6, r: 1.1 }]],
  nas: [['rect', { x: 3.6, y: 4.8, width: 16.8, height: 4.4, rx: 1.3, fill: 'none', 'stroke-width': 1.6 }],
        ['rect', { x: 3.6, y: 14.8, width: 16.8, height: 4.4, rx: 1.3, fill: 'none', 'stroke-width': 1.6 }],
        ['circle', { cx: 7, cy: 7, r: 1 }],
        ['circle', { cx: 7, cy: 17, r: 1 }],
        ['path', { d: 'M12 10.2v3.6M10.3 12.4L12 14.1l1.7-1.7', fill: 'none', 'stroke-width': 1.5 }]],
  linux: [['ellipse', { cx: 12, cy: 14.8, rx: 5, ry: 6.2 }],
          ['circle', { cx: 12, cy: 6.6, r: 3.6 }],
          ['circle', { cx: 10.7, cy: 6.2, r: 0.85, fill: 'var(--surface-1)' }],
          ['circle', { cx: 13.3, cy: 6.2, r: 0.85, fill: 'var(--surface-1)' }]],
  unknown: [['circle', { cx: 12, cy: 12, r: 7.6, fill: 'none', 'stroke-width': 1.7 }],
            ['path', { d: 'M9.8 9.8a2.3 2.3 0 1 1 2.7 2.9v1.2', fill: 'none', 'stroke-width': 1.7 }],
            ['circle', { cx: 12.4, cy: 16.4, r: 1 }]]
};

function osFamily(os) {
  var s = String(os || '').toLowerCase();
  /* TrueNAS is FreeBSD underneath, so it has to be tested before bsd. */
  if (s.indexOf('truenas') > -1) return 'nas';
  if (s.indexOf('macos') > -1 || s.indexOf('mac os') > -1 || s.indexOf('darwin') > -1) return 'apple';
  if (s.indexOf('raspbian') > -1 || s.indexOf('raspberry') > -1) return 'raspberry';
  if (s.indexOf('ubuntu') > -1) return 'ubuntu';
  if (s.indexOf('debian') > -1) return 'debian';
  if (s.indexOf('bsd') > -1) return 'bsd';
  if (s.indexOf('linux') > -1 || s.indexOf('alpine') > -1) return 'linux';
  return 'unknown';
}

function osIcon(os) {
  var fam = osFamily(os);
  var svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('class', 'osicon');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  (OS_ICONS[fam] || OS_ICONS.unknown).forEach(function (spec) {
    /* Built with createElementNS rather than innerHTML: assigning innerHTML on
       an SVG element is not reliable in older iPad Safari. */
    var el2 = document.createElementNS(NS_SVG, spec[0]);
    var a = spec[1];
    for (var k in a) { if (Object.prototype.hasOwnProperty.call(a, k)) el2.setAttribute(k, a[k]); }
    if (!a.fill) el2.setAttribute('fill', 'currentColor');
    if (a['stroke-width']) {
      el2.setAttribute('stroke', 'currentColor');
      el2.setAttribute('stroke-linecap', 'round');
      el2.setAttribute('stroke-linejoin', 'round');
    }
    svg.appendChild(el2);
  });
  svg.appendChild(document.createComment(fam));
  return svg;
}

/* ---------- overview ---------- */

function renderOverview(data) {
  titleEl.textContent = 'Home network';
  var frag = document.createDocumentFragment();

  if (!data.hosts.length) {
    frag.appendChild(el('div', 'empty', 'No hosts have reported yet.'));
    root.replaceChildren(frag);
    return;
  }

  var bad = data.hosts.filter(function (h) {
    return h.status === 'critical' || h.status === 'warning' || h.status === 'stale';
  }).length;
  metaEl.textContent = data.hosts.length + ' hosts · ' +
    (bad ? bad + ' need attention' : 'all clear') + ' · ' +
    new Date(data.now * 1000).toLocaleTimeString();

  var grid = el('div', 'grid');
  data.hosts.forEach(function (h) {
    var s = st(h.status);
    var a = el('a', 'card');
    a.href = '#/host/' + encodeURIComponent(h.host);
    a.style.setProperty('--st', s.css);

    var row = el('div', 'row1');
    row.appendChild(osIcon(h.os));
    row.appendChild(el('span', 'name', h.host));
    var chip = el('span', 'chip');
    var dot = el('span', 'dot'); chip.appendChild(dot);
    chip.appendChild(el('span', 'glyph', s.glyph));
    chip.appendChild(el('span', null, s.label));
    row.appendChild(chip);
    a.appendChild(row);

    var os = [h.os || 'unknown os', fmtUptime(h.uptime_seconds)].filter(Boolean).join(' · ');
    var osEl = el('div', 'os', os);
    osEl.title = os;            /* truncated by CSS; full text on hover */
    a.appendChild(osEl);

    a.appendChild(meter('CPU', h.cpu.pct, h.cpu.status));
    a.appendChild(meter('Memory', h.mem.pct, h.mem.status,
      h.mem.total_bytes ? fmtBytes(h.mem.used_bytes) + ' of ' + fmtBytes(h.mem.total_bytes) : ''));

    var worst = null;
    h.disk.mounts.forEach(function (d) {
      if (d.pct !== null && (!worst || d.pct > worst.pct)) worst = d;
    });
    a.appendChild(meter('Disk' + (worst ? ' · ' + worst.mount : ''),
      h.disk.worst_pct, h.disk.status,
      worst ? fmtBytes(worst.used_bytes) + ' of ' + fmtBytes(worst.total_bytes) +
              (h.disk.mounts.length > 1 ? '  (+' + (h.disk.mounts.length - 1) + ' more)' : '') : ''));

    a.appendChild(el('div', 'sub', (h.stale ? 'last seen ' : 'updated ') + fmtAge(h.age_seconds) +
      (h.source === 'truenas' ? ' · via API' : '')));
    grid.appendChild(a);
  });
  frag.appendChild(grid);
  root.replaceChildren(frag);
}

/* ---------- sparkline ---------- */

var NS = 'http://www.w3.org/2000/svg';

function sparkline(points, colorVar) {
  /* points: [{ts, v}] with v possibly null. Single series -> no legend needed. */
  var W = 600, H = 62, PAD = 3;
  var svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'spark');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('preserveAspectRatio', 'none');

  var real = points.filter(function (p) { return p.v !== null && p.v !== undefined; });
  if (real.length < 2) {
    var t = document.createElementNS(NS, 'text');
    t.setAttribute('x', 0); t.setAttribute('y', H / 2);
    t.setAttribute('fill', 'var(--muted)');
    t.setAttribute('font-size', '13');
    t.textContent = 'not enough history yet';
    svg.appendChild(t);
    return svg;
  }

  var t0 = points[0].ts, t1 = points[points.length - 1].ts;
  var span = Math.max(1, t1 - t0);
  var x = function (p) { return ((p.ts - t0) / span) * W; };
  var y = function (v) { return H - PAD - (Math.max(0, Math.min(100, v)) / 100) * (H - PAD * 2); };

  /* recessive baseline only -- no gridlines competing with a 62px sparkline */
  var base = document.createElementNS(NS, 'line');
  base.setAttribute('x1', 0); base.setAttribute('x2', W);
  base.setAttribute('y1', H - PAD); base.setAttribute('y2', H - PAD);
  base.setAttribute('stroke', 'var(--baseline)'); base.setAttribute('stroke-width', '1');
  svg.appendChild(base);

  /* break the line across gaps rather than drawing through missing samples */
  var d = '', pen = false;
  points.forEach(function (p) {
    if (p.v === null || p.v === undefined) { pen = false; return; }
    d += (pen ? 'L' : 'M') + x(p).toFixed(1) + ' ' + y(p.v).toFixed(1) + ' ';
    pen = true;
  });
  var path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d.trim());
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', colorVar);
  path.setAttribute('stroke-width', '2');
  path.setAttribute('vector-effect', 'non-scaling-stroke');
  path.setAttribute('stroke-linejoin', 'round');
  path.setAttribute('stroke-linecap', 'round');
  svg.appendChild(path);

  /* hover crosshair + tooltip */
  var vline = document.createElementNS(NS, 'line');
  vline.setAttribute('stroke', 'var(--baseline)');
  vline.setAttribute('stroke-width', '1');
  vline.setAttribute('vector-effect', 'non-scaling-stroke');
  vline.setAttribute('y1', 0); vline.setAttribute('y2', H);
  vline.style.display = 'none';
  svg.appendChild(vline);

  var dotm = document.createElementNS(NS, 'circle');
  dotm.setAttribute('r', '4');
  dotm.setAttribute('fill', colorVar);
  dotm.setAttribute('stroke', 'var(--surface-1)');
  dotm.setAttribute('stroke-width', '2');
  dotm.style.display = 'none';
  svg.appendChild(dotm);

  function move(ev) {
    var r = svg.getBoundingClientRect();
    if (!r.width) return;
    var cx = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left) / r.width * W;
    var best = null, bd = Infinity;
    real.forEach(function (p) {
      var dd = Math.abs(x(p) - cx);
      if (dd < bd) { bd = dd; best = p; }
    });
    if (!best) return;
    vline.setAttribute('x1', x(best)); vline.setAttribute('x2', x(best));
    vline.style.display = '';
    dotm.setAttribute('cx', x(best)); dotm.setAttribute('cy', y(best.v));
    dotm.style.display = '';
    tipEl.textContent = best.v.toFixed(1) + '% · ' +
      new Date(best.ts * 1000).toLocaleTimeString();
    tipEl.style.display = 'block';
    var tw = tipEl.offsetWidth;
    var px = (ev.touches ? ev.touches[0].clientX : ev.clientX);
    tipEl.style.left = Math.max(6, Math.min(window.innerWidth - tw - 6, px - tw / 2)) + 'px';
    tipEl.style.top = (r.top - tipEl.offsetHeight - 8) + 'px';
  }
  function leave() {
    vline.style.display = 'none'; dotm.style.display = 'none'; tipEl.style.display = 'none';
  }
  svg.addEventListener('mousemove', move);
  svg.addEventListener('mouseleave', leave);
  svg.addEventListener('touchstart', move, { passive: true });
  svg.addEventListener('touchmove', move, { passive: true });
  svg.addEventListener('touchend', leave);
  return svg;
}

/* ---------- detail ---------- */

function renderDetail(host, data) {
  var c = data.current, s = st(c.status);
  titleEl.textContent = host;
  metaEl.textContent = (c.os || 'unknown os') + ' · ' + fmtUptime(c.uptime_seconds) +
    ' · ' + (c.stale ? 'last seen ' : 'updated ') + fmtAge(c.age_seconds);

  var frag = document.createDocumentFragment();

  var back = el('div', 'banner');
  var hi = osIcon(c.os); hi.classList.add('osicon-lg');
  back.appendChild(hi);
  var a = el('a', null, '← All hosts');
  a.href = '#/'; a.style.color = 'inherit';
  back.appendChild(a);
  var chip = el('span', 'chip');
  chip.style.setProperty('--st', s.css);
  chip.style.marginLeft = '14px';
  var dot = el('span', 'dot'); dot.style.background = s.css; chip.appendChild(dot);
  var g = el('span', 'glyph', s.glyph); g.style.color = s.css; chip.appendChild(g);
  chip.appendChild(el('span', null, s.label));
  back.appendChild(chip);
  frag.appendChild(back);

  var panels = el('div', 'panels');

  /* CPU */
  var p1 = el('div', 'panel');
  p1.appendChild(el('h2', null, 'CPU utilisation'));
  var b1 = el('div', 'big');
  b1.appendChild(document.createTextNode(fmtPct(c.cpu.pct)));
  p1.appendChild(b1);
  p1.appendChild(sparkline(data.history.map(function (h) {
    return { ts: h.ts, v: h.cpu_pct };
  }), st(c.cpu.status).css));
  panels.appendChild(p1);

  /* Memory */
  var p2 = el('div', 'panel');
  p2.appendChild(el('h2', null, 'Memory'));
  var b2 = el('div', 'big');
  b2.appendChild(document.createTextNode(fmtPct(c.mem.pct)));
  var u2 = el('span', 'unit', c.mem.total_bytes
    ? '  ' + fmtBytes(c.mem.used_bytes) + ' / ' + fmtBytes(c.mem.total_bytes) : '');
  b2.appendChild(u2);
  p2.appendChild(b2);
  p2.appendChild(sparkline(data.history.map(function (h) {
    return { ts: h.ts, v: h.mem_pct };
  }), st(c.mem.status).css));
  panels.appendChild(p2);

  /* Disk -- every real mount, worst first */
  var p3 = el('div', 'panel');
  p3.appendChild(el('h2', null, 'Disk space'));
  var b3 = el('div', 'big');
  b3.appendChild(document.createTextNode(fmtPct(c.disk.worst_pct)));
  b3.appendChild(el('span', 'unit', '  worst mount'));
  p3.appendChild(b3);
  var mounts = el('div', 'mounts');
  c.disk.mounts.slice().sort(function (a2, b4) { return (b4.pct || 0) - (a2.pct || 0); })
    .forEach(function (d) {
      mounts.appendChild(meter(d.mount, d.pct, d.status,
        fmtBytes(d.used_bytes) + ' of ' + fmtBytes(d.total_bytes)));
    });
  if (!c.disk.mounts.length) mounts.appendChild(el('div', 'sub', 'no mounts reported'));
  p3.appendChild(mounts);
  panels.appendChild(p3);

  frag.appendChild(panels);
  root.replaceChildren(frag);
}

/* ---------- routing + polling ---------- */

function currentRoute() {
  var m = (location.hash || '#/').match(/^#\/host\/(.+)$/);
  return m ? { view: 'host', host: decodeURIComponent(m[1]) } : { view: 'overview' };
}

function fail(msg) {
  root.replaceChildren(el('div', 'empty', msg));
  metaEl.textContent = 'disconnected';
}

function tick() {
  var r = currentRoute();
  var url = r.view === 'host'
    ? '/api/host/' + encodeURIComponent(r.host) + '?minutes=60'
    : '/api/overview';
  fetch(url, { cache: 'no-store' })
    .then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    })
    .then(function (data) {
      if (r.view === 'host') renderDetail(r.host, data); else renderOverview(data);
    })
    .catch(function (e) { fail('Cannot reach the netdash server (' + e.message + ').'); });
}

function restart() {
  if (timer) clearInterval(timer);
  tick();
  timer = setInterval(tick, REFRESH_MS);
}

window.addEventListener('hashchange', function () { tipEl.style.display = 'none'; restart(); });

/* theme toggle -- dark by default for the wall panel */
var themeBtn = document.getElementById('theme');
function applyTheme(t) {
  if (t) document.documentElement.setAttribute('data-theme', t);
  else document.documentElement.removeAttribute('data-theme');
  var dark = t ? t === 'dark'
    : !window.matchMedia('(prefers-color-scheme: light)').matches;
  themeBtn.textContent = dark ? 'Light' : 'Dark';
}
try { applyTheme(localStorage.getItem('netdash-theme')); } catch (e) { applyTheme(null); }
themeBtn.addEventListener('click', function () {
  var cur = document.documentElement.getAttribute('data-theme');
  var next = cur === 'dark' ? 'light' : cur === 'light' ? 'dark'
    : (window.matchMedia('(prefers-color-scheme: light)').matches ? 'dark' : 'light');
  try { localStorage.setItem('netdash-theme', next); } catch (e) {}
  applyTheme(next);
});

restart();
