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
    uptime_seconds  INTEGER
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


def connect(path):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_sample(conn, payload, source="push"):
    """payload: dict from a collector. Returns the new sample id."""
    ts = int(payload.get("ts") or time.time())
    cur = conn.execute(
        """INSERT INTO samples
             (host, ts, os, source, cpu_pct, mem_used_bytes, mem_total_bytes, uptime_seconds)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            payload["host"],
            ts,
            payload.get("os"),
            source,
            payload.get("cpu_pct"),
            payload.get("mem_used_bytes"),
            payload.get("mem_total_bytes"),
            payload.get("uptime_seconds"),
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
