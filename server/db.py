"""SQLite storage for netdash. Rolling window only -- no long-term retention."""

import sqlite3
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
    patch_reboot       INTEGER
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
    ],
}


def _migrate(conn):
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


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
    conn.commit()
    return conn


def insert_sample(conn, payload, source="push"):
    """payload: dict from a collector. Returns the new sample id."""
    ts = int(payload.get("ts") or time.time())
    # A collector that has never run a patch check omits the key entirely, and
    # one whose state file was unreadable sends null. Both mean "unknown", so
    # both land as nulls rather than zeros -- a zero here would read on the
    # dashboard as "checked, and up to date".
    p = payload.get("patches") or {}
    if not isinstance(p, dict):
        p = {}
    cur = conn.execute(
        """INSERT INTO samples
             (host, ts, os, source, cpu_pct, mem_used_bytes, mem_total_bytes, uptime_seconds,
              patch_security, patch_other, patch_checked_at, patch_source, patch_detail,
              patch_reboot)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            payload["host"],
            ts,
            payload.get("os"),
            source,
            payload.get("cpu_pct"),
            payload.get("mem_used_bytes"),
            payload.get("mem_total_bytes"),
            payload.get("uptime_seconds"),
            p.get("security"),
            p.get("other"),
            p.get("checked_at"),
            p.get("source"),
            p.get("detail") or None,
            None if p.get("reboot_required") is None else int(bool(p["reboot_required"])),
        ),
    )
    sid = cur.lastrowid
    for d in payload.get("disks") or []:
        conn.execute(
            "INSERT INTO disks (sample_id, mount, used_bytes, total_bytes) VALUES (?,?,?,?)",
            (sid, d.get("mount"), d.get("used_bytes"), d.get("total_bytes")),
        )
    conn.commit()
    return sid


def prune(conn, retention_hours):
    cutoff = int(time.time()) - retention_hours * 3600
    conn.execute(
        "DELETE FROM disks WHERE sample_id IN (SELECT id FROM samples WHERE ts < ?)",
        (cutoff,),
    )
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.commit()


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
        out.append(d)
    return out


def history(conn, host, since_seconds):
    cutoff = int(time.time()) - since_seconds
    rows = conn.execute(
        """SELECT id, ts, cpu_pct, mem_used_bytes, mem_total_bytes
             FROM samples WHERE host=? AND ts >= ? ORDER BY ts ASC""",
        (host, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def known_hosts(conn):
    return [r["host"] for r in conn.execute("SELECT DISTINCT host FROM samples ORDER BY host")]
