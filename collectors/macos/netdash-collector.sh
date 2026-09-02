#!/bin/sh
# netdash collector -- macOS. Native tools only: top, vm_stat, sysctl, df, curl.
# Usage: netdash-collector.sh [--print]
set -eu

MODE="${1:-}"

CONF="${NETDASH_CONF:-/usr/local/etc/netdash/collector.conf}"
[ -f "$CONF" ] || CONF="/opt/homebrew/etc/netdash/collector.conf"
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
HOST="${NETDASH_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"

# ---- CPU: second sample from `top` (the first is a since-boot average) ----
CPU=$(top -l 2 -n 0 -s 1 2>/dev/null | awk '
  /^CPU usage:/ { last=$0 }
  END {
    if (last == "") { print "null"; exit }
    if (match(last, /[0-9.]+% idle/)) {
      idle = substr(last, RSTART, RLENGTH-6) + 0
      p = 100 - idle; if (p<0) p=0; if (p>100) p=100
      printf "%.1f", p
    } else print "null"
  }')

# ---- Memory: active + wired + compressed, against hw.memsize ----
MEM_TOTAL=$(sysctl -n hw.memsize)
MEM_USED=$(vm_stat | awk '
  /page size of/ { for(i=1;i<=NF;i++) if ($i+0 > 4000) { ps=$i+0; break } }
  /^Pages active/                  { gsub(/\./,"",$3); active=$3 }
  /^Pages wired down/              { gsub(/\./,"",$4); wired=$4 }
  /^Pages occupied by compressor/  { gsub(/\./,"",$5); comp=$5 }
  END { if (!ps) ps=4096; printf "%.0f", (active+wired+comp)*ps }')

# ---- Disk: real volumes; skip APFS synthetic/system helper mounts ----
DISKS=$(df -k 2>/dev/null | awk '
  NR>1 && $1 ~ /^\/dev\// && $2+0 > 0 {
    mp=$9; for(i=10;i<=NF;i++) mp=mp" "$i
    if (mp ~ /^\/System\/Volumes\/(VM|Preboot|Update|xarts|iSCPreboot|Hardware|Recovery)/) next
    if (mp ~ /^\/private\/var\/vm/) next
    gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
    printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, $3*1024, $2*1024
  }')

# kern.boottime reads '{ sec = N, usec = M } ...'. Anchor on the leading brace:
# a greedy .* before 'sec' happily runs past the 'u' in 'usec' and captures the
# microseconds instead, which reads as ~20000 days of uptime.
BOOT=$(sysctl -n kern.boottime | sed -n 's/^{ *sec *= *\([0-9][0-9]*\).*/\1/p')
UPTIME=$(( $(date +%s) - ${BOOT:-0} ))
OS="$(sw_vers -productName 2>/dev/null || echo macOS) $(sw_vers -productVersion 2>/dev/null || echo '')"
ARCH=$(uname -m)

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%d,"disks":[%s]}' \
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
echo "netdash: post failed (curl $rc): $ERR" >&2
exit "$rc"
