/* The UPS tab, card and detail page, rendered for real against API-shaped
 * payloads. Emits JSON { "case": null | "message" }, driven by the `ups` case
 * in tests/run.sh.
 */
'use strict';
var r = require('./render.js');
var ctx = r.ctx, textOf = r.textOf, out = {};

function check(name, fn) {
  try { fn(); out[name] = null; } catch (e) { out[name] = e.message; }
}
function has(node, s) {
  var t = textOf(node);
  if (t.indexOf(s) < 0) throw new Error('missing ' + JSON.stringify(s) + ' in: ' + t.slice(0, 600));
}
function hasNot(node, s) {
  var t = textOf(node);
  if (t.indexOf(s) >= 0) throw new Error('unexpected ' + JSON.stringify(s) + ' in: ' + t.slice(0, 600));
}
function countTags(node, tag) {
  if (!node || typeof node !== 'object') return 0;
  var n = String(node.tagName).toLowerCase() === tag ? 1 : 0;
  (node.children || []).forEach(function (c) { n += countTags(c, tag); });
  return n;
}
function merge(a, b) { var o = {}, k; for (k in a) o[k] = a[k]; for (k in b) o[k] = b[k]; return o; }

var NOW = 1788436210;
var TH = { charge: { warn: 50, crit: 20 }, runtime_seconds: { warn: 600, crit: 300 },
           load: { warn: 80, crit: 95 } };
function reading(o) {
  var rd = { power: 'utility', status_raw: 'OL', ts: NOW - 12, age_seconds: 12,
    flags: [{ flag: 'OL', known: true, meaning: 'on utility power', severity: null }],
    charge_pct: 100, runtime_seconds: 1851, load_pct: 19, input_voltage: 120.0,
    output_voltage: null, input_frequency: null, battery_voltage: 27.4,
    battery_voltage_nominal: 24.0, temperature_c: null, battery_temperature_c: null,
    realpower_w: null, apparent_power_va: null, realpower_nominal_w: 810,
    power_nominal_va: null, charge_low_pct: 10, runtime_low_seconds: 120,
    model: 'Back-UPS RS 1350MS', manufacturer: 'American Power Conversion',
    serial: null, firmware: '951.e3 .D', firmware_aux: null, driver: 'usbhid-ups',
    driver_version: null, beeper: 'disabled',
    battery: { replace_flag: false, type: null, reported_date: '2001/09/25',
               reported_mfr_date: null, test_result: 'No test initiated' },
    alarm_text: null, invalid: [], alarms: [], severity: 'ok' };
  return merge(rd, o || {});
}
function ups(o) {
  return merge({ id: 'mercury-ups', name: 'Back-UPS RS 1350MS', attached_to: 'mercury',
    endpoint: 'ups@10.0.0.2:3493', enabled: true, poll_seconds: 30,
    stale_after_seconds: 90, thresholds: TH, config_error: null,
    comm: { state: 'ok', detail: 'reading normally', failures: 0, error: null,
            error_kind: null, error_code: null, last_attempt: NOW - 12,
            last_success: NOW - 12, age_seconds: 12 },
    fresh: true, reading: reading(), power: 'utility', power_since: NOW - 86400,
    status: 'ok', runtime_is_estimate: true }, o || {});
}
var BATT = ups({ status: 'warning', power: 'battery', power_since: NOW - 300,
  reading: reading({ power: 'battery', status_raw: 'OB DISCHRG', charge_pct: 88,
    runtime_seconds: 1500, input_voltage: 0,
    flags: [{ flag: 'OB', known: true, meaning: 'on battery', severity: 'warning' },
            { flag: 'DISCHRG', known: true, meaning: 'discharging', severity: null }],
    alarms: [{ code: 'OB', severity: 'warning', text: 'on battery', source: 'device' }],
    severity: 'warning' }) });
var LOST = ups({ id: 'second-ups', name: 'second', status: 'stale', power: 'unknown',
  fresh: false, power_since: null,
  comm: { state: 'lost', detail: 'connection refused by 10.0.0.9:3493', failures: 4,
          error: 'connection refused by 10.0.0.9:3493', error_kind: 'network',
          last_attempt: NOW - 5, last_success: NOW - 900, age_seconds: 900 },
  reading: reading({ ts: NOW - 900, age_seconds: 900 }) });

function overview(list, tab, hosts) {
  ctx.renderOverview({ now: NOW, thresholds: {}, collector_newest: null,
                       hosts: hosts || [], ups: list }, tab);
}
function detail(u, extra) {
  var d = { now: NOW, current: u, minutes: 1440, resolution: 'raw', retention_hours: 48,
    history: [{ ts: NOW - 120, power: 'utility', charge: 100, runtime: 1851, load: 18, input_v: 120 },
              { ts: NOW - 90, power: 'utility', charge: 100, runtime: 1840, load: 19, input_v: 120.5 },
              { ts: NOW - 60, power: 'battery', charge: 99, runtime: 1700, load: 19, input_v: 0 }],
    events: [] };
  for (var k in (extra || {})) d[k] = extra[k];
  ctx.renderUpsDetail(u.id, d, '24h');
}

check('the UPS tab draws one card per UPS, not host meters', function () {
  overview([ups(), BATT], 'ups');
  has(ctx.root, 'On utility power');
  has(ctx.root, 'On battery');
  has(ctx.root, 'Battery');
  has(ctx.root, '(UPS estimate)');
  hasNot(ctx.root, 'CPU');
  hasNot(ctx.root, 'Memory');
});

check('the UPS tab appears only when a UPS is configured', function () {
  overview([ups()], '', [{ host: 'h', status: 'ok', is_vm: false, services: [], disk: { mounts: [] },
    cpu: {}, mem: {}, reachability: {}, eol: null, patches: null }]);
  has(ctx.root, 'UPS');
  overview([], '', [{ host: 'h', status: 'ok', is_vm: false, services: [], disk: { mounts: [] },
    cpu: {}, mem: {}, reachability: {}, eol: null, patches: null }]);
  hasNot(ctx.root, 'UPS');
});

check('on battery leads the header', function () {
  overview([BATT], 'ups');
  if (ctx.metaEl.textContent.indexOf('UPS ON BATTERY') < 0) throw new Error(ctx.metaEl.textContent);
});

check('a UPS not reporting is in the header and never reads healthy', function () {
  overview([LOST], 'ups');
  if (ctx.metaEl.textContent.indexOf('1 UPS not reporting') < 0) throw new Error(ctx.metaEl.textContent);
  if (/all clear/.test(ctx.metaEl.textContent)) throw new Error(ctx.metaEl.textContent);
  has(ctx.root, 'No current reading');
  has(ctx.root, 'last reading 15m ago');
  hasNot(ctx.root, 'On utility power');
});

check('a missing reading is "not reported", not 0%', function () {
  overview([ups({ reading: reading({ load_pct: null, runtime_seconds: null }) })], 'ups');
  has(ctx.root, 'not reported');
  has(ctx.root, 'runtime not reported');
});

check('unknown status flags are shown, as themselves', function () {
  overview([ups({ reading: reading({ flags: [
    { flag: 'OL', known: true, meaning: 'on utility power', severity: null },
    { flag: 'XYZZY', known: false, meaning: 'not a status NUT documents', severity: 'unknown' }],
    alarms: [{ code: 'XYZZY', severity: 'unknown', text: 'unrecognised status flag XYZZY',
               source: 'device' }] }) })], 'ups');
  has(ctx.root, 'XYZZY');
});

check('UPS strings are text, never markup', function () {
  var evil = '<img src=x onerror=alert(1)>';
  var u = ups({ name: evil, reading: reading({ model: evil, firmware: evil, alarm_text: evil,
    alarms: [{ code: 'ups.alarm', severity: 'warning', text: evil, source: 'device' }] }),
    vars: { 'ups.model': evil } });
  overview([u], 'ups');
  has(ctx.root, evil);
  if (countTags(ctx.root, 'img')) throw new Error('an <img> was created');
  detail(u);
  has(ctx.root, evil);
  if (countTags(ctx.root, 'img')) throw new Error('an <img> was created on the detail page');
});

check('the detail page: power, communication, estimate, rated not measured', function () {
  detail(ups({ vars: { 'ups.status': 'OL', 'battery.charge': '100' } }), { events: [
    { ts: NOW - 600, kind: 'ups:power', from_state: 'utility', to_state: 'battery',
      detail: 'on battery — seen after 5 min without data; the change happened at an unknown time in that gap' },
    { ts: NOW - 300, kind: 'ups:comm', from_state: 'ok', to_state: 'lost', detail: 'refused' }] });
  has(ctx.root, 'Runtime (estimate)');
  has(ctx.root, 'Rated power');
  has(ctx.root, '810 W');
  hasNot(ctx.root, 'Output power (measured)');
  has(ctx.root, 'Communication');
  has(ctx.root, 'unknown time in that gap');
  has(ctx.root, 'All variables (2)');
  has(ctx.root, 'sends it no commands');
});

check('the device-reported battery date is labelled unverified', function () {
  detail(ups());
  has(ctx.root, '2001/09/25 — unverified');
  hasNot(ctx.root, 'replaced');
});

check('a stale detail page says every value is old', function () {
  detail(LOST);
  has(ctx.root, 'No current reading — every value below is from 15m ago');
  has(ctx.root, 'connection refused');
});

check('a UPS on battery renders its detail page', function () {
  detail(BATT, { resolution: 'hourly', history: [
    { ts: NOW - 7200, charge: 99, runtime: 1800, load: 19, input_v: 120, samples: 120, on_battery: 3 },
    { ts: NOW - 3600, charge: null, runtime: null, load: null, input_v: null, samples: 0, on_battery: 0 }] });
  has(ctx.root, 'On battery');
});

check('#/ups and #/ups/<id>/<range> are routes', function () {
  ctx.location.hash = '#/ups';
  var a = ctx.currentRoute();
  ctx.location.hash = '#/ups/mercury-ups/7d';
  var b = ctx.currentRoute();
  ctx.location.hash = '#/';
  if (a.view !== 'overview' || a.tab !== 'ups') throw new Error(JSON.stringify(a));
  if (b.view !== 'ups' || b.id !== 'mercury-ups' || b.range !== '7d') throw new Error(JSON.stringify(b));
});

console.log(JSON.stringify(out));
