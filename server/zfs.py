"""ZFS pool health, from whichever source has it, in one shape.

Two sources, one record per pool:

  * a host collector sends `zpool status -p` as text (zpool_status), and it is
    parsed here, on the server. Parsing on the collector would mean awk on three
    platforms and a redeploy to every ZFS host for each fix; the text is a few
    kilobytes, and only the parsed record is kept.
  * the TrueNAS poller reads /pool and /boot/get_state, which already carry
    the same facts as JSON. pool_from_truenas maps them.

A pool record:

  {"name": "tank", "state": "ONLINE", "boot": False,
   "read_errors": 0, "write_errors": 0, "checksum_errors": 0,
   "data_errors": 0,                 # permanent errors; None when not reported
   "bad_devices": [{"name": "ada3", "state": "FAULTED"}],
   "scan": {"function": "SCRUB", "state": "FINISHED", "start": epoch,
            "end": epoch, "errors": 0} or None when never scanned,
   "detail": "One or more devices ..." or None}

Error counts are summed over leaf devices only. zpool also prints counts on
the vdev and pool lines, and adding those in would count one bad sector two or
three times.
"""

import re

# Device states that are not a working member. Spares read AVAIL or INUSE,
# which are neither, and are handled by their section.
GOOD = ("ONLINE",)
_STATES = ("ONLINE", "DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED",
           "SUSPENDED", "AVAIL", "INUSE", "SPLIT")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

# "Sun Oct 26 03:01:23 2026", ctime's shape, which zpool always prints in.
_DATE_RE = re.compile(r"[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+"
                      r"(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})")


def _tz_seconds(tz):
    """'+0100' -> 3600. Anything else is UTC."""
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", str(tz or "").strip())
    if not m:
        return 0
    s = int(m.group(2)) * 3600 + int(m.group(3)) * 60
    return -s if m.group(1) == "-" else s


def _epoch(text, tz):
    """The first ctime-style date in text, read in the host's zone, or None.

    Parsed by hand rather than with strptime, whose %a and %b follow the
    server's locale; zpool's dates are the collector's C locale.
    """
    m = _DATE_RE.search(text or "")
    if not m or m.group(1) not in _MONTHS:
        return None
    import calendar
    t = calendar.timegm((int(m.group(6)), _MONTHS[m.group(1)], int(m.group(2)),
                         int(m.group(3)), int(m.group(4)), int(m.group(5)), 0, 0, 0))
    return t - _tz_seconds(tz)


def _count(s):
    """An error column: '0', '12', or without -p, '1.2K'."""
    m = re.match(r"^(\d+(?:\.\d+)?)([KMGTPE]?)$", str(s))
    if not m:
        return 0
    mult = 1000 ** ("KMGTPE".index(m.group(2)) + 1) if m.group(2) else 1
    return int(float(m.group(1)) * mult)


def _scan(text, tz):
    """The `scan:` paragraph of one pool -> the scan record, or None."""
    t = " ".join((text or "").split())
    if not t or t.startswith("none requested"):
        return None
    out = {"function": None, "state": None, "start": None, "end": None, "errors": None}
    if t.startswith("scrub") or t.startswith("resilver"):
        out["function"] = "SCRUB" if t.startswith("scrub") else "RESILVER"
    else:
        # "error scrub ..." (OpenZFS 2.2) and anything newer than this parser.
        return None
    if " in progress since " in t:
        out["state"] = "SCANNING"
        out["start"] = _epoch(t.split(" in progress since ", 1)[1], tz)
    elif " paused since " in t:
        out["state"] = "PAUSED"
        out["start"] = _epoch(t.split(" paused since ", 1)[1], tz)
    elif " canceled on " in t:
        out["state"] = "CANCELED"
        out["end"] = _epoch(t.split(" canceled on ", 1)[1], tz)
    else:
        out["state"] = "FINISHED"
        out["end"] = _epoch(t.rsplit(" on ", 1)[-1], tz) if " on " in t else None
    m = re.search(r" with (\d+) errors?", t)
    if m:
        out["errors"] = int(m.group(1))
    return out


def _config(lines):
    """The device tree under `config:` -> (leaf totals, bad devices).

    Rows are "NAME STATE READ WRITE CKSUM [note]", nested by indentation. The
    first row is the pool itself; logs, cache, special, dedup and spares are
    section headers with no state of their own. A spare that is AVAIL is not a
    bad device; one that is anything but AVAIL or INUSE is.
    """
    rows = []
    for ln in lines:
        if not ln.strip() or ln.strip().startswith("NAME "):
            continue
        indent = len(ln) - len(ln.lstrip())
        f = ln.split()
        rows.append((indent, f))
    tot = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0}
    bad = []
    section = None
    for i, (indent, f) in enumerate(rows):
        if len(f) == 1 or (len(f) > 1 and f[1] not in _STATES):
            section = f[0]           # logs / cache / special / dedup / spares
            continue
        name, state = f[0], f[1]
        if i == 0:
            continue                 # the pool's own row; its state is `state:`
        leaf = i + 1 >= len(rows) or rows[i + 1][0] <= indent
        if section == "spares":
            if state not in ("AVAIL", "INUSE"):
                bad.append({"name": name, "state": state, "leaf": True})
            continue
        if state not in GOOD:
            bad.append({"name": name, "state": state, "leaf": leaf})
        if leaf and len(f) >= 5:
            tot["read_errors"] += _count(f[2])
            tot["write_errors"] += _count(f[3])
            tot["checksum_errors"] += _count(f[4])
    return tot, _culprits(bad)


def _culprits(bad):
    """The devices to name. A mirror is DEGRADED because a disk under it is
    FAULTED, and naming both says one thing twice: the disk is what gets
    replaced. A vdev is named only when nothing under it is."""
    leaves = [b for b in bad if b["leaf"]]
    return [{"name": b["name"], "state": b["state"]} for b in (leaves or bad)]


def parse_zpool_status(text, tz=None):
    """`zpool status [-p]` -> a list of pool records. Never raises.

    tz is the collector's `date +%z`: zpool prints its dates in the host's
    local time with no zone, and without it a scrub's age could be out by
    the host's offset from UTC.
    """
    pools = []
    if not text:
        return pools
    # One block per "pool:" line. Each block is "key: value" paragraphs, a
    # value continuing on lines indented past the key, then config and errors.
    blocks = re.split(r"(?m)^\s*pool: ", str(text))
    for blk in blocks[1:]:
        try:
            lines = blk.splitlines()
            rec = {"name": lines[0].strip(), "state": None, "boot": False,
                   "read_errors": 0, "write_errors": 0, "checksum_errors": 0,
                   "data_errors": None, "bad_devices": [], "scan": None,
                   "detail": None}
            fields, key = {}, None
            cfg_lines = []
            for ln in lines[1:]:
                m = re.match(r"^\s*([a-z]+): ?(.*)$", ln)
                if m and m.group(1) in ("state", "status", "action", "see",
                                        "scan", "config", "errors", "remove",
                                        "checkpoint", "comment"):
                    key = m.group(1)
                    fields[key] = m.group(2)
                    continue
                if key == "config":
                    cfg_lines.append(ln.expandtabs(8))
                elif key:
                    fields[key] += " " + ln.strip()
            rec["state"] = (fields.get("state") or "").strip().upper() or None
            st = " ".join((fields.get("status") or "").split())
            rec["detail"] = st or None
            rec["scan"] = _scan(fields.get("scan"), tz)
            err = (fields.get("errors") or "").strip()
            if err.startswith("No known data errors"):
                rec["data_errors"] = 0
            else:
                m = re.match(r"^(\d+) data errors?", err)
                if m:
                    rec["data_errors"] = int(m.group(1))
            tot, bad = _config(cfg_lines)
            rec.update(tot)
            rec["bad_devices"] = bad
            if rec["name"] and rec["state"]:
                pools.append(rec)
        except Exception:
            continue
    return pools


def _ms_epoch(v):
    """TrueNAS dates: {"$date": 1791735740000} -> 1791735740."""
    if isinstance(v, dict) and v.get("$date") is not None:
        try:
            return int(v["$date"]) // 1000
        except (TypeError, ValueError):
            return None
    return None


def _walk(node, tot, bad, disks):
    """One device-tree node of a TrueNAS pool, and everything under it."""
    kids = node.get("children") or []
    state = str(node.get("status") or "").upper()
    # /pool names a leaf's disk; /boot/get_state gives only the partition's
    # path, /dev/ada7p2, which is the disk ada7. A vdev has neither and is
    # named for itself (mirror-0) or its type.
    disk = node.get("disk") or (re.sub(
        r"p\d+$", "", str(node.get("path")).rsplit("/", 1)[-1])
        if not kids and node.get("path") else None)
    name = disk or node.get("name") or node.get("type")
    if state and state not in GOOD:
        bad.append({"name": name, "state": state, "leaf": not kids})
    if not kids:
        if disk:
            disks.append(disk)
        s = node.get("stats") or {}
        for k in ("read_errors", "write_errors", "checksum_errors"):
            try:
                tot[k] += int(s.get(k) or 0)
            except (TypeError, ValueError):
                pass
    for c in kids:
        _walk(c, tot, bad, disks)


def pool_from_truenas(p, boot=False):
    """One /pool entry, or /boot/get_state, as a pool record. None if unusable.

    Both endpoints answer the same shape on CORE 13.0 and FreeCORE 15.1
    (verified live): status, healthy, status_detail, a scan block with epoch
    milliseconds, and a device tree with per-device stats. Spares are not
    walked for bad states: an idle spare reads AVAIL.
    """
    if not isinstance(p, dict) or not p.get("name") or not p.get("status"):
        return None
    tot = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0}
    bad, disks = [], []
    # /pool calls the tree `topology`, /boot/get_state calls it `groups`;
    # the nodes inside are the same. Spares are "spare" in both.
    for section, nodes in (p.get("topology") or p.get("groups") or {}).items():
        if section in ("spare", "spares"):
            continue
        for n in nodes or []:
            if isinstance(n, dict):
                _walk(n, tot, bad, disks)
    sc = p.get("scan") or {}
    scan = None
    if sc.get("function") and sc.get("state"):
        scan = {"function": str(sc["function"]).upper(),
                "state": str(sc["state"]).upper(),
                "start": _ms_epoch(sc.get("start_time")),
                "end": _ms_epoch(sc.get("end_time")),
                "errors": sc.get("errors")}
    rec = {"name": p["name"], "state": str(p["status"]).upper(), "boot": bool(boot),
           "data_errors": None, "bad_devices": _culprits(bad), "scan": scan,
           # The disks under it, by device name, so SMART results can be
           # filed under their pool. /disk's own `pool` field is null on
           # both boxes.
           "disks": disks,
           # status_detail is set on a healthy pool too ("Some supported
           # features are not enabled") -- that is a note, not a fault.
           "detail": p.get("status_detail") or None}
    rec.update(tot)
    return rec


# ---------- judging ----------

# 42, not 35. A TrueNAS scrub task's default is "threshold 35 days", checked
# weekly on Sunday: it scrubs only once 35 days have passed, so a pool on the
# stock schedule is routinely 36-41 days since its last scrub. Warning at 35
# would turn every such pool amber for most of a week, every five weeks.
SCRUB_MAX_AGE_DAYS = 42
SMART_MAX_AGE_DAYS = 35

_CRIT_STATES = ("FAULTED", "UNAVAIL", "SUSPENDED", "REMOVED")


def judge_pool(p):
    """(severity, reason) for a pool's state and error counters."""
    state = p.get("state") or ""
    if state in _CRIT_STATES:
        return "critical", "pool " + state
    if p.get("data_errors"):
        # Permanent errors are files ZFS could not repair: data already lost.
        return "critical", "%d permanent data error%s" % (
            p["data_errors"], "" if p["data_errors"] == 1 else "s")
    if state and state not in GOOD:
        return "warning", "pool " + state
    errs = sum(int(p.get(k) or 0) for k in ("read_errors", "write_errors", "checksum_errors"))
    if errs:
        return "warning", "%d device error%s" % (errs, "" if errs == 1 else "s")
    if p.get("bad_devices"):
        return "warning", "%s %s" % (p["bad_devices"][0]["name"], p["bad_devices"][0]["state"])
    return "ok", None


def judge_scrub(scan, now, max_age_days=SCRUB_MAX_AGE_DAYS):
    """(severity, reason, age_days) for a pool's last scan.

    A pool never scrubbed is noted ("unknown"), not coloured: nothing here
    knows when the pool was created, and a pool made yesterday has done
    nothing wrong. A resilver is not a scrub, so it says nothing about age.
    A scrub running now is judged on nothing until it finishes.
    """
    if not scan:
        return "unknown", "never scrubbed", None
    if scan.get("function") != "SCRUB":
        if scan.get("state") == "SCANNING":
            return "ok", "resilver in progress", None
        return "unknown", "no scrub since the last resilver", None
    if scan.get("state") in ("SCANNING", "PAUSED"):
        return "ok", "scrub " + ("in progress" if scan["state"] == "SCANNING" else "paused"), None
    if scan.get("errors"):
        return "warning", "last scrub found %d error%s" % (
            scan["errors"], "" if scan["errors"] == 1 else "s"), None
    end = scan.get("end")
    if not end:
        return "unknown", "scrub time unknown", None
    age = max(0.0, (now - end) / 86400.0)
    if scan.get("state") == "CANCELED":
        if age > max_age_days:
            return "warning", "last scrub canceled %d days ago" % int(age), age
        return "ok", "last scrub canceled", age
    if age > max_age_days:
        return "warning", "last scrub %d days ago" % int(age), age
    return "ok", None, age
