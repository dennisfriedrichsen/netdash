#!/bin/sh
# Deploy/refresh the netdash server on this VM. Safe to re-run -- that is the upgrade path.
set -eu
[ "$(id -u)" = "0" ] || { echo "run as root (sudo)" >&2; exit 1; }
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

id -u netdash >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin netdash
install -d -m 755 /opt/netdash
rm -rf /opt/netdash/server
install -d -m 755 /opt/netdash/server /opt/netdash/server/static
install -m 644 "$SRC"/*.py /opt/netdash/server/
install -m 644 "$SRC"/static/* /opt/netdash/server/static/
install -d -m 755 /etc/netdash
install -d -o netdash -g netdash -m 750 /var/lib/netdash

if [ ! -f /etc/netdash/server.json ]; then
  install -m 640 -o root -g netdash "$SRC/config.json" /etc/netdash/server.json
  echo "installed /etc/netdash/server.json"
else
  echo "kept existing /etc/netdash/server.json"
fi
chown -R netdash:netdash /var/lib/netdash

install -m 644 "$SRC/netdash.service" /etc/systemd/system/netdash.service
systemctl daemon-reload
systemctl enable --now netdash.service
sleep 1
systemctl --no-pager --lines=5 status netdash.service || true
