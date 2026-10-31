"""The MCP server against a real netdash server on 127.0.0.1, seeded by
seed_fake.py. Driven by the `mcp` case in tests/run.sh, which reads the JSON
this prints.

The MCP server runs as a subprocess, spoken to over its stdin and stdout
exactly as an agent's client would. Nothing leaves this machine.
"""
import contextlib, io, json, os, subprocess, sys, tempfile, threading, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import app, db, seed_fake

TMP = tempfile.mkdtemp()
NOW = int(time.time())

# ---------- a real server, seeded ----------

cfg = json.load(open(os.path.join(root, "server", "config.example.json")))
cfg.update(db_path=os.path.join(TMP, "t.db"), bind_host="127.0.0.1", bind_port=0)
cfg["eol"] = {"enabled": False}             # endoflife.date is the network
cfg["reachability"] = {"enabled": False}
# Disabled, so nothing is ever polled, but still listed with its state.
cfg["services"] = {"checks": [{"kind": "plex", "host": "linux-server",
                               "url": "http://127.0.0.1:9", "enabled": False}]}
for k in ("truenas", "hubitat", "unifi"):
    cfg.pop(k, None)
# A laptop allowed to sleep: silent, and not a problem.
cfg["hosts"] = {"sleepy-laptop": {"expect_up": False}}
path = os.path.join(TMP, "server.json")
json.dump(cfg, open(path, "w"))

with contextlib.redirect_stdout(io.StringIO()):
    sys.argv = ["seed_fake.py", path]
    seed_fake.main()
sys.argv = [sys.argv[0], root]

app.CFG = app.load_config(path)
app.CONN = db.connect(app.CFG["db_path"])
db.insert_sample(app.CONN, {"host": "sleepy-laptop", "ts": NOW - 7200, "os": "macOS 15.6",
                            "cpu_pct": 3.0, "mem_used_bytes": 1, "mem_total_bytes": 4,
                            "uptime_seconds": 9, "disks": []})
# bsd-server's pool down a disk, as its collector's zpool status would report
# it. Written straight to its storage row, at its latest sample's time, so the
# seeded readings and the fleet's counts stay as the other checks expect.
_bsd_ts = app.CONN.execute("SELECT MAX(ts) FROM samples WHERE host='bsd-server'").fetchone()[0]
app.CONN.execute("INSERT INTO storage (host, ts, pools, smart) VALUES (?,?,?,NULL)",
                 ("bsd-server", _bsd_ts, json.dumps([{
                     "name": "tank", "state": "DEGRADED", "boot": False,
                     "read_errors": 0, "write_errors": 0, "checksum_errors": 0,
                     "data_errors": 0, "bad_devices": [{"name": "ada3p1", "state": "UNAVAIL"}],
                     "scan": None, "detail": None}])))
app.CONN.commit()
db.record_event(app.CONN, "pi-armv7", "status", "ok", "warning", "disk / at 93%",
                ts=NOW - 600)
db.record_event(app.CONN, "linux-server", "service:plex-linux-server", "ok", "fail",
                "connection refused", ts=NOW - 300)
db.record_event(app.CONN, "linux-server", "servicex", None, "odd",
                "a kind that only starts with 'service'", ts=NOW - 200)
db.record_event(app.CONN, "pi-armv7", "status", "critical", "ok", "two days ago",
                ts=NOW - 2 * 86400)

POSTS = []


class Counting(app.Handler):
    def do_POST(self):
        POSTS.append(self.path)
        return app.Handler.do_POST(self)


srv = app.Server(("127.0.0.1", 0), Counting)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % srv.server_address[1]

# ---------- the MCP server, as a client would run it ----------

MCP = os.path.join(root, "mcp", "netdash-mcp.py")


class Session:
    def __init__(self, url=URL):
        self.p = subprocess.Popen([sys.executable, MCP, "--url", url, "--timeout", "3"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.lines = []
        self.n = 0

    def send(self, msg):
        self.p.stdin.write((msg if isinstance(msg, str) else json.dumps(msg)) + "\n")
        self.p.stdin.flush()

    def read(self):
        line = self.p.stdout.readline()
        self.lines.append(line)
        return json.loads(line)

    def rpc(self, method, params=None):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method,
                   **({"params": params} if params is not None else {})})
        return self.read()

    def tool(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        return r["isError"], (text if r["isError"] else json.loads(text))

    def close(self):
        self.p.stdin.close()
        rest = self.p.stdout.read()
        self.p.wait(timeout=5)
        return rest


s = Session()
out = {}

init = s.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"}})["result"]
out["init_version"] = init["protocolVersion"]
out["init_tools_cap"] = "tools" in init["capabilities"]
out["server_version"] = init["serverInfo"]["version"]
out["has_instructions"] = bool(init.get("instructions"))

# A notification is never answered: the next line must be the ping's reply.
s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
out["after_notification"] = s.rpc("ping")
out["ping_id"] = s.n

tools = s.rpc("tools/list")["result"]["tools"]
out["tool_names"] = sorted(t["name"] for t in tools)
out["all_read_only"] = all(t["annotations"]["readOnlyHint"] for t in tools)
out["schemas_are_objects"] = all(t["inputSchema"]["type"] == "object" for t in tools)

err, fleet = s.tool("fleet_status")
out["fleet_err"] = err
out["fleet_total"] = fleet["hosts_total"]
out["fleet_by_status"] = fleet["by_status"]
out["fleet_iso"] = fleet["as_of"]
byname = {h["host"]: h for h in fleet["hosts"]}
out["laptop_status"] = byname["sleepy-laptop"]["status"]
out["no_thresholds_per_host"] = not any("thresholds" in h for h in fleet["hosts"])

err, probs = s.tool("fleet_status", problems_only=True)
out["problems"] = sorted(h["host"] for h in probs["hosts"])
out["zfs_brief"] = byname["bsd-server"].get("storage")
out["healthy_no_storage"] = all("storage" not in h for h in fleet["hosts"] if h["host"] != "bsd-server")
err, zd = s.tool("host_detail", host="bsd-server")
out["zfs_detail"] = zd.get("storage")

err, pi = s.tool("host_detail", host="pi-armv7", minutes=60)
out["pi_err"] = err
out["pi_root"] = [m for m in pi["disk_mounts"] if m["mount"] == "/"][0]
out["pi_cpu_points"] = (pi.get("cpu_history") or {}).get("points")
out["pi_has_series"] = "series" in pi
out["pi_events"] = [e["to"] for e in pi.get("recent_events") or []]
err, pi2 = s.tool("host_detail", host="pi-armv7", include_series=True)
out["pi_series_len"] = len(pi2.get("series") or [])
out["pi_series_cols"] = pi2.get("series_columns")

out["bad_host"] = s.tool("host_detail", host="pi")
out["missing_host_arg"] = s.tool("host_detail")

err, ev = s.tool("events", hours=24)
out["ev_24h"] = sorted(e["kind"] for e in ev["events"])
err, ev = s.tool("events", hours=72)
out["ev_72h_count"] = ev["count"]
err, ev = s.tool("events", hours=24, kind="service")
out["ev_service"] = [e["kind"] for e in ev["events"]]
err, ev = s.tool("events", hours=72, host="pi-armv7")
out["ev_host"] = sorted({e["host"] for e in ev["events"]})
out["ev_bad_hours"] = s.tool("events", hours="lots")

err, pat = s.tool("patches", host="linux-server")
out["patches_ls"] = pat["hosts"][0]
err, pat = s.tool("patches")
out["patches_all"] = len(pat["hosts"])
out["patches_bad_host"] = s.tool("patches", host="nope")[0]

err, svc = s.tool("services")
out["services"] = [(r["host"], r["kind"], r["state"]) for r in svc["services"]]
out["disabled_not_a_problem"] = "services" not in byname["linux-server"]
out["services_bad_host"] = s.tool("services", host="nope")[0]

out["unknown_tool"] = s.rpc("tools/call", {"name": "forget_host", "arguments": {}})
out["unknown_method"] = s.rpc("resources/list")
s.send("{not json")
out["parse_error"] = s.read()
s.send(json.dumps([{"jsonrpc": "2.0", "id": 900, "method": "ping"},
                   {"jsonrpc": "2.0", "method": "notifications/cancelled"}]))
out["batch"] = s.read()
s.send("[]")
out["empty_batch"] = s.read()

rest = s.close()
out["trailing_stdout"] = rest
out["stdout_all_json"] = all(json.loads(l) is not None for l in s.lines)
out["posts"] = POSTS

# An unknown protocol version gets the newest one back, not an error.
s2 = Session()
out["init_unknown_version"] = s2.rpc("initialize", {"protocolVersion": "1999-01-01"})[
    "result"]["protocolVersion"]
s2.close()

# Nothing listening: the tool answers with the reason, and the session lives.
s3 = Session("http://127.0.0.1:9")
s3.rpc("initialize", {"protocolVersion": "2025-06-18"})
out["down_err"] = s3.tool("fleet_status")
out["down_still_alive"] = "result" in s3.rpc("ping")
s3.close()

# The API itself: kind is a prefix at ':' only, and a junk `since` is refused.
from urllib.request import urlopen
from urllib.error import HTTPError
out["api_kind_service"] = sorted(e["kind"] for e in json.load(
    urlopen(URL + "/api/events?kind=service"))["events"])
out["api_kind_exact"] = sorted(e["kind"] for e in json.load(
    urlopen(URL + "/api/events?kind=servicex"))["events"])
out["api_host"] = sorted({e["host"] for e in json.load(
    urlopen(URL + "/api/events?host=pi-armv7"))["events"]})
out["api_since"] = len(json.load(
    urlopen(URL + "/api/events?since=%d" % (NOW - 3600)))["events"])
try:
    urlopen(URL + "/api/events?since=yesterday")
    out["api_bad_since"] = 200
except HTTPError as e:
    out["api_bad_since"] = e.code

# A URL with no scheme is the user's typo, said at startup -- not every tool
# blaming netdash for sending something that is not JSON.
r = subprocess.run([sys.executable, MCP, "--url", "netdash.lan:8080"],
                   input="", capture_output=True, text=True, timeout=10)
out["no_scheme"] = [r.returncode, "http://" in r.stderr, r.stdout]

# In-process, against a stub API, for what a real server will not produce.
import importlib.util
spec = importlib.util.spec_from_file_location("netdash_mcp", MCP)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# Hourly rollups: cpu_pct is the hour's average. A ten-minute spike to 100% is
# an hour averaging 25%; the peak is the spike, and when it was.
hourly = [{"ts": NOW - 7200, "cpu_pct": 5.0, "cpu_min": 2.0, "cpu_max": 9.0},
          {"ts": NOW - 3600, "cpu_pct": 25.0, "cpu_min": 4.0, "cpu_max": 100.0},
          {"ts": NOW, "cpu_pct": 6.0, "cpu_min": 3.0, "cpu_max": 8.0}]
st = m.series_stats(hourly, "cpu_pct", "cpu_min", "cpu_max")
out["hourly_stats"] = [st["min"], st["avg"], st["max"], st["max_at"] == m.iso(NOW - 3600)]
raw = [{"ts": NOW - 60, "cpu_pct": 10.0}, {"ts": NOW, "cpu_pct": 30.0}]
st = m.series_stats(raw, "cpu_pct", "cpu_min", "cpu_max")
out["raw_stats"] = [st["min"], st["avg"], st["max"]]


class Stub:
    def get(self, path, **q):
        return {"hosts": [{"os": "no name"}]}   # a host row with no "host"


# A KeyError inside a tool is a bug to log, not "unknown tool" for a tool the
# agent was just shown.
err = io.StringIO()
with contextlib.redirect_stderr(err):
    r = m.handle(Stub(), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "patches", "arguments": {"host": "x"}}})
out["inner_keyerror"] = [r["error"]["code"], "KeyError" in err.getvalue()]

srv.shutdown()
print(json.dumps(out))
