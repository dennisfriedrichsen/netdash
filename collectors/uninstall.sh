#!/bin/sh
# Remove the netdash collector from a Linux or FreeBSD host.
set -eu
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl disable --now netdash-collector.timer 2>/dev/null || true
  systemctl disable --now netdash-patchcheck.timer 2>/dev/null || true
  rm -f /etc/systemd/system/netdash-collector.timer /etc/systemd/system/netdash-collector.service
  rm -f /etc/systemd/system/netdash-patchcheck.timer /etc/systemd/system/netdash-patchcheck.service
  systemctl daemon-reload
fi
TMP=$(mktemp)
crontab -l 2>/dev/null | grep -v 'netdash-collector' | grep -v 'netdash-patchcheck' > "$TMP" || true
crontab "$TMP" 2>/dev/null || true; rm -f "$TMP"
rm -f /usr/local/bin/netdash-collector /usr/local/bin/netdash-patchcheck
# The cached patch result is state this host produced, not configuration, so it
# goes with the scripts that produced it.
rm -f /var/lib/netdash-collector/patches.json /var/db/netdash-collector/patches.json
rmdir /var/lib/netdash-collector /var/db/netdash-collector 2>/dev/null || true
# Older layout, when this shared the server's data directory.
rm -f /var/lib/netdash/patches.json /var/db/netdash/patches.json
echo "removed collector (config left at /etc/netdash or /usr/local/etc/netdash)"
