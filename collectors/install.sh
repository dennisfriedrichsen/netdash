#!/bin/sh
# netdash collector installer for Linux and FreeBSD hosts.
# macOS hosts use the Homebrew formula in /homebrew instead.
#
#   sudo ./install.sh --url http://10.0.0.3:8080/api/ingest --token <TOKEN>
#
# Re-running is the upgrade path: it overwrites the script and leaves config alone.
set -eu

URL=""; TOKEN=""; INTERVAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url)      URL="$2"; shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS=$(uname -s)

case "$OS" in
  Linux)   SRC="$SRC_DIR/linux/netdash-collector.sh";   CONF_DIR=/etc/netdash ;;
  FreeBSD) SRC="$SRC_DIR/freebsd/netdash-collector.sh"; CONF_DIR=/usr/local/etc/netdash ;;
  Darwin)  echo "macOS: use the Homebrew formula (brew install <tap>/netdash-collector)" >&2; exit 1 ;;
  *)       echo "unsupported OS: $OS" >&2; exit 1 ;;
esac
[ -f "$SRC" ] || { echo "collector not found: $SRC" >&2; exit 1; }

BIN=/usr/local/bin/netdash-collector
CONF="$CONF_DIR/collector.conf"

install -d -m 755 "$CONF_DIR"
install -m 755 "$SRC" "$BIN"
echo "installed $BIN"

if [ -f "$CONF" ] && [ -z "$URL" ]; then
  echo "kept existing $CONF"
else
  [ -n "$URL" ] || { echo "--url is required on first install" >&2; exit 2; }
  umask 077
  cat > "$CONF" <<EOF
# netdash collector configuration
NETDASH_URL="$URL"
NETDASH_TOKEN="$TOKEN"
# NETDASH_HOSTNAME="override-if-needed"
EOF
  chmod 600 "$CONF"
  echo "wrote $CONF"
fi

# ---- verify before scheduling: a broken collector should fail loudly, now ----
echo "--- test run ---"
"$BIN" --print || { echo "collector failed; not scheduling" >&2; exit 1; }
echo "--- test post ---"
"$BIN" && echo "post OK" || { echo "post failed; check URL/token/firewall" >&2; exit 1; }

# ---- schedule ----
if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  SEC="${INTERVAL:-30}"
  cat > /etc/systemd/system/netdash-collector.service <<EOF
[Unit]
Description=netdash metric collector
After=network-online.target

[Service]
Type=oneshot
ExecStart=$BIN
EOF
  cat > /etc/systemd/system/netdash-collector.timer <<EOF
[Unit]
Description=Run netdash collector every ${SEC}s

[Timer]
OnBootSec=45
OnUnitActiveSec=${SEC}
AccuracySec=5s
Unit=netdash-collector.service

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now netdash-collector.timer
  echo "scheduled: systemd timer every ${SEC}s"
  systemctl list-timers netdash-collector.timer --no-pager 2>/dev/null | head -3 || true
else
  # cron: one-minute granularity is the floor, which is inside the 30-60s target.
  CRON_LINE="* * * * * $BIN >/dev/null 2>&1"
  TMP=$(mktemp)
  crontab -l 2>/dev/null | grep -v 'netdash-collector' > "$TMP" || true
  echo "$CRON_LINE" >> "$TMP"
  crontab "$TMP"
  rm -f "$TMP"
  echo "scheduled: root crontab, every 60s"
fi

echo "done."
