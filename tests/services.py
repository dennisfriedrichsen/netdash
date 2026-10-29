"""Service checks against a real HTTP server on 127.0.0.1, serving the replies
the real services gave (tests/fixtures/services). Driven by the `services`
case in tests/run.sh, which reads the JSON this prints.

Everything here is mocked: nothing leaves this machine. The live checks are a
separate, manual step -- see "Service checks" in the README.
"""
import base64, http.server, json, os, shutil, socket, subprocess, sys
import tempfile, threading, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import services
import app, db

FIX = os.path.join(root, "tests", "fixtures", "services")
TMP = tempfile.mkdtemp()


def fixture(name, mode="rb"):
    with open(os.path.join(FIX, name), mode) as f:
        return f.read()


# ---------- a server that answers however a case needs it to ----------

LOG = []          # (method, path, headers) of every request served
ROUTES = {}       # path -> fn(handler) -> (status, headers, body)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _go(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        self.req_body = self.rfile.read(n) if n else b""
        LOG.append((method, self.path, dict(self.headers)))
        fn = ROUTES.get(self.path)
        if fn is None:
            status, headers, body = 404, {}, b"not found"
        else:
            r = fn(self)
            if r is None:          # the route wrote its own reply
                return
            status, headers, body = r
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")


class S(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass  # a client hanging up on a slow route is the point of the case


srv = S(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % PORT


def check(kind, **kw):
    c = dict({"kind": kind, "host": "h", "url": BASE, "enabled": True,
              "timeout_seconds": 1}, **kw)
    return services.parse_config({"checks": [c]})[0]


def run(c, memo=None):
    o, d, f, ms = services.run(c, {} if memo is None else memo)
    return [o, d, f]


def secret(name, value):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write(value + "\n")
    os.chmod(p, 0o600)
    return p


out = {}
J = {"Content-Type": "application/json"}

# ---------- Home Assistant ----------

ROUTES["/manifest.json"] = lambda h: (200, J, fixture("hass-manifest.json"))
LOG.clear()
out["ha_ok"] = run(check("home_assistant"))
# The thing that must never happen without a token: a request to /api/.
out["ha_untokened_paths"] = [p for _, p, _ in LOG]

ROUTES["/manifest.json"] = lambda h: (200, {}, fixture("plex-identity.xml"))
out["ha_not_json"] = run(check("home_assistant"))
ROUTES["/manifest.json"] = lambda h: (200, J, b'{"name": "Jellyfin"}')
out["ha_other_app"] = run(check("home_assistant"))
ROUTES["/manifest.json"] = lambda h: (502, {}, b"bad gateway")
out["ha_502"] = run(check("home_assistant"))

TOKEN = "eyJhbGciOiJIUzI1NiJ9.test-token-value.sig"
CONFIG = {"version": "2026.10.2", "state": "RUNNING", "recovery_mode": False,
          "safe_mode": False, "location_name": "Home"}


def api_config(body):
    def fn(h):
        if h.headers.get("Authorization") != "Bearer " + TOKEN:
            return 401, J, b'{"message":"Unauthorized"}'
        return 200, J, json.dumps(body).encode()
    return fn


tok = secret("hass.token", TOKEN)
ROUTES["/api/config"] = api_config(CONFIG)
out["ha_token_ok"] = run(check("home_assistant", token_file=tok))
out["ha_token_header"] = [h.get("Authorization") == "Bearer " + TOKEN
                          for _, p, h in LOG if p == "/api/config"][-1:]
ROUTES["/api/config"] = api_config(dict(CONFIG, state="NOT_RUNNING"))
out["ha_not_running"] = run(check("home_assistant", token_file=tok))
ROUTES["/api/config"] = api_config(dict(CONFIG, recovery_mode=True))
out["ha_recovery"] = run(check("home_assistant", token_file=tok))
ROUTES["/api/config"] = api_config(CONFIG)

# A wrong token is refused once and then not sent again: Home Assistant's
# ip_ban counts every one of these.
bad = secret("hass-bad.token", "not-the-token")
c = check("home_assistant", token_file=bad)
memo = {}
LOG.clear()
first = run(c, memo)
second = run(c, memo)
out["ha_auth"] = [first, second, len([p for _, p, _ in LOG if p == "/api/config"])]
secret("hass-bad.token", TOKEN)          # fixed: the file changed
out["ha_auth_fixed"] = run(c, memo)
out["ha_no_token_file"] = run(check("home_assistant",
                                    token_file=os.path.join(TMP, "missing")))
out["ha_empty_token"] = run(check("home_assistant", token_file=secret("empty", "")))

# ---------- Plex ----------

ROUTES["/identity"] = lambda h: (200, {"Content-Type": "text/xml"}, fixture("plex-identity.xml"))
out["plex_ok"] = run(check("plex"))
out["plex_id_match"] = run(check(
    "plex", machine_identifier="0123456789abcdef0123456789abcdef01234567"))
out["plex_id_mismatch"] = run(check("plex", machine_identifier="ffff"))
ROUTES["/identity"] = lambda h: (200, {"Content-Type": "text/html"},
                                 b"<html><body>Welcome to nginx!</body></html>")
out["plex_wrong_body"] = run(check("plex"))
ROUTES["/identity"] = lambda h: (401, {}, b"Unauthorized")
out["plex_401"] = run(check("plex"))
ROUTES["/identity"] = lambda h: (503, {}, b"")
out["plex_503"] = run(check("plex"))

# ---------- Transmission ----------

SID = "EXAMPLEsessionidEXAMPLEsessionidEXAMPLEsession0"
H409 = {}
for line in fixture("transmission-rpc-409.headers", "r").splitlines()[1:]:
    if ":" in line:
        k, v = line.split(":", 1)
        if k.lower() not in ("content-length", "date"):
            H409[k.strip()] = v.strip()


def rpc(user=None, password=None, reply=None):
    def fn(h):
        if user is not None:
            want = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
            if h.headers.get("Authorization") != want:
                return 401, {}, b"<h1>401: Unauthorized</h1>"
        if h.headers.get("X-Transmission-Session-Id") != SID:
            return 409, H409, fixture("transmission-rpc-409.html")
        if json.loads(h.req_body).get("method") != "session-stats":
            return 400, {}, b"unexpected method"
        return 200, J, reply or fixture("transmission-session-stats.json")
    return fn


ROUTES["/transmission/rpc"] = rpc()
c, memo = check("transmission"), {}
LOG.clear()
out["tx_ok"] = run(c, memo)
out["tx_first_round_requests"] = len(LOG)
LOG.clear()
run(c, memo)
out["tx_sid_reused_requests"] = len(LOG)
out["tx_methods"] = sorted({m for m, p, _ in LOG})

ROUTES["/transmission/rpc"] = rpc(user="netdash", password="s3cret-tx")
out["tx_needs_auth"] = run(check("transmission"))
pw = secret("tx.pw", "s3cret-tx")
out["tx_auth_ok"] = run(check("transmission", username="netdash", password_file=pw))
badpw = secret("tx-bad.pw", "wrong")
c, memo = check("transmission", username="netdash", password_file=badpw), {}
LOG.clear()
out["tx_auth_bad"] = [run(c, memo), run(c, memo), len(LOG)]
ROUTES["/transmission/rpc"] = lambda h: (403, {}, b"<h1>403: Forbidden</h1>")
out["tx_whitelist"] = run(check("transmission"))
ROUTES["/transmission/rpc"] = rpc(reply=b'{"arguments":{},"result":"no such method"}')
out["tx_result"] = run(check("transmission"))
ROUTES["/transmission/rpc"] = lambda h: (409, {}, b"conflict")
out["tx_409_no_sid"] = run(check("transmission"))

# ---------- the network misbehaving ----------


def hang(h):
    time.sleep(3)
    return 200, J, fixture("hass-manifest.json")


def trickle(h):
    # Headers at once, then the body one byte at a time: every socket call
    # returns inside the timeout, so only a deadline on the whole exchange
    # stops this.
    h.send_response(200)
    h.send_header("Content-Length", "100")
    h.end_headers()
    for _ in range(100):
        try:
            h.wfile.write(b"x")
            h.wfile.flush()
        except OSError:
            return None
        time.sleep(0.2)
    return None


ROUTES["/manifest.json"] = hang
t0 = time.monotonic()
out["timeout"] = run(check("home_assistant", timeout_seconds=1))
out["timeout_elapsed"] = time.monotonic() - t0
ROUTES["/identity"] = trickle
t0 = time.monotonic()
out["trickle"] = run(check("plex", timeout_seconds=1))
out["trickle_elapsed"] = time.monotonic() - t0

s = socket.socket()
s.bind(("127.0.0.1", 0))
closed = s.getsockname()[1]
s.close()
out["refused"] = run(check("plex", url="http://127.0.0.1:%d" % closed))
out["unresolvable"] = run(check("plex", url="http://no-such-host.invalid:32400"))
ROUTES["/identity"] = lambda h: (200, {}, b"<MediaContainer " + b"x" * (2 * services.MAX_BODY))
out["huge"] = run(check("plex"))

# ---------- configuration ----------

cfgs = services.parse_config({"checks": [
    {"kind": "plex", "host": "plexserver", "url": BASE},                    # no enabled
    {"kind": "plex", "host": "plexserver", "url": BASE, "enabled": False},
    {"kind": "plex", "host": "a", "enabled": True},                          # no url
    {"kind": "plex", "host": "b", "url": "http://u:p@10.0.0.1/", "enabled": True},
    {"kind": "gopher", "host": "c", "url": BASE, "enabled": True},
    {"kind": "plex", "id": "dup", "host": "d", "url": BASE, "enabled": True},
    {"kind": "plex", "id": "dup", "host": "e", "url": BASE, "enabled": True},
    {"kind": "ipmi", "host": "f", "enabled": True},                           # no address
    {"kind": "transmission", "host": "g", "url": BASE, "username": "x", "enabled": True},
    {"kind": "ipmi", "host": "h", "address": "10.0.0.9", "tls_fingerprint_sha256": "zz",
     "enabled": True},
    {"kind": "plex", "host": "i", "url": BASE, "interval_seconds": 1, "timeout_seconds": 999,
     "enabled": True},
    "not a dict",
]})
out["cfg"] = [[c["enabled"], c["config_error"]] for c in cfgs]
out["cfg_clamped"] = [cfgs[-1]["interval_seconds"], cfgs[-1]["timeout_seconds"]]
out["cfg_none"] = [services.parse_config(None), services.parse_config({}),
                   services.parse_config({"checks": []})]
out["cfg_error_run"] = run(cfgs[2])

# Where each check goes, as the Services table shows it -- from the same
# config the check runs from.
eps = services.parse_config({"checks": [
    {"kind": "plex", "host": "p", "url": "http://10.0.0.133:32400", "enabled": True},
    {"kind": "home_assistant", "host": "h", "url": "https://ha.example", "enabled": True},
    {"kind": "transmission", "host": "t", "url": "http://10.0.0.152:9091", "enabled": True},
    {"kind": "ipmi", "host": "m", "address": "10.0.0.219", "enabled": True},
    {"kind": "ipmi", "host": "n", "address": "10.0.0.220", "https": False, "enabled": True,
     "username": "u", "password_file": "/x"},
    {"kind": "home_assistant", "host": "h2", "url": "http://10.0.0.113:8123",
     "token_file": "/x", "enabled": True},
]})
out["endpoints"] = [[v["address"], v["ports"], v["probe"]]
                    for v in (services.public(c) for c in eps)]

# ---------- TLS: pinned, CA'd, or refused -- never unverified ----------

tls = {"skipped": None}
openssl = shutil.which("openssl")
if not openssl:
    tls["skipped"] = "no openssl binary"
else:
    key, crt = os.path.join(TMP, "k.pem"), os.path.join(TMP, "c.pem")
    r = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", crt, "-days", "2", "-subj", "/CN=IPMI",
                        "-addext", "subjectAltName=IP:127.0.0.1"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        tls["skipped"] = "openssl could not make a certificate"
    else:
        import ssl, hashlib
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        tsrv = S(("127.0.0.1", 0), H)
        tsrv.socket = ctx.wrap_socket(tsrv.socket, server_side=True)
        threading.Thread(target=tsrv.serve_forever, daemon=True).start()
        turl = "https://127.0.0.1:%d" % tsrv.server_address[1]
        der = ssl.PEM_cert_to_DER_cert(open(crt).read())
        pin = ":".join("%02X" % b for b in hashlib.sha256(der).digest())
        ROUTES["/identity"] = lambda h: (200, {}, fixture("plex-identity.xml"))
        tls["unpinned"] = run(check("plex", url=turl))
        tls["pinned"] = run(check("plex", url=turl, tls_fingerprint_sha256=pin))
        tls["wrong_pin"] = run(check("plex", url=turl, tls_fingerprint_sha256="ab" * 32))
        tls["ca_file"] = run(check("plex", url=turl, ca_file=crt))
        # The setting that would turn verification off does not exist.
        tls["verify_false_ignored"] = run(check("plex", url=turl, verify_tls=False))
out["tls"] = tls

# ---------- folding, staleness, disabled ----------

c = check("plex", failures_before_fail=2)
now = 1_800_000_000
a = services.fold(None, c, "ok", "media server answers", {}, 3, now)
b = services.fold(a, c, "fail", "connection refused by x", {}, 3, now + 60)
d = services.fold(b, c, "fail", "connection refused by x", {}, 3, now + 120)
e = services.fold(d, c, "ok", "media server answers", {}, 3, now + 180)
au = services.fold(e, c, "auth", "token rejected", {}, 3, now + 240)
out["fold"] = [[x["state"], x["failures"], x["detail"]] for x in (a, b, d, e, au)]
out["fold_since"] = [d["since"] == now + 120, e["since"] == now + 180]

v = services.view(c, d, now + 125)
out["view_fail"] = [v["state"], v["severity"], v["age_seconds"]]
v = services.view(c, a, now + 10_000)
out["view_stale"] = [v["state"], v["severity"], v["detail"]]
off = services.parse_config({"checks": [{"kind": "plex", "host": "h", "url": BASE}]})[0]
v = services.view(off, None, now)
out["view_disabled"] = [v["state"], v["severity"]]
v = services.view(c, None, now)
out["view_pending"] = [v["state"], v["severity"]]
v = services.view(cfgs[2], None, now)
out["view_config"] = [v["state"], v["severity"], v["detail"]]
out["view_keys"] = sorted(services.view(check("transmission", username="u",
                                              password_file=pw), a, now))

# ---------- the server: attached to hosts, never their status ----------

app.CFG = {"thresholds": {"cpu": {"warn": 80, "crit": 95}, "mem": {"warn": 85, "crit": 95},
                          "disk": {"warn": 85, "crit": 95}},
           "stale_after_seconds": 180, "patch_stale_hours": 48,
           "eol": {"enabled": False}, "hosts": {}, "reachability": {}}
app.CONN = db.connect(os.path.join(TMP, "t.db"))
t = time.time()


def sample(host):
    return {"host": host, "ts": t - 5, "os": "Debian GNU/Linux 13 (trixie)",
            "cpu_pct": 5.0, "mem_used_bytes": 10, "mem_total_bytes": 100,
            "uptime_seconds": 100, "disks": []}


old = app.summarize(sample("plexserver"), t)      # a config with no services block
out["old_config"] = [old["services"], old["services_severity"], old["status"]]

app.CFG["services"] = {"checks": [
    {"kind": "plex", "host": "plexserver", "url": BASE, "enabled": True},
    {"kind": "transmission", "host": "plexserver", "url": BASE, "enabled": False},
    {"kind": "home_assistant", "host": "hassium", "url": BASE, "enabled": True,
     "token_file": tok},
]}
plex = app.service_checks()[0]
app.SVC[plex["id"]] = services.fold(
    {"state": "unknown", "failures": 1}, plex, "fail", "connection refused", {}, 2, t)
v = app.summarize(sample("plexserver"), t)
out["attached"] = [v["status"], v["services_severity"],
                   [[s["name"], s["state"], s["severity"]] for s in v["services"]]]
out["other_host"] = [s["name"] for s in app.summarize(sample("hassium"), t)["services"]]
out["unattached"] = app.summarize(sample("cerium"), t)["services"]
# Nothing secret reaches the API: not the token, not where it is kept.
blob = json.dumps(app.summarize(sample("hassium"), t))
out["api_has_secret"] = [TOKEN in blob, tok in blob, "token_file" in blob]

# Events: a settled change is logged, a held single failure is not, and a
# restart picks up where the log left off.
seq = []
real_run = services.run
services.run = lambda c, memo, now=None: seq.pop(0)
c = app.service_checks()[0]
app.SVC.pop(c["id"], None)
last = None
for i, step in enumerate([("ok", "answers", {}, 1), ("fail", "refused", {}, 1),
                          ("fail", "refused", {}, 1), ("fail", "refused", {}, 1),
                          ("ok", "answers", {}, 1)]):
    seq.append(step)
    last = app.service_round(c, {}, last, now=t + i)
services.run = real_run
ev = db.recent_events(app.CONN, host="plexserver")
out["events"] = [[e["kind"], e["from_state"], e["to_state"], e["detail"]] for e in reversed(ev)]
out["events_seed"] = db.last_service_states(app.CONN)

# One check hanging holds up nothing else: not the API, not other checks.
ROUTES["/manifest.json"] = hang
ROUTES["/identity"] = lambda h: (200, {}, fixture("plex-identity.xml"))
app.CFG["services"] = {"checks": [
    {"kind": "home_assistant", "id": "slow", "host": "hassium", "url": BASE,
     "enabled": True, "timeout_seconds": 5, "interval_seconds": 15},
    {"kind": "plex", "id": "fast", "host": "plexserver", "url": BASE,
     "enabled": True, "interval_seconds": 15},
]}
app.SVC.clear()
for c in app.service_checks():
    threading.Thread(target=app.service_round, args=(c, {}, None), daemon=True).start()
time.sleep(0.5)
t0 = time.monotonic()
hv = app.summarize(sample("hassium"), time.time())
out["api_while_hung_s"] = time.monotonic() - t0
out["api_while_hung"] = [s["state"] for s in hv["services"]]
out["other_check_done"] = [s["state"] for s in app.summarize(
    sample("plexserver"), time.time())["services"]]

print(json.dumps(out, default=str))
