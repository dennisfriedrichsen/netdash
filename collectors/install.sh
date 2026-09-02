#!/bin/sh
# netdash collector installer for Linux and BSD hosts.
# macOS hosts use the formula from dennisfriedrichsen/homebrew-tap instead.
#
#   sudo ./install.sh --url https://netdash.example/api/ingest --token <TOKEN>
#
# Re-running is the upgrade path: it overwrites the script and leaves config alone.
#
# Linux:   Debian/Ubuntu/Raspbian, Fedora, Arch, openSUSE, Alpine
# BSD:     FreeBSD, OpenBSD, NetBSD
set -eu

URL=""; TOKEN=""; INTERVAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url)      URL="$2"; shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = "0" ] || { echo "run as root (sudo/doas)" >&2; exit 1; }

SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OS=$(uname -s)

case "$OS" in
  Linux)   SRC="$SRC_DIR/linux/netdash-collector.sh"; CONF_DIR=/etc/netdash ;;
  FreeBSD) SRC="$SRC_DIR/bsd/netdash-collector.sh";   CONF_DIR=/usr/local/etc/netdash ;;
  OpenBSD|NetBSD)
           SRC="$SRC_DIR/bsd/netdash-collector.sh";   CONF_DIR=/etc/netdash ;;
  Darwin)  echo "macOS: use the Homebrew formula (brew install dennisfriedrichsen/tap/netdash-collector)" >&2; exit 1 ;;
  *)       echo "unsupported OS: $OS" >&2; exit 1 ;;
esac
[ -f "$SRC" ] || { echo "collector not found: $SRC" >&2; exit 1; }

# ---- the collector needs an HTTP client; say how to get one on THIS distro ----
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1 \
   && ! command -v fetch >/dev/null 2>&1; then
  if   command -v apk     >/dev/null 2>&1; then HINT="apk add curl"
  elif command -v dnf     >/dev/null 2>&1; then HINT="dnf install curl"
  elif command -v yum     >/dev/null 2>&1; then HINT="yum install curl"
  elif command -v pacman  >/dev/null 2>&1; then HINT="pacman -S curl"
  elif command -v zypper  >/dev/null 2>&1; then HINT="zypper install curl"
  elif command -v apt-get >/dev/null 2>&1; then HINT="apt-get install curl"
  elif command -v pkg_add >/dev/null 2>&1; then HINT="pkg_add curl"
  elif command -v pkgin   >/dev/null 2>&1; then HINT="pkgin install curl"
  elif command -v pkg     >/dev/null 2>&1; then HINT="pkg install curl"
  else HINT="install curl"
  fi
  echo "netdash: no curl, wget or fetch on this host. Install one first:" >&2
  echo "  $HINT" >&2
  exit 1
fi

BIN=/usr/local/bin/netdash-collector
CONF="$CONF_DIR/collector.conf"

# OpenBSD and NetBSD have no `install -D`-style parent creation guarantees, and
# some minimal images lack /usr/local/bin entirely.
mkdir -p "$CONF_DIR" /usr/local/bin
chmod 755 "$CONF_DIR" 2>/dev/null || true
cp "$SRC" "$BIN"
chmod 755 "$BIN"
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
schedule_cron() {
  # One-minute granularity is the floor, which is inside the 30-60s target.
  CRON_LINE="* * * * * $BIN >/dev/null 2>&1"
  TMP=$(mktemp 2>/dev/null || echo /tmp/netdash.cron.$$)
  crontab -l 2>/dev/null | grep -v 'netdash-collector' > "$TMP" || true
  echo "$CRON_LINE" >> "$TMP"
  crontab "$TMP"
  rm -f "$TMP"
  echo "scheduled: root crontab, every 60s"
}

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

elif command -v rc-update >/dev/null 2>&1; then
  # Alpine and other OpenRC systems: cron works, but crond is not running by
  # default on a minimal install, so scheduling silently does nothing without this.
  schedule_cron
  if ! rc-service crond status >/dev/null 2>&1; then
    rc-update add crond default >/dev/null 2>&1 || true
    rc-service crond start >/dev/null 2>&1 || true
    echo "enabled and started crond (OpenRC)"
  fi

else
  schedule_cron
  # OpenBSD/NetBSD run cron from init already; FreeBSD likewise.
  if [ "$OS" = "FreeBSD" ] || [ "$OS" = "NetBSD" ] || [ "$OS" = "OpenBSD" ]; then
    command -v service >/dev/null 2>&1 && service cron status >/dev/null 2>&1 || true
  fi
fi

echo "done."
