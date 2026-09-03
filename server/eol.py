"""Is a host's OS still supported? Answered from endoflife.date.

This is a different question from the patch badge, and the reason it exists is
TrueNAS CORE: a box that will never report a pending update again, because
there will never be another one. "0 updates pending" and "abandoned upstream"
look identical from inside the machine.

Nothing about the fleet leaves the network. The only outbound traffic is a
plain GET of a public product file (`/api/v1/products/debian`), fetched on a
slow schedule; the OS strings are matched against it locally. The server never
tells endoflife.date what it runs.

HTTP happens only on the refresh thread, never in a request handler: a
dashboard poll must not block on somebody else's API, and an unreachable
endoflife.date must leave the badge *unknown* rather than the page hanging.
"""

import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime

API = "https://endoflife.date/api/v1/products/%s"

# OS string -> (endoflife.date product, version). The version is whatever the
# collector reported; matching it to a release cycle is done separately, since
# the two are spelled differently on nearly every platform.
_RULES = [
    # Raspbian IS Debian, and reports a Debian version. Must precede the Debian
    # rule only because its own name comes first in the string.
    (re.compile(r"^Raspbian\b.*?(\d[\d.]*)", re.I), "debian"),
    (re.compile(r"^Debian\b.*?(\d[\d.]*)", re.I), "debian"),
    (re.compile(r"^Ubuntu\s+(\d+\.\d+(?:\.\d+)?)", re.I), "ubuntu"),
    (re.compile(r"^Fedora\b.*?(\d+)", re.I), "fedora"),
    (re.compile(r"^Alpine\s+Linux\s+v?(\d[\d.]*)", re.I), "alpine-linux"),
    (re.compile(r"^FreeBSD\s+(\d[\d.]*)", re.I), "freebsd"),
    (re.compile(r"^OpenBSD\s+(\d[\d.]*)", re.I), "openbsd"),
    (re.compile(r"^NetBSD\s+(\d[\d.]*)", re.I), "netbsd"),
    (re.compile(r"^macOS\s+(\d[\d.]*)", re.I), "macos"),
    (re.compile(r"^openSUSE\s+Leap\s+(\d[\d.]*)", re.I), "opensuse"),
]

# Rolling releases have no cycle to go end-of-life. Saying "unknown" for them
# would be a permanent unanswerable question on the card; "rolling" is the
# actual answer.
_ROLLING = [re.compile(p, re.I) for p in (r"^Arch\s+Linux", r"^openSUSE\s+Tumbleweed",
                                          r"^openSUSE\s+Slowroll")]

# Products whose endoflife.date "eoes" (extended) phase is free and applies
# to every install by default, so it is safe to treat as the real end of
# support rather than the "eol" phase before it. See the comment in lookup()
# -- this is not true of every product that has an eoes phase (Ubuntu's is
# paid ESM), so the set is deliberately an allowlist, not a default.
_FREE_EXTENDED_SUPPORT = {"debian"}

# product -> {"ts": fetched_at, "releases": [...]}
_CACHE = {}
_ERRORS = {}


def parse_os(os_string):
    """'Ubuntu 24.04.4 LTS (x86_64)' -> ('ubuntu', '24.04.4')."""
    s = (os_string or "").strip()
    for pat in _ROLLING:
        if pat.search(s):
            return ("rolling", None)
    for pat, product in _RULES:
        m = pat.search(s)
        if m:
            return (product, m.group(1))
    return (None, None)


def match_release(releases, version):
    """Find the cycle a reported version belongs to.

    Platforms spell the same thing differently, so the reported version is
    trimmed component by component until a cycle matches -- longest first:
      Ubuntu  24.04.4 -> 24.04     (cycles are two components)
      NetBSD  11.0    -> 11        (cycles are one)
      macOS   26.6.2  -> 26
      FreeBSD 15.1    -> 15.1      (both "15.1" and "15" exist; the exact one wins)
    """
    by_name = {r.get("name"): r for r in releases or []}
    parts = str(version or "").split(".")
    for n in range(len(parts), 0, -1):
        cand = ".".join(parts[:n])
        if cand in by_name:
            return by_name[cand]
    return None


def _fetch(product, timeout=20):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(API % product, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read())["result"]["releases"]


def refresh(os_strings, cfg):
    """Fetch the products this fleet actually needs. Called off the request path."""
    wanted = set()
    for s in os_strings:
        product, _ = parse_os(s)
        if product and product != "rolling":
            wanted.add(product)
    ttl = int(cfg.get("refresh_hours") or 24) * 3600
    now = time.time()
    for product in sorted(wanted):
        hit = _CACHE.get(product)
        if hit and now - hit["ts"] < ttl:
            continue
        try:
            _CACHE[product] = {"ts": now, "releases": _fetch(product)}
            _ERRORS.pop(product, None)
        except Exception as e:
            # Said once per product, and again only when the error changes: this
            # runs forever on a timer into a journal nothing rotates. A stale
            # cache entry is kept and keeps being used -- release dates change
            # on the scale of months, so yesterday's copy is still right.
            msg = "%s: %s" % (type(e).__name__, str(e)[:150])
            if _ERRORS.get(product) != msg:
                sys.stderr.write("eol: cannot refresh %s: %s\n" % (product, msg))
                _ERRORS[product] = msg


def _override(os_string, overrides):
    """Longest matching prefix from the config wins."""
    best = None
    for prefix, value in (overrides or {}).items():
        if os_string and os_string.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, value)
    return best


def lookup(os_string, cfg, today=None):
    """Cache-only. Returns the shape the UI consumes; never performs I/O."""
    today = today or date.today()
    warn_days = int(cfg.get("warn_days") or 90)

    # An admin's explicit statement beats the database, and is the only way to
    # answer for a product endoflife.date does not track. TrueNAS CORE is the
    # case in point: the `truenas` product covers SCALE only (23.10 onward), so
    # CORE 13.0 has no cycle to match and would otherwise read "unknown"
    # forever -- on the very box whose abandonment prompted all this.
    ov = _override(os_string, cfg.get("overrides"))
    if ov:
        prefix, value = ov
        eol_date = None if value is True else str(value)
        is_eol = True if value is True else str(value) <= today.isoformat()
        return {"status": "eol" if is_eol else "supported", "source": "config",
                "product": prefix, "cycle": None, "eol_date": eol_date,
                "days_left": None}

    product, version = parse_os(os_string)
    if product == "rolling":
        return {"status": "rolling", "source": "rolling", "product": None,
                "cycle": None, "eol_date": None, "days_left": None}
    if not product:
        return _unknown()

    hit = _CACHE.get(product)
    if not hit:
        return _unknown(product)
    rel = match_release(hit["releases"], version)
    if not rel:
        return _unknown(product)

    # endoflife.date models a further support phase after "eol" via
    # isEoes/eoesFrom for several products, but what that phase actually *is*
    # differs by vendor, so trusting it is not safe in general. Debian's is
    # its own LTS Team taking over from the Security Team, free and on by
    # default for every install -- eolFrom there marks a handover, not an
    # ending (https://www.debian.org/News/2026/20260712), so eoesFrom is the
    # boundary that actually means "abandoned". Ubuntu's eoes is Extended
    # Security Maintenance: paid, opt-in via Ubuntu Pro, and absent on a
    # stock install -- trusting it there would call an unpatched-since-2025
    # box "supported" until 2030. Anything not in this set keeps using
    # eolFrom/isEol, which undersells support at worst rather than oversells
    # it -- the safer direction to be wrong in for a product not checked here.
    if product in _FREE_EXTENDED_SUPPORT and rel.get("eoesFrom"):
        is_eol, eol_from = bool(rel.get("isEoes")), rel.get("eoesFrom")
    else:
        is_eol, eol_from = bool(rel.get("isEol")), rel.get("eolFrom")

    # isEol/isEoes are precomputed upstream, so no date arithmetic is needed
    # for the verdict itself -- only for "how long left".
    if is_eol:
        status, days = "eol", None
    elif not eol_from:
        # Known cycle, no announced end date -- macOS and NetBSD are both like
        # this. Supported, with nothing to count down to.
        status, days = "supported", None
    else:
        days = (datetime.strptime(eol_from, "%Y-%m-%d").date() - today).days
        status = "eol_soon" if days <= warn_days else "supported"
    return {"status": status, "source": "endoflife.date", "product": product,
            "cycle": rel.get("name"), "eol_date": eol_from, "days_left": days}


def _unknown(product=None):
    return {"status": "unknown", "source": None, "product": product,
            "cycle": None, "eol_date": None, "days_left": None}
