#!/usr/bin/env python3
"""netdash MCP server -- Python 3 standard library only.

Lets an LLM agent (Claude Code and the like) ask netdash what is wrong and
what changed, rather than a person reading the dashboard to it:

    claude mcp add netdash -- python3 /path/to/mcp/netdash-mcp.py \\
        --url http://netdash.example:8080

It speaks MCP over stdio (newline-delimited JSON-RPC 2.0) and is a client of
the server's HTTP API, nothing more: it runs where the agent runs, usually a
laptop, and never on the netdash server. It has no database, no state, and no
tool that writes. Acknowledging a patch or forgetting a host stays a human
action on the dashboard -- an agent that can silence a warning is one that can
silence the warning you needed.

stdout carries protocol and nothing else; anything for a person goes to stderr.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

NETDASH_VERSION="0.5.1"

# Newest first. A client asking for one of these gets it back; anything else
# gets the newest, which is what the spec asks of a server that cannot match.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
netdash monitors a home network: CPU, memory and disk per host, reachability,
pending security patches, OS end of life, and application checks (Home
Assistant, Plex, a BMC, ...). Start with fleet_status(problems_only=true) to
see what needs attention, then host_detail or events for the why.

Host status is one of ok, warning, critical, stale (expected to report and has
not), down (confirmed not answering), away (configured as allowed to go
quiet -- a laptop asleep; not a problem), stopped (a jail its host says is
stopped) or unknown. Patches and end of life are
deliberately not part of status; services are not either. Acknowledged
patches and EOL warnings were reviewed by a person and are not news.

Everything here is read-only. To acknowledge or forget anything, tell the
user to do it on the dashboard."""


class ToolError(Exception):
    """A failure to report to the agent as the tool's answer, not a crash."""


class NotFound(ToolError):
    """The API answered 404."""


class UnknownTool(Exception):
    """tools/call named a tool we do not have."""


# ---------- the netdash API ----------

class Api:
    def __init__(self, url, timeout):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def get(self, path, **query):
        q = {k: v for k, v in query.items() if v is not None}
        url = self.url + path + ("?" + urlencode(q) if q else "")
        req = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "netdash-mcp/" + NETDASH_VERSION})
        try:
            with urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or e.reason
            except Exception:
                msg = e.reason
            raise (NotFound if e.code == 404 else ToolError)(
                "netdash answered %d for %s: %s" % (e.code, path, msg))
        except (URLError, OSError) as e:
            raise ToolError("cannot reach netdash at %s: %s"
                            % (self.url, getattr(e, "reason", e)))
        try:
            return json.loads(body)
        except ValueError as e:
            raise ToolError("netdash sent something that is not JSON for %s: %s"
                            % (path, e))


# ---------- shaping ----------
#
# The API answers a browser, which wants everything. An agent pays for every
# token it reads, so the shapes below keep what explains a state and drop what
# only draws it: no thresholds blocks repeated per host, no chart points
# unless asked for, and timestamps as local ISO time, because "1788300000" is
# a number the agent then has to convert, and gets wrong.

def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts)).astimezone().isoformat(timespec="seconds")


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _ackable_unreviewed(block):
    return bool(block) and not block.get("acknowledged")


def patches_brief(p):
    if not p:
        return None
    return _drop_none({
        "status": p.get("status"),
        "security": p.get("security"),
        "other": p.get("other"),
        "reboot_required": p.get("reboot_required"),
        "checked": iso(p.get("checked_at")),
        "acknowledged": p.get("acknowledged") or None,
        "detail": p.get("detail") or None,
    })


def eol_brief(e):
    if not e or e.get("status") in (None, "unknown"):
        return None
    return _drop_none({
        "status": e.get("status"),
        "product": e.get("product"),
        "cycle": e.get("cycle"),
        "eol_date": e.get("eol_date"),
        "acknowledged": e.get("acknowledged") or None,
    })


def service_brief(s):
    return _drop_none({
        "name": s.get("name"), "kind": s.get("kind"), "id": s.get("id"),
        "state": s.get("state"), "severity": s.get("severity"),
        "detail": s.get("detail"),
        "since": iso(s.get("since")),
    })


def status_of(h):
    """The status as the dashboard draws it. The API leaves "away" to the UI
    (stale, on a host allowed to go quiet), and an agent that has to rederive
    it will sooner or later report a sleeping laptop as an outage."""
    if h.get("status") == "stale" and not h.get("expect_up", True):
        return "away"
    return h.get("status")


def _pct(v):
    return round(v, 1) if isinstance(v, float) else v


def host_brief(h):
    """One host as a few lines of facts. Disk lists only mounts that are not
    ok -- the worst percentage is already there for the rest. Reachability is
    given only where it is the explanation: a host that is reporting is up,
    and saying so for every host is noise."""
    status = status_of(h)
    reach = h.get("reachability") or {}
    disk = h.get("disk") or {}
    cpu, mem = h.get("cpu") or {}, h.get("mem") or {}
    out = {
        "host": h.get("host"),
        "status": status,
        "os": h.get("os"),
        "last_report": iso(h.get("ts")),
        "age_seconds": h.get("age_seconds"),
        "down_reason": h.get("down_reason"),
        "reachability": (_drop_none({"state": reach.get("state"),
                                     "detail": reach.get("detail")})
                         if status in ("stale", "down", "away") else None),
        "cpu": _drop_none({"pct": _pct(cpu.get("pct")), "status": cpu.get("status"),
                           "brief": cpu.get("brief")}),
        "mem": _drop_none({"pct": _pct(mem.get("pct")), "status": mem.get("status")}),
        "disk": _drop_none({
            "worst_pct": disk.get("worst_pct"), "status": disk.get("status"),
            "mounts_not_ok": [_drop_none({"mount": m.get("mount"), "pct": m.get("pct"),
                                          "status": m.get("status")})
                              for m in disk.get("mounts") or []
                              if m.get("status") not in (None, "ok")],
        }),
        "uptime_seconds": h.get("uptime_seconds"),
        "patches": patches_brief(h.get("patches")),
        "eol": eol_brief(h.get("eol")),
        "services": [service_brief(s) for s in h.get("services") or []
                     if s.get("severity") != "ok" and s.get("state") != "disabled"],
        "services_severity": h.get("services_severity"),
        "virt": h.get("virt"),
        "parent": h.get("parent"),
        "collector_version": h.get("collector_version"),
        "collector_outdated": h.get("collector_outdated") or None,
    }
    return _drop_none(out)


def needs_attention(h):
    """Anything a person would want to hear about. Away and stopped are grey on
    the dashboard for a reason, and anything a person already acknowledged is
    not news either."""
    if status_of(h) in ("warning", "critical", "stale", "down"):
        return True
    p = h.get("patches") or {}
    if p.get("status") == "security" and not p.get("acknowledged"):
        return True
    if p.get("reboot_required"):
        return True
    e = h.get("eol") or {}
    if e.get("status") in ("eol", "eol_soon") and not e.get("acknowledged"):
        return True
    if h.get("services_severity") in ("warning", "critical"):
        return True
    return False


def series_stats(points, key, lo_key=None, hi_key=None):
    """min/avg/max over a window. In hourly rollups `key` is the hour's
    average, so the extremes come from lo_key and hi_key where a point has
    them: a ten-minute spike to 100% is an hour averaging 25%, and the max of
    the averages would report the spike as 25%."""
    vals = []
    for p in points:
        v = p.get(key)
        if v is None:
            continue
        lo, hi = p.get(lo_key) if lo_key else None, p.get(hi_key) if hi_key else None
        vals.append((p["ts"], v, v if lo is None else lo, v if hi is None else hi))
    if not vals:
        return None
    peak = max(vals, key=lambda v: v[3])
    return {
        "min": round(min(v[2] for v in vals), 1),
        "avg": round(sum(v[1] for v in vals) / len(vals), 1),
        "max": round(peak[3], 1),
        "max_at": iso(peak[0]),
        "last": round(vals[-1][1], 1),
        "points": len(vals),
    }


def downsample(points, n):
    if len(points) <= n:
        return points
    step = len(points) / float(n)
    return [points[int(i * step)] for i in range(n - 1)] + [points[-1]]


def event_brief(e):
    return _drop_none({
        "time": iso(e.get("ts")), "host": e.get("host"), "kind": e.get("kind"),
        "from": e.get("from_state"), "to": e.get("to_state"),
        "detail": e.get("detail"),
    })


# ---------- tools ----------

def _int(args, key, default, lo, hi):
    v = args.get(key, default)
    if v is None:
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ToolError("%s must be a whole number" % key)
    return max(lo, min(v, hi))


def _host_names(api):
    return sorted(h["host"] for h in api.get("/api/overview").get("hosts") or [])


def _unknown_host(api, host):
    """The known names go in the error, so an agent that guessed "truenas" for
    "truenas-core" can fix it without another round trip."""
    return ToolError("no host named %r. Known hosts: %s"
                     % (host, ", ".join(_host_names(api))))


def tool_fleet_status(api, args):
    ov = api.get("/api/overview")
    hosts = ov.get("hosts") or []
    counts = {}
    for h in hosts:
        s = status_of(h)
        counts[s] = counts.get(s, 0) + 1
    shown = [h for h in hosts if needs_attention(h)] if args.get("problems_only") else hosts
    return {
        "as_of": iso(ov.get("now")),
        "hosts_total": len(hosts),
        "by_status": counts,
        "collector_newest": ov.get("collector_newest"),
        "hosts": [host_brief(h) for h in shown],
        **({"note": "only hosts needing attention are listed"}
           if args.get("problems_only") else {}),
    }


def tool_host_detail(api, args):
    host = str(args.get("host") or "").strip()
    if not host:
        raise ToolError("host is required")
    minutes = _int(args, "minutes", 60, 5, 400 * 1440)
    try:
        d = api.get("/api/host/" + quote(host, safe=""), minutes=minutes)
    except NotFound:
        raise _unknown_host(api, host)
    cur = d.get("current") or {}
    hist = d.get("history") or []
    disk_hist = d.get("disk_history") or {}
    out = host_brief(cur)
    out["thresholds"] = cur.get("thresholds")
    out["disk_mounts"] = []
    for m in (cur.get("disk") or {}).get("mounts") or []:
        # How far the mount moved over the window, in percentage points: the
        # question behind "is this disk filling" that the projection answers
        # only over days.
        pcts = [p["pct"] for p in disk_hist.get(m.get("mount")) or []
                if p.get("pct") is not None]
        out["disk_mounts"].append(_drop_none({
            "mount": m.get("mount"), "pct": _pct(m.get("pct")), "status": m.get("status"),
            "used_bytes": m.get("used_bytes"), "total_bytes": m.get("total_bytes"),
            "projection": m.get("projection"),
            "change_pct_points": round(pcts[-1] - pcts[0], 2) if len(pcts) > 1 else None,
        }))
    out["patches"] = patches_brief(cur.get("patches"))
    if cur.get("patches", {}).get("entries"):
        out["patches"]["pending"] = [_drop_none({
            "package": i.get("package"),
            "acknowledged": bool(i.get("acked_at")) or None,
        }) for i in cur["patches"]["entries"]]
    out["services"] = [service_brief(s) for s in cur.get("services") or []]
    out["window"] = {"minutes": d.get("minutes"), "resolution": d.get("resolution"),
                     "disk_resolution": d.get("disk_resolution")}
    out["cpu_history"] = series_stats(hist, "cpu_pct", "cpu_min", "cpu_max")
    out["mem_history"] = series_stats(hist, "mem_pct", "mem_min", "mem_max")
    if args.get("include_series"):
        out["series"] = [[iso(p["ts"]),
                          None if p.get("cpu_pct") is None else round(p["cpu_pct"], 1),
                          None if p.get("mem_pct") is None else round(p["mem_pct"], 1)]
                         for p in downsample(hist, 120)]
        out["series_columns"] = ["time", "cpu_pct", "mem_pct"]
    out["recent_events"] = [event_brief(e) for e in d.get("events") or []]
    return _drop_none(out)


def tool_events(api, args):
    hours = _int(args, "hours", 24, 1, 24 * 400)
    limit = _int(args, "limit", 100, 1, 500)
    since = int(time.time()) - hours * 3600
    d = api.get("/api/events", host=args.get("host") or None,
                kind=args.get("kind") or None, since=since, limit=limit)
    evs = d.get("events") or []
    return {
        "as_of": iso(d.get("now")),
        "since": iso(since),
        "count": len(evs),
        "truncated": len(evs) >= limit or None,
        "events": [event_brief(e) for e in evs],
    }


def tool_patches(api, args):
    host = args.get("host")
    hosts = api.get("/api/overview").get("hosts") or []
    if host:
        hosts = [h for h in hosts if h.get("host") == host]
        if not hosts:
            raise _unknown_host(api, host)
    out = []
    for h in hosts:
        p = h.get("patches") or {}
        row = {"host": h.get("host"), "os": h.get("os"),
               **(patches_brief(p) or {"status": "unknown"})}
        if p.get("entries"):
            row["pending"] = [_drop_none({"package": i.get("package"),
                                          "acknowledged": bool(i.get("acked_at")) or None})
                              for i in p["entries"]]
        if p.get("unnamed_count"):
            row["unnamed_count"] = p["unnamed_count"]
        e = eol_brief(h.get("eol"))
        if e:
            row["eol"] = e
        out.append(_drop_none(row))
    return {"hosts": out}


def tool_services(api, args):
    host = args.get("host")
    hosts = api.get("/api/overview").get("hosts") or []
    if host:
        hosts = [h for h in hosts if h.get("host") == host]
        if not hosts:
            raise _unknown_host(api, host)
    rows = []
    for h in hosts:
        for s in h.get("services") or []:
            r = service_brief(s)
            r["host"] = h.get("host")
            r["checked"] = iso(s.get("checked_at"))
            if s.get("hardware"):
                r["hardware"] = s["hardware"]
            rows.append(_drop_none(r))
    return {"services": rows}


_RO = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = {
    "fleet_status": (tool_fleet_status, {
        "description": "Every host's current status: resources, reachability, patches, "
                       "end of life and service checks, with counts by status. "
                       "Set problems_only to list just the hosts that need attention.",
        "inputSchema": {"type": "object", "properties": {
            "problems_only": {"type": "boolean", "default": False,
                              "description": "only hosts that need attention"},
        }},
    }),
    "host_detail": (tool_host_detail, {
        "description": "One host in depth: every disk with its fill projection, CPU and "
                       "memory over a window (min/avg/max and when the peak was), "
                       "pending packages, service checks, and its recent state changes.",
        "inputSchema": {"type": "object", "required": ["host"], "properties": {
            "host": {"type": "string", "description": "host name as netdash shows it"},
            "minutes": {"type": "integer", "default": 60, "minimum": 5,
                        "description": "how far back; beyond the raw retention the "
                                       "server answers with hourly rollups"},
            "include_series": {"type": "boolean", "default": False,
                               "description": "also return up to 120 CPU/memory points"},
        }},
    }),
    "events": (tool_events, {
        "description": "The state-change log: hosts going down, stale, warning or back to "
                       "ok, and service checks failing or recovering. Newest first.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "only this host"},
            "hours": {"type": "integer", "default": 24, "minimum": 1,
                      "description": "how far back"},
            "kind": {"type": "string",
                     "description": "'status' for host status, 'service' for every "
                                    "service check, 'service:<id>' for one, 'host' "
                                    "for hosts removed from the dashboard"},
            "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
        }},
    }),
    "patches": (tool_patches, {
        "description": "Pending updates per host: security and other counts, the named "
                       "security packages and whether a person acknowledged each, reboot "
                       "required, and the OS release's end-of-life state.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "only this host"},
        }},
    }),
    "services": (tool_services, {
        "description": "Every application and management-controller check (Home Assistant, "
                       "Plex, Transmission, BMC ...) with its state, detail and hardware "
                       "sensors where read.",
        "inputSchema": {"type": "object", "properties": {
            "host": {"type": "string", "description": "only checks attached to this host"},
        }},
    }),
}


# ---------- MCP over stdio ----------

def tool_list():
    return [dict(spec, name=name, annotations=_RO) for name, (_, spec) in TOOLS.items()]


def call_tool(api, name, args):
    if name not in TOOLS:
        raise UnknownTool(name)
    try:
        result = TOOLS[name][0](api, args or {})
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}],
                "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def handle(api, msg):
    """One JSON-RPC message in, its response out -- or None for a notification,
    which by definition is never answered."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      -32600, "invalid request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            return _result(mid, {
                "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "netdash", "version": NETDASH_VERSION},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": tool_list()})
        if method == "tools/call":
            try:
                return _result(mid, call_tool(api, params.get("name"),
                                              params.get("arguments")))
            except UnknownTool:
                return _error(mid, -32602, "unknown tool: %s" % params.get("name"))
        return _error(mid, -32601, "method not found: %s" % method)
    except Exception as e:
        # A bug here must cost one answer, not the session: the agent's client
        # treats a dead server as gone until someone restarts it.
        sys.stderr.write("netdash-mcp: %s: %s: %s\n" % (method, type(e).__name__, e))
        return _error(mid, -32603, "internal error: %s" % e)


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(api, inp, out):
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "parse error")
        else:
            if msg == []:                      # JSON-RPC 2.0: one error, not silence
                reply = _error(None, -32600, "invalid request: empty batch")
            elif isinstance(msg, list):        # a batch, from a 2025-03-26 client
                reply = [r for r in (handle(api, m) for m in msg) if r is not None] or None
            else:
                reply = handle(api, msg)
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description="netdash MCP server (stdio)")
    ap.add_argument("--url", default=os.environ.get("NETDASH_URL"),
                    help="netdash server base URL, e.g. http://netdash.lan:8080 "
                         "(or NETDASH_URL)")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="seconds to wait for the netdash API (default 10)")
    ap.add_argument("--version", action="version", version=NETDASH_VERSION)
    a = ap.parse_args(argv)
    if not a.url:
        ap.error("--url (or NETDASH_URL) is required")
    # Without a scheme urllib refuses every request, and each tool would then
    # blame netdash for a typo in the client's own config.
    if urlsplit(a.url).scheme not in ("http", "https"):
        ap.error("--url must start with http:// or https://, got %r" % a.url)
    try:
        serve(Api(a.url, a.timeout), sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
