#!/bin/sh
# netdash collector -- Linux (Debian, Ubuntu, Raspbian, Fedora, Arch, openSUSE,
# Alpine, Slackware, ...). Native tools only: /proc, df, and curl or wget.
# Usage: netdash-collector.sh [--print]   (--print dumps JSON, posts nothing)
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.5.1"

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

# ---- Virtualisation ----
# systemd-detect-virt is the canonical answer where it exists: it prints "none"
# and exits 1 on bare metal, so the exit status is not the signal -- the word is.
# Absent (Alpine, which has no systemd), the CPUID hypervisor flag says whether
# this is a guest at all, and DMI says whose. A host that can answer neither
# reports null rather than guessing at "none": claiming bare metal is a claim.
#
# Containers come first, as they do in systemd-detect-virt: a container inside a
# VM is still a container, and its kernel is the VM's. Without systemd, podman
# and docker each leave a marker file, and LXC and systemd-nspawn put
# container=<name> in pid 1's environment. A container that shows none of these
# falls through to the hypervisor checks and reads as its host -- the reason a
# per-host "virt" override exists.
VIRT=null
detect_virt() {
  if command -v systemd-detect-virt >/dev/null 2>&1; then
    systemd-detect-virt 2>/dev/null || true
    return
  fi
  if [ -e /run/.containerenv ]; then echo podman; return; fi
  if [ -e /.dockerenv ]; then echo docker; return; fi
  C=$(tr '\000' '\n' < /proc/1/environ 2>/dev/null | sed -n 's/^container=//p' | head -1)
  if [ -n "$C" ]; then echo "$C"; return; fi
  if ! grep -qm1 '^flags.*[[:space:]]hypervisor\([[:space:]]\|$\)' /proc/cpuinfo 2>/dev/null; then
    # No hypervisor bit. Only call it bare metal if DMI is actually readable;
    # an unreadable DMI on an unknown platform is not evidence of anything.
    [ -r /sys/class/dmi/id/sys_vendor ] && echo none
    return
  fi
  V=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)
  P=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
  case "$V $P" in
    *BHYVE*|*bhyve*)              echo bhyve ;;
    *QEMU*|*qemu*)                echo kvm ;;
    *VMware*|*vmware*)            echo vmware ;;
    *innotek*|*VirtualBox*)       echo oracle ;;
    *Microsoft*|*Hyper-V*)        echo microsoft ;;
    *Xen*|*xen*)                  echo xen ;;
    *"Amazon EC2"*)               echo amazon ;;
    *Parallels*)                  echo parallels ;;
    *)                            echo vm ;;   # a guest, vendor unrecognised
  esac
}
V=$(detect_virt)
[ -n "$V" ] && VIRT="\"$V\""

# Container names as systemd-detect-virt prints them; the server keeps the same
# list. Anything else -- "none", a hypervisor, unknown -- is not a container.
case "$V" in
  lxc|lxc-libvirt|systemd-nspawn|docker|podman|rkt|openvz|wsl|proot|pouch|container-other) CTR=1 ;;
  *) CTR=0 ;;
esac

# ---- Container cgroup ----
# A container shares its host's kernel, so /proc/stat, /proc/meminfo and
# /proc/uptime describe the HOST -- unless lxcfs is mounted over them, which
# LXC usually does and docker never does. The container's own cgroup is the one
# reading that is right either way: cgroup v2 at /sys/fs/cgroup, v1 split into
# per-controller directories. Neither readable leaves the /proc figures, which
# are the host's -- the best left to report, not a correct one.
CG=""
if [ "$CTR" = 1 ]; then
  if [ -r /sys/fs/cgroup/cpu.stat ] && [ -r /sys/fs/cgroup/memory.current ]; then CG=v2
  elif [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ] && [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then CG=v1
  fi
fi

# ---- CPU: two samples of /proc/stat, one second apart ----
read_cpu() { awk '/^cpu /{idle=$5+$6; total=0; for(i=2;i<=NF;i++) total+=$i; print total, idle; exit}' /proc/stat; }
if [ -n "$CG" ]; then
  # CPU time the cgroup used, in microseconds, against wall time from
  # /proc/uptime (host or lxcfs, both advance in real seconds), divided by the
  # CPUs it may use: its cpu.max quota when it has one, else the cores visible
  # to it -- nproc honours a cpuset. One busy core of four is 25%, as on a host.
  cg_usage() {
    if [ "$CG" = v2 ]; then awk '$1=="usage_usec"{print $2; exit}' /sys/fs/cgroup/cpu.stat
    else awk '{printf "%.0f", $1/1000}' /sys/fs/cgroup/cpuacct/cpuacct.usage; fi
  }
  U1=$(cg_usage); W1=$(cut -d' ' -f1 /proc/uptime)
  sleep 1
  U2=$(cg_usage); W2=$(cut -d' ' -f1 /proc/uptime)
  NCPU=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo || true)
  QUOTA=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null || true)
  CPU=$(awk -v u1="$U1" -v u2="$U2" -v w1="$W1" -v w2="$W2" -v n="${NCPU:-0}" -v q="$QUOTA" 'BEGIN{
    split(q, Q, " "); if (Q[1] ~ /^[0-9]+$/ && Q[2]+0 > 0 && Q[1]/Q[2] < n) n = Q[1]/Q[2]
    dw=(w2-w1)*1e6
    if (u1=="" || u2=="" || dw<=0 || n<=0) { print "null"; exit }
    p=100*(u2-u1)/dw/n; if(p<0)p=0; if(p>100)p=100; printf "%.1f", p
  }')
else
set -- $(read_cpu); T1=$1; I1=$2
sleep 1
set -- $(read_cpu); T2=$1; I2=$2
CPU=$(awk -v t1="$T1" -v i1="$I1" -v t2="$T2" -v i2="$I2" 'BEGIN{
  dt=t2-t1; di=i2-i1;
  if (dt<=0) { print "null" } else { p=100*(dt-di)/dt; if(p<0)p=0; if(p>100)p=100; printf "%.1f", p }
}')
fi

# ---- Memory: used = MemTotal - MemAvailable (excludes reclaimable cache) ----
eval "$(awk '
  /^MemTotal:/     {t=$2}
  /^MemAvailable:/ {a=$2}
  # %.0f, not %d: BusyBox awk (Alpine) uses 32-bit signed ints for %d, so a
  # machine with more than 2 GiB reported mem_total_bytes as -2147483648.
  END { printf "MEM_TOTAL=%.0f\nMEM_USED=%.0f\n", t*1024, (t-a)*1024 }
' /proc/meminfo)"
if [ -n "$CG" ]; then
  # The cgroup's charge less its inactive file cache -- the same "not
  # reclaimable" meaning as MemAvailable above. The total is the cgroup's limit
  # when it has one below the machine's RAM, else that RAM: an unlimited
  # container can use all of it.
  if [ "$CG" = v2 ]; then
    CUR=$(cat /sys/fs/cgroup/memory.current); LIM=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || true)
    INF=$(awk '$1=="inactive_file"{print $2; exit}' /sys/fs/cgroup/memory.stat 2>/dev/null || true)
  else
    CUR=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes)
    LIM=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || true)
    INF=$(awk '$1=="total_inactive_file"{print $2; exit}' /sys/fs/cgroup/memory/memory.stat 2>/dev/null || true)
  fi
  eval "$(awk -v c="$CUR" -v l="$LIM" -v i="${INF:-0}" -v t="$MEM_TOTAL" 'BEGIN{
    if (l ~ /^[0-9]+$/ && l+0 > 0 && l+0 < t+0) t = l
    u = c - i; if (u < 0) u = 0
    printf "MEM_TOTAL=%.0f\nMEM_USED=%.0f\n", t, u }')"
fi

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
#
# Except a container's root: docker and podman build it from overlay, which is
# otherwise skipped as a layer of something else. There it is the container's
# own disk, and its df figures are those of the host filesystem under it.
DISKS=$( { cat /proc/self/mounts 2>/dev/null || cat /proc/mounts 2>/dev/null
           printf '@@DF@@\n'
           df -P -k 2>/dev/null; } | awk -v ctr="$CTR" '
  function unesc(s) {
    gsub(/\\040/," ",s); gsub(/\\011/,"\t",s); gsub(/\\134/,"\\",s); return s
  }
  /^@@DF@@$/ { indf=1; next }

  # --- /proc/mounts: device, mountpoint, fstype ---
  !indf {
    fs=$3
    if (ctr && fs == "overlay" && $2 == "/") { fstype["/"]=fs; dev["/"]=unesc($1); next }
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
# In a container that is the host's uptime unless lxcfs serves /proc/uptime.
# Without lxcfs, the container's age is the host's uptime less the moment its
# pid 1 started, which /proc/1/stat gives in clock ticks since host boot -- the
# 20th field after the parenthesised command name, which may itself hold spaces.
if [ "$CTR" = 1 ] && ! awk '$2=="/proc/uptime"{f=1} END{exit !f}' /proc/mounts 2>/dev/null; then
  HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
  CUP=$(sed 's/.*) //' /proc/1/stat 2>/dev/null | awk -v up="$UPTIME" -v hz="$HZ" '
    $20+0 > 0 && hz+0 > 0 { a = up - $20/hz; if (a >= 0) printf "%.0f", a }')
  [ -n "$CUP" ] && UPTIME=$CUP
fi
OS=$( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-${NAME:-Linux}}" )
ARCH=$(uname -m)
# Slackware's PRETTY_NAME already ends in the architecture ("Slackware 15.0
# x86_64"), which the " (arch)" this script appends to it then repeated.
case "$OS" in *" $ARCH") OS=${OS%" $ARCH"} ;; esac

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

JSON=$(printf '{"host":"%s","os":"%s (%s)","cpu_pct":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"uptime_seconds":%d,"disks":[%s],"patches":%s,"virt":%s,"collector_version":"%s"}' \
  "$HOST" "$OS" "$ARCH" "$CPU" "$MEM_USED" "$MEM_TOTAL" "$UPTIME" "$DISKS" "$PATCHES" "$VIRT" "$NETDASH_VERSION")

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
