#!/bin/sh
# netdash collector -- macOS. Native tools only: top, vm_stat, sysctl, df, curl.
# Usage: netdash-collector.sh [--print]
set -eu

MODE="${1:-}"

# cron on the BSDs and BusyBox crond run with a minimal PATH that often omits
# /usr/local/bin, where curl lives. Appended, not prepended: this adds the
# standard directories when they are missing without overriding whatever the
# caller already chose.
PATH="$PATH:/usr/local/sbin:/usr/local/bin:/opt/homebrew/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

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

# ---- Disk: one entry per APFS container ----
# APFS volumes in a container share its free space: every volume reports the
# container's total but only its own Used, so no single volume's used/total is
# the disk's fullness. On rhenium the boot container showed Data at 83% while
# the container was really 90% full. So group by container (disk3s1s1 and
# disk3s5 are both disk3) and report used = total - available -- the same
# treatment ZFS pools get on FreeBSD and TrueNAS.
#
# Containers are then dropped unless they hold storage you could actually free:
#   * every volume read-only  -> a mounted image. Xcode's CoreSimulator runtimes
#     are read-only APFS images, permanently ~97% full because that is what a
#     packed image is. They made argon read CRITICAL forever.
#   * every volume nobrowse   -> Apple's own "not user-facing" marker. Catches
#     the xarts/iSCPreboot/Hardware container, which is read-WRITE and so not
#     caught by the rule above.
# Neither test alone is sufficient: Data is nobrowse, and "/" is read-only, so
# each is rescued by the other volume in its container. Enumerating Apple's
# synthetic volume names was the previous approach and it broke the moment
# argon (Apple Silicon, macOS 26) showed a set rhenium (Intel) does not have.
#
# -l keeps this to local filesystems: the NAS is storage the NAS reports.
DISKS=$( { mount 2>/dev/null; printf '@@DF@@\n'; df -k -l 2>/dev/null; } | awk '
  /^@@DF@@$/ { indf=1; next }

  # --- mount(8): collect per-container flags ---
  !indf {
    if ($1 !~ /^\/dev\//) next
    d=$1; sub(/^\/dev\//,"",d); sub(/s[0-9]+.*$/,"",d)
    seenmount=1
    if (match($0, /\([^)]*\)$/)) {
      f=substr($0, RSTART+1, RLENGTH-2)
      if (f !~ /read-only/) rw[d]=1      # container has a writable volume
      if (f !~ /nobrowse/)  br[d]=1      # container has a user-visible volume
    } else { rw[d]=1; br[d]=1 }
    next
  }

  # --- df -k -l ---
  $1 == "Filesystem" { next }
  $1 ~ /^\/dev\// && $2+0 > 0 {
    d=$1; sub(/^\/dev\//,"",d); sub(/s[0-9]+.*$/,"",d)
    mp=$9; for(i=10;i<=NF;i++) mp=mp" "$i
    if ($2+0 > tot[d]) tot[d]=$2+0
    if (!(d in av) || $4+0 < av[d]) av[d]=$4+0
    if (mp == "/") lbl[d]="/"
    else if (!(d in lbl)) lbl[d]=mp
    else if (lbl[d] != "/" && lbl[d] ~ /^\/System\/Volumes\//) lbl[d]=mp
    seen[d]=1
  }

  END {
    for (d in seen) {
      # If mount(8) gave us nothing, report everything rather than nothing --
      # filtering on absent data would silently drop every disk.
      if (seenmount && (!(d in rw) || !(d in br))) continue
      u = tot[d] - av[d]; if (u < 0) u = 0
      mp = lbl[d]
      gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
      printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, u*1024, tot[d]*1024
    }
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
if [ "$rc" -eq 22 ] && [ -z "$NETDASH_TOKEN" ]; then
  echo "netdash: server rejected the post and NETDASH_TOKEN is empty in $CONF" >&2
else
  echo "netdash: post failed (curl $rc): $ERR" >&2
fi
exit "$rc"
