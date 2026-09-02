#!/bin/sh
# netdash collector -- Linux (Debian, Ubuntu, Raspbian, Fedora, Arch, openSUSE,
# Alpine, ...). Native tools only: /proc, df, and curl or wget.
# Usage: netdash-collector.sh [--print]   (--print dumps JSON, posts nothing)
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.3.2"

MODE="${1:-}"   # capture before `set --` below reuses the positional params

# cron on the BSDs and BusyBox crond run with a minimal PATH that often omits
# /usr/local/bin, where curl lives. Appended, not prepended: this adds the
# standard directories when they are missing without overriding whatever the
# caller already chose.
PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/pkg/sbin:/usr/pkg/bin:/opt/homebrew/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

CONF="${NETDASH_CONF:-/etc/netdash/collector.conf}"
[ -f "$CONF" ] && . "$CONF"

NETDASH_URL="${NETDASH_URL:-}"
NETDASH_TOKEN="${NETDASH_TOKEN:-}"
HOST="${NETDASH_HOSTNAME:-$(hostname -s 2>/dev/null || uname -n | cut -d. -f1)}"

# ---- CPU: two samples of /proc/stat, one second apart ----
read_cpu() { awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total, idle; exit}' /proc/stat; }
set -- $(read_cpu); T1=$1; I1=$2
sleep 1
set -- $(read_cpu); T2=$1; I2=$2
CPU=$(awk -v t1="$T1" -v i1="$I1" -v t2="$T2" -v i2="$I2" 'BEGIN{
  dt=t2-t1; di=i2-i1;
  if (dt<=0) { print "null" } else { p=100*(dt-di)/dt; if(p<0)p=0; if(p>100)p=100; printf "%.1f", p }
}')

# ---- Memory: used = MemTotal - MemAvailable (excludes reclaimable cache) ----
eval "$(awk '
  /^MemTotal:/     {t=$2}
  /^MemAvailable:/ {a=$2}
  # %.0f, not %d: BusyBox awk (Alpine) uses 32-bit signed ints for %d, so a
  # machine with more than 2 GiB reported mem_total_bytes as -2147483648.
  END { printf "MEM_TOTAL=%.0f\nMEM_USED=%.0f\n", t*1024, (t-a)*1024 }
' /proc/meminfo)"

# ---- Disk ----
# Built on /proc/mounts rather than df's flags: BusyBox df (Alpine) rejects both
# -l and -x, so the old invocation returned nothing at all there. /proc/mounts is
# universal on Linux and carries the filesystem type and source device, which is
# what the filtering actually needs.
#
# Entries are grouped by source device, because several mount points can be one
# filesystem: btrfs subvolumes (the Fedora and openSUSE default layouts) and bind
# mounts all repeat the same device with identical usage. ZFS is grouped by pool
# and reported as total-available, matching FreeBSD, TrueNAS and macOS -- its
# datasets share the pool's free space, so no single dataset's used/total is the
# disk's fullness.
#
# Network filesystems are skipped: that storage belongs to the host serving it.
DISKS=$( { cat /proc/self/mounts 2>/dev/null || cat /proc/mounts 2>/dev/null
           printf '@@DF@@\n'
           df -P -k 2>/dev/null; } | awk '
  function unesc(s) {
    gsub(/\\040/," ",s); gsub(/\\011/,"\t",s); gsub(/\\134/,"\\",s); return s
  }
  /^@@DF@@$/ { indf=1; next }

  # --- /proc/mounts: device, mountpoint, fstype ---
  !indf {
    fs=$3
    if (fs ~ /^(proc|sysfs|devtmpfs|tmpfs|devpts|cgroup|cgroup2|pstore|bpf|securityfs|debugfs|tracefs|configfs|fusectl|mqueue|hugetlbfs|autofs|binfmt_misc|squashfs|overlay|ramfs|efivarfs|rpc_pipefs|nsfs|selinuxfs|iso9660|udf|fuse\.gvfsd-fuse|fuse\.portal|fuse\.snapfuse)$/) next
    if (fs ~ /^(nfs|nfs3|nfs4|cifs|smb3|smbfs|ceph|glusterfs|afs|9p|davfs|fuse\.sshfs|fuse\.rclone|fuse\.s3fs)$/) next
    mp=unesc($2)
    fstype[mp]=fs
    dev[mp]=unesc($1)
    next
  }

  # --- df -P -k ---
  $1 == "Filesystem" { next }
  $2+0 > 0 {
    mp=$6; for(i=7;i<=NF;i++) mp=mp" "$i
    if (!(mp in fstype)) next
    fs=fstype[mp]; d=dev[mp]
    if (fs == "zfs") { i=index(d "/", "/"); d="zfs:" substr(d,1,i-1); shared[d]=1 }
    if (!(d in seen)) { seen[d]=1; tot[d]=$2+0; usd[d]=$3+0; avl[d]=$4+0; lbl[d]=mp }
    else {
      if ($2+0 > tot[d]) tot[d]=$2+0
      if ($3+0 > usd[d]) usd[d]=$3+0
      if ($4+0 < avl[d]) avl[d]=$4+0
    }
    if (mp == "/") lbl[d]="/"
    else if (lbl[d] != "/" && length(mp) < length(lbl[d])) lbl[d]=mp
  }

  END {
    for (d in seen) {
      u = (d in shared) ? tot[d]-avl[d] : usd[d]
      if (u < 0) u = 0
      mp = lbl[d]
      gsub(/\\/,"\\\\",mp); gsub(/"/,"\\\"",mp)
      printf "%s{\"mount\":\"%s\",\"used_bytes\":%.0f,\"total_bytes\":%.0f}", (n++?",":""), mp, u*1024, tot[d]*1024
    }
  }')

UPTIME=$(awk '{printf "%.0f", $1}' /proc/uptime)
OS=$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-${NAME:-Linux}}" )
ARCH=$(uname -m)

# Raspberry Pi OS reports PRETTY_NAME="Debian GNU/Linux 13 (trixie)", so a Pi is
# indistinguishable from any other Debian box by os-release alone. The board
# model lives in the device tree (absent on x86, present on most ARM SBCs).
MODEL=""
for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
  if [ -r "$f" ]; then
    MODEL=$(tr -d '\000' < "$f" | sed -e 's/ Rev [0-9.]*$//' -e 's/[[:space:]]*$//')
    break
  fi
done
[ -n "$MODEL" ] && OS="$OS - $MODEL"

# ---- Patch status ----
# Read back from whatever netdash-patchcheck last wrote on its own daily
# schedule. This stays a file read on purpose: every mechanism that can answer
# "are there security updates" costs seconds and usually a network round trip,
# which is untenable at a 30s cadence. An absent, unreadable or truncated file
# reports null, and the server renders that as "unknown" -- never as up to date.
PATCHES=null
# An explicit NETDASH_PATCH_STATE is authoritative: if the admin names a file
# and it is missing or malformed, that reports unknown rather than quietly
# falling through to some other host-state file left in a default location.
if [ -n "${NETDASH_PATCH_STATE:-}" ]; then
  PATCH_FILES="$NETDASH_PATCH_STATE"
else
  PATCH_FILES="/var/lib/netdash-collector/patches.json /var/db/netdash-collector/patches.json /var/lib/netdash/patches.json"
fi
for f in $PATCH_FILES; do
  [ -r "$f" ] || continue
  P=$(tr -d '\n' < "$f" 2>/dev/null || true)
  case "$P" in '{'*'}') PATCHES="$P"; break ;; esac
done

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%d,"disks":[%s],"patches":%s,"collector_version":"%s"}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS" "$PATCHES" "$NETDASH_VERSION")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

[ -n "$NETDASH_URL" ] || { echo "netdash: NETDASH_URL not set (see $CONF)" >&2; exit 1; }

# ---- POST: curl if present, else wget (Alpine ships BusyBox wget, no curl) ----
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
  # BusyBox wget exits 1 for everything, so the message is the only signal.
  # GNU wget uses 4 for a network failure and 8 for an HTTP error response.
  case "$rc$ERR" in
    4*|*"server returned error"*|*"ERROR 4"*|*"ERROR 5"*|*40[0-9]*|*50[0-9]*) ;;
    *) exit 0 ;;
  esac
else
  echo "netdash: neither curl nor wget found; install one of them" >&2
  exit 1
fi

# Reaching here means a real misconfiguration, not a transient network problem.
# Unreachable-server cases exited 0 above: this runs every 30-60s and its log is
# never rotated, so a laptop off the LAN must not add ~1440 lines a day.
if [ -z "$NETDASH_TOKEN" ]; then
  echo "netdash: server rejected the post and NETDASH_TOKEN is empty in $CONF" >&2
else
  echo "netdash: post failed (rc $rc): $ERR" >&2
fi
exit "$rc"
