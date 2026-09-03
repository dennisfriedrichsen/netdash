"""Poll a Hubitat Elevation hub over its local HTTP endpoints. Nothing is installed.

Auth: none. These endpoints are open to anyone on the LAN when the hub's own
"hub security" is off, which is how most hubs run. If security is later turned
on, every path below answers with the login page instead of data -- that is
detected and reported as such rather than as a dead hub.

Endpoints (verified live against a C-8 Pro on 2.5.1.174):
  GET /hub2/hubData                  -> model, version, name, alerts{}
  GET /hub/advanced/freeOSMemory     -> free OS memory, KB, live
  GET /hub/advanced/freeOSMemoryLast -> CSV, one row, the 5-minute averages
  GET /hub/cpuInfo                   -> "Processors N" + "Load Average X"
  GET /hub/cloud/checkForUpdate      -> platform update availability

Quirks this module works around, all confirmed on the live hub:

  * The "5m CPU avg" column is a LOAD AVERAGE, not a percentage. Hubitat's own
    Hub Information driver divides it by the core count and multiplies by 100
    (hubInfo.groovy: `cpuWork = (cpuWork/4.0D)*100.0D`), and hardcodes the 4.
    We read the count from /hub/cpuInfo instead. Reporting the raw 0.37 as a
    percentage would show a busy hub as permanently idle.

  * freeOSMemoryLast is rewritten every five minutes, so its memory column is
    up to five minutes stale. /hub/advanced/freeOSMemory is live, so memory
    comes from there and only the load average comes from the CSV -- otherwise
    the memory trace would be a staircase at the dashboard's poll rate.

  * The hub reports FREE memory and never reports the total, so mem_total_bytes
    has to be declared in config. On this hub free alone was observed at
    1,335,680 KB (1.27 GiB), which rules out the 1 GiB the non-Pro C-8 ships;
    the C-8 Pro has 2 GiB. Declared, not measured -- if it is absent, memory is
    reported as unknown rather than guessed.

  * There is no uptime anywhere in the HTTP surface. freeOSMemoryHistory looks
    like a boot-time proxy but is capped at roughly a week, so on any hub up
    longer than that its first row is a truncation rather than a boot. Reported
    as None.

  * hubData carries alerts.platformUpdateAvailable, which looks like the same
    fact checkForUpdate returns for free. It is a cache the hub refreshes on
    its own schedule, and it was observed reading false while an update was
    genuinely available. The cloud call is the truth.

Deliberately never logged: response bodies. checkForUpdate returns the hub
owner's registered email address in `accountEmails`, and an error path that
echoes the body would put it in the journal forever.
"""

import json
import sys
import time
import urllib.error
import urllib.request


class HubitatError(Exception):
    pass


def _get(cfg, path, timeout=10):
    """GET one endpoint and return its body as text.

    Bodies are never included in error messages -- see the module docstring.
    The status code and the path are enough to act on, and they are all that is
    safe to write to a journal nothing rotates.
    """
    url = "%s://%s%s" % (cfg.get("scheme", "http"), cfg["host"], path)
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HubitatError("GET %s -> HTTP %s" % (path, e.code))
    except Exception as e:
        raise HubitatError("GET %s -> %s" % (path, e))


def _secured(path):
    """The error for a hub that has started asking for a login.

    Hub security turns every endpoint here into a 200 that serves the login
    page, so the failure looks like malformed data rather than an auth error.
    Saying "the hub is answering, it just will not tell us this any more" is
    the difference between a five-second fix and an afternoon.
    """
    return HubitatError(
        "%s did not return the expected data -- if hub security was turned on, "
        "these endpoints now serve the login page and this poller cannot read "
        "them" % path)


def _json(cfg, path):
    body = _get(cfg, path)
    try:
        return json.loads(body)
    except ValueError:
        raise _secured(path)


def _number(cfg, path):
    body = _get(cfg, path).strip()
    try:
        return float(body)
    except ValueError:
        raise _secured(path)


# The core count cannot change without a reboot into different hardware, so it
# is fetched once and kept. Cached on success only: a failed first fetch must
# not pin None for the life of the process.
_CORES = {"value": None}


def _cores(cfg):
    if _CORES["value"]:
        return _CORES["value"]
    for line in _get(cfg, "/hub/cpuInfo").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].lower() == "processors":
            try:
                n = int(parts[1])
            except ValueError:
                continue
            if n > 0:
                _CORES["value"] = n
                return n
    return None


def _cpu_pct(cfg):
    """5-minute load average, as a percentage of total CPU capacity.

    Returns None rather than a wrong number if the core count is unavailable:
    a load figure printed as a percentage is not a degraded reading, it is a
    different quantity wearing a percent sign.
    """
    rows = [r for r in _get(cfg, "/hub/advanced/freeOSMemoryLast").splitlines() if r.strip()]
    if len(rows) < 2:
        raise _secured("/hub/advanced/freeOSMemoryLast")
    cols = rows[-1].split(",")
    if len(cols) < 3:
        raise _secured("/hub/advanced/freeOSMemoryLast")
    try:
        load = float(cols[2])
    except ValueError:
        raise _secured("/hub/advanced/freeOSMemoryLast")

    cores = _cores(cfg)
    if not cores:
        return None
    return round(max(0.0, min(100.0, load / cores * 100.0)), 1)


def _memory(cfg):
    """(used_bytes, total_bytes). Total is declared in config, not measured."""
    total = cfg.get("mem_total_bytes")
    if not total:
        return None, None
    free = _number(cfg, "/hub/advanced/freeOSMemory") * 1024
    # A declared total that is smaller than the measured free memory is a wrong
    # declaration, not a hub at negative usage. Report nothing rather than a
    # number that would paint the card green for the wrong reason.
    if free > total:
        return None, int(total)
    return int(total - free), int(total)


# checkForUpdate is a round trip through Hubitat's cloud, so it runs on its own
# slow interval rather than at the metric rate, and the last good answer is
# kept across metric polls. Same shape as truenas._patches, and for the same
# reasons -- see the comments there.
_PATCH_CACHE = {"ts": 0.0, "value": None, "error": None}


def _patches(cfg):
    """Platform update status, in the shape the collectors' patch check sends.

    Hubitat ships one platform image and classifies nothing, so `security` is
    null rather than 0: a 0 would claim the hub had been checked for CVEs and
    found clean. See PATCH-CHECKS.md.
    """
    now = time.time()
    interval = int(cfg.get("patch_poll_seconds") or 3600)
    # Gate on the last ATTEMPT, not the last success, so a hub that can never
    # reach the cloud is retried hourly rather than on every metric poll.
    if now - _PATCH_CACHE["ts"] < interval:
        return _PATCH_CACHE["value"]
    _PATCH_CACHE["ts"] = now

    try:
        r = _json(cfg, "/hub/cloud/checkForUpdate") or {}
    except HubitatError as e:
        # Said once, and again only when the error changes.
        msg = str(e)[:200]
        if msg != _PATCH_CACHE["error"]:
            sys.stderr.write("hubitat update check unavailable: %s\n" % msg)
            _PATCH_CACHE["error"] = msg
        return _PATCH_CACHE["value"]
    _PATCH_CACHE["error"] = None

    # "UPDATE_AVAILABLE" is the only status string seen on a live hub, so the
    # up-to-date case is taken from the unambiguous boolean instead of guessing
    # at the spelling of its opposite. Anything that matches neither leaves the
    # previous answer standing and ages out into "unknown" on the dashboard,
    # rather than being read as "no update" on no evidence.
    status = str(r.get("status") or "").upper()
    upgrade = r.get("upgrade")
    if status == "UPDATE_AVAILABLE" or upgrade is True:
        available = 1
    elif upgrade is False:
        available = 0
    else:
        return _PATCH_CACHE["value"]

    version = str(r.get("version") or "")
    value = {
        "security": None,
        "other": available,
        "checked_at": int(now),
        "source": "hubitat-cloud",
        "detail": ("%s available" % version).strip() if available
                  else "no per-package security classification on Hubitat",
    }
    _PATCH_CACHE.update(ts=now, value=value)
    return value


def collect(cfg):
    """Return a payload in the same shape the host collectors POST."""
    if not cfg.get("host"):
        raise HubitatError("hubitat.host is not set in config.json")

    hub = _json(cfg, "/hub2/hubData") or {}
    model = hub.get("model") or ""
    version = hub.get("version") or ""

    cpu = None
    mem_used = mem_total = None
    try:
        cpu = _cpu_pct(cfg)
        mem_used, mem_total = _memory(cfg)
    except HubitatError:
        pass   # metrics are best-effort; identity and patches still report

    return {
        "host": cfg.get("display_name") or hub.get("name") or cfg["host"],
        "ts": int(time.time()),
        "os": ("Hubitat %s %s" % (model, version)).strip(),
        "cpu_pct": cpu,
        "mem_used_bytes": mem_used,
        "mem_total_bytes": mem_total,
        # No uptime anywhere in the HTTP surface -- see the module docstring.
        "uptime_seconds": None,
        # The hub has no filesystem it will report. Its database size is not a
        # mount, and inventing a disk row from it to keep the card symmetrical
        # would be the one genuinely dishonest thing available here.
        "disks": [],
        "patches": _patches(cfg),
    }


if __name__ == "__main__":
    import os
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    print(json.dumps(collect(json.load(open(path))["hubitat"]), indent=2))
