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

/* Patch status is its own axis, never folded into the host status above:
   resource pressure is live and self-clearing, pending patches sit amber for a
   week. Same rule about colour though -- the word carries the meaning, and the
   four words are all different, so the palette is decoration. */
var PATCH = {
  security: { css: 'var(--st-crit)', glyph: '✕' },
  /* Installed but not running yet. Distinct from 'updates' because the work is
     done and only a reboot is outstanding, and emphatically distinct from
     'ok' -- the package database is clean while the old code still runs. */
  reboot:   { css: 'var(--st-crit)', glyph: '⭮' },
  updates:  { css: 'var(--st-warn)', glyph: '▲' },
  ok:       { css: 'var(--st-ok)',   glyph: '✓' },
  unknown:  { css: 'var(--st-none)', glyph: '–' }
};

function patchText(p) {
  if (!p) return 'not checked';
  if (p.status === 'security') return p.security + ' security';
  if (p.status === 'reboot') return 'reboot required';
  if (p.status === 'updates') {
    var n = p.other || 0;
    return n + ' update' + (n === 1 ? '' : 's');
  }
  if (p.status === 'ok') return 'up to date';
  /* Two very different unknowns, and the difference is the whole point: one
     host has never been checked, the other was checked so long ago that the
     answer is no longer worth believing. Neither is "up to date". */
  return p.checked_at ? 'check stale' : 'not checked';
}

function patchTitle(p) {
  if (!p || !p.checked_at) {
    return 'No patch check has run on this host yet — run netdash-patchcheck.';
  }
  var bits = [];
  bits.push(p.security === null || p.security === undefined
    ? 'no security classification available on this platform'
    : p.security + ' security');
  if (p.other !== null && p.other !== undefined) bits.push(p.other + ' other');
  /* Worth saying even when something else is already pending: updates that are
     installed but not running are invisible to the package database. */
  if (p.packages) bits.push(p.packages);
  if (p.reboot_required) bits.push('reboot required for installed updates');
  bits.push('checked ' + fmtAge(p.age_seconds));
  if (p.source) bits.push('via ' + p.source);
  if (p.detail) bits.push(p.detail);
  return bits.join(' · ');
}

function patchRow(p) {
  var s = PATCH[(p && p.status) || 'unknown'] || PATCH.unknown;
  var row = el('div', 'patch');
  row.style.setProperty('--st', s.css);
  row.appendChild(el('span', 'pdot'));
  row.appendChild(el('span', null, 'Patches'));
  row.appendChild(el('span', 'pglyph', s.glyph));
  row.appendChild(el('span', 'pval', patchText(p)));
  row.title = patchTitle(p);
  return row;
}

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
  /* Puffy, drawn as a plain spiky fish: at 19px a literal pufferfish turns to
     mush, so it is body + four spines + tail + eye and nothing else. */
  openbsd: [['circle', { cx: 10.8, cy: 12.6, r: 5.2, fill: 'none', 'stroke-width': 1.7 }],
            ['path', { d: 'M10.8 7.4V5.4M14.5 8.9l1.8-1.8M7.1 8.9L5.3 7.1M10.8 17.8v2', fill: 'none', 'stroke-width': 1.5 }],
            ['path', { d: 'M16.4 12.6l4.1-2.7v5.4z' }],
            ['circle', { cx: 8.7, cy: 11.5, r: 1.05 }]],
  /* NetBSD's flag. The swallowtail notch is what separates it from a plain
     rectangle at icon size. */
  netbsd: [['path', { d: 'M6.2 3.4v17.2', fill: 'none', 'stroke-width': 1.8 }],
           ['path', { d: 'M7.3 4.7h12.4l-3.1 3.5 3.1 3.5H7.3z' }]],
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
  /* Both must precede the generic 'bsd' test below, which would otherwise claim
     them and render every BSD as the FreeBSD daemon. */
  if (s.indexOf('openbsd') > -1) return 'openbsd';
  if (s.indexOf('netbsd') > -1) return 'netbsd';
  if (s.indexOf('bsd') > -1) return 'bsd';
  /* 'suse' is here because openSUSE is the one distro whose PRETTY_NAME carries
     no 'Linux' -- "openSUSE Tumbleweed", "openSUSE Leap 15.6" -- so it fell
     through to the question mark. */
  if (s.indexOf('linux') > -1 || s.indexOf('alpine') > -1 || s.indexOf('suse') > -1) return 'linux';
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

/* End-of-life is shown ONLY when it is bad news. A badge on every card reading
   "supported" is noise at fleet scale; the whole value is spotting the one box
   nobody ships fixes for any more. */
var EOL = {
  eol:      { label: 'EOL', css: 'var(--st-crit)' },
  eol_soon: { label: 'EOL soon', css: 'var(--st-warn)' }
};

function eolBadge(e) {
  if (!e || !EOL[e.status]) return null;
  var spec = EOL[e.status];
  var b = el('span', 'eolbadge', spec.label);
  b.style.color = spec.css;
  b.style.borderColor = spec.css;
  b.title = e.status === 'eol'
    ? ('This release is past end of life' + (e.eol_date ? ' (' + e.eol_date + ')' : '') +
       ' \u2014 upstream ships no further fixes' +
       (e.source === 'config' ? ', per server.json' : ', per endoflife.date') + '.')
    : ('End of life in ' + e.days_left + ' days, on ' + e.eol_date + ' (endoflife.date).');
  return b;
}

/* How a host's virtualisation reads in prose, including where the answer came
   from. A value set in server.json is not the same claim as one the machine
   made about itself, and the difference should be visible rather than buried. */
function virtLabel(h) {
  if (!h.virt) return 'virtualisation not reported';
  var base = h.virt === 'none' ? 'bare metal' : 'guest on ' + h.virt;
  return base + (h.virt_source === 'config' ? ' (set in server.json)' : '');
}

/* ---------- compact row ----------
   Built for density: forty hosts on one screen means roughly 30px each, so
   everything here is a glyph, a short number or nothing. No bars -- a meter
   track needs vertical space to read, and at this size the number IS the
   signal. Colour carries urgency, the number carries the value, and the title
   attribute carries the detail you would otherwise scroll for. */
function compactRow(h) {
  var s = st(h.status);
  var a = el('a', 'crow');
  a.href = '#/host/' + encodeURIComponent(h.host);
  a.style.setProperty('--st', s.css);

  a.appendChild(el('span', 'cdot'));
  var ic = osIcon(h.os); ic.classList.add('cicon');
  a.appendChild(ic);

  var name = el('span', 'cname', h.host);
  name.title = [h.os || 'unknown os', fmtUptime(h.uptime_seconds),
                virtLabel(h)].filter(Boolean).join(' · ');
  a.appendChild(name);

  /* Three numbers, each coloured by its own status. Tabular figures so the
     columns line up down the whole grid rather than jittering per row. */
  ['cpu', 'mem', 'disk'].forEach(function (k) {
    var m = k === 'disk' ? { pct: h.disk.worst_pct, status: h.disk.status } : h[k];
    var v = el('span', 'cnum', fmtPct(m.pct));
    v.style.color = m.status === 'ok' ? 'var(--text-2)' : st(m.status).css;
    v.title = k === 'disk' && h.disk.mounts.length
      ? 'Fullest filesystem: ' + fmtPct(m.pct)
      : k.toUpperCase() + ' ' + fmtPct(m.pct);
    a.appendChild(v);
  });

  var flags = el('span', 'cflags');
  var p = h.patches || {};
  var ps = PATCH[p.status || 'unknown'] || PATCH.unknown;
  var pg = el('span', 'cflag', ps.glyph);
  pg.style.color = p.status === 'ok' ? 'var(--muted)' : ps.css;
  pg.title = 'Patches: ' + patchText(p) + '\n' + patchTitle(p);
  flags.appendChild(pg);

  if (h.eol && (h.eol.status === 'eol' || h.eol.status === 'eol_soon')) {
    var e = el('span', 'cflag', '\u26A0');
    e.style.color = h.eol.status === 'eol' ? 'var(--st-crit)' : 'var(--st-warn)';
    e.title = h.eol.status === 'eol' ? 'Past end of life' : 'EOL ' + h.eol.eol_date;
    flags.appendChild(e);
  }
  if (h.collector_outdated) {
    var c = el('span', 'cflag', 'v');
    c.style.color = 'var(--st-warn)';
    c.title = 'Collector v' + h.collector_version + ', older than v' + '';
    flags.appendChild(c);
  }
  a.appendChild(flags);
  return a;
}

/* ---------- overview ---------- */

function renderOverview(data, tab) {
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
  var eolCount = data.hosts.filter(function (h) {
    return h.eol && h.eol.status === 'eol';
  }).length;
  var behind = data.hosts.filter(function (h) { return h.collector_outdated; }).length;
  metaEl.textContent = data.hosts.length + ' hosts · ' +
    (bad ? bad + ' need attention' : 'all clear') +
    (eolCount ? ' · ' + eolCount + ' past EOL' : '') +
    (behind ? ' · ' + behind + ' on an old collector' : '') + ' · ' +
    new Date(data.now * 1000).toLocaleTimeString();

  frag.appendChild(renderTabs(data, tab));

  var hosts = tabHosts(data.hosts, tab);
  if (!hosts.length) {
    frag.appendChild(el('div', 'empty',
      'No hosts here yet — virtualisation is only reported by collector 0.4.0 and later.'));
    root.replaceChildren(frag);
    return;
  }

  /* All hosts get the dense one-line view; the filtered tabs are small enough
     to afford the full cards, and that is where you go to actually look at a
     machine rather than scan for the one that is wrong. */
  if (tab === '') {
    var list = el('div', 'compact');
    hosts.forEach(function (h) { list.appendChild(compactRow(h)); });
    frag.appendChild(list);
    root.replaceChildren(frag);
    return;
  }

  var grid = el('div', 'grid');
  hosts.forEach(function (h) {
    var s = st(h.status);
    var a = el('a', 'card');
    a.href = '#/host/' + encodeURIComponent(h.host);
    a.style.setProperty('--st', s.css);

    var row = el('div', 'row1');
    row.appendChild(osIcon(h.os));
    row.appendChild(el('span', 'name', h.host));
    var eb = eolBadge(h.eol);
    if (eb) row.appendChild(eb);
    var chip = el('span', 'chip');
    var dot = el('span', 'dot'); chip.appendChild(dot);
    chip.appendChild(el('span', 'glyph', s.glyph));
    chip.appendChild(el('span', null, s.label));
    row.appendChild(chip);
    a.appendChild(row);

    var os = [h.os || 'unknown os', fmtUptime(h.uptime_seconds),
              h.virt && h.virt !== 'none' ? h.virt : null].filter(Boolean).join(' · ');
    var osEl = el('div', 'os', os);
    osEl.title = os + ' · ' + virtLabel(h);
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

    a.appendChild(patchRow(h.patches));

    var sub = el('div', 'sub', (h.stale ? 'last seen ' : 'updated ') + fmtAge(h.age_seconds) +
      (h.source === 'truenas' ? ' · via API' : ''));
    if (h.collector_version) {
      var v = el('span', h.collector_outdated ? 'ver old' : 'ver', ' · v' + h.collector_version);
      if (h.collector_outdated) {
        v.title = 'Older than v' + data.collector_newest +
                  ', which other hosts are running. Re-run install.sh here.';
      }
      sub.appendChild(v);
    }
    a.appendChild(sub);
    grid.appendChild(a);
  });
  frag.appendChild(grid);
  root.replaceChildren(frag);
}

/* ---------- sparkline ---------- */

var NS = 'http://www.w3.org/2000/svg';

function sparkline(points, colorVar, th) {
  /* points: [{ts, v}] with v possibly null. `th` is this host's effective
     {warn, crit} for the metric, which is what turns a floating line into a
     line with a position: 14% means nothing on its own, and everything once
     you can see it is nowhere near the 85 that would matter.

     The scale is fixed 0-100 rather than fitted to the data. Auto-scaling would
     make two percentage points of idle CPU jitter fill the panel and look
     like an event. */
  var W = 600, H = 84, PAD = 3;
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

  function rule(v, colour, dash) {
    var l = document.createElementNS(NS, 'line');
    l.setAttribute('x1', 0); l.setAttribute('x2', W);
    l.setAttribute('y1', y(v)); l.setAttribute('y2', y(v));
    l.setAttribute('stroke', colour);
    l.setAttribute('stroke-width', '1');
    l.setAttribute('vector-effect', 'non-scaling-stroke');
    if (dash) l.setAttribute('stroke-dasharray', dash);
    svg.appendChild(l);
    return l;
  }

  /* Bound the plot top and bottom so it reads as an area with a known extent
     rather than open space with a line in it. */
  rule(100, 'var(--grid)');
  rule(0, 'var(--baseline)');

  /* The thresholds, where the host has them. Dashed and drawn under the series
     so they read as guides rather than data. This is the whole point of the
     redesign: the question is never "what is the number", which is printed
     above in 34px -- it is "how much room is left". */
  if (th && th.warn != null) rule(th.warn, 'var(--st-warn)', '4 4');
  if (th && th.crit != null) rule(th.crit, 'var(--st-crit)', '4 4');

  /* break the line across gaps rather than drawing through missing samples */
  var d = '', pen = false;
  points.forEach(function (p) {
    if (p.v === null || p.v === undefined) { pen = false; return; }
    d += (pen ? 'L' : 'M') + x(p).toFixed(1) + ' ' + y(p.v).toFixed(1) + ' ';
    pen = true;
  });
  /* Fill under the line, drawn first so the stroke sits on top. Mass beneath
     the trace is what stops it floating: the eye reads the filled area against
     the 0 and 100 rules and gets the proportion without reading a number. */
  /* One closed subpath per contiguous run, matching how the stroke breaks:
     filling straight across a gap would draw an area over minutes the host
     never reported. */
  var area = '', run = [];
  function flushRun() {
    if (run.length > 1) {
      area += 'M' + x(run[0]).toFixed(1) + ' ' + y(0);
      run.forEach(function (p) { area += 'L' + x(p).toFixed(1) + ' ' + y(p.v).toFixed(1); });
      area += 'L' + x(run[run.length - 1]).toFixed(1) + ' ' + y(0) + 'Z';
    }
    run = [];
  }
  points.forEach(function (p) {
    if (p.v === null || p.v === undefined) { flushRun(); return; }
    run.push(p);
  });
  flushRun();
  if (area) {
    var fill = document.createElementNS(NS, 'path');
    fill.setAttribute('d', area);
    fill.setAttribute('fill', colorVar);
    fill.setAttribute('opacity', '0.16');
    fill.setAttribute('stroke', 'none');
    svg.appendChild(fill);
  }

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

/* Axis labels live in HTML, not in the SVG: the chart uses
   preserveAspectRatio="none" so the plot stretches to the panel width, which
   would stretch any text inside it too. */
function chartCaption(th, minutes) {
  var c = el('div', 'cap');
  c.appendChild(el('span', null, minutes + ' min ago'));
  var mid = el('span', null);
  if (th && th.warn != null) {
    mid.appendChild(el('span', 'capw', 'warn ' + th.warn + '%'));
    if (th.crit != null) {
      mid.appendChild(document.createTextNode('  '));
      mid.appendChild(el('span', 'capc', 'crit ' + th.crit + '%'));
    }
  }
  c.appendChild(mid);
  c.appendChild(el('span', null, 'now'));
  return c;
}

/* ---------- detail ---------- */

function renderDetail(host, data) {
  var c = data.current, s = st(c.status);
  titleEl.textContent = host;
  metaEl.textContent = (c.os || 'unknown os') + ' · ' + fmtUptime(c.uptime_seconds) +
    ' · ' + (c.stale ? 'last seen ' : 'updated ') + fmtAge(c.age_seconds) +
    (c.collector_version ? ' · collector v' + c.collector_version : '') +
    (c.eol && c.eol.status === 'eol' ? ' · PAST EOL'
      : c.eol && c.eol.status === 'eol_soon' ? ' · EOL ' + c.eol.eol_date : '');

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
  var th = c.thresholds || data.thresholds || {};
  p1.appendChild(sparkline(data.history.map(function (h) {
    return { ts: h.ts, v: h.cpu_pct };
  }), st(c.cpu.status).css, th.cpu));
  p1.appendChild(chartCaption(th.cpu, 60));
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
  }), st(c.mem.status).css, th.mem));
  p2.appendChild(chartCaption(th.mem, 60));
  panels.appendChild(p2);

  /* Disk -- every real mount, worst first */
  var p3 = el('div', 'panel');
  p3.appendChild(el('h2', null, 'Disk space'));
  var b3 = el('div', 'big');
  b3.appendChild(document.createTextNode(fmtPct(c.disk.worst_pct)));
  /* Name the mount rather than describing the number. "worst mount" says what
     the figure is rather than what it belongs to, and on a host with one
     filesystem it claims a superlative over a field of one. */
  var fullest = null;
  c.disk.mounts.forEach(function (d) {
    if (d.pct !== null && (!fullest || d.pct > fullest.pct)) fullest = d;
  });
  if (fullest) {
    b3.appendChild(el('span', 'unit', c.disk.mounts.length > 1
      ? '  fullest: ' + fullest.mount
      : '  ' + fullest.mount));
  }
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

  /* Patches -- no sparkline: this moves once a day, and a flat line for 23 of
     every 24 hours would say nothing a number does not. */
  var pp = c.patches || {};
  var ps = PATCH[pp.status || 'unknown'] || PATCH.unknown;
  var p4 = el('div', 'panel');
  p4.appendChild(el('h2', null, 'Patch status'));
  var b4 = el('div', 'big');
  b4.appendChild(document.createTextNode(patchText(pp)));
  b4.style.color = ps.css;
  p4.appendChild(b4);

  if (pp.checked_at) {
    if (pp.security === null || pp.security === undefined) {
      p4.appendChild(el('div', 'sub',
        'no security classification available on this platform'));
    } else if (pp.other !== null && pp.other !== undefined) {
      p4.appendChild(el('div', 'sub', pp.security + ' security · ' + pp.other + ' other'));
    }
    p4.appendChild(el('div', 'sub',
      (pp.source || 'unknown source') + ' · checked ' + fmtAge(pp.age_seconds)));
    if (pp.packages) p4.appendChild(el('div', 'sub', pp.packages));
    if (pp.reboot_required) {
      p4.appendChild(el('div', 'sub',
        'updates are installed but not running until this host reboots'));
    }
    if (pp.detail) p4.appendChild(el('div', 'sub', pp.detail));
  } else {
    p4.appendChild(el('div', 'sub',
      'No patch check has run on this host yet — run netdash-patchcheck.'));
  }
  panels.appendChild(p4);

  frag.appendChild(panels);
  root.replaceChildren(frag);
}

/* ---------- routing + polling ---------- */

var TABS = [
  { id: '',      label: 'All',        title: 'Every host, one line each' },
  { id: 'bare',  label: 'Bare metal', title: 'Hosts reporting no hypervisor' },
  { id: 'vms',   label: 'VMs',        title: 'Hosts reporting a hypervisor' }
];

function currentRoute() {
  var h = location.hash || '#/';
  var m = h.match(/^#\/host\/(.+)$/);
  if (m) return { view: 'host', host: decodeURIComponent(m[1]) };
  m = h.match(/^#\/(bare|vms)$/);
  return { view: 'overview', tab: m ? m[1] : '' };
}

/* Hosts that cannot tell whether they are virtualised appear in neither
   filtered tab. Guessing would put them in the wrong one, and a host missing
   from a list is easier to notice than one silently misfiled -- the All tab
   shows everything and says "?" for them. */
function tabHosts(hosts, tab) {
  if (tab === 'bare') return hosts.filter(function (h) { return h.is_vm === false; });
  if (tab === 'vms')  return hosts.filter(function (h) { return h.is_vm === true; });
  return hosts;
}

function renderTabs(data, active) {
  var nav = el('nav', 'tabs');
  TABS.forEach(function (t) {
    var a = el('a', 'tab' + (t.id === active ? ' on' : ''));
    a.href = '#/' + t.id;
    a.title = t.title;
    a.appendChild(el('span', null, t.label));
    var n = tabHosts(data.hosts, t.id).length;
    a.appendChild(el('span', 'tabn', String(n)));
    nav.appendChild(a);
  });
  var unknown = data.hosts.filter(function (h) { return h.is_vm === null; }).length;
  if (unknown) {
    var note = el('span', 'tabnote', unknown + ' cannot report virtualisation');
    note.title = 'These appear only under All. Re-run install.sh to collect it.';
    nav.appendChild(note);
  }
  return nav;
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
      if (r.view === 'host') renderDetail(r.host, data); else renderOverview(data, r.tab);
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
