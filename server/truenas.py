"""Poll a TrueNAS CORE box over its REST API v2.0. Nothing is installed on the NAS.

Auth: API keys authenticate as `Authorization: Bearer <key>` (the box's own OpenAPI
document only advertises HTTP basic, but Bearer is what GUI-created API keys use on
CORE 13.x). We send Bearer and fall back to basic if the box rejects it.

Endpoints used (all confirmed present in 10.0.0.2's own /api/v2.0/ OpenAPI document):
  GET  /system/info        -> physmem, uptime_seconds, version
  POST /reporting/get_data -> cpu + memory time series
  GET  /pool/dataset       -> per-pool used/available (root datasets only)
"""

import base64
import json
import ssl
import time
import urllib.error
import urllib.request


class TrueNASError(Exception):
    pass


def _ctx(verify_tls):
    if verify_tls:
        return ssl.create_default_context()
    c = ssl.create_default_context()
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
        raw = base64.b64encode(key.encode()).decode()
        req.add_header("Authorization", "Basic " + raw)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ctx(cfg.get("verify_tls", False))) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 401 and auth == "bearer":
            return _request(cfg, path, method, body, timeout, auth="basic")
        raise TrueNASError("%s %s -> HTTP %s: %s"
                           % (method, path, e.code, e.read()[:200].decode("utf-8", "replace")))
    except Exception as e:
        raise TrueNASError("%s %s -> %s" % (method, path, e))


def _latest_row(graph_result):
    """reporting/get_data returns {name, legend, start, end, step, data:[[ts,v,...]]}.
    Return (legend, last row with any non-null value)."""
    legend = graph_result.get("legend") or []
    for row in reversed(graph_result.get("data") or []):
        if any(v is not None for v in row[1:]):
            return legend, row
    return legend, None


def _cpu_pct(cfg):
    """CPU busy % = 100 - idle, from the 'cpu' reporting graph."""
    res = _request(cfg, "/reporting/get_data", "POST", {
        "graphs": [{"name": "cpu"}],
        "reporting_query": {"unit": "MINUTE", "page": 1},
    })
    if not res:
        return None
    legend, row = _latest_row(res[0])
    if not row:
        return None
    # legend[0] corresponds to row[1]; find the idle series.
    for i, name in enumerate(legend):
        if str(name).lower().strip() == "idle":
            idle = row[i + 1]
            if idle is not None:
                return max(0.0, min(100.0, 100.0 - float(idle)))
    # No idle series -> sum the busy series instead.
    busy = [v for v in row[1:] if v is not None]
    return max(0.0, min(100.0, sum(float(v) for v in busy))) if busy else None


def _memory(cfg, physmem):
    """Used bytes from the 'memory' graph; free/cache/inactive are not 'used'."""
    try:
        res = _request(cfg, "/reporting/get_data", "POST", {
            "graphs": [{"name": "memory"}],
            "reporting_query": {"unit": "MINUTE", "page": 1},
        })
    except TrueNASError:
        return None
    if not res:
        return None
    legend, row = _latest_row(res[0])
    if not row:
        return None
    vals = {str(n).lower().strip(): row[i + 1] for i, n in enumerate(legend)}
    # FreeBSD memory graph series: free, active, inactive, cache, wired, laundry
    free_like = sum(float(vals[k]) for k in ("free", "cache", "inactive")
                    if vals.get(k) is not None)
    if free_like and physmem:
        return max(0, int(physmem - free_like))
    used_like = sum(float(vals[k]) for k in ("active", "wired", "laundry")
                    if vals.get(k) is not None)
    return int(used_like) or None


def _pools(cfg):
    """Root datasets == one entry per pool, with used/available in bytes."""
    out = []
    try:
        datasets = _request(cfg, "/pool/dataset")
    except TrueNASError:
        return out
    for ds in datasets or []:
        name = ds.get("name") or ""
        if "/" in name:            # child dataset, not a pool root
            continue
        used = (ds.get("used") or {}).get("parsed")
        avail = (ds.get("available") or {}).get("parsed")
        if used is None or avail is None:
            continue
        out.append({
            "mount": name,
            "used_bytes": int(used),
            "total_bytes": int(used) + int(avail),
        })
    return out


def collect(cfg):
    """Return a payload in the same shape the host collectors POST."""
    if not cfg.get("api_key"):
        raise TrueNASError("truenas.api_key is not set in config.json")

    info = _request(cfg, "/system/info") or {}
    physmem = info.get("physmem")

    try:
        cpu = _cpu_pct(cfg)
    except TrueNASError:
        cpu = None

    return {
        "host": cfg.get("display_name") or cfg["host"],
        "ts": int(time.time()),
        "os": "TrueNAS CORE %s" % (info.get("version") or "").replace("TrueNAS-", ""),
        "cpu_pct": cpu,
        "mem_used_bytes": _memory(cfg, physmem),
        "mem_total_bytes": physmem,
        "uptime_seconds": int(info["uptime_seconds"]) if info.get("uptime_seconds") else None,
        "disks": _pools(cfg),
    }


if __name__ == "__main__":
    # Self-test: python3 truenas.py [config.json]  -- prints what the NAS returns.
    import os
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    tn = json.load(open(path))["truenas"]
    print(json.dumps(collect(tn), indent=2))
