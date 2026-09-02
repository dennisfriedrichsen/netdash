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
awkprog=re.search(r"pkg_admin audit \| awk '(.*?)' \| sort -u", src, re.S).group(1)
pat=re.search(r"/(vulnerabilit\w*)/", awkprog).group(1)

def count(text):
    """The shipped pipeline: awk | sort -u | grep -c ."""
    a=subprocess.run(["awk",awkprog],input=text,capture_output=True,text=True).stdout
    u=subprocess.run(["sort","-u"],input=a,capture_output=True,text=True).stdout
    return int(subprocess.run(["grep","-c","."],input=u,capture_output=True,text=True).stdout or 0)

found=open(f"{root}/tests/fixtures/netbsd/pkg_admin-audit.txt").read()
nodb=open(f"{root}/tests/fixtures/netbsd/pkg_admin-audit-nodb.txt").read()
# Same package, two advisories: must count as one package to upgrade, matching
# FreeBSD's "N package(s) found".
twice=found + found.replace("symlink-attack","denial-of-service")
print(json.dumps({
  "pattern": pat, "found": count(found), "nodb": count(nodb), "twice": count(twice),
  # A prefix match -- the obvious "be lenient about singular/plural" instinct --
  # matches the path in the error message instead.
  "naive_nodb": int(subprocess.run(["grep","-c","vulnerabilit"],input=nodb,
                                   capture_output=True,text=True).stdout or 0),
}))
PY
)
  check "one vulnerable package is counted once" \
        "assert d['found']==1, d" "$J"
  # Deduplicated by package name so this means what FreeBSD's "N package(s)
  # found" means: packages needing an upgrade. A package with three advisories
  # is still one action.
  check "two advisories against one package still count one package" \
        "assert d['twice']==1, d" "$J"
  # 'Cannot open /usr/pkg/pkgdb/pkg-vulnerabilities' must count as zero, and
  # the surrounding script bails on the absent database before this ever runs.
  # If it did count, a host with no database would report a clean bill of health.
  check "the 'Cannot open pkg-vulnerabilities' error counts zero" \
        "assert d['nodb']==0, d" "$J"
  check "a lenient 'vulnerabilit' prefix would match the error path instead" \
        "assert d['naive_nodb']==1 and d['nodb']==0, d" "$J"
fi

if want patches-freebsd; then
  echo "patches-freebsd (count packages to upgrade, not advisories against them)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/bsd/netdash-patchcheck.sh").read()
sed_expr=re.search(r"\| sed -n '([^']*package\(s\) found[^']*)'", src).group(1)
fallback=re.search(r"\| grep -c '(is vulnerable)'", src).group(1)
fx=open(f"{root}/tests/fixtures/freebsd/pkg-audit.txt").read()

pkgs=subprocess.run(["sed","-n",sed_expr],input=fx,capture_output=True,text=True).stdout.strip()
hdrs=int(subprocess.run(["grep","-c",fallback],input=fx,capture_output=True,text=True).stdout or 0)
probs=subprocess.run(["sed","-n",r"s/^\([0-9][0-9]*\) problem(s).*/\1/p"],
                     input=fx,capture_output=True,text=True).stdout.strip()
print(json.dumps({
  "packages": int(pkgs or 0), "headers": hdrs, "problems": int(probs or 0),
  "pkgbase_detected": "pkg info -e FreeBSD-runtime" in src,
}))
PY
)
  # The captured host: 14 problems across 9 packages. One chromium carried 300+
  # CVEs on its own, so counting advisories would let a single package swamp the
  # badge. Nine is the number of upgrades to perform.
  check "the summary line yields 9 packages, not 14 problems" \
        "assert d['packages']==9 and d['problems']==14, d" "$J"
  check "the 'is vulnerable' header fallback agrees at 9" \
        "assert d['headers']==9, d" "$J"
  # On a pkgbase host the base system IS packages, so pkg audit already covers
  # it and freebsd-update owns nothing -- its fetch would pull binary patches
  # for a base it does not manage.
  check "pkgbase is detected so freebsd-update is skipped there" \
        "assert d['pkgbase_detected'], d" "$J"
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

if want patches-openbsd; then
  echo "patches-openbsd (empty output is only an all-clear when syspatch exited 0)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/bsd/netdash-patchcheck.sh").read()
ob=re.search(r"^OpenBSD\)(.*?)^  ;;", src, re.S|re.M).group(1)
# A current box prints nothing at all and exits 0 (verified on OpenBSD 7.9), so
# the count is a line count over possibly-empty input.
empty=int(subprocess.run(["grep","-c","."],input="",capture_output=True,text=True).stdout or 0)
two=int(subprocess.run(["grep","-c","."],input="001_libcrypto\n002_ssh\n",
                       capture_output=True,text=True).stdout or 0)
print(json.dumps({
  "empty_counts_zero": empty,
  "two_counts_two": two,
  # The load-bearing line: the exit status is tested, so a mirror that cannot be
  # reached is distinguished from a box with no patches pending. Both produce no
  # output on stdout; only the status tells them apart.
  "guards_exit_status": bool(re.search(r"if ! OUT=\$\(syspatch -c", ob)),
  "no_swallow": "syspatch -c 2>/dev/null || true" not in ob,
  "requires_root": 'id -u' in ob,
}))
PY
)
  check "no patches pending counts zero" \
        "assert d['empty_counts_zero']==0 and d['two_counts_two']==2, d" "$J"
  # syspatch -c fetches the index from the mirror on every run and prints
  # nothing when there is nothing to do. An unreachable mirror also prints
  # nothing, so treating bare emptiness as an all-clear would report a box that
  # could not be checked as fully patched.
  check "syspatch's exit status is checked, not swallowed" \
        "assert d['guards_exit_status'] and d['no_swallow'], d" "$J"
  check "root is required up front (only -l and usage work unprivileged)" \
        "assert d['requires_root'], d" "$J"
fi

if want patches-fedora; then
  echo "patches-fedora (dnf5 three-column rows; a fresh refresh dates to now)"
  J=$(python3 - "$ROOT" <<'PY'
import re,subprocess,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/linux/netdash-patchcheck.sh").read()
prog=re.search(r"count_dnf\(\) \{ run \$DNF -q check-update \"\$@\" \| awk '(.*?)'; \}", src).group(1)
def count(f):
    txt=open(f"{root}/tests/fixtures/linux/{f}").read()
    return int(subprocess.run(["awk",prog],input=txt,capture_output=True,text=True).stdout.strip() or 0)
allc, secc = count("dnf5-check-update.txt"), count("dnf5-check-update-security.txt")

# The cache directories are probed in order; /var/cache/dnf is an empty dnf4
# leftover on a dnf5 host and must not win over the live /var/cache/libdnf5.
order=re.search(r"for d in ([^;]*); do\n\s*META=", src).group(1).split()
# A successful refresh must define checked_at, before any cache-mtime fallback.
refreshed_first = re.search(r'if \[ "\$REFRESHED" -eq 1 \]; then\n\s*CHECKED=\$NOW\nelif \[ -n "\$META" \]', src)
print(json.dumps({"all": allc, "security": secc, "other": allc-secc,
                  "cache_order": order,
                  "refreshed_wins": bool(refreshed_first)}))
PY
)
  check "11 pending rows, 6 of them security, 5 other" \
        "assert (d['all'],d['security'],d['other'])==(11,6,5), d" "$J"
  # The kernel rows appear in check-update but not in --security on that host,
  # so they land in `other`. Which is why a green badge after `dnf update` is
  # not the same as a patched running system -- see needs-restarting.
  check "the dnf5 three-column layout parses at all (NF==3 was dnf4-shaped)" \
        "assert d['all']==11, d" "$J"
  check "the live libdnf5 cache is probed before the dnf4 leftover" \
        "assert d['cache_order'][0].endswith('libdnf5'), d" "$J"
  # dnf leaves /var/cache/libdnf5's mtime untouched when the metadata it
  # fetched is unchanged: a Fedora host checking hourly reported checked_at as
  # 344 minutes old, and at patch_stale_hours would have flipped to "unknown"
  # while working perfectly.
  check "a successful refresh sets checked_at to now, not the cache mtime" \
        "assert d['refreshed_wins'], d" "$J"
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
         "patch_source":None,"patch_detail":None,"patch_reboot":None}
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
  # Installed but not running: the package database is clean and the host is
  # still on the old code.
  "reboot":       s(patch_security=0,patch_other=0,patch_reboot=1,patch_checked_at=now-2*H),
  "reboot_stale": s(patch_security=0,patch_other=0,patch_reboot=1,patch_checked_at=now-72*H),
  "reboot_and_sec": s(patch_security=2,patch_other=0,patch_reboot=1,patch_checked_at=now-2*H),
  "reboot_and_oth": s(patch_security=0,patch_other=9,patch_reboot=1,patch_checked_at=now-2*H),
  "reboot_null":  s(patch_security=0,patch_other=0,patch_reboot=None,patch_checked_at=now-2*H),
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
  # A Fedora host finished `dnf update` with a clean database while still
  # booted on the old kernel and old libbluez, both of which had been on its
  # security list. Nothing pending plus a pending reboot is not "up to date".
  check "a clean database with a pending reboot is not ok" \
        "assert d['reboot']=='reboot', d" "$J"
  check "a pending reboot outranks plain updates but not security" \
        "assert d['reboot_and_oth']=='reboot' and d['reboot_and_sec']=='security', d" "$J"
  # Staleness still wins: an old reading is unknown whatever it claimed.
  check "a stale reading is unknown even when it reported a reboot" \
        "assert d['reboot_stale']=='unknown', d" "$J"
  # Most platforms have no way to answer. null must not read as "no reboot".
  check "no reboot mechanism (null) leaves an otherwise-clean host ok" \
        "assert d['reboot_null']=='ok', d" "$J"
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
block=re.search(r"(PATCHES=null\n.*?\ndone)", src, re.S).group(1)
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

if want version; then
  echo "version (six hardcoded copies must not drift, and 0.3.10 > 0.3.9)"
  J=$(python3 - "$ROOT" <<'PY'
import re,sys,json,glob
root=sys.argv[1]; sys.path.insert(0,f"{root}/server")
import app
want=open(f"{root}/VERSION").read().strip()
found={}
for f in sorted(glob.glob(f"{root}/collectors/*/netdash-*.sh")):
    m=re.search(r'^NETDASH_VERSION="([^"]+)"', open(f).read(), re.M)
    found[f.split("/collectors/")[1]] = m.group(1) if m else None
def newest(vs): return app.newest_version([{"collector_version":v} for v in vs])
print(json.dumps({
  "want": want, "found": found,
  "mismatched": {k:v for k,v in found.items() if v != want},
  "double_digit": ".".join(str(x) for x in newest(["0.3.9","0.3.10"])),
  "string_compare_would_say": max(["0.3.9","0.3.10"]),
  "ignores_unversioned": ".".join(str(x) for x in newest([None,"0.3.1",None])),
}))
PY
)
  check "every collector and patch check matches the VERSION file" \
        "assert not d['mismatched'], d['mismatched']" "$J"
  check "all six scripts carry a version at all" \
        "assert len(d['found'])==6 and all(d['found'].values()), d['found']" "$J"
  # Compared per component, not as text: "0.3.10" sorts before "0.3.9" as a
  # string, which would mark an entire fleet outdated the first time a minor
  # number reached double digits.
  check "0.3.10 is newer than 0.3.9 (string compare gets this backwards)" \
        "assert d['double_digit']=='0.3.10' and d['string_compare_would_say']=='0.3.9', d" "$J"
  # A host that has not upgraded yet reports no version at all; that is not the
  # same as running an old one, and must not drag the fleet baseline down.
  check "hosts reporting no version do not affect the newest seen" \
        "assert d['ignores_unversioned']=='0.3.1', d" "$J"
fi

echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
