"""UPS monitoring over the Network UPS Tools protocol. Read-only, unauthenticated.

A UPS is not a host. It has no CPU, memory, disk, OS, patches or end of life,
and giving it a host card would mean either inventing those or leaving a card
that is two thirds blank and one third misleading. So it has its own card, its
own history and its own events, and nothing here pretends otherwise.

What this module will send, ever, is one line per poll:

    LIST VAR <upsname>

That is the whole vocabulary. upsd answers it without a login, and it changes
nothing. Every byte that leaves this module goes through _send(), which checks
the line against ALLOWED and refuses anything else -- so INSTCMD, SET VAR, FSD,
LOGIN, USERNAME, PASSWORD and PRIMARY cannot be sent by a bug, a config value
or a UPS name with a newline in it. Nothing here takes or stores a credential:
a device configured with one is refused rather than quietly ignored, because a
setting that does nothing reads as a setting that logs in.

The reply is untrusted input from the network:

    BEGIN LIST VAR ups
    VAR ups battery.charge "100"
    VAR ups ups.status "OL CHRG"
    END LIST VAR ups

or a single "ERR <code>". Values are quoted and backslash-escaped, so they are
parsed, not split on spaces. The envelope, the UPS name on every line and the
variable names are all checked; a reply that is cut short, oversized, slow,
names another UPS, or holds a line that is none of the above is rejected
whole rather than half-believed. Reads are bounded in time (one deadline for
the whole exchange, not per recv), in total bytes and in line length.

ups.status is a set of tokens, not a string -- "OL", "OB LB", "OL CHRG RB" --
and every token is kept: a token this module does not know is shown as itself
and never dropped, because the one flag nobody anticipated is the one most
worth seeing.

Two axes are kept apart throughout. Power is what the UPS says about the
mains and its battery. Communication is whether we could ask. A UPS we cannot
reach is not a UPS on battery, and it is not a healthy one either: it is a
UPS we know nothing current about, and reads as exactly that.
"""

import math
import re
import socket
import time

DEFAULT_PORT = 3493

# A full LIST VAR from a usbhid-ups APC is ~60 lines and ~2.5 KB. These are
# generous by an order of magnitude and still small enough that a hostile or
# broken peer cannot make the poller hold much.
MAX_RESPONSE = 64 * 1024
MAX_LINE = 1024
MAX_VARS = 512

# upsd's own rules for a UPS name are looser than this, but every real one is
# letters, digits and a little punctuation, and this is what keeps a newline
# or a space out of the one line we send.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_VAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_HOST = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")

# The allowlist. Anything that does not match is never written to the socket.
ALLOWED = (re.compile(r"\ALIST VAR [A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z"),)

# Error kinds, for the card and for tests. NETWORK: nothing answered properly
# at the TCP level. PROTOCOL: something answered, but not with a NUT reply we
# can trust. DRIVER: upsd answered and says it has no current data from the
# UPS -- the USB cable, the driver, or the UPS itself. CONFIG: upsd answered
# and refused the question as asked: wrong UPS name, access rules.
NETWORK, PROTOCOL, DRIVER, CONFIG = "network", "protocol", "driver", "config"

_DRIVER_ERRS = ("DATA-STALE", "DRIVER-NOT-CONNECTED")


class NutError(Exception):
    def __init__(self, kind, detail, code=None):
        Exception.__init__(self, detail)
        self.kind = kind
        self.detail = detail
        self.code = code


def command_allowed(line):
    return isinstance(line, str) and any(p.match(line) for p in ALLOWED)


def _send(sock, line):
    if not command_allowed(line):
        raise ValueError("refusing to send %r to upsd: not an allowlisted read-only query"
                         % line[:60])
    sock.sendall((line + "\n").encode("ascii"))


# ---------- the wire ----------

class _Lines:
    """Lines off a socket, under one deadline and one byte budget."""

    def __init__(self, sock, deadline, max_bytes):
        self.sock, self.deadline, self.max_bytes = sock, deadline, max_bytes
        self.buf = b""
        self.total = 0
        self.eof = False

    def next(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                raw, self.buf = self.buf[:i], self.buf[i + 1:]
                return _decode(raw)
            if len(self.buf) > MAX_LINE:
                raise NutError(PROTOCOL, "reply line longer than %d bytes" % MAX_LINE)
            if self.eof:
                # A last line with no newline is a cut-off reply, not a line.
                return None
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout()
            self.sock.settimeout(left)
            chunk = self.sock.recv(4096)
            if not chunk:
                self.eof = True
                continue
            self.total += len(chunk)
            if self.total > self.max_bytes:
                raise NutError(PROTOCOL, "reply larger than %d bytes" % self.max_bytes)
            self.buf += chunk


def _decode(raw):
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if len(raw) > MAX_LINE:
        raise NutError(PROTOCOL, "reply line longer than %d bytes" % MAX_LINE)
    text = raw.decode("utf-8", "replace")
    if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in text):
        raise NutError(PROTOCOL, "control characters in reply")
    return text


def parse_var_line(line, ups):
    """'VAR ups battery.charge "100"' -> ("battery.charge", "100").

    Raises NutError(PROTOCOL) for anything that is not exactly that: another
    UPS's name, a bad variable name, an unquoted or unterminated value, or
    anything after the closing quote.
    """
    if not line.startswith("VAR "):
        raise NutError(PROTOCOL, "unexpected line in reply: %r" % line[:60])
    rest = line[4:]
    sp = rest.find(" ")
    if sp < 0:
        raise NutError(PROTOCOL, "truncated VAR line: %r" % line[:60])
    who, rest = rest[:sp], rest[sp + 1:]
    if who != ups:
        raise NutError(PROTOCOL, "reply carries a variable for UPS %r, not %r"
                       % (who[:40], ups))
    sp = rest.find(" ")
    if sp < 0:
        raise NutError(PROTOCOL, "VAR line with no value: %r" % line[:60])
    name, rest = rest[:sp], rest[sp + 1:]
    if not _VAR.match(name):
        raise NutError(PROTOCOL, "bad variable name %r" % name[:40])
    if not rest.startswith('"'):
        raise NutError(PROTOCOL, "unquoted value for %s" % name)
    out, i, closed = [], 1, False
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            i += 1
            if i >= len(rest):
                break
            out.append(rest[i])
        elif ch == '"':
            closed = True
            break
        else:
            out.append(ch)
        i += 1
    if not closed:
        raise NutError(PROTOCOL, "unterminated value for %s" % name)
    if rest[i + 1:].strip():
        raise NutError(PROTOCOL, "trailing text after the value of %s" % name)
    return name, "".join(out)


def _err(line, where):
    parts = line.split()
    code = parts[1] if len(parts) > 1 else "?"
    code = re.sub(r"[^A-Z0-9-]", "", code.upper())[:40] or "?"
    if code in _DRIVER_ERRS:
        return NutError(DRIVER, "upsd at %s has no current data from the UPS (ERR %s) -- "
                                "check the USB cable, the driver, and the UPS" % (where, code),
                        code)
    if code == "UNKNOWN-UPS":
        return NutError(CONFIG, "upsd at %s does not know this UPS name (ERR UNKNOWN-UPS)"
                        % where, code)
    if code == "ACCESS-DENIED":
        return NutError(CONFIG, "upsd at %s refused the read (ERR ACCESS-DENIED) -- "
                                "netdash does not log in; allow LIST VAR from this host"
                        % where, code)
    return NutError(CONFIG if code.startswith(("INVALID", "UNKNOWN")) else PROTOCOL,
                    "upsd at %s answered ERR %s" % (where, code), code)


def query(host, port, ups, timeout=5.0, max_bytes=MAX_RESPONSE):
    """Every variable upsd has for one UPS, as {name: value-string}.

    Raises NutError for anything short of a complete, well-formed reply about
    exactly this UPS. Never returns a partial dict.
    """
    if not isinstance(ups, str) or not _NAME.match(ups):
        raise NutError(CONFIG, "bad UPS name %r" % (ups,))
    where = "%s:%s" % (host, port)
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except socket.gaierror:
        raise NutError(NETWORK, "cannot resolve %s" % host)
    except (socket.timeout, TimeoutError):
        raise NutError(NETWORK, "no connection to upsd at %s within %gs" % (where, timeout))
    except ConnectionRefusedError:
        raise NutError(NETWORK, "connection refused by %s -- upsd not running or not "
                                "listening there" % where)
    except OSError as e:
        raise NutError(NETWORK, "cannot connect to %s: %s" % (where, e.strerror or e))
    try:
        _send(sock, "LIST VAR " + ups)
        lines = _Lines(sock, deadline, max_bytes)
        first = lines.next()
        if first is None:
            raise NutError(NETWORK, "upsd at %s closed the connection without answering"
                           % where)
        if first.startswith("ERR"):
            raise _err(first, where)
        if first != "BEGIN LIST VAR " + ups:
            raise NutError(PROTOCOL, "reply from %s does not open with BEGIN LIST VAR %s: %r"
                           % (where, ups, first[:60]))
        out = {}
        while True:
            line = lines.next()
            if line is None:
                raise NutError(PROTOCOL, "reply from %s cut off after %d variables, before "
                                         "END LIST VAR" % (where, len(out)))
            if line == "END LIST VAR " + ups:
                return out
            if line.startswith("ERR"):
                raise _err(line, where)
            name, value = parse_var_line(line, ups)
            if name in out:
                raise NutError(PROTOCOL, "variable %s listed twice" % name)
            if len(out) >= MAX_VARS:
                raise NutError(PROTOCOL, "more than %d variables" % MAX_VARS)
            out[name] = value
    except (socket.timeout, TimeoutError):
        raise NutError(NETWORK, "no complete reply from upsd at %s within %gs"
                       % (where, timeout))
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        raise NutError(NETWORK, "upsd at %s dropped the connection mid-reply" % where)
    except OSError as e:
        if isinstance(e, NutError):
            raise
        raise NutError(NETWORK, "reading from %s: %s" % (where, e.strerror or e))
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------- configuration ----------

THRESHOLDS = {
    # Percent of charge left: below warn is amber, below crit red.
    "charge": {"warn": 50, "crit": 20},
    # The UPS's own runtime estimate, in seconds, read the same way.
    "runtime_seconds": {"warn": 600, "crit": 300},
    # Percent of rated load: above warn amber, above crit red. OVER is the
    # UPS's own word on overload and is red regardless.
    "load": {"warn": 80, "crit": 95},
}

_CREDENTIALS = ("username", "password", "password_file", "login", "user", "secret")


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return max(lo, min(hi, v))


def _merge_thresholds(*blocks):
    out = {k: dict(v) for k, v in THRESHOLDS.items()}
    for b in blocks:
        if not isinstance(b, dict):
            continue
        for metric, levels in b.items():
            if metric in out and isinstance(levels, dict):
                for lvl in ("warn", "crit"):
                    if isinstance(levels.get(lvl), (int, float)) \
                            and not isinstance(levels.get(lvl), bool):
                        out[metric][lvl] = levels[lvl]
    return out


def parse_config(block):
    """The `ups` block -> a list of normalised devices. Never raises.

    A malformed entry is still returned, carrying `config_error`, so it shows
    as a UPS that cannot be read rather than vanishing.
    """
    if not isinstance(block, dict):
        return []
    out, seen = [], set()
    for i, raw in enumerate(block.get("devices") or []):
        if not isinstance(raw, dict):
            continue
        d = {}
        d["host"] = raw.get("host") if isinstance(raw.get("host"), str) else ""
        d["ups"] = raw.get("ups") if isinstance(raw.get("ups"), str) else "ups"
        d["port"] = raw.get("port", DEFAULT_PORT)
        d["id"] = str(raw.get("id") or "%s-%s" % (d["ups"], d["host"] or i))
        d["name"] = str(raw.get("name") or "%s@%s" % (d["ups"], d["host"]))
        d["attached_to"] = (raw.get("attached_to")
                            if isinstance(raw.get("attached_to"), str) else None)
        d["enabled"] = raw.get("enabled") is True

        def pick(k):
            return raw.get(k) if raw.get(k) is not None else block.get(k)
        d["poll_seconds"] = _clamp(pick("poll_seconds"), 10, 3600, 30)
        d["timeout_seconds"] = _clamp(pick("timeout_seconds"), 1, 30, 5)
        d["failures_before_lost"] = int(_clamp(pick("failures_before_lost"), 1, 10, 2))
        d["stale_after_seconds"] = max(
            d["poll_seconds"] + d["timeout_seconds"],
            _clamp(pick("stale_after_seconds"), 30, 86400, 3 * d["poll_seconds"]))
        d["thresholds"] = _merge_thresholds(block.get("thresholds"), raw.get("thresholds"))
        d["config_error"] = _validate(d, raw, seen)
        seen.add(d["id"])
        out.append(d)
    return out


def _validate(d, raw, seen):
    if not _NAME.match(d["id"]):
        return "id %r: letters, digits, '.', '_' and '-' only" % d["id"]
    if d["id"] in seen:
        return "duplicate id %r" % d["id"]
    for k in _CREDENTIALS:
        if raw.get(k):
            return ("netdash reads UPS data without logging in and never stores a "
                    "upsd credential; remove %s" % k)
    if not d["host"] or not _HOST.match(d["host"]):
        return "needs a host (upsd's address)"
    if not _NAME.match(d["ups"]):
        return "ups name %r: letters, digits, '.', '_' and '-' only" % d["ups"]
    try:
        p = int(d["port"])
    except (TypeError, ValueError):
        return "port must be 1-65535"
    if isinstance(d["port"], bool) or not 1 <= p <= 65535:
        return "port must be 1-65535"
    d["port"] = p
    return None


def public(d):
    return {"id": d["id"], "name": d["name"], "attached_to": d["attached_to"],
            "endpoint": "%s@%s:%s" % (d["ups"], d["host"], d["port"]),
            "enabled": d["enabled"], "poll_seconds": d["poll_seconds"],
            "stale_after_seconds": d["stale_after_seconds"],
            "thresholds": d["thresholds"]}


# ---------- interpretation ----------

# Every ups.status token NUT documents, with what it means here. The severity
# is what the token says about protection on its own; None is information.
FLAGS = {
    "OL": ("on utility power", None),
    "OB": ("on battery", "warning"),
    "LB": ("battery low", "critical"),
    "HB": ("battery high", None),
    "RB": ("battery needs replacing", "warning"),
    "CHRG": ("charging", None),
    "DISCHRG": ("discharging", None),
    "BYPASS": ("on bypass — load not protected", "warning"),
    "CAL": ("runtime calibration running", None),
    "OFF": ("output off — load not powered by the UPS", "critical"),
    "OVER": ("overloaded", "critical"),
    "TRIM": ("trimming high input voltage", None),
    "BOOST": ("boosting low input voltage", None),
    "FSD": ("forced shutdown in progress", "critical"),
    "ALARM": ("alarm raised — see ups.alarm", "warning"),
    "TEST": ("self-test running", None),
}

# Numbers read from the reply: variable, key in the reading, plausible range.
# A value outside it is reported as implausible and shown as missing -- never
# clamped into range, and never zero.
NUMBERS = (
    ("battery.charge", "charge_pct", 0, 100),
    ("battery.runtime", "runtime_seconds", 0, 7 * 86400),
    ("ups.load", "load_pct", 0, 300),
    ("input.voltage", "input_voltage", 0, 500),
    ("output.voltage", "output_voltage", 0, 500),
    ("input.frequency", "input_frequency", 0, 100),
    ("battery.voltage", "battery_voltage", 0, 500),
    ("battery.voltage.nominal", "battery_voltage_nominal", 0, 500),
    ("ups.temperature", "temperature_c", -40, 120),
    ("battery.temperature", "battery_temperature_c", -40, 120),
    # Measured output power, where the UPS measures it. Many do not; the RS
    # 1350MS reports only the rating below.
    ("ups.realpower", "realpower_w", 0, 100000),
    ("ups.power", "apparent_power_va", 0, 100000),
    # Ratings, not measurements.
    ("ups.realpower.nominal", "realpower_nominal_w", 0, 100000),
    ("ups.power.nominal", "power_nominal_va", 0, 100000),
    ("battery.charge.low", "charge_low_pct", 0, 100),
    ("battery.runtime.low", "runtime_low_seconds", 0, 86400),
)

TEXTS = (
    ("model", ("ups.model", "device.model")),
    ("manufacturer", ("ups.mfr", "device.mfr")),
    ("serial", ("ups.serial", "device.serial")),
    ("firmware", ("ups.firmware",)),
    ("firmware_aux", ("ups.firmware.aux",)),
    ("driver", ("driver.name",)),
    ("driver_version", ("driver.version",)),
    ("beeper", ("ups.beeper.status",)),
)

_RANK = {"unknown": 0, "ok": 1, "warning": 2, "critical": 3}
TEXT_MAX = 200


def _text(v):
    return None if v is None else str(v)[:TEXT_MAX]


def _number(s):
    try:
        f = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def status_flags(raw):
    """'OL CHRG RB' -> [{'flag','meaning','severity','known'}], order kept,
    duplicates dropped, unknown tokens kept as themselves."""
    out, seen = [], set()
    for tok in (raw or "").split():
        tok = tok[:32]
        if tok in seen:
            continue
        seen.add(tok)
        k = FLAGS.get(tok)
        out.append({"flag": tok, "known": bool(k),
                    "meaning": k[0] if k else "not a status NUT documents",
                    "severity": k[1] if k else "unknown"})
    return out


def power_source(tokens):
    """What is feeding the load, from the status tokens. 'unknown' when the
    tokens do not say -- never a guess at 'utility'."""
    t = set(tokens)
    if "OFF" in t:
        return "off"
    if "OB" in t:
        return "battery"
    if "BYPASS" in t:
        return "bypass"
    if "OL" in t:
        return "utility"
    return "unknown"


def _level_below(v, th):
    if v is None:
        return None
    if v < th["crit"]:
        return "critical"
    if v < th["warn"]:
        return "warning"
    return None


def _level_above(v, th):
    if v is None:
        return None
    if v >= th["crit"]:
        return "critical"
    if v >= th["warn"]:
        return "warning"
    return None


def fmt_runtime(s):
    if s is None:
        return None
    s = int(s)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % round(s / 60.0)
    return "%.1f h" % (s / 3600.0)


def interpret(v, thresholds=None):
    """A LIST VAR dict -> the reading the API shows. Pure; never raises.

    Alarms come in two sorts, told apart by `source`: "device" for what the UPS
    itself reports (a status flag, ups.alarm, a token nobody documents) and
    "threshold" for our own lines on charge, runtime and load. Only device
    alarms become events: an estimate that wanders across 600 s every few
    polls is a trend for the chart, not news for the log.
    """
    th = thresholds or THRESHOLDS
    raw_status = v.get("ups.status")
    flags = status_flags(raw_status)
    tokens = [f["flag"] for f in flags]
    power = power_source(tokens)

    r = {"power": power, "status_raw": _text(raw_status), "flags": flags}
    invalid = []
    for var, key, lo, hi in NUMBERS:
        if var not in v:
            r[key] = None
            continue
        n = _number(v[var])
        if n is None or not lo <= n <= hi:
            invalid.append("%s=%r" % (var, _text(v[var])[:40]))
            n = None
        r[key] = n
    if r["runtime_seconds"] is not None:
        r["runtime_seconds"] = int(r["runtime_seconds"])
    for key, names in TEXTS:
        r[key] = next((_text(v[n]) for n in names if v.get(n) not in (None, "")), None)

    # Battery health: what the device says, labelled as the device's word. A
    # battery.date is often the factory default and never a verified
    # replacement date (an RS 1350MS bought this decade reports 2001/09/25).
    r["battery"] = {
        "replace_flag": "RB" in tokens,
        "type": _text(v.get("battery.type")),
        "reported_date": _text(v.get("battery.date")),
        "reported_mfr_date": _text(v.get("battery.mfr.date")),
        "test_result": _text(v.get("ups.test.result")),
    }
    r["alarm_text"] = _text(v.get("ups.alarm"))
    r["invalid"] = invalid

    alarms = []
    for f in flags:
        if f["severity"] in ("warning", "critical"):
            alarms.append({"code": f["flag"], "severity": f["severity"],
                           "text": f["meaning"], "source": "device"})
        elif not f["known"]:
            alarms.append({"code": f["flag"], "severity": "unknown",
                           "text": "unrecognised status flag %s" % f["flag"],
                           "source": "device"})
    if r["alarm_text"]:
        alarms.append({"code": "ups.alarm", "severity": "warning",
                       "text": r["alarm_text"], "source": "device"})
    if raw_status is None or not tokens:
        alarms.append({"code": "no-status", "severity": "warning",
                       "text": "UPS reported no ups.status — power state unknown",
                       "source": "device"})

    for key, metric, below, what in (
            ("charge_pct", "charge", True, "battery charge %.0f%%"),
            ("runtime_seconds", "runtime_seconds", True, "estimated runtime %s"),
            ("load_pct", "load", False, "load %.0f%%")):
        val = r[key]
        sev = (_level_below if below else _level_above)(val, th[metric])
        if sev:
            txt = (what % fmt_runtime(val)) if key == "runtime_seconds" else (what % val)
            alarms.append({"code": metric, "severity": sev, "text": txt,
                           "source": "threshold"})
    if invalid:
        alarms.append({"code": "invalid", "severity": "unknown",
                       "text": "implausible or unreadable: " + ", ".join(invalid[:5]),
                       "source": "threshold"})
    r["alarms"] = alarms
    worst = "ok"
    for a in alarms:
        if _RANK[a["severity"]] > _RANK[worst]:
            worst = a["severity"]
    r["severity"] = worst
    return r


# ---------- live state ----------

def fold(prev, d, result, now, vars_=None):
    """The new live entry for a device, from the previous one and one poll.

    `result` is either an interpreted reading (success) or a NutError. A
    failure keeps the last good reading and its time -- it is the last thing
    we know -- and never replaces it with nothing or with zeros.
    """
    prev = dict(prev or {})
    if isinstance(result, NutError):
        prev.update(failures=prev.get("failures", 0) + 1, error=result.detail,
                    error_kind=result.kind, error_code=result.code,
                    attempted_at=int(now))
        return prev
    return {"reading": result, "vars": vars_ or {}, "read_at": int(now),
            "attempted_at": int(now), "failures": 0, "error": None,
            "error_kind": None, "error_code": None,
            "previous_read_at": prev.get("read_at"),
            "power_since": (prev.get("power_since")
                            if prev.get("reading") and prev["reading"]["power"] == result["power"]
                            and prev.get("power_since") else int(now))}


def comm_state(d, live, now):
    """('pending'|'ok'|'failing'|'lost'|'stale', detail)."""
    if not live:
        return "pending", "not read yet"
    need = 1 if live.get("error_kind") == CONFIG else d["failures_before_lost"]
    f = live.get("failures", 0)
    if f >= need:
        return "lost", live.get("error") or "no reply"
    age = now - live["read_at"] if live.get("read_at") else None
    if age is None:
        return ("failing", "%s (%d of %d failed polls)" % (live.get("error"), f, need))
    if age > d["stale_after_seconds"]:
        return "stale", ("no reading for %ds%s" % (
            age, " — last error: " + live["error"] if live.get("error") else ""))
    if f:
        return "failing", "%s (%d of %d failed polls)" % (live.get("error"), f, need)
    return "ok", "reading normally"


def view(d, live, now, full=False):
    """One UPS as the API shows it. Freshness is judged here, at read time, so
    a wedged poller reads stale rather than showing its last answer for ever.

    When the reading is not fresh, `power` is "unknown" and `status` is
    "stale": the last reading is still returned, under `reading`, with its
    age, for what it is -- the last thing known, not the state now.
    """
    out = public(d)
    out["config_error"] = d.get("config_error")
    if not d["enabled"]:
        out.update(status="unknown", fresh=False, power="unknown", reading=None,
                   comm={"state": "disabled", "detail": "disabled in server.json"})
        return out
    if d.get("config_error"):
        out.update(status="warning", fresh=False, power="unknown", reading=None,
                   comm={"state": "config", "detail": d["config_error"]})
        return out
    state, detail = comm_state(d, live, now)
    live = live or {}
    reading = live.get("reading")
    fresh = bool(reading) and state in ("ok", "failing")
    out["comm"] = {
        "state": state, "detail": detail,
        "failures": live.get("failures", 0),
        "error": live.get("error"), "error_kind": live.get("error_kind"),
        "error_code": live.get("error_code"),
        "last_attempt": live.get("attempted_at"),
        "last_success": live.get("read_at"),
        "age_seconds": int(now - live["read_at"]) if live.get("read_at") else None,
    }
    out["fresh"] = fresh
    if reading:
        rd = dict(reading)
        rd["ts"] = live["read_at"]
        rd["age_seconds"] = int(now - live["read_at"])
        out["reading"] = rd
    else:
        out["reading"] = None
    out["power"] = reading["power"] if fresh else "unknown"
    out["power_since"] = live.get("power_since") if fresh else None
    if fresh:
        out["status"] = reading["severity"]
        if out["status"] == "unknown":
            out["status"] = "ok"
    elif not reading:
        out["status"] = "stale" if state == "lost" else "unknown"
    else:
        # Silence after a critical reading is not a reason to stop shouting:
        # a UPS last seen on battery and low, then gone quiet, has most likely
        # shut down with everything behind it.
        out["status"] = "critical" if reading["severity"] == "critical" else "stale"
    out["runtime_is_estimate"] = True
    if full:
        out["vars"] = dict(live.get("vars") or {})
    return out


# ---------- transitions ----------

POWER_STATES = ("utility", "battery", "bypass", "off")


def _power_words(r):
    bits = []
    if r.get("charge_pct") is not None:
        bits.append("charge %.0f%%" % r["charge_pct"])
    if r.get("runtime_seconds") is not None:
        bits.append("runtime ≈ %s (UPS estimate)" % fmt_runtime(r["runtime_seconds"]))
    if r.get("load_pct") is not None:
        bits.append("load %.0f%%" % r["load_pct"])
    return " · ".join(bits)


_POWER_TEXT = {"utility": "on utility power", "battery": "on battery",
               "bypass": "on bypass", "off": "output off"}


def transitions(recorded, d, live, prev_read_at, now):
    """The events one poll earns, as (kind, from, to, detail) tuples, and
    `recorded` updated to match. Pure, so deduplication is testable.

    `recorded` maps event kind -> last recorded to_state, seeded from the
    event log at start. A kind is recorded only when its state moves. The
    first sighting of a kind with nothing on record is recorded only when it
    is bad -- a UPS on battery when netdash first looks is worth a line, one
    on utility power is not news.

    `prev_read_at` is when the previous good reading was taken, or None. When
    the gap since then is longer than staleness allows, the change happened at
    some unknown point inside it, and the detail says so rather than letting
    the event's timestamp pass for the moment of the outage.
    """
    out = []
    state, detail = comm_state(d, live, now)

    def emit(kind, to, text, bad):
        prev = recorded.get(kind)
        if prev == to:
            return
        if prev is None and not bad:
            recorded[kind] = to
            return
        out.append((kind, prev, to, text))
        recorded[kind] = to

    if state == "lost":
        emit("ups:comm", "lost", detail, True)
        return out
    if state not in ("ok", "failing"):
        return out
    emit("ups:comm", "ok", "reading again", False)

    r = live["reading"]
    gap = None
    if prev_read_at is None:
        gap = "first reading since netdash started; when this began is not known"
    elif now - prev_read_at > d["stale_after_seconds"]:
        gap = ("seen after %s without data; the change happened at an unknown time in "
               "that gap" % _dur(now - prev_read_at))
    if r["power"] in POWER_STATES:
        words = _POWER_TEXT[r["power"]]
        extra = _power_words(r)
        text = words + (" · " + extra if extra else "")
        prev_power = recorded.get("ups:power")
        if gap and prev_power != r["power"]:
            text += " — " + gap
        emit("ups:power", r["power"], text, r["power"] != "utility")

    active = {a["code"]: a for a in r["alarms"] if a["source"] == "device"}
    for kind in [k for k in recorded if k.startswith("ups:alarm:")]:
        code = kind[len("ups:alarm:"):]
        if code not in active and recorded[kind] != "ok":
            emit(kind, "ok", "cleared", False)
    for code, a in sorted(active.items()):
        emit("ups:alarm:" + code, a["severity"], a["text"], True)
    return out


def _dur(s):
    s = int(s)
    if s < 120:
        return "%d s" % s
    if s < 7200:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%.1f h" % (s / 3600.0)
    return "%d days" % (s // 86400)


if __name__ == "__main__":
    # A one-off read for checking an endpoint by hand. Sends the same single
    # LIST VAR the poller does, and nothing else.
    import json
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: nut.py HOST [UPS] [PORT]")
    host = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "ups"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_PORT
    try:
        v = query(host, port, name)
    except NutError as e:
        sys.exit("%s error: %s" % (e.kind, e.detail))
    print(json.dumps({"vars": v, "reading": interpret(v)}, indent=2, ensure_ascii=False))
