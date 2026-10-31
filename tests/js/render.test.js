/* Render both dashboard views for real and report what threw.
 *
 * Emits JSON: { "name": null } when a case passed, { "name": "message" } when
 * it threw. Driven from tests/run.sh.
 *
 * This exists because `node --check` proves only that app.js parses, and the
 * bug that prompted it parsed perfectly: a `var ackRow` inside renderDetail
 * shadowed the top-level ackRow() across the whole function scope, so the
 * detail view of any host with an end-of-life warning died with "ackRow is not
 * a function" -- reported to the user, wrongly, as a server outage. Nothing
 * short of calling the render functions catches that.
 */
'use strict';
var r = require('./render.js');
var ctx = r.ctx, textOf = r.textOf, out = {};

/* The stub DOM has no querySelectorAll -- deliberately, it is a tree not a
   browser -- so counting elements means walking it. */
function countClass(node, cls) {
  if (!node || typeof node !== 'object') return 0;
  /* el() assigns className; sparkline() uses setAttribute. Check both. */
  var own = String(node.className || node.attrs && node.attrs['class'] || '')
              .split(' ').indexOf(cls) >= 0 ? 1 : 0;
  (node.children || []).forEach(function (c) { own += countClass(c, cls); });
  return own;
}

function countTags(node, tag) {
  if (!node || typeof node !== 'object') return 0;
  var n = node.tagName === tag ? 1 : 0;
  (node.children || []).forEach(function (c) { n += countTags(c, tag); });
  return n;
}

function host(o) {
  var h = { host: 'h', os: 'Debian GNU/Linux 13 (trixie)', source: 'push', ts: 1788436201,
    age_seconds: 5, stale: false, expect_up: true, down_reason: null,
    reachability: { state: 'up', detail: 'reporting normally', via: 'sample', failures: 0,
                    address: null, address_source: null, checked_at: 1788436201 },
    uptime_seconds: 99999, cpu: { pct: 12.5, status: 'ok' },
    mem: { pct: 40, used_bytes: 40, total_bytes: 100, status: 'ok' },
    disk: { worst_pct: 50, status: 'ok',
            mounts: [{ mount: '/', used_bytes: 50, total_bytes: 100, pct: 50, status: 'ok' }] },
    thresholds: { cpu: { warn: 80, crit: 95 }, mem: { warn: 85, crit: 95 },
                  disk: { warn: 85, crit: 95 } },
    patches: { status: 'ok', security: 0, other: 0, reboot_required: false,
               checked_at: 1788436000, age_seconds: 200, source: 'apt', detail: null,
               packages: null, entries: [], acked_count: 0, acknowledged: false,
               acked_at: null },
    eol: { status: 'supported', source: 'endoflife.date', product: 'debian', cycle: '13',
           eol_date: '2030-06-01', days_left: 900, ackable: false, acknowledged: false,
           acked_at: null },
    collector_version: '0.4.0', collector_outdated: false, virt: 'kvm',
    virt_source: 'host', is_vm: true, status: 'ok' };
  for (var k in o) h[k] = o[k];
  return h;
}
function merge(a, b) { var o = {}, k; for (k in a) o[k] = a[k]; for (k in b) o[k] = b[k]; return o; }
function check(name, fn) {
  try { fn(); out[name] = null; } catch (e) { out[name] = e.message; }
}

var SOON = { status: 'eol_soon', source: 'endoflife.date', product: 'freebsd', cycle: '15.0',
             eol_date: '2026-09-30', days_left: 27, ackable: true, acknowledged: false,
             acked_at: null };
var PAST = { status: 'eol', source: 'config', product: 'truenas', cycle: null,
             eol_date: null, days_left: null, ackable: true, acknowledged: false,
             acked_at: null };
var DOWN = { status: 'down', stale: true, age_seconds: 10877, down_reason: 'unreachable',
             reachability: { state: 'down', via: 'banner:22', failures: 2,
                             detail: 'port 22 accepted the connection but sent nothing',
                             address: '10.0.0.113', address_source: 'config',
                             checked_at: 1788436200 } };
var HIST = [{ ts: 1788436100, cpu_pct: 10, mem_used_bytes: 40, mem_total_bytes: 100, mem_pct: 40 },
            { ts: 1788436201, cpu_pct: 12, mem_used_bytes: 41, mem_total_bytes: 100, mem_pct: 41 }];
function detail(h, extra) {
  var d = { now: 1788436210, thresholds: h.thresholds, current: h, history: HIST,
            minutes: 60, resolution: 'raw', retention_hours: 48, events: [],
            disk_history: {}, disk_resolution: 'hourly' };
  for (var k in (extra || {})) d[k] = extra[k];
  return d;
}
function overview(hosts, tab) {
  ctx.renderOverview({ now: 1788436210, thresholds: host().thresholds,
                       collector_newest: '0.4.0', hosts: hosts }, tab);
}

// -- the detail view, where the shadowing bug lived --
check('detail_eol_soon', function () { ctx.renderDetail('fbsd', detail(host({ eol: SOON }))); });
check('detail_eol_past', function () { ctx.renderDetail('mercury', detail(host({ eol: PAST }))); });
check('detail_eol_acked', function () {
  ctx.renderDetail('h', detail(host({ eol: merge(SOON, { acknowledged: true, acked_at: 1788430000 }) })));
});
/* One entry of a pending security list, as patch_summary() sends it. */
function item(name, acked) {
  return { package: name, acknowledged: !!acked,
           acked_at: acked ? 1788430000 : null };
}
function SEC(entries, extra) {
  var p = { status: 'security', security: entries.length, other: 5,
            reboot_required: false, checked_at: 1788436000, age_seconds: 200,
            source: 'pkg audit', detail: null,
            packages: entries.map(function (i) { return i.package; }).join(', '),
            entries: entries,
            acked_count: entries.filter(function (i) { return i.acknowledged; }).length,
            acknowledged: false, acked_at: null };
  p.acknowledged = p.acked_count === entries.length;
  for (var k in (extra || {})) p[k] = extra[k];
  return p;
}
check('detail_both_ack_rows', function () {
  ctx.renderDetail('h', detail(host({ eol: SOON,
    patches: SEC([item('a'), item('b')]) })));
});
check('detail_down', function () { ctx.renderDetail('hassium', detail(host(DOWN))); });
check('detail_away', function () {
  ctx.renderDetail('argon', detail(host({ status: 'stale', stale: true, expect_up: false })));
});

// -- the overview, both layouts --
check('overview_compact', function () {
  overview([host({ host: 'a' }), host({ host: 'b', eol: SOON }),
            host({ host: 'c', eol: merge(SOON, { acknowledged: true }) }),
            host(merge(DOWN, { host: 'd' }))], '');
});
check('overview_cards', function () {
  overview([host({ host: 'b', eol: SOON }), host(merge(DOWN, { host: 'c' }))], 'vms');
});
check('overview_empty', function () { overview([], ''); });

// -- the All tab, grouped by OS --
check('all_tab_groups_by_os_when_asked', function () {
  var hs = [host({ host: 'arch', os: 'Arch Linux' }),
            host({ host: 'deb1' }),
            host({ host: 'fbsd', os: 'FreeBSD 15.0' }),
            host({ host: 'deb2', os: 'Debian 13' }),
            host({ host: 'mystery', os: null }),
            host({ host: 'suse', os: 'openSUSE Tumbleweed' }),
            host({ host: 'slack', os: 'Slackware 15.0 x86_64 (x86_64)' })];
  var labels = ctx.osGroups(hs).map(function (g) {
    return g.label + ':' + g.hosts.map(function (h) { return h.host; }).join('+');
  }).join(' ');
  if (labels !== 'Arch:arch Debian:deb1+deb2 FreeBSD:fbsd openSUSE:suse Slackware:slack Unknown OS:mystery') {
    throw new Error(labels);
  }
  var saved = ctx.localStorage.getItem;
  try {
    overview(hs, '');
    if (countClass(top()[1], 'cgroup')) throw new Error('grouped without being asked');
    ctx.localStorage.getItem = function (k) { return k === 'netdash-group' ? 'os' : null; };
    overview(hs, '');
    if (countClass(top()[1], 'cgroup') !== 6) throw new Error('expected 6 group headings');
    if (countClass(top()[1], 'crow') !== 7) throw new Error('a host went missing');
    if (textOf(top()[0]).indexOf('By OS') < 0) throw new Error('no toggle on the All tab');
    overview(hs, 'vms');
    if (textOf(top()[0]).indexOf('By OS') >= 0) throw new Error('toggle leaked onto VMs');
  } finally { ctx.localStorage.getItem = saved; }
});

// -- behaviour a user would notice --
check('acked_eol_soon_has_no_triangle', function () {
  if (textOf(ctx.compactRow(host({ eol: SOON }))).indexOf('⚠') < 0) {
    throw new Error('an unacknowledged eol_soon host shows no triangle at all');
  }
  if (textOf(ctx.compactRow(host({ eol: merge(SOON, { acknowledged: true }) })))
        .indexOf('⚠') >= 0) {
    throw new Error('acknowledged, and the triangle is still on the All view');
  }
});
check('acked_eol_past_has_no_triangle', function () {
  if (textOf(ctx.compactRow(host({ eol: merge(PAST, { acknowledged: true }) })))
        .indexOf('⚠') >= 0) {
    throw new Error('acknowledged past-EOL still shows the triangle');
  }
});
check('unacked_eol_past_has_a_triangle', function () {
  if (textOf(ctx.compactRow(host({ eol: PAST }))).indexOf('⚠') < 0) {
    throw new Error('past EOL must still show red on the All view');
  }
});
// -- the detail page carries the same EOL badge as the overview (issue #3) --
function findClass(node, cls) {
  if (!node || typeof node !== 'object') return null;
  if (String(node.className || '').split(' ').indexOf(cls) >= 0) return node;
  for (var i = 0; i < (node.children || []).length; i++) {
    var f = findClass(node.children[i], cls);
    if (f) return f;
  }
  return null;
}
check('detail_eol_soon_is_a_yellow_badge', function () {
  ctx.renderDetail('fbsd', detail(host({ eol: SOON })));
  var b = findClass(ctx.root, 'eolbadge');
  if (!b) throw new Error('no EOL badge on the detail page');
  if (b.textContent !== 'EOL soon') throw new Error('badge reads ' + b.textContent);
  if (b.style.color !== 'var(--st-warn)') throw new Error('badge is ' + b.style.color);
  if (b.title.indexOf('2026-09-30') < 0) throw new Error('the date is gone: ' + b.title);
  if (ctx.metaEl.textContent.indexOf('EOL') >= 0) {
    throw new Error('still muted text in the header too: ' + ctx.metaEl.textContent);
  }
});
check('detail_eol_past_is_a_red_badge', function () {
  ctx.renderDetail('mercury', detail(host({ eol: PAST })));
  var b = findClass(ctx.root, 'eolbadge');
  if (!b) throw new Error('no EOL badge on the detail page');
  if (b.textContent !== 'EOL') throw new Error('badge reads ' + b.textContent);
  if (b.style.color !== 'var(--st-crit)') throw new Error('badge is ' + b.style.color);
  if (ctx.metaEl.textContent.indexOf('EOL') >= 0) {
    throw new Error('still muted text in the header too: ' + ctx.metaEl.textContent);
  }
});
check('detail_supported_has_no_eol_badge', function () {
  ctx.renderDetail('h', detail(host()));
  if (findClass(ctx.root, 'eolbadge')) throw new Error('a supported release grew a badge');
});

// -- the adopted fleet, which the console's card and detail page both render --
var FLEET = { total: 3, online: 2, offline: 1, offline_names: ['Nano HD'], updatable: 1,
  members: [
    { name: 'Nano HD', model: 'Nano HD', state: 'OFFLINE', online: false, cpu_pct: null,
      cpu_status: 'unknown', mem_pct: null, mem_status: 'unknown', uptime_seconds: null,
      firmware: '6.7.54', firmware_updatable: false },
    { name: 'USW-16-PoE', model: 'USW 16 PoE', state: 'ONLINE', online: true, cpu_pct: 16.4,
      cpu_status: 'ok', mem_pct: 38, mem_status: 'ok', uptime_seconds: 1094445,
      firmware: '7.5.10', firmware_updatable: true },
    { name: 'U7 Pro', model: 'U7 Pro', state: 'ONLINE', online: true, cpu_pct: 3.2,
      cpu_status: 'ok', mem_pct: 49.8, mem_status: 'ok', uptime_seconds: 2417194,
      firmware: '8.7.11', firmware_updatable: false } ] };
var GW = { host: 'lanthanum', os: 'UniFi OS 5.1.31', source: 'unifi', fleet: FLEET,
  mem: { pct: 63.2, used_bytes: null, total_bytes: null, status: 'ok' },
  disk: { worst_pct: null, status: 'unknown', mounts: [] }, uptime_seconds: 667072,
  /* A gateway is bare metal, which is also what puts it on the 'bare' tab the
     card assertions below render -- on the default 'kvm' host() it would be
     filtered out and every one of them would pass against an empty grid. */
  virt: 'none', virt_source: 'host', is_vm: false, collector_version: null };

check('detail_fleet', function () { ctx.renderDetail('lanthanum', detail(host(GW))); });
check('overview_fleet_cards', function () { overview([host(GW)], 'bare'); });
check('detail_fleet_all_online', function () {
  ctx.renderDetail('lanthanum', detail(host(merge(GW, {
    fleet: merge(FLEET, { offline: 0, online: 3, offline_names: [] }) }))));
});
check('fleet_card_names_the_offline_device', function () {
  overview([host(GW)], 'bare');
  var t = textOf(ctx.root);
  if (t.indexOf('Fleet') < 0) throw new Error('the console card grew no fleet badge');
  /* One offline device out of three, so the badge has room to say which. */
  if (t.indexOf('Nano HD') < 0) {
    throw new Error('the badge counted the outage without naming it: ' + t);
  }
});
check('a_host_with_no_fleet_renders_no_fleet_row', function () {
  overview([host()], 'vms');
  if (textOf(ctx.root).indexOf('Fleet') >= 0) {
    throw new Error('a plain host grew a fleet badge');
  }
});
check('a_big_fleet_falls_back_to_a_count', function () {
  var many = { total: 9, online: 5, offline: 4, offline_names: ['a', 'b', 'c', 'd'],
               updatable: 0, members: FLEET.members };
  overview([host(merge(GW, { fleet: many }))], 'bare');
  var t = textOf(ctx.root);
  if (t.indexOf('4 of 9 offline') < 0) {
    throw new Error('four names do not fit a badge; expected a count: ' + t);
  }
});

// -- history: long ranges, projections and the event log --
var EVENTS = [
  { id: 3, ts: 1788436000, host: 'h', kind: 'status', from_state: 'down',
    to_state: 'ok', detail: null },
  { id: 2, ts: 1788425000, host: 'h', kind: 'status', from_state: 'ok',
    to_state: 'down', detail: 'no icmp reply from 10.0.0.9' }
];
var ROLLED = [{ ts: 1788350000, cpu_pct: 12, mem_pct: 44, cpu_min: 3, cpu_max: 61,
                mem_min: 40, mem_max: 48, samples: 68 },
              { ts: 1788353600, cpu_pct: 15, mem_pct: 45, cpu_min: 4, cpu_max: 70,
                mem_min: 41, mem_max: 49, samples: 69 }];
var PROJ = { slope_per_day: 1.42, days_to_full: 14, days_observed: 30 };

check('detail_hourly_range', function () {
  ctx.renderDetail('h', detail(host(), { history: ROLLED, minutes: 43200,
                                         resolution: 'hourly' }), '30d');
});
check('detail_events', function () {
  ctx.renderDetail('h', detail(host(), { events: EVENTS }), '24h');
});
check('detail_projection', function () {
  var h = host();
  h.disk = { worst_pct: 78, status: 'ok', mounts: [
    { mount: '/data', used_bytes: 78, total_bytes: 100, pct: 78, status: 'ok',
      projection: PROJ }] };
  ctx.renderDetail('h', detail(h), '30d');
});
check('an_hourly_range_says_so_in_the_caption', function () {
  ctx.renderDetail('h', detail(host(), { history: ROLLED, minutes: 43200,
                                         resolution: 'hourly' }), '30d');
  var t = textOf(ctx.root);
  if (t.indexOf('· hourly') < 0) {
    throw new Error('a rollup chart must not imply per-sample detail: ' + t);
  }
  if (t.indexOf('43200 min ago') >= 0) {
    throw new Error('30 days should not be spelled in minutes');
  }
});
check('a_projection_reaches_the_page', function () {
  var h = host();
  h.disk = { worst_pct: 78, status: 'ok', mounts: [
    { mount: '/data', used_bytes: 78, total_bytes: 100, pct: 78, status: 'ok',
      projection: PROJ }] };
  ctx.renderDetail('h', detail(h), '30d');
  var t = textOf(ctx.root);
  if (t.indexOf('full in ~14 d') < 0) throw new Error('no projection drawn: ' + t);
  if (t.indexOf('+1.42 pts/day') < 0) throw new Error('projection lost its slope: ' + t);
});
check('an_outage_that_ended_still_shows', function () {
  ctx.renderDetail('h', detail(host(), { events: EVENTS }), '24h');
  var t = textOf(ctx.root);
  if (t.indexOf('ok → down') < 0) {
    throw new Error('a recovered host lost the record of going down: ' + t);
  }
});
check('no_events_panel_when_nothing_has_happened', function () {
  ctx.renderDetail('h', detail(host()), '24h');
  if (textOf(ctx.root).indexOf('Recent changes') >= 0) {
    throw new Error('an empty event log grew a panel');
  }
});

var DISKH = { '/data': [
    { ts: 1788340000, pct: 60, used_bytes: 60, total_bytes: 100 },
    { ts: 1788343600, pct: 65, used_bytes: 65, total_bytes: 100 },
    { ts: 1788347200, pct: 78, used_bytes: 78, total_bytes: 100 }],
  '/': [
    { ts: 1788340000, pct: 20, used_bytes: 20, total_bytes: 100 },
    { ts: 1788343600, pct: 20, used_bytes: 20, total_bytes: 100 }] };
function twoMounts() {
  var h = host();
  h.disk = { worst_pct: 78, status: 'ok', mounts: [
    { mount: '/data', used_bytes: 78, total_bytes: 100, pct: 78, status: 'ok',
      projection: PROJ },
    { mount: '/', used_bytes: 20, total_bytes: 100, pct: 20, status: 'ok',
      projection: null }] };
  return h;
}

check('detail_disk_history', function () {
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH }), '24h');
});
check('every_mount_gets_its_own_full_panel', function () {
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH }), '24h');
  /* Two mounts means two disk panels plus CPU and memory: four traces, each a
     full .spark like the others, not a sliver tucked under a bar. */
  /* +1 for the os icon in the banner, which is also an <svg>. */
  var n = countTags(ctx.root, 'svg');
  if (n !== 5) throw new Error('expected 4 traces + os icon, got ' + n);
  var t = textOf(ctx.root);
  if (t.indexOf('Disk · /data') < 0 || t.indexOf('Disk · /') < 0) {
    throw new Error('each mount needs its own titled panel: ' + t);
  }

});
check('disk_traces_are_coloured_like_cpu_and_memory', function () {
  /* sparkline() puts this value straight into stroke/fill attributes, so a
     bare custom-property name draws nothing. Every trace on the page must
     carry a real colour, and a healthy mount must carry the same one CPU and
     memory do. */
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH }), '24h');
  var strokes = [];
  (function walk(n) {
    if (!n || typeof n !== 'object') return;
    var v = n.attrs && n.attrs.stroke;
    if (v && String(v).indexOf('--st') >= 0) strokes.push(v);
    (n.children || []).forEach(walk);
  })(ctx.root);
  strokes.forEach(function (v) {
    if (v.indexOf('var(') !== 0) {
      throw new Error('a trace was given a bare token, not a colour: ' + v);
    }
  });
  if (strokes.indexOf('var(--st-ok)') < 0) {
    throw new Error('no healthy trace drawn in the ok colour: ' + strokes.join(','));
  }
});
check('a_disk_panel_carries_a_caption_like_cpu_does', function () {
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH }), '24h');
  /* Three captions: cpu, memory, and one per mount. A trace with no axis
     caption is the thing that made these read as decoration before. */
  var caps = countClass(ctx.root, 'cap');
  if (caps !== 4) throw new Error('expected 4 chart captions, got ' + caps);
});
check('a_mount_with_no_history_still_gets_its_panel', function () {
  /* sparkline() draws its own "not enough history yet" placeholder, so a mount
     that has only just appeared keeps its panel rather than vanishing. */
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: {} }), '24h');
  if (textOf(ctx.root).indexOf('Disk · /data') < 0) {
    throw new Error('a mount without history lost its panel');
  }
});
check('a_host_with_no_mounts_says_so', function () {
  var h = host();
  h.disk = { worst_pct: null, status: 'unknown', mounts: [] };
  ctx.renderDetail('lanthanum', detail(h), '24h');
  if (textOf(ctx.root).indexOf('no mounts reported') < 0) {
    throw new Error('an appliance with no filesystem lost its blank panel');
  }
});
check('a_daily_disk_range_says_daily', function () {
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH, minutes: 43200,
    resolution: 'hourly', disk_resolution: 'daily' }), '30d');
  if (textOf(ctx.root).indexOf('daily') < 0) {
    throw new Error('a daily disk chart must say so');
  }
});

// -- disk resolution: what one point on a disk chart is --
function find(node, pred, acc) {
  acc = acc || [];
  if (!node || typeof node !== 'object') return acc;
  if (pred(node)) acc.push(node);
  (node.children || []).forEach(function (c) { find(c, pred, acc); });
  return acc;
}
function diskPanel(mount) {
  return find(ctx.root, function (n) {
    return n.className === 'panel' && (n.children || []).some(function (c) {
      return c.tagName === 'h2' && c.textContent === 'Disk · ' + mount;
    });
  })[0];
}
function trace(panel) {
  var svg = find(panel, function (n) { return n.tagName === 'svg'; })[0];
  var line = find(svg, function (n) {
    return n.tagName === 'path' && n.attrs.fill === 'none';
  })[0];
  var area = find(svg, function (n) {
    return n.tagName === 'path' && n.attrs.fill !== 'none';
  })[0];
  var d = line ? line.attrs.d : '';
  return { svg: svg, d: d, vertices: (d.match(/[ML]/g) || []).length,
           runs: (d.match(/M/g) || []).length,
           areas: area ? (area.attrs.d.match(/M/g) || []).length : 0,
           lone: find(svg, function (n) { return n.attrs['class'] === 'lone'; }).length };
}
function hover(svg, clientX) {
  svg.dispatch('mousemove', { clientX: clientX });
  return ctx.tipEl.textContent;
}
/* Sixty one-minute readings ending at `now`, irregular by a few seconds,
   across a clock-hour boundary: the shape freebsd16pkgbase reports. */
var NOW = 1788436210;
function rawPoints(n, skip) {
  var pts = [];
  for (var i = n - 1; i >= 0; i--) {
    var ts = NOW - i * 60 - (i * 7) % 11;
    if (skip && skip(i)) continue;
    pts.push({ ts: ts, pct: 30 + i / 100, used_bytes: 30e9 + i * 1e8, total_bytes: 100e9 });
  }
  return pts;
}
function oneMount(pct) {
  var h = host();
  h.disk = { worst_pct: pct, status: 'ok', mounts: [
    { mount: '/', used_bytes: pct, total_bytes: 100, pct: pct, status: 'ok', projection: null }] };
  return h;
}
function rawDetail(pts, extra) {
  return detail(oneMount(30), merge({ disk_history: { '/': pts }, disk_resolution: 'raw',
    disk_meta: { resolution: 'raw', bucket_seconds: null, aggregate: null, source: 'raw',
                 gap_seconds: 300, retention_hours: 48, points: pts.length } }, extra || {}));
}

check('a_1h_disk_view_plots_every_raw_reading', function () {
  var pts = rawPoints(60);
  if (Math.floor(pts[0].ts / 3600) === Math.floor(pts[59].ts / 3600)) {
    throw new Error('fixture must cross a clock hour');
  }
  ctx.renderDetail('freebsd16pkgbase', rawDetail(pts), '1h');
  var t = trace(diskPanel('/'));
  if (t.vertices !== 60 || t.runs !== 1) {
    throw new Error('expected one unbroken line through 60 readings, got ' +
                    t.vertices + ' vertices in ' + t.runs + ' runs');
  }
  var cap = textOf(diskPanel('/'));
  if (cap.indexOf('each reading') < 0) throw new Error('raw caption missing: ' + cap);
});
check('a_raw_disk_tooltip_names_the_reading_and_its_moment', function () {
  var pts = rawPoints(60);
  ctx.renderDetail('freebsd16pkgbase', rawDetail(pts), '1h');
  var t = trace(diskPanel('/'));
  var tip = hover(t.svg, 599);
  var want = new Date(pts[59].ts * 1000).toLocaleString([], { year: 'numeric', month: 'short',
    day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  if (tip.indexOf('30.0%') !== 0 || tip.indexOf('reading at ' + want) < 0 ||
      tip.indexOf('/') < 0 || /max|readings/.test(tip)) {
    throw new Error('raw tooltip should be one reading at its own second: ' + tip + ' / ' + want);
  }
  /* The leftmost reading, an hour earlier, in the other clock hour. */
  var first = hover(t.svg, 0);
  var w0 = new Date(pts[0].ts * 1000).toLocaleString([], { year: 'numeric', month: 'short',
    day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  if (first.indexOf(w0) < 0 || first.indexOf('30.6%') !== 0) {
    throw new Error('hovering the left edge should find the oldest reading: ' + first);
  }
});
check('a_reporting_gap_breaks_the_disk_line', function () {
  /* Readings 20-35 minutes ago missing: one gap wider than gap_seconds. */
  ctx.renderDetail('h', rawDetail(rawPoints(60, function (i) { return i >= 20 && i < 36; })), '1h');
  var t = trace(diskPanel('/'));
  if (t.runs !== 2 || t.areas !== 2 || t.vertices !== 44) {
    throw new Error('a 16-minute hole must split line and fill: ' + JSON.stringify(t.runs) +
                    ' runs, ' + t.areas + ' areas, ' + t.vertices + ' vertices');
  }
});
check('a_missed_reading_inside_the_gap_allowance_does_not_break_it', function () {
  ctx.renderDetail('h', rawDetail(rawPoints(60, function (i) { return i === 30 || i === 31; })), '1h');
  var t = trace(diskPanel('/'));
  if (t.runs !== 1 || t.vertices !== 58) throw new Error('two missed minutes broke the line: ' + t.runs);
});
check('a_null_disk_reading_is_a_break_not_zero', function () {
  var pts = rawPoints(10);
  pts[5] = { ts: pts[5].ts, pct: null, used_bytes: null, total_bytes: 0 };
  ctx.renderDetail('h', rawDetail(pts), '1h');
  var t = trace(diskPanel('/'));
  /* y(0) is the bottom edge, 81.0: no vertex may sit there. */
  if (/[ML][\d.]+ 81\.0/.test(t.d)) throw new Error('a null was drawn as an empty disk: ' + t.d);
  if (t.runs !== 2 || t.vertices !== 9) throw new Error('null should split the line: ' + t.d);
});
check('an_isolated_disk_reading_still_shows', function () {
  /* One reading alone between two long silences draws no line segment, so
     it needs a mark of its own. */
  ctx.renderDetail('h', rawDetail(rawPoints(60, function (i) {
    return (i > 5 && i < 30) || (i > 30 && i < 55);
  })), '1h');
  var t = trace(diskPanel('/'));
  if (t.lone !== 1) throw new Error('expected one lone dot, got ' + t.lone);
});
check('a_5min_disk_view_says_max_and_shows_its_interval', function () {
  var b0 = NOW - NOW % 300 - 300;
  var pts = [
    { ts: b0 - 300, peak_ts: b0 - 200, pct: 50, used_bytes: 50e9, total_bytes: 100e9,
      samples: 5, capacity_changed: false, coverage: 'full' },
    { ts: b0, peak_ts: b0 + 127, pct: 99, used_bytes: 99e9, total_bytes: 100e9,
      samples: 5, capacity_changed: false, coverage: 'full' },
    { ts: b0 + 300, peak_ts: b0 + 310, pct: 50, used_bytes: 50e9, total_bytes: 100e9,
      samples: 1, capacity_changed: false, coverage: 'partial' }];
  ctx.renderDetail('h', detail(oneMount(50), { minutes: 1440, disk_history: { '/': pts },
    disk_resolution: '5min', disk_meta: { resolution: '5min', bucket_seconds: 300,
    aggregate: 'max', source: 'raw', gap_seconds: 450 } }), '24h');
  var p = diskPanel('/'), t = trace(p);
  if (textOf(p).indexOf('5-min max') < 0) throw new Error('caption must say 5-min max: ' + textOf(p));
  var tip = hover(t.svg, 300);
  var hm = { hour: '2-digit', minute: '2-digit' };
  var start = new Date(b0 * 1000).toLocaleString([], { year: 'numeric', month: 'short',
    day: 'numeric', hour: '2-digit', minute: '2-digit' });
  var end = new Date((b0 + 300) * 1000).toLocaleString([], hm);
  var peak = new Date((b0 + 127) * 1000).toLocaleString([], hm);
  ['99.0%', '5-min max', start + '–' + end, 'peak at ' + peak, '5 readings'].forEach(function (w) {
    if (tip.indexOf(w) < 0) throw new Error('5-min tooltip lacks "' + w + '": ' + tip);
  });
  if (tip.indexOf('reading at') >= 0) throw new Error('a bucket posed as a reading: ' + tip);
  /* The newest bucket ends after the payload's `now`: still filling. */
  var last = hover(t.svg, 599);
  if (!/ 1 reading( |$)/.test(last) || last.indexOf('so far, still in progress') < 0) {
    throw new Error('the bucket still filling must say so: ' + last);
  }
  /* The same bucket read by a server whose clock is past its end has lost
     readings to the prune, and says that instead. */
  ctx.renderDetail('h', detail(oneMount(50), { now: NOW + 3600, minutes: 1440,
    disk_history: { '/': pts }, disk_resolution: '5min', disk_meta: { resolution: '5min',
    bucket_seconds: 300, aggregate: 'max', source: 'raw', gap_seconds: 450 } }), '24h');
  var old = hover(trace(diskPanel('/')).svg, 599);
  if (old.indexOf('partial: some readings already expired') < 0) {
    throw new Error('an ended partial bucket must say readings expired: ' + old);
  }
});
check('an_hourly_disk_tooltip_flags_a_resize', function () {
  var b0 = NOW - NOW % 3600;
  var pts = [
    { ts: b0 - 3600, peak_ts: b0 - 3000, pct: 80, used_bytes: 80e9, total_bytes: 100e9,
      samples: 60, capacity_changed: true, total_min: 100e9, total_max: 200e9, coverage: 'full' },
    { ts: b0, peak_ts: b0 + 60, pct: 40, used_bytes: 80e9, total_bytes: 200e9,
      samples: 30, capacity_changed: false, coverage: 'partial' }];
  ctx.renderDetail('h', detail(oneMount(40), { minutes: 2880, disk_history: { '/': pts },
    disk_resolution: 'hourly', disk_meta: { resolution: 'hourly', bucket_seconds: 3600,
    aggregate: 'max', source: 'raw', gap_seconds: 5400 } }), '24h');
  var p = diskPanel('/'), t = trace(p);
  if (textOf(p).indexOf('hourly max') < 0) throw new Error('caption: ' + textOf(p));
  var tip = hover(t.svg, 0);
  if (tip.indexOf('capacity changed') < 0 || tip.indexOf('80.0%') !== 0) {
    throw new Error('resize not flagged: ' + tip);
  }
});
check('a_daily_legacy_disk_point_does_not_claim_to_be_complete', function () {
  var d0 = NOW - NOW % 86400;
  var pts = [
    { ts: d0 - 3 * 86400, peak_ts: null, pct: 41, used_bytes: null, total_bytes: null,
      samples: 9, capacity_changed: null, coverage: null },
    { ts: d0 - 2 * 86400, peak_ts: null, pct: 55, used_bytes: 55e10, total_bytes: 1e12,
      samples: 1440, capacity_changed: false, coverage: 'full' }];
  ctx.renderDetail('h', detail(oneMount(55), { minutes: 10080, resolution: 'hourly',
    disk_history: { '/': pts }, disk_resolution: 'daily',
    disk_meta: { resolution: 'daily', bucket_seconds: 86400, aggregate: 'max',
                 source: 'daily_rollups', gap_seconds: 129600 } }), '7d');
  var p = diskPanel('/'), t = trace(p);
  if (textOf(p).indexOf('daily max') < 0) throw new Error('caption: ' + textOf(p));
  var legacy = hover(t.svg, 0), whole = hover(t.svg, 599);
  if (legacy.indexOf('older summary, may be incomplete') < 0 || legacy.indexOf(' / ') >= 0) {
    throw new Error('a legacy day passed as complete, or invented bytes: ' + legacy);
  }
  if (whole.indexOf('incomplete') >= 0 || whole.indexOf('1440 readings') < 0) {
    throw new Error('a full day was doubted: ' + whole);
  }
});
check('a_refresh_redraws_the_1h_view_with_the_new_reading', function () {
  /* tick() calls renderDetail again with the next payload every 30 s. The
     panel must be replaced, not stacked, and carry the new reading. */
  var pts = rawPoints(60);
  ctx.renderDetail('freebsd16pkgbase', rawDetail(pts), '1h');
  var next = pts.slice(1).concat([{ ts: NOW + 60, pct: 31, used_bytes: 31e9, total_bytes: 100e9 }]);
  ctx.renderDetail('freebsd16pkgbase', rawDetail(next), '1h');
  var panels = find(ctx.root, function (n) {
    return n.tagName === 'h2' && n.textContent === 'Disk · /';
  });
  if (panels.length !== 1) throw new Error('refresh stacked ' + panels.length + ' disk panels');
  var t = trace(diskPanel('/'));
  if (t.vertices !== 60) throw new Error('refresh lost readings: ' + t.vertices);
  if (hover(t.svg, 599).indexOf('31.0%') !== 0) throw new Error('newest reading not drawn last');
});
check('an_older_server_without_disk_meta_still_renders', function () {
  ctx.renderDetail('h', detail(twoMounts(), { disk_history: DISKH, disk_resolution: 'hourly' }), '24h');
  if (textOf(ctx.root).indexOf('hourly max') < 0) throw new Error('old payload lost its caption');
});
check('cpu_and_memory_keep_their_own_tooltip', function () {
  ctx.renderDetail('h', rawDetail(rawPoints(60)), '1h');
  var cpu = find(ctx.root, function (n) { return n.tagName === 'svg' && n.attrs['class'] === 'spark'; })[0];
  var tip = hover(cpu, 599);
  if (!/^12\.0% · /.test(tip) || tip.indexOf('reading') >= 0) throw new Error('cpu tooltip changed: ' + tip);
});

// -- os icons for the polled appliances --
check('appliances_get_their_own_icons', function () {
  var want = [['UniFi OS 5.1.31', 'gateway'], ['Hubitat C-8 Pro 2.5.1.174', 'hub'],
              ['TrueNAS CORE 13.0-U6.8', 'nas'], ['FreeCORE 13.3', 'nas'],
              ['Debian GNU/Linux 13', 'debian'],
              ['Slackware 15.0 x86_64 (x86_64)', 'linux'],
              ['FreeBSD 15.1', 'bsd'], ['', 'unknown']];
  want.forEach(function (c) {
    var got = ctx.osFamily(c[0]);
    if (got !== c[1]) {
      throw new Error(JSON.stringify(c[0]) + ' mapped to ' + got + ', wanted ' + c[1]);
    }
  });
});
check('an_appliance_icon_actually_draws_shapes', function () {
  /* osIcon() always returns an <svg>, so a missing family fails silently as an
     empty box. Count the shapes, not the element. */
  /* Linux is 9: the feet, front and pear bottom are what stop Tux reading as
     a snowman, and dropping any of them is a silent regression. */
  [['UniFi OS 5.1.31', 4], ['Hubitat C-8 Pro 2.5.1.174', 3],
   ['Alpine Linux 3.24', 9]].forEach(function (c) {
    var svg = ctx.osIcon(c[0]);
    var shapes = (svg.children || []).filter(function (x) {
      return x.tagName !== '#comment';
    });
    if (shapes.length < c[1]) {
      throw new Error(c[0] + ' drew only ' + shapes.length + ' shapes');
    }
    shapes.forEach(function (x) {
      if (!x.attrs.fill && !x.attrs.stroke) {
        throw new Error(c[0] + ' has a <' + x.tagName + '> with neither fill nor stroke');
      }
    });
  });
});

/* The ask this feature exists for: python312 is acknowledged on its own, and
   stays that way while the rest of the list comes and goes. The page has to
   offer that per package -- one button for the whole state cannot express it. */
check('every_pending_package_gets_its_own_ack_row', function () {
  ctx.renderDetail('h', detail(host({ patches: SEC(
    [item('python312-3.12.14', true), item('giflib-6.1.3'), item('curl-8.9.0')]) })));
  var t = textOf(ctx.root);
  ['python312-3.12.14', 'giflib-6.1.3', 'curl-8.9.0'].forEach(function (n) {
    if (t.indexOf(n) < 0) throw new Error(n + ' is not on the page at all');
  });
  if (countClass(ctx.root, 'ackrow') < 4) {
    throw new Error('wanted a row per package plus the bulk one, got ' +
                    countClass(ctx.root, 'ackrow'));
  }
  if (t.indexOf('un-acknowledge') < 0) {
    throw new Error('the acknowledged package offers no way back to pending');
  }
  /* Two left to review, so the bulk link saves a click and says how many. */
  if (t.indexOf('acknowledge all 2') < 0) {
    throw new Error('no "acknowledge all 2" for the two still pending');
  }
});

/* "check stale" and "checked before reboot" are both unknown, and telling them
   apart is the point: one host has been ignored for a fortnight, the other was
   patched and rebooted ten minutes ago and its old verdict is simply gone. The
   card said "reboot required" through the reboot that satisfied it. */
check('a_check_from_before_the_boot_says_so_and_drops_the_reboot_flag', function () {
  var p = { status: 'unknown', security: 6, other: 0, reboot_required: null,
            checked_at: 1788400000, age_seconds: 43200, predates_boot: true,
            source: 'apt', detail: null, packages: null, entries: [],
            acked_count: 0, unnamed_count: 0, acknowledged: false, acked_at: null };
  ctx.renderDetail('h', detail(host({ patches: p })));
  var t = textOf(ctx.root);
  if (t.indexOf('checked before reboot') < 0) {
    throw new Error('does not say why it is unknown: ' + t.slice(0, 300));
  }
  if (t.indexOf('reboot required') >= 0) {
    throw new Error('still asks for the reboot that already happened: ' + t.slice(0, 300));
  }
  if (t.indexOf('check stale') >= 0) {
    throw new Error('reads as a neglected host rather than a rebooted one');
  }
});

/* openSUSE Leap counts security patches and names none of them, so there are
   no entries at all to hang the explanation off -- and a red badge with an
   empty panel is the least useful thing the page could show. */
check('a_check_that_names_nothing_still_explains_its_red_badge', function () {
  var p = SEC([]);
  p.security = 2;
  p.unnamed_count = 2;
  p.packages = null;
  p.source = 'zypper-patches';
  p.acknowledged = false;
  ctx.renderDetail('h', detail(host({ patches: p })));
  var t = textOf(ctx.root);
  if (t.indexOf('2 further security updates were counted but not named by zypper-patches') < 0) {
    throw new Error('nothing explains the badge: ' + t.slice(0, 300));
  }
  if (countClass(ctx.root, 'ackrow') !== 0) {
    throw new Error('offers an ack with nothing named: ' +
                    countClass(ctx.root, 'ackrow') + ' rows');
  }
});

/* A check that counts more than it names -- openSUSE Leap, or a host still on
   the old capped collector -- leaves packages nobody can review. The page says
   so, and offers no way to acknowledge them. */
check('a_counted_but_unnamed_remainder_is_stated_and_not_ackable', function () {
  var p = SEC([item('a', true), item('b', true)]);
  p.security = 5;
  p.unnamed_count = 3;
  p.acknowledged = false;
  p.acked_count = 2;
  ctx.renderDetail('h', detail(host({ patches: p })));
  var t = textOf(ctx.root);
  if (t.indexOf('3 further security updates were counted but not named') < 0) {
    throw new Error('the remainder is not explained: ' + t.slice(0, 300));
  }
  /* A row for each named package plus the bulk un-acknowledge one, and
     nothing beyond that: the remainder gets a sentence, never a link. */
  if (countClass(ctx.root, 'ackrow') !== 3) {
    throw new Error('wanted 3 ack rows, got ' + countClass(ctx.root, 'ackrow'));
  }
  /* "un-acknowledge all" is fine and expected -- both named packages are
     acked. What must not appear is a bulk *acknowledge* link. */
  if (/(^|[^-])acknowledge all \d/.test(t)) {
    throw new Error('offers a bulk ack while packages are unnamed: ' + t.slice(0, 300));
  }
});

/* Every entry is a package by name, and every one of them gets its own
   acknowledge link. The page used to carry a seventh row standing in for the
   packages the capped list never named -- "3 further packages the check did
   not name" -- with an acknowledge link beside it, which asked an admin to
   sign off on something the page could not show them. */
check('every_listed_package_is_named_and_separately_ackable', function () {
  var names = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'];
  ctx.renderDetail('h', detail(host({ patches: SEC(names.map(function (n) {
    return item(n);
  })) })));
  var t = textOf(ctx.root);
  if (t.indexOf('further package') >= 0 || t.indexOf('more)') >= 0) {
    throw new Error('a group stands in for named packages: ' + t.slice(0, 200));
  }
  names.forEach(function (n) {
    if (t.indexOf(n) < 0) { throw new Error('package ' + n + ' is not shown'); }
  });
  /* A row per package, plus the bulk one -- and not a tenth for a group. */
  if (countClass(ctx.root, 'ackrow') !== names.length + 1) {
    throw new Error('wanted ' + (names.length + 1) + ' ack rows, got ' +
                    countClass(ctx.root, 'ackrow'));
  }
});

/* The check reports in its package manager's order, which is nobody's reading
   order. Both the per-package rows and the plain summary a stale check falls
   back to come out alphabetical, ignoring case. */
check('pending_packages_are_listed_alphabetically', function () {
  var want = ['apache24', 'curl-8.9.0', 'Giflib-6.1.3', 'python312-3.12.14'];
  function inOrder(t, what) {
    var at = want.map(function (n) { return t.indexOf(n); });
    for (var i = 0; i < at.length; i++) {
      if (at[i] < 0) throw new Error(what + ': ' + want[i] + ' is missing');
      if (i && at[i] < at[i - 1]) {
        throw new Error(what + ': ' + want[i] + ' comes before ' + want[i - 1]);
      }
    }
  }
  ctx.renderDetail('h', detail(host({ patches: SEC(
    [item('python312-3.12.14'), item('curl-8.9.0'), item('Giflib-6.1.3'),
     item('apache24', true)]) })));
  inOrder(textOf(ctx.root), 'ack rows');

  var p = SEC([]);
  p.status = 'unknown';
  p.security = 4;
  p.packages = 'python312-3.12.14, curl-8.9.0, Giflib-6.1.3, apache24';
  ctx.renderDetail('h', detail(host({ patches: p })));
  inOrder(textOf(ctx.root), 'summary line');
});

check('down_row_names_the_host', function () {
  if (textOf(ctx.compactRow(host(merge(DOWN, { host: 'hassium' })))).indexOf('hassium') < 0) {
    throw new Error('a down row must still say which host it is');
  }
});

/* A host that has been down longer than the retention window: still in the
   fleet, with nothing left to draw. The dashboard could not previously be in
   this state at all -- such a host left the list instead -- so every panel
   here is reading a null it used to be guaranteed a number for. */
var GONE = { host: 'ubuntu22dot04server', os: 'Ubuntu 22.04.5 LTS',
  status: 'down', stale: true, no_samples: true, age_seconds: 1468800,
  down_reason: 'unreachable', uptime_seconds: null, collector_version: null,
  virt: null, virt_source: null, is_vm: null,
  reachability: { state: 'down', via: 'icmp', failures: 2, detail: 'no reply in 2s',
                  address: '10.0.0.42', address_source: 'ingest', checked_at: 1788436200 },
  cpu: { pct: null, status: 'unknown' },
  mem: { pct: null, used_bytes: null, total_bytes: null, status: 'unknown' },
  disk: { worst_pct: null, status: 'unknown', mounts: [] },
  patches: { status: 'unknown', security: null, other: null, reboot_required: null,
             checked_at: null, age_seconds: null, source: null, detail: null,
             packages: null, entries: [], acked_count: 0, acknowledged: false,
             acked_at: null },
  eol: { status: 'unknown', source: null, product: null, cycle: null, eol_date: null,
         days_left: null, ackable: false, acknowledged: false, acked_at: null } };

check('detail_no_samples', function () {
  ctx.renderDetail('ubuntu22dot04server',
                   { now: 1788436210, thresholds: host().thresholds, current: host(GONE),
                     history: [], minutes: 1440, resolution: 'hourly', retention_hours: 24,
                     events: [], disk_history: {}, disk_resolution: 'daily' });
});
check('overview_no_samples', function () {
  overview([host({ host: 'a' }), host(GONE)], '');
  overview([host(GONE, 'bare')], 'bare');
});

/* An empty page under a red banner reads as a page that failed to load. It
   has to say that the emptiness IS the information. */
check('a_lost_host_says_its_readings_are_gone', function () {
  ctx.renderDetail('ubuntu22dot04server',
                   { now: 1788436210, thresholds: host().thresholds, current: host(GONE),
                     history: [], minutes: 1440, resolution: 'hourly', retention_hours: 24,
                     events: [], disk_history: {}, disk_resolution: 'daily' });
  var t = textOf(ctx.root);
  if (t.indexOf('No readings left') < 0) {
    throw new Error('nothing explains the empty panels: ' + t.slice(0, 200));
  }
  if (t.indexOf('17d ago') < 0) {
    throw new Error('seventeen days is not readable as hours: ' + t.slice(0, 200));
  }
});

/* Nothing removes a host on its own any more, so the page has to offer it --
   and only where the offer is honest. */
check('a_silent_host_can_be_forgotten', function () {
  ctx.renderDetail('ubuntu22dot04server',
                   { now: 1788436210, thresholds: host().thresholds, current: host(GONE),
                     history: [], minutes: 1440, resolution: 'hourly', retention_hours: 24,
                     events: [], disk_history: {}, disk_resolution: 'daily' });
  if (textOf(ctx.root).indexOf('Forget this host') < 0) {
    throw new Error('a host with no data left offers no way to remove it');
  }
});
check('a_reporting_host_is_not_offered_forgetting', function () {
  ctx.renderDetail('h', detail(host()));
  if (textOf(ctx.root).indexOf('Forget this host') >= 0) {
    throw new Error('offered to forget a host that would re-register on its next push');
  }
});

// -- containers: a third kind, on a tab of their own --
var JAIL = { virt: 'jail', is_vm: false, is_container: true, parent: 'mercury' };
check('containers_tab_holds_only_containers', function () {
  var hs = [host({ host: 'plexserver', virt: 'jail', is_vm: false, is_container: true }),
            host({ host: 'metal', virt: 'none', is_vm: false, is_container: false }),
            host({ host: 'guest' })];
  var got = ctx.tabHosts(hs, 'containers').map(function (h) { return h.host; }).join();
  if (got !== 'plexserver') throw new Error('containers tab held ' + got);
  got = ctx.tabHosts(hs, 'bare').map(function (h) { return h.host; }).join();
  if (got !== 'metal') throw new Error('bare metal tab held ' + got);
  overview(hs, 'containers');
});
check('detail_links_a_jail_to_its_host', function () {
  ctx.renderDetail('plexserver', detail(host(merge(JAIL, { host: 'plexserver' }))));
  if (textOf(ctx.root).indexOf('jail on mercury') < 0) {
    throw new Error('the detail page did not name the jail\'s host');
  }
});

check('stopped_jail_is_grey_and_named', function () {
  var stopped = host(merge(JAIL, { host: 'jellyfin', status: 'stopped', source: 'guest',
    cpu: { pct: null, status: 'unknown' }, mem: { pct: null, status: 'unknown' },
    guest: { state: 'stopped', boot: false, address: null, seen_at: 1788436200, vouched: true } }));
  overview([stopped, host({ host: 'b' })], '');
  var t = textOf(ctx.root);
  if (t.indexOf('Stopped · stopped on mercury') < 0) throw new Error('row read: ' + t);
  if (t.indexOf('DOWN') >= 0) throw new Error('a stopped jail was counted down: ' + t);
  if (ctx.st('stopped').css !== 'var(--st-none)') throw new Error('stopped is not grey');
});

// -- service checks: a second axis on the card, never the host's own dot --
function SVCV(o) {
  var s = { id: 'plex-plexserver', kind: 'plex', name: 'Plex', host: 'h', role: 'app',
            target: 'http://10.0.0.133:32400', auth: null, enabled: true,
            interval_seconds: 60, timeout_seconds: 5,
            checks: 'media server answers /identity -- not proof that libraries or playback work',
            state: 'ok', outcome: 'ok', severity: 'ok', detail: 'media server answers',
            failures: 0, latency_ms: 3, checked_at: 1788436200, since: 1788430000,
            age_seconds: 10, facts: { version: '1.41.3.9314-a0bfb8370' } };
  for (var k in o) s[k] = o[k];
  return s;
}
var BMC = SVCV({ id: 'ipmi-mercury', kind: 'ipmi', name: 'BMC', role: 'bmc',
  target: '10.0.0.219', checks: 'management controller answers (IPMI 623/udp, HTTPS); ' +
  'sensors not read -- no credentials', detail: 'BMC answers (IPMI and HTTPS)',
  facts: { ipmi20: true, reach: { ipmi: 'IPMI answers', https: 'web interface answers (HTTP 200)' } },
  hardware: { status: 'unknown', reason: 'no credentials configured -- reachability only' } });
var FAILING = SVCV({ state: 'fail', outcome: 'fail', severity: 'critical',
                     detail: 'connection refused by 10.0.0.133:32400', failures: 2 });

check('a_failing_service_is_on_its_card_and_the_host_stays_ok', function () {
  overview([host({ host: 'plexserver', services: [FAILING], services_severity: 'critical' })], 'vms');
  var t = textOf(ctx.root);
  if (t.indexOf('Plex') < 0 || t.indexOf('failing') < 0) throw new Error('card read: ' + t);
  if (t.indexOf('connection refused') < 0) throw new Error('the reason is not on hover: ' + t);
  if (t.indexOf('OK') < 0) throw new Error('the host itself should still read OK: ' + t);
  if (t.indexOf('not proof that libraries or playback work') < 0) {
    throw new Error('the card does not say what was actually checked: ' + t);
  }
});
check('the_header_counts_failing_services', function () {
  overview([host({ host: 'a', services: [FAILING], services_severity: 'critical' }),
            host({ host: 'b', services: [SVCV({ state: 'auth', outcome: 'auth',
              severity: 'warning', detail: 'token rejected' })], services_severity: 'warning' }),
            host({ host: 'c' })], '');
  var m = ctx.metaEl.textContent;
  if (m.indexOf('1 service failing') < 0) throw new Error('header read: ' + m);
  if (m.indexOf('1 service check needs attention') < 0) throw new Error('header read: ' + m);
  if (m.indexOf('all clear') >= 0) throw new Error('all clear beside a failing service: ' + m);
  overview([host({ host: 'a', services: [SVCV({})], services_severity: 'ok' })], '');
  if (ctx.metaEl.textContent.indexOf('all clear') < 0) {
    throw new Error('answering services should leave the header clear: ' + ctx.metaEl.textContent);
  }
});
check('the_compact_row_flags_the_worst_service_and_names_them', function () {
  var row = ctx.compactRow(host({ services: [SVCV({}), FAILING], services_severity: 'critical' }));
  var t = textOf(row);
  if (t.indexOf('✕') < 0 || t.indexOf('Plex: failing') < 0) throw new Error('row read: ' + t);
  var quiet = textOf(ctx.compactRow(host({ services: [SVCV({})] })));
  if (quiet.indexOf('Plex: answering') < 0) throw new Error('quiet row read: ' + quiet);
  var none = textOf(ctx.compactRow(host({})));
  if (none.indexOf('Plex') >= 0) throw new Error('a host with no checks grew a flag');
});
check('a_disabled_check_stays_off_the_card_but_on_the_detail_page', function () {
  var off = SVCV({ state: 'disabled', severity: 'unknown', detail: 'disabled in server.json' });
  var card = textOf(ctx.compactRow(host({ services: [off] })));
  if (card.indexOf('Plex') >= 0) throw new Error('a disabled check is on the row: ' + card);
  ctx.renderDetail('h', detail(host({ services: [off] })));
  var t = textOf(ctx.root);
  if (t.indexOf('Services') < 0 || t.indexOf('disabled') < 0) throw new Error('detail read: ' + t);
});
check('a_down_host_row_still_carries_its_bmc', function () {
  var row = ctx.compactRow(host(merge(DOWN, { host: 'mercury', services: [merge(BMC, {
    hardware: { status: 'ok', power: 'on', counts: {}, problems: [], max_temp_c: 41 } })] })));
  var t = textOf(row);
  if (t.indexOf('BMC answers, power on') < 0) throw new Error('row read: ' + t);
  if (t.indexOf('mercury') < 0) throw new Error('the down row lost its host name: ' + t);
});
check('the_bmc_says_it_is_separate_and_that_its_sensors_are_unknown', function () {
  ctx.renderDetail('mercury', detail(host({ host: 'mercury', services: [BMC] })));
  var t = textOf(ctx.root);
  if (t.indexOf('Management controller of mercury at 10.0.0.219') < 0) throw new Error(t);
  if (t.indexOf('Hardware sensors: unknown') < 0) throw new Error('detail read: ' + t);
  if (t.indexOf('sensors ok') >= 0) throw new Error('unknown sensors were called ok: ' + t);
});
check('a_bmc_with_a_failed_supply_is_red_and_names_it', function () {
  var bad = merge(BMC, { severity: 'critical', hardware: { status: 'critical', power: 'on',
    max_temp_c: 45, no_reading: 1,
    counts: { power: { ok: 1, warning: 0, critical: 1, unknown: 0 } },
    problems: [{ name: 'PS2 Status', severity: 'critical',
                 reading: 'Presence detected, Failure detected' }] } });
  var row = ctx.serviceRow(bad);
  if (row.style['--st'] !== 'var(--st-crit)') throw new Error('row colour ' + row.style['--st']);
  if (textOf(row).indexOf('PS2 Status critical') < 0) throw new Error(textOf(row));
  ctx.renderDetail('mercury', detail(host({ host: 'mercury', services: [bad] })));
  var t = textOf(ctx.root);
  if (t.indexOf('power supplies 1/2 ok') < 0 || t.indexOf('Failure detected') < 0) throw new Error(t);
});
check('a_refused_sensor_login_reads_as_such', function () {
  var r = ctx.serviceRow(merge(BMC, { severity: 'warning', hardware: {
    status: 'unknown', error: 'auth', reason: 'BMC refused the login' } }));
  if (textOf(r).indexOf('sensors login refused') < 0) throw new Error(textOf(r));
  if (r.style['--st'] !== 'var(--st-warn)') throw new Error('colour ' + r.style['--st']);
});
check('every_service_state_has_its_own_word', function () {
  var words = ['ok', 'fail', 'auth', 'config', 'error', 'stale', 'unknown', 'pending', 'disabled']
    .map(function (s) { return ctx.svcStyle({ state: s }).word; });
  var uniq = words.filter(function (w, i) { return words.indexOf(w) === i; });
  if (uniq.length !== words.length) throw new Error('shared words: ' + words.join(', '));
  if (ctx.svcStyle({ state: 'fail' }).css !== 'var(--st-crit)') throw new Error('fail is not red');
  ['auth', 'config', 'error', 'stale'].forEach(function (s) {
    if (ctx.svcStyle({ state: s }).css !== 'var(--st-warn)') throw new Error(s + ' is not amber');
  });
});
check('a_service_event_renders_in_the_log', function () {
  ctx.renderDetail('plexserver', detail(host({ services: [SVCV({})] }), { events: [
    { ts: 1788436000, host: 'plexserver', kind: 'service:plex-plexserver', from_state: 'ok',
      to_state: 'fail', detail: 'Plex: connection refused' }] }));
  var t = textOf(ctx.root);
  if (t.indexOf('ok → fail') < 0 || t.indexOf('Plex: connection refused') < 0) throw new Error(t);
});

// -- the Services tab: every check, one row each --
function SV(o) { return SVCV(merge({ address: '10.0.0.133', ports: ['32400/tcp'],
                                     probe: 'GET /identity' }, o || {})); }
var SVC_HOSTS = [
  host({ host: 'plexserver', services: [SV({})], services_severity: 'ok' }),
  host(merge(DOWN, { host: 'mercury', services: [merge(BMC, { address: '10.0.0.219',
    ports: ['623/udp', '443/tcp'], probe: 'IPMI ping + HTTPS' })] })),
  host({ host: 'transmission', services: [SV({ name: 'Transmission', id: 'tx',
    address: '10.0.0.152', ports: ['9091/tcp'], probe: 'RPC session-stats',
    state: 'fail', severity: 'critical', detail: 'connection refused by 10.0.0.152:9091',
    since: 1788436000 })] }),
  host({ host: 'hassium', services: [SV({ name: 'Home Assistant', id: 'ha',
    state: 'disabled', severity: 'unknown', checked_at: null, since: null })] }),
  host({ host: 'cerium' })
];
/* renderOverview hands root one fragment; the stub DOM keeps it as a node. */
function top() {
  var k = ctx.root.children;
  return k.length === 1 && k[0].tagName === '#fragment' ? k[0].children : k;
}
function svcRows() {
  overview(SVC_HOSTS, 'services');
  var table = top()[1];
  return table.children.filter(function (r) {
    return String(r.className).indexOf('srow') === 0 && String(r.className).indexOf('shead') < 0;
  });
}
check('the_services_tab_lists_every_check_worst_first', function () {
  var rows = svcRows();
  var names = rows.map(function (r) { return r.children[1].textContent; });
  if (names.join(',') !== 'Transmission,BMC,Plex,Home Assistant') throw new Error(names.join(','));
  if (String(rows[3].className).indexOf('isoff') < 0) throw new Error('disabled row not dimmed');
});
check('a_service_row_says_where_how_and_what', function () {
  var t = textOf(svcRows()[0]);
  ['Transmission', 'transmission', '10.0.0.152', '9091/tcp', 'RPC session-stats',
   'failing', 'connection refused by 10.0.0.152:9091', '4m']
    .forEach(function (w) { if (t.indexOf(w) < 0) throw new Error('missing ' + w + ': ' + t); });
  if (svcRows()[0].href !== '#/host/transmission') throw new Error('row links to ' + svcRows()[0].href);
});
check('the_bmc_row_keeps_its_host_status_apart', function () {
  var r = svcRows()[1], t = textOf(r);
  if (t.indexOf('623/udp, 443/tcp') < 0 || t.indexOf('reachable') < 0) throw new Error(t);
  if (t.indexOf('sensors unknown') < 0) throw new Error('sensor state missing: ' + t);
  if (t.indexOf('mercury: DOWN') < 0) throw new Error('host status not beside the host: ' + t);
  if (r.style['--st'] !== 'var(--st-ok)') throw new Error('a down host painted its BMC row ' + r.style['--st']);
});
check('the_services_tab_counts_checks_not_hosts', function () {
  overview(SVC_HOSTS, 'services');
  var tabs = top()[0].children.filter(function (c) { return c.tagName === 'a'; });
  var svc = tabs.filter(function (c) { return textOf(c).indexOf('Services') >= 0; })[0];
  if (!svc || textOf(svc).indexOf('4') < 0) throw new Error(svc ? textOf(svc) : 'no Services tab');
  if (svc.className.indexOf(' on') < 0) throw new Error('Services tab not marked active');
});
check('no_checks_configured_says_how_to_add_one', function () {
  overview([host({ host: 'a' })], 'services');
  if (textOf(ctx.root).indexOf('add a services block') < 0) throw new Error(textOf(ctx.root));
});
check('the_services_route_is_recognised', function () {
  ctx.location.hash = '#/services';
  var r = ctx.currentRoute();
  ctx.location.hash = '#/';
  if (r.view !== 'overview' || r.tab !== 'services') throw new Error(JSON.stringify(r));
});

// -- ZFS pool health: mercury's live answers with tank DEGRADED (ada4 FAULTED)
// and ada3's SMART test failed, as storage_summary() renders them --
var STORAGE = {"status": "critical","pools": [{"name": "tank","state": "DEGRADED","boot": false,"severity": "warning","reason": "pool DEGRADED","read_errors": 0,"write_errors": 0,"checksum_errors": 0,"data_errors": null,"bad_devices": [{"name": "ada4","state": "FAULTED","serial": "SERIAL-ADA4"}],"scan": {"function": "SCRUB","state": "FINISHED","start": 1793000000,"end": 1793000000,"errors": 0},"scrub": {"severity": "ok","reason": null,"age_days": 20.0},"detail": null,"disks": ["ada0","ada2","ada4","ada1","ada3"]},{"name": "tank001","state": "ONLINE","boot": false,"severity": "ok","reason": null,"read_errors": 0,"write_errors": 0,"checksum_errors": 0,"data_errors": null,"bad_devices": [],"scan": {"function": "SCRUB","state": "FINISHED","start": 1793000000,"end": 1793000000,"errors": 0},"scrub": {"severity": "ok","reason": null,"age_days": 34.5},"detail": null,"disks": ["ada6","ada5"]},{"name": "freenas-boot","state": "ONLINE","boot": true,"severity": "ok","reason": null,"read_errors": 0,"write_errors": 0,"checksum_errors": 0,"data_errors": null,"bad_devices": [],"scan": {"function": "SCRUB","state": "FINISHED","start": 1793000000,"end": 1793000000,"errors": 0},"scrub": {"severity": "ok","reason": null,"age_days": 5.3},"detail": "Some supported features are not enabled on the pool. The pool can still be used, but some features are unavailable.","disks": ["ada7"]}],"smart": [{"disk": "ada0","serial": "SERIAL-ADA0","model": "WDC WD40EFRX-68N32N0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": 11,"severity": "ok","note": null,"pool": "tank"},{"disk": "ada1","serial": "SERIAL-ADA1","model": "WDC WD40EFRX-68N32N0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": null,"severity": "ok","note": null,"pool": "tank"},{"disk": "ada2","serial": "SERIAL-ADA2","model": "WDC WD40EFRX-68N32N0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": null,"severity": "ok","note": null,"pool": "tank"},{"disk": "ada3","serial": "SERIAL-ADA3","model": "ST4000VN006-3CW104","status": "FAILED","status_verbose": "Completed: read failure","test": "Short offline","age_hours": null,"severity": "critical","note": null,"pool": "tank"},{"disk": "ada4","serial": "SERIAL-ADA4","model": "WDC WD40EFRX-68N32N0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": null,"severity": "ok","note": null,"pool": "tank"},{"disk": "ada5","serial": "SERIAL-ADA5","model": "WDC WDS100T1R0A-68A4W0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": null,"severity": "ok","note": null,"pool": "tank001"},{"disk": "ada6","serial": "SERIAL-ADA6","model": "WDC WDS100T1R0A-68A4W0","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": 12,"severity": "ok","note": null,"pool": "tank001"},{"disk": "ada7","serial": "SERIAL-ADA7","model": "16GB SATA Flash Drive","status": "SUCCESS","status_verbose": "Completed without error","test": "Short offline","age_hours": 11,"severity": "ok","note": null,"pool": "freenas-boot"}],"failing": ["SERIAL-ADA3"],"limits": {"scrub_max_age_days": 42,"smart_max_age_days": 35},"ts": 1788436201};
var NAS = { host: 'mercury', os: 'TrueNAS CORE 13.0 U6.8', source: 'truenas', virt: 'none',
  virt_source: 'config', is_vm: false, status: 'critical', storage: STORAGE };
check('detail_storage', function () { ctx.renderDetail('mercury', detail(host(NAS))); });
check('the_storage_panel_names_the_pool_and_the_disks_to_pull', function () {
  ctx.renderDetail('mercury', detail(host(NAS)));
  var t = textOf(ctx.root);
  ['Pool health', 'tank', 'DEGRADED', 'ada4 · SERIAL-ADA4', 'FAULTED', 'freenas-boot (boot)',
   'SERIAL-ADA3 (ada3, tank)', 'Completed: read failure', '8 disks · 1 failed',
   'Scrubs warn after 42 days']
    .forEach(function (w) { if (t.indexOf(w) < 0) throw new Error('missing ' + w + ': ' + t); });
});
check('the_card_says_which_pool_turned_the_dot', function () {
  overview([host(NAS)], 'bare');
  var t = textOf(ctx.root);
  if (t.indexOf('Pools') < 0 || t.indexOf('tank DEGRADED') < 0) throw new Error(t);
});
check('a_healthy_pool_set_reads_healthy_and_a_plain_host_has_no_row', function () {
  var ok = JSON.parse(JSON.stringify(STORAGE));
  ok.status = 'ok'; ok.failing = [];
  ok.pools.forEach(function (p) { p.severity = 'ok'; p.state = 'ONLINE'; p.bad_devices = []; });
  ok.smart = null;
  overview([host(merge(NAS, { status: 'ok', storage: ok }))], 'bare');
  if (textOf(ctx.root).indexOf('3 pools healthy') < 0) throw new Error(textOf(ctx.root));
  ctx.renderDetail('mercury', detail(host(merge(NAS, { storage: ok }))));
  if (textOf(ctx.root).indexOf('SMART') >= 0) throw new Error('SMART line with no SMART source');
  overview([host()], 'vms');
  if (textOf(ctx.root).indexOf('Pools') >= 0) throw new Error('a host without ZFS grew a Pools row');
});
check('a_storage_event_takes_the_status_palette', function () {
  ctx.renderDetail('mercury', detail(host(NAS), { events: [
    { ts: 1788436000, kind: 'storage:pool:tank', from_state: 'ok', to_state: 'warning',
      detail: 'tank DEGRADED · ada4 FAULTED (SERIAL-ADA4)' }] }));
  if (textOf(ctx.root).indexOf('ada4 FAULTED (SERIAL-ADA4)') < 0) throw new Error(textOf(ctx.root));
});

console.log(JSON.stringify(out));
