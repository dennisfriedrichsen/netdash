#!/bin/sh
# netdash regression tests.
#
#   sh tests/run.sh          # run everything
#   sh tests/run.sh openbsd  # run cases whose name contains "openbsd"
#
# Every fixture in tests/fixtures/ is real output captured from a real host --
# see tests/fixtures/README.md for provenance. Each case locks in a bug that
# actually shipped, so a regression here is a bug that already bit once.
#
# Needs: sh, awk, python3. No network, no root, nothing installed.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FIX="$ROOT/tests/fixtures"
SHIMS="$ROOT/tests/mocks/bin"
FILTER="${1:-}"
PASS=0; FAIL=0

# Build a PATH directory holding only the named shims, so a case can emulate a
# host where a command is absent (a FreeBSD box with no zfs, say).
mkbin() {
  d=$(mktemp -d); shift_list="$*"
  for c in $shift_list; do ln -s "$SHIMS/$c" "$d/$c"; done
  echo "$d"
}

check() { # name, python-expression-file, json
  name=$1; expr=$2; json=$3
  if printf '%s' "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
$expr
" 2>/tmp/netdash-test-err; then
    PASS=$((PASS+1)); printf '  ok   %s\n' "$name"
  else
    FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$name"
    sed 's/^/         /' /tmp/netdash-test-err | tail -3
  fi
}

run_collector() { # collector, bindir  -> prints JSON
  NETDASH_MOCK_TMP=$(mktemp -d)
  export NETDASH_MOCK_TMP NETDASH_FIXTURES="$FIX"
  env "PATH=$1:/usr/bin:/bin" NETDASH_CONF=/dev/null \
      sh "$ROOT/collectors/$2/netdash-collector.sh" --print 2>/dev/null
  rm -rf "$NETDASH_MOCK_TMP"
}

want() { case "$1" in *"$FILTER"*) return 0 ;; esac; [ -z "$FILTER" ]; }

# ---------------------------------------------------------------- FreeBSD ----
if want freebsd-zfs; then
  echo "freebsd-zfs (ZFS pools plus an EFI partition)"
  B=$(mkbin x uname sysctl df zfs hostname)
  J=$(MOCK_PHYSMEM=32090000000 MOCK_PAGESIZE=4096 MOCK_FREE=3000000 \
      MOCK_INACT=1500000 MOCK_LAUNDRY=50000 MOCK_ARC=8000000000 \
      MOCK_BOOTTIME="{ sec = 1787700000, usec = 123456 } Tue Aug 25 12:00:00 2026" \
      NETDASH_MOCK_OS=FreeBSD NETDASH_MOCK_REL=15.1-RELEASE-p3 \
      run_collector "$B" bsd)
  check "pools reported one per pool, not per dataset" \
        "assert [x['mount'] for x in d['disks']]==['zroot','ssdpool','/boot/efi'], d['disks']" "$J"
  check "devfs and the nfs mount excluded" \
        "assert not any('nas' in x['mount'] or x['mount']=='/dev' for x in d['disks'])" "$J"
  check "ARC excluded from memory (else a ZFS box drifts to 100%)" \
        "assert 16 < 100*d['mem_used_bytes']/d['mem_total_bytes'] < 18, d" "$J"
  check "boottime anchored on the brace, not usec (~20000d bug)" \
        "assert 0 < d['uptime_seconds'] < 86400*400, d['uptime_seconds']" "$J"
  rm -rf "$B"
fi

if want freebsd-nozfs; then
  echo "freebsd-nozfs (absent sysctl must not abort the script under set -e)"
  B=$(mkbin x uname sysctl df hostname)      # no zfs shim at all
  J=$(MOCK_PHYSMEM=32090000000 MOCK_PAGESIZE=4096 MOCK_FREE=3000000 \
      MOCK_INACT=1500000 MOCK_LAUNDRY=50000 \
      MOCK_BOOTTIME="{ sec = 1787700000, usec = 0 }" \
      NETDASH_MOCK_OS=FreeBSD NETDASH_MOCK_REL=15.1-RELEASE-p3 \
      run_collector "$B" bsd)
  check "collector still produces output with no zfs(8)" \
        "assert d['disks']==[{'mount':'/boot/efi','used_bytes':1331200,'total_bytes':268435456}], d['disks']" "$J"
  check "memory still reported (no ARC sysctl to read)" \
        "assert d['mem_used_bytes'] > 0" "$J"
  rm -rf "$B"
fi

# ---------------------------------------------------------------- OpenBSD ----
if want openbsd; then
  echo "openbsd (6-field comma cp_time, vmstat -s memory, bare-epoch boottime)"
  B=$(mkbin x uname sysctl df vmstat hostname)
  J=$(MOCK_PHYSMEM=497573888 MOCK_BOOTTIME=1787412753 \
      NETDASH_MOCK_OS=OpenBSD NETDASH_MOCK_REL=7.9 \
      run_collector "$B" bsd)
  check "cp_time parsed despite 6 comma-separated fields" \
        "assert d['cpu_pct'] is not None and 0 <= d['cpu_pct'] <= 100, d['cpu_pct']" "$J"
  check "memory from vmstat -s (sysctl vm.uvmexp is refused)" \
        "assert abs(100*d['mem_used_bytes']/d['mem_total_bytes'] - 37.1) < 0.2, d" "$J"
  check "bare-epoch boottime accepted" \
        "assert 0 < d['uptime_seconds'] < 86400*400, d['uptime_seconds']" "$J"
  check "only /dev/ devices kept (mfs and nfs dropped)" \
        "assert sorted(x['mount'] for x in d['disks'])==['/','/home','/usr'], d['disks']" "$J"
  rm -rf "$B"
fi

# ----------------------------------------------------------------- NetBSD ----
if want netbsd; then
  echo "netbsd (key=value cp_time, ZFS pools, df -l does NOT drop pseudo-fs)"
  B=$(mkbin x uname sysctl df vmstat zfs hostname)
  J=$(MOCK_PHYSMEM64=534945792 MOCK_PHYSMEM=534945792 MOCK_BOOTTIME=1787497937 \
      NETDASH_MOCK_OS=NetBSD NETDASH_MOCK_REL=11.0 \
      run_collector "$B" bsd)
  check "cp_time parsed from 'user = N, nice = N, ...' pairs" \
        "assert d['cpu_pct'] is not None and 0 <= d['cpu_pct'] <= 100, d['cpu_pct']" "$J"
  check "tmpfs/kernfs/ptyfs/procfs dropped (df -l does not exclude them here)" \
        "assert not any(x['mount'] in ('/tmp','/kern','/dev/pts','/proc','/var/shm') for x in d['disks']), d['disks']" "$J"
  check "ZFS pool reported (dataset names have no /dev/ prefix)" \
        "assert 'tank' in [x['mount'] for x in d['disks']], d['disks']" "$J"
  check "hw.physmem64 used for total" \
        "assert d['mem_total_bytes']==534945792, d['mem_total_bytes']" "$J"
  rm -rf "$B"
fi

# ------------------------------------------------------------------ macOS ----
if want macos-intel; then
  echo "macos-intel (APFS volumes share one container)"
  B=$(mkbin x uname sysctl df mount hostname sw_vers top vm_stat)
  J=$(MOCK_MEMSIZE=17179869184 MOCK_BOOTTIME="{ sec = 1788000000, usec = 654321 }" \
      NETDASH_MOCK_OS=Darwin NETDASH_MOCK_REL=15.7.9 NETDASH_MOCK_ARCH=x86_64 \
      NETDASH_MOCK_FIXDIR=macos NETDASH_MOCK_DF=df-k-l-intel NETDASH_MOCK_MOUNT=mount-intel \
      run_collector "$B" macos)
  check "one entry per container, not per volume" \
        "assert sorted(x['mount'] for x in d['disks'])==['/','/Volumes/photos_library'], d['disks']" "$J"
  check "boot container reports total-available (89.8%), not Data's 83.2%" \
        "r=[x for x in d['disks'] if x['mount']=='/'][0]; p=100*r['used_bytes']/r['total_bytes']; assert abs(p-89.8)<0.2, p" "$J"
  check "single-volume container unaffected" \
        "r=[x for x in d['disks'] if 'photos' in x['mount']][0]; p=100*r['used_bytes']/r['total_bytes']; assert abs(p-15.9)<0.2, p" "$J"
  rm -rf "$B"
fi

if want macos-applesilicon; then
  echo "macos-applesilicon (Xcode simulator runtimes and Apple system volumes)"
  B=$(mkbin x uname sysctl df mount hostname sw_vers top vm_stat)
  J=$(MOCK_MEMSIZE=17179869184 MOCK_BOOTTIME="{ sec = 1788000000, usec = 654321 }" \
      NETDASH_MOCK_OS=Darwin NETDASH_MOCK_REL=26.6.2 NETDASH_MOCK_ARCH=arm64 \
      NETDASH_MOCK_FIXDIR=macos NETDASH_MOCK_DF=df-k-l-applesilicon NETDASH_MOCK_MOUNT=mount-applesilicon \
      run_collector "$B" macos)
  check "read-only CoreSimulator runtimes dropped (they pin 97% forever)" \
        "assert not any('CoreSimulator' in x['mount'] for x in d['disks']), d['disks']" "$J"
  check "read-write but all-nobrowse container dropped (xarts/iSCPreboot/Hardware)" \
        "assert not any('Hardware' in x['mount'] for x in d['disks']), d['disks']" "$J"
  check "boot container survives and reads 91.4%" \
        "assert [x['mount'] for x in d['disks']]==['/'], d['disks']; p=100*d['disks'][0]['used_bytes']/d['disks'][0]['total_bytes']; assert abs(p-91.4)<0.2, p" "$J"
  rm -rf "$B"
fi

# ------------------------------------------------------------------ Linux ----
# The Linux collector reads /proc directly, which PATH cannot intercept, so these
# drive the shipped awk programs against fixtures instead of the whole script.
if want linux-disks; then
  echo "linux-disks (openSUSE btrfs subvolumes, ZFS-on-Linux, NFS)"
  for AWK in awk busybox-awk; do
    if [ "$AWK" = busybox-awk ] && ! command -v busybox >/dev/null 2>&1; then
      echo "  skip busybox awk (not installed)"; continue
    fi
    J=$(python3 - "$ROOT" "$AWK" <<'PY'
import re,subprocess,sys,json
root,awkname=sys.argv[1],sys.argv[2]
src=open(f"{root}/collectors/linux/netdash-collector.sh").read()
prog=re.search(r"df -P -k 2>/dev/null; \} \| awk '(.*?)'\)", src, re.S).group(1)
stream=open(f"{root}/tests/fixtures/linux/proc-mounts-opensuse.txt").read()+"@@DF@@\n"+ \
       open(f"{root}/tests/fixtures/linux/df-P-k-opensuse.txt").read()
cmd=["busybox","awk",prog] if awkname=="busybox-awk" else ["awk",prog]
out=subprocess.run(cmd,input=stream,capture_output=True,text=True)
print(json.dumps({"disks":json.loads("["+out.stdout+"]")}))
PY
)
    check "[$AWK] 8 btrfs subvolumes collapse to one filesystem" \
          "assert sorted(x['mount'] for x in d['disks'])==['/','/boot/efi','/zfsroot'], d['disks']" "$J"
    check "[$AWK] ZFS datasets grouped into one pool" \
          "r=[x for x in d['disks'] if x['mount']=='/zfsroot'][0]; assert abs(100*r['used_bytes']/r['total_bytes']-40.0)<0.5" "$J"
    check "[$AWK] NFS mount excluded" \
          "assert not any('media' in x['mount'] for x in d['disks'])" "$J"
  done
fi

if want linux-bigmem; then
  echo "linux-bigmem (BusyBox awk uses 32-bit ints for %d)"
  if command -v busybox >/dev/null 2>&1; then
    J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/linux/netdash-collector.sh").read()
prog=re.search(r"eval \"\$\(awk '(.*?)' /proc/meminfo\)\"", src, re.S).group(1)
meminfo="MemTotal:        4014516 kB\nMemAvailable:    2358676 kB\n"
out=subprocess.run(["busybox","awk",prog],input=meminfo,capture_output=True,text=True)
vals=dict(l.split("=",1) for l in out.stdout.strip().split("\n"))
print(json.dumps({"mem_total_bytes":int(float(vals["MEM_TOTAL"]))}))
PY
)
    check "4 GiB host does not report -2147483648" \
          "assert d['mem_total_bytes']==4110864384, d['mem_total_bytes']" "$J"
  else
    echo "  skip (busybox not installed)"
  fi
fi

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
