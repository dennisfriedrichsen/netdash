"""Poll a TrueNAS CORE box over its REST API v2.0. Nothing is installed on the NAS.

Also polls anything else that serves the CORE API -- FreeCORE, the community
continuation of CORE, answers every endpoint below identically. Only the name
differs, and /system/product_name (unauthenticated) is where it says so.

Auth: API keys authenticate as `Authorization: Bearer <key>` (the box's own OpenAPI
document only advertises HTTP basic, but Bearer is what GUI-created API keys use on
CORE 13.x). We send Bearer and fall back to basic if the box rejects it.

Endpoints (verified live against TrueNAS CORE 13.0-U6.8):
  GET  /system/info        -> physmem, uptime_seconds, version
  POST /reporting/get_data -> cpu / memory / arcsize series
  GET  /pool/dataset       -> per-pool used+available (root datasets only)
  GET  /system/product_name -> "TrueNAS" on CORE, "FreeCORE" on FreeCORE

Not yet verified against the live box:
  POST /update/check_available -> {"status": "AVAILABLE"|"UNAVAILABLE", "version": ...}

Quirks this module works around, all confirmed on the live box:
  * `reporting_query.unit` accepts only HOUR/DAY/WEEK/MONTH/YEAR -- not MINUTE.
  * A data row has NO leading timestamp: row[i] pairs with legend[i].
  * The CPU graph is a *cumulative* percentage stack (idle is the last series and
    is always exactly 100), so busy% is the cumulative value just before idle.
  * Memory legend entries are decorated: `memory-active_value`, not `active`.
  * The final row of every graph is all zeros (the not-yet-filled RRD bucket).
"""

import base64
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request


class TrueNASError(Exception):
    pass


def _ctx(verify_tls):
    c = ssl.create_default_context()
    if not verify_tls:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def _request(cfg, path, method="GET", body=None, timeout=15, auth="bearer"):
    url = "%s://%s/api/v2.0%s" % (cfg.get("scheme", "https"), cfg["host"], path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    key = cfg.get("api_key") or ""
    if auth == "bearer":
        req.add_header("Authorization", "Bearer " + key)
    else:
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(key.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ctx(cfg.get("verify_tls", False))) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 401 and auth == "bearer":
            return _request(cfg, path, method, body, timeout, auth="basic")
        raise TrueNASError("%s %s -> HTTP %s: %s"
                           % (method, path, e.code,
                              e.read()[:200].decode("utf-8", "replace")))
    except Exception as e:
        raise TrueNASError("%s %s -> %s" % (method, path, e))


def _graphs(cfg, names):
    return _request(cfg, "/reporting/get_data", "POST", {
        "graphs": [{"name": n} for n in names],
        "reporting_query": {"unit": "HOUR", "page": 1},
    }) or []


def _norm(label):
    """'memory-active_value' -> 'active';  'idle' -> 'idle'."""
    s = re.sub(r"_value$", "", str(label))
    return s.split("-", 1)[1] if "-" in s else s


def _last_row(graph):
    """Newest row carrying real data. The trailing RRD bucket is all zeros."""
    for row in reversed(graph.get("data") or []):
        if any(v not in (None, 0, 0.0) for v in row):
            return dict(zip([_norm(x) for x in graph.get("legend") or []], row))
    return None


def _cpu_pct(graph):
    vals = _last_row(graph)
    if not vals or vals.get("idle") is None:
        return None
    legend = [_norm(x) for x in graph.get("legend") or []]
    series = [vals.get(k) for k in legend]
    idle_at = legend.index("idle")

    # Cumulative stack: values never decrease and idle caps at 100.
    ordered = [v for v in series if v is not None]
    cumulative = (abs(float(vals["idle"]) - 100.0) < 0.01
                  and all(a <= b + 1e-9 for a, b in zip(ordered, ordered[1:])))
    if cumulative and idle_at > 0:
        busy = series[idle_at - 1]
    else:
        busy = 100.0 - float(vals["idle"])
    if busy is None:
        return None
    return round(max(0.0, min(100.0, float(busy))), 1)


def _mem_used(graph, arc_graph, physmem):
    """Used = active + wired + laundry, minus the ZFS ARC.

    ARC lives in wired but is reclaimable cache, so counting it would push a
    healthy NAS to ~100% as ARC grows to fill RAM. Excluding it matches what the
    Linux collector does with MemAvailable (which also excludes reclaimable cache),
    so the two read the same way on the dashboard.
    """
    vals = _last_row(graph)
    if not vals:
        return None
    used = sum(float(vals[k]) for k in ("active", "wired", "laundry")
               if vals.get(k) is not None)
    if not used:
        return None
    arc = _last_row(arc_graph) if arc_graph else None
    if arc and arc.get("arc") is not None:
        used -= float(arc["arc"])
    if physmem:
        used = min(used, float(physmem))
    return max(0, int(used))


def _pools(cfg):
    """Root datasets == one entry per pool, with used/available in bytes."""
    out = []
    for ds in _request(cfg, "/pool/dataset") or []:
        name = ds.get("name") or ""
        if "/" in name:            # child dataset, not a pool root
            continue
        used = (ds.get("used") or {}).get("parsed")
        avail = (ds.get("available") or {}).get("parsed")
        if used is None or avail is None:
            continue
        out.append({"mount": name,
                    "used_bytes": int(used),
                    "total_bytes": int(used) + int(avail)})
    return out


# update.check_available contacts iXsystems' update server, so it is polled on
# its own slow interval rather than at the metric rate. The last good answer is
# kept across metric polls; when the call fails it is returned unchanged and
# ages out into "unknown" on the dashboard rather than flipping to a wrong value.
#
# One cache per box, keyed by host. A single shared cache meant a second NAS
# was answered with the first one's update status -- and, gated on the first
# one's attempt time, never asked at all.
_PATCH_CACHES = {}


def _patch_cache(cfg):
    return _PATCH_CACHES.setdefault(cfg.get("host"),
                                    {"ts": 0.0, "value": None, "error": None})


def _patches(cfg):
    """OS-image update status, in the shape the collectors' patch check sends.

    CORE reports one image update, with no per-package security classification
    -- so `security` is null rather than 0, which would claim the box had been
    checked for CVEs and found clean. See PATCH-CHECKS.md.

    Note for later: `update.check_available` was removed in TrueNAS 25.x in
    favour of `update.status`. This call is why the poller is CORE-specific.
    """
    cache = _patch_cache(cfg)
    now = time.time()
    interval = int(cfg.get("patch_poll_seconds") or 3600)
    # Gate on the last ATTEMPT, not the last success. Gating on a cached value
    # meant a box that could never answer -- CORE 13 cannot reach
    # update-master.ixsystems.com -- was retried on every 60s metric poll
    # instead of hourly, and made the NAS attempt an internet round trip each
    # time. Recording the attempt first is what makes the interval real.
    if now - cache["ts"] < interval:
        return cache["value"]
    cache["ts"] = now

    try:
        r = _request(cfg, "/update/check_available", "POST", {}) or {}
    except TrueNASError as e:
        # Said once, and again only when the error changes. This runs forever on
        # a schedule into a journal nothing rotates, so repeating an unchanging
        # failure hourly would bury everything else -- but staying silent leaves
        # a permanently "unknown" badge with no explanation anywhere.
        msg = str(e)[:200]
        if msg != cache["error"]:
            sys.stderr.write("truenas update check unavailable on %s: %s\n"
                             % (cfg.get("display_name") or cfg.get("host"), msg))
            cache["error"] = msg
        return cache["value"]
    cache["error"] = None

    status = str(r.get("status") or "").upper()
    if status not in ("AVAILABLE", "UNAVAILABLE"):
        return cache["value"]

    version = r.get("version") or ""
    value = {
        "security": None,
        "other": 1 if status == "AVAILABLE" else 0,
        "checked_at": int(now),
        "source": "truenas-update",
        "detail": ("%s available" % version).strip() if status == "AVAILABLE"
                  else "no per-package security classification on CORE",
    }
    cache.update(ts=now, value=value)
    return value


def _product(cfg):
    """What the box calls itself, as the OS string should name it.

    CORE answers "TrueNAS", and is shown as "TrueNAS CORE" because that is the
    string the EOL override and the dashboard icon already key on. Anything
    else -- "FreeCORE" -- is shown as it names itself: it is a different
    product with its own support life, and calling it TrueNAS CORE would hand
    it CORE's end-of-life date. A box too old to answer is CORE.
    """
    try:
        name = str(_request(cfg, "/system/product_name") or "").strip()
    except TrueNASError:
        name = ""
    return "TrueNAS CORE" if name in ("", "TrueNAS") else name


def collect(cfg):
    """Return a payload in the same shape the host collectors POST."""
    if not cfg.get("api_key"):
        raise TrueNASError("api_key is not set for %s in config.json"
                           % (cfg.get("display_name") or cfg.get("host")))

    info = _request(cfg, "/system/info") or {}
    physmem = info.get("physmem")

    cpu = mem = None
    try:
        gs = {g.get("name"): g for g in _graphs(cfg, ["cpu", "memory", "arcsize"])}
        if gs.get("cpu"):
            cpu = _cpu_pct(gs["cpu"])
        if gs.get("memory"):
            mem = _mem_used(gs["memory"], gs.get("arcsize"), physmem)
    except TrueNASError:
        pass   # metrics are best-effort; pools and uptime still report

    # "TrueNAS-13.0-U6.8" -> "13.0 U6.8". The product prefix is stripped
    # whatever it says, since the name comes from _product instead.
    version = re.sub(r"^[A-Za-z]\w*-", "", info.get("version") or "").replace("-", " ", 1)
    return {
        "host": cfg.get("display_name") or cfg["host"],
        "ts": int(time.time()),
        "os": ("%s %s" % (_product(cfg), version)).strip(),
        "cpu_pct": cpu,
        "mem_used_bytes": mem,
        "mem_total_bytes": physmem,
        "uptime_seconds": int(info["uptime_seconds"]) if info.get("uptime_seconds") else None,
        "disks": _pools(cfg),
        "patches": _patches(cfg),
    }


if __name__ == "__main__":
    import os
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    block = json.load(open(path))["truenas"]
    # The block is one box or a list of them; check every one configured.
    for one in (block if isinstance(block, list) else [block]):
        print(json.dumps(collect(one), indent=2))
