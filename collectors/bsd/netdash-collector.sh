#!/bin/sh
# netdash collector -- FreeBSD, OpenBSD and NetBSD.
# Native tools only: sysctl, df, and curl / fetch / ftp.
# Usage: netdash-collector.sh [--print]
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.5.1"

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

# ---- Jail ----
# A FreeBSD jail runs on its host's kernel, so every kernel counter read below
# -- cp_time, physmem, the page queues, boottime, vm_guest -- describes the
# HOST. Reported as-is, a jail on a TrueNAS box shows that box's CPU, memory
# and uptime under the jail's name. security.jail.jailed is 1 only inside one;
# the other BSDs have no such sysctl and read as not jailed.
JAILED=$(sysctl -n security.jail.jailed 2>/dev/null || true)

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
if [ "$JAILED" = 1 ]; then
  # Inside a jail ps sees only the jail's own processes, so their summed %cpu
  # is the jail's share. ps counts one fully busy core as 100, so divide by the
  # core count to land on the same 0-100 scale as cp_time. %cpu is ps's
  # decaying average rather than a one-second delta: it lags a spike by a few
  # seconds, which is the price of not needing racct on the host.
  NCPU=$(sysctl -n hw.ncpu 2>/dev/null || true)
  CPU=$(ps -ax -o %cpu= 2>/dev/null | awk -v n="${NCPU:-0}" '
    { s += $1 } END { if (n+0 <= 0) { print "null"; exit }
                      p=s/n; if(p>100)p=100; printf "%.1f", p }')
else
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
fi

# ---- Memory ----
# Reported as "not reclaimable": free and inactive pages are available to the
# system, and the ZFS ARC is cache. Laundry pages are not: they are dirty, and
# must be written out -- usually to swap -- before they can be reused, so they
# count as used. This matches the Linux collector's MemAvailable (which counts
# anonymous memory as used) and the TrueNAS poller (active + wired + laundry,
# less ARC), so every host means the same thing (issue #7).
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
  if [ "$JAILED" = 1 ]; then
    # The page queues are the host's. The jail's footprint is its processes'
    # resident memory, against the host's RAM -- a jail has no memory size of
    # its own unless rctl caps it. Shared libraries are counted once per
    # process that maps them, so this reads a little high, never low.
    MEM_USED=$(ps -ax -o rss= 2>/dev/null | awk '{ s += $1 } END { printf "%.0f", s*1024 }')
    [ -n "$MEM_TOTAL" ] || MEM_TOTAL=null
  else
  PS=$(sysctl_n vm.stats.vm.v_page_size)
  FREE=$(sysctl_n vm.stats.vm.v_free_count)
  INACT=$(sysctl_n vm.stats.vm.v_inactive_count)
  ARC=$(sysctl_n kstat.zfs.misc.arcstats.size); ARC=${ARC:-0}
  if [ -n "$MEM_TOTAL" ] && [ -n "$PS" ] && [ -n "$FREE" ] && [ -n "$INACT" ]; then
    MEM_USED=$(awk -v t="$MEM_TOTAL" -v ps="$PS" -v f="$FREE" -v i="$INACT" -v a="$ARC" 'BEGIN{
      u = t - (f+i)*ps - a; if (u<0) u=0; printf "%.0f", u }')
  else
    MEM_TOTAL="${MEM_TOTAL:-null}"
  fi
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
  #
  # What counts as reclaimable differs. NetBSD keeps file cache on the active
  # queue as well as the inactive one, so free + inactive counted most of the
  # cache as used: an idle 512 MiB host with no swap in use read 77%, and 98%
  # whenever the pagedaemon happened to have drained the inactive queue. NetBSD
  # reports the whole cache as "cached file pages", so subtract that in place
  # of inactive -- not as well as it: the inactive pages overlap the file
  # pages, and subtracting both would count part of the cache twice. OpenBSD
  # has no such counter and keeps free + inactive (issue #4).
  MEM_TOTAL=$(sysctl_n hw.physmem64); [ -n "$MEM_TOTAL" ] || MEM_TOTAL=$(sysctl_n hw.physmem)
  eval "$(vmstat -s 2>/dev/null | awk '
    $2=="bytes" && $3=="per" && $4=="page"  { ps=$1 }
    $2=="pages" && $3=="free"     && NF==3  { free=$1 }
    $2=="pages" && $3=="inactive" && NF==3  { inact=$1 }
    $2=="cached" && $3=="file" && $4=="pages" && NF==4 { file=$1; hasfile=1 }
    END { printf "VM_PS=%.0f\nVM_FREE=%.0f\nVM_INACT=%.0f\nVM_FILE=%s\n", ps, free, inact, (hasfile ? sprintf("%.0f", file) : "") }')"
  VM_PS=${VM_PS:-0}; VM_FREE=${VM_FREE:-0}; VM_INACT=${VM_INACT:-0}
  VM_RECLAIM=${VM_FILE:-$VM_INACT}
  if [ -n "$MEM_TOTAL" ] && [ "$VM_PS" -gt 0 ] && [ "$VM_FREE" -gt 0 ]; then
    # Same shape as the FreeBSD formula: physical total minus what the system
    # can reclaim, so every host means the same thing.
    MEM_USED=$(awk -v t="$MEM_TOTAL" -v ps="$VM_PS" -v f="$VM_FREE" -v r="$VM_RECLAIM" 'BEGIN{
      u = t - (f+r)*ps; if (u<0) u=0; printf "%.0f", u }')
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
  # In a jail, / is reported on its own below, whatever its filesystem.
  OTHER=$(df -k -T -l 2>/dev/null | awk -v jailed="$JAILED" '
    NR>1 && $3+0 > 0 && $2 !~ /^(zfs|devfs|procfs|fdescfs|tmpfs|linprocfs|linsysfs|nullfs|cd9660|fusefs|nfs|nfs4|smbfs|cifs)$/ {
      mp=$7; for(i=8;i<=NF;i++) mp=mp" "$i
      if (jailed == 1 && mp == "/") next
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

# A jail's root is usually a ZFS dataset, which the filter above drops in favour
# of whole pools -- and a jail cannot see the pools. Its root is the one disk
# that is its own, so it is reported directly. Datasets share their pool's free
# space, so this is the host pool's fullness as seen from the jail. nullfs
# mounts stay excluded: they are the host's storage lent in, not the jail's.
if [ "$JAILED" = 1 ]; then
  ZPOOLS=""
  JROOT=$(df -k / 2>/dev/null | awk '
    NR==2 && $2+0 > 0 { printf "{\"mount\":\"/\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", $3*1024, ($3+$4)*1024 }')
  if [ -n "$JROOT" ] && [ -n "$OTHER" ]; then OTHER="$JROOT,$OTHER"
  else OTHER="$JROOT$OTHER"; fi
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
if [ "$JAILED" = 1 ]; then
  # boottime is the host's. A jail has no boot of its own, but its oldest
  # process started when the jail did, give or take a few seconds.
  # A jail with nothing running in it but this script has no age to read --
  # null, not the host's uptime, which is the one number known to be wrong.
  JUP=$(ps -ax -o etimes= 2>/dev/null | awk '$1+0 > m { m = $1+0 } END { if (m > 0) printf "%.0f", m }')
  UPTIME=${JUP:-null}
  # uname -r is the host's kernel; the jail's own release is its userland,
  # which is what its packages and patches are built against.
  JREL=$(freebsd-version -u 2>/dev/null || true)
  [ -n "$JREL" ] && OS="FreeBSD $JREL"
fi

# ---- Virtualisation ----
# FreeBSD answers directly. OpenBSD and NetBSD have no equivalent sysctl, so
# the DMI strings are matched instead: a recognised hypervisor vendor names it,
# a real hardware vendor means bare metal, and nothing readable means null
# rather than a guess. Verified: kern.vm_guest reads "none" on the bare-metal
# FreeBSD host.
VIRT=null
virt_name() {
  case "$1" in
    *BHYVE*|*bhyve*)        echo bhyve ;;
    *QEMU*|*qemu*)          echo kvm ;;
    *VMware*|*vmware*)      echo vmware ;;
    *innotek*|*VirtualBox*) echo oracle ;;
    *Microsoft*|*Hyper-V*)  echo microsoft ;;
    *Xen*|*xen*)            echo xen ;;
    *Parallels*)            echo parallels ;;
    *)                      echo "" ;;
  esac
}
case "$OSNAME" in
FreeBSD)
  # kern.vm_guest inside a jail answers for the host, so a jail on a physical
  # box would read as bare metal.
  if [ "$JAILED" = 1 ]; then V=jail; else V=$(sysctl_n kern.vm_guest); fi
  [ -n "$V" ] && VIRT="\"$V\""
  ;;
OpenBSD|NetBSD)
  HWV=""
  for k in hw.vendor hw.product machdep.dmi.system-vendor machdep.dmi.system-product; do
    HWV="$HWV $(sysctl_n "$k")"
  done
  V=$(virt_name "$HWV")
  if [ -n "$V" ]; then
    VIRT="\"$V\""
  elif [ -n "$(printf '%s' "$HWV" | tr -d ' ')" ]; then
    # DMI readable and no hypervisor vendor in it: real hardware.
    VIRT='"none"'
  fi
  ;;
esac

# ---- Patch status ----
# Read back from whatever netdash-patchcheck last wrote on its own daily
# schedule. This stays a file read on purpose: syspatch -c fetches from the
# mirror on every run, and pkg audit parses the whole vulnerability database.
# An absent, unreadable or truncated file reports null, which the server
# renders as "unknown" -- never as up to date.
PATCHES=null
# An explicit NETDASH_PATCH_STATE is authoritative: if the admin names a file
# and it is missing or malformed, that reports unknown rather than quietly
# falling through to some other host-state file left in a default location.
if [ -n "${NETDASH_PATCH_STATE:-}" ]; then
  PATCH_FILES="$NETDASH_PATCH_STATE"
else
  PATCH_FILES="/var/db/netdash-collector/patches.json /var/lib/netdash-collector/patches.json /var/db/netdash/patches.json"
fi
for f in $PATCH_FILES; do
  [ -r "$f" ] || continue
  P=$(tr -d '\n' < "$f" 2>/dev/null || true)
  case "$P" in '{'*'}') PATCHES="$P"; break ;; esac
done
# Run inside a jail by its host (see "Jails, from the host" below), the host's
# copy of the jail's patch check arrives here directly: it lives on the host,
# never in the jail's filesystem where the jail could rewrite it.
case "${NETDASH_PATCHES_JSON:-}" in
  '{'*'}'|null) PATCHES="$NETDASH_PATCHES_JSON" ;;
esac

# ---- ZFS pool health ----
# `zpool status -p` as text, parsed by the server (server/zfs.py): a parser
# fix then ships with the server rather than to every ZFS host. -p gives exact
# error counts; an OpenZFS too old for it falls back to the plain form, whose
# "1.2K" the server also reads. LC_ALL=C pins the scrub dates to the English
# ctime form, and the zone goes with them because zpool prints local time
# without one. Nothing is sent without pools, or when zpool fails: silence
# lets the last report age out, where an empty one would say "no pools".
# Not in a jail: it cannot see the pools, and its host reports them.
# Capped at 64 KB, a quarter of what the server accepts in one payload.
ZFS_JSON=""
if [ "$JAILED" != 1 ] && command -v zpool >/dev/null 2>&1; then
  ZST=$(LC_ALL=C zpool status -p 2>/dev/null || LC_ALL=C zpool status 2>/dev/null || true)
  case "$ZST" in
    *"pool:"*)
      ZST=$(printf '%s\n' "$ZST" | head -c 65536 | tr -d '\r' | awk '
        { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, "\\t")
          printf "%s%s", (NR > 1 ? "\\n" : ""), $0 }')
      ZFS_JSON=",\"zpool_status\":\"$ZST\",\"tz\":\"$(date +%z)\""
      ;;
  esac
fi

# ---- Address, inside a jail ----
# The jail's host reports where the jail can be probed, and with vnet -- every
# iocage jail on a TrueNAS box -- jls on the host knows no address at all. From
# inside, ifconfig lists only the jail's own interfaces.
EXTRA="$ZFS_JSON"
if [ "$JAILED" = 1 ]; then
  JADDR=$(ifconfig -a inet 2>/dev/null | awk '$1=="inet" && $2 !~ /^127\./ { print $2; exit }')
  case "$JADDR" in
    *[!0-9.]*|"") ;;
    *) EXTRA=",\"address\":\"$JADDR\"" ;;
  esac
fi

# ---- Jails, from the host ----
# A host reports its own jails, so a jail needs nothing installed to be listed:
# running ones from jls, stopped ones from jail.conf and iocage, which jls
# cannot see. Each running jail is measured by running THIS script inside it
# with jexec -- the same jail-mode readings an in-jail collector takes, from
# the jail's own ps, df and freebsd-version. Jail-side binaries run as the
# jail's root inside the jail, never as the host's: nothing from a jail's
# filesystem is executed or trusted out here.
#
# The script travels as sh -c's argument, not on stdin, and stdin is
# /dev/null: anything inside that reads a terminal -- pkg's bootstrapper asks
# "fetch and install it now?" -- must read end-of-file, not the script.
#
# Names are kept to what the server accepts. A jail named outside that set is
# skipped rather than escaped: it could not be looked up on the dashboard.
jail_boot() { # name -> true|false|null, from rc.conf's jail_enable/jail_list
  case "$(sysrc -n jail_enable 2>/dev/null || true)" in
    [Yy][Ee][Ss]) ;;
    "") echo null; return ;;
    *) echo false; return ;;
  esac
  JL=$(sysrc -n jail_list 2>/dev/null || true)
  if [ -z "$JL" ]; then echo true; return; fi
  case " $JL " in *" $1 "*) echo true ;; *) echo false ;; esac
}
JAILS=""
if [ "$OSNAME" = FreeBSD ] && [ "$JAILED" != 1 ] && [ "${NETDASH_JAILS:-yes}" != no ] \
   && command -v jls >/dev/null 2>&1 && command -v jexec >/dev/null 2>&1; then
  SELF=$(cat "$0" 2>/dev/null || true)
  JSTATE=${NETDASH_JAIL_STATE:-/var/db/netdash-collector/jails}
  JTMP=$(mktemp "${TMPDIR:-/tmp}/netdash-jails.XXXXXX")
  RUNNING=" "
  jls -h jid name host.hostname 2>/dev/null | sed 1d > "$JTMP" || true
  while read -r JID JNAME JHOST; do
    [ -n "$JID" ] || continue
    case "$JNAME" in ""|*[!A-Za-z0-9_.-]*) continue ;; esac
    # Its hostname, short, is what a collector inside it would call itself,
    # and what TrueNAS lists it as -- so every source agrees on one name.
    JSHORT=${JHOST%%.*}
    case "$JSHORT" in ""|-|*[!A-Za-z0-9_.-]*) JSHORT=$JNAME ;; esac
    RUNNING="$RUNNING$JNAME "
    JP=null
    if [ -r "$JSTATE/$JNAME.json" ]; then
      P=$(tr -d '\n' < "$JSTATE/$JNAME.json" 2>/dev/null || true)
      case "$P" in '{'*'}') JP=$P ;; esac
    fi
    E=""
    [ -n "$SELF" ] && E=$(env -u ASSUME_ALWAYS_YES NETDASH_CONF=/dev/null NETDASH_JAILS=no \
        NETDASH_HOSTNAME="$JSHORT" NETDASH_PATCHES_JSON="$JP" \
        jexec "$JID" /bin/sh -c "$SELF" netdash-collector --print </dev/null 2>/dev/null || true)
    # A jail with no /bin/sh, or one whose run failed, is still running: it is
    # listed with no readings rather than left out.
    case "$E" in
      '{'*'}') ;;
      *) E="{\"host\":\"$JSHORT\",\"virt\":\"jail\"}" ;;
    esac
    E="${E%\}},\"state\":\"running\",\"boot\":$(jail_boot "$JNAME")}"
    JAILS="$JAILS${JAILS:+,}$E"
  done < "$JTMP"
  # Stopped: every jail a manager knows of that jls did not just list.
  { for f in ${NETDASH_JAIL_CONF:-/etc/jail.conf /etc/jail.conf.d/*.conf}; do
      [ -r "$f" ] && awk '/^[ \t]*[A-Za-z0-9_.-]+[ \t]*\{/ {
        sub(/^[ \t]*/, ""); sub(/[ \t]*\{.*/, ""); print "conf", $0, "-" }' "$f"
    done
    if command -v iocage >/dev/null 2>&1; then
      iocage list -h 2>/dev/null | awk -F '\t' '$3=="down" { print "iocage", $2, $4 }'
    fi
  } > "$JTMP" 2>/dev/null || true
  while read -r JSRC JNAME JREL; do
    case "$JNAME" in ""|*[!A-Za-z0-9_.-]*) continue ;; esac
    case "$RUNNING" in *" $JNAME "*) continue ;; esac
    RUNNING="$RUNNING$JNAME "
    JOS=null
    case "$JREL" in -|""|*[!A-Za-z0-9._-]*) ;; *) JOS="\"FreeBSD $JREL\"" ;; esac
    if [ "$JSRC" = conf ]; then JB=$(jail_boot "$JNAME"); else JB=null; fi
    JAILS="$JAILS${JAILS:+,}{\"host\":\"$JNAME\",\"os\":$JOS,\"virt\":\"jail\",\"state\":\"stopped\",\"boot\":$JB}"
  done < "$JTMP"
  rm -f "$JTMP"
  EXTRA="$EXTRA,\"jails\":[$JAILS]"
fi

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%s,"disks":[%s],"patches":%s,"virt":%s,"collector_version":"%s"%s}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS" "$PATCHES" "$VIRT" "$NETDASH_VERSION" "$EXTRA")

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
