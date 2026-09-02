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

# ---------------------------------------------------------------- patches ----
# The patch badge's job is to be believable, so these pin the two ways it could
# lie: counting the wrong packages as security, and showing a stale or absent
# check as "up to date". See PATCH-CHECKS.md.

if want patches-apt; then
  echo "patches-apt (security origin lives inside the parentheses, not in the name)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/linux/netdash-patchcheck.sh").read()
prog=re.search(r"dist-upgrade \| awk '(.*?)'\)", src, re.S).group(1)
fixture=open(f"{root}/tests/fixtures/linux/apt-dist-upgrade.txt").read()
out=subprocess.run(["awk",prog],input=fixture,capture_output=True,text=True)
sec,oth=out.stdout.split()
# What a naive whole-line match would have counted, for the contrast below.
naive=sum(1 for l in fixture.splitlines() if l.startswith("Inst ") and "-security" in l)
print(json.dumps({"security":int(sec),"other":int(oth),"naive":naive}))
PY
)
  check "3 security updates found among 36 pending" \
        "assert d['security']==3, d" "$J"
  check "the other 33 counted as non-security" \
        "assert d['other']==33, d" "$J"
  # debian-security-support is a real package whose NAME contains -security but
  # which ships from the plain archive. Matching the whole Inst line counts it,
  # and the fixture exists to keep that mistake from coming back.
  check "a package NAMED *-security is not miscounted as a security update" \
        "assert d['naive']==4 and d['security']==3, d" "$J"
fi

if want patches-zypper; then
  echo "patches-zypper (Tumbleweed reports 0 patches needed while 23 updates wait)"
  # As with linux-disks, these drive the shipped parsers rather than the whole
  # script: the branch is chosen by reading /etc/os-release, which PATH cannot
  # intercept.
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/linux/netdash-patchcheck.sh").read()
awkprog=re.search(r"list-updates \| awk '(.*?)'\)", src, re.S).group(1)
totp,secp=re.findall(r"sed -n '([^']*)'", src)[:2]

updates=open(f"{root}/tests/fixtures/linux/zypper-list-updates-tumbleweed.txt").read()
check=open(f"{root}/tests/fixtures/linux/zypper-patch-check-tumbleweed.txt").read()

def run(cmd,inp):
    return subprocess.run(cmd,input=inp,capture_output=True,text=True).stdout.strip()

print(json.dumps({
  "rolling":  int(run(["awk",awkprog],updates) or 0),
  "patch_total": run(["sed","-n",totp],check),
  "patch_sec":   run(["sed","-n",secp],check),
}))
PY
)
  check "the rolling branch counts 23 pending updates" \
        "assert d['rolling']==23, d" "$J"
  check "the header row and the ---+--- separator are not counted" \
        "assert d['rolling']==23, d" "$J"
  # This is the whole reason Tumbleweed is routed by os-release ID rather than
  # by whether patch-check produced output. On this real host patch-check says
  # '0 patches needed (0 security patches)' while kernel-default and libseccomp2
  # are among 23 waiting updates -- taking that branch would paint the card
  # green. Patches are a Leap/SLE concept and Tumbleweed ships none.
  check "patch-check reports 0/0 on the very same host: never route rolling here" \
        "assert d['patch_total']=='0' and d['patch_sec']=='0', d" "$J"
fi

if want patches-netbsd; then
  echo "patches-netbsd (a missing vulnerability database must not read as clean)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/bsd/netdash-patchcheck.sh").read()
pat=re.search(r"pkg_admin audit \| grep -c '([^']*)'", src).group(1)
def count(p,f):
    out=subprocess.run(["grep","-c",p,f],capture_output=True,text=True).stdout.strip()
    return int(out or 0)
found=f"{root}/tests/fixtures/netbsd/pkg_admin-audit.txt"
nodb=f"{root}/tests/fixtures/netbsd/pkg_admin-audit-nodb.txt"
print(json.dumps({
  "pattern": pat,
  "found":   count(pat, found),
  "nodb":    count(pat, nodb),
  # A prefix match -- the obvious "be lenient about singular/plural" instinct --
  # matches the path in the error message instead.
  "naive_nodb": count("vulnerabilit", nodb),
}))
PY
)
  check "one vulnerable package is counted once" \
        "assert d['found']==1, d" "$J"
  # 'Cannot open /usr/pkg/pkgdb/pkg-vulnerabilities' must count as zero, and
  # the surrounding script bails on the absent database before this ever runs.
  # If it did count, a host with no database would report a clean bill of health.
  check "the 'Cannot open pkg-vulnerabilities' error counts zero" \
        "assert d['nodb']==0, d" "$J"
  check "a lenient 'vulnerabilit' prefix would match the error path instead" \
        "assert d['naive_nodb']==1 and d['nodb']==0, d" "$J"
fi

if want patches-macos; then
  echo "patches-macos (nothing pending must be told apart from nothing parsed)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/macos/netdash-patchcheck.sh").read()
# Pull the live patterns out of the script so the test cannot drift from it.
clear=re.search(r"grep -q '([^']*No new software[^']*)'", src).group(1)
entry=re.search(r"grep -c '(\^\[\*-\][^']*)'", src).group(1)
rec=re.search(r"grep -Ec '(Recommended:[^']*)'", src).group(1)

def probe(text):
    def g(flags,pat):
        r=subprocess.run(["grep"]+flags+[pat],input=text,capture_output=True,text=True)
        return r.returncode==0 if "-q" in flags else int(r.stdout.strip() or 0)
    return {"allclear":    g(["-q"],  clear),
            "entries":     g(["-c"],  entry),
            "recommended": g(["-Ec"], rec)}

none=open(f"{root}/tests/fixtures/macos/softwareupdate-none.txt").read()
banner=open(f"{root}/tests/fixtures/macos/softwareupdate-stdout-only.txt").read()
print(json.dumps({"none": probe(none), "empty": probe(""), "banner": probe(banner),
                  "counters_referenced": bool(re.search(r"^\s*(REC|ALL)=\$\(sud ", src, re.M)),
                  "captures_stderr": "softwareupdate --list --no-scan 2>&1" in src}))
PY
)
  check "a Mac with nothing pending reports the all-clear string" \
        "assert d['none']['allclear'] and d['none']['entries']==0, d" "$J"
  # Both cases count zero entries, so the entry count alone cannot tell them
  # apart. Only the explicit all-clear string makes 'nothing pending' safe to
  # report as ok; empty output falls through to the guard and reads unknown.
  check "empty output has no all-clear string, so it cannot be read as ok" \
        "assert not d['empty']['allclear'] and d['empty']['entries']==0, d" "$J"
  # LastRecommendedUpdatesAvailable read 1 on a macOS 26 host that softwareupdate
  # said was fully up to date, even after a forced fresh scan. Nothing resets it.
  check "the unreliable plist counters are not used for the counts" \
        "assert not d['counters_referenced'], d" "$J"
  # softwareupdate puts its banner on stdout and "No new software available."
  # on stderr, so 2>/dev/null leaves a lone banner -- no all-clear string and no
  # entries, which is the unparseable case rather than a clean bill of health.
  check "the banner alone is not an all-clear" \
        "assert not d['banner']['allclear'] and d['banner']['entries']==0, d" "$J"
  check "stderr is captured, or every Mac reads as unparseable" \
        "assert d['captures_stderr'], d" "$J"
fi

if want patches-stale; then
  echo "patches-stale (an old or missing check must read unknown, never ok)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,time
root=sys.argv[1]; sys.path.insert(0,f"{root}/server")
import app
app.CFG={"patch_stale_hours":48}
now=time.time(); H=3600
def s(**kw):
    row={"patch_security":None,"patch_other":None,"patch_checked_at":None,
         "patch_source":None,"patch_detail":None}
    row.update(kw)
    return app.patch_summary(row,now)["status"]
print(json.dumps({
  "never":     s(),
  "fresh_ok":  s(patch_security=0,patch_other=0,patch_checked_at=now-2*H),
  "stale_ok":  s(patch_security=0,patch_other=0,patch_checked_at=now-72*H),
  "stale_sec": s(patch_security=9,patch_other=0,patch_checked_at=now-72*H),
  "security":  s(patch_security=3,patch_other=41,patch_checked_at=now-2*H),
  "updates":   s(patch_security=0,patch_other=12,patch_checked_at=now-2*H),
  "noclass":   s(patch_security=None,patch_other=7,patch_checked_at=now-2*H),
  "noclass0":  s(patch_security=None,patch_other=0,patch_checked_at=now-2*H),
}))
PY
)
  check "a host that has never been checked is unknown, not ok" \
        "assert d['never']=='unknown', d" "$J"
  # The whole point of the design: 0 pending from a check three days old is
  # indistinguishable from a patched host, so it must not render green.
  check "a 72h-old all-clear is unknown, not ok" \
        "assert d['stale_ok']=='unknown', d" "$J"
  check "a 72h-old check is unknown even when it found security updates" \
        "assert d['stale_sec']=='unknown', d" "$J"
  check "a fresh all-clear is ok" \
        "assert d['fresh_ok']=='ok', d" "$J"
  check "security outranks plain updates" \
        "assert d['security']=='security' and d['updates']=='updates', d" "$J"
  # Alpine, Arch without arch-audit and Tumbleweed cannot classify at all, so
  # they send null rather than 0 -- 0 would claim a CVE check found nothing.
  check "unclassifiable platform with updates pending reads as updates" \
        "assert d['noclass']=='updates', d" "$J"
  check "unclassifiable platform with nothing pending is still ok" \
        "assert d['noclass0']=='ok', d" "$J"
fi

if want patches-statefile; then
  echo "patches-statefile (a truncated state file must not become a bad reading)"
  TMPD=$(mktemp -d)
  printf '{"security":3,"other":41,"checked_at":1788300000,"source":"apt","detail":""}\n' > "$TMPD/good"
  printf '{"security":3,"other":41'                                                       > "$TMPD/truncated"
  printf 'not json at all\n'                                                              > "$TMPD/garbage"
  : > "$TMPD/empty"
  # All three collectors carry the same read-back block. Extracting and running
  # it keeps this honest on any OS: the suite must not need a Linux /proc, a
  # FreeBSD sysctl or a Mac to check shared logic.
  for FAM in linux bsd macos; do
    J=$(python3 - "$ROOT" "$FAM" "$TMPD" <<'PY'
import re,subprocess,sys,json
root,fam,tmpd=sys.argv[1],sys.argv[2],sys.argv[3]
src=open(f"{root}/collectors/{fam}/netdash-collector.sh").read()
block=re.search(r"(PATCHES=null\nfor f in .*?\ndone)", src, re.S).group(1)
out={}
for name in ("good","truncated","garbage","empty","/nonexistent/nope"):
    path=name if name.startswith("/") else f"{tmpd}/{name}"
    r=subprocess.run(["sh","-c",f'NETDASH_PATCH_STATE="{path}"\n'+block+'\nprintf "%s" "$PATCHES"'],
                     capture_output=True,text=True)
    out[name.strip("/").split("/")[-1] if name.startswith("/") else name]=r.stdout
print(json.dumps(out))
PY
)
    check "[$FAM] a well-formed state file is passed through" \
          "assert d['good'].startswith('{') and 'apt' in d['good'], d" "$J"
    check "[$FAM] truncated, garbage, empty and absent all report null" \
          "assert all(d[k]=='null' for k in ('truncated','garbage','empty','nope')), d" "$J"
  done
  rm -rf "$TMPD"
fi

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
