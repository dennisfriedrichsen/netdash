"""SQLite storage for netdash. Rolling window only -- no long-term retention."""

import json
import re
import sqlite3
import threading
import time
import os

SCHEMA = """
-- The fleet itself: one row per host netdash has heard from, and the only
-- answer to the question "which machines are we watching".
--
-- Membership used to be derived from `samples`, which quietly made it expire
-- with the data. A host that stopped reporting held its place for
-- retention_hours, and then the pruner deleted its last sample and the host
-- left the dashboard altogether -- silently, and precisely for the hosts that
-- had earned a red card. ubuntu22dot04server locked up, went red for a day,
-- and by the next morning the wall panel showed a complete, all-green fleet
-- with a machine missing from it. Nothing looked wrong, which is the worst
-- thing an outage can manage to look like.
--
-- So this is derived from nothing and expires on no timer. Samples age out
-- underneath it and the row stays, which is what lets a host with no data
-- left still be listed, still be probed, and still read DOWN. A host leaves
-- only when a person says so -- forget_host, from the button on its detail
-- page -- because "this machine is decommissioned" is a fact no amount of
-- silence can establish.
--
-- os, source and peer_addr are copies of the last sample's, kept for the same
-- reason the row is: the card still needs an icon and the prober still needs
-- an address after the sample they came from has gone.
CREATE TABLE IF NOT EXISTS hosts (
    host       TEXT    PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    os         TEXT,
    source     TEXT    NOT NULL DEFAULT 'push',
    peer_addr  TEXT
);

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
    -- Vestigial. The security package names used to ride here on every sample,
    -- capped at six by the collector because of what that cost; they live in
    -- patch_pending now and this is written NULL. Kept only so a rollback onto
    -- the previous server can still read the rows it wrote. See patch_pending.
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
--
-- Each sample is folded in once, as it arrives -- see rollup() -- so a bucket
-- carries running sums beside its averages: the average of an hour cannot be
-- extended by another sample, its sum and count can. `samples` counts every
-- sample in the hour; cpu_n and mem_n count the ones that had a value, which
-- is what the averages divide by.
--
-- coverage says how much of the bucket that is:
--   'full'    folded from the bucket's first sample on
--   'partial' started after some of the bucket's raw samples were pruned,
--             so it holds only what was left (a late sample, a re-enabled
--             history)
--   NULL      written by a server from before this column, whose roller
--             rewrote each bucket from the raw samples left on hand -- so an
--             old one may be just the tail of its hour. Nothing remains that
--             could say whether it is, and nothing here pretends to.
CREATE TABLE IF NOT EXISTS rollups (
    host    TEXT    NOT NULL,
    -- Unix ts of the start of the hour, UTC. Integer arithmetic, no calendar.
    bucket  INTEGER NOT NULL,
    samples INTEGER NOT NULL,
    cpu_min REAL, cpu_avg REAL, cpu_max REAL,
    mem_min REAL, mem_avg REAL, mem_max REAL,
    cpu_sum REAL, cpu_n INTEGER,
    mem_sum REAL, mem_n INTEGER,
    coverage TEXT,
    PRIMARY KEY (host, bucket)
);
CREATE INDEX IF NOT EXISTS idx_rollups_bucket ON rollups(bucket);

-- Daily, per mount. Only samples with a positive total and a used figure
-- count; `samples` is how many did. Each sample's percentage is taken against
-- its own total, so a mount grown mid-day keeps an honest pct_avg; total_bytes
-- is the largest total seen that day and total_min the smallest, and the two
-- differ only on a day the capacity changed.
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
    pct_sum     REAL,
    used_sum    REAL,
    total_min   INTEGER,
    coverage    TEXT,
    PRIMARY KEY (host, mount, bucket)
);
CREATE INDEX IF NOT EXISTS idx_disk_rollups_bucket ON disk_rollups(bucket);

-- Bookkeeping the roller and the pruner share, as integers by name:
--   rollup_folded_id   the highest samples.id already folded into the rollups
--   raw_pruned_before  the latest cutoff prune() has deleted raw samples
--                      before; a bucket starting at or after it still has all
--                      of its raw samples
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT    PRIMARY KEY,
    value INTEGER
);

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
--
-- missing_since is when prune() first found the package gone from the pending
-- list, on the one source that gets a grace period for it -- see ACK_GRACE.
-- NULL while the package is pending, and on every other source.
CREATE TABLE IF NOT EXISTS patch_acks (
    host          TEXT    NOT NULL,
    package       TEXT    NOT NULL,
    acked_at      INTEGER NOT NULL,
    missing_since INTEGER,
    PRIMARY KEY (host, package)
);

-- The security-relevant packages a host's latest check named, one row each.
--
-- These used to ride on every sample as a comma-joined string in
-- samples.patch_packages, which is why they were capped at six names with a
-- "(+N more)" tail: fifty names on a 30-second sample is fifty names written
-- 2,880 times a day. But only the newest row's copy was ever read -- the ack
-- path and the prune both start from latest_per_host -- so the cap was paying
-- for storage nothing looked at, and charging an admin the difference: the
-- packages past the sixth could only be acknowledged as an anonymous group,
-- keyed on how many of them there were.
--
-- That group was not merely unreadable, it was unsound. Which packages fell
-- into it is positional -- the checks emit backend order and none of them
-- sort -- so the count could hold steady while the membership changed, and an
-- ack made about one set of packages would silently cover another. That is the
-- landmine prune() exists to defuse, reintroduced inside the one entry nobody
-- could inspect.
--
-- Keyed per host rather than per sample because that is the rate the data
-- actually moves at: the check runs daily, the sample every thirty seconds.
CREATE TABLE IF NOT EXISTS patch_pending (
    host     TEXT    NOT NULL,
    ord      INTEGER NOT NULL,
    package  TEXT    NOT NULL,
    PRIMARY KEY (host, package)
);
CREATE INDEX IF NOT EXISTS idx_patch_pending ON patch_pending(host, ord);

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

-- Jails as their host reports them: which machine runs each one, and whether
-- it is running. Written by the parent on every report -- a FreeBSD collector
-- from jls, a TrueNAS box from its jail list -- and read by summarize(), which
-- is what turns a stopped jail grey instead of red.
--
-- Its own table rather than columns on samples, because it outlives them in
-- both directions. A jail with its own collector keeps pushing its own
-- samples, and the parent's report must still say where it runs without
-- writing a second, emptier sample beside them. And a jail that is stopped
-- has no samples worth taking at all; what is known about it is this row.
--
-- state is "running", "stopped", or "absent": in the parent's previous report
-- and not in this one. On a plain FreeBSD host jls only lists running jails,
-- so a jail that stops simply vanishes from the report -- absent is how that
-- reads as stopped rather than as a jail that went silent for no reason.
-- seen_at is when the parent last said anything about it; a parent that goes
-- quiet stops vouching for its jails, and they fall back to their own samples.
CREATE TABLE IF NOT EXISTS guests (
    host     TEXT    PRIMARY KEY,
    parent   TEXT    NOT NULL,
    state    TEXT    NOT NULL,
    boot     INTEGER,
    address  TEXT,
    seen_at  INTEGER NOT NULL
);

-- ZFS pool health and SMART results, the latest report only, one row a host.
--
-- Not per sample like disks: a pool's state changes a few times in its life,
-- and copying the same "ONLINE, scrubbed Sunday" into every sample of the
-- window would store a million rows to say one thing. Transitions are what
-- deserve keeping, and the event log keeps them (kind "storage:...").
--
-- pools and smart are JSON, in the record shapes zfs.py documents. smart is
-- NULL for a host that has no source of it -- every host collector, for now
-- -- which is "unknown", not "no disks". ts is the sample that carried them,
-- so summarize() can ignore a row the host has stopped refreshing.
CREATE TABLE IF NOT EXISTS storage (
    host   TEXT    PRIMARY KEY,
    ts     INTEGER NOT NULL,
    pools  TEXT,
    smart  TEXT
);
"""


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently
# does nothing on an existing database, so a deploy onto a live box would keep
# the old table and every insert would fail on the unknown column.
_ADDED_COLUMNS = {
    "patch_acks": [
        ("missing_since", "INTEGER"),
    ],
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
    "rollups": [
        ("cpu_sum", "REAL"),
        ("cpu_n", "INTEGER"),
        ("mem_sum", "REAL"),
        ("mem_n", "INTEGER"),
        ("coverage", "TEXT"),
    ],
    "disk_rollups": [
        ("pct_sum", "REAL"),
        ("used_sum", "REAL"),
        ("total_min", "INTEGER"),
        ("coverage", "TEXT"),
    ],
}


def _migrate(conn):
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


def _migrate_hosts(conn):
    """Seed the host register from the samples a live database already holds.

    Every host in the rolling window is a host we are watching, so the first
    connect after this ships has to say so -- otherwise an upgrade would show
    an empty dashboard until every collector had pushed again.

    Hosts whose samples were already pruned cannot be recovered here. Nothing
    is left that says they existed except their events, and resurrecting a
    machine that was deliberately decommissioned a year ago is worse than the
    gap: those come back on their next push, or by hand with register_host.
    """
    have = {r["host"] for r in conn.execute("SELECT host FROM hosts")}
    rows = conn.execute(
        """SELECT s.host, s.ts, s.os, s.source, s.peer_addr,
                  (SELECT MIN(ts) FROM samples WHERE host = s.host) AS first_seen
             FROM samples s
             JOIN (SELECT host, MAX(ts) AS mts FROM samples GROUP BY host) m
               ON s.host = m.host AND s.ts = m.mts
         GROUP BY s.host"""
    ).fetchall()
    for r in rows:
        if r["host"] in have:
            continue
        conn.execute(
            """INSERT INTO hosts (host, first_seen, last_seen, os, source, peer_addr)
               VALUES (?,?,?,?,?,?)""",
            (r["host"], r["first_seen"], r["ts"], r["os"],
             r["source"] or "push", r["peer_addr"]),
        )


# The tail older collectors appended once the name list was capped: "openssl,
# zlib1g (+3 more)". Nothing emits it any more -- the cap is gone, and every
# security package is named -- but a collector upgrades on its own schedule and
# netdash has to keep reading whatever the fleet is still sending. Stripped on
# the way in rather than stored: the count is not a package, and making it an
# ack target is the bug this replaced.
#
# Not anchored to the end of the string: FreeBSD's base-system branch appended
# its staged patch after the cap had already been applied, so on those payloads
# the tail can land in the middle.
_MORE_RE = re.compile(r"\s*\(\+(\d+) more\)\s*")

# What the collectors join names with. A comma alone is part of a name.
_JOIN_RE = re.compile(r",\s+")


def split_packages(packages):
    """The package names in a check's reported string, in the order reported.

    A check reports one string -- "openssl, zlib1g, python312-3.12.14" -- and an
    ack is made against one package at a time, so that string has to come apart
    the same way everywhere it is used.

    Split on the ", " the collectors join with, never on a bare comma: a FreeBSD
    package name carries its PORTEPOCH as one, and a live host had exactly that
    -- "gimp-2.10.38,2" came apart into a package and a stray "2", each
    separately acknowledgeable and neither meaning anything.
    """
    text = _MORE_RE.sub(", ", (packages or "").strip())
    return [n.strip() for n in _JOIN_RE.split(text) if n.strip()]


def _set_patch_pending(conn, host, packages):
    """Replace the host's pending security package list. Caller holds _DB.

    Rewritten only when it actually differs. The list arrives on every sample,
    once or twice a minute, and is the same string all day -- the check behind
    it runs daily. Comparing first turns 2,880 pointless rewrites a day per
    host into the handful that mean something.
    """
    names = split_packages(packages)
    have = [r["package"] for r in conn.execute(
        "SELECT package FROM patch_pending WHERE host=? ORDER BY ord", (host,))]
    if have == names:
        return
    conn.execute("DELETE FROM patch_pending WHERE host=?", (host,))
    conn.executemany(
        "INSERT INTO patch_pending (host, ord, package) VALUES (?,?,?)",
        [(host, i, n) for i, n in enumerate(names)],
    )


def patch_pending(conn, host):
    """The security packages this host's latest check named, in report order."""
    with _DB:
        return [r["package"] for r in conn.execute(
            "SELECT package FROM patch_pending WHERE host=? ORDER BY ord", (host,))]


def cpu_floor(conn, host, ts, seconds):
    """The lowest CPU reading this host has had over the `seconds` up to `ts`.

    None when the host has not been reporting that long, so a first sample can
    never count as a condition that has persisted. The window reaches back to
    the last sample AT OR BEFORE its start -- the reading that was in effect
    when it opened -- rather than only to samples inside it: a collector on a
    60s cadence may have nothing that lands exactly on the boundary, and one
    missing edge sample would otherwise make five minutes look like four.
    """
    with _DB:
        return conn.execute(
            """SELECT MIN(cpu_pct) FROM samples
                WHERE host=? AND ts <= ?
                  AND ts >= (SELECT MAX(ts) FROM samples WHERE host=? AND ts <= ?)""",
            (host, ts, host, ts - seconds)).fetchone()[0]


def _migrate_patch_pending(conn):
    """Seed the pending list from the newest sample that still carries one.

    Without this an upgrade would show every host with zero pending packages
    until its next patchcheck -- up to a day -- and prune() would read that
    empty list as "nothing is pending here" and delete every ack on the box.

    Reads the newest row that has a non-NULL patch_packages, not simply the
    newest row: the new insert_sample writes NULL there, so by the time this
    runs on a second start the recent rows are all empty and the last real
    answer is further back. A table that already holds anything has been
    seeded, which is what makes this safe to run on every connect.
    """
    if conn.execute("SELECT 1 FROM patch_pending LIMIT 1").fetchone():
        return
    rows = conn.execute(
        """SELECT host, patch_packages FROM samples
            WHERE id IN (SELECT MAX(id) FROM samples
                          WHERE patch_packages IS NOT NULL GROUP BY host)"""
    ).fetchall()
    for r in rows:
        _set_patch_pending(conn, r["host"], r["patch_packages"])


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
        for pkg in split_packages(r["packages"])
    ]
    conn.execute("DROP TABLE patch_acks")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO patch_acks (host, package, acked_at) VALUES (?,?,?)", rows
    )


# One connection is shared by every request thread (check_same_thread=False) and
# by six background loops, so every use of it is serialised by hand.
#
# Writes need this because a write that spans more than one statement is not
# atomic on its own. sqlite3's own locking makes each *statement* atomic, but
# nothing stops another thread from slipping a statement between two of ours --
# and insert_sample reads lastrowid, a *connection*-level value, after its
# INSERT. Two collectors posting in the same second (cron fires them all at :01)
# raced there: the second INSERT moved lastrowid before the first thread had
# read it, so one sample's disk rows were written against the other sample's id.
# The dashboard showed "no mounts reported" for the robbed host and a doubled
# mount list for the other. Held across the commit too: a commit from another
# thread would otherwise end our transaction early, publishing a sample with
# only some of its disks attached.
#
# Reads need it for a blunter reason: a sqlite3 connection is not safe to use
# from two threads at once at all. Unguarded reads raised
# "InterfaceError: bad parameter or other API misuse" (SQLITE_MISUSE) and the
# occasional "IndexError: tuple index out of range" out of the reachability
# prober and /api/overview -- rarely enough to look like a fluke, until a
# change that added one query per host to summarize() turned it into every
# sweep.
#
# Where the collision lands is itself a race, so what it costs varies. On the
# sweeps caught in the act it landed on reach_prober's last statement -- the
# latest_per_host() that drops verdicts for forgotten hosts -- because the fast
# path marking reporting hosts up had already run for all 28 of them, and with
# the whole fleet reporting there was nothing left to probe. Land it a few
# statements earlier, on a sweep that does have a silent host to probe, and the
# probing is what gets skipped instead, leaving exactly the host the prober
# exists to make a verdict about sitting at "unknown". Either way the prober
# catches it and says nothing but one line on stderr.
#
# Reentrant, because the guarded writes call the guarded reads. prune() holds
# this and calls latest_per_host() and patch_pending(); insert_sample() holds
# it and calls _set_patch_pending(), which reads before it writes. A plain
# Lock deadlocks on all three.
#
# Serialising every read is affordable here and would not be everywhere: these
# are small indexed queries over a fleet of tens, and WAL means the writer
# never blocks on a reader at the file level. The honest fix at a larger size
# is a connection per thread.
_DB = threading.RLock()


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
    _migrate_patch_pending(conn)
    _migrate_hosts(conn)
    _migrate_rollups(conn)
    conn.commit()
    return conn


def register_host(conn, host, ts, os_string=None, source="push", peer=None):
    """Note that this host exists and was heard from at `ts`.

    Called on every ingest, and callable by hand to put back a host that was
    lost before the register existed. Never overwrites what it was not told:
    a sample carrying no OS string or arriving without a peer address must not
    erase the ones the prober and the card have been using.

    Caller holds _DB and commits -- this is part of storing the sample, not
    a write of its own.
    """
    conn.execute(
        """INSERT INTO hosts (host, first_seen, last_seen, os, source, peer_addr)
             VALUES (?,?,?,?,?,?)
           ON CONFLICT(host) DO UPDATE SET
             last_seen = MAX(hosts.last_seen, excluded.last_seen),
             os        = COALESCE(excluded.os, hosts.os),
             source    = excluded.source,
             peer_addr = COALESCE(excluded.peer_addr, hosts.peer_addr)""",
        (host, int(ts), int(ts), os_string, source, peer),
    )


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
    with _DB:
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
                # Vestigial: the names live in patch_pending now, at the rate
                # they change rather than the rate samples arrive. Kept as a
                # column so a rollback onto the previous server still reads its
                # own rows, and left NULL so the 4 MB of duplicated strings
                # already in the window ages out with the window.
                None,
                payload.get("collector_version"),
                payload.get("virt"),
                peer,
            ),
        )
        sid = cur.lastrowid
        register_host(conn, payload["host"], ts, payload.get("os"), source, peer)
        # Only a sample that actually carries a check may speak for what is
        # pending. A collector whose patchcheck has never run, or has stopped
        # reporting, has told us nothing -- and wiping the list on that silence
        # would drop every ack with it the moment the check came back.
        if p.get("checked_at") is not None:
            _set_patch_pending(conn, payload["host"], p.get("packages"))
        for d in payload.get("disks") or []:
            conn.execute(
                "INSERT INTO disks (sample_id, mount, used_bytes, total_bytes) VALUES (?,?,?,?)",
                (sid, d.get("mount"), d.get("used_bytes"), d.get("total_bytes")),
            )
        # Only a payload that carries pool health may speak for it, the same
        # rule as the patch list: a host whose zpool call failed, or an older
        # collector, has said nothing, and its last report ages out instead.
        if isinstance(payload.get("pools"), list):
            smart = payload.get("smart")
            conn.execute(
                "INSERT OR REPLACE INTO storage (host, ts, pools, smart) VALUES (?,?,?,?)",
                (payload["host"], ts, json.dumps(payload["pools"]),
                 json.dumps(smart) if isinstance(smart, list) else None),
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


# How long an acknowledged macOS update may be missing from the pending list
# before its ack is dropped. softwareupdate's cached list can come up empty for
# a day and then offer the same update again: argon reported
# "macOS 27.0.1-26A5434", then nothing, then the same label a day later, and
# the prune in between had deleted the ack that was made against it.
#
# Only macOS, because only there does the reappearance of a name prove nothing
# changed. The label carries the version and the build, so the same label is
# the same update that was reviewed. apt and dnf report bare names -- openssl,
# not openssl at a version -- and a fresh openssl advisory inside the window
# would come back already silenced. The BSDs name the installed version too,
# and could join this list if their audits turn out to flicker the same way.
ACK_GRACE = 3 * 86400
GRACE_SOURCES = ("softwareupdate", "softwareupdate+brew")


def prune(conn, retention_hours, rollup_days=None, event_days=None, fold=True):
    """Drop raw samples past the window, and rollups past a much longer one.

    Three tiers with three lifetimes, so "how long do we keep data" stops being
    one number that has to be wrong for something. Raw is expensive and only
    interesting recently; rollups are cheap and only interesting over months;
    events are almost free and are the one thing worth keeping indefinitely,
    which is what `event_days=None` means.

    Folds every unfolded sample into the rollups first, in the same
    transaction as the delete, so no raw sample is deleted before it has been
    counted. The pruner and the roller run on separate timers, and main()
    prunes before the roller's first pass: on a server that had been down
    longer than the window, that prune used to delete every sample the roller
    had not yet seen. `fold=False` is for a server with history turned off,
    which keeps no rollups to fold into.
    """
    with _DB:
        cutoff = int(time.time()) - retention_hours * 3600
        if fold:
            _fold(conn)
        _set_meta(conn, "raw_pruned_before",
                  max(cutoff, _meta(conn, "raw_pruned_before") or 0))
        conn.execute(
            "DELETE FROM disks WHERE sample_id IN (SELECT id FROM samples WHERE ts < ?)",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM fleet WHERE sample_id IN (SELECT id FROM samples WHERE ts < ?)",
            (cutoff,),
        )
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        # An ack for a host netdash no longer watches is not "still silencing
        # something", it is a leftover -- and if the name is ever reused, one
        # that would misapply to whatever that new host reports. Keyed on the
        # register rather than on the samples: a host that is merely down has
        # not stopped being ours, and dropping its acks while it was offline
        # would have turned it red on the way back up over a decision somebody
        # had already made. forget_host is what clears these now.
        conn.execute(
            "DELETE FROM patch_acks WHERE host NOT IN (SELECT host FROM hosts)"
        )
        # And an ack for a package that is no longer pending is not silencing
        # anything either -- it is a landmine. Left in place, a python312 fixed
        # in March would come back vulnerable in July already silenced, by a
        # decision made about a different advisory. Only hosts whose latest
        # sample actually carries a check are cleaned: a collector that stopped
        # reporting patches has not told us anything about what is pending, and
        # dropping its acks on that silence would un-silence the lot the moment
        # it came back.
        #
        # Except on macOS, where an ack outlives a short absence. See ACK_GRACE.
        now = int(time.time())
        for s in latest_per_host(conn):
            if s["patch_checked_at"] is None:
                continue
            host = s["host"]
            keep = set(patch_pending(conn, host))
            gone = [r["package"] for r in conn.execute(
                "SELECT package FROM patch_acks WHERE host=?", (host,))
                if r["package"] not in keep]
            if s["patch_source"] not in GRACE_SOURCES:
                conn.executemany("DELETE FROM patch_acks WHERE host=? AND package=?",
                                 [(host, p) for p in gone])
                continue
            # Back on the list: the absence was the blip, and the clock resets.
            conn.executemany(
                "UPDATE patch_acks SET missing_since=NULL WHERE host=? AND package=?",
                [(host, p) for p in keep])
            conn.executemany(
                """UPDATE patch_acks SET missing_since=? WHERE host=? AND package=?
                     AND missing_since IS NULL""",
                [(now, host, p) for p in gone])
            conn.executemany(
                "DELETE FROM patch_acks WHERE host=? AND package=? AND missing_since<=?",
                [(host, p, now - ACK_GRACE) for p in gone])
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


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, int(value)))


def _migrate_rollups(conn):
    """Give a database from before the folding roller its prune horizon.

    A fresh database has pruned nothing, so every bucket it starts can be
    whole. One that was pruned before raw_pruned_before existed may have lost
    raw samples from any time before its oldest survivor, and so that is the
    horizon it starts from: a bucket beginning at or after the oldest sample
    left is provably intact, and one beginning earlier is not. A database with
    rollups but no samples at all could have lost anything up to now.

    rollup_folded_id is left unset, so the first fold takes in every sample on
    hand and _heal_legacy rebuilds the old rows those samples fully cover.
    """
    if _meta(conn, "raw_pruned_before") is not None:
        return
    oldest = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    if oldest is None:
        had = (conn.execute("SELECT 1 FROM rollups LIMIT 1").fetchone()
               or conn.execute("SELECT 1 FROM disk_rollups LIMIT 1").fetchone())
        oldest = int(time.time()) if had else 0
    _set_meta(conn, "raw_pruned_before", oldest)


HOUR, DAY = 3600, 86400

# Both upserts merge a batch of raw samples into whatever their buckets already
# hold. {where} picks the batch, over `s` (samples) and, for disks, `d`.
#
# min() and max() of two values are NULL if either is, so each side falls back
# to the other: a bucket that had no cpu reading yet takes the new one, and a
# batch with none leaves the old one alone.
#
# The DO UPDATE never touches a NULL-coverage row. That row was written by the
# old roller and has no sums to add to; _heal_legacy rebuilds it from raw if
# the raw is all still there, and otherwise it is left exactly as it was.
_FOLD_HOURLY = """
INSERT INTO rollups (host, bucket, samples,
                     cpu_min, cpu_avg, cpu_max, cpu_sum, cpu_n,
                     mem_min, mem_avg, mem_max, mem_sum, mem_n, coverage)
SELECT host, bucket, COUNT(*),
       MIN(cpu_pct), AVG(cpu_pct), MAX(cpu_pct), TOTAL(cpu_pct), COUNT(cpu_pct),
       MIN(pct), AVG(pct), MAX(pct), TOTAL(pct), COUNT(pct),
       CASE WHEN bucket >= :horizon THEN 'full' ELSE 'partial' END
  FROM (SELECT s.host, s.ts - (s.ts %% %(len)d) AS bucket, s.cpu_pct,
               COALESCE(s.mem_pct,
                        CASE WHEN s.mem_total_bytes > 0
                             THEN 100.0 * s.mem_used_bytes / s.mem_total_bytes
                        END) AS pct
          FROM samples s WHERE {where})
 WHERE 1
 GROUP BY host, bucket
ON CONFLICT (host, bucket) DO UPDATE SET
   samples = samples + excluded.samples,
   cpu_sum = cpu_sum + excluded.cpu_sum,
   cpu_n   = cpu_n + excluded.cpu_n,
   cpu_avg = (cpu_sum + excluded.cpu_sum) / NULLIF(cpu_n + excluded.cpu_n, 0),
   cpu_min = min(COALESCE(cpu_min, excluded.cpu_min), COALESCE(excluded.cpu_min, cpu_min)),
   cpu_max = max(COALESCE(cpu_max, excluded.cpu_max), COALESCE(excluded.cpu_max, cpu_max)),
   mem_sum = mem_sum + excluded.mem_sum,
   mem_n   = mem_n + excluded.mem_n,
   mem_avg = (mem_sum + excluded.mem_sum) / NULLIF(mem_n + excluded.mem_n, 0),
   mem_min = min(COALESCE(mem_min, excluded.mem_min), COALESCE(excluded.mem_min, mem_min)),
   mem_max = max(COALESCE(mem_max, excluded.mem_max), COALESCE(excluded.mem_max, mem_max))
 WHERE rollups.coverage IS NOT NULL
""" % {"len": HOUR}

# A disk row counts only with a positive total and a used figure: a mount
# reporting a zero or missing total has no percentage to give, and averaging
# it in as zero would read as a disk that emptied itself.
_FOLD_DAILY = """
INSERT INTO disk_rollups (host, mount, bucket, samples,
                          pct_avg, pct_max, pct_sum, used_avg, used_sum,
                          total_bytes, total_min, coverage)
SELECT host, mount, bucket, COUNT(*),
       AVG(pct), MAX(pct), TOTAL(pct), AVG(used), TOTAL(used),
       MAX(total), MIN(total),
       CASE WHEN bucket >= :horizon THEN 'full' ELSE 'partial' END
  FROM (SELECT s.host, d.mount, s.ts - (s.ts %% %(len)d) AS bucket,
               100.0 * d.used_bytes / d.total_bytes AS pct,
               d.used_bytes AS used, d.total_bytes AS total
          FROM samples s JOIN disks d ON d.sample_id = s.id
         WHERE {where} AND d.total_bytes > 0 AND d.used_bytes IS NOT NULL)
 WHERE 1
 GROUP BY host, mount, bucket
ON CONFLICT (host, mount, bucket) DO UPDATE SET
   samples     = samples + excluded.samples,
   pct_sum     = pct_sum + excluded.pct_sum,
   pct_avg     = (pct_sum + excluded.pct_sum) / (samples + excluded.samples),
   pct_max     = max(pct_max, excluded.pct_max),
   used_sum    = used_sum + excluded.used_sum,
   used_avg    = (used_sum + excluded.used_sum) / (samples + excluded.samples),
   total_bytes = max(total_bytes, excluded.total_bytes),
   total_min   = min(total_min, excluded.total_min)
 WHERE disk_rollups.coverage IS NOT NULL
""" % {"len": DAY}


def _fold(conn):
    """Fold every sample not yet in the rollups into them. Caller holds _DB
    and commits.

    The watermark is samples.id, not ts. ids only grow -- AUTOINCREMENT never
    reuses one, even after the prune -- and SQLite allots them one writer at a
    time, so everything at or below the newest id is already committed and
    nothing will turn up beneath it later. A ts watermark would miss a sample
    that arrives late with an old timestamp, and step with the clock.

    Each sample is read once, so a pass costs what arrived since the last one
    rather than a scan of the whole raw window, and the watermark advances in
    the same transaction as the sums: a crash leaves both as they were, and
    the next pass picks up where this one would have.
    """
    done = _meta(conn, "rollup_folded_id") or 0
    top = conn.execute("SELECT MAX(id) FROM samples").fetchone()[0] or 0
    if top > done:
        args = {"lo": done, "hi": top,
                "horizon": _meta(conn, "raw_pruned_before") or 0}
        where = "s.id > :lo AND s.id <= :hi"
        conn.execute(_FOLD_HOURLY.format(where=where), args)
        conn.execute(_FOLD_DAILY.format(where=where), args)
        _set_meta(conn, "rollup_folded_id", top)
    _heal_legacy(conn, top)


def _heal_legacy(conn, top):
    """Rebuild old-roller rows whose raw samples are all still on hand.

    Only a bucket starting at or after the prune horizon qualifies: its raw
    samples are provably intact, so recomputing it from them gives the whole
    bucket and it can say 'full'. One starting earlier has lost samples to the
    prune -- maybe only the ones its old row already lacked, maybe more -- and
    is left exactly as the old roller wrote it, NULL coverage and all.

    The horizon here also takes the oldest surviving sample into account,
    because a legacy row can only come from an old server, and an old server's
    prune never moved raw_pruned_before. Probed through the bucket index, so a
    pass with nothing to heal costs two index seeks.
    """
    oldest = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
    if oldest is None:
        return
    horizon = max(_meta(conn, "raw_pruned_before") or 0, oldest)
    for r in conn.execute(
            "SELECT host, bucket FROM rollups WHERE bucket >= ? AND coverage IS NULL",
            (horizon,)).fetchall():
        args = {"host": r["host"], "lo": r["bucket"], "hi": r["bucket"] + HOUR,
                "top": top, "horizon": horizon}
        where = "s.host = :host AND s.ts >= :lo AND s.ts < :hi AND s.id <= :top"
        if conn.execute("SELECT 1 FROM samples s WHERE %s LIMIT 1" % where, args).fetchone():
            conn.execute("DELETE FROM rollups WHERE host=? AND bucket=?",
                         (r["host"], r["bucket"]))
            conn.execute(_FOLD_HOURLY.format(where=where), args)
    for r in conn.execute(
            "SELECT host, mount, bucket FROM disk_rollups"
            " WHERE bucket >= ? AND coverage IS NULL", (horizon,)).fetchall():
        args = {"host": r["host"], "mount": r["mount"], "lo": r["bucket"],
                "hi": r["bucket"] + DAY, "top": top, "horizon": horizon}
        where = ("s.host = :host AND d.mount = :mount AND s.ts >= :lo AND s.ts < :hi"
                 " AND s.id <= :top")
        if conn.execute(
                "SELECT 1 FROM samples s JOIN disks d ON d.sample_id = s.id WHERE %s"
                " AND d.total_bytes > 0 AND d.used_bytes IS NOT NULL LIMIT 1" % where,
                args).fetchone():
            conn.execute("DELETE FROM disk_rollups WHERE host=? AND mount=? AND bucket=?",
                         (r["host"], r["mount"], r["bucket"]))
            conn.execute(_FOLD_DAILY.format(where=where), args)


def rollup(conn, window_hours=None):
    """Fold the samples that arrived since the last pass into their buckets.

    This used to recompute every bucket in the raw window from the samples
    still on hand and INSERT OR REPLACE the result. Fine while a bucket's
    samples were all there; but the cutoff walks through the oldest hour and
    day in the window, and every pass then rewrote that bucket from its
    shrinking tail, until the last pass before the cutoff cleared it left the
    tail behind for good. Mercury's older daily disk rows held a median of 8.5
    samples against 1,337 for a whole day, and an hour whose peak came early
    kept only the quiet minutes after it.

    So each sample is now added to its bucket exactly once, and a bucket only
    ever grows. The current hour still updates on every pass, a missed pass
    just means the next one has more to fold, and prune() folds before it
    deletes, so a sample cannot age out of raw before it has been counted.

    `window_hours` is accepted and ignored: callers pass the raw window, and
    the fold no longer needs one.
    """
    with _DB:
        _fold(conn)
        conn.commit()


def rollup_series(conn, host, since):
    """Hourly points for one host, shaped like history() so the UI can share code.

    coverage rides along so a chart can tell a pre-fix bucket, which may be a
    truncated tail, from one known to be whole. See the rollups table.
    """
    with _DB:
        rows = conn.execute(
            """SELECT bucket AS ts, cpu_avg AS cpu_pct, mem_avg AS mem_pct,
                      cpu_min, cpu_max, mem_min, mem_max, samples, coverage
                 FROM rollups WHERE host=? AND bucket >= ? ORDER BY bucket ASC""",
            (host, int(since)),
        ).fetchall()
        return [dict(r) for r in rows]


def disk_series(conn, host, since):
    """Daily per-mount points, oldest first."""
    with _DB:
        rows = conn.execute(
            """SELECT mount, bucket AS ts, pct_avg, pct_max, used_avg, total_bytes,
                      total_min, samples, coverage
                 FROM disk_rollups WHERE host=? AND bucket >= ? ORDER BY mount, bucket ASC""",
            (host, int(since)),
        ).fetchall()
        return [dict(r) for r in rows]


def raw_extent(conn, host, since):
    """(samples, oldest ts) for one host since `since`: what a raw disk view
    would cost, read off the (host, ts) index without touching the disk rows."""
    with _DB:
        r = conn.execute("SELECT COUNT(*), MIN(ts) FROM samples WHERE host=? AND ts >= ?",
                         (host, int(since))).fetchone()
        return r[0], r[1]


def disk_history(conn, host, since, bucket=HOUR, now=None):
    """Per-mount usage over time from raw samples: every reading, or one per bucket.

    bucket=0 (or None) returns each reading as collected, at its own ts. Any
    other bucket returns, per mount and bucket, the single reading with the
    highest percentage -- MAX rather than AVG, because the question a disk
    chart is asked is how close this got, and an average hides the spike that
    filled it. pct, used_bytes and total_bytes all come from that one reading,
    so the point is something the host actually reported: taking MAX(used) and
    MAX(total) separately, as this used to, paired the fullest moment with the
    largest capacity and on a resized mount described a disk that never
    existed.

    A bucketed point's ts is the bucket start -- it ends `bucket` seconds
    later -- and it says so: `peak_ts` (when that reading was taken; on a tie,
    one of the tied ones), `samples` (how many readings the bucket holds), and
    `capacity_changed`, with `total_min`/`total_max` only when it is true, so a
    host with forty mounts does not ship two totals per point that say
    nothing. `coverage` is 'partial' for a bucket that
    starts before the raw prune horizon -- some of its readings are gone -- or
    that has not ended yet, and 'full' otherwise.

    `since` is aligned down to a bucket boundary, so the first bucket is whole
    rather than a sliver labelled with a start time it does not cover.

    A reading with no used figure or a zero/absent total has no percentage and
    is left out, never counted as empty. Two readings of one mount at the same
    ts collapse to the later-written one.
    """
    bucket = int(bucket or 0)
    now = int(time.time() if now is None else now)
    since = int(since)
    if bucket:
        since -= since % bucket
    valid = ("s.host = :host AND s.ts >= :since AND d.total_bytes > 0"
             " AND d.used_bytes IS NOT NULL")
    with _DB:
        if not bucket:
            rows = conn.execute(
                """SELECT d.mount AS mount, s.ts AS ts, d.used_bytes AS used,
                          d.total_bytes AS total
                     FROM samples s JOIN disks d ON d.sample_id = s.id
                    WHERE %s ORDER BY d.mount, s.ts, s.id""" % valid,
                {"host": host, "since": since}).fetchall()
            out = {}
            for r in rows:
                pts = out.setdefault(r["mount"], [])
                p = {"ts": r["ts"], "pct": round(100.0 * r["used"] / r["total"], 3),
                     "used_bytes": r["used"], "total_bytes": r["total"]}
                if pts and pts[-1]["ts"] == p["ts"]:
                    pts[-1] = p
                else:
                    pts.append(p)
            return out
        horizon = _meta(conn, "raw_pruned_before") or 0
        # The one MAX() is what makes ts, used and total bare columns from the
        # row holding it -- documented SQLite behaviour, and what keeps the
        # point a single reading. It must stay the only min()/max() aggregate
        # in the query, or SQLite may take the bare columns from another's
        # row; the capacities come back as a list for that reason. Among
        # readings tied at the maximum -- most of them, on a quiet mount --
        # which one supplies peak_ts is SQLite's choice. This costs what the
        # GROUP BY it replaces did; a window function picking the same row
        # measured four times slower over 40 mounts x 48 h.
        rows = conn.execute(
            """SELECT d.mount AS mount, s.ts - s.ts %% :b AS bucket,
                      MAX(1.0 * d.used_bytes / d.total_bytes) AS frac,
                      s.ts AS ts, d.used_bytes AS used, d.total_bytes AS total,
                      COUNT(*) AS n, GROUP_CONCAT(DISTINCT d.total_bytes) AS totals
                 FROM samples s JOIN disks d ON d.sample_id = s.id
                WHERE %s
                GROUP BY d.mount, bucket
                ORDER BY d.mount, bucket""" % valid,
            {"host": host, "since": since, "b": bucket}).fetchall()
        out = {}
        for r in rows:
            end = r["bucket"] + bucket
            totals = [int(x) for x in str(r["totals"]).split(",")]
            p = {"ts": r["bucket"], "peak_ts": r["ts"],
                 "pct": round(100.0 * r["used"] / r["total"], 3),
                 "used_bytes": r["used"], "total_bytes": r["total"],
                 "samples": r["n"], "capacity_changed": len(totals) > 1,
                 "coverage": "partial" if r["bucket"] < horizon or end > now else "full"}
            if p["capacity_changed"]:
                p["total_min"], p["total_max"] = min(totals), max(totals)
            out.setdefault(r["mount"], []).append(p)
        return out


def disk_daily(conn, host, since):
    """Daily per-mount maxima from disk_rollups, shaped like disk_history's buckets.

    The rollup keeps pct_max but not the reading it came from. Where the
    capacity held all day, that reading's used figure is pct_max of the one
    total and is given; on a day the mount was resized there is no telling
    which total the peak was against, so used_bytes and total_bytes are null
    rather than a guess, and total_min/total_max say what is known. A legacy
    row (coverage NULL, from before the folding roller) has no total_min and
    is treated the same way. Its coverage stays NULL: it may be the truncated
    tail of a day, and nothing here pretends otherwise.
    """
    out = {}
    for r in disk_series(conn, host, since):
        tmax, tmin = r["total_bytes"], r["total_min"]
        steady = tmax is not None and tmin is not None and tmin == tmax
        used = (int(round(r["pct_max"] * tmax / 100.0))
                if steady and r["pct_max"] is not None else None)
        out.setdefault(r["mount"], []).append({
            "ts": r["ts"], "peak_ts": None,
            "pct": r["pct_max"], "pct_avg": r["pct_avg"],
            "used_bytes": used, "total_bytes": tmax if steady else None,
            "used_avg": r["used_avg"], "samples": r["samples"],
            "total_min": tmin, "total_max": tmax,
            "capacity_changed": (None if tmin is None or tmax is None else tmin != tmax),
            "coverage": r["coverage"]})
    return out


def record_event(conn, host, kind, from_state, to_state, detail=None, ts=None):
    with _DB:
        conn.execute(
            """INSERT INTO events (ts, host, kind, from_state, to_state, detail)
               VALUES (?,?,?,?,?,?)""",
            (int(ts or time.time()), host, kind, from_state, to_state, detail),
        )
        conn.commit()


def recent_events(conn, host=None, limit=100, since=None, kind=None):
    """Newest first. `kind` matches itself and everything under it, so
    "service" finds every "service:<id>" without the caller knowing the ids."""
    with _DB:
        q = "SELECT * FROM events WHERE 1=1"
        args = []
        if host:
            q += " AND host=?"
            args.append(host)
        if since:
            q += " AND ts >= ?"
            args.append(int(since))
        if kind:
            # substr rather than LIKE: an id may carry "_", which LIKE reads as
            # a wildcard.
            q += " AND (kind=? OR substr(kind, 1, ?)=?)"
            args += [kind, len(kind) + 1, kind + ":"]
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in conn.execute(q, args)]


def last_states(conn):
    """The most recent to_state per host, for seeding the watcher after a restart.

    Without this a restart forgets what every host was last seen doing, and the
    first sweep silently treats whatever it finds as the starting state -- so a
    machine that went down during a deploy would never produce an event at all.

    One grouped pass for the newest id per host, not a correlated MAX(id) per
    row: that re-scanned a host's whole history for each of its events, and
    with events never pruned it held the lock -- and so every request -- for
    over a minute after each restart.
    """
    with _DB:
        rows = conn.execute(
            """SELECT host, to_state FROM events
                WHERE id IN (SELECT MAX(id) FROM events
                              WHERE kind='status' GROUP BY host)"""
        ).fetchall()
        return {r["host"]: r["to_state"] for r in rows}


def last_service_states(conn):
    """The most recent to_state per service check, keyed by event kind
    ("service:<id>"). The same job last_states does for hosts: a restart must
    not forget that Plex was down, or its recovery would never be logged."""
    with _DB:
        rows = conn.execute(
            """SELECT kind, to_state FROM events
                WHERE id IN (SELECT MAX(id) FROM events
                              WHERE kind LIKE 'service:%' GROUP BY kind)"""
        ).fetchall()
        return {r["kind"]: r["to_state"] for r in rows}


def last_storage_states(conn):
    """The most recent to_state per (host, "storage:..." kind), for seeding
    the watcher: a pool that went DEGRADED before a restart must still have
    its repair logged after it."""
    with _DB:
        rows = conn.execute(
            """SELECT host, kind, to_state FROM events
                WHERE id IN (SELECT MAX(id) FROM events
                              WHERE kind LIKE 'storage:%' GROUP BY host, kind)"""
        ).fetchall()
        return {(r["host"], r["kind"]): r["to_state"] for r in rows}


# Cached because latest_per_host runs in four background loops and the answer
# cannot change while the process is up -- the schema is settled by connect().
_SAMPLE_COLS = None


def _sample_columns(conn):
    global _SAMPLE_COLS
    if _SAMPLE_COLS is None:
        _SAMPLE_COLS = [r["name"] for r in conn.execute("PRAGMA table_info(samples)")]
    return _SAMPLE_COLS


def _no_sample(conn, h):
    """A row for a host whose samples have all aged out.

    Every metric null rather than absent, so nothing downstream has to know
    this row is different: a null reads as "unknown" through _level(), which
    is exactly what we know about a host we have no data for. `ts` is the last
    time it reported, which makes it enormously stale, which is what turns it
    red once the probe or the clock agrees.
    """
    d = dict.fromkeys(_sample_columns(conn))
    d.update({
        "host": h["host"],
        "ts": h["last_seen"],
        "os": h["os"],
        "source": h["source"] or "push",
        "peer_addr": h["peer_addr"],
        "disks": [],
        "fleet": [],
        "no_samples": True,
    })
    return d


def _storage(conn, host):
    """The host's last pool and SMART report, or None. Caller holds _DB."""
    r = conn.execute("SELECT ts, pools, smart FROM storage WHERE host=?",
                     (host,)).fetchone()
    if not r:
        return None
    try:
        return {"ts": r["ts"], "pools": json.loads(r["pools"] or "[]"),
                "smart": json.loads(r["smart"]) if r["smart"] else None}
    except ValueError:
        return None


def latest_per_host(conn):
    """Every known host, each with its most recent sample -- or with none.

    The host list comes from `hosts`; the samples are joined onto it. That
    order is the fix for losing machines: a host down longer than the
    retention window has no sample to be the latest one, and a fleet built out
    of `samples` therefore stopped including it. Now it is still in the list,
    with no readings and a very old timestamp, and summarize() does the rest.
    """
    with _DB:
        rows = conn.execute(
            """SELECT s.* FROM samples s
                 JOIN (SELECT host, MAX(ts) AS mts FROM samples GROUP BY host) m
                   ON s.host = m.host AND s.ts = m.mts
               GROUP BY s.host
               ORDER BY s.host"""
        ).fetchall()
        latest = {}
        for r in rows:
            d = dict(r)
            d["no_samples"] = False
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
            d["storage"] = _storage(conn, d["host"])
            latest[d["host"]] = d

        out = []
        seen = set()
        for h in conn.execute("SELECT * FROM hosts ORDER BY host"):
            seen.add(h["host"])
            out.append(latest.get(h["host"]) or _no_sample(conn, h))
        # A sample whose host is somehow not registered still counts as a host.
        # Belt and braces -- _migrate_hosts and insert_sample between them should
        # make this impossible -- but dropping a host that is actively reporting
        # is the one failure this whole table exists to prevent.
        for host in sorted(set(latest) - seen):
            out.append(latest[host])
        return out


def forget_host(conn, host):
    """Remove a host from the fleet and everything stored about it.

    The counterpart to a register that never expires: something has to be able
    to say a machine is gone, and now that silence no longer does it, this is
    it. An act, deliberately, rather than a timer.

    Its events are the exception, and are kept. "ubuntu22dot04server went down
    on the 4th" stays true after the machine is decommissioned, and the event
    log is the one part of netdash that is allowed to remember things that no
    longer exist.

    Returns False if the host was not known, so the API can answer 404 rather
    than pretending to have done something.
    """
    with _DB:
        known = conn.execute("SELECT 1 FROM hosts WHERE host=?", (host,)).fetchone()
        # Explicit rather than trusting ON DELETE CASCADE: it only fires with
        # foreign_keys=ON, which is a per-connection pragma, and orphaned disk
        # rows would be re-attached to a future sample that happened to reuse
        # the id.
        for t in ("disks", "fleet"):
            conn.execute(
                "DELETE FROM %s WHERE sample_id IN "
                "(SELECT id FROM samples WHERE host=?)" % t, (host,))
        for t in ("samples", "rollups", "disk_rollups", "patch_acks",
                  "patch_pending", "eol_acks", "guests", "storage", "hosts"):
            conn.execute("DELETE FROM %s WHERE host=?" % t, (host,))
        conn.commit()
    return bool(known)


def record_guests(conn, parent, entries, ts):
    """Store what a parent just said about its jails.

    entries: [{"host", "state", "boot", "address"}], already validated. Every
    jail this parent reported before and did not mention now becomes "absent"
    -- keeping its address and boot flag, which are still the last known ones.
    A jail that moved to another parent is that parent's now; the old one's
    silence about it is not news.
    """
    with _DB:
        named = set()
        for e in entries:
            named.add(e["host"])
            conn.execute(
                """INSERT INTO guests (host, parent, state, boot, address, seen_at)
                     VALUES (?,?,?,?,?,?)
                   ON CONFLICT(host) DO UPDATE SET
                     parent = excluded.parent, state = excluded.state,
                     boot = excluded.boot,
                     address = COALESCE(excluded.address, guests.address),
                     seen_at = excluded.seen_at""",
                (e["host"], parent, e["state"],
                 None if e.get("boot") is None else int(bool(e["boot"])),
                 e.get("address"), int(ts)),
            )
        for r in conn.execute("SELECT host FROM guests WHERE parent=?", (parent,)).fetchall():
            if r["host"] not in named:
                conn.execute("UPDATE guests SET state='absent', seen_at=? WHERE host=?",
                             (int(ts), r["host"]))
        conn.commit()


def host_row(conn, host):
    """The register entry for one host -- its last source and last_seen -- plus
    the virt its newest sample reported, or None if the host is unknown."""
    with _DB:
        r = conn.execute(
            """SELECT h.*, (SELECT virt FROM samples WHERE host = h.host
                             ORDER BY ts DESC LIMIT 1) AS virt
                 FROM hosts h WHERE h.host=?""", (host,)).fetchone()
        return dict(r) if r else None


def guest(conn, host):
    """What a parent last said about this jail, or None if none ever has."""
    with _DB:
        r = conn.execute("SELECT * FROM guests WHERE host=?", (host,)).fetchone()
        return dict(r) if r else None


def guests(conn):
    """{host: row} for every jail some parent has reported."""
    with _DB:
        return {r["host"]: dict(r) for r in conn.execute("SELECT * FROM guests")}


def history(conn, host, since_seconds):
    with _DB:
        cutoff = int(time.time()) - since_seconds
        rows = conn.execute(
            """SELECT id, ts, cpu_pct, mem_used_bytes, mem_total_bytes, mem_pct
                 FROM samples WHERE host=? AND ts >= ? ORDER BY ts ASC""",
            (host, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def known_hosts(conn):
    with _DB:
        return [r["host"] for r in conn.execute("SELECT host FROM hosts ORDER BY host")]


def get_patch_acks(conn, host):
    """{package: acked_at} for every package acknowledged on this host."""
    with _DB:
        rows = conn.execute("SELECT package, acked_at FROM patch_acks WHERE host=?", (host,))
        return {r["package"]: r["acked_at"] for r in rows}


def ack_patch(conn, host, packages, now):
    """Silence these pending packages on this host -- each one on its own terms,
    so an ack survives the rest of the list changing. See the note on the table.

    Takes a list rather than one name because "acknowledge all" is one decision
    made at one moment, and writing it as one statement keeps every row in it
    carrying the same timestamp.
    """
    with _DB:
        conn.executemany(
            """INSERT INTO patch_acks (host, package, acked_at) VALUES (?,?,?)
               ON CONFLICT(host, package) DO UPDATE SET
                 acked_at=excluded.acked_at, missing_since=NULL""",
            [(host, p, now) for p in packages],
        )
        conn.commit()


def unack_patch(conn, host, package=None):
    """One package back to pending, or the whole host when package is None."""
    with _DB:
        if package is None:
            conn.execute("DELETE FROM patch_acks WHERE host=?", (host,))
        else:
            conn.execute("DELETE FROM patch_acks WHERE host=? AND package=?",
                         (host, package))
        conn.commit()


def get_eol_ack(conn, host):
    with _DB:
        r = conn.execute("SELECT * FROM eol_acks WHERE host=?", (host,)).fetchone()
        return dict(r) if r else None


def ack_eol(conn, host, status, product, cycle, eol_date, now):
    """Silence the host's current end-of-life phase -- this phase, on this
    release, with this date. See the note on the table."""
    with _DB:
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
    with _DB:
        conn.execute("DELETE FROM eol_acks WHERE host=?", (host,))
        conn.commit()
