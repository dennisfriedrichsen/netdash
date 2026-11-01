#!/bin/sh
# netdash collector -- illumos: OmniOS, and other distributions on the same
# kernel. Native tools only: kstat, zfs, df, smbios, and curl or wget.
# Usage: netdash-collector.sh [--print]
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.5.1"

MODE="${1:-}"

# cron's PATH on illumos is /usr/bin alone. kstat, zfs, zpool and smbios live
# in /usr/sbin; OmniOS's extra repository installs under /opt/ooce. Appended,
# not prepended, as on the other platforms.
PATH="$PATH:/usr/sbin:/usr/bin:/sbin:/opt/ooce/bin:/usr/local/bin"
export PATH

CONF="${NETDASH_CONF:-}"
if [ -z "$CONF" ]; then
  for c in /etc/netdash/collector.conf /usr/local/etc/netdash/collector.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
  done
  CONF="${CONF:-/etc/netdash/collector.conf}"
fi
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
# Not `hostname -s`: illumos's hostname takes its argument as the name to SET,
# so that would rename the machine "-s". uname -n only reads.
HOST="${NETDASH_HOSTNAME:-$(uname -n | sed 's/\..*//')}"

# nawk where there is one. The awk scripts below need nothing beyond POSIX,
# but on older illumos /usr/bin/awk is the pre-POSIX one.
if command -v nawk >/dev/null 2>&1; then AWK=nawk; else AWK=awk; fi

# ---- Zones ----
# kstat in a non-global zone answers for the whole machine: every CPU, all of
# physmem, the global zone's boot time. Reported as-is, a zone would wear its
# host's numbers under its own name -- the jail bug on FreeBSD, which the BSD
# collector has to work around. Until zones are measured as zones, the
# collector runs in the global zone only and says so.
ZONE=$(zonename 2>/dev/null || echo global)
if [ "$ZONE" != global ]; then
  echo "netdash: in zone '$ZONE'. Run the collector in the global zone; zones are not measured yet." >&2
  exit 1
fi

# One value from kstat -p, which prints "module:instance:name:statistic<TAB>value".
# Must never fail: under set -e a failing VAR=$(...) ends the script, and
# zfs:0:arcstats does not exist on a host without ZFS loaded.
kstat_v() { kstat -p "$1" 2>/dev/null | $AWK 'NR == 1 { print $NF }' || true; }

# ---- CPU: two samples of the per-CPU nanosecond counters ----
# cpu::sys keeps time spent idle, in user and in the kernel, per CPU, in
# nanoseconds -- the counters mpstat reads. Busy is user + kernel; intr and
# dtrace are left out, as mpstat leaves them out of its usr/sys/idl split.
cpu_ns() {
  kstat -p 'cpu::sys:cpu_nsec_*' 2>/dev/null | $AWK '
    $1 ~ /:cpu_nsec_idle$/   { i += $2 }
    $1 ~ /:cpu_nsec_user$/   { b += $2 }
    $1 ~ /:cpu_nsec_kernel$/ { b += $2 }
    END { if (b + i > 0) printf "%.0f %.0f", b, i }' || true
}
S1=$(cpu_ns); sleep 1; S2=$(cpu_ns)
CPU=$($AWK -v a="$S1" -v b="$S2" 'BEGIN {
  if (split(a, A, " ") != 2 || split(b, B, " ") != 2) { print "null"; exit }
  db = B[1] - A[1]; di = B[2] - A[2]
  if (db + di <= 0) { print "null"; exit }
  p = 100 * db / (db + di); if (p < 0) p = 0; if (p > 100) p = 100
  printf "%.1f", p }')

# ---- Memory ----
# physmem is the RAM the kernel manages, in pages; freemem is what is free
# now. The ZFS ARC is held as kernel memory and never shows up as free, so it
# is subtracted, as the FreeBSD and TrueNAS readings subtract it: a ZFS box
# would otherwise drift to 100% and stay there. Verified on OmniOS r151054:
# physmem 259625 pages of 4096 bytes = the 1014 MiB the dashboard shows.
MEM_TOTAL=null
MEM_USED=null
PS=$(pagesize 2>/dev/null || true)
PHYS=$(kstat_v unix:0:system_pages:physmem)
FREE=$(kstat_v unix:0:system_pages:freemem)
ARC=$(kstat_v zfs:0:arcstats:size); ARC=${ARC:-0}
if [ -n "$PS" ] && [ -n "$PHYS" ] && [ -n "$FREE" ]; then
  MEM_TOTAL=$($AWK -v p="$PHYS" -v s="$PS" 'BEGIN { printf "%.0f", p * s }')
  MEM_USED=$($AWK -v t="$MEM_TOTAL" -v f="$FREE" -v s="$PS" -v a="$ARC" 'BEGIN {
    u = t - f * s - a; if (u < 0) u = 0; printf "%.0f", u }')
fi

# ---- Disk ----
# One entry per ZFS pool, from the pool's root dataset: used + available, the
# usable view, as on FreeBSD -- the datasets share the pool's free space, and
# listing each would repeat the same pool (OmniOS's own install has nine).
# Then any UFS or PCFS mount, by df -n's type. Everything else df lists on a
# stock install is swap-backed tmpfs, lofs over files already counted, or a
# kernel pseudo-filesystem (devfs, ctfs, objfs, mntfs, sharefs, bootfs, fd).
ZPOOLS=""
if command -v zfs >/dev/null 2>&1; then
  ZPOOLS=$(zfs list -Hp -d 0 -o name,used,avail 2>/dev/null | $AWK '
    { total = $2 + $3
      if (total > 0) printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++ ? "," : ""), $1, $2, total }')
fi
# df -n prints "<mount> : <type>", padded. A long filesystem name can push
# df -k's numbers onto a second line, so its rows are joined before reading.
OTHER=""
TMPM=$(mktemp "${TMPDIR:-/tmp}/netdash-mounts.XXXXXX")
df -n 2>/dev/null | $AWK '{ t = $NF; sub(/[ \t]*:[ \t]*[^ \t:]+[ \t]*$/, "")
                           if (t == "ufs" || t == "pcfs") print }' > "$TMPM" || true
while IFS= read -r MP; do
  [ -n "$MP" ] || continue
  R=$(df -k "$MP" 2>/dev/null | $AWK -v mp="$MP" '
    NR > 1 { line = line " " $0 }
    END { n = split(line, F, " "); if (n < 6 || F[2] + 0 <= 0) exit
          gsub(/\\/, "\\\\", mp); gsub(/"/, "\\\"", mp)
          printf "{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", mp, F[3] * 1024, F[2] * 1024 }' || true)
  [ -n "$R" ] && OTHER="$OTHER${OTHER:+,}$R"
done < "$TMPM"
rm -f "$TMPM"
if [ -n "$ZPOOLS" ] && [ -n "$OTHER" ]; then DISKS="$ZPOOLS,$OTHER"
else DISKS="$ZPOOLS$OTHER"; fi

# ---- Uptime ----
BOOT=$(kstat_v unix:0:system_misc:boot_time)
case "$BOOT" in
  ''|*[!0-9]*) UPTIME=null ;;
  *) UPTIME=$(( $(date +%s) - BOOT )) ;;
esac

# ---- OS ----
# uname -sr says "SunOS 5.11" on every illumos distribution, which names none
# of them. /etc/release's first line is the distribution and its release --
# "OmniOS v11 r151054by" -- and r151054 is what its support window is
# measured by. Kept to characters that cannot break the JSON below.
REL="${NETDASH_RELEASE_FILE:-/etc/release}"
OS=$($AWK 'NR == 1 { sub(/^[ \t]+/, ""); sub(/[ \t]+$/, ""); print; exit }' "$REL" 2>/dev/null \
     | tr -cd 'A-Za-z0-9 ._()+:/-' || true)
[ -n "$OS" ] || OS=$(uname -sr)
# isainfo -k is the kernel's instruction set, amd64. uname -m says i86pc,
# the platform, which is not what the other collectors report.
ARCH=$(isainfo -k 2>/dev/null || uname -p)

# ---- Virtualisation ----
# The SMBIOS system record names the hypervisor in Manufacturer or Product:
# bhyve says "FreeBSD" / "BHYVE". A readable record with no hypervisor in it is
# real hardware; nothing readable (smbios needs root) is null, not a guess.
virt_name() {
  case "$1" in
    *BHYVE*|*bhyve*)        echo bhyve ;;
    *QEMU*|*qemu*|*KVM*)    echo kvm ;;
    *VMware*|*vmware*)      echo vmware ;;
    *innotek*|*VirtualBox*) echo oracle ;;
    *Microsoft*|*Hyper-V*)  echo microsoft ;;
    *Xen*|*xen*)            echo xen ;;
    *Parallels*)            echo parallels ;;
    *)                      echo "" ;;
  esac
}
VIRT=null
HWV=$(smbios -t SMB_TYPE_SYSTEM 2>/dev/null | $AWK -F': ' '
  $1 ~ /^[ \t]*(Manufacturer|Product)$/ { printf "%s ", $2 }' || true)
V=$(virt_name "$HWV")
if [ -n "$V" ]; then
  VIRT="\"$V\""
elif [ -n "$(printf '%s' "$HWV" | tr -d ' ')" ]; then
  VIRT='"none"'
fi

# ---- Patch status ----
# Read back from what netdash-patchcheck last wrote on its own daily schedule:
# `pkg update -n` plans against the publishers' catalogues and takes seconds.
# Absent or malformed reads null, which the server shows as unknown.
PATCHES=null
if [ -n "${NETDASH_PATCH_STATE:-}" ]; then
  PATCH_FILES="$NETDASH_PATCH_STATE"
else
  PATCH_FILES="/var/lib/netdash-collector/patches.json"
fi
for f in $PATCH_FILES; do
  [ -r "$f" ] || continue
  P=$(tr -d '\n' < "$f" 2>/dev/null || true)
  case "$P" in '{'*'}') PATCHES="$P"; break ;; esac
done
# Whether a reboot is pending is read fresh every run, not from that file.
# illumos cron has no @reboot, so the patch check cannot run at boot as it
# does elsewhere, and the flag it wrote before an update's reboot would stay
# up until 03:00 -- the reading a reboot exists to clear. beadm is local and
# quick: the BE marked N is running, the one marked R boots next (see the
# patch check). A listing it cannot read leaves the file's answer alone.
# The space after the colon is optional: a file from an earlier collector on
# the same host wrote `"reboot_required": null`, and matching the colon alone
# left `false null` behind -- JSON the server rejects, sample and all.
if [ "$PATCHES" != null ] && command -v beadm >/dev/null 2>&1; then
  RB=$(beadm list -H 2>/dev/null | $AWK -F';' '
    $3 ~ /R/ && $3 ~ /N/ { nr = 1 }
    $3 ~ /R/ && $3 !~ /N/ { r = 1 }
    END { if (r) print "true"; else if (nr) print "false" }' || true)
  [ -n "$RB" ] && PATCHES=$(printf '%s' "$PATCHES" | \
    sed "s/\"reboot_required\":[ ]*[a-z]*/\"reboot_required\":$RB/")
fi

# ---- ZFS pool health ----
# `zpool status -p` as text, parsed by the server (server/zfs.py), as from the
# BSD, Linux and macOS collectors. illumos prints the same layout -- devices
# are named c3t0d0, not ada0 -- and the parser needs nothing new for it.
# Capped at 64 KB by awk: illumos head has no -c.
ZFS_JSON=""
if command -v zpool >/dev/null 2>&1; then
  ZST=$(LC_ALL=C zpool status -p 2>/dev/null || LC_ALL=C zpool status 2>/dev/null || true)
  case "$ZST" in
    *"pool:"*)
      ZST=$(printf '%s\n' "$ZST" | tr -d '\r' | $AWK '
        { n += length($0) + 1; if (n > 65536) exit
          gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); gsub(/\t/, "\\t")
          printf "%s%s", (NR > 1 ? "\\n" : ""), $0 }')
      ZFS_JSON=",\"zpool_status\":\"$ZST\",\"tz\":\"$(date +%z)\""
      ;;
  esac
fi

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%s,"disks":[%s],"patches":%s,"virt":%s,"collector_version":"%s"%s}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS" "$PATCHES" "$VIRT" "$NETDASH_VERSION" "$ZFS_JSON")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

[ -n "$NETDASH_URL" ] || { echo "netdash: NETDASH_URL not set (see $CONF)" >&2; exit 1; }

# ---- POST: curl (OmniOS ships it in /usr/bin), else wget ----
rc=0
if command -v curl >/dev/null 2>&1; then
  ERR=$(curl -fsS --connect-timeout 5 --max-time 15 -X POST "$NETDASH_URL" \
    -H "Content-Type: application/json" \
    -H "X-Netdash-Token: $NETDASH_TOKEN" \
    --data "$JSON" -o /dev/null 2>&1) || rc=$?
  [ "$rc" -eq 0 ] && exit 0
  case "$rc" in
    6|7|28|35) exit 0 ;;   # resolve/connect/timeout/TLS -- see below
  esac
elif command -v wget >/dev/null 2>&1; then
  ERR=$(wget -q -O /dev/null --timeout=15 \
    --header="Content-Type: application/json" \
    --header="X-Netdash-Token: $NETDASH_TOKEN" \
    --post-data="$JSON" "$NETDASH_URL" 2>&1) || rc=$?
  [ "$rc" -eq 0 ] && exit 0
  # GNU wget uses 4 for a network failure and 8 for an HTTP error response.
  [ "$rc" -eq 4 ] && exit 0
else
  echo "netdash: neither curl nor wget found; install one of them" >&2
  exit 1
fi

# Reaching here means a real misconfiguration, not a transient network problem.
# An unreachable server exited 0 above: this runs every 60s, and a server
# restart must not fill the cron log.
if [ -z "$NETDASH_TOKEN" ]; then
  echo "netdash: server rejected the post and NETDASH_TOKEN is empty in $CONF" >&2
else
  echo "netdash: post failed (rc $rc): $ERR" >&2
fi
exit "$rc"
