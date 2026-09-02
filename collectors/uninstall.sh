#!/bin/sh
# Remove the netdash collector from a Linux or FreeBSD host.
set -eu
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl disable --now netdash-collector.timer 2>/dev/null || true
  rm -f /etc/systemd/system/netdash-collector.timer /etc/systemd/system/netdash-collector.service
  systemctl daemon-reload
fi
TMP=$(mktemp); crontab -l 2>/dev/null | grep -v 'netdash-collector' > "$TMP" || true
crontab "$TMP" 2>/dev/null || true; rm -f "$TMP"
rm -f /usr/local/bin/netdash-collector
echo "removed collector (config left at /etc/netdash or /usr/local/etc/netdash)"
