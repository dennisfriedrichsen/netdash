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
               packages: null, acknowledged: false, acked_at: null },
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
check('detail_both_ack_rows', function () {
  ctx.renderDetail('h', detail(host({ eol: SOON, patches: {
    status: 'security', security: 3, other: 5, reboot_required: false, checked_at: 1788436000,
    age_seconds: 200, source: 'pkg audit', detail: null, packages: 'a, b',
    acknowledged: false, acked_at: null } })));
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

// -- os icons for the polled appliances --
check('appliances_get_their_own_icons', function () {
  var want = [['UniFi OS 5.1.31', 'gateway'], ['Hubitat C-8 Pro 2.5.1.174', 'hub'],
              ['TrueNAS CORE 13.0-U6.8', 'nas'], ['Debian GNU/Linux 13', 'debian'],
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

check('down_row_names_the_host', function () {
  if (textOf(ctx.compactRow(host(merge(DOWN, { host: 'hassium' })))).indexOf('hassium') < 0) {
    throw new Error('a down row must still say which host it is');
  }
});

console.log(JSON.stringify(out));
