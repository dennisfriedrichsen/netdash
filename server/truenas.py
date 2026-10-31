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
  GET  /boot/get_state     -> the boot pool, which /pool/dataset leaves out,
                              and its health, scan and topology
  GET  /pool               -> each data pool's status, scan and topology
  GET  /disk               -> serial, model and standby setting per disk
  GET  /smart/test/results -> every disk's SMART self-test log, newest first
  POST /disk/smart_attributes "ada0" -> attribute 9, power-on hours, which is
                              what the test log dates its entries in
  GET  /jail               -> every iocage jail, running or not (see _jails)
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
import datetime
import ipaddress
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

import zfs
try:
    import zoneinfo
except ImportError:          # Python < 3.9: jail uptimes go unreported
    zoneinfo = None


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


def _boot_state(cfg):
    """/boot/get_state, or {} when the box cannot answer it. Fetched once a
    poll and read twice: for the boot pool's capacity and for its health."""
    try:
        return _request(cfg, "/boot/get_state") or {}
    except TrueNASError:
        return {}


def _boot_pool(st):
    """The OS drive, as one more disk row named for its pool.

    /pool/dataset lists data pools only, so without this the boot pool -- where
    every update leaves another boot environment behind until someone deletes
    it -- was never shown at all. It is named freenas-boot on CORE and
    boot-pool on FreeCORE, and both answer this endpoint the same way (verified
    live on each). Its zpool size and allocation are used as they are: a boot
    pool is a single disk or a mirror, with no raidz parity to make the raw
    size overstate what is usable.

    Best-effort, like the metric graphs: a box that cannot answer still reports
    its data pools. Returns a list so the caller can simply add it on.
    """
    props = st.get("properties") or {}
    size = (props.get("size") or {}).get("parsed")
    alloc = (props.get("allocated") or {}).get("parsed")
    if not st.get("name") or size is None or alloc is None or int(size) <= 0:
        return []
    return [{"mount": st["name"], "used_bytes": int(alloc), "total_bytes": int(size)}]


def _pool_health(cfg, boot):
    """Every pool's health, boot pool included, as zfs pool records.

    Polled at the metric rate, unlike SMART: a pool going DEGRADED is the
    reading that matters most on a NAS, and /pool is one cheap call that
    reads the kernel's state without touching a disk.

    None, not [], when /pool cannot be read: "no pools" would be a claim,
    and the dashboard should show it as unknown instead.
    """
    try:
        rows = _request(cfg, "/pool") or []
    except TrueNASError:
        return None
    out = [r for r in (zfs.pool_from_truenas(p) for p in rows if isinstance(p, dict)) if r]
    b = zfs.pool_from_truenas(boot, boot=True) if boot else None
    if b:
        out.append(b)
    return out


# SMART is polled on its own slow interval, like the update check. The test
# log changes once a day at most, and power-on hours need a smartctl run per
# disk on the NAS. Per box, keyed by host, for the same reason _PATCH_CACHES is.
_SMART_CACHES = {}


def _power_on_hours(cfg, disk):
    """Attribute 9 for one disk, or None.

    Only ATA disks have it: FreeCORE's virtual disks answer 422 "Only ATA
    device support S.M.A.R.T. attributes", and so would NVMe. Their tests are
    still reported, just without an age.
    """
    try:
        attrs = _request(cfg, "/disk/smart_attributes", "POST", disk) or []
    except TrueNASError:
        return None
    for a in attrs if isinstance(attrs, list) else []:
        if isinstance(a, dict) and a.get("id") == 9:
            raw = (a.get("raw") or {}).get("value")
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def _smart(cfg):
    """The newest self-test per disk, named by serial. None until known.

    /smart/test/results dates each test by the disk's power-on hours when it
    ran (`lifetime`), not by the clock, so a test's age is the disk's power-on
    hours now minus that. Reading them runs smartctl, which wakes a disk that
    has spun down -- so a disk with a standby timer is not asked, and its age
    is left unknown rather than keeping it spinning for netdash.

    Ordered by the log: entry 1 is the newest. A disk with SMART switched off
    in the GUI is skipped; one with no tests at all is listed with none.
    """
    cache = _SMART_CACHES.setdefault(cfg.get("host"), {"ts": 0.0, "value": None})
    now = time.time()
    interval = int(cfg.get("smart_poll_seconds") or 3600)
    if now - cache["ts"] < interval:
        return cache["value"]
    cache["ts"] = now
    try:
        disks = _request(cfg, "/disk") or []
        results = _request(cfg, "/smart/test/results") or []
    except TrueNASError:
        return cache["value"]
    by_name = {d.get("name"): d for d in disks if isinstance(d, dict)}
    out = []
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict) or not r.get("disk"):
            continue
        d = by_name.get(r["disk"]) or {}
        if d.get("togglesmart") is False:
            continue
        tests = [t for t in r.get("tests") or [] if isinstance(t, dict)]
        tests.sort(key=lambda t: t.get("num") or 0)
        t = tests[0] if tests else None
        age = None
        if t and t.get("lifetime") is not None \
                and str(d.get("hddstandby") or "ALWAYS ON").upper() == "ALWAYS ON":
            poh = _power_on_hours(cfg, r["disk"])
            if poh is not None:
                age = max(0, poh - int(t["lifetime"]))
        out.append({
            "disk": r["disk"],
            "serial": d.get("serial") or None,
            "model": d.get("model") or None,
            "status": (str(t.get("status") or "").upper() or None) if t else None,
            "status_verbose": t.get("status_verbose") if t else None,
            "test": t.get("description") if t else None,
            "age_hours": age,
        })
    out.sort(key=lambda x: x["disk"])
    cache["value"] = out
    return out


def _jail_address(ip4_addr):
    """The jail's IPv4 address from iocage's ip4_addr, or None.

    iocage writes "<iface>|<addr>[/<mask>]", comma-separated when there are
    several -- "epair0b|10.0.0.133" on mercury for a running DHCP jail -- and
    "DHCP (not running)" or "none" when there is nothing to report. The first
    non-loopback address wins; anything that does not parse is no address,
    rather than a string the prober would try to ping.
    """
    for part in str(ip4_addr or "").split(","):
        addr = part.split("|")[-1].split("/")[0].strip()
        try:
            ip = ipaddress.IPv4Address(addr)
        except ValueError:
            continue
        if not ip.is_loopback and not ip.is_unspecified:
            return str(ip)
    return None


def _jail_uptime(last_started, info, now):
    """Seconds since iocage last started the jail, or None.

    last_started is the NAS's wall-clock time with no zone -- "2026-10-16
    02:05:25" -- so it is read in the timezone /system/info reports. A jail
    whose start predates the NAS's own boot cannot have been running since
    then; iocage keeps the stamp across reboots, so that is capped at the NAS's
    uptime rather than reported as months the jail was not running.
    """
    tz = info.get("timezone")
    if not last_started or not tz or zoneinfo is None:
        return None
    try:
        started = datetime.datetime.strptime(str(last_started), "%Y-%m-%d %H:%M:%S")
        started = started.replace(tzinfo=zoneinfo.ZoneInfo(tz)).timestamp()
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None
    up = now - started
    if info.get("uptime_seconds"):
        up = min(up, float(info["uptime_seconds"]))
    return int(up) if up >= 0 else None


def _jails(cfg, info):
    """Every jail on the box, for the server to list as hosts of their own.

    What CORE's API knows: whether each jail is up, its release, its address,
    whether it starts at boot, and when it last started. What it does not:
    CPU, memory or disk per jail -- pcpu and memoryuse in the same records are
    rctl limits, "off" here, and used/available read "readonly". So these
    entries carry no metrics, and a jail with its own collector supplies them.

    Best-effort: a box with no jails, or with the jail plugin not activated,
    answers with an error or an empty list, and either way reports no jails.
    """
    try:
        rows = _request(cfg, "/jail") or []
    except TrueNASError:
        return []
    now = time.time()
    out = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        # The jail's hostname is what a collector inside it reports itself as,
        # so it is the name both sources agree on. iocage defaults it to the
        # jail name, which stands in when it is unset.
        name = str(j.get("host_hostname") or j.get("id") or "").split(".")[0]
        if not name:
            continue
        up = j.get("state") == "up"
        rel = j.get("release")
        out.append({
            "host": name,
            "os": ("FreeBSD %s" % rel) if rel else None,
            "virt": "jail",
            "state": "running" if up else "stopped",
            "boot": None if j.get("boot") is None else bool(j.get("boot")),
            "address": _jail_address(j.get("ip4_addr")) if up else None,
            "uptime_seconds": _jail_uptime(j.get("last_started"), info, now) if up else None,
        })
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
    boot = _boot_state(cfg)
    return {
        "host": cfg.get("display_name") or cfg["host"],
        "ts": int(time.time()),
        "os": ("%s %s" % (_product(cfg), version)).strip(),
        "cpu_pct": cpu,
        "mem_used_bytes": mem,
        "mem_total_bytes": physmem,
        "uptime_seconds": int(info["uptime_seconds"]) if info.get("uptime_seconds") else None,
        "disks": _pools(cfg) + _boot_pool(boot),
        "pools": _pool_health(cfg, boot),
        "smart": _smart(cfg),
        "patches": _patches(cfg),
        "jails": _jails(cfg, info),
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
