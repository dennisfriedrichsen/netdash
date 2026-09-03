"""Poll a UniFi OS console over the Network Integration API. Nothing is installed.

Auth: an API key, sent as `X-API-KEY`. Created in the Network application under
Settings -> Control Plane -> Integrations, or on UniFi OS 5.x under Settings ->
System -> Advanced -> API. The key inherits the permissions of the account that
created it -- Ubiquiti offers no genuinely read-only key -- so it is worth
making under a dedicated admin rather than your own.

Endpoints (verified live against a UDM Pro, UniFi OS 5.1.31, Network 10.6.101):
  GET /api/system                                 -> the console's own MAC, no auth
  GET  …/integration/v1/sites                     -> site id
  GET  …/sites/{s}/devices                        -> name, model, state, firmware
  GET  …/sites/{s}/devices/{d}/statistics/latest  -> cpu/mem percentages, uptime

Quirks this module works around, all confirmed on the live console:

  * The API reports the gateway's WAN address in `ipAddress` -- 192.168.0.25 on
    a console reached at 10.0.0.1. It is never used for anything. The configured
    host is the only address that means anything for reachability, and feeding
    this one to the prober would have it probing the wrong interface entirely.

  * There are no byte counts anywhere: memory is `memoryUtilizationPct` and
    nothing else. That is why samples carry mem_pct -- see db.py.

  * There is no storage of any kind in this API, so `disks` stays empty. The
    classic /proxy/network/api/s/{site}/stat/device does report a storage array,
    but only to a session logged in with an admin password.

  * Every path under /proxy/network returns 401 when unauthenticated, including
    paths that cannot exist. A 401 therefore says nothing about whether the
    Integration API is enabled -- only that UniFi OS gated the request before
    routing it.

The console identifies itself through the unauthenticated /api/system, whose
`mac` is matched against the device list. That is what makes `device_name`
optional: asking the console who it is beats asking the operator to spell it.
"""

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

_API = "/proxy/network/integration/v1"


class UniFiError(Exception):
    pass


def _ctx(verify_tls):
    c = ssl.create_default_context()
    if not verify_tls:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def _request(cfg, path, timeout=15, auth=True):
    url = "%s://%s%s" % (cfg.get("scheme", "https"), cfg["host"], path)
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if auth:
        req.add_header("X-API-KEY", cfg.get("api_key") or "")
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ctx(cfg.get("verify_tls", False))) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        # Bodies are not echoed. A 401 here is the console telling us the key is
        # wrong, and the response adds nothing the status code has not said.
        if e.code == 401:
            raise UniFiError(
                "GET %s -> HTTP 401: the API key was rejected. Note this is also "
                "what an unauthenticated request gets for any path at all, so it "
                "means the key is wrong, not that the endpoint is missing" % path)
        raise UniFiError("GET %s -> HTTP %s" % (path, e.code))
    except Exception as e:
        raise UniFiError("GET %s -> %s" % (path, e))


def _norm_mac(mac):
    return re.sub(r"[^0-9a-f]", "", str(mac or "").lower())


def _site_id(cfg):
    sites = (_request(cfg, _API + "/sites") or {}).get("data") or []
    if not sites:
        raise UniFiError("the API key is valid but no sites are visible to it")
    want = cfg.get("site")
    if want:
        for s in sites:
            if want in (s.get("internalReference"), s.get("name"), s.get("id")):
                return s["id"]
        raise UniFiError("no site named %r (have: %s)"
                         % (want, ", ".join(str(s.get("internalReference")) for s in sites)))
    for s in sites:
        if s.get("internalReference") == "default":
            return s["id"]
    return sites[0]["id"]


def _console_mac(cfg):
    """The console's own MAC, from the endpoint that needs no key.

    Best effort: if this fails we fall back to naming or model matching rather
    than refusing to report, because a console that cannot answer /api/system
    is still a console whose devices we can read.
    """
    try:
        return _norm_mac((_request(cfg, "/api/system", auth=False) or {}).get("mac"))
    except UniFiError:
        return None


# A gateway is the device the console runs on. Matched by MAC where possible;
# these prefixes are the fallback, and are prefixes rather than exact models so
# a UDM SE or a UCG Fiber is recognised without a table update.
_GATEWAY_MODELS = ("UDM", "UDR", "UCG", "UXG", "UX", "UDW")


def _split(cfg, devices):
    """(the console itself, everything else it has adopted)."""
    want_name = cfg.get("device_name")
    if want_name:
        for i, d in enumerate(devices):
            if d.get("name") == want_name:
                return d, devices[:i] + devices[i + 1:]
        raise UniFiError("no adopted device named %r (have: %s)"
                         % (want_name, ", ".join(str(d.get("name")) for d in devices)))

    mac = _console_mac(cfg)
    if mac:
        for i, d in enumerate(devices):
            if _norm_mac(d.get("macAddress")) == mac:
                return d, devices[:i] + devices[i + 1:]

    for i, d in enumerate(devices):
        if str(d.get("model") or "").upper().startswith(_GATEWAY_MODELS):
            return d, devices[:i] + devices[i + 1:]
    raise UniFiError("cannot tell which adopted device is the console itself; "
                     "set unifi.device_name in config.json")


def _stats(cfg, site, device_id):
    """Live statistics for one device, or {} if it will not say.

    Per device rather than per poll: a device that has just gone offline stops
    answering this while the rest of the fleet is perfectly readable, and losing
    the whole sample to one dead access point would be exactly backwards.
    """
    try:
        return _request(cfg, "%s/sites/%s/devices/%s/statistics/latest"
                        % (_API, site, device_id)) or {}
    except UniFiError:
        return {}


def _pct(v):
    if v is None:
        return None
    try:
        return round(max(0.0, min(100.0, float(v))), 1)
    except (TypeError, ValueError):
        return None


def _patches(device, now):
    """Firmware update state, in the shape the collectors' patch check sends.

    UniFi ships one firmware image per device and classifies nothing, so
    `security` is null rather than 0 -- a 0 would claim the console had been
    checked for CVEs and found clean. See PATCH-CHECKS.md.

    Unlike the TrueNAS and Hubitat checks this needs no slow poll of its own:
    it is the controller's own view, already present on the device call we are
    making anyway, and costs no round trip off the LAN.
    """
    updatable = device.get("firmwareUpdatable")
    if updatable is None:
        return None
    return {
        "security": None,
        "other": 1 if updatable else 0,
        "checked_at": int(now),
        "source": "unifi-controller",
        "detail": ("firmware newer than %s available" % device.get("firmwareVersion")
                   if updatable else
                   "no per-package security classification on UniFi OS"),
    }


def collect(cfg):
    """Return a payload in the same shape the host collectors POST."""
    if not cfg.get("api_key"):
        raise UniFiError("unifi.api_key is not set in config.json")

    site = _site_id(cfg)
    devices = (_request(cfg, "%s/sites/%s/devices" % (_API, site)) or {}).get("data") or []
    if not devices:
        raise UniFiError("the site has no adopted devices")

    console, rest = _split(cfg, devices)
    cs = _stats(cfg, site, console["id"])
    now = time.time()

    fleet = []
    if cfg.get("fleet", True):
        for d in rest:
            s = _stats(cfg, site, d["id"])
            fleet.append({
                "name": d.get("name"),
                "model": d.get("model"),
                "state": d.get("state"),
                "cpu_pct": _pct(s.get("cpuUtilizationPct")),
                "mem_pct": _pct(s.get("memoryUtilizationPct")),
                "uptime_seconds": s.get("uptimeSec"),
                "firmware": d.get("firmwareVersion"),
                "firmware_updatable": d.get("firmwareUpdatable"),
            })

    return {
        "host": cfg.get("display_name") or console.get("name") or cfg["host"],
        "ts": int(now),
        "os": ("UniFi OS %s" % (console.get("firmwareVersion") or "")).strip(),
        "cpu_pct": _pct(cs.get("cpuUtilizationPct")),
        # No byte counts anywhere in this API -- see the module docstring.
        "mem_used_bytes": None,
        "mem_total_bytes": None,
        "mem_pct": _pct(cs.get("memoryUtilizationPct")),
        "uptime_seconds": cs.get("uptimeSec"),
        # No storage in this API. The console's disk is real, but reporting a
        # number we do not have would be worse than the blank meter.
        "disks": [],
        "fleet": fleet,
        "patches": _patches(console, now),
    }


if __name__ == "__main__":
    import os
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    print(json.dumps(collect(json.load(open(path))["unifi"]), indent=2))
