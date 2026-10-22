"""Application and management-controller checks, attached to hosts the fleet
already has. Nothing is installed anywhere; this server asks, read-only.

A host being up says nothing about what it is for. hassium can push a perfect
sample every minute while Home Assistant on it has crashed, and a card that is
green because the kernel is fine would be the false reassurance the rest of
this project keeps refusing to give. So these checks are a second axis on the
card, never folded into the host's own dot -- the same separation patches and
the UniFi fleet already have, for the same reason: a colour that can mean two
different things stops meaning either.

Each check says exactly what it proves, in `checks`, and no more. A web
frontend that answers is not Home Assistant running automations; a Plex
/identity reply is not a library that plays; a Transmission RPC reply is not a
download making progress. The words on the card are the claim.

Outcomes of one attempt -- the vocabulary everything else here is built from:

  OK      the service answered, and answered as itself.
  FAIL    the check ran and the service did not answer properly: timed out,
          refused, a 5xx, or a reply that is not what this service sends.
  AUTH    the service answered and refused us: a token or password is
          missing, wrong, or no longer accepted.
  CONFIG  the check cannot be carried out as configured: no URL, an
          unreadable credential file, a certificate that does not match its
          pin. A fact about this server's setup, not about the service.
  ERROR   the check could not run for reasons on this side: the name does not
          resolve, no route, a tool that is not installed.

As in reach.py, only FAIL is evidence against the service. The others are
"cannot tell", and are shown amber rather than red: a dashboard that paints a
service red because of its own broken plumbing teaches you to stop believing
red.

Secrets: tokens and passwords are read from files named in config, at check
time, and never appear in config, in the API, in logs or in error text. A
credential the service rejects is not sent again until its file changes --
Home Assistant's ip_ban, Transmission's anti-brute-force and a BMC's lockout
policy would all otherwise turn a typo into a ban, one poll at a time.

TLS is verified, always. A device with a self-signed certificate -- every BMC
ever shipped -- is pinned by fingerprint (tls_fingerprint_sha256) or given its
own CA (ca_file), per check. There is no switch to turn verification off.
"""

import base64
import errno
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit

OK, FAIL, AUTH, CONFIG, ERROR = "ok", "fail", "auth", "config", "error"

# Read caps. Every reply checked here is a few KB at most; a service that
# streams more than this is not answering the question it was asked.
MAX_BODY = 256 * 1024


class Outcome(Exception):
    """Raised from anywhere inside a check to end it with this verdict."""

    def __init__(self, outcome, detail, facts=None):
        Exception.__init__(self, detail)
        self.outcome = outcome
        self.detail = detail
        self.facts = facts or {}


# ---------- configuration ----------

KIND_DEFAULTS = {
    # name shown on the card, default poll interval
    "home_assistant": ("Home Assistant", 60),
    "plex": ("Plex", 60),
    "transmission": ("Transmission", 60),
    # Reachability is cheap; sensors are read on their own slower clock.
    "ipmi": ("BMC", 60),
}

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def parse_config(block):
    """The `services` block -> a list of normalised checks.

    Never raises. A malformed entry is still returned, carrying the reason in
    `config_error`, so it shows on its host's card as a check that cannot run
    rather than vanishing -- a typo should look like a typo, not like a check
    nobody asked for. Entries with no host to attach to are the exception:
    they have nowhere to be shown, so they are returned with host None and
    reported once at startup.
    """
    if not isinstance(block, dict):
        return []
    defaults = {
        "interval_seconds": block.get("interval_seconds"),
        "timeout_seconds": block.get("timeout_seconds"),
        "failures_before_fail": block.get("failures_before_fail"),
        "stale_after_seconds": block.get("stale_after_seconds"),
    }
    out, seen = [], set()
    for i, raw in enumerate(block.get("checks") or []):
        if not isinstance(raw, dict):
            continue
        c = dict(raw)
        kind = str(c.get("kind") or "")
        name, interval = KIND_DEFAULTS.get(kind, (kind or "check", 60))
        c["kind"] = kind
        c["host"] = c.get("host") if isinstance(c.get("host"), str) else None
        c["id"] = str(c.get("id") or "%s-%s" % (kind or "check", c["host"] or i))
        c["name"] = str(c.get("name") or name)
        c["enabled"] = c.get("enabled") is True
        for k, v in defaults.items():
            if c.get(k) is None and v is not None:
                c[k] = v
        c["interval_seconds"] = _clamp(c.get("interval_seconds"), 15, 3600, interval)
        c["timeout_seconds"] = _clamp(c.get("timeout_seconds"), 1, 30, 5)
        c["failures_before_fail"] = int(_clamp(c.get("failures_before_fail"), 1, 10, 2))
        # Three missed rounds, or what config says. Never shorter than one
        # round plus its timeout, or a slow-but-working check reads stale.
        c["stale_after_seconds"] = max(
            c["interval_seconds"] + c["timeout_seconds"],
            _clamp(c.get("stale_after_seconds"), 30, 86400, 3 * c["interval_seconds"]))
        c["config_error"] = _validate(c, seen)
        seen.add(c["id"])
        out.append(c)
    return out


def _validate(c, seen):
    if not _ID.match(c["id"]):
        return "id %r: letters, digits, '.', '_' and '-' only" % c["id"]
    if c["id"] in seen:
        return "duplicate id %r" % c["id"]
    if c["kind"] not in KIND_DEFAULTS:
        return "unknown kind %r (want one of %s)" % (
            c["kind"], ", ".join(sorted(KIND_DEFAULTS)))
    if c["kind"] == "ipmi":
        if not c.get("address"):
            return "ipmi needs an address"
    else:
        u = c.get("url")
        if not u:
            return "%s needs a url" % c["kind"]
        p = urlsplit(str(u))
        if p.scheme not in ("http", "https") or not p.hostname:
            return "url must be http:// or https:// with a host"
        # Credentials in a URL end up in the API, the logs and the browser.
        if p.username or p.password:
            return "url carries credentials; use token_file / username + password_file"
    pin = c.get("tls_fingerprint_sha256")
    if pin and _norm_pin(pin) is None:
        return "tls_fingerprint_sha256 must be 64 hex digits (colons allowed)"
    if c.get("username") and not c.get("password_file"):
        return "username given without password_file"
    if c.get("password_file") and not c.get("username"):
        return "password_file given without username"
    return None


def _norm_pin(pin):
    h = re.sub(r"[\s:]", "", str(pin)).lower()
    return h if re.fullmatch(r"[0-9a-f]{64}", h) else None


def public(c):
    """What the API may say about a check's configuration. No secrets, and no
    secret file paths either: knowing where the token lives is half of
    taking it."""
    auth = None
    if c.get("token_file"):
        auth = "token"
    elif c.get("username"):
        auth = "password"
    return {
        "id": c["id"], "kind": c["kind"], "name": c["name"], "host": c["host"],
        "role": "bmc" if c["kind"] == "ipmi" else "app",
        "target": c.get("url") or c.get("address"),
        "auth": auth,
        "enabled": c["enabled"],
        "interval_seconds": c["interval_seconds"],
        "timeout_seconds": c["timeout_seconds"],
    }


# ---------- secrets ----------

def _read_secret(path, what):
    """A credential from its file. The path may be named in an error, the
    content never."""
    try:
        with open(path) as f:
            s = f.read().strip()
    except OSError as e:
        raise Outcome(CONFIG, "cannot read %s: %s" % (what, e.strerror or e))
    if not s:
        raise Outcome(CONFIG, "%s is empty" % what)
    return s


def _fingerprint(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _latched(memo, secret):
    """True if this exact credential was rejected before. It is not sent again
    until the file holding it changes."""
    return memo.get("rejected") == _fingerprint(secret)


def _reject(memo, secret, detail):
    memo["rejected"] = _fingerprint(secret)
    raise Outcome(AUTH, detail + " -- not retried until the credential file changes")


# ---------- HTTP ----------

class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self, what):
        try:
            return json.loads(self.body)
        except ValueError:
            raise Outcome(FAIL, "%s answered HTTP %d but not with JSON" % (what, self.status))


def _tls_context(c):
    pin = _norm_pin(c["tls_fingerprint_sha256"]) if c.get("tls_fingerprint_sha256") else None
    if pin:
        # The pin is the verification: the certificate must be exactly this
        # one, which is a stronger claim than any chain to a CA, and the only
        # one available for a self-signed, long-expired BMC certificate. The
        # comparison happens right after the handshake, before a byte of the
        # request is sent.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # Old BMC firmware speaks nothing newer than TLS 1.2.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx, pin
    try:
        ctx = ssl.create_default_context(cafile=c.get("ca_file") or None)
    except (OSError, ssl.SSLError) as e:
        raise Outcome(CONFIG, "cannot load ca_file: %s" % e)
    return ctx, None


def _net_error(e, where, timeout):
    """Map a socket-level failure onto an outcome, the way reach.tcp does."""
    if isinstance(e, Outcome):
        return e
    if isinstance(e, (socket.timeout, TimeoutError)):
        return Outcome(FAIL, "no response from %s in %gs" % (where, timeout))
    if isinstance(e, ConnectionRefusedError):
        return Outcome(FAIL, "connection refused by %s" % where)
    if isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return Outcome(FAIL, "connection to %s dropped" % where)
    if isinstance(e, socket.gaierror):
        return Outcome(ERROR, "cannot resolve %s" % where)
    if isinstance(e, ssl.SSLCertVerificationError):
        return Outcome(CONFIG, "TLS certificate of %s not trusted (%s) -- pin it with "
                               "tls_fingerprint_sha256 or give a ca_file"
                       % (where, e.verify_message or e.reason))
    if isinstance(e, ssl.SSLError):
        return Outcome(FAIL, "TLS handshake with %s failed: %s" % (where, e.reason or e))
    if isinstance(e, http.client.HTTPException):
        return Outcome(FAIL, "malformed HTTP reply from %s (%s)" % (where, type(e).__name__))
    if isinstance(e, OSError):
        # EHOSTUNREACH on a LAN is an unanswered ARP: nothing is there.
        # ENETUNREACH and EACCES are this server's routing and firewall.
        if e.errno in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
            return Outcome(FAIL, "%s unreachable" % where)
        return Outcome(ERROR, "%s: %s" % (errno.errorcode.get(e.errno, e.errno), where))
    return Outcome(ERROR, "%s: %s" % (type(e).__name__, where))


def http_request(c, method, url, headers=None, body=None, timeout=None):
    """One request, no redirects, a bounded read, and a deadline on the whole
    exchange rather than per socket call -- a server that trickles a byte a
    second must not hold a check for minutes."""
    timeout = float(timeout or c["timeout_seconds"])
    deadline = time.monotonic() + timeout
    p = urlsplit(url)
    where = "%s:%s" % (p.hostname, p.port or (443 if p.scheme == "https" else 80))
    path = (p.path or "/") + ("?" + p.query if p.query else "")
    conn = None
    # Socket timeouts bound each recv, not the exchange: a server dribbling a
    # byte every half second never trips one, and http.client's read(n) waits
    # for all n. So a watchdog shuts the socket at the deadline, which wakes
    # whatever call is blocked on it.
    expired = threading.Event()

    def _expire():
        expired.set()
        s = conn.sock if conn is not None else None
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        if p.scheme == "https":
            ctx, pin = _tls_context(c)
            conn = http.client.HTTPSConnection(p.hostname, p.port, timeout=timeout,
                                               context=ctx)
            conn.connect()
            if pin:
                der = conn.sock.getpeercert(binary_form=True) or b""
                got = hashlib.sha256(der).hexdigest()
                if got != pin:
                    raise Outcome(CONFIG, "TLS certificate of %s does not match the "
                                          "pinned fingerprint (it presents %s...)"
                                  % (where, got[:16]))
        else:
            conn = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
            conn.connect()
        # The watchdog may have fired while the socket did not exist yet.
        if expired.is_set() or time.monotonic() > deadline:
            raise socket.timeout()
        h = {"User-Agent": "netdash", "Accept": "*/*", "Connection": "close"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        chunks, n = [], 0
        while n < MAX_BODY:
            if time.monotonic() > deadline:
                raise socket.timeout()
            b = r.read1(min(65536, MAX_BODY - n))
            if not b:
                break
            chunks.append(b)
            n += len(b)
        if expired.is_set():
            raise socket.timeout()
        return Response(r.status, {k.lower(): v for k, v in r.getheaders()},
                        b"".join(chunks))
    except Exception as e:
        if expired.is_set() and not isinstance(e, Outcome):
            e = socket.timeout()
        raise _net_error(e, where, timeout)
    finally:
        watchdog.cancel()
        if conn is not None:
            conn.close()


def _base(c):
    return str(c["url"]).rstrip("/")


# ---------- Home Assistant ----------

def check_home_assistant(c, memo):
    """Without a token: the frontend's /manifest.json, which needs no auth.
    With one: /api/config, whose `state` is the core's own RUNNING/STARTING.

    Deliberately never an unauthenticated request to /api/. Home Assistant
    answers that with a 401 and logs it as a failed login, and with ip_ban
    enabled enough of those ban this server's address outright.
    """
    base = _base(c)
    if not c.get("token_file"):
        r = http_request(c, "GET", base + "/manifest.json")
        if r.status != 200:
            raise Outcome(FAIL, "/manifest.json answered HTTP %d" % r.status)
        j = r.json("/manifest.json")
        if not isinstance(j, dict) or j.get("name") != "Home Assistant":
            raise Outcome(FAIL, "/manifest.json answered, but not as Home Assistant")
        return OK, "web frontend answers", {}

    token = _read_secret(c["token_file"], "token_file")
    if _latched(memo, token):
        raise Outcome(AUTH, "token was rejected earlier -- not retried until "
                            "token_file changes")
    r = http_request(c, "GET", base + "/api/config",
                     headers={"Authorization": "Bearer " + token})
    if r.status in (401, 403):
        _reject(memo, token, "token rejected (HTTP %d)" % r.status)
    if r.status != 200:
        raise Outcome(FAIL, "/api/config answered HTTP %d" % r.status)
    j = r.json("/api/config")
    if not isinstance(j, dict) or "version" not in j:
        raise Outcome(FAIL, "/api/config answered, but not as Home Assistant")
    facts = {"version": str(j.get("version"))}
    if j.get("recovery_mode") or j.get("safe_mode"):
        raise Outcome(FAIL, "core is in %s mode" % (
            "recovery" if j.get("recovery_mode") else "safe"), facts)
    state = j.get("state")
    if state is None:
        return OK, "core API answers (this version reports no run state)", facts
    if state != "RUNNING":
        raise Outcome(FAIL, "core state is %s" % state, facts)
    return OK, "core reports RUNNING", facts


# ---------- Plex ----------

_MC = re.compile(r"<MediaContainer\b([^>]*)>")
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def check_plex(c, memo):
    """/identity: unauthenticated, and answered by Plex Media Server itself.
    Parsed with a regex rather than an XML parser on purpose -- one start tag
    is all there is, and an XML parser on a reply from the network is a
    parser's worth of attack surface for nothing."""
    r = http_request(c, "GET", _base(c) + "/identity")
    if r.status in (401, 403):
        raise Outcome(AUTH, "/identity answered HTTP %d" % r.status)
    if r.status != 200:
        raise Outcome(FAIL, "/identity answered HTTP %d" % r.status)
    m = _MC.search(r.text())
    attrs = dict(_ATTR.findall(m.group(1))) if m else {}
    if not attrs.get("machineIdentifier"):
        raise Outcome(FAIL, "/identity answered, but not with a Plex MediaContainer")
    facts = {"version": attrs.get("version")}
    want = c.get("machine_identifier")
    if want and attrs["machineIdentifier"] != want:
        raise Outcome(FAIL, "a different Plex server answered (machineIdentifier "
                            "%s...)" % attrs["machineIdentifier"][:8], facts)
    return OK, "media server answers", facts


# ---------- Transmission ----------

def check_transmission(c, memo):
    """The daemon's RPC, asked for session-stats -- a read. The 409 dance is
    Transmission's CSRF guard: the first request is refused with a session id
    that every later one must carry, and is remembered between rounds."""
    url = _base(c) + str(c.get("rpc_path") or "/transmission/rpc")
    headers = {"Content-Type": "application/json"}
    secret = None
    if c.get("username"):
        secret = _read_secret(c["password_file"], "password_file")
        if _latched(memo, secret):
            raise Outcome(AUTH, "credentials were rejected earlier -- not retried "
                                "until password_file changes")
        headers["Authorization"] = "Basic " + base64.b64encode(
            ("%s:%s" % (c["username"], secret)).encode("utf-8")).decode("ascii")
    body = json.dumps({"method": "session-stats"}).encode("utf-8")
    r = None
    for _ in range(2):
        if memo.get("sid"):
            headers["X-Transmission-Session-Id"] = memo["sid"]
        r = http_request(c, "POST", url, headers=headers, body=body)
        if r.status != 409:
            break
        sid = r.headers.get("x-transmission-session-id")
        if not sid:
            raise Outcome(FAIL, "RPC answered 409 without a session id -- not Transmission")
        memo["sid"] = sid
        memo["rpc_version"] = r.headers.get("x-transmission-rpc-version")
    if r.status == 401:
        if secret is None:
            raise Outcome(AUTH, "RPC requires a username and password_file")
        _reject(memo, secret, "credentials rejected (HTTP 401)")
    if r.status == 403:
        raise Outcome(AUTH, "RPC refused this server's address (rpc-whitelist)")
    if r.status == 409:
        raise Outcome(FAIL, "RPC kept refusing the session id it issued")
    if r.status != 200:
        raise Outcome(FAIL, "RPC answered HTTP %d" % r.status)
    j = r.json("RPC")
    if not isinstance(j, dict) or j.get("result") != "success":
        raise Outcome(FAIL, "RPC answered result=%r" % (
            j.get("result") if isinstance(j, dict) else None))
    a = j.get("arguments") or {}
    facts = {k: a.get(k) for k in ("torrentCount", "activeTorrentCount",
                                   "pausedTorrentCount") if isinstance(a.get(k), int)}
    rpc_version = r.headers.get("x-transmission-rpc-version") or memo.get("rpc_version")
    if rpc_version:
        facts["rpc_version"] = rpc_version
    detail = "daemon answers RPC"
    if "torrentCount" in facts:
        detail += " -- %d torrent%s, %d active" % (
            facts["torrentCount"], "" if facts["torrentCount"] == 1 else "s",
            facts.get("activeTorrentCount", 0))
    return OK, detail, facts


# ---------- IPMI ----------
#
# Two separate questions with two separate answers, and the second may stay
# unknown for ever without that being a failure:
#
#   Is the BMC there?  Answered with no credentials at all: the IPMI pre-session
#                      Get Channel Authentication Capabilities on 623/udp, which
#                      every ipmitool session starts with and which changes
#                      nothing, plus the HTTPS interface with its certificate
#                      pinned. Either answering means the BMC is alive.
#
#   Is the hardware?   Only with credentials -- a USER-privilege account, which
#                      the BMC itself will not let issue a single write -- and
#                      ipmitool installed. Without both, sensors are reported
#                      unknown and say why. Never a guess, never a green.

IPMI_PORT = 623


def _ipmi_checksum(b):
    return (-sum(b)) & 0xFF


def channel_auth_request(seq=0):
    """RMCP + IPMI 1.5 session-less Get Channel Authentication Capabilities,
    channel 0x0E (this one), requesting IPMI 2.0 data, USER privilege."""
    hdr = [0x20, 0x06 << 2]
    body = [0x81, (seq & 0x3F) << 2, 0x38, 0x8E, 0x02]
    msg = hdr + [_ipmi_checksum(hdr)] + body + [_ipmi_checksum(body)]
    return bytes([0x06, 0x00, 0xFF, 0x07,       # RMCP v1.0, no ack, class IPMI
                  0x00, 0, 0, 0, 0, 0, 0, 0, 0,  # auth none, seq 0, session 0
                  len(msg)] + msg)


def parse_channel_auth(data):
    """The reply, or None if it is not one. Returns facts worth showing."""
    if len(data) < 14 + 7 + 1 or data[0] != 0x06 or data[3] != 0x07:
        return None
    msg = data[14:]
    # rqAddr, netFn/lun, cksum, rsAddr, seq, cmd, completion code, data...
    if len(msg) < 8 or (msg[1] >> 2) != 0x07 or msg[5] != 0x38:
        return None
    cc = msg[6]
    if cc != 0x00:
        return {"completion_code": cc}
    ipmi20 = bool(len(msg) > 8 and msg[8] & 0x80 and len(msg) > 10 and msg[10] & 0x02)
    return {"completion_code": 0, "channel": msg[7], "ipmi20": ipmi20}


def ipmi_ping(addr, timeout):
    """(answered, detail, facts). Raises Outcome(ERROR) only for this side's
    own failures -- a name that does not resolve, no route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        raise Outcome(ERROR, "cannot open a UDP socket: %s" % e)
    try:
        s.settimeout(timeout)
        s.connect((addr, IPMI_PORT))
        s.send(channel_auth_request(random.randrange(64)))
        data = s.recv(512)
    except (socket.timeout, TimeoutError):
        return False, "no IPMI answer on %d/udp in %gs" % (IPMI_PORT, timeout), {}
    except ConnectionRefusedError:
        return False, "%d/udp closed (ICMP port unreachable)" % IPMI_PORT, {}
    except socket.gaierror:
        raise Outcome(ERROR, "cannot resolve %s" % addr)
    except OSError as e:
        if e.errno in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
            return False, "%s unreachable" % addr, {}
        raise Outcome(ERROR, "%s: %s" % (errno.errorcode.get(e.errno, e.errno), addr))
    finally:
        s.close()
    info = parse_channel_auth(data)
    if info is None:
        return False, "something answered on %d/udp, but not IPMI" % IPMI_PORT, {}
    if info["completion_code"]:
        return True, "IPMI answers (completion code 0x%02x)" % info["completion_code"], {}
    return True, "IPMI answers", {"ipmi20": info["ipmi20"]}


# --- ipmitool output ---------------------------------------------------------
#
# `ipmitool sdr elist` prints one sensor per line:
#
#   CPU Temp         | 01h | ok  |  3.1 | 45 degrees C
#   FAN1             | 41h | ok  | 29.1 | 1500 RPM
#   FAN3             | 43h | ns  | 29.3 | No Reading
#   PS1 Status       | C8h | ok  | 10.1 | Presence detected
#
# The status column is ipmitool's reading of the sensor's thresholds; for a
# discrete sensor -- a power supply, an intrusion switch -- it says little,
# and the event text in the last column carries the news.

_SEVERITY = {"ok": "ok", "nc": "warning", "lnc": "warning", "unc": "warning",
             "cr": "critical", "lcr": "critical", "ucr": "critical",
             "nr": "critical", "lnr": "critical", "unr": "critical"}

# Discrete states that are bad news whatever the status column says.
_DISCRETE_CRIT = ("failure detected", "predictive failure", "ac lost",
                  "input lost", "out of range", "config error",
                  "power supply failure")
_DISCRETE_WARN = ("intrusion",)

_RANK = {"unknown": 0, "ok": 1, "warning": 2, "critical": 3}


def _category(name, entity, reading):
    n, r = name.lower(), reading.lower()
    if "degrees" in r:
        return "temperature"
    if r.endswith("rpm") or n.startswith("fan"):
        return "fan"
    if entity.split(".")[0] == "10" or re.match(r"^(ps|psu)\s?\d|power supply", n):
        return "power"
    if r.endswith("volts"):
        return "voltage"
    return "other"


def parse_sdr(text):
    """`ipmitool sdr elist` -> [sensor]. Lines that are not sensors are
    skipped, not guessed at."""
    out = []
    for line in text.splitlines():
        cols = [x.strip() for x in line.split("|")]
        if len(cols) != 5 or not cols[0]:
            continue
        name, _sid, status, entity, reading = cols
        status = status.lower()
        if status == "ns" or reading.lower() in ("no reading", "disabled", ""):
            # An unpopulated header -- FAN5 on a board with four fans -- or a
            # sensor that has nothing to say. Kept so it can be counted, but
            # it is not evidence of health.
            sev = None
        else:
            sev = _SEVERITY.get(status, "unknown")
            low = reading.lower()
            if any(k in low for k in _DISCRETE_CRIT):
                sev = "critical"
            elif sev == "ok" and any(k in low for k in _DISCRETE_WARN):
                sev = "warning"
        value = None
        m = re.match(r"^(-?\d+(?:\.\d+)?)\s+(degrees C|RPM|Volts|Watts|Amps)$", reading)
        if m:
            value = float(m.group(1))
        out.append({"name": name, "status": status, "severity": sev,
                    "category": _category(name, entity, reading),
                    "reading": reading, "value": value})
    return out


_CHASSIS_FAULTS = ("Power Overload", "Main Power Fault", "Power Control Fault",
                   "Drive Fault", "Cooling/Fan Fault")


def parse_chassis(text):
    """`ipmitool chassis status` -> {power, faults[]} or None."""
    kv = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()
    if "System Power" not in kv:
        return None
    return {"power": kv["System Power"],
            "faults": [k for k in _CHASSIS_FAULTS if kv.get(k, "").lower() == "true"]}


def summarize_hardware(sensors, chassis):
    """The verdict on the hardware, from what was actually read. Nothing read
    is unknown, never ok."""
    read = [s for s in sensors if s["severity"] is not None]
    if not read and not chassis:
        return {"status": "unknown", "reason": "ipmitool returned no readable sensors"}
    worst = "ok" if read else "unknown"
    problems = []
    for s in read:
        if _RANK[s["severity"]] > _RANK.get(worst, 0):
            worst = s["severity"]
        if s["severity"] in ("warning", "critical"):
            problems.append({"name": s["name"], "severity": s["severity"],
                             "reading": s["reading"]})
    if chassis:
        for f in chassis["faults"]:
            worst = "critical"
            problems.append({"name": f, "severity": "critical", "reading": "asserted"})
        if worst == "unknown":
            worst = "ok"
    counts = {}
    for s in read:
        c = counts.setdefault(s["category"], {"ok": 0, "warning": 0, "critical": 0,
                                              "unknown": 0})
        c[s["severity"]] += 1
    temps = [s["value"] for s in read if s["category"] == "temperature"
             and s["value"] is not None]
    return {"status": worst, "problems": problems, "counts": counts,
            "max_temp_c": max(temps) if temps else None,
            "no_reading": sum(1 for s in sensors if s["severity"] is None),
            "power": chassis["power"] if chassis else None,
            "sensors": [{k: s[k] for k in ("name", "category", "severity", "reading")}
                        for s in read]}


# What ipmitool prints when the BMC turned the login down, as opposed to not
# answering. "Unable to establish IPMI v2 / RMCP+ session" on its own is
# either, so it is not on this list and lands as a plain failure.
_IPMI_AUTH = ("unauthorized name", "hmac is invalid", "invalid authentication",
              "privilege level exceeds", "insufficient privilege")


def _ipmitool(c, args, timeout):
    tool = c.get("ipmitool_path") or shutil.which("ipmitool")
    if not tool or not os.path.exists(tool):
        raise Outcome(ERROR, "ipmitool is not installed on this server")
    # -L USER: a session at USER privilege cannot issue a single write, so a
    # mistake here cannot reach the BMC's settings or the power button.
    # -f: the password is read from its file by ipmitool itself; it is never
    # in argv (visible to every user via ps) or in the environment.
    cmd = [tool, "-I", "lanplus", "-H", c["address"], "-U", c["username"],
           "-f", c["password_file"], "-L", "USER",
           "-N", str(int(min(timeout, 10))), "-R", "1"] + args
    try:
        p = subprocess.run(cmd, timeout=timeout, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Outcome(FAIL, "ipmitool %s timed out after %gs" % (args[0], timeout))
    except OSError as e:
        raise Outcome(ERROR, "cannot run ipmitool: %s" % e)
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0:
        first = err.splitlines()[0][:160] if err else "exit %d" % p.returncode
        if any(k in err.lower() for k in _IPMI_AUTH):
            raise Outcome(AUTH, "BMC refused the login: %s" % first)
        raise Outcome(FAIL, "ipmitool %s: %s" % (args[0], first))
    return p.stdout.decode("utf-8", "replace")


def ipmi_hardware(c, memo, now):
    """Sensor health on its own slower clock. Returns the hardware dict.

    Credentials missing is not an error, it is the configured state: the
    sensors are unknown and say so. A rejected login is latched like every
    other credential here -- BMCs lock accounts out.
    """
    if not (c.get("username") and c.get("password_file")):
        return {"status": "unknown",
                "reason": "no credentials configured -- reachability only"}
    every = _clamp(c.get("sensor_interval_seconds"), 60, 3600, 300)
    cached = memo.get("hw")
    if cached and now - cached["checked_at"] < every:
        return cached
    try:
        secret = _read_secret(c["password_file"], "password_file")
        if _latched(memo, secret):
            raise Outcome(AUTH, "BMC login was rejected earlier -- not retried "
                                "until password_file changes")
        t = _clamp(c.get("ipmitool_timeout_seconds"), 5, 120, 30)
        try:
            sdr = _ipmitool(c, ["sdr", "elist"], t)
            chassis = parse_chassis(_ipmitool(c, ["chassis", "status"], t))
        except Outcome as o:
            if o.outcome == AUTH:
                memo["rejected"] = _fingerprint(secret)
            raise
        hw = summarize_hardware(parse_sdr(sdr), chassis)
    except Outcome as o:
        hw = {"status": "unknown", "reason": o.detail, "error": o.outcome}
    hw["checked_at"] = int(now)
    memo["hw"] = hw
    return hw


def check_ipmi(c, memo, now=None):
    now = now or time.time()
    t = c["timeout_seconds"]
    answered, ping_detail, facts = ipmi_ping(c["address"], t)
    reach = {"ipmi": ping_detail}

    web_ok = None
    if c.get("https", True):
        try:
            r = http_request(c, "GET", "https://%s/" % c["address"])
            web_ok = r.status < 500
            reach["https"] = "web interface answers (HTTP %d)" % r.status
        except Outcome as o:
            web_ok = False
            reach["https"] = o.detail
            # A certificate that no longer matches its pin is the one HTTPS
            # result that outranks a working IPMI port: something other than
            # the BMC we know may be answering at its address.
            if o.outcome == CONFIG and "pinned" in o.detail:
                raise Outcome(CONFIG, o.detail, {"reach": reach})

    if not answered and not web_ok:
        raise Outcome(FAIL, "BMC not answering: %s; %s" % (
            ping_detail, reach.get("https", "https not checked")), {"reach": reach})

    facts["reach"] = reach
    facts["hardware"] = ipmi_hardware(c, memo, now)
    via = " and ".join(k for k, ok in (("IPMI", answered), ("HTTPS", web_ok)) if ok)
    return OK, "BMC answers (%s)" % via, facts


KINDS = {"home_assistant": check_home_assistant, "plex": check_plex,
         "transmission": check_transmission, "ipmi": check_ipmi}


def what(c):
    """What each check proves, in the words the card uses. Kept beside the
    checks so the claim and the code that earns it are read together."""
    k = c["kind"]
    if k == "home_assistant":
        return ("core API state (/api/config) -- not proof that automations run"
                if c.get("token_file") else
                "web frontend answers (/manifest.json, no token) -- not proof "
                "that the core or automations are running")
    if k == "plex":
        return "media server answers /identity -- not proof that libraries or playback work"
    if k == "transmission":
        return "daemon answers RPC (session-stats) -- not proof that downloads progress"
    if k == "ipmi":
        return ("management controller answers (IPMI 623/udp, HTTPS)"
                + ("; sensors via IPMI" if c.get("username") else
                   "; sensors not read -- no credentials"))
    return k


# ---------- running and folding ----------

def run(c, memo, now=None):
    """One attempt. Returns (outcome, detail, facts, latency_ms). Never raises."""
    now = now or time.time()
    if c.get("config_error"):
        return CONFIG, c["config_error"], {}, None
    t0 = time.monotonic()
    try:
        fn = KINDS[c["kind"]]
        res = fn(c, memo, now) if c["kind"] == "ipmi" else fn(c, memo)
        outcome, detail, facts = res
    except Outcome as o:
        outcome, detail, facts = o.outcome, o.detail, o.facts
    except Exception as e:  # a bug here must not kill the worker thread
        outcome, detail, facts = ERROR, "check crashed: %s" % type(e).__name__, {}
    return outcome, detail, facts, int((time.monotonic() - t0) * 1000)


def fold(prev, c, outcome, detail, facts, latency_ms, now):
    """The new live entry for a check, from the previous one and one attempt.

    A single failure is not an outage: it is held as "unknown" until it
    repeats failures_before_fail times, exactly as the reachability prober
    holds a single dropped ping. The count is reported either way. Only FAIL
    is held -- AUTH and CONFIG do not fix themselves on a retry, and showing
    them at once is how they get fixed.
    """
    prev = prev or {}
    fails = (prev.get("failures", 0) + 1) if outcome == FAIL else 0
    need = c["failures_before_fail"]
    if outcome == FAIL and fails < need:
        state = "unknown"
        detail = "%s (%d of %d failed checks)" % (detail, fails, need)
    else:
        state = outcome
    return {"state": state, "outcome": outcome, "detail": detail, "facts": facts,
            "failures": fails, "latency_ms": latency_ms, "checked_at": int(now),
            "since": (prev.get("since") if prev.get("state") == state
                      else int(now))}


# Severity in the words app.py's _RANK already uses, so the header can count
# service problems with the same rules as everything else.
SEVERITY = {OK: "ok", FAIL: "critical", AUTH: "warning", CONFIG: "warning",
            ERROR: "warning", "stale": "warning", "unknown": "unknown",
            "pending": "unknown", "disabled": "unknown"}


def view(c, live, now):
    """One check as the API shows it: configuration, last verdict, and how
    old that verdict is. Staleness is judged here, at read time, so a wedged
    worker shows up as stale rather than as its last good answer for ever."""
    out = public(c)
    out["checks"] = what(c)
    if not c["enabled"]:
        out.update(state="disabled", severity="unknown",
                   detail="disabled in server.json")
        return out
    if c.get("config_error") and not live:
        out.update(state=CONFIG, severity="warning", detail=c["config_error"])
        return out
    if not live:
        out.update(state="pending", severity="unknown", detail="not checked yet")
        return out
    age = int(now - live["checked_at"])
    out.update({k: live[k] for k in ("state", "outcome", "detail", "failures",
                                     "latency_ms", "checked_at", "since")})
    out["age_seconds"] = age
    out["facts"] = {k: v for k, v in (live.get("facts") or {}).items()
                    if k != "hardware"}
    hw = (live.get("facts") or {}).get("hardware")
    if hw is not None:
        out["hardware"] = hw
    if age > c["stale_after_seconds"]:
        out["state"] = "stale"
        out["detail"] = "no result for %ds -- last: %s" % (age, live["detail"])
    sev = SEVERITY.get(out["state"], "unknown")
    # Hardware is the one thing that can make a reachable BMC red: a reachable
    # controller reporting a failed power supply is not a healthy machine.
    if hw and out["state"] == OK and hw.get("status") in ("warning", "critical"):
        sev = hw["status"]
    # Sensors that were configured and could not be read -- a refused login,
    # no ipmitool -- are a setup problem worth amber. Sensors nobody gave
    # credentials for are reachability-only by choice, and stay as they are.
    elif hw and out["state"] == OK and hw.get("error"):
        sev = "warning"
    out["severity"] = sev
    return out
