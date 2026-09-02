#!/usr/bin/env python3
"""netdash central server -- Python 3 standard library only.

Routes:
  POST /api/ingest        collector push (JSON, X-Netdash-Token header)
  GET  /api/overview      all hosts, latest sample + rolled-up status
  GET  /api/host/<name>   one host: latest + recent history
  GET  /                  dashboard UI
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import eol  # noqa: E402
import truenas  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

CFG = {}
CONN = None


# Defaults for keys added after the first release. deploy.sh never overwrites an
# existing /etc/netdash/server.json -- that is the point of it -- so an upgraded
# server reads a config written before these keys existed, and a bare
# CFG["patch_stale_hours"] would KeyError on every request.
DEFAULTS = {
    # Per-host settings, keyed by the hostname the collector reports. Each entry
    # may carry "thresholds" and/or "stale_after_seconds"; anything absent falls
    # back to the global value.
    "hosts": {},
    # Whether a host's OS is still supported upstream, from endoflife.date.
    # Sends nothing about the fleet: it fetches public product files and matches
    # locally. Set "enabled": false to make no outbound request at all.
    "eol": {"enabled": True, "refresh_hours": 24, "warn_days": 30, "overrides": {}},
    # How old a patch check may be before the dashboard stops believing it.
    # Two days: the checks run daily, so this tolerates one missed run before
    # the badge drops to "unknown".
    "patch_stale_hours": 48,
}


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


# ---------- status logic ----------

def host_cfg(host):
    return (CFG.get("hosts") or {}).get(host) or {}


def thresholds_for(host):
    """Global thresholds with any per-host override merged over the top.

    Merged per metric AND per level, so an override can say just
    {"mem": {"warn": 92}} and still inherit crit from the global block. Making
    it replace the whole metric would mean restating numbers you did not want
    to change, which is how a fleet's thresholds quietly drift apart.
    """
    base = CFG["thresholds"]
    ov = host_cfg(host).get("thresholds") or {}
    if not ov:
        return base
    out = {}
    for metric, levels in base.items():
        merged = dict(levels)
        merged.update(ov.get(metric) or {})
        out[metric] = merged
    return out


def stale_after_for(host):
    ov = host_cfg(host).get("stale_after_seconds")
    return int(ov if ov is not None else CFG["stale_after_seconds"])


def _level(pct, t):
    if pct is None:
        return "unknown"
    if pct >= t["crit"]:
        return "critical"
    if pct >= t["warn"]:
        return "warning"
    return "ok"


_RANK = {"unknown": 0, "ok": 1, "warning": 2, "critical": 3, "stale": 4}


def _vparts(v):
    """'0.3.10' -> (0, 3, 10). Unparseable or missing sorts lowest."""
    if not v:
        return ()
    out = []
    for part in str(v).split("."):
        digits = "".join(c for c in part if c.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def newest_version(hosts):
    """The highest collector version any host is reporting.

    Compared numerically per component, not as a string: "0.3.10" is newer than
    "0.3.9" but sorts before it alphabetically, which would mark the whole fleet
    outdated the first time a minor number reached double digits.

    Using the newest *seen* rather than the server's own version is deliberate:
    the server does not know what has been released, only what its hosts run, and
    upgrading one host is what makes the rest visibly stale.
    """
    best = ()
    for h in hosts:
        p = _vparts(h.get("collector_version"))
        if p > best:
            best = p
    return best


def patch_summary(sample, now):
    """Patch status for one sample.

    Derived here rather than in the collector because staleness is judged
    against patch_stale_hours, which is server configuration -- a collector
    cannot know the threshold it will be measured by.

    The one rule that matters: an old or missing check is "unknown", never
    "ok". "0 pending" from metadata last refreshed months ago is
    indistinguishable from a patched host, and quietly showing it green is the
    worst thing a security indicator can do. Anything uncertain reads as a
    dash. See PATCH-CHECKS.md.
    """
    sec = sample.get("patch_security")
    oth = sample.get("patch_other")
    reboot = sample.get("patch_reboot")
    reboot = None if reboot is None else bool(reboot)
    checked = sample.get("patch_checked_at")
    age = int(now - checked) if checked else None

    if checked is None or age is None or age > CFG["patch_stale_hours"] * 3600:
        status = "unknown"
    elif sec:
        status = "security"
    elif reboot:
        # Nothing left to install, but what was installed is not running yet.
        # A Fedora host reached exactly this state: a clean package database
        # while still booted on the old kernel and the old libbluez, both of
        # which had been on its security list. Green here would be a lie.
        status = "reboot"
    elif oth:
        status = "updates"
    elif sec is None and oth is None:
        # The check ran but the platform could classify nothing at all.
        status = "unknown"
    else:
        status = "ok"

    return {
        "status": status,
        "security": sec,
        "other": oth,
        "reboot_required": reboot,
        "checked_at": checked,
        "age_seconds": age,
        "source": sample.get("patch_source"),
        "detail": sample.get("patch_detail"),
        "packages": sample.get("patch_packages"),
    }


def summarize(sample, now=None):
    """Turn a raw sample row into the shape the UI consumes."""
    now = now or time.time()
    th = thresholds_for(sample["host"])
    age = now - sample["ts"]

    # A collector may legitimately report a metric it could not read -- OpenBSD
    # refuses to expose vm.uvmexp via sysctl, for instance. Every ratio here
    # needs BOTH halves: with only the total, this raised TypeError and the
    # whole /api/overview response 500'd, so one quiet host took the entire
    # dashboard down rather than showing itself as unknown.
    mem_pct = None
    if sample.get("mem_total_bytes") and sample.get("mem_used_bytes") is not None:
        mem_pct = 100.0 * sample["mem_used_bytes"] / sample["mem_total_bytes"]

    disks = []
    worst_disk_pct = None
    for d in sample.get("disks", []):
        pct = None
        if d.get("total_bytes") and d.get("used_bytes") is not None:
            pct = 100.0 * d["used_bytes"] / d["total_bytes"]
            if worst_disk_pct is None or pct > worst_disk_pct:
                worst_disk_pct = pct
        disks.append({
            "mount": d["mount"],
            "used_bytes": d["used_bytes"],
            "total_bytes": d["total_bytes"],
            "pct": pct,
            "status": _level(pct, th["disk"]),
        })

    cpu_status = _level(sample.get("cpu_pct"), th["cpu"])
    mem_status = _level(mem_pct, th["mem"])
    disk_status = _level(worst_disk_pct, th["disk"])

    stale = age > stale_after_for(sample["host"])

    virt_override = host_cfg(sample["host"]).get("virt")
    virt = virt_override if virt_override else sample.get("virt")
    virt_source = ("config" if virt_override
                   else "host" if sample.get("virt") else None)
    # Patch status is deliberately NOT part of this rollup. Resource pressure is
    # live and self-clearing; pending patches sit amber for a week and train you
    # to ignore amber. It gets its own badge, and "needs attention" in the header
    # stays a statement about CPU, memory and disk.
    overall = "stale" if stale else max(
        (cpu_status, mem_status, disk_status), key=lambda s: _RANK[s]
    )

    return {
        "host": sample["host"],
        "os": sample.get("os"),
        "source": sample.get("source", "push"),
        "ts": sample["ts"],
        "age_seconds": int(age),
        "stale": stale,
        "uptime_seconds": sample.get("uptime_seconds"),
        "cpu": {"pct": sample.get("cpu_pct"), "status": cpu_status},
        "mem": {
            "pct": mem_pct,
            "used_bytes": sample.get("mem_used_bytes"),
            "total_bytes": sample.get("mem_total_bytes"),
            "status": mem_status,
        },
        "disk": {"worst_pct": worst_disk_pct, "status": disk_status, "mounts": disks},
        # The effective values, so an override can be confirmed from the API
        # rather than inferred from a card being a colour you did not expect.
        "thresholds": th,
        "patches": patch_summary(sample, now),
        # Derived from the OS string against a cached lookup, so it is computed
        # per request rather than stored per sample -- the answer changes with
        # the calendar, not with anything the host reports.
        "eol": (eol.lookup(sample.get("os"), CFG["eol"])
                if CFG["eol"].get("enabled") else eol._unknown()),
        "collector_version": sample.get("collector_version"),
        # "none", a hypervisor name, or None when nothing can tell.
        #
        # A config entry wins over the collector. Some hosts can never report it
        # -- TrueNAS is polled over its API and runs no collector at all -- and
        # on OpenBSD and NetBSD the detection is a DMI heuristic that an admin
        # may simply know better than. virt_source says which answered, so an
        # override is auditable rather than indistinguishable from a real
        # reading; is_vm stays tri-state either way.
        "virt": virt,
        "virt_source": virt_source,
        "is_vm": (None if not virt else virt != "none"),
        "status": overall,
    }


# ---------- HTTP ----------

# A client that went away. These surface from the response write rather than
# from the work, so they can land in any handler's `except Exception`.
CLIENT_GONE = (ConnectionResetError, BrokenPipeError,
               ConnectionAbortedError, TimeoutError)


class Handler(BaseHTTPRequestHandler):
    server_version = "netdash"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("NETDASH_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _fail(self, code, where, e):
        """Answer an error, and say so in the log.

        log_message is silent unless NETDASH_VERBOSE, and BaseHTTPRequestHandler
        routes log_error through it, so before this every 4xx/5xx these handlers
        returned left no trace at all: a collector posting malformed JSON, or a
        database error, dropped a host off the dashboard with an empty journal.

        A disconnect is re-raised rather than answered -- the socket is already
        gone, so writing to it just produces a second exception. Server's
        handle_error logs it as one line."""
        if isinstance(e, CLIENT_GONE):
            raise
        sys.stderr.write("%s: %s: %s\n" % (where, type(e).__name__, e))
        return self._json(code, {"error": str(e)})

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    # -- POST /api/ingest --
    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/ingest":
            return self._json(404, {"error": "not found"})

        token = CFG.get("ingest_token") or ""
        if token and self.headers.get("X-Netdash-Token") != token:
            return self._json(401, {"error": "bad or missing X-Netdash-Token"})

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 256 * 1024:
                return self._json(400, {"error": "bad content-length"})
            payload = json.loads(self.rfile.read(n))
        except Exception as e:
            return self._fail(400, "ingest: bad json", e)

        if not payload.get("host"):
            return self._json(400, {"error": "missing 'host'"})

        try:
            db.insert_sample(CONN, payload)
        except Exception as e:
            return self._fail(500, "ingest %s" % payload["host"], e)
        return self._json(200, {"ok": True, "host": payload["host"]})

    # -- GET --
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path

        if path == "/api/overview":
            try:
                samples = db.latest_per_host(CONN)
                now = time.time()
                hosts = [summarize(s, now) for s in samples]
                # A host is "behind" only against a version actually seen in this
                # fleet, and only when it reported one at all -- a host that has
                # not upgraded its collector yet reports nothing and must not be
                # confused with one running an old version.
                newest = newest_version(hosts)
                for h in hosts:
                    h["collector_outdated"] = bool(
                        h.get("collector_version") and _vparts(h["collector_version"]) < newest
                    )
                return self._json(200, {
                    "now": int(now),
                    "thresholds": CFG["thresholds"],
                    "collector_newest": ".".join(str(x) for x in newest) or None,
                    "hosts": hosts,
                })
            except Exception as e:
                return self._fail(500, "overview", e)

        if path.startswith("/api/host/"):
            host = path[len("/api/host/"):]
            try:
                mins = int((parse_qs(u.query).get("minutes") or ["60"])[0])
            except ValueError:
                mins = 60
            mins = max(5, min(mins, CFG["retention_hours"] * 60))
            samples = [s for s in db.latest_per_host(CONN) if s["host"] == host]
            if not samples:
                return self._json(404, {"error": "unknown host"})
            hist = db.history(CONN, host, mins * 60)
            for h in hist:
                if h.get("mem_total_bytes") and h.get("mem_used_bytes") is not None:
                    h["mem_pct"] = 100.0 * h["mem_used_bytes"] / h["mem_total_bytes"]
                else:
                    h["mem_pct"] = None
            return self._json(200, {
                "now": int(time.time()),
                "thresholds": CFG["thresholds"],
                "current": summarize(samples[0]),
                "history": hist,
            })

        if path == "/api/health":
            return self._json(200, {"ok": True, "hosts": len(db.known_hosts(CONN))})

        # static
        rel = "index.html" if path == "/" else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)


class Server(ThreadingHTTPServer):
    """A client hanging up mid-request is routine here -- a wall-panel browser
    tab closed, a collector that hit its own timeout and walked away -- and says
    nothing about the server. socketserver's handle_error prints a full
    traceback for each one into a journal nothing rotates, so a flapping
    collector can bury real errors. These collapse to a single line; anything
    else keeps its traceback."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            addr = client_address[0] if isinstance(client_address, tuple) else client_address
            sys.stderr.write("client %s hung up mid-request (%s)\n"
                             % (addr, type(exc).__name__))
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


# ---------- background workers ----------

def eol_refresher():
    """Keep the endoflife.date cache warm, off the request path.

    Release dates move on the scale of months, so this is deliberately lazy:
    once at startup and then on a long timer. A failure keeps the previous
    cache rather than clearing it -- yesterday's copy of Debian's EOL date is
    still correct today.
    """
    cfg = CFG["eol"]
    interval = max(3600, int(cfg.get("refresh_hours") or 24) * 3600)
    while True:
        try:
            os_strings = [s.get("os") for s in db.latest_per_host(CONN)]
            eol.refresh(os_strings, cfg)
        except Exception as e:
            sys.stderr.write("eol refresh error: %s\n" % e)
        time.sleep(interval)


def pruner():
    while True:
        time.sleep(600)
        try:
            db.prune(CONN, CFG["retention_hours"])
        except Exception as e:
            sys.stderr.write("prune error: %s\n" % e)


def truenas_poller():
    tn = CFG.get("truenas") or {}
    interval = int(tn.get("poll_seconds") or 60)
    while True:
        try:
            payload = truenas.collect(tn)
            if payload:
                db.insert_sample(CONN, payload, source="truenas")
        except Exception as e:
            sys.stderr.write("truenas poll error: %s\n" % e)
        time.sleep(interval)


def main():
    global CFG, CONN
    cfg_path = os.environ.get("NETDASH_CONFIG") or os.path.join(HERE, "config.json")
    if not os.path.exists(cfg_path):
        sys.exit("config not found: %s (copy config.example.json)" % cfg_path)
    CFG = load_config(cfg_path)
    CONN = db.connect(CFG["db_path"])
    db.prune(CONN, CFG["retention_hours"])

    # A per-host block keyed by a hostname that never reports does nothing at
    # all, silently, and a typo looks exactly like a host not installed yet.
    # Say which, once, rather than leaving it to be discovered.
    configured = set(CFG.get("hosts") or {})
    if configured:
        unseen = sorted(configured - set(db.known_hosts(CONN)))
        if unseen:
            sys.stderr.write("config: per-host settings for hosts that have "
                             "never reported: %s\n" % ", ".join(unseen))

    threading.Thread(target=pruner, daemon=True).start()
    if CFG["eol"].get("enabled"):
        threading.Thread(target=eol_refresher, daemon=True).start()
        sys.stderr.write("eol lookups enabled (endoflife.date)\n")
    if (CFG.get("truenas") or {}).get("enabled"):
        threading.Thread(target=truenas_poller, daemon=True).start()
        sys.stderr.write("truenas poller started\n")

    srv = Server((CFG["bind_host"], CFG["bind_port"]), Handler)
    sys.stderr.write("netdash listening on %s:%s\n" % (CFG["bind_host"], CFG["bind_port"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
