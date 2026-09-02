#!/bin/sh
# netdash collector -- FreeBSD. Native tools only: sysctl, df, curl or fetch.
# Usage: netdash-collector.sh [--print]
set -eu

MODE="${1:-}"

CONF="${NETDASH_CONF:-/usr/local/etc/netdash/collector.conf}"
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
HOST="${NETDASH_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"

# ---- CPU: two samples of kern.cp_time (user nice sys intr idle) ----
cp_time() { sysctl -n kern.cp_time; }
S1=$(cp_time); sleep 1; S2=$(cp_time)
CPU=$(awk -v a="$S1" -v b="$S2" 'BEGIN{
  na=split(a,A," "); nb=split(b,B," ")
  if (na<5 || nb<5) { print "null"; exit }
  dt=0; for(i=1;i<=5;i++) dt += B[i]-A[i]
  di = B[5]-A[5]
  if (dt<=0) { print "null"; exit }
  p=100*(dt-di)/dt; if(p<0)p=0; if(p>100)p=100
  printf "%.1f", p
}')

# ---- Memory: physmem minus what is genuinely reclaimable ----
MEM_TOTAL=$(sysctl -n hw.physmem)
PS=$(sysctl -n vm.stats.vm.v_page_size)
FREE=$(sysctl -n vm.stats.vm.v_free_count)
INACT=$(sysctl -n vm.stats.vm.v_inactive_count)
LAUND=$(sysctl -n vm.stats.vm.v_laundry_count 2>/dev/null || echo 0)
MEM_USED=$(awk -v t="$MEM_TOTAL" -v ps="$PS" -v f="$FREE" -v i="$INACT" -v l="$LAUND" 'BEGIN{
  u = t - (f+i+l)*ps; if (u<0) u=0; printf "%.0f", u }')

# ---- Disk: real filesystems (UFS/ZFS); skip pseudo-filesystems ----
DISKS=$(df -k -t noprocfs,nodevfs,nofdescfs,notmpfs,nolinprocfs,nolinsysfs 2>/dev/null | awk '
  NR>1 && $2+0 > 0 {
    mp=$6; for(i=7;i<=NF;i++) mp=mp" "$i
    gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
    printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, $3*1024, $2*1024
  }')

BOOT=$(sysctl -n kern.boottime | sed -n 's/.*sec *= *\([0-9]*\).*/\1/p')
UPTIME=$(( $(date +%s) - ${BOOT:-0} ))
OS="$(uname -sr)"
ARCH=$(uname -m)

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%d,"disks":[%s]}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

[ -n "$NETDASH_URL" ] || { echo "netdash: NETDASH_URL not set (see $CONF)" >&2; exit 1; }

if command -v curl >/dev/null 2>&1; then
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
else
  # fetch(1) is in the FreeBSD base system; curl is not. It has no granular
  # exit codes, so unreachable-vs-misconfigured cannot be told apart here;
  # this branch stays loud. Install curl on a host that moves networks.
  exec fetch -q -T 10 -o /dev/null \
    --method=POST \
    --header="Content-Type: application/json" \
    --header="X-Netdash-Token: $NETDASH_TOKEN" \
    --body="$JSON" "$NETDASH_URL"
fi
