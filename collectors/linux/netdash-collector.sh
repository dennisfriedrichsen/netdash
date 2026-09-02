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
DISKS=$(df -P -B1 -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null | awk '
  NR>1 && $2+0 > 0 {
    mp=$6; for(i=7;i<=NF;i++) mp=mp" "$i;
    gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp);
    printf "%s{\"mount\":\"%s\",\"used_bytes\":%d,\"total_bytes\":%d}", (n++?",":""), mp, $3, $2
  }')

UPTIME=$(awk '{printf "%d", $1}' /proc/uptime)
OS=$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}" )
ARCH=$(uname -m)

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%d,"mem_total_bytes":%d,"uptime_seconds":%d,"disks":[%s]}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

[ -n "$NETDASH_URL" ] || { echo "netdash: NETDASH_URL not set (see $CONF)" >&2; exit 1; }
exec curl -fsS --max-time 10 -X POST "$NETDASH_URL" \
  -H "Content-Type: application/json" \
  -H "X-Netdash-Token: $NETDASH_TOKEN" \
  --data "$JSON" -o /dev/null
