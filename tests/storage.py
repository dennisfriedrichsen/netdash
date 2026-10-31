"""ZFS pool health, scrubs and SMART, from TrueNAS and from a ZFS host's
collector. Driven by the `storage` case in tests/run.sh, which reads the JSON
this prints.

The TrueNAS answers are the fixtures captured live from mercury (CORE 13.0)
and freecore (FreeCORE 15.1); the zpool text is cerium's. Every failure below
is one of those real answers with one field changed, because no box here has
a DEGRADED pool to capture.
"""
import copy, json, os, sys, tempfile, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import app, db, truenas, zfs

FIX = os.path.join(root, "tests", "fixtures")
tmp = tempfile.mkdtemp()
app.CFG = {"thresholds": {"cpu": {"warn": 80, "crit": 95}, "mem": {"warn": 85, "crit": 95},
                          "disk": {"warn": 85, "crit": 95}},
           "stale_after_seconds": 180, "patch_stale_hours": 48,
           "eol": {"enabled": False}, "hosts": {}, "reachability": {}}
app.CONN = db.connect(os.path.join(tmp, "t.db"))
out = {}


def load(box, name):
    with open(os.path.join(FIX, "truenas", box, name)) as f:
        return json.load(f)


def fake_for(box, answers=None):
    """A _request answering from one box's fixtures; `answers` overrides
    a path. Records every call, so a test can say what was never asked."""
    calls = []
    files = {"/pool": "pool.json", "/boot/get_state": "boot-get_state.json",
             "/disk": "disk.json", "/smart/test/results": "smart-test-results.json"}

    def fake(cfg, path, method="GET", body=None, timeout=15, auth="bearer"):
        calls.append((path, body))
        if answers and path in answers:
            if isinstance(answers[path], Exception):
                raise answers[path]
            return copy.deepcopy(answers[path])
        if path == "/system/info":
            return {"physmem": 8 << 30, "uptime_seconds": 1000, "version": "TrueNAS-13.0-U6.8"}
        if path == "/system/product_name":
            return "TrueNAS"
        if path == "/pool/dataset":
            return [{"name": "tank", "used": {"parsed": 1}, "available": {"parsed": 3}}]
        if path in files:
            return load(box, files[path])
        if path == "/disk/smart_attributes":
            try:
                r = load(box, "smart-attributes-%s.json" % body)
            except FileNotFoundError:
                raise truenas.TrueNASError("POST %s -> HTTP 422" % path)
            if isinstance(r, dict) and "__error__" in r:
                raise truenas.TrueNASError(r["__error__"])
            return r
        raise truenas.TrueNASError("%s %s -> HTTP 404" % (method, path))
    return fake, calls


def poll(host, box, answers=None, at=None):
    """One TrueNAS poll, stored, and the host's summary."""
    truenas._SMART_CACHES.clear()
    truenas._request, calls = fake_for(box, answers)
    p = truenas.collect({"host": host, "api_key": "k"})
    p["ts"] = int(at or time.time())
    db.insert_sample(app.CONN, p, source="truenas")
    s = [x for x in db.latest_per_host(app.CONN) if x["host"] == host][0]
    return app.summarize(s, at or time.time()), calls


def brief(v):
    sg = v["storage"]
    return {"status": v["status"], "storage": sg and sg["status"],
            "pools": sg and [[p["name"], p["state"], p["severity"], p["boot"],
                              p["scrub"]["severity"]] for p in sg["pools"]],
            "bad": sg and [d for p in sg["pools"] for d in p["bad_devices"]],
            "failing": sg and sg["failing"]}


# --- the two live boxes, as they are -------------------------------------
now = time.time()
v, calls = poll("mercury", "core13")
out["core13"] = brief(v)
sm = {d["disk"]: d for d in v["storage"]["smart"]}
out["core13_smart"] = {k: [d["serial"], d["status"], d["age_hours"], d["pool"], d["severity"]]
                       for k, d in sm.items()}
out["core13_scrub_age"] = {p["name"]: p["scrub"]["age_days"] is not None
                           for p in v["storage"]["pools"]}
out["core13_attr_calls"] = sorted(b for p, b in calls if p == "/disk/smart_attributes")

v, calls = poll("freecore", "freecore15")
out["freecore15"] = brief(v)
out["freecore15_smart"] = [[d["disk"], d["status"], d["severity"], d["note"]]
                           for d in v["storage"]["smart"]]
out["freecore15_tank_scrub"] = [p["scrub"] for p in v["storage"]["pools"] if p["name"] == "tank"][0]
# A disk with no tests is never asked its power-on hours.
out["freecore15_attr_calls"] = [b for p, b in calls if p == "/disk/smart_attributes"]

# --- failures, each one field away from a live answer ---------------------
pools = load("core13", "pool.json")
deg = copy.deepcopy(pools)
deg[0]["status"] = "DEGRADED"; deg[0]["healthy"] = False
deg[0]["topology"]["data"][0]["status"] = "DEGRADED"
deg[0]["topology"]["data"][0]["children"][2]["status"] = "FAULTED"   # ada4
out["degraded"] = brief(poll("mercury", "core13", {"/pool": deg})[0])

flt = copy.deepcopy(pools)
flt[1]["status"] = "FAULTED"
out["faulted"] = brief(poll("mercury", "core13", {"/pool": flt})[0])

cks = copy.deepcopy(pools)
cks[1]["topology"]["data"][0]["children"][0]["stats"]["checksum_errors"] = 3
cks[1]["topology"]["data"][0]["stats"]["checksum_errors"] = 3   # counted once, not twice
v = poll("mercury", "core13", {"/pool": cks})[0]
out["cksum"] = brief(v)
out["cksum_count"] = [p["checksum_errors"] for p in v["storage"]["pools"] if p["name"] == "tank001"][0]

boot = load("core13", "boot-get_state.json")
bb = copy.deepcopy(boot)
bb["groups"]["data"][0]["status"] = "FAULTED"; bb["status"] = "DEGRADED"
out["boot_degraded"] = brief(poll("mercury", "core13", {"/boot/get_state": bb})[0])

sres = load("core13", "smart-test-results.json")
bad = copy.deepcopy(sres)
for r in bad:
    if r["disk"] == "ada3":
        r["tests"][0]["status"] = "FAILED"
        r["tests"][0]["status_verbose"] = "Completed: read failure"
out["smart_failed"] = brief(poll("mercury", "core13", {"/smart/test/results": bad})[0])

# A disk with a standby timer is not woken to read its power-on hours.
disks = load("core13", "disk.json")
sb = copy.deepcopy(disks)
for d in sb:
    if d["name"] == "ada0":
        d["hddstandby"] = "60"
v, calls = poll("mercury", "core13", {"/disk": sb})
out["standby_calls"] = sorted(b for p, b in calls if p == "/disk/smart_attributes")
out["standby_age"] = [d["age_hours"] for d in v["storage"]["smart"] if d["disk"] == "ada0"][0]

# A disk untested for longer than smart_max_age_days is noted, not coloured.
old = copy.deepcopy(sres)
for r in old:
    if r["disk"] == "ada0":
        r["tests"][0]["lifetime"] -= 24 * 40
v = poll("mercury", "core13", {"/smart/test/results": old})[0]
out["smart_old"] = [v["status"]] + [[d["severity"], d["note"]] for d in v["storage"]["smart"]
                                    if d["disk"] == "ada0"][0]

# /pool unanswerable: no health is claimed, and the capacity rows still come.
down = truenas.TrueNASError("GET -> HTTP 500")
v = poll("nas-nopool", "core13", {"/pool": down, "/disk": down})[0]
out["no_pool_answer"] = [v["status"], v["storage"] is None,
                         [m["mount"] for m in v["disk"]["mounts"]]]

# --- scrubs ---------------------------------------------------------------
def scan(**k):
    s = {"function": "SCRUB", "state": "FINISHED", "start": None, "end": now - 3 * 86400, "errors": 0}
    s.update(k)
    return s
out["scrub"] = {
    "recent": zfs.judge_scrub(scan(), now)[0],
    "41d": zfs.judge_scrub(scan(end=now - 41 * 86400), now)[0],
    "43d": zfs.judge_scrub(scan(end=now - 43 * 86400), now)[:2],
    "errors": zfs.judge_scrub(scan(errors=2), now)[:2],
    "running": zfs.judge_scrub(scan(state="SCANNING", end=None), now)[0],
    "never": zfs.judge_scrub(None, now)[:2],
    "resilvered": zfs.judge_scrub(scan(function="RESILVER"), now)[0],
}
app.CFG["hosts"] = {"mercury": {"thresholds": {"zfs": {"scrub_max_age_days": 10}}}}
v = poll("mercury", "core13")[0]
out["scrub_override"] = [v["status"], v["storage"]["limits"]["scrub_max_age_days"],
                         [p["name"] for p in v["storage"]["pools"] if p["scrub"]["severity"] == "warning"]]
app.CFG["hosts"] = {}

# --- a host that stops sending pool health -------------------------------
db.insert_sample(app.CONN, {"host": "mercury", "ts": int(now) + 600, "cpu_pct": 1.0})
s = [x for x in db.latest_per_host(app.CONN) if x["host"] == "mercury"][0]
out["storage_ages_out"] = app.summarize(s, now + 600)["storage"] is None

# --- the event log --------------------------------------------------------
recorded = []
app_record = db.record_event
db.record_event = lambda conn, host, kind, a, b, detail=None, ts=None: recorded.append([host, kind, a, b, detail])
last = {}
ok = poll("nas-ev", "core13")[0]["storage"]
app.storage_events("nas-ev", ok, last)
first = list(recorded)
dg = poll("nas-ev", "core13", {"/pool": deg})[0]["storage"]
app.storage_events("nas-ev", dg, last)
app.storage_events("nas-ev", dg, last)           # unchanged: nothing more
app.storage_events("nas-ev", ok, last)           # repaired
fc = poll("freecore", "freecore15")[0]["storage"]
app.storage_events("freecore", fc, {})           # never scrubbed: unknown, not logged
# First sight of a pool already bad is logged.
app.storage_events("nas2", dg, {})
db.record_event = app_record
out["events_first"] = first
out["events"] = recorded

db.record_event(app.CONN, "mercury", "storage:pool:tank", "ok", "warning", "tank DEGRADED")
db.record_event(app.CONN, "mercury", "storage:pool:tank", "warning", "ok", "tank ONLINE")
db.record_event(app.CONN, "mercury", "service:x", None, "ok")
out["seed"] = {"|".join(k): v for k, v in db.last_storage_states(app.CONN).items()}

# --- a ZFS host's collector -----------------------------------------------
with open(os.path.join(FIX, "freebsd", "zpool-status-p.txt")) as f:
    text = f.read()
pl = zfs.parse_zpool_status(text, "+0000")
out["cerium"] = [[p["name"], p["state"], p["read_errors"], p["write_errors"], p["checksum_errors"],
                  p["data_errors"], p["bad_devices"], (p["scan"] or {}).get("function"),
                  (p["scan"] or {}).get("state"), bool((p["scan"] or {}).get("end"))] for p in pl]
# zpool indents with tabs; the capture came through a terminal paste, which
# turned them into four spaces. Both must parse alike.
tabbed = "\n".join(("\t" + l[4:]) if l.startswith("    ") else l for l in text.splitlines())
out["tabs_same"] = zfs.parse_zpool_status(tabbed, "+0000") == pl
# The zone moves the scrub time by exactly the offset.
a = zfs.parse_zpool_status(text, "+0000")
b = zfs.parse_zpool_status(text, "-0500")
out["tz_shift"] = [(y["scan"]["end"] - x["scan"]["end"]) if x["scan"] and x["scan"].get("end") else None
                   for x, y in zip(a, b)]

# The same text with a mirror member gone, as zpool prints it.
first_pool = pl[0]["name"]
lines = text.splitlines()
out["cerium_degraded"] = None
for i, ln in enumerate(lines):
    f = ln.split()
    # The first leaf device row of the first pool: replace ONLINE with
    # UNAVAIL on it and DEGRADED on the pool and its vdev.
    if len(f) >= 5 and f[1] == "ONLINE" and f[0] != first_pool and not f[0].startswith(("mirror", "raidz")):
        lines[i] = ln.replace("ONLINE", "UNAVAIL", 1) + "  cannot open"
        dev = f[0]
        break
deg_text = "\n".join(lines).replace(" state: ONLINE", " state: DEGRADED", 1)
d = zfs.parse_zpool_status(deg_text, "+0000")[0]
out["cerium_degraded"] = [d["state"], d["bad_devices"] == [{"name": dev, "state": "UNAVAIL"}],
                          zfs.judge_pool(d)[0]]
perm = text.replace("errors: No known data errors", "errors: 4 data errors, use '-v' for a list", 1)
out["cerium_perm"] = zfs.judge_pool(zfs.parse_zpool_status(perm, "+0000")[0])
out["plain_counts"] = zfs._count("1.2K")

# Ingest turns the text into pools and stores no text.
pay = app.ingest_zpool({"host": "cerium", "zpool_status": text, "tz": "+0100"})
out["ingest"] = ["zpool_status" in pay, len(pay.get("pools") or [])]
out["ingest_absent"] = "pools" in app.ingest_zpool({"host": "cerium"})
db.insert_sample(app.CONN, dict(pay, ts=int(now), os="FreeBSD 15.1-RELEASE-p6 (amd64)"))
s = [x for x in db.latest_per_host(app.CONN) if x["host"] == "cerium"][0]
v = app.summarize(s, now)
out["cerium_view"] = [v["status"], v["storage"]["smart"], len(v["storage"]["pools"])]
out["garbage"] = zfs.parse_zpool_status("no pools available\n") == [] \
    and zfs.parse_zpool_status("  pool: x\n nonsense") == []

print(json.dumps(out))
