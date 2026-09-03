#!/bin/sh
# Deploy/refresh the netdash server on FreeBSD. Safe to re-run -- that is the
# upgrade path. Mirrors deploy.sh, but FreeBSD has no systemd and its base
# system has no useradd(8), so service management and account creation are
# rc.d/pw(8) here; everything else -- paths chosen, upgrade behaviour,
# ownership -- follows deploy.sh's Linux layout as closely as the platform
# allows.
set -eu
[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found -- run: pkg install python3" >&2
  exit 1
}

# FreeBSD convention: third-party software and its config live under
# /usr/local (matching where the bsd collector already puts
# collector.conf), and variable data that isn't a package's own state goes
# under /var/db rather than Linux's /var/lib.
APP_DIR=/usr/local/netdash/server
CONF_DIR=/usr/local/etc/netdash
DATA_DIR=/var/db/netdash

pw groupadd netdash 2>/dev/null || true
id -u netdash >/dev/null 2>&1 ||
  pw useradd netdash -g netdash -c "netdash" -d /nonexistent -s /usr/sbin/nologin

install -d -m 755 /usr/local/netdash
rm -rf "$APP_DIR"
install -d -m 755 "$APP_DIR" "$APP_DIR/static"
install -m 644 "$SRC"/*.py "$APP_DIR"/
install -m 644 "$SRC"/static/* "$APP_DIR/static"/
install -d -m 755 "$CONF_DIR"
install -d -o netdash -g netdash -m 750 "$DATA_DIR"

if [ ! -f "$CONF_DIR/server.json" ]; then
  # Same file as the Linux default, with the one path that differs by
  # platform rewritten -- everything else in it is OS-agnostic.
  sed 's#"db_path": "/var/lib/netdash/netdash.db"#"db_path": "/var/db/netdash/netdash.db"#' \
    "$SRC/config.example.json" > "$CONF_DIR/server.json"
  chown root:netdash "$CONF_DIR/server.json"
  chmod 640 "$CONF_DIR/server.json"
  echo "installed $CONF_DIR/server.json"
else
  echo "kept existing $CONF_DIR/server.json"
fi
chown -R netdash:netdash "$DATA_DIR"

install -m 555 "$SRC/netdash.rc" /usr/local/etc/rc.d/netdash
sysrc netdash_enable=YES >/dev/null

# restart, not `onestart`: onestart is a no-op when already running, which
# left the old code loaded in memory on a re-run of this script -- same
# reasoning as deploy.sh's `systemctl restart` over `enable --now`.
service netdash restart
sleep 1
service netdash status || true
