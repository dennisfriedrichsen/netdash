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
function detail(h) { return { now: 1788436210, thresholds: h.thresholds, current: h, history: HIST }; }
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
check('down_row_names_the_host', function () {
  if (textOf(ctx.compactRow(host(merge(DOWN, { host: 'hassium' })))).indexOf('hassium') < 0) {
    throw new Error('a down row must still say which host it is');
  }
});

console.log(JSON.stringify(out));
