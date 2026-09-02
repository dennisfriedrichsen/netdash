#!/bin/sh
# netdash collector -- Linux (Debian/Ubuntu/Raspbian).
# Native tools only: /proc/stat, /proc/meminfo, df, curl.
# Usage: netdash-collector.sh [--print]   (--print dumps JSON, posts nothing)
set -eu

MODE="${1:-}"   # capture before `set --` below reuses the positional params

CONF="${NETDASH_CONF:-/etc/netdash/collector.conf}"
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
HOST="${NETDASH_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"

# ---- CPU: two samples of /proc/stat, one second apart ----
read_cpu() { awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total, idle; exit}' /proc/stat; }
set -- $(read_cpu); T1=$1; I1=$2
sleep 1
set -- $(read_cpu); T2=$1; I2=$2
CPU=$(awk -v t1="$T1" -v i1="$I1" -v t2="$T2" -v i2="$I2" 'BEGIN{
  dt=t2-t1; di=i2-i1;
  if (dt<=0) { print "null" } else { p=100*(dt-di)/dt; if(p<0)p=0; if(p>100)p=100; printf "%.1f", p }
}')

# ---- Memory: used = MemTotal - MemAvailable ----
eval "$(awk '
  /^MemTotal:/     {t=$2}
  /^MemAvailable:/ {a=$2}
  END { printf "MEM_TOTAL=%d\nMEM_USED=%d\n", t*1024, (t-a)*1024 }
' /proc/meminfo)"

# ---- Disk: real mounts only ----
# -l keeps this to LOCAL filesystems. An NFS/SMB mount is storage owned by
# another host, which already reports it -- counting it here double-counts it
# and lets a full fileserver show up as this host being critical.
DISKS=$(df -P -B1 -l -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | awk '
  NR>1 && $2+0 > 0 {
    mp=$6; for(i=7;i<=NF;i++) mp=mp" "$i;
    gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp);
    printf "%s{\"mount\":\"%s\",\"used_bytes\":%d,\"total_bytes\":%d}", (n++?",":""), mp, $3, $2
  }')

UPTIME=$(awk '{printf "%d", $1}' /proc/uptime)
OS=$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}" )
ARCH=$(uname -m)

# Raspberry Pi OS reports PRETTY_NAME="Debian GNU/Linux 13 (trixie)", so a Pi is
# indistinguishable from any other Debian box by os-release alone. The board
# model lives in the device tree (absent on x86, present on most ARM SBCs).
# Appending it keeps the payload shape unchanged while letting the dashboard
# tell a Pi apart. Trailing NUL and the "Rev 1.4" suffix are dropped.
MODEL=""
for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
  if [ -r "$f" ]; then
    MODEL=$(tr -d '\000' < "$f" | sed -e 's/ Rev [0-9.]*$//' -e 's/[[:space:]]*$//')
    break
  fi
done
[ -n "$MODEL" ] && OS="$OS - $MODEL"

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%d,"mem_total_bytes":%d,"uptime_seconds":%d,"disks":[%s]}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

[ -n "$NETDASH_URL" ] || { echo "netdash: NETDASH_URL not set (see $CONF)" >&2; exit 1; }
rc=0
ERR=$(curl -fsS --connect-timeout 5 --max-time 15 -X POST "$NETDASH_URL" \
  -H "Content-Type: application/json" \
  -H "X-Netdash-Token: $NETDASH_TOKEN" \
  --data "$JSON" -o /dev/null 2>&1) || rc=$?
[ "$rc" -eq 0 ] && exit 0

case "$rc" in
  6|7|28|35)
    # Could not resolve / connect / timed out / TLS. The server is simply not
    # reachable from here -- a laptop off the LAN, or the server restarting.
    # Expected and self-correcting, so stay silent: this runs every 60s and
    # would otherwise add ~1440 lines a day to the launchd or cron log.
    exit 0 ;;
esac
# Anything else (notably 22 = HTTP 4xx/5xx, i.e. a bad token) is a real
# misconfiguration that will not fix itself. Say so.
if [ "$rc" -eq 22 ] && [ -z "$NETDASH_TOKEN" ]; then
  echo "netdash: server rejected the post and NETDASH_TOKEN is empty in $CONF" >&2
else
  echo "netdash: post failed (curl $rc): $ERR" >&2
fi
exit "$rc"
