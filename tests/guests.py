"""Jails as their host reports them: the server half of host-side jail
reporting. Driven by the `guests` case in tests/run.sh, which reads the JSON
this prints. Names, releases and addresses are the ones mercury, freecore and
cerium actually have.
"""
import json, os, sys, tempfile, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import app, db

tmp = tempfile.mkdtemp()
app.CFG = {"thresholds": {"cpu": {"warn": 80, "crit": 95}, "mem": {"warn": 85, "crit": 95},
                          "disk": {"warn": 85, "crit": 95}},
           "stale_after_seconds": 180, "patch_stale_hours": 48,
           "eol": {"enabled": False}, "hosts": {}, "reachability": {}}
app.CONN = db.connect(os.path.join(tmp, "t.db"))
now = time.time()


def post(payload, source="push", at=None):
    payload = dict(payload, ts=int(at or now))
    db.insert_sample(app.CONN, payload, source=source)
    app.ingest_jails(payload, now=at or now)


def view(at=None):
    return {s["host"]: app.summarize(s, at or now) for s in db.latest_per_host(app.CONN)}


# mercury, polled over its API: one jail up, two stopped. Names and releases
# as the live box listed them.
mercury = {"host": "mercury", "os": "TrueNAS CORE 13.0 U6.8", "virt": "none", "jails": [
    {"host": "plexserver", "os": "FreeBSD 13.1-RELEASE-p9", "virt": "jail",
     "state": "running", "boot": True, "address": "10.0.0.133", "uptime_seconds": 8542097},
    {"host": "jellyfin", "os": "FreeBSD 13.2-RELEASE-p12", "virt": "jail",
     "state": "stopped", "boot": False},
    {"host": "transmission", "os": "FreeBSD 13.5-RELEASE-p14", "virt": "jail",
     "state": "stopped", "boot": False},
]}
post(mercury, source="truenas")
# cerium, a FreeBSD host collector, also has a "transmission" -- running, with
# readings, because the host measured it from inside.
cerium = {"host": "cerium", "os": "FreeBSD 15.1-RELEASE-p6 (amd64)", "virt": "none", "jails": [
    {"host": "transmission", "os": "FreeBSD 15.1-RELEASE-p6 (amd64)", "virt": "jail",
     "cpu_pct": 2.5, "mem_used_bytes": 200, "mem_total_bytes": 1000, "uptime_seconds": 500,
     "disks": [{"mount": "/", "used_bytes": 10, "total_bytes": 100}],
     "state": "running", "boot": True, "address": "10.0.0.150"},
]}
post(cerium)
# fastfetch-core pushes for itself; freecore then lists it too.
post({"host": "fastfetch-core", "os": "FreeBSD 15.1-RELEASE-p5 (amd64)", "virt": "jail",
      "cpu_pct": 0.1, "mem_used_bytes": 64, "mem_total_bytes": 4000, "uptime_seconds": 8000})
post({"host": "freecore", "os": "FreeCORE 15.1 U1.1", "jails": [
    {"host": "fastfetch-core", "os": "FreeBSD 15.1-RELEASE-p5", "virt": "jail",
     "state": "running", "boot": True, "address": "10.0.0.77", "uptime_seconds": 24704}]},
     source="truenas")
# A malformed entry and a name the dashboard could not route to are dropped.
post({"host": "junkhost", "jails": [{"host": "bad name"}, "nope", {"host": "junkhost"}]})

app.REACH["plexserver"] = {"state": "up", "probe": "up", "failures": 0, "address": "10.0.0.133",
                           "address_source": "parent", "via": "icmp", "detail": "answered",
                           "checked_at": int(now)}
v = view()
out = {
    "plex": [v["plexserver"][k] for k in ("status", "parent", "is_container", "os")],
    "plex_addr": list(app.address_for("plexserver", {})),
    "jellyfin": [v["jellyfin"]["status"], v["jellyfin"]["guest"]["state"], v["jellyfin"]["down_reason"]],
    "transmission": [v["transmission"]["status"], v["transmission"]["parent"]],
    "cerium_tx": ("transmission.cerium" in v) and [v["transmission.cerium"][k] for k in ("status", "parent")]
                 + [v["transmission.cerium"]["cpu"]["pct"]],
    "ffc_samples": app.CONN.execute("SELECT source, COUNT(*) FROM samples WHERE host='fastfetch-core' GROUP BY source").fetchall(),
    "ffc": [v["fastfetch-core"][k] for k in ("status", "parent")] + [v["fastfetch-core"]["cpu"]["pct"]],
    "junk": sorted(h for h in v if h not in ("mercury", "cerium", "freecore", "plexserver", "jellyfin",
                                            "transmission", "transmission.cerium", "fastfetch-core", "junkhost")),
}

# A minute later cerium's transmission keeps its qualified name, even while
# mercury -- holder of the plain one -- has gone quiet.
later = now + 600
post(dict(cerium), at=later)
out["tx_name_stable"] = "transmission.cerium" in view(later) and \
    db.guest(app.CONN, "transmission.cerium")["seen_at"] == int(later)
# mercury silent for ten minutes. Its stopped jails stay stopped on its last
# word, rather than each turning red with the NAS.
out["unvouched"] = view(later)["jellyfin"]["status"]

# mercury back, and plexserver gone from its list: absent reads as stopped.
post(dict(mercury, jails=[j for j in mercury["jails"] if j["host"] != "plexserver"]),
     source="truenas", at=later)
v2 = view(later)
out["absent"] = [v2["plexserver"]["status"], v2["plexserver"]["guest"]["state"]]

# A running jail whose address stops answering is down, though its parent's
# fresh report says running.
post(mercury, source="truenas", at=later)
app.REACH["plexserver"] = {"state": "down", "probe": "down", "failures": 2, "address": "10.0.0.133",
                           "address_source": "parent", "via": "icmp", "detail": "no reply",
                           "checked_at": int(later)}
v3 = view(later)
out["probe_down"] = [v3["plexserver"]["status"], v3["plexserver"]["down_reason"]]
# ...but a stopped one is never down, whatever the probe says.
app.REACH["jellyfin"] = dict(app.REACH["plexserver"])
out["stopped_probe"] = v3["jellyfin"]["status"]
# Forgetting a jail takes its guests row with it.
db.forget_host(app.CONN, "jellyfin")
out["forgot"] = db.guest(app.CONN, "jellyfin")
print(json.dumps(out, default=list))
