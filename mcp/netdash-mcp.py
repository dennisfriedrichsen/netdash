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
netdash monitors a home network: CPU, memory and disk per host, ZFS pool
health (state, scrubs, SMART), reachability, pending security patches, OS end of life, and application checks (Home
Assistant, Plex, a BMC, ...). Start with fleet_status(problems_only=true) to
see what needs attention, then host_detail or events for the why.

Host status is one of ok, warning, critical, stale (expected to report and has
not), down (confirmed not answering), away (configured as allowed to go
quiet -- a laptop asleep; not a problem), stopped (a jail its host says is
stopped) or unknown. A ZFS pool that is degraded, has errors or an overdue
scrub, or a disk that failed SMART, does count toward status. Patches and end
of life are deliberately not part of status; services are not either. Acknowledged
patches and EOL warnings were reviewed by a person and are not news.

An appliance -- a UniFi console -- reports the devices it manages under
"members". Their faults (an access point offline, one over its CPU or memory
threshold) are deliberately not part of the console's status: the console
itself can be healthy while an AP is down. They do count toward attention.

problems_only, and each host's "attention" list, cover status, unacknowledged
security patches, reboots, end of life, failing service checks and appliance
members. They leave out ordinary (non-security) updates, a patch check whose
answer is unknown or old, and an outdated collector: "nothing needs attention"
is not "nothing to maintain". An away or stopped host is not a problem, but
patch and end-of-life findings it last reported still count.

History is summarised, never complete by assumption: host_detail says what
resolution answered, how much of the window it covers, and how its averages
are weighted. Hourly rollups give the hour a peak fell in, not its minute;
daily disk points are each day's peak, not its reading at a moment.

A host with "collector": false runs no collector -- a printer, a camera, a
router -- and is on the dashboard only because a service check names it. It
has no readings and never reports; its status is the reachability probe's
answer (ok, down, or unknown until probed), and its checks are its substance.

Each service check carries "checks": exactly what a pass proves, and what it
does not. A tcp check passing means something accepts connections on that
port, not that the service works. Report it in those terms.

Everything here is read-only. To acknowledge or forget anything, tell the
user to do it on the dashboard."""


class ToolError(Exception):
    """A failure to report to the agent as the tool's answer, not a crash."""


class NotFound(ToolError):
    """The API answered 404."""


class UnknownTool(Exception):
    """tools/call named a tool we do not have."""


class InvalidParams(Exception):
    """tools/call arguments that are not even an object."""


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
        "source": p.get("source"),
        # Why an unknown is unknown: a check from before the last reboot reads
        # otherwise as a check that never ran.
        "predates_boot": p.get("predates_boot") or None,
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


def service_brief(s, full=False):
    """A check in brief. In full -- host_detail and the services tool -- also
    what it asks, what a pass proves, and what it found: the claim a check
    makes is in its `checks` sentence, and an agent without it will report a
    port that accepts connections as a service that works."""
    out = {
        "name": s.get("name"), "kind": s.get("kind"), "id": s.get("id"),
        "state": s.get("state"), "severity": s.get("severity"),
        "detail": s.get("detail"),
        "since": iso(s.get("since")),
    }
    if full:
        out.update(target=s.get("target"), probe=s.get("probe"),
                   checks=s.get("checks"), facts=s.get("facts") or None)
    return _drop_none(out)


def storage_brief(sg, full=False):
    """Pool health. In a fleet listing, only what is wrong; in host_detail,
    every pool, since "is tank scrubbed" is a fair question about a healthy
    one."""
    if not sg:
        return None
    pools = [_drop_none({
        "pool": p.get("name"), "state": p.get("state"), "severity": p.get("severity"),
        "reason": p.get("reason") if p.get("severity") != "ok" else None,
        "boot": p.get("boot") or None,
        "bad_devices": [_drop_none(b) for b in p.get("bad_devices") or []],
        "last_scrub": iso((p.get("scan") or {}).get("end")),
        "scrub": (p.get("scrub") or {}).get("reason"),
        "scrub_severity": (p.get("scrub") or {}).get("severity"),
    }) for p in sg.get("pools") or []
        if full or p.get("severity") != "ok"
        or (p.get("scrub") or {}).get("severity") == "warning"]
    smart = [_drop_none({
        "disk": d.get("disk"), "serial": d.get("serial"), "pool": d.get("pool"),
        "status": d.get("status"), "note": d.get("note"),
        "test_age_hours": d.get("age_hours"),
    }) for d in sg.get("smart") or []
        if full or d.get("severity") == "critical"]
    if not full and sg.get("status") == "ok":
        return None
    # In brief, `smart` is only the failed disks -- which disk, by serial, is
    # the thing a person has to go and pull.
    return _drop_none({"status": sg.get("status"), "pools": pools,
                       "smart_failed": sg.get("failing"), "smart": smart})


_BAD = ("warning", "critical")


def members_brief(f, full=False):
    """An appliance's managed devices. Counted in brief, with the offline and
    over-threshold ones named; every member in full. Kept apart from the
    console's own status, as the API keeps it: see INSTRUCTIONS."""
    if not f:
        return None
    members = f.get("members") or []
    unhealthy = [_drop_none({
        "name": m.get("name"),
        "cpu_pct": m.get("cpu_pct") if m.get("cpu_status") in _BAD else None,
        "cpu_status": m.get("cpu_status") if m.get("cpu_status") in _BAD else None,
        "mem_pct": m.get("mem_pct") if m.get("mem_status") in _BAD else None,
        "mem_status": m.get("mem_status") if m.get("mem_status") in _BAD else None,
    }) for m in members
        if m.get("online") and (m.get("cpu_status") in _BAD or m.get("mem_status") in _BAD)]
    out = {
        "total": f.get("total"), "online": f.get("online"),
        "offline": f.get("offline") or None,
        "offline_names": f.get("offline_names") or None,
        "over_threshold": unhealthy,
        "firmware_updatable": f.get("updatable") or None,
    }
    if full:
        out["members"] = [_drop_none({
            "name": m.get("name"), "model": m.get("model"), "state": m.get("state"),
            "online": m.get("online"),
            "cpu_pct": _pct(m.get("cpu_pct")), "cpu_status": m.get("cpu_status"),
            "mem_pct": _pct(m.get("mem_pct")), "mem_status": m.get("mem_status"),
            "uptime_seconds": m.get("uptime_seconds"), "firmware": m.get("firmware"),
            "firmware_updatable": m.get("firmware_updatable"),
        }) for m in members]
    return _drop_none(out)


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
    if h.get("source") == "services":
        # No collector: nothing has ever reported, so there is no last report,
        # no readings and no patch state to give -- a timestamp here would be
        # the server's clock, and would read as a host that just checked in.
        return _drop_none({
            "host": h.get("host"),
            "status": status,
            "collector": False,
            "down_reason": h.get("down_reason"),
            "reachability": _drop_none({"state": reach.get("state"),
                                        "detail": reach.get("detail"),
                                        "address": reach.get("address")}),
            "services": [service_brief(s) for s in h.get("services") or []
                         if s.get("severity") != "ok" and s.get("state") != "disabled"],
            "services_severity": h.get("services_severity"),
            "attention": attention_reasons(h),
        })
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
        "storage": storage_brief(h.get("storage")),
        "services": [service_brief(s) for s in h.get("services") or []
                     if s.get("severity") != "ok" and s.get("state") != "disabled"],
        "services_severity": h.get("services_severity"),
        "members": members_brief(h.get("fleet")),
        "attention": attention_reasons(h),
        "virt": h.get("virt"),
        "parent": h.get("parent"),
        "collector_version": h.get("collector_version"),
        "collector_outdated": h.get("collector_outdated") or None,
    }
    return _drop_none(out)


def attention_reasons(h):
    """Why a person would want to hear about this host, one phrase each.
    Away and stopped are grey on the dashboard for a reason, and anything a
    person already acknowledged is not news either. What is deliberately left
    out is in INSTRUCTIONS."""
    out = []
    s = status_of(h)
    if s in ("warning", "critical", "stale", "down"):
        out.append("status " + s)
    p = h.get("patches") or {}
    if p.get("status") == "security" and not p.get("acknowledged"):
        out.append("security updates not acknowledged")
    if p.get("reboot_required"):
        out.append("reboot required")
    e = h.get("eol") or {}
    if e.get("status") in ("eol", "eol_soon") and not e.get("acknowledged"):
        out.append("end of life" if e["status"] == "eol" else "end of life soon")
    if h.get("services_severity") in _BAD:
        out.append("service checks " + h["services_severity"])
    f = h.get("fleet") or {}
    if f.get("offline"):
        out.append("members offline: " + ", ".join(f.get("offline_names") or ["?"]))
    hot = [m.get("name") or "?" for m in f.get("members") or []
           if m.get("online") and (m.get("cpu_status") in _BAD or m.get("mem_status") in _BAD)]
    if hot:
        out.append("members over threshold: " + ", ".join(hot))
    return out


def needs_attention(h):
    return bool(attention_reasons(h))


HOUR = 3600


def series_stats(points, key, lo_key=None, hi_key=None, n_key=None, bucket=0):
    """min/avg/max over a window, saying how much each figure can be trusted.

    Raw points (bucket=0) are readings: the peak has an exact time, and avg is
    the mean of the readings. Hourly rollups (bucket=3600) are summaries:
    `key` is the hour's average, so the extremes come from lo_key and hi_key
    -- a ten-minute spike to 100% is an hour averaging 25%, and the max of the
    averages would report it as 25% -- and the peak is known only to the hour,
    given as the interval it fell in. avg is weighted by each hour's count of
    readings (n_key) so it means what the raw mean means; legacy rollups have
    no count, and then every hour weighs the same, and the answer says so."""
    vals = []
    for p in points:
        v = p.get(key)
        if v is None:
            continue
        lo, hi = p.get(lo_key) if lo_key else None, p.get(hi_key) if hi_key else None
        vals.append((p["ts"], v, v if lo is None else lo, v if hi is None else hi,
                     p.get(n_key) if n_key else None))
    if not vals:
        return None
    peak = max(vals, key=lambda v: v[3])
    out = {
        "min": round(min(v[2] for v in vals), 1),
        "max": round(peak[3], 1),
        "last": round(vals[-1][1], 1),
        "points": len(vals),
    }
    if not bucket:
        out["avg"] = round(sum(v[1] for v in vals) / len(vals), 1)
        out["averaging"] = "mean of readings"
        out["max_at"] = iso(peak[0])
        return out
    weights = [v[4] for v in vals]
    if all(isinstance(w, int) and w > 0 for w in weights):
        out["avg"] = round(sum(v[1] * w for v, w in zip(vals, weights)) / sum(weights), 1)
        out["averaging"] = "mean of readings, weighted by readings per hour"
        out["readings"] = sum(weights)
    else:
        out["avg"] = round(sum(v[1] for v in vals) / len(vals), 1)
        out["averaging"] = ("mean of hourly averages, each hour weighted equally "
                            "(some hours predate per-hour reading counts)")
    out["max_within"] = {"from": iso(peak[0]), "to": iso(peak[0] + bucket)}
    return out


def history_quality(hist, resolution, minutes, now):
    """How much of the asked-for window the CPU and memory history covers.

    Hourly: buckets present against the hours in the window, and each one's
    coverage -- 'full', 'partial' (folded after some of its readings were
    pruned) or unknown (written by an old server whose roller could keep just
    the tail of an hour). Raw: the span the readings cover and the silences in
    it. `complete` is true only when nothing above says otherwise."""
    start = now - minutes * 60
    if not hist:
        return {"resolution": resolution, "points": 0, "complete": False,
                "warning": "no history in this window"}
    out = {"resolution": resolution, "points": len(hist),
           "observed_from": iso(hist[0]["ts"])}
    warn = []
    if resolution == "hourly":
        expected = now // HOUR - start // HOUR + 1
        cov = {}
        for p in hist:
            c = p.get("coverage") or "unknown"
            cov[c] = cov.get(c, 0) + 1
        out.update(observed_to=iso(min(hist[-1]["ts"] + HOUR, now)),
                   hours_expected=expected, coverage=cov,
                   readings=sum(p.get("samples") or 0 for p in hist))
        if len(hist) < expected:
            warn.append("%d of %d hours have no data" % (expected - len(hist), expected))
        if cov.get("partial"):
            warn.append("partial hours (some readings pruned before they were "
                        "rolled up): %d" % cov["partial"])
        if cov.get("unknown"):
            warn.append("hours that predate coverage tracking and may hold only "
                        "part of their readings: %d" % cov["unknown"])
    else:
        ts = [p["ts"] for p in hist]
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        typical = gaps[len(gaps) // 2] if gaps else None
        limit = max(300, 3 * (typical or 60))
        silent = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > limit]
        out.update(observed_to=iso(ts[-1]), interval_seconds=typical)
        if ts[0] - start > limit:
            warn.append("readings start %d minutes into the window" % ((ts[0] - start) // 60))
        if silent:
            out["gaps"] = [{"from": iso(a), "to": iso(b)} for a, b in silent[:10]]
            warn.append("silences longer than %d seconds: %d" % (limit, len(silent)))
    out["complete"] = not warn
    if warn:
        out["warning"] = "; ".join(warn)
    return out


def disk_change(points, meta):
    """How a mount moved over the window, said in terms of what the points are.

    Raw points are readings, so last minus first is the change between two
    moments, and when they were. Bucketed points -- raw or daily -- are each
    bucket's peak, and the difference of two peaks says nothing certain about
    the readings at either end; it is given as peak-to-peak, by name, with the
    buckets it spans."""
    pts = [p for p in points if p.get("pct") is not None]
    if len(pts) < 2:
        return {}
    a, b = pts[0], pts[-1]
    out = {}
    if not meta.get("aggregate"):
        out.update(change_pct_points=round(b["pct"] - a["pct"], 2),
                   change_from=iso(a["ts"]), change_to=iso(b["ts"]))
    else:
        out.update(peak_change_pct_points=round(b["pct"] - a["pct"], 2),
                   first_peak={"bucket_start": iso(a["ts"]), "pct": round(a["pct"], 2)},
                   last_peak={"bucket_start": iso(b["ts"]), "pct": round(b["pct"], 2)})
    if any(p.get("capacity_changed") for p in pts):
        out["capacity_changed"] = True
    cov = {}
    for p in pts:
        c = p.get("coverage", "full") or "unknown"
        if c != "full":
            cov[c] = cov.get(c, 0) + 1
    if cov:
        out["points_not_full"] = cov
    return out


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

_TYPES = {
    # bool is an int in Python, and true must not become one hour.
    "integer": ("a whole number",
                lambda v: (isinstance(v, int) and not isinstance(v, bool))
                or (isinstance(v, float) and v.is_integer())),
    "boolean": ("true or false", lambda v: isinstance(v, bool)),
    "string": ("a string", lambda v: isinstance(v, str)),
}


def check_args(name, args):
    """Hold the arguments to the schema the tool advertised. A client may not,
    and a guess -- 1.9 hours read as 1, "false" read as true -- answers a
    question the agent did not ask. null is the same as leaving it out."""
    if not isinstance(args, dict):
        raise InvalidParams("arguments must be an object, got %s" % json.dumps(args))
    props = TOOLS[name][1]["inputSchema"]["properties"]
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ToolError("%s takes no argument %s; it takes: %s"
                        % (name, ", ".join(unknown), ", ".join(sorted(props)) or "none"))
    for k, v in args.items():
        what, ok = _TYPES[props[k]["type"]]
        if v is not None and not ok(v):
            raise ToolError("%s must be %s, got %s" % (k, what, json.dumps(v)))
    return {k: v for k, v in args.items() if v is not None}


def _int(args, key, default, lo, hi):
    """Clamped to [lo, hi]; the tool reports the value it used."""
    v = args.get(key)
    return default if v is None else max(lo, min(int(v), hi))


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
    if cur.get("source") == "services":
        # No collector: its checks and its state changes are all there is.
        out["services"] = [service_brief(s, full=True) for s in cur.get("services") or []]
        out["recent_events"] = [event_brief(e) for e in d.get("events") or []]
        return _drop_none(out)
    out["thresholds"] = cur.get("thresholds")
    disk_meta = d.get("disk_meta") or {}
    out["disk_mounts"] = []
    for m in (cur.get("disk") or {}).get("mounts") or []:
        out["disk_mounts"].append(_drop_none({
            "mount": m.get("mount"), "pct": _pct(m.get("pct")), "status": m.get("status"),
            "used_bytes": m.get("used_bytes"), "total_bytes": m.get("total_bytes"),
            "projection": m.get("projection"),
            **disk_change(disk_hist.get(m.get("mount")) or [], disk_meta),
        }))
    out["patches"] = patches_brief(cur.get("patches"))
    if cur.get("patches", {}).get("entries"):
        out["patches"]["pending"] = [_drop_none({
            "package": i.get("package"),
            "acknowledged": bool(i.get("acked_at")) or None,
        }) for i in cur["patches"]["entries"]]
    out["services"] = [service_brief(s, full=True) for s in cur.get("services") or []]
    out["storage"] = storage_brief(cur.get("storage"), full=True)
    out["members"] = members_brief(cur.get("fleet"), full=True)
    resolution = d.get("resolution")
    bucket = HOUR if resolution == "hourly" else 0
    now = int(d.get("now") or time.time())
    out["window"] = _drop_none({
        "minutes": d.get("minutes"),
        # The server clamps to what it retains; say so rather than answer a
        # different question than the one asked.
        "minutes_requested": minutes if minutes != d.get("minutes") else None,
        "resolution": resolution,
        "history": history_quality(hist, resolution, d.get("minutes") or minutes, now),
        "disk": _drop_none({
            "resolution": d.get("disk_resolution"),
            "bucket_seconds": disk_meta.get("bucket_seconds"),
            "points_are": ("the peak reading of each bucket"
                           if disk_meta.get("aggregate") == "max" else "readings"),
            "source": disk_meta.get("source"),
            "gap_seconds": disk_meta.get("gap_seconds"),
        }),
    })
    out["cpu_history"] = series_stats(hist, "cpu_pct", "cpu_min", "cpu_max", "cpu_n", bucket)
    out["mem_history"] = series_stats(hist, "mem_pct", "mem_min", "mem_max", "mem_n", bucket)
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
    host = args.get("host") or None
    q = dict(host=host, kind=args.get("kind") or None, since=since)
    before = args.get("cursor") or None
    # One more than asked for says whether there is more, rather than a full
    # page being taken to mean so. At the API's ceiling of 500, a second
    # one-row look past the page does the same.
    d = api.get("/api/events", limit=min(limit + 1, 500), before=before, **q)
    evs = d.get("events") or []
    has_more = len(evs) > limit
    evs = evs[:limit]
    if limit >= 500 and len(evs) == limit:
        last = evs[-1]
        has_more = bool(api.get("/api/events", limit=1, before="%d:%d" % (
            last["ts"], last["id"]), **q).get("events"))
    out = {
        "as_of": iso(d.get("now")),
        "since": iso(since),
        "hours": hours,
        "hours_requested": args["hours"] if args.get("hours") not in (None, hours) else None,
        "limit": limit,
        "count": len(evs),
        "has_more": has_more,
        "next_cursor": "%d:%d" % (evs[-1]["ts"], evs[-1]["id"]) if has_more else None,
        "events": [event_brief(e) for e in evs],
    }
    if host and not evs and not before and host not in _host_names(api):
        # Not an error: a host removed from the dashboard keeps its history,
        # and this is how one would ask for it. But an empty answer for a
        # misspelt name must not read as a quiet host.
        out["note"] = ("no host named %r is on the dashboard now -- removed, or "
                       "misspelt. Known hosts: %s" % (host, ", ".join(_host_names(api))))
    return _drop_none(out)


def tool_patches(api, args):
    host = args.get("host")
    hosts = api.get("/api/overview").get("hosts") or []
    if host:
        hosts = [h for h in hosts if h.get("host") == host]
        if not hosts:
            raise _unknown_host(api, host)
    out = []
    for h in hosts:
        if h.get("source") == "services":
            continue    # no collector, so no patch check to have run
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
            r = service_brief(s, full=True)
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
                       "end of life, service checks and an appliance's managed devices, "
                       "with counts by status and, per host, why it needs attention. "
                       "Set problems_only to list just the hosts that need attention.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "problems_only": {"type": "boolean", "default": False,
                              "description": "only hosts that need attention"},
        }},
    }),
    "host_detail": (tool_host_detail, {
        "description": "One host in depth: every disk with its fill projection and how it "
                       "moved over the window, CPU and memory over the window (min/avg/max, "
                       "the peak's time -- exact for raw readings, the hour it fell in for "
                       "rollups -- and how the average is weighted), how complete that "
                       "history is, an appliance's managed devices, pending packages, "
                       "service checks, and its recent state changes.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["host"], "properties": {
            "host": {"type": "string", "description": "host name as netdash shows it"},
            "minutes": {"type": "integer", "default": 60, "minimum": 5,
                        "description": "how far back; beyond the raw retention the "
                                       "server answers with hourly rollups, and it is "
                                       "clamped to what the server keeps"},
            "include_series": {"type": "boolean", "default": False,
                               "description": "also return up to 120 CPU/memory points"},
        }},
    }),
    "events": (tool_events, {
        "description": "The state-change log: hosts going down, stale, warning or back to "
                       "ok, service checks failing or recovering, and ZFS pools, scrubs "
                       "and SMART tests changing severity. Newest first. hours is clamped "
                       "to 1..9600 and limit to 1..500; has_more says whether "
                       "older matching events exist, and next_cursor fetches them.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "host": {"type": "string", "description": "only this host"},
            "hours": {"type": "integer", "default": 24, "minimum": 1,
                      "description": "how far back"},
            "kind": {"type": "string",
                     "description": "'status' for host status, 'service' for every "
                                    "service check, 'service:<id>' for one, 'storage' "
                                    "for pool, scrub and SMART changes, 'host' "
                                    "for hosts removed from the dashboard"},
            "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
            "cursor": {"type": "string",
                       "description": "next_cursor from a previous answer, for the "
                                      "page after it; keep the other arguments the same"},
        }},
    }),
    "patches": (tool_patches, {
        "description": "Pending updates per host: security and other counts, the named "
                       "security packages and whether a person acknowledged each, reboot "
                       "required, and the OS release's end-of-life state.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
            "host": {"type": "string", "description": "only this host"},
        }},
    }),
    "services": (tool_services, {
        "description": "Every service check -- applications (Home Assistant, Plex, "
                       "Transmission), management controllers (BMC), and generic http, "
                       "tcp and dns checks -- with its state, detail, target, what a "
                       "pass proves (checks), what it found (facts), and hardware "
                       "sensors where read.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
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
        result = TOOLS[name][0](api, check_args(name, {} if args is None else args))
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
            except InvalidParams as e:
                return _error(mid, -32602, str(e))
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
