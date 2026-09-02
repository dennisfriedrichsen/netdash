#!/usr/bin/env python3
"""Backfill the DB with synthetic samples so the dashboard can be verified
before any real collector exists.  Usage: python3 seed_fake.py [config.json]"""

import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

# host -> (os label, mem GB, [(mount, size GB, base fullness)], cpu baseline)
HOSTS = {
    "mac-desktop":  ("macOS 15.6",        32, [("/", 1000, 0.62)], 18),
    "mac-laptop":   ("macOS 15.6",        16, [("/", 500, 0.41)], 9),
    "pi-arm64":     ("Debian 13 (arm64)",  8, [("/", 64, 0.55)], 12),
    "bsd-server":   ("FreeBSD 15.0",      16, [("/", 240, 0.33), ("/var", 100, 0.19)], 7),
    "pi-armv7":     ("Raspbian 13",        4, [("/", 32, 0.78)], 22),
    "linux-server": ("Ubuntu 24.04",      16, [("/", 480, 0.66)], 15),
    "linux-small":  ("Debian 13",          8, [("/", 120, 0.49)], 26),
    "truenas":      ("TrueNAS CORE 13.0", 64, [("tank", 16000, 0.71),
                                               ("boot-pool", 240, 0.12)], 11),
}
# one host deliberately unhealthy so the warning/critical styling is exercised
STRESSED = {"pi-armv7": ("disk", 0.93), "linux-small": ("cpu", 88)}


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    cfg = json.load(open(cfg_path))
    conn = db.connect(cfg["db_path"])

    now = int(time.time())
    step = 45          # seconds between synthetic samples
    minutes = 90
    n = (minutes * 60) // step

    conn.execute("DELETE FROM disks")
    conn.execute("DELETE FROM samples")
    conn.commit()

    total = 0
    for host, (oslabel, mem_gb, mounts, cpu_base) in HOSTS.items():
        phase = random.random() * math.tau
        mem_total = mem_gb * 1024 ** 3
        for i in range(n, -1, -1):
            ts = now - i * step
            wave = math.sin(phase + i / 9.0) * 0.5 + math.sin(phase * 2 + i / 3.0) * 0.2
            cpu = cpu_base + wave * cpu_base * 0.8 + random.uniform(-3, 3)
            memfrac = 0.45 + 0.12 * math.sin(phase + i / 25.0) + random.uniform(-0.02, 0.02)

            stress = STRESSED.get(host)
            if stress and stress[0] == "cpu":
                cpu = stress[1] + random.uniform(-6, 6)

            payload = {
                "host": host, "ts": ts, "os": oslabel,
                "cpu_pct": round(max(0.2, min(100.0, cpu)), 1),
                "mem_used_bytes": int(mem_total * max(0.05, min(0.97, memfrac))),
                "mem_total_bytes": mem_total,
                "uptime_seconds": 3600 * 24 * 11 + i * step,
                "disks": [],
            }
            for mount, size_gb, full in mounts:
                if stress and stress[0] == "disk" and mount == "/":
                    full = stress[1]
                total_b = size_gb * 1024 ** 3
                drift = 1 + (n - i) * 0.00002
                payload["disks"].append({
                    "mount": mount,
                    "used_bytes": int(total_b * min(0.995, full * drift)),
                    "total_bytes": total_b,
                })
            db.insert_sample(conn, payload,
                             source="truenas" if host == "truenas" else "push")
            total += 1

    print("seeded %d samples across %d hosts (%d min of history)"
          % (total, len(HOSTS), minutes))


if __name__ == "__main__":
    main()
