"""SQLite storage for netdash. Rolling window only -- no long-term retention."""

import re
import sqlite3
import threading
import time
import os

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    host            TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    os              TEXT,
    source          TEXT    NOT NULL DEFAULT 'push',
    cpu_pct         REAL,
    mem_used_bytes  INTEGER,
    mem_total_bytes INTEGER,
    -- Memory as a percentage, for sources that report one and no byte counts.
    -- The UniFi API gives memoryUtilizationPct and never a total, so the ratio
    -- the dashboard normally derives from used/total cannot be computed. NULL
    -- for everything that does report bytes -- summarize() prefers this when
    -- it is set and falls back to the ratio, so the two never disagree.
    mem_pct         REAL,
    uptime_seconds  INTEGER,
    -- Patch status, all nullable: a host whose netdash-patchcheck has never run
    -- reports nothing here and reads as "unknown". patch_security is null
    -- rather than 0 when the platform cannot classify updates at all (Alpine,
    -- Arch without arch-audit, openSUSE Tumbleweed) -- see PATCH-CHECKS.md.
    patch_security     INTEGER,
    patch_other        INTEGER,
    patch_checked_at   INTEGER,
    patch_source       TEXT,
    patch_detail       TEXT,
    -- 1 / 0 / NULL. NULL means the host has no way to answer, not "no".
    patch_reboot       INTEGER,
    -- Names of the security-relevant packages, capped by the collector.
    patch_packages     TEXT,
    -- Which netdash-collector produced this sample, for spotting hosts left
    -- behind on an old one. NULL for TrueNAS, which has no collector.
    collector_version  TEXT,
    -- "none" for bare metal, else the hypervisor ("bhyve", "kvm", ...).
    -- NULL means the host could not tell, which is not the same as bare metal.
    virt               TEXT,
    -- Where this push came from, so the reachability prober has an address to
    -- try when the reported hostname does not resolve on the server's side.
    -- Last resort only: behind NAT this is the gateway, and probing the
    -- gateway would report a dead host as alive. See address_for in app.py.
    peer_addr          TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_host_ts ON samples(host, ts DESC);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS disks (
    sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    mount       TEXT    NOT NULL,
    used_bytes  INTEGER,
    total_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_disks_sample ON disks(sample_id);

-- Devices that belong to an appliance rather than to netdash: the switches and
-- access points a UniFi console has adopted.
--
-- They hang off a sample for the same reason disk rows do -- they are something
-- that sample observed, not hosts in their own right. They never push, run no
-- collector, and cannot be probed apart from the console that reports them, so
-- putting them in `samples` would drag five extra rows through known_hosts,
-- latest_per_host, the reachability sweep and the EOL lookups, to be filtered
-- back out again at render time.
--
-- `state` is the controller's word, not our probe: if it is wrong about a
-- switch, so are we. That is a real limit and not a gap -- when the controller
-- itself stops answering, the poll fails and the console's own card goes stale
-- and then down, which is the honest report of "we no longer know".
CREATE TABLE IF NOT EXISTS fleet (
    sample_id          INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    name               TEXT,
    model              TEXT,
    -- "ONLINE" or anything else. Not normalised to a boolean: "PENDING_ADOPTION"
    -- and "OFFLINE" are both not-online and mean different things to a person.
    state              TEXT,
    cpu_pct            REAL,
    mem_pct            REAL,
    uptime_seconds     INTEGER,
    firmware           TEXT,
    firmware_updatable INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fleet_sample ON fleet(sample_id);

-- Downsampled history, kept long after the raw samples behind it are gone.
--
-- Raw samples answer "what is it doing"; these answer "is this normal, and
-- which way is it going". A 52-second sample rate is the right resolution for
-- the first question and pointless for the second -- keeping it for a year
-- would cost 5.6 GB to say what 40 MB says just as well.
--
-- Two periods rather than one, because the metrics move at different speeds.
-- CPU and memory are volatile and their intra-day shape is the information, so
-- they roll up hourly. Disk moves slowly and the useful question about it is a
-- slope measured in days, so it rolls up daily -- which is also what keeps this
-- small: 76 mounts daily is 28k rows a year, hourly would be 666k.
--
-- min/avg/max, not just avg: an hour that averaged 40% CPU and an hour that
-- alternated between 5% and 95% are different facts about a machine, and an
-- average alone erases the second one entirely.
CREATE TABLE IF NOT EXISTS rollups (
    host    TEXT    NOT NULL,
    -- Unix ts of the start of the hour, UTC. Integer arithmetic, no calendar.
    bucket  INTEGER NOT NULL,
    samples INTEGER NOT NULL,
    cpu_min REAL, cpu_avg REAL, cpu_max REAL,
    mem_min REAL, mem_avg REAL, mem_max REAL,
    PRIMARY KEY (host, bucket)
);
CREATE INDEX IF NOT EXISTS idx_rollups_bucket ON rollups(bucket);

CREATE TABLE IF NOT EXISTS disk_rollups (
    host        TEXT    NOT NULL,
    mount       TEXT    NOT NULL,
    -- Unix ts of the start of the day, UTC.
    bucket      INTEGER NOT NULL,
    samples     INTEGER NOT NULL,
    pct_avg     REAL,
    pct_max     REAL,
    used_avg    REAL,
    total_bytes INTEGER,
    PRIMARY KEY (host, mount, bucket)
);
CREATE INDEX IF NOT EXISTS idx_disk_rollups_bucket ON disk_rollups(bucket);

-- State transitions. Tiny, and the only part of netdash with a memory.
--
-- Everything else here is a photograph of now: REACH lives in memory and dies
-- on restart, and samples age out in a day. A host that drops for four minutes
-- every night at 03:00 was, until this table, completely invisible -- nothing
-- recorded that it had happened and nothing could be asked about it afterwards.
--
-- `kind` is deliberately open rather than a status enum. A WAN link flapping or
-- an access point dropping off a controller are the same sort of fact and
-- belong in the same log.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    host       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_host ON events(host, ts DESC);

-- A silenced patch-security item: one row per acknowledged package, not one
-- per host. Still narrow -- it acknowledges "this package's pending security
-- issue", never the host in general -- but narrow along the axis that actually
-- moves. The whole-state ack this replaces was keyed on the security count and
-- the whole package list together, so on a FreeBSD box carrying a months-old
-- pkg audit hit on python312, every unrelated package that turned up
-- vulnerable and was then fixed made that reviewed-and-understood python312 ack
-- lapse, and it had to be made again over a decision nothing had changed about.
--
-- Keyed on the name exactly as the check reports it, version and all: a package
-- that changes underneath an ack is genuinely new information about the thing
-- that was reviewed, and gets to surface again.
--
-- The host reads as acknowledged only when every pending item is acked, so a
-- newly vulnerable package still turns the badge red on its own -- without
-- disturbing the acks already made around it.
CREATE TABLE IF NOT EXISTS patch_acks (
    host      TEXT    NOT NULL,
    package   TEXT    NOT NULL,
    acked_at  INTEGER NOT NULL,
    PRIMARY KEY (host, package)
);

-- A silenced end-of-life warning, one per host. Narrow in the same way and
-- for the same reason as patch_acks, but the state it pins down is different:
-- the phase, the release, and the date.
--
-- Recording the phase is what makes "EOL soon" and "past EOL" two separate
-- decisions. Acknowledging "Debian 12 goes EOL in three weeks" is a statement
-- that you know and have a plan; it is emphatically not consent to be silent
-- on the day it actually goes unsupported, which is a materially worse fact
-- about the machine and deserves to interrupt again. Recording the cycle and
-- date covers the rest: an upgrade moves the host to a release nobody
-- acknowledged, and upstream moving a date is new information about a
-- decision that was made against the old one.
CREATE TABLE IF NOT EXISTS eol_acks (
    host      TEXT    PRIMARY KEY,
    status    TEXT    NOT NULL,
    product   TEXT    NOT NULL,
    cycle     TEXT    NOT NULL,
    eol_date  TEXT    NOT NULL,
    acked_at  INTEGER NOT NULL
);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently
# does nothing on an existing database, so a deploy onto a live box would keep
# the old table and every insert would fail on the unknown column.
_ADDED_COLUMNS = {
    "samples": [
        ("patch_security", "INTEGER"),
        ("patch_other", "INTEGER"),
        ("patch_checked_at", "INTEGER"),
        ("patch_source", "TEXT"),
        ("patch_detail", "TEXT"),
        ("patch_reboot", "INTEGER"),
        ("patch_packages", "TEXT"),
        ("collector_version", "TEXT"),
        ("mem_pct", "REAL"),
        ("virt", "TEXT"),
        ("peer_addr", "TEXT"),
    ],
}


def _migrate(conn):
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


# The tail the collectors append once the name list is capped: "openssl, zlib1g
# (+3 more)". See "Naming the packages" in PATCH-CHECKS.md for why it is capped.
# Not anchored to the end of the string: FreeBSD's base-system branch appends
# its staged patch to the list after the cap has already been applied, so the
# tail can land in the middle.
_MORE_RE = re.compile(r"\s*\(\+(\d+) more\)\s*")

# What cap_names() joins names with. A comma alone is part of a name.
_JOIN_RE = re.compile(r",\s+")


def patch_entries(packages, security):
    """The individual things an ack can be made against, in the order reported.

    A check reports one string -- "openssl, zlib1g, python312-3.12.14" -- and an
    ack is now made against one package at a time, so that string has to come
    apart the same way everywhere it is used: the read that decides whether a
    host is silenced, the write that records an ack, and the prune that drops
    acks for packages nobody is waiting on any more.

    The capped tail is an entry in its own right, and the count is part of its
    name. Six names are all a collector will ever send, so on a host with nine
    vulnerable packages the other three can only be acknowledged as a group --
    keyed on how many there are, so that group ack lapses the moment the number
    moves. That is exactly as strong as the whole-state ack this replaces, and
    it is the only part of this that still is. A platform that classifies a
    count but names nothing (a staged base update, an unparsed check) lands in
    the same group for the same reason.
    """
    text = (packages or "").strip()
    m = _MORE_RE.search(text)
    if m:
        rest = text[m.end():]
        text = text[:m.start()] + (", " + rest if rest else "")
    # Split on the ", " the collectors join with, never on a bare comma: a
    # FreeBSD package name carries its PORTEPOCH as one, and a live host had
    # exactly that -- "gimp-2.10.38,2" came apart into a package and a stray
    # "2", each separately acknowledgeable and neither meaning anything.
    names = [n.strip() for n in _JOIN_RE.split(text) if n.strip()]
    unnamed = int(m.group(1)) if m else 0
    if security:
        unnamed = max(unnamed, int(security) - len(names))
    if unnamed > 0:
        names.append("(+%d more)" % unnamed)
    return names


def patch_entry_unnamed(entry):
    """How many unnamed packages an entry stands for, or None for a real name."""
    m = _MORE_RE.fullmatch(entry or "")
    return int(m.group(1)) if m else None


def _migrate_patch_acks(conn):
    """Carry whole-host acks over to the per-package table.

    CREATE TABLE IF NOT EXISTS is silent on a database that already has the old
    one, so a deploy onto a live box would otherwise keep the (host, security,
    packages) shape and every ack would fail on the unknown column. Splitting
    the stored list is the honest conversion: an admin who acknowledged three
    packages as one state did review those three packages, and losing that would
    turn every acknowledged host red on the first deploy for no reason.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(patch_acks)")}
    if not cols or "package" in cols:
        return
    old = conn.execute("SELECT host, security, packages, acked_at FROM patch_acks").fetchall()
    rows = [
        (r["host"], pkg, r["acked_at"])
        for r in old
        for pkg in patch_entries(r["packages"], r["security"])
    ]
    conn.execute("DROP TABLE patch_acks")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO patch_acks (host, package, acked_at) VALUES (?,?,?)", rows
    )


# One connection is shared by every request thread (check_same_thread=False), so
# a write that spans more than one statement has to be serialised by hand.
# sqlite3's own locking makes each statement atomic, but nothing stops another
# thread from slipping a statement between two of ours -- and insert_sample
# reads lastrowid, a *connection*-level value, after its INSERT. Two collectors
# posting in the same second (cron fires them all at :01) raced there: the
# second INSERT moved lastrowid before the first thread had read it, so one
# sample's disk rows were written against the other sample's id. The dashboard
# showed "no mounts reported" for the robbed host and a doubled mount list for
# the other. Held across the commit too: a commit from another thread would
# otherwise end our transaction early, publishing a sample with only some of
# its disks attached.
_WRITE = threading.Lock()


def connect(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _migrate_patch_acks(conn)
    conn.commit()
    return conn


def insert_sample(conn, payload, source="push", peer=None):
    """payload: dict from a collector. Returns the new sample id."""
    ts = int(payload.get("ts") or time.time())
    # A collector that has never run a patch check omits the key entirely, and
    # one whose state file was unreadable sends null. Both mean "unknown", so
    # both land as nulls rather than zeros -- a zero here would read on the
    # dashboard as "checked, and up to date".
    p = payload.get("patches") or {}
    if not isinstance(p, dict):
        p = {}
    with _WRITE:
        cur = conn.execute(
            """INSERT INTO samples
                 (host, ts, os, source, cpu_pct, mem_used_bytes, mem_total_bytes, mem_pct,
                  uptime_seconds,
                  patch_security, patch_other, patch_checked_at, patch_source, patch_detail,
                  patch_reboot, patch_packages, collector_version, virt, peer_addr)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["host"],
                ts,
                payload.get("os"),
                source,
                payload.get("cpu_pct"),
                payload.get("mem_used_bytes"),
                payload.get("mem_total_bytes"),
                payload.get("mem_pct"),
                payload.get("uptime_seconds"),
                p.get("security"),
                p.get("other"),
                p.get("checked_at"),
                p.get("source"),
                p.get("detail") or None,
                None if p.get("reboot_required") is None else int(bool(p["reboot_required"])),
                p.get("packages") or None,
                payload.get("collector_version"),
                payload.get("virt"),
                peer,
            ),
        )
        sid = cur.lastrowid
        for d in payload.get("disks") or []:
            conn.execute(
                "INSERT INTO disks (sample_id, mount, used_bytes, total_bytes) VALUES (?,?,?,?)",
                (sid, d.get("mount"), d.get("used_bytes"), d.get("total_bytes")),
            )
        for f in payload.get("fleet") or []:
            conn.execute(
                """INSERT INTO fleet (sample_id, name, model, state, cpu_pct, mem_pct,
                                      uptime_seconds, firmware, firmware_updatable)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (sid, f.get("name"), f.get("model"), f.get("state"), f.get("cpu_pct"),
                 f.get("mem_pct"), f.get("uptime_seconds"), f.get("firmware"),
                 None if f.get("firmware_updatable") is None
                 else int(bool(f["firmware_updatable"]))),
            )
        conn.commit()
        return sid


def prune(conn, retention_hours, rollup_days=None, event_days=None):
    """Drop raw samples past the window, and rollups past a much longer one.

    Three tiers with three lifetimes, so "how long do we keep data" stops being
    one number that has to be wrong for something. Raw is expensive and only
    interesting recently; rollups are cheap and only interesting over months;
    events are almost free and are the one thing worth keeping indefinitely,
    which is what `event_days=None` means.
    """
    with _WRITE:
        cutoff = int(time.time()) - retention_hours * 3600
        conn.execute(
            "DELETE FROM disks WHERE sample_id IN (SELECT id FROM samples WHERE ts < ?)",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM fleet WHERE sample_id IN (SELECT id FROM samples WHERE ts < ?)",
            (cutoff,),
        )
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        # An ack for a host that has since aged out of the rolling window entirely
        # is not "still silencing something", it is a leftover -- and if the name
        # is ever reused, one that would misapply to whatever that new host reports.
        conn.execute(
            "DELETE FROM patch_acks WHERE host NOT IN (SELECT DISTINCT host FROM samples)"
        )
        # And an ack for a package that is no longer pending is not silencing
        # anything either -- it is a landmine. Left in place, a python312 fixed
        # in March would come back vulnerable in July already silenced, by a
        # decision made about a different advisory. Only hosts whose latest
        # sample actually carries a check are cleaned: a collector that stopped
        # reporting patches has not told us anything about what is pending, and
        # dropping its acks on that silence would un-silence the lot the moment
        # it came back.
        for s in latest_per_host(conn):
            if s["patch_checked_at"] is None:
                continue
            keep = patch_entries(s["patch_packages"], s["patch_security"])
            if keep:
                conn.execute(
                    "DELETE FROM patch_acks WHERE host=? AND package NOT IN (%s)"
                    % ",".join("?" * len(keep)),
                    [s["host"]] + keep,
                )
            else:
                conn.execute("DELETE FROM patch_acks WHERE host=?", (s["host"],))
        conn.execute(
            "DELETE FROM eol_acks WHERE host NOT IN (SELECT DISTINCT host FROM samples)"
        )
        if rollup_days:
            rcut = int(time.time()) - int(rollup_days) * 86400
            conn.execute("DELETE FROM rollups WHERE bucket < ?", (rcut,))
            conn.execute("DELETE FROM disk_rollups WHERE bucket < ?", (rcut,))
        if event_days:
            conn.execute("DELETE FROM events WHERE ts < ?",
                         (int(time.time()) - int(event_days) * 86400,))
        conn.commit()


HOUR, DAY = 3600, 86400


def rollup(conn, window_hours):
    """Fold every raw sample still on hand into its hour and day buckets.

    Recomputes the whole raw window on each run rather than tracking a
    watermark. That costs one pass over ~100k rows, which SQLite does in
    milliseconds, and buys the property that matters: it is idempotent and
    self-healing. A missed run, a restart mid-hour, a clock step, a bucket
    written from a partly-filled hour -- all of them correct themselves on the
    next pass, because every bucket in the window is rewritten from whatever
    raw data currently backs it.

    INSERT OR REPLACE rather than INSERT: the current hour is necessarily
    incomplete when it is first written, and gets replaced by the full hour on
    a later run. Over a 48-hour raw window every bucket is rewritten dozens of
    times before the samples behind it age out, so what finally remains is
    always a complete bucket.
    """
    since = int(time.time()) - int(window_hours) * HOUR
    with _WRITE:
        conn.execute(
            """INSERT OR REPLACE INTO rollups
                 (host, bucket, samples, cpu_min, cpu_avg, cpu_max,
                  mem_min, mem_avg, mem_max)
               SELECT host, ts - (ts % ?), COUNT(*),
                      MIN(cpu_pct), AVG(cpu_pct), MAX(cpu_pct),
                      MIN(pct), AVG(pct), MAX(pct)
                 FROM (SELECT host, ts, cpu_pct,
                              COALESCE(mem_pct,
                                       CASE WHEN mem_total_bytes > 0
                                            THEN 100.0 * mem_used_bytes / mem_total_bytes
                                       END) AS pct
                         FROM samples WHERE ts >= ?)
                GROUP BY host, ts - (ts % ?)""",
            (HOUR, since, HOUR),
        )
        conn.execute(
            """INSERT OR REPLACE INTO disk_rollups
                 (host, mount, bucket, samples, pct_avg, pct_max, used_avg, total_bytes)
               SELECT s.host, d.mount, s.ts - (s.ts % ?), COUNT(*),
                      AVG(100.0 * d.used_bytes / d.total_bytes),
                      MAX(100.0 * d.used_bytes / d.total_bytes),
                      AVG(d.used_bytes), MAX(d.total_bytes)
                 FROM samples s JOIN disks d ON d.sample_id = s.id
                WHERE s.ts >= ? AND d.total_bytes > 0 AND d.used_bytes IS NOT NULL
                GROUP BY s.host, d.mount, s.ts - (s.ts % ?)""",
            (DAY, since, DAY),
        )
        conn.commit()


def rollup_series(conn, host, since):
    """Hourly points for one host, shaped like history() so the UI can share code."""
    rows = conn.execute(
        """SELECT bucket AS ts, cpu_avg AS cpu_pct, mem_avg AS mem_pct,
                  cpu_min, cpu_max, mem_min, mem_max, samples
             FROM rollups WHERE host=? AND bucket >= ? ORDER BY bucket ASC""",
        (host, int(since)),
    ).fetchall()
    return [dict(r) for r in rows]


def disk_series(conn, host, since):
    """Daily per-mount points, oldest first."""
    rows = conn.execute(
        """SELECT mount, bucket AS ts, pct_avg, pct_max, used_avg, total_bytes
             FROM disk_rollups WHERE host=? AND bucket >= ? ORDER BY mount, bucket ASC""",
        (host, int(since)),
    ).fetchall()
    return [dict(r) for r in rows]


def disk_history(conn, host, since, bucket=HOUR):
    """Per-mount usage over time, bucketed in SQL rather than shipped per sample.

    Disk is never served at sample resolution, at any range. A mount does not
    move meaningfully inside an hour, so 69 points an hour per mount would be
    the same number repeated -- several hundred kilobytes of JSON, on a page
    that reloads every 30 seconds, to draw a line that is already flat between
    the hours. Bucketing here caps a 48-hour view at 48 points per mount.

    MAX rather than AVG within a bucket: the question a disk chart is asked is
    how close this got, and an average across an hour hides the spike that
    filled it.
    """
    rows = conn.execute(
        """SELECT d.mount AS mount, s.ts - (s.ts % ?) AS ts,
                  MAX(100.0 * d.used_bytes / d.total_bytes) AS pct,
                  MAX(d.used_bytes) AS used_bytes, MAX(d.total_bytes) AS total_bytes
             FROM samples s JOIN disks d ON d.sample_id = s.id
            WHERE s.host = ? AND s.ts >= ? AND d.total_bytes > 0
                  AND d.used_bytes IS NOT NULL
            GROUP BY d.mount, s.ts - (s.ts % ?)
            ORDER BY d.mount, ts ASC""",
        (int(bucket), host, int(since), int(bucket)),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["mount"], []).append(
            {"ts": r["ts"], "pct": r["pct"],
             "used_bytes": r["used_bytes"], "total_bytes": r["total_bytes"]})
    return out


def record_event(conn, host, kind, from_state, to_state, detail=None, ts=None):
    with _WRITE:
        conn.execute(
            """INSERT INTO events (ts, host, kind, from_state, to_state, detail)
               VALUES (?,?,?,?,?,?)""",
            (int(ts or time.time()), host, kind, from_state, to_state, detail),
        )
        conn.commit()


def recent_events(conn, host=None, limit=100, since=None):
    q = "SELECT * FROM events WHERE 1=1"
    args = []
    if host:
        q += " AND host=?"
        args.append(host)
    if since:
        q += " AND ts >= ?"
        args.append(int(since))
    q += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args)]


def last_states(conn):
    """The most recent to_state per host, for seeding the watcher after a restart.

    Without this a restart forgets what every host was last seen doing, and the
    first sweep silently treats whatever it finds as the starting state -- so a
    machine that went down during a deploy would never produce an event at all.
    """
    rows = conn.execute(
        """SELECT host, to_state FROM events e
            WHERE kind='status' AND id = (SELECT MAX(id) FROM events
                                           WHERE host=e.host AND kind='status')"""
    ).fetchall()
    return {r["host"]: r["to_state"] for r in rows}


def latest_per_host(conn):
    rows = conn.execute(
        """SELECT s.* FROM samples s
             JOIN (SELECT host, MAX(ts) AS mts FROM samples GROUP BY host) m
               ON s.host = m.host AND s.ts = m.mts
           GROUP BY s.host
           ORDER BY s.host"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["disks"] = [
            dict(x)
            for x in conn.execute(
                "SELECT mount, used_bytes, total_bytes FROM disks WHERE sample_id=? ORDER BY mount",
                (r["id"],),
            ).fetchall()
        ]
        # Ordered by the controller's own naming rather than by health: a list
        # that reorders itself when a switch goes offline is one you cannot
        # scan for the device you were looking for.
        d["fleet"] = [
            dict(x)
            for x in conn.execute(
                """SELECT name, model, state, cpu_pct, mem_pct, uptime_seconds,
                          firmware, firmware_updatable
                     FROM fleet WHERE sample_id=? ORDER BY name""",
                (r["id"],),
            ).fetchall()
        ]
        out.append(d)
    return out


def history(conn, host, since_seconds):
    cutoff = int(time.time()) - since_seconds
    rows = conn.execute(
        """SELECT id, ts, cpu_pct, mem_used_bytes, mem_total_bytes, mem_pct
             FROM samples WHERE host=? AND ts >= ? ORDER BY ts ASC""",
        (host, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def known_hosts(conn):
    return [r["host"] for r in conn.execute("SELECT DISTINCT host FROM samples ORDER BY host")]


def get_patch_acks(conn, host):
    """{package: acked_at} for every package acknowledged on this host."""
    rows = conn.execute("SELECT package, acked_at FROM patch_acks WHERE host=?", (host,))
    return {r["package"]: r["acked_at"] for r in rows}


def ack_patch(conn, host, packages, now):
    """Silence these pending packages on this host -- each one on its own terms,
    so an ack survives the rest of the list changing. See the note on the table.

    Takes a list rather than one name because "acknowledge all" is one decision
    made at one moment, and writing it as one statement keeps every row in it
    carrying the same timestamp.
    """
    with _WRITE:
        conn.executemany(
            """INSERT INTO patch_acks (host, package, acked_at) VALUES (?,?,?)
               ON CONFLICT(host, package) DO UPDATE SET acked_at=excluded.acked_at""",
            [(host, p, now) for p in packages],
        )
        conn.commit()


def unack_patch(conn, host, package=None):
    """One package back to pending, or the whole host when package is None."""
    with _WRITE:
        if package is None:
            conn.execute("DELETE FROM patch_acks WHERE host=?", (host,))
        else:
            conn.execute("DELETE FROM patch_acks WHERE host=? AND package=?",
                         (host, package))
        conn.commit()


def get_eol_ack(conn, host):
    r = conn.execute("SELECT * FROM eol_acks WHERE host=?", (host,)).fetchone()
    return dict(r) if r else None


def ack_eol(conn, host, status, product, cycle, eol_date, now):
    """Silence the host's current end-of-life phase -- this phase, on this
    release, with this date. See the note on the table."""
    with _WRITE:
        conn.execute(
            """INSERT INTO eol_acks (host, status, product, cycle, eol_date, acked_at)
                 VALUES (?,?,?,?,?,?)
               ON CONFLICT(host) DO UPDATE SET
                 status=excluded.status, product=excluded.product,
                 cycle=excluded.cycle, eol_date=excluded.eol_date,
                 acked_at=excluded.acked_at""",
            (host, status, product or "", cycle or "", eol_date or "", now),
        )
        conn.commit()


def unack_eol(conn, host):
    with _WRITE:
        conn.execute("DELETE FROM eol_acks WHERE host=?", (host,))
        conn.commit()
