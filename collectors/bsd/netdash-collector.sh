#!/bin/sh
# netdash collector -- FreeBSD, OpenBSD and NetBSD.
# Native tools only: sysctl, df, and curl / fetch / ftp.
# Usage: netdash-collector.sh [--print]
set -eu

MODE="${1:-}"

# cron on the BSDs and BusyBox crond run with a minimal PATH that often omits
# /usr/local/bin, where curl lives. Appended, not prepended: this adds the
# standard directories when they are missing without overriding whatever the
# caller already chose.
PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/pkg/sbin:/usr/pkg/bin:/opt/homebrew/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

# FreeBSD packages live under /usr/local/etc; OpenBSD and NetBSD installs put
# the config in /etc. Take whichever exists.
CONF="${NETDASH_CONF:-}"
if [ -z "$CONF" ]; then
  for c in /usr/local/etc/netdash/collector.conf /etc/netdash/collector.conf \
           /usr/pkg/etc/netdash/collector.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
  done
  CONF="${CONF:-/usr/local/etc/netdash/collector.conf}"
fi
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
HOST="${NETDASH_HOSTNAME:-$(hostname -s 2>/dev/null || hostname)}"

# ---- CPU: two samples of kern.cp_time ----
# Three different renderings of the same counters:
#   FreeBSD  1000 50 500 20 8430                     (5, space-separated)
#   OpenBSD  7413,20,29570,0,20845,90572418          (6, comma-separated)
#   NetBSD   user = 16238, nice = 3814, ... idle = N (5, "key = value" pairs)
# Strip everything that is not a digit or separator, so all three reduce to bare
# numbers. Without this, NetBSD only worked by accident: awk coerced the "user"
# and "=" tokens to zero, which happened to leave the arithmetic right while
# quietly depending on idle staying the last field.
# Idle is last on all three, so sum every field and take the last as idle.
cp_time() { sysctl -n kern.cp_time 2>/dev/null | tr -cd '0-9 ,\n' | tr ',' ' '; }
S1=$(cp_time); sleep 1; S2=$(cp_time)
CPU=$(awk -v a="$S1" -v b="$S2" 'BEGIN{
  na=split(a,A," "); nb=split(b,B," ")
  if (na<4 || nb<4 || na!=nb) { print "null"; exit }
  dt=0; for(i=1;i<=nb;i++) dt += B[i]-A[i]
  di = B[nb]-A[na]
  if (dt<=0) { print "null"; exit }
  p=100*(dt-di)/dt; if(p<0)p=0; if(p>100)p=100
  printf "%.1f", p
}')

# ---- Memory ----
# Reported as "not reclaimable": free, inactive and laundry pages are available
# to the system, and the ZFS ARC is cache. This matches the Linux collector's
# MemAvailable and the TrueNAS poller, so every host means the same thing.
# Any sysctl that does not exist leaves the value null rather than wrong.
OSNAME=$(uname -s)
MEM_TOTAL=null
MEM_USED=null

# Must never return non-zero: under `set -e`, VAR=$(failing_cmd) aborts the
# script. Several of these sysctls are legitimately absent -- hw.physmem64 does
# not exist on OpenBSD, and kstat.zfs.misc.arcstats.size does not exist on a
# FreeBSD box without ZFS -- and querying one must yield "" rather than death.
sysctl_n() { sysctl -n "$1" 2>/dev/null || true; }

case "$OSNAME" in
FreeBSD)
  MEM_TOTAL=$(sysctl_n hw.physmem)
  PS=$(sysctl_n vm.stats.vm.v_page_size)
  FREE=$(sysctl_n vm.stats.vm.v_free_count)
  INACT=$(sysctl_n vm.stats.vm.v_inactive_count)
  LAUND=$(sysctl_n vm.stats.vm.v_laundry_count); LAUND=${LAUND:-0}
  ARC=$(sysctl_n kstat.zfs.misc.arcstats.size); ARC=${ARC:-0}
  if [ -n "$MEM_TOTAL" ] && [ -n "$PS" ] && [ -n "$FREE" ] && [ -n "$INACT" ]; then
    MEM_USED=$(awk -v t="$MEM_TOTAL" -v ps="$PS" -v f="$FREE" -v i="$INACT" -v l="$LAUND" -v a="$ARC" 'BEGIN{
      u = t - (f+i+l)*ps - a; if (u<0) u=0; printf "%.0f", u }')
  else
    MEM_TOTAL="${MEM_TOTAL:-null}"
  fi
  ;;
OpenBSD|NetBSD)
  # OpenBSD refuses to expose vm.uvmexp through sysctl -- it answers "use vmstat
  # or systat" -- and has no hw.physmem64 or vm.uvmexp2 (those are NetBSD names).
  # vmstat -s prints the page counters on both, and its figures cross-check
  # against top: free 51501 pages = 201 MiB against top's "Free: 201M".
  #
  # Field-exact matching matters here: a loose /pages free/ also matches
  # "66725 pages freed by pagedaemon", which is a lifetime counter, not a gauge.
  MEM_TOTAL=$(sysctl_n hw.physmem64); [ -n "$MEM_TOTAL" ] || MEM_TOTAL=$(sysctl_n hw.physmem)
  eval "$(vmstat -s 2>/dev/null | awk '
    $2=="bytes" && $3=="per" && $4=="page"  { ps=$1 }
    $2=="pages" && $3=="free"     && NF==3  { free=$1 }
    $2=="pages" && $3=="inactive" && NF==3  { inact=$1 }
    END { printf "VM_PS=%.0f\nVM_FREE=%.0f\nVM_INACT=%.0f\n", ps, free, inact }')"
  VM_PS=${VM_PS:-0}; VM_FREE=${VM_FREE:-0}; VM_INACT=${VM_INACT:-0}
  if [ -n "$MEM_TOTAL" ] && [ "$VM_PS" -gt 0 ] && [ "$VM_FREE" -gt 0 ]; then
    # Same shape as the FreeBSD formula: physical total minus what the system
    # can reclaim (free + inactive), so every host means the same thing.
    MEM_USED=$(awk -v t="$MEM_TOTAL" -v ps="$VM_PS" -v f="$VM_FREE" -v i="$VM_INACT" 'BEGIN{
      u = t - (f+i)*ps; if (u<0) u=0; printf "%.0f", u }')
  else
    MEM_TOTAL="${MEM_TOTAL:-null}"
  fi
  ;;
esac
[ -n "$MEM_TOTAL" ] || MEM_TOTAL=null
[ -n "$MEM_USED" ] || MEM_USED=null

# ---- Disk ----
# FreeBSD: one entry per ZFS pool (datasets share the pool's free space, so
# listing each would report the same pool dozens of times -- 40 on one test
# host),
# using used+available, the usable view, rather than zpool's raw size which
# counts raidz parity. Plus real non-ZFS mounts filtered on df -T's type column,
# because `df -t no<type>` does not exclude on FreeBSD.
# OpenBSD/NetBSD: no df -T, so keep rows whose device sits under /dev/. That is
# what drops tmpfs/kernfs/ptyfs/procfs, since -l does NOT exclude them on NetBSD
# -- its -l output is identical to plain df. NetBSD does ship ZFS, so the pool
# branch above runs there too whenever zfs(8) is present; without it those
# datasets would vanish entirely, their device names not starting with /dev/.
# -l restricts all of them to local filesystems.
ZPOOLS=""
if command -v zfs >/dev/null 2>&1; then
  ZPOOLS=$(zfs list -Hp -d 0 -o name,used,avail 2>/dev/null | awk '
    { total=$2+$3
      if (total > 0) printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), $1, $2, total }')
fi

if [ "$OSNAME" = "FreeBSD" ]; then
  OTHER=$(df -k -T -l 2>/dev/null | awk '
    NR>1 && $3+0 > 0 && $2 !~ /^(zfs|devfs|procfs|fdescfs|tmpfs|linprocfs|linsysfs|nullfs|cd9660|fusefs|nfs|nfs4|smbfs|cifs)$/ {
      mp=$7; for(i=8;i<=NF;i++) mp=mp" "$i
      gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
      printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, $4*1024, $3*1024
    }')
else
  OTHER=$(df -k -l 2>/dev/null | awk '
    NR>1 && $1 ~ /^\/dev\// && $2+0 > 0 {
      mp=$6; for(i=7;i<=NF;i++) mp=mp" "$i
      gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
      printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, $3*1024, $2*1024
    }')
fi

if [ -n "$ZPOOLS" ] && [ -n "$OTHER" ]; then DISKS="$ZPOOLS,$OTHER"
else DISKS="$ZPOOLS$OTHER"; fi

# FreeBSD and NetBSD print '{ sec = N, usec = M }'; anchor on the leading brace,
# because a greedy .* before 'sec' runs past the 'u' in 'usec' and captures the
# microseconds (~20000 days of uptime). OpenBSD prints a bare epoch instead.
RAWBOOT=$(sysctl -n kern.boottime 2>/dev/null || true)
BOOT=$(printf '%s' "$RAWBOOT" | sed -n 's/^{ *sec *= *\([0-9][0-9]*\).*/\1/p')
[ -n "$BOOT" ] || BOOT=$(printf '%s' "$RAWBOOT" | sed -n 's/^\([0-9][0-9]*\)$/\1/p')
if [ -n "$BOOT" ]; then UPTIME=$(( $(date +%s) - BOOT )); else UPTIME=0; fi
OS="$(uname -sr)"
ARCH=$(uname -m)

# ---- Patch status ----
# Read back from whatever netdash-patchcheck last wrote on its own daily
# schedule. This stays a file read on purpose: syspatch -c fetches from the
# mirror on every run, and pkg audit parses the whole vulnerability database.
# An absent, unreadable or truncated file reports null, which the server
# renders as "unknown" -- never as up to date.
PATCHES=null
for f in ${NETDASH_PATCH_STATE:-} /var/db/netdash-collector/patches.json \
         /var/lib/netdash-collector/patches.json /var/db/netdash/patches.json; do
  [ -r "$f" ] || continue
  P=$(tr -d '\n' < "$f" 2>/dev/null || true)
  case "$P" in '{'*'}') PATCHES="$P"; break ;; esac
done

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%d,"disks":[%s],"patches":%s}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS" "$PATCHES")

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
  if [ "$rc" -eq 22 ] && [ -z "$NETDASH_TOKEN" ]; then
    echo "netdash: server rejected the post and NETDASH_TOKEN is empty in $CONF" >&2
  else
    echo "netdash: post failed (curl $rc): $ERR" >&2
  fi
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
