"""UPS monitoring over NUT, against a fake upsd on 127.0.0.1. Driven by the
`ups` case in tests/run.sh, which reads the JSON this prints:

    {"case name": null}            passed
    {"case name": "what failed"}   failed

Nothing leaves this machine. The fake server records every line any client
sends it, so the last case can prove the poller never sent anything but the
allowlisted read-only query -- across every path above it, error paths too.
"""
import contextlib, importlib.util, io, json, os, socket, socketserver, sys
import tempfile, threading, time, traceback
from urllib.request import urlopen
from urllib.error import HTTPError

root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import app, db, nut  # noqa: E402

OUT = {}


def case(name):
    def wrap(fn):
        try:
            fn()
            OUT[name] = None
        except Exception as e:
            OUT[name] = "%s: %s | %s" % (type(e).__name__, e,
                                         traceback.format_exc().splitlines()[-3].strip())
        return fn
    return wrap


def eq(a, b):
    if a != b:
        raise AssertionError("%r != %r" % (a, b))


# ---------- a fake upsd ----------

RECEIVED = []          # every line any connection sent, across the whole run
_RLOCK = threading.Lock()

SAMPLE = {
    "ups.status": "OL",
    "battery.charge": "100",
    "battery.runtime": "1851",
    "ups.load": "19",
    "input.voltage": "120.0",
    "battery.voltage": "27.4",
    "battery.voltage.nominal": "24.0",
    "ups.realpower.nominal": "810",
    "ups.model": "Back-UPS RS 1350MS",
    "ups.mfr": "American Power Conversion",
    "ups.firmware": "951.e3 .D",
    "ups.test.result": "No test initiated",
    "ups.beeper.status": "disabled",
    "battery.date": "2001/09/25",
    "driver.name": "usbhid-ups",
}


def quote(v):
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeUpsd(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        socketserver.ThreadingTCPServer.__init__(self, ("127.0.0.1", 0), _Handler)
        self.mode = "vars"
        self.devices = {"ups": dict(SAMPLE)}
        self.connections = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server
        srv.connections += 1
        line = self.rfile.readline(4096)
        with _RLOCK:
            RECEIVED.append(line)
        text = line.decode("ascii", "replace").rstrip("\r\n")
        mode = srv.mode
        w = self.wfile.write
        ups = text[len("LIST VAR "):] if text.startswith("LIST VAR ") else None
        if mode == "silent":
            time.sleep(3)
            return
        if mode == "drop":
            return
        if mode == "drip":
            for ch in b"BEGIN LIST VAR ups\n":
                try:
                    w(bytes([ch])); self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.15)
            return
        if mode.startswith("ERR "):
            w((mode + "\n").encode())
            return
        if mode == "garbage":
            w(b"HELLO THERE\n")
            return
        if mode == "huge":
            w(b"BEGIN LIST VAR ups\n")
            for i in range(5000):
                try:
                    w(('VAR ups x.v%d "%s"\n' % (i, "y" * 40)).encode())
                except OSError:
                    return
            return
        if ups not in srv.devices:
            w(b"ERR UNKNOWN-UPS\n")
            return
        lines = ["BEGIN LIST VAR %s" % ups]
        lines += ["VAR %s %s %s" % (ups, k, quote(v)) for k, v in srv.devices[ups].items()]
        lines.append("END LIST VAR %s" % ups)
        if mode == "truncate":
            lines = lines[:4]
            body = "\n".join(lines) + "\n"
        elif mode == "wrongups":
            lines[2] = lines[2].replace("VAR %s " % ups, "VAR other ", 1)
            body = "\n".join(lines) + "\n"
        elif mode == "unterminated":
            lines[2] = 'VAR %s battery.charge "100' % ups
            body = "\n".join(lines) + "\n"
        elif mode == "trailing":
            lines[2] = 'VAR %s battery.charge "100" junk' % ups
            body = "\n".join(lines) + "\n"
        elif mode == "dupe":
            lines.insert(2, lines[1])
            body = "\n".join(lines) + "\n"
        elif mode == "errmid":
            lines = lines[:3] + ["ERR DATA-STALE"]
            body = "\n".join(lines) + "\n"
        elif mode == "nolf":
            body = "\n".join(lines)          # END line without its newline
        elif mode == "crlf":
            body = "\r\n".join(lines) + "\r\n"
        else:
            body = "\n".join(lines) + "\n"
        try:
            w(body.encode("utf-8"))
        except OSError:
            pass


S = FakeUpsd()
S2 = FakeUpsd()


def q(mode="vars", ups="ups", timeout=2.0, srv=S, devices=None):
    srv.mode = mode
    if devices is not None:
        srv.devices = devices
    try:
        return nut.query("127.0.0.1", srv.port, ups, timeout)
    finally:
        srv.mode = "vars"
        srv.devices = {"ups": dict(SAMPLE)}


def qerr(mode, **kw):
    try:
        q(mode, **kw)
    except nut.NutError as e:
        return e
    raise AssertionError("no error for mode %s" % mode)


def with_status(status, **extra):
    v = dict(SAMPLE)
    if status is None:
        v.pop("ups.status")
    else:
        v["ups.status"] = status
    v.update(extra)
    return v


def reading(status, **extra):
    return nut.interpret(q(devices={"ups": with_status(status, **extra)}))


# ---------- protocol ----------

@case("healthy online: utility power, charge, runtime, load, rated not measured watts")
def _():
    v = q()
    r = nut.interpret(v)
    eq(r["power"], "utility")
    eq((r["charge_pct"], r["runtime_seconds"], r["load_pct"]), (100.0, 1851, 19.0))
    eq(r["severity"], "ok")
    eq(r["realpower_w"], None)
    eq(r["realpower_nominal_w"], 810.0)
    eq(r["output_voltage"], None)
    eq(r["temperature_c"], None)
    eq([f["flag"] for f in r["flags"]], ["OL"])
    eq(r["alarms"], [])


@case("quoted values are parsed, not split: spaces and escapes survive")
def _():
    v = q(devices={"ups": dict(SAMPLE, **{"ups.firmware": "951.e3 .D",
                                          "ups.alarm": 'say "hi" \\ there'})})
    eq(v["ups.firmware"], "951.e3 .D")
    eq(v["ups.alarm"], 'say "hi" \\ there')
    eq(nut.parse_var_line('VAR ups a.b "x \\"y\\" \\\\z"', "ups"), ("a.b", 'x "y" \\z'))


@case("CRLF line endings are accepted")
def _():
    eq(q("crlf")["ups.status"], "OL")


@case("the implausible battery.date is passed through only as device-reported")
def _():
    r = nut.interpret(q())
    eq(r["battery"]["reported_date"], "2001/09/25")
    assert "replacement" not in json.dumps(r["battery"]).lower()


@case("on battery reads battery, amber")
def _():
    r = reading("OB DISCHRG", **{"battery.charge": "92", "battery.runtime": "1500"})
    eq(r["power"], "battery")
    eq(r["severity"], "warning")
    assert "OB" in [a["code"] for a in r["alarms"]]


@case("low battery on battery is red")
def _():
    r = reading("OB LB", **{"battery.charge": "9", "battery.runtime": "120"})
    eq((r["power"], r["severity"]), ("battery", "critical"))
    eq({a["code"] for a in r["alarms"] if a["source"] == "device"}, {"OB", "LB"})


@case("replace battery on utility is amber and sets the health flag")
def _():
    r = reading("OL RB")
    eq((r["power"], r["severity"]), ("utility", "warning"))
    assert r["battery"]["replace_flag"]


@case("overload is red even on utility power")
def _():
    r = reading("OL OVER", **{"ups.load": "104"})
    eq((r["power"], r["severity"]), ("utility", "critical"))
    eq(r["load_pct"], 104.0)


@case("combined flags are all kept, worst wins")
def _():
    r = reading("OB LB RB OVER DISCHRG")
    eq([f["flag"] for f in r["flags"]], ["OB", "LB", "RB", "OVER", "DISCHRG"])
    eq(r["power"], "battery")
    eq(r["severity"], "critical")
    eq({a["code"] for a in r["alarms"] if a["source"] == "device"}, {"OB", "LB", "RB", "OVER"})


@case("BYPASS, OFF and FSD: what feeds the load, and how bad")
def _():
    eq((reading("OL BYPASS")["power"], reading("OL BYPASS")["severity"]), ("bypass", "warning"))
    eq((reading("OFF")["power"], reading("OFF")["severity"]), ("off", "critical"))
    r = reading("OB LB FSD")
    eq((r["power"], r["severity"]), ("battery", "critical"))
    assert "FSD" in [a["code"] for a in r["alarms"]]


@case("an unknown flag is kept and shown, never dropped")
def _():
    r = reading("OL XYZZY CHRG")
    eq([f["flag"] for f in r["flags"]], ["OL", "XYZZY", "CHRG"])
    x = [f for f in r["flags"] if f["flag"] == "XYZZY"][0]
    eq(x["known"], False)
    assert any(a["code"] == "XYZZY" and a["severity"] == "unknown" for a in r["alarms"])
    eq(r["power"], "utility")


@case("missing fields are None, never zero")
def _():
    r = nut.interpret(q(devices={"ups": {"ups.status": "OL"}}))
    for k in ("charge_pct", "runtime_seconds", "load_pct", "input_voltage",
              "output_voltage", "temperature_c", "realpower_w"):
        eq(r[k], None)
    eq(r["model"], None)
    eq(r["severity"], "ok")


@case("unsupported or implausible values are ignored and named, not clamped")
def _():
    r = reading("OL", **{"battery.charge": "n/a", "ups.load": "999", "battery.runtime": "nan"})
    eq((r["charge_pct"], r["load_pct"], r["runtime_seconds"]), (None, None, None))
    eq(len(r["invalid"]), 3)


@case("no ups.status means power unknown, amber, never utility")
def _():
    r = reading(None)
    eq(r["power"], "unknown")
    eq(r["severity"], "warning")
    r = reading("")
    eq(r["power"], "unknown")


@case("netdash thresholds colour charge, runtime and load, marked as ours")
def _():
    r = reading("OL CHRG", **{"battery.charge": "35", "battery.runtime": "250"})
    th = {a["code"]: a for a in r["alarms"] if a["source"] == "threshold"}
    eq(th["charge"]["severity"], "warning")
    eq(th["runtime_seconds"]["severity"], "critical")


@case("timeout: a silent upsd fails within the deadline")
def _():
    t0 = time.monotonic()
    e = qerr("silent", timeout=0.6)
    eq(e.kind, nut.NETWORK)
    assert "within" in e.detail, e.detail
    assert time.monotonic() - t0 < 2.0


@case("timeout is one deadline for the exchange, not per read")
def _():
    t0 = time.monotonic()
    e = qerr("drip", timeout=0.8)
    eq(e.kind, nut.NETWORK)
    assert time.monotonic() - t0 < 2.0, time.monotonic() - t0


@case("refused connection")
def _():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    try:
        nut.query("127.0.0.1", port, "ups", 1)
        raise AssertionError("no error")
    except nut.NutError as e:
        eq(e.kind, nut.NETWORK)
        assert "refused" in e.detail, e.detail


@case("disconnect before answering")
def _():
    e = qerr("drop")
    eq(e.kind, nut.NETWORK)


@case("truncated reply is rejected whole")
def _():
    e = qerr("truncate")
    eq(e.kind, nut.PROTOCOL)
    assert "cut off" in e.detail, e.detail
    e = qerr("nolf")
    eq(e.kind, nut.PROTOCOL)


@case("malformed replies are rejected: garbage, wrong UPS, bad quoting, duplicates")
def _():
    for mode in ("garbage", "wrongups", "unterminated", "trailing", "dupe"):
        e = qerr(mode)
        eq((mode, e.kind), (mode, nut.PROTOCOL))


@case("oversized reply is cut off at the byte cap")
def _():
    e = qerr("huge")
    eq(e.kind, nut.PROTOCOL)


@case("NUT ERR replies are told apart: driver, config, mid-list")
def _():
    eq(qerr("ERR DATA-STALE").kind, nut.DRIVER)
    eq(qerr("ERR DRIVER-NOT-CONNECTED").kind, nut.DRIVER)
    e = qerr("ERR ACCESS-DENIED")
    eq((e.kind, e.code), (nut.CONFIG, "ACCESS-DENIED"))
    eq(qerr("vars", ups="nosuch").code, "UNKNOWN-UPS")
    eq(qerr("errmid").kind, nut.DRIVER)


# ---------- configuration ----------

@case("config: defaults, credentials refused, bad port and duplicates caught")
def _():
    ds = nut.parse_config({"poll_seconds": 30, "devices": [
        {"id": "a", "host": "10.0.0.2", "enabled": True},
        {"id": "b", "host": "10.0.0.2", "password": "x", "enabled": True},
        {"id": "c", "host": "10.0.0.2", "port": 99999, "enabled": True},
        {"id": "a", "host": "10.0.0.3", "enabled": True},
        {"id": "e", "host": "10.0.0.2", "ups": "ups\nFSD ups", "enabled": True},
    ]})
    eq((ds[0]["port"], ds[0]["ups"], ds[0]["config_error"]), (3493, "ups", None))
    assert "credential" in ds[1]["config_error"]
    assert "port" in ds[2]["config_error"]
    assert "duplicate" in ds[3]["config_error"]
    assert ds[4]["config_error"]
    eq(nut.parse_config(None), [])


@case("the example config parses, with the working endpoint disabled")
def _():
    cfg = json.load(open(os.path.join(root, "server", "config.example.json")))
    ds = nut.parse_config(cfg["ups"])
    eq([(d["host"], d["port"], d["ups"], d["enabled"], d["config_error"]) for d in ds],
       [("10.0.0.2", 3493, "ups", False, None)])


# ---------- the server: state, history, events ----------

TMP = tempfile.mkdtemp()
cfg = json.load(open(os.path.join(root, "server", "config.example.json")))
cfg.update(db_path=os.path.join(TMP, "t.db"), bind_host="127.0.0.1", bind_port=0)
cfg["eol"] = {"enabled": False}
cfg["reachability"] = {"enabled": False}
for k in ("truenas", "hubitat", "unifi", "services"):
    cfg.pop(k, None)
cfg["ups"] = {"poll_seconds": 30, "timeout_seconds": 1, "failures_before_lost": 2,
              "devices": [
                  {"id": "mercury-ups", "name": "Back-UPS RS 1350MS", "host": "127.0.0.1",
                   "port": S.port, "ups": "ups", "attached_to": "mercury", "enabled": True},
                  {"id": "second-ups", "host": "127.0.0.1", "port": S2.port,
                   "ups": "ups", "enabled": True},
                  {"id": "spare", "host": "127.0.0.1", "port": S.port, "enabled": False},
              ]}
cpath = os.path.join(TMP, "server.json")
json.dump(cfg, open(cpath, "w"))
app.CFG = app.load_config(cpath)
app.CONN = db.connect(app.CFG["db_path"])
DEV = {d["id"]: d for d in app.ups_devices()}
T0 = int(time.time()) - 3600


def poll(dev_id, t, mode="vars", status=None, srv=None, recorded=None, **extra):
    srv = srv or (S if dev_id == "mercury-ups" else S2)
    srv.mode = mode
    srv.devices = {"ups": with_status(status or "OL", **extra)}
    try:
        return app.ups_round(DEV[dev_id], RECORDED.setdefault(dev_id, {})
                             if recorded is None else recorded, now=t)
    finally:
        srv.mode = "vars"
        srv.devices = {"ups": dict(SAMPLE)}


def view(dev_id, t):
    return [v for v in app.ups_views(t) if v["id"] == dev_id][0]


RECORDED = {}
EVENTS = []


@case("first healthy reading: fresh, ok, and no event (utility power is not news)")
def _():
    ev = poll("mercury-ups", T0)
    eq(ev, [])
    v = view("mercury-ups", T0 + 1)
    eq((v["status"], v["fresh"], v["power"], v["comm"]["state"]), ("ok", True, "utility", "ok"))
    eq(v["runtime_is_estimate"], True)


@case("one failed poll is held: still fresh, comm failing, no event")
def _():
    ev = poll("mercury-ups", T0 + 30, mode="drop")
    eq(ev, [])
    v = view("mercury-ups", T0 + 31)
    eq((v["comm"]["state"], v["fresh"], v["status"]), ("failing", True, "ok"))


@case("a second failure is lost contact: stale, power unknown, last reading kept with its age")
def _():
    ev = poll("mercury-ups", T0 + 60, mode="drop")
    # "ok" was noted silently on the first reading, so this is a transition.
    eq([e[:3] for e in ev], [("ups:comm", "ok", "lost")])
    v = view("mercury-ups", T0 + 61)
    eq((v["comm"]["state"], v["fresh"], v["status"], v["power"]),
       ("lost", False, "stale", "unknown"))
    eq(v["reading"]["power"], "utility")
    eq(v["reading"]["age_seconds"], 61)
    eq(v["reading"]["charge_pct"], 100.0)


@case("lost contact is logged once, not every poll")
def _():
    eq(poll("mercury-ups", T0 + 90, mode="drop"), [])
    eq(poll("mercury-ups", T0 + 120, mode="ERR DATA-STALE"), [])
    v = view("mercury-ups", T0 + 121)
    eq(v["comm"]["error_kind"], nut.DRIVER)


@case("recovery after a gap: comm back, and the outage is not dated by the event")
def _():
    ev = poll("mercury-ups", T0 + 600, status="OB DISCHRG",
              **{"battery.charge": "80", "battery.runtime": "1200"})
    kinds = {e[0]: e for e in ev}
    eq(set(kinds), {"ups:comm", "ups:power", "ups:alarm:OB"})
    eq(kinds["ups:power"][1:3], ("utility", "battery"))
    assert "unknown time" in kinds["ups:power"][3], kinds["ups:power"][3]
    assert "estimate" in kinds["ups:power"][3]
    v = view("mercury-ups", T0 + 601)
    eq((v["fresh"], v["power"], v["status"]), (True, "battery", "warning"))


@case("on battery for many polls: one power event, not one per poll")
def _():
    n = 0
    for i in range(1, 21):
        n += len(poll("mercury-ups", T0 + 600 + 30 * i, status="OB DISCHRG",
                      **{"battery.charge": str(80 - i)}))
    eq(n, 0)


@case("low battery adds its own alarm event; back on utility clears both")
def _():
    ev = poll("mercury-ups", T0 + 1260, status="OB LB", **{"battery.charge": "10"})
    eq([e[:3] for e in ev], [("ups:alarm:LB", None, "critical")])
    ev = poll("mercury-ups", T0 + 1290, status="OL CHRG", **{"battery.charge": "11"})
    got = sorted(e[:3] for e in ev)
    eq(got, [("ups:alarm:LB", "critical", "ok"), ("ups:alarm:OB", "warning", "ok"),
             ("ups:power", "battery", "utility")])
    power = [e for e in ev if e[0] == "ups:power"][0]
    assert "unknown time" not in power[3], power[3]


@case("failed polls store nothing: history has gaps, never zero rows")
def _():
    rows = db.ups_history(app.CONN, "mercury-ups", T0 - 1)
    eq(len(rows), 1 + 1 + 20 + 2)  # first, recovery, 20 on battery, LB, OL
    assert all(r["charge"] is not None and r["charge"] > 0 for r in rows)
    eq([r["ts"] for r in rows][:2], [T0, T0 + 600])


@case("a wedged poller reads stale at read time, not as its last answer")
def _():
    d = DEV["mercury-ups"]
    poll("mercury-ups", T0 + 1320, status="OL CHRG", **{"battery.charge": "60"})
    v = view("mercury-ups", T0 + 1320 + d["stale_after_seconds"] + 5)
    eq((v["comm"]["state"], v["fresh"], v["status"], v["power"]),
       ("stale", False, "stale", "unknown"))


@case("restart: seeded from the event log, a still-ongoing state is not logged again")
def _():
    seeded = db.last_ups_states(app.CONN)["mercury-ups"]
    eq(seeded["ups:power"], "utility")
    app.UPS.pop("mercury-ups", None)
    rec = dict(seeded)
    eq(poll("mercury-ups", T0 + 1500, status="OL CHRG", recorded=rec), [])
    # But a change found on the first poll after a restart is logged, with no date.
    app.UPS.pop("mercury-ups", None)
    rec = dict(seeded)
    ev = poll("mercury-ups", T0 + 1530, status="OB", recorded=rec)
    p = [e for e in ev if e[0] == "ups:power"][0]
    assert "not known" in p[3], p[3]
    RECORDED["mercury-ups"] = rec


@case("first sighting ever on battery is logged; on utility it is not")
def _():
    ev = poll("second-ups", T0, status="OB", recorded={})
    assert ("ups:power", None, "battery") in [e[:3] for e in ev], ev
    app.UPS.pop("second-ups", None)


@case("silence after a critical reading stays critical")
def _():
    rec = {}
    poll("second-ups", T0 + 10, status="OB LB", recorded=rec)
    poll("second-ups", T0 + 40, mode="drop", recorded=rec)
    poll("second-ups", T0 + 70, mode="drop", recorded=rec)
    v = view("second-ups", T0 + 71)
    eq((v["fresh"], v["status"], v["power"]), (False, "critical", "unknown"))
    RECORDED["second-ups"] = rec


@case("multiple UPSes are polled and judged independently")
def _():
    poll("second-ups", T0 + 2000, status="OL", **{"ups.model": "Smart-UPS 1500"})
    poll("mercury-ups", T0 + 2000, status="OB DISCHRG")
    vs = {v["id"]: v for v in app.ups_views(T0 + 2001)}
    eq(sorted(vs), ["mercury-ups", "second-ups", "spare"])
    eq((vs["mercury-ups"]["power"], vs["second-ups"]["power"]), ("battery", "utility"))
    eq(vs["second-ups"]["reading"]["model"], "Smart-UPS 1500")
    eq((vs["spare"]["comm"]["state"], vs["spare"]["status"]), ("disabled", "unknown"))
    hosts = {r["host"] for r in db.recent_events(app.CONN, kind="ups", limit=500)}
    eq(hosts, {"mercury-ups", "second-ups"})


@case("hourly rollups count readings and battery time; prune keeps them")
def _():
    db.rollup(app.CONN)
    rows = db.ups_rollup_series(app.CONN, "mercury-ups", T0 - 3600)
    assert rows, rows
    assert sum(r["samples"] for r in rows) == len(db.ups_history(app.CONN, "mercury-ups", 0))
    assert sum(r["on_battery"] for r in rows) > 0
    assert all(r["charge"] is None or 0 <= r["charge"] <= 100 for r in rows)
    # Fold twice: a bucket only grows by new readings.
    before = sum(r["samples"] for r in rows)
    db.rollup(app.CONN)
    eq(sum(r["samples"] for r in db.ups_rollup_series(app.CONN, "mercury-ups", T0 - 3600)),
       before)


# ---------- the API ----------

srv = app.Server(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % srv.server_address[1]


def get(path):
    with urlopen(URL + path, timeout=5) as r:
        return json.loads(r.read())


@case("API: /api/ups lists every UPS, /api/overview carries them apart from hosts")
def _():
    # Live readings, at the real clock, so freshness is judged as in production.
    now = time.time()
    poll("mercury-ups", now, status="OL")
    poll("second-ups", now, mode="drop")
    poll("second-ups", now, mode="drop")
    d = get("/api/ups")
    vs = {v["id"]: v for v in d["ups"]}
    eq((vs["mercury-ups"]["fresh"], vs["mercury-ups"]["power"]), (True, "utility"))
    eq((vs["second-ups"]["fresh"], vs["second-ups"]["status"]), (False, "stale"))
    assert "vars" not in vs["mercury-ups"]
    ov = get("/api/overview")
    eq(sorted(u["id"] for u in ov["ups"]), ["mercury-ups", "second-ups", "spare"])
    assert not any(h["host"] in ("mercury-ups", "second-ups") for h in ov["hosts"])


@case("API: /api/ups/<id> has the reading, raw variables, history and only its events")
def _():
    d = get("/api/ups/mercury-ups?minutes=1440")
    eq(d["resolution"], "raw")
    eq(d["current"]["vars"]["ups.firmware"], "951.e3 .D")
    assert len(d["history"]) > 5
    assert d["events"] and all(e["host"] == "mercury-ups" and e["kind"].startswith("ups:")
                               for e in d["events"])
    d = get("/api/ups/mercury-ups?minutes=%d" % (30 * 1440))
    eq(d["resolution"], "hourly")
    try:
        get("/api/ups/nosuch")
        raise AssertionError("no 404")
    except HTTPError as e:
        eq(e.code, 404)


# ---------- MCP ----------

spec = importlib.util.spec_from_file_location("ndmcp", os.path.join(root, "mcp", "netdash-mcp.py"))
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
API = mcp.Api(URL, 5)


def tool(name, **args):
    r = mcp.call_tool(API, name, args)
    text = r["content"][0]["text"]
    return r["isError"], (text if r["isError"] else json.loads(text))


@case("MCP: the ups tool lists each UPS with power, freshness and communication")
def _():
    err, d = tool("ups")
    assert not err, d
    us = {u["id"]: u for u in d["ups"]}
    m = us["mercury-ups"]
    eq((m["power"], m["fresh"], m["communication"]["state"]), ("utility", True, "ok"))
    eq(m["reading"]["runtime_seconds_estimate"], 1851)
    eq(m["reading"]["rated_power_w"], 810.0)
    assert "measured_power_w" not in m["reading"]
    eq(m["reading"]["not_reported"], [])
    s = us["second-ups"]
    eq((s["power"], s["fresh"], s["reading"]["is_current"]), ("unknown", False, False))
    assert s["communication"]["state"] == "lost"


@case("MCP: one UPS in depth -- history stats, battery health unverified, events")
def _():
    err, d = tool("ups", id="mercury-ups", hours=24)
    assert not err, d
    assert d["history"]["readings"] > 5
    assert d["history"]["readings_on_battery"] > 0
    assert "unverified" in d["reading"]["battery_health"]["device_reported_battery_date"]
    assert any(e["kind"] == "ups:power" for e in d["recent_events"])
    err, msg = tool("ups", id="nosuch")
    assert err and "mercury-ups" in msg, msg


@case("MCP: missing readings are named as not reported, never zero")
def _():
    now = time.time()
    S.devices = {"ups": {"ups.status": "OL"}}
    app.ups_round(DEV["mercury-ups"], RECORDED["mercury-ups"], now=now)
    S.devices = {"ups": dict(SAMPLE)}
    err, d = tool("ups")
    m = [u for u in d["ups"] if u["id"] == "mercury-ups"][0]
    eq(set(m["reading"]["not_reported"]), set(mcp._UPS_CORE))
    assert "battery_charge_pct" not in m["reading"]


@case("MCP: fleet_status carries UPSes needing attention, apart from hosts")
def _():
    err, d = tool("fleet_status", problems_only=True)
    assert not err, d
    eq([u["id"] for u in d.get("ups", [])], ["second-ups"])
    assert all(h["host"] != "second-ups" for h in d["hosts"])


# ---------- the one rule ----------

@case("nothing is sent but the allowlisted query, and nothing else can be")
def _():
    # Every line every connection received, across every case above.
    assert len(RECEIVED) > 40, len(RECEIVED)
    bad = [l for l in RECEIVED if l not in (b"LIST VAR ups\n", b"LIST VAR nosuch\n")]
    eq(bad, [])
    for line in ("INSTCMD ups test.battery.start", "SET VAR ups ups.delay.shutdown 20",
                 "FSD ups", "LOGIN ups", "USERNAME admin", "PASSWORD x", "PRIMARY ups",
                 "LIST VAR ups\nFSD ups", "LIST VAR ups FSD", "LIST CMD ups",
                 "LIST RW ups", "GET VAR ups ups.status", "LOGOUT", ""):
        try:
            nut._send(None, line)
            raise AssertionError("sent %r" % line)
        except ValueError:
            pass
    # A UPS name that tries to smuggle a second command never reaches a socket.
    before = S.connections
    try:
        nut.query("127.0.0.1", S.port, "ups\nFSD ups", 1)
        raise AssertionError("no error")
    except nut.NutError as e:
        eq(e.kind, nut.CONFIG)
    eq(S.connections, before)
    # And the module has exactly one place that writes to a socket.
    src = open(os.path.join(root, "server", "nut.py")).read()
    eq(src.count("sendall("), 1)
    eq(src.count(".send("), 0)


print(json.dumps(OUT))
