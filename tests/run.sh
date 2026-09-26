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
# node is optional: it enables the `render` case, which runs the dashboard
# JS instead of merely parsing it. Without it that one case skips.
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

# A generator that dies leaves json empty, which lands here as a failure rather
# than aborting the run: under `set -e` a failing J=$(...) assignment would take
# the whole suite with it, and every case after the broken one would silently
# never execute.
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
) || J=''
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
) || J=''
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
prog=re.search(r'"\$UPG" \| awk \'(.*?)\'\)', src, re.S).group(1)
names=re.search(r"SECPKGS=\$\(printf[^|]*\| awk '(.*?)' \| join_names\)", src, re.S).group(1)
joinfn=re.search(r"(join_names\(\) \{.*?\n\})", src, re.S).group(1)
fixture=open(f"{root}/tests/fixtures/linux/apt-dist-upgrade.txt").read()
out=subprocess.run(["awk",prog],input=fixture,capture_output=True,text=True)
sec,oth=out.stdout.split()

def join(text):
    return subprocess.run(["sh","-c",joinfn+"\njoin_names"],input=text,
                          capture_output=True,text=True).stdout
raw=subprocess.run(["awk",names],input=fixture,capture_output=True,text=True).stdout
# What a naive whole-line match would have counted, for the contrast below.
naive=sum(1 for l in fixture.splitlines() if l.startswith("Inst ") and "-security" in l)
print(json.dumps({"security":int(sec),"other":int(oth),"naive":naive,
  "names": join(raw),
  "nine_names": join("".join("pkg-%d\n" % i for i in range(1,10))),
  "no_quotes": join('ok-1.0\nbad"quote\\\\slash\n'),
  "portepoch": join("gimp-2.10.38,2\nopenssl\n"),
}))
PY
) || J=''
  check "3 security updates found among 36 pending" \
        "assert d['security']==3, d" "$J"
  check "the other 33 counted as non-security" \
        "assert d['other']==33, d" "$J"
  # debian-security-support is a real package whose NAME contains -security but
  # which ships from the plain archive. Matching the whole Inst line counts it,
  # and the fixture exists to keep that mistake from coming back.
  check "a package NAMED *-security is not miscounted as a security update" \
        "assert d['naive']==4 and d['security']==3, d" "$J"
  # The badge says how many; the names say which, so a host can be triaged
  # without logging into it. Only the security ones are listed.
  check "the three security packages are named, and only those" \
        "assert d['names']=='openssl-provider-legacy, libssl3t64, openssl', d" "$J"
  # The cap this replaces stopped at six and summarised the rest as "(+3 more)",
  # which the server then had to offer as a single anonymous ack target -- a
  # package you cannot see is a package you cannot review. Every name now rides.
  check "every name is reported, none summarised into a group" \
        "assert d['nine_names']=='pkg-1, pkg-2, pkg-3, pkg-4, pkg-5, pkg-6, pkg-7, pkg-8, pkg-9', d" "$J"
  # The join is ", " precisely so a name containing a bare comma survives it:
  # FreeBSD carries PORTEPOCH that way and db.split_packages splits to match.
  check "a PORTEPOCH comma is not mistaken for a separator" \
        "assert d['portepoch']=='gimp-2.10.38,2, openssl', d" "$J"
  # The names go into a JSON string built by printf in shell, so a quote or
  # backslash in a package name would produce malformed JSON.
  check "quotes and backslashes are stripped before the names reach JSON" \
        "assert '\"' not in d['no_quotes'] and '\\\\' not in d['no_quotes'], d" "$J"
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
) || J=''
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

def pkgin(text):
    """Run the shipped pkgin branch against captured output."""
    blk=re.search(r'(PKGIN=\$\(run pkgin -n upgrade\).*?esac)', src, re.S).group(1)
    blk=blk.replace('$(run pkgin -n upgrade)', '"$PKGIN_IN"')
    r=subprocess.run(["sh","-c",'OTH=null\n'+blk+'\nprintf "%s" "$OTH"'],
                     env={"PKGIN_IN":text,"PATH":"/usr/bin:/bin"},capture_output=True,text=True)
    return r.stdout

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
 "pkgin_none": pkgin(open(f"{root}/tests/fixtures/netbsd/pkgin-upgrade-none.txt").read()),
 "pkgin_junk": pkgin("some wording this version does not use\n"),
}))
PY
) || J=''
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
  # "nothing to do." is a real answer, not a parse failure: a NetBSD box with
  # nothing pending must not look like one nobody could ask. Wording this pkgin
  # does not use still leaves the count null rather than guessing at zero.
  check "pkgin's 'nothing to do' is a real 0, unrecognised wording is null" \
        "assert (d['pkgin_none'],d['pkgin_junk'])==('0','null'), d" "$J"
fi

if want patches-freebsd; then
  echo "patches-freebsd (count packages to upgrade, not advisories against them)"
  J=$(python3 - "$ROOT" <<'PY'
import os,re,shlex,shutil,subprocess,sys,tempfile,json
root=sys.argv[1]
src=open(f"{root}/collectors/bsd/netdash-patchcheck.sh").read()
sed_expr=re.search(r"\| sed -n '([^']*package\(s\) found[^']*)'", src).group(1)
fallback=re.search(r"\| grep -c '(is vulnerable)'", src).group(1)
fx=open(f"{root}/tests/fixtures/freebsd/pkg-audit.txt").read()

pkgs=subprocess.run(["sed","-n",sed_expr],input=fx,capture_output=True,text=True).stdout.strip()
hdrs=int(subprocess.run(["grep","-c",fallback],input=fx,capture_output=True,text=True).stdout or 0)
probs=subprocess.run(["sed","-n",r"s/^\([0-9][0-9]*\) problem(s).*/\1/p"],
                     input=fx,capture_output=True,text=True).stdout.strip()

# The freebsd-update branch (non-pkgbase base, e.g. a 14.4 host) increments
# SEC but must also touch SECPKGS -- otherwise two different staged base
# patches that both leave pkg audit's own list empty are indistinguishable to
# netdash's patch-ack fingerprint (security count + package list). Extracted
# and run standalone, same as the sed/grep expressions above, rather than
# executing the whole script: this path has no pkg/freebsd-update mock to run
# it end to end, same as the pkgbase path above.
snippet = re.search(r"SECPKGS=\$\(printf '%s%sfreebsd-update.*?\)\n", src, re.S).group(0)
def secpkgs(existing, fake_version):
    # A shell function named freebsd-version is a syntax error under dash's
    # strict POSIX parser (hyphens are not valid in a function name there),
    # unlike bash -- so the mock has to be a real PATH executable, same as
    # every other mocked command this suite uses.
    d = tempfile.mkdtemp()
    with open(f"{d}/freebsd-version", "w") as f:
        f.write(f"#!/bin/sh\necho {shlex.quote(fake_version)}\n")
    os.chmod(f"{d}/freebsd-version", 0o755)
    script = "SECPKGS=%s\n%s\nprintf '%%s' \"$SECPKGS\"" % (shlex.quote(existing), snippet)
    env = {**os.environ, "PATH": d + ":" + os.environ["PATH"]}
    out = subprocess.run(["sh","-c",script],capture_output=True,text=True,env=env).stdout
    shutil.rmtree(d)
    return out

print(json.dumps({
  "packages": int(pkgs or 0), "headers": hdrs, "problems": int(probs or 0),
  "pkgbase_detected": "pkg info -e FreeBSD-runtime" in src,
  # pkg audit exits 1 for BOTH "fetch failed" and "found vulnerable packages",
  # so the fetch has to be judged by stderr, never by exit status.
  "fetch_by_stderr": bool(re.search(r'FERR=\$\(pkg audit -F -q 2>&1 >/dev/null', src)),
  "no_exit_status_gate": "pkg audit -F -q >/dev/null 2>&1 &&" not in src,
  "version_only":    secpkgs("", "14.4-RELEASE-p7"),
  "joins_with_pkgs": secpkgs("chromium-1.2.3", "14.4-RELEASE-p7"),
  "version_bump":    secpkgs("", "14.4-RELEASE-p8"),
  # A real 14.4 host with nothing staged (freebsd-update updatesready exits 2)
  # left SRC at the plain "pkg-audit" default -- identical to freebsd-update
  # never running at all. The source line must be set as soon as the branch
  # is entered, before the inner "is anything actually pending" check, same
  # as the pkgbase branch sets its source unconditionally.
  "src_set_before_updatesready_check":
    src.index('SRC="pkg-audit+freebsd-update"') < src.index("if freebsd-update updatesready"),
}))
PY
) || J=''
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
  # A traditional-base host with a clean freebsd-update (nothing staged) must
  # still say so was checked -- a real fleet host reported plain "pkg-audit"
  # here, indistinguishable from freebsd-update not running at all.
  check "a clean freebsd-update check still records that base was checked" \
        "assert d['src_set_before_updatesready_check'], d" "$J"
  # The captured host exits 1 with nine vulnerable packages and an empty stderr,
  # so a successful fetch is indistinguishable from a failed one by status alone.
  check "the vuln.xml fetch is judged by stderr, not by exit status" \
        "assert d['fetch_by_stderr'] and d['no_exit_status_gate'], d" "$J"
  check "a staged base update names the running version, even with no vulnerable packages" \
        "assert d['version_only']=='freebsd-update: staged patch beyond 14.4-RELEASE-p7', d" "$J"
  check "it joins onto an existing pkg audit list rather than replacing it" \
        "assert d['joins_with_pkgs']=='chromium-1.2.3, freebsd-update: staged patch beyond 14.4-RELEASE-p7', d" "$J"
  # The whole point: once the running version moves, a stale ack against the
  # old string stops matching on its own.
  check "a different running version yields a different string" \
        "assert d['version_only'] != d['version_bump'], d" "$J"
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
) || J=''
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
  "quirks_none": int(subprocess.run(
      ["sh","-c","grep -v '^quirks-[0-9][^ ]* signed on ' | grep -c . || true"],
      input=open(f"{root}/tests/fixtures/openbsd/pkg_add-un-none.txt").read(),
      capture_output=True,text=True).stdout or 0),
  "quirks_naive": int(subprocess.run(["grep","-c","."],
      input=open(f"{root}/tests/fixtures/openbsd/pkg_add-un-none.txt").read(),
      capture_output=True,text=True).stdout or 0),
  "requires_root": 'id -u' in ob,
  # pkg_add falls back to /etc/installurl when PKG_PATH is unset, so gating the
  # package count on PKG_PATH skipped it on every normally configured host.
  "no_pkgpath_gate": 'if [ -n "${PKG_PATH:-}" ]; then' not in ob,
  "quirks_filtered": bool(re.search(r"grep -v '\^quirks", ob)),
}))
PY
) || J=''
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
  check "the package count is not gated on PKG_PATH" \
        "assert d['no_pkgpath_gate'], d" "$J"
  # "quirks-7.194 signed on ..." is printed whenever pkg_add reads the index and
  # is not an update; counting it reported one phantom package on an idle host.
  check "pkg_add's quirks signature line is not counted as an update" \
        "assert d['quirks_filtered'] and d['quirks_none']==0 and d['quirks_naive']==1, d" "$J"
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
) || J=''
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
) || J=''
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
) || J=''
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
) || J=''
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

if want eol; then
  echo "eol (real fleet OS strings map to real release cycles)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json; sys.path.insert(0,f"{sys.argv[1]}/server")
import eol
from datetime import date
# Cycle data captured from endoflife.date, not fetched: the suite runs with no
# network. Only the shapes the matcher depends on are kept.
eol._CACHE.update({
 # isEoes/eoesFrom captured alongside isEol/eolFrom: Debian 12 has already
 # passed eolFrom (Security Team handoff) but not eoesFrom (its own LTS Team
 # stepping back), and Debian 11 has now passed both.
 "debian":  {"ts":9e9,"releases":[{"name":"13","isEol":False,"eolFrom":"2028-08-09",
                                    "isEoes":False,"eoesFrom":"2030-06-30"},
                                  {"name":"12","isEol":True,"eolFrom":"2026-07-11",
                                   "isEoes":False,"eoesFrom":"2028-06-30"},
                                  {"name":"11","isEol":True,"eolFrom":"2024-08-14",
                                   "isEoes":True,"eoesFrom":"2026-08-31"}]},
 # 20.04's eoesFrom is 2030 -- Ubuntu Pro ESM, paid and opt-in. A stock install
 # gets none of it, so it must not make an already-eolFrom release read supported.
 "ubuntu":  {"ts":9e9,"releases":[{"name":"24.04","isEol":False,"eolFrom":"2029-05-31"},
                                  {"name":"22.04","isEol":False,"eolFrom":"2027-06-01"},
                                  {"name":"20.04","isEol":True,"eolFrom":"2025-05-31",
                                   "isEoes":False,"eoesFrom":"2030-04-23"}]},
 "netbsd":  {"ts":9e9,"releases":[{"name":"11","isEol":False,"eolFrom":None}]},
 "macos":   {"ts":9e9,"releases":[{"name":"26","isEol":False,"eolFrom":None}]},
 "freebsd": {"ts":9e9,"releases":[{"name":"15.1","isEol":False,"eolFrom":"2027-03-31"},
                                  {"name":"15","isEol":False,"eolFrom":"2028-12-31"}]},
 "fedora":  {"ts":9e9,"releases":[{"name":"43","isEol":False,"eolFrom":"2026-12-09"}]},
})
cfg={"warn_days":30,"overrides":{"TrueNAS CORE":"2025-03-31"}}
T=date(2026,9,2)
def q(s): return eol.lookup(s,cfg,today=T)
# Same real cycle, two different "todays", to exercise the warn_days boundary
# without inventing a release: Fedora 43 ends 2026-12-09.
FED="Fedora Linux 43 (Server Edition) (x86_64)"
def qd(s,d): return eol.lookup(s,cfg,today=d)
print(json.dumps({
 "ubuntu_trims":  q("Ubuntu 24.04.4 LTS (x86_64)")["cycle"],
 "netbsd_trims":  q("NetBSD 11.0 (amd64)")["cycle"],
 "macos_trims":   q("macOS 26.6.2 (arm64)")["cycle"],
 "freebsd_exact": q("FreeBSD 15.1-RELEASE-p3 (amd64)")["cycle"],
 "raspbian":      q("Raspbian GNU/Linux 13 (trixie) - Raspberry Pi 3 Model B Plus (armv7l)")["cycle"],
 "arch":          q("Arch Linux (x86_64)")["status"],
 "tumbleweed":    q("openSUSE Tumbleweed (x86_64)")["status"],
 "truenas":       q("TrueNAS CORE 13.0 U6.8")["status"],
 "truenas_src":   q("TrueNAS CORE 13.0 U6.8")["source"],
 "old_debian":    q("Debian GNU/Linux 11 (bullseye) (x86_64)")["status"],
 "debian12":      q("Debian GNU/Linux 12 (bookworm) (x86_64)")["status"],
 "debian12_date": q("Debian GNU/Linux 12 (bookworm) (x86_64)")["eol_date"],
 "debian13_date": q("Debian GNU/Linux 13 (trixie) (x86_64)")["eol_date"],
 "ubuntu_esm":    q("Ubuntu 20.04.6 LTS (x86_64)")["status"],
 "no_eol_date":   q("macOS 26.6.2 (arm64)")["status"],
 "unmatched":     q("Plan 9 from Bell Labs")["status"],
 "fedora_98d":    qd(FED, date(2026,9,2))["status"],
 "fedora_98d_n":  qd(FED, date(2026,9,2))["days_left"],
 "fedora_19d":    qd(FED, date(2026,11,20))["status"],
 "fedora_19d_n":  qd(FED, date(2026,11,20))["days_left"],
 # A product the matcher recognises but the cache does not hold. It must not
 # fall back to an optimistic "supported".
 "uncached":      q("openSUSE Leap 15.6 (x86_64)")["status"],
 "uncached_prod": q("openSUSE Leap 15.6 (x86_64)")["product"],
}))
PY
) || J=''
  # Every platform spells its version differently from its release cycle.
  check "versions are trimmed to the cycle the platform actually uses" \
        "assert (d['ubuntu_trims'],d['netbsd_trims'],d['macos_trims'])==('24.04','11','26'), d" "$J"
  # FreeBSD publishes both "15.1" and "15"; the exact one must win.
  check "the most specific matching cycle wins over a shorter one" \
        "assert d['freebsd_exact']=='15.1', d" "$J"
  check "Raspbian resolves as Debian" \
        "assert d['raspbian']=='13', d" "$J"
  # Rolling releases have no cycle to expire; "unknown" would be a permanent
  # unanswerable question on the card, "rolling" is the real answer.
  check "rolling releases report rolling, not unknown" \
        "assert d['arch']=='rolling' and d['tumbleweed']=='rolling', d" "$J"
  # endoflife.date's truenas product covers SCALE only, so CORE 13 has no cycle.
  # The config override is the only way to answer for it.
  check "a config override answers where endoflife.date has no product" \
        "assert d['truenas']=='eol' and d['truenas_src']=='config', d" "$J"
  check "a past-EOL release reads eol" \
        "assert d['old_debian']=='eol', d" "$J"
  # Debian's LTS Team takes over for free the moment the Security Team steps
  # back (https://www.debian.org/News/2026/20260712) -- eolFrom marks that
  # handover, not an ending, so a release between eolFrom and eoesFrom must
  # still read supported, against the later date.
  check "Debian between eolFrom and eoesFrom reads supported, not eol" \
        "assert d['debian12']=='supported' and d['debian12_date']=='2028-06-30', d" "$J"
  check "a Debian release still in regular support also reports its eoes date" \
        "assert d['debian13_date']=='2030-06-30', d" "$J"
  # Ubuntu's own extended phase is Extended Security Maintenance -- paid,
  # opt-in via Ubuntu Pro, and absent on a stock install. It must not rescue
  # an eolFrom release the way Debian's free LTS does.
  check "Ubuntu's paid ESM does not rescue an eolFrom release" \
        "assert d['ubuntu_esm']=='eol', d" "$J"
  # warn_days defaults to 30, not 90: Fedora's ~13-month cycle would otherwise
  # hold those hosts amber for three months twice a year, and a warning that is
  # always on is one nobody reads. 98 days out is quiet; 19 days out is not.
  check "98 days out is supported at the 30-day warning, 19 days out is eol_soon" \
        "assert (d['fedora_98d'],d['fedora_19d'])==('supported','eol_soon'), d" "$J"
  check "the countdown is right in both directions" \
        "assert (d['fedora_98d_n'],d['fedora_19d_n'])==(98,19), d" "$J"
  # macOS and NetBSD announce no end date at all; that is supported, not eol.
  check "a cycle with no announced end date is supported, not eol" \
        "assert d['no_eol_date']=='supported', d" "$J"
  # Both must be unknown rather than an optimistic "supported": one is an OS
  # nothing recognises, the other a product the cache has not fetched.
  check "an unrecognised OS and an uncached product both read unknown" \
        "assert d['unmatched']=='unknown' and d['uncached']=='unknown', d" "$J"
  check "an uncached product is still identified, just not answered" \
        "assert d['uncached_prod']=='opensuse', d" "$J"
fi

if want reboot-kernel; then
  echo "reboot-kernel (Debian 13 has no flag mechanism; compare kernels instead)"
  J=$(python3 - "$ROOT" <<'PY'
import re,sys,json,subprocess
root=sys.argv[1]
src=open(f"{root}/collectors/linux/netdash-patchcheck.sh").read()
sortcmd=re.search(r"KNEW=\$\(ls /boot/vmlinuz-\* 2>/dev/null \| ([^)]*)\)", src).group(1)
boot="6.12.107+deb13-amd64\n6.12.94+deb13-amd64\n"
def newest(cmd):
    return subprocess.run(["sh","-c","sed 's|.*/vmlinuz-||' | "+cmd],
                          input=boot,capture_output=True,text=True).stdout.strip()
print(json.dumps({
  "uses_version_sort": "sort -V" in sortcmd,
  "newest": newest(sortcmd),
  "plain_sort_would_say": newest("sort | tail -1"),
  # The gate tests the script both packages ship, not either package name.
  "gates_on_script": "/usr/share/update-notifier/notify-reboot-required" in src,
  # The old gate queried dpkg for a package that does not exist on Debian 13.
  # Mentioning it in a comment is fine; gating on it is not.
  "no_package_gate": "dpkg-query" not in src,
  # The kernel fallback may only ever prove true; an unchanged kernel cannot
  # rule out an openssl update wanting a restart.
  "fallback_sets_only_true": bool(re.search(
      r'\[ "\$KNEW" != "\$KRUN" \]; then\n\s*REBOOT=true', src)),
}))
PY
) || J=''
  # 6.12.94 sorts after 6.12.107 as text, so a plain sort calls a freshly booted
  # Debian host stale on every point release.
  check "kernels compare by version, not as strings" \
        "assert d['uses_version_sort'] and d['newest'].endswith('107+deb13-amd64'), d" "$J"
  check "a plain sort would pick the older kernel" \
        "assert d['plain_sort_would_say'].endswith('94+deb13-amd64'), d" "$J"
  # update-notifier-common does not exist on Debian 13; reboot-notifier ships
  # the same script there. Testing for the script covers both.
  check "the flag mechanism is detected by its script, not by a package name" \
        "assert d['gates_on_script'] and d['no_package_gate'], d" "$J"
  check "the kernel fallback can only report true, never false" \
        "assert d['fallback_sets_only_true'], d" "$J"
fi

if want host-thresholds; then
  echo "host-thresholds (per-host overrides merge, they do not replace)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,time; sys.path.insert(0,f"{sys.argv[1]}/server")
import app
app.CFG={"thresholds":{"cpu":{"warn":80,"crit":95},
                       "mem":{"warn":85,"crit":95},
                       "disk":{"warn":85,"crit":95}},
         "stale_after_seconds":180,
         "patch_stale_hours":48,
         "eol":{"enabled":False},
         "hosts":{
           # NetBSD reads high because active file cache counts as used, so its
           # memory warning is raised -- but only warn, not crit.
           "netbsd11dot0":{"thresholds":{"mem":{"warn":92}}},
           # A laptop that is legitimately off the LAN for hours.
           "argon":{"stale_after_seconds":900},
         }}
now=time.time()
def s(host, **kw):
    row={"host":host,"ts":now-kw.pop("age",5),"os":"x","cpu_pct":None,
         "mem_used_bytes":None,"mem_total_bytes":None,"uptime_seconds":1,
         "patch_security":None,"patch_other":None,"patch_checked_at":None,
         "patch_source":None,"patch_detail":None,"patch_reboot":None,
         "patch_packages":None,"collector_version":None,"disks":[]}
    row.update(kw)
    return app.summarize(row, now)
# 90% memory: over the global 85 warn, under netbsd's raised 92.
mem=dict(mem_used_bytes=90,mem_total_bytes=100)
print(json.dumps({
  "override_warn":  app.thresholds_for("netbsd11dot0")["mem"]["warn"],
  "inherited_crit": app.thresholds_for("netbsd11dot0")["mem"]["crit"],
  "other_metric":   app.thresholds_for("netbsd11dot0")["cpu"]["warn"],
  "unlisted_host":  app.thresholds_for("hermes")["mem"]["warn"],
  "netbsd_at_90":   s("netbsd11dot0", **mem)["mem"]["status"],
  "hermes_at_90":   s("hermes", **mem)["mem"]["status"],
  "stale_override": app.stale_after_for("argon"),
  "stale_default":  app.stale_after_for("hermes"),
  "argon_600s":     s("argon", age=600)["stale"],
  "hermes_600s":    s("hermes", age=600)["stale"],
  "reported":       s("netbsd11dot0", **mem)["thresholds"]["mem"],
}))
PY
) || J=''
  # An override naming only warn must inherit crit, not blank it: restating
  # numbers you did not mean to change is how a fleet's thresholds drift.
  check "an override merges per level, inheriting what it does not name" \
        "assert (d['override_warn'],d['inherited_crit'])==(92,95), d" "$J"
  check "metrics the override does not mention are untouched" \
        "assert d['other_metric']==80, d" "$J"
  check "a host with no entry gets the global thresholds" \
        "assert d['unlisted_host']==85, d" "$J"
  # The point of the whole thing: 90% memory is a warning everywhere except the
  # host told to expect it.
  check "90% memory warns on a default host and not on the overridden one" \
        "assert (d['hermes_at_90'],d['netbsd_at_90'])==('warning','ok'), d" "$J"
  # Laptops leave the LAN; a fixed 180s marks them stale for being asleep.
  check "stale_after_seconds is overridable per host" \
        "assert (d['stale_override'],d['stale_default'])==(900,180), d" "$J"
  check "a 10-minute-old sample is stale by default but not for the laptop" \
        "assert d['hermes_600s'] is True and d['argon_600s'] is False, d" "$J"
  # A card being an unexpected colour should be explainable from the API.
  check "the effective thresholds are reported per host" \
        "assert d['reported']=={'warn':92,'crit':95}, d" "$J"
fi

if want cpu-sustain; then
  echo "cpu-sustain (a spike is normal operation; only held load changes colour)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,time; sys.path.insert(0,f"{sys.argv[1]}/server")
import app, db
app.CFG={"thresholds":{"cpu":{"warn":80,"crit":95},
                       "mem":{"warn":85,"crit":95},
                       "disk":{"warn":85,"crit":95}},
         "stale_after_seconds":180,"patch_stale_hours":48,
         "eol":{"enabled":False},"reachability":{"enabled":False},
         "hosts":{"legacy":{"thresholds":{"cpu":{"sustain_seconds":0}}}}}
app.CONN=db.connect(":memory:")
now=int(time.time())
def host(name, readings):
    """readings: newest last, one per minute, ending now."""
    for i,c in enumerate(readings):
        db.insert_sample(app.CONN, {"host":name,"ts":now-60*(len(readings)-1-i),
                                    "os":"x","cpu_pct":c,"disks":[]})
    s=[x for x in db.latest_per_host(app.CONN) if x["host"]==name][0]
    v=app.summarize(s, now)
    return [v["cpu"]["status"], v["cpu"]["brief"], v["status"]]
print(json.dumps({
  "spike_crit":   host("spike",   [5,5,5,5,5,5,99]),
  "spike_warn":   host("warm",    [5,5,5,5,5,5,85]),
  "held_crit":    host("held",    [99,98,97,99,99,98,99]),
  "held_warn":    host("busy",    [85,99,90,99,86,97,99]),
  "dip_in_window":host("dip",     [99,99,99,40,99,99,99]),
  # Six minutes of history is enough; four is not, however high.
  "too_new":      host("new",     [99,99,99,99,99]),
  "quiet":        host("quiet",   [3,4,5]),
  "legacy":       host("legacy",  [5,5,5,5,5,5,99]),
}))
PY
) || J=''
  # The complaint this exists for: one sample over crit turned a card red.
  check "a single reading over crit leaves the host ok" \
        "assert d['spike_crit']==['ok',True,'ok'], d" "$J"
  check "and a single reading over warn does not turn it amber either" \
        "assert d['spike_warn']==['ok',True,'ok'], d" "$J"
  check "crit held for the whole window is critical" \
        "assert d['held_crit']==['critical',False,'critical'], d" "$J"
  # Each level needs its own line held, so a host swinging between 85 and 99
  # has held warn, not crit.
  check "readings all over warn but not all over crit is a warning" \
        "assert d['held_warn']==['warning',True,'warning'], d" "$J"
  check "one dip inside the window means it did not hold" \
        "assert d['dip_in_window'][0]=='ok', d" "$J"
  # A host that has not been reporting for the window has nothing that held.
  check "a host newer than the window cannot be held" \
        "assert d['too_new'][0]=='ok', d" "$J"
  check "a quiet host is simply ok" \
        "assert d['quiet']==['ok',False,'ok'], d" "$J"
  check "sustain_seconds 0 restores one-reading behaviour, per host" \
        "assert d['legacy']==['critical',False,'critical'], d" "$J"
fi

if want virt; then
  echo "virt (unknown must not read as bare metal)"
  J=$(python3 - "$ROOT" <<'PY'
import re,sys,json,subprocess,time; sys.path.insert(0,f"{sys.argv[1]}/server")
root=sys.argv[1]
import app
app.CFG={"thresholds":{"cpu":{"warn":80,"crit":95},"mem":{"warn":85,"crit":95},
                       "disk":{"warn":85,"crit":95}},
         "stale_after_seconds":180,"patch_stale_hours":48,
         "eol":{"enabled":False},"hosts":{}}
now=time.time()
def _sum(host, virt):
    row={"host":host,"ts":now,"os":"x","cpu_pct":None,"mem_used_bytes":None,
         "mem_total_bytes":None,"uptime_seconds":1,"disks":[],"virt":virt,
         "patch_security":None,"patch_other":None,"patch_checked_at":None,
         "patch_source":None,"patch_detail":None,"patch_reboot":None,
         "patch_packages":None,"collector_version":None}
    return app.summarize(row, now)
def _with(cfgvirt, reported):
    app.CFG["hosts"] = {"h": {"virt": cfgvirt}} if cfgvirt else {}
    return _sum("h", reported)
def ov(c, r):  return _with(c, r)["virt"]
def ovs(c, r): return _with(c, r)["virt_source"]
def ovm(c, r): return _with(c, r)["is_vm"]

def s(virt):
    app.CFG["hosts"]={}
    row={"host":"h","ts":now,"os":"x","cpu_pct":None,"mem_used_bytes":None,
         "mem_total_bytes":None,"uptime_seconds":1,"disks":[],"virt":virt,
         "patch_security":None,"patch_other":None,"patch_checked_at":None,
         "patch_source":None,"patch_detail":None,"patch_reboot":None,
         "patch_packages":None,"collector_version":None}
    return app.summarize(row, now)["is_vm"]

# The vendor mapping is a shell case statement; run the shipped one.
src=open(f"{root}/collectors/bsd/netdash-collector.sh").read()
fn=re.search(r"(virt_name\(\) \{.*?\n\})", src, re.S).group(1)
def name(v):
    return subprocess.run(["sh","-c",fn+'\nvirt_name "$1"',"sh",v],
                          capture_output=True,text=True).stdout.strip()
print(json.dumps({
  "none": s("none"), "bhyve": s("bhyve"), "missing": s(None), "empty": s(""),
  "map_bhyve":  name("FreeBSD BHYVE"),
  "map_qemu":   name("QEMU Standard PC"),
  "map_vmware": name("VMware, Inc. VMware Virtual Platform"),
  "map_real":   name("Dell Inc. PowerEdge R640"),
  # A host that cannot report -- TrueNAS runs no collector -- answered from
  # config instead, and the source says so.
  "cfg_virt":   ov("none", None), "cfg_src": ovs("none", None),
  "cfg_is_vm":  ovm("none", None),
  # An override also wins over a reading, which is how a wrong DMI heuristic
  # gets corrected without touching the collector.
  "cfg_beats":  ov("none", "kvm"), "cfg_beats_src": ovs("none", "kvm"),
  # With no override the host's own answer stands, and is labelled as its own.
  "host_wins":  ov(None, "bhyve"), "host_src": ovs(None, "bhyve"),
}))
PY
) || J=''
  # Tri-state on purpose: a host that cannot answer is not a bare-metal host.
  # Collapsing null to false would file every pre-0.4.0 collector under "bare
  # metal", which is the one answer nobody checked.
  check "is_vm is true, false or null -- never a guess" \
        "assert (d['none'],d['bhyve'],d['missing'],d['empty'])==(False,True,None,None), d" "$J"
  check "known hypervisor vendors are named from their DMI strings" \
        "assert (d['map_bhyve'],d['map_qemu'],d['map_vmware'])==('bhyve','kvm','vmware'), d" "$J"
  # Real hardware matches nothing, and the caller turns that into "none" only
  # because DMI was readable at all.
  check "a real hardware vendor matches no hypervisor" \
        "assert d['map_real']=='', d" "$J"
  # TrueNAS is polled over its API and runs no collector, so config is the only
  # place its answer can come from.
  check "a config entry answers for a host that cannot report" \
        "assert (d['cfg_virt'],d['cfg_src'],d['cfg_is_vm'])==('none','config',False), d" "$J"
  check "an override beats the collector, and says so" \
        "assert (d['cfg_beats'],d['cfg_beats_src'])==('none','config'), d" "$J"
  check "without an override the host's own answer stands" \
        "assert (d['host_wins'],d['host_src'])==('bhyve','host'), d" "$J"
fi

if want schedule; then
  echo "schedule (the patch check runs at boot as well as daily)"
  # The state file is written by the patch check and by nothing else, so
  # between a reboot and the next daily run the dashboard describes the machine
  # as it was before it went down. reboot_required is the loudest case: it is
  # read from /var/run/reboot-required, which a reboot clears, so a patched and
  # rebooted host kept asking to be rebooted for up to a day.
  J=$(python3 - "$ROOT" <<'PY'
import re,sys,json
root=sys.argv[1]
src=open(f"{root}/collectors/install.sh").read()

def unit(name):
    m=re.search(r"cat > /etc/systemd/system/%s <<EOF\n(.*?)\nEOF" % re.escape(name), src, re.S)
    return m.group(1) if m else ""

pt=unit("netdash-patchcheck.timer")
ps=unit("netdash-patchcheck.service")
ct=unit("netdash-collector.timer")
cron=re.search(r"schedule_cron\(\) \{(.*?)\n\}", src, re.S).group(1)

print(json.dumps({
  "patch_timer": pt,
  "patch_boot":  bool(re.search(r"^OnBootSec=", pt, re.M)),
  "patch_daily": bool(re.search(r"^OnCalendar=", pt, re.M)),
  # A randomised delay applies to every elapse point including the boot one,
  # so it would put the reboot verdict up to an hour behind the reboot.
  "patch_randomised": bool(re.search(r"^RandomizedDelaySec=", pt, re.M)),
  "patch_persistent": bool(re.search(r"^Persistent=true", pt, re.M)),
  "svc_wants_network": bool(re.search(r"^Wants=network-online.target", ps, re.M)),
  "svc_after_network": bool(re.search(r"^After=network-online.target", ps, re.M)),
  # The collector's own schedule must be untouched by any of this.
  "collector_interval": bool(re.search(r"^OnUnitActiveSec=", ct, re.M)),
  "cron_reboot": bool(re.search(r'RCRON_LINE="@reboot', cron)),
  "cron_daily":  bool(re.search(r'PCRON_LINE="\$PMIN 3 \* \* \*', cron)),
  # A cron without @reboot must lose the boot run, not the whole crontab.
  "cron_fallback": "noreboot" in cron,
  # The filter that clears an older install has to catch the @reboot line too,
  # or re-running the installer stacks up a second one every time.
  "cron_filters_old": cron.count("grep -v 'netdash-patchcheck'") == 1,
}))
PY
) || J=''
  check "the patch check timer fires at boot" \
        "assert d['patch_boot'] is True, d['patch_timer']" "$J"
  check "and still on its daily schedule" \
        "assert d['patch_daily'] is True and d['patch_persistent'] is True, d['patch_timer']" "$J"
  # Spreading the fleet moved into the calendar minute for this reason.
  check "with no randomised delay, which would also delay the boot run" \
        "assert d['patch_randomised'] is False, d['patch_timer']" "$J"
  # After= alone orders nothing unless something pulls the target in, and a
  # refresh that runs before the network is up keeps the previous result --
  # including the stale reboot flag the boot run exists to clear.
  check "the check is ordered after the network, and pulls it in" \
        "assert d['svc_wants_network'] is True and d['svc_after_network'] is True, d" "$J"
  check "the collector's own cadence is left alone" \
        "assert d['collector_interval'] is True, d" "$J"
  check "cron hosts get an @reboot line beside the daily one" \
        "assert d['cron_reboot'] is True and d['cron_daily'] is True, d" "$J"
  # BusyBox and the BSDs all have @reboot; one that does not would reject the
  # whole file and take the collector's schedule down with it.
  check "and a cron that rejects @reboot keeps the rest of the schedule" \
        "assert d['cron_fallback'] is True, d" "$J"
  check "re-running the installer replaces the boot line rather than stacking it" \
        "assert d['cron_filters_old'] is True, d" "$J"  # The text assertions above say the line is written; these run the real
  # function against a stub crontab and say what ends up scheduled.
  CRONDIR=$(mktemp -d)
  mkdir -p "$CRONDIR/bin"
  cat > "$CRONDIR/bin/crontab" <<'SH'
#!/bin/sh
if [ "$1" = "-l" ]; then cat "$CRONSTORE" 2>/dev/null || exit 1; exit 0; fi
if [ -n "$STRICT" ] && grep -q '^@reboot' "$1"; then
  echo "crontab: bad minute" >&2; exit 1
fi
cp "$1" "$CRONSTORE"
SH
  chmod +x "$CRONDIR/bin/crontab"
  sed -n '/^schedule_cron() {/,/^}/p' "$ROOT/collectors/install.sh" > "$CRONDIR/fn.sh"

  run_cron() { # store, strict -> prints the resulting crontab
    CRONSTORE="$1" STRICT="${2:-}" PATH="$CRONDIR/bin:/usr/bin:/bin" \
      BIN=/usr/local/bin/netdash-collector PBIN=/usr/local/bin/netdash-patchcheck \
      PMIN=17 sh -c ". $CRONDIR/fn.sh; schedule_cron" >/dev/null 2>&1
    cat "$1" 2>/dev/null
  }

  OUT=$(run_cron "$CRONDIR/a")
  J=$(printf '%s' "$OUT" | python3 -c "
import sys,json
lines=[l for l in sys.stdin.read().splitlines() if l.strip()]
print(json.dumps({'lines':lines}))
")
  check "a fresh cron install schedules collector, daily check and @reboot" \
        "assert len(d['lines'])==3 and d['lines'][2].startswith('@reboot') and '/netdash-patchcheck' in d['lines'][2], d" "$J"

  # Running the installer again must replace those lines, not stack a second
  # @reboot onto every upgrade.
  OUT=$(run_cron "$CRONDIR/a")
  J=$(printf '%s' "$OUT" | python3 -c "
import sys,json
lines=[l for l in sys.stdin.read().splitlines() if l.strip()]
print(json.dumps({'lines':lines,'reboots':sum(1 for l in lines if l.startswith('@reboot'))}))
")
  check "and re-running it leaves exactly one of each, not two" \
        "assert len(d['lines'])==3 and d['reboots']==1, d" "$J"

  # The failure that would matter: a cron that rejects @reboot must cost the
  # boot run only. Losing the whole file would stop the collector reporting.
  OUT=$(run_cron "$CRONDIR/b" strict)
  J=$(printf '%s' "$OUT" | python3 -c "
import sys,json
lines=[l for l in sys.stdin.read().splitlines() if l.strip()]
print(json.dumps({'lines':lines}))
")
  check "a cron that rejects @reboot still gets its collector and daily check" \
        "assert len(d['lines'])==2 and d['lines'][0].startswith('* * * * *') and '/netdash-collector' in d['lines'][0], d" "$J"
  rm -rf "$CRONDIR"
fi

if want concurrency; then
  echo "concurrency (one connection, many threads, no SQLITE_MISUSE)"
  # The reachability prober, the event watcher, the roller, the pruner, three
  # appliance pollers and every HTTP handler share one sqlite3 connection.
  # A connection is not safe to use from two threads at once, and unguarded
  # reads raised "bad parameter or other API misuse" out of the prober -- which
  # caught it, skipped the probing, and left the hosts that had stopped
  # reporting sitting at "unknown". Exactly the ones it exists to judge.
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,os,time,threading,tempfile; sys.path.insert(0,f"{sys.argv[1]}/server")
import db

conn = db.connect(os.path.join(tempfile.mkdtemp(), "race.db"))
now = int(time.time())
HOSTS = ["h%02d" % i for i in range(12)]
for h in HOSTS:
    db.insert_sample(conn, {"host":h,"ts":now,"os":"x","cpu_pct":1.0,
        "disks":[{"mount":"/","used_bytes":1,"total_bytes":2}],
        "patches":{"security":3,"other":1,"checked_at":now,"source":"apt",
                   "packages":"a, b, c"}})

errors = []
stop = threading.Event()

def guard(fn):
    def run():
        while not stop.is_set():
            try:
                fn()
            except Exception as e:
                errors.append("%s: %s" % (type(e).__name__, e))
                return
    return run

def reader():
    for s in db.latest_per_host(conn):
        db.patch_pending(conn, s["host"])
        db.get_patch_acks(conn, s["host"])

def writer():
    h = HOSTS[int(time.time() * 1000) % len(HOSTS)]
    db.insert_sample(conn, {"host":h,"ts":int(time.time()),"os":"x","cpu_pct":2.0,
        "disks":[{"mount":"/","used_bytes":1,"total_bytes":2}],
        "patches":{"security":3,"other":1,"checked_at":int(time.time()),
                   "source":"apt","packages":"a, b, c"}})

def events():
    db.recent_events(conn, limit=20); db.last_states(conn); db.known_hosts(conn)

threads = ([threading.Thread(target=guard(reader)) for _ in range(4)] +
           [threading.Thread(target=guard(writer)) for _ in range(2)] +
           [threading.Thread(target=guard(events)) for _ in range(2)])
for t in threads: t.start()
time.sleep(3.0)
stop.set()
for t in threads: t.join(10)

# The write path still has to have worked, not merely not crashed.
rows = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
print(json.dumps({"errors": errors[:5], "error_count": len(errors), "rows": rows}))
PY
) || J=''
  check "hammering one connection from eight threads raises nothing" \
        "assert d['error_count']==0, d['errors']" "$J"
  check "and the writes actually landed rather than merely not crashing" \
        "assert d['rows'] > 12, d" "$J"
fi

if want patches-preboot; then
  echo "patches-preboot (a check older than the boot describes a machine that is gone)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,time; sys.path.insert(0,f"{sys.argv[1]}/server")
import app, db
app.CONN = None
app.CFG = {"thresholds":{"cpu":{"warn":80,"crit":95},"mem":{"warn":85,"crit":95},
                         "disk":{"warn":85,"crit":95}},
           "stale_after_seconds":180,"patch_stale_hours":48,
           "eol":{"enabled":False},"hosts":{}}
now = time.time()

def pc(uptime, checked_ago, sec=6, reboot=True):
    return app.patch_summary({
        "host":"h","ts":int(now),"uptime_seconds":uptime,
        "patch_security":sec,"patch_other":0,
        "patch_checked_at":int(now - checked_ago),
        "patch_source":"apt","patch_detail":None,"patch_reboot":reboot,
        "patch_packages":None}, now)

H = 3600
# The live case: up 2h, checked 12h ago -- ten hours before the reboot.
rebooted   = pc(2*H, 12*H)
# The ordinary case: up 25h, checked 12h ago, well after the boot.
steady     = pc(25*H, 12*H)
# Boundary: checked one second after the boot still counts as after it.
just_after = pc(2*H, 2*H - 1)
just_befor = pc(2*H, 2*H + 1)
# A host that reports no uptime cannot be judged this way, and must not be
# demoted on a guess.
no_uptime  = pc(None, 12*H)
# Already stale on the clock -- the older rule still wins, and reads the same.
ancient    = pc(2000*H, 100*H)
# rhenium: up three days on a check twenty days old. Stale on the clock AND
# from before the boot -- stale is the reason given.
stale_pre  = pc(72*H, 480*H)

print(json.dumps({
  "rebooted":        [rebooted["status"], rebooted["predates_boot"], rebooted["reboot_required"]],
  "steady":          [steady["status"], steady["predates_boot"], steady["reboot_required"]],
  "just_after":      [just_after["status"], just_after["predates_boot"]],
  "just_before":     [just_befor["status"], just_befor["predates_boot"]],
  "no_uptime":       [no_uptime["status"], no_uptime["predates_boot"]],
  "ancient":         [ancient["status"], ancient["predates_boot"]],
  "stale_preboot":   [stale_pre["status"], stale_pre["predates_boot"], stale_pre["reboot_required"]],
  "rebooted_entries": rebooted["entries"],
}))
PY
) || J=''
  # The live symptom: up two hours, still asking to be rebooted on the strength
  # of a check that ran ten hours before it went down.
  check "a check from before the last boot reads unknown, not its old verdict" \
        "assert d['rebooted'][0]=='unknown' and d['rebooted'][1] is True, d" "$J"
  # /var/run/reboot-required is tmpfs: the reboot that satisfied it emptied it,
  # so a pending reboot dated before that boot is known to be wrong, not merely old.
  check "and its reboot flag is dropped rather than repeated" \
        "assert d['rebooted'][2] is None, d" "$J"
  check "while a check from after the boot is left entirely alone" \
        "assert d['steady']==['security', False, True], d" "$J"
  check "the boundary is the boot moment itself, not a fudge around it" \
        "assert d['just_after'][1] is False and d['just_before'][1] is True, d" "$J"
  # A host reporting no uptime gives us nothing to compare against.
  check "a host that reports no uptime is not demoted on a guess" \
        "assert d['no_uptime']==['security', False], d" "$J"
  check "and a check already stale on the clock still reads unknown" \
        "assert d['ancient'][0]=='unknown', d" "$J"
  # rhenium: checked 20 days ago, rebooted 3 days ago. The cause is the patch
  # check job that stopped running, not the reboot, so the card says stale.
  check "a stale check that also predates the boot is reported as stale" \
        "assert d['stale_preboot'][:2]==['unknown', False], d" "$J"
  check "while still dropping its reboot flag" \
        "assert d['stale_preboot'][2] is None, d" "$J"
  # Nothing is offered for acknowledgement on a reading we have just disowned.
  check "nothing on a disowned reading is offered for acknowledgement" \
        "assert d['rebooted_entries']==[], d" "$J"
fi

if want patch-pending; then
  echo "patch-pending (the package list lives at the rate it changes)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,os,time,sqlite3,tempfile; sys.path.insert(0,f"{sys.argv[1]}/server")
import app, db

path = os.path.join(tempfile.mkdtemp(), "nd.db")
conn = db.connect(path)
now = int(time.time())
HOST = "cerium"
SIX = "a, b, c, d, e, f"

def push(pkgs, sec=6, checked=True, host=HOST):
    p = {"security":sec,"other":0,"source":"pkg-audit"}
    if checked: p["checked_at"] = now
    if pkgs is not None: p["packages"] = pkgs
    db.insert_sample(conn, {"host":host,"ts":now,"os":"x","patches":p})

push(SIX)
stored = db.patch_pending(conn, HOST)
# The names must not also be riding on the sample any more: that column is the
# 4 MB this moved, written 2,880 times a day to be read once.
on_sample = conn.execute(
    "SELECT patch_packages FROM samples WHERE host=?", (HOST,)).fetchone()[0]

# The same list arrives on every sample all day while the check behind it runs
# daily. Rewriting it each time would be 2,880 pointless writes a host.
rowids = [r[0] for r in conn.execute("SELECT rowid FROM patch_pending WHERE host=?", (HOST,))]
push(SIX); push(SIX)
rowids_after = [r[0] for r in conn.execute("SELECT rowid FROM patch_pending WHERE host=?", (HOST,))]

# A real change does land.
push("a, b, zzz", sec=3)
changed = db.patch_pending(conn, HOST)

# A collector whose patchcheck has never run, or has stopped reporting, has
# told us nothing about what is pending -- and wiping the list on that silence
# would drop every ack with it the moment the check came back.
db.ack_patch(conn, HOST, ["a"], now)
push(None, checked=False)
after_silence = db.patch_pending(conn, HOST)
acks_after_silence = sorted(db.get_patch_acks(conn, HOST))

# Forgetting a host takes its pending list with it, or the next machine to
# reuse the name inherits a list nobody checked.
push(SIX, host="gone")
db.forget_host(conn, "gone")
after_forget = db.patch_pending(conn, "gone")

# Upgrading a live box: the names are in the old column and nowhere else. Left
# unseeded, every host would read as nothing-pending until its next daily
# check, and prune() would take that as licence to drop all its acks.
old = os.path.join(tempfile.mkdtemp(), "old.db")
c = sqlite3.connect(old); c.executescript(db.SCHEMA)
c.execute("INSERT INTO samples (host, ts, patch_security, patch_checked_at, patch_packages) VALUES (?,?,?,?,?)",
          ("oldhost", now, 6, now, SIX))
c.commit(); c.close()
migrated = db.patch_pending(db.connect(old), "oldhost")

print(json.dumps({
  "stored": stored, "on_sample": on_sample,
  "not_rewritten": rowids == rowids_after and len(rowids) == 6,
  "changed": changed,
  "after_silence": after_silence, "acks_after_silence": acks_after_silence,
  "after_forget": after_forget,
  "migrated": migrated,
}))
PY
) || J=''
  check "the names a check reported are stored once, in report order" \
        "assert d['stored']==['a','b','c','d','e','f'], d" "$J"
  check "and no longer ride along on every sample row" \
        "assert d['on_sample'] is None, d" "$J"
  check "an unchanged list is not rewritten on every sample" \
        "assert d['not_rewritten'] is True, d" "$J"
  check "while a list that actually moved is" \
        "assert d['changed']==['a','b','zzz'], d" "$J"
  check "a sample carrying no check does not erase what is pending" \
        "assert d['after_silence']==['a','b','zzz'], d" "$J"
  check "nor the acknowledgements made against it" \
        "assert d['acks_after_silence']==['a'], d" "$J"
  check "forgetting a host takes its pending list with it" \
        "assert d['after_forget']==[], d" "$J"
  # Otherwise an upgrade reads as nothing-pending everywhere for up to a day,
  # and the pruner clears every ack on the fleet on the strength of it.
  check "an upgrade seeds the list from the column it used to live in" \
        "assert d['migrated']==['a','b','c','d','e','f'], d" "$J"
fi

if want patch-ack; then
  echo "patch-ack (acknowledging silences one package, not the whole list)"
  J=$(python3 - "$ROOT" <<'PY'
import sys,json,os,time,sqlite3,tempfile; sys.path.insert(0,f"{sys.argv[1]}/server")
import app, db

app.CONN = db.connect(":memory:")
app.CFG = {"thresholds":{"cpu":{"warn":80,"crit":95},"mem":{"warn":85,"crit":95},
                         "disk":{"warn":85,"crit":95}},
           "stale_after_seconds":180,"patch_stale_hours":48,
           "eol":{"enabled":False},"hosts":{}}
now = time.time()
HOST = "freebsd15dot1package"
PY312, GIF, SETUP = "python312-3.12.14", "giflib-6.1.3", "py312-setuptools-63.1.0_3"
THREE = ", ".join([PY312, GIF, SETUP])

def sample(sec, pkgs, host=HOST):
    return {"host":host,"ts":now,"os":"x","cpu_pct":None,"mem_used_bytes":None,
            "mem_total_bytes":None,"uptime_seconds":1,
            "patch_security":sec,"patch_other":0,"patch_checked_at":now,
            "patch_source":"pkg-audit-pkgbase","patch_detail":None,"patch_reboot":False,
            "patch_packages":pkgs,"collector_version":None,"disks":[]}

def patches(sec, pkgs, host=HOST):
    # What insert_sample does with the reported string, without the sample:
    # the pending list is per host now, not a column on every row.
    with db._DB:
        db._set_patch_pending(app.CONN, host, pkgs)
        app.CONN.commit()
    return app.summarize(sample(sec, pkgs, host), now)["patches"]

def acked(p):
    return sorted(i["package"] for i in p["entries"] if i["acknowledged"])

before = patches(3, THREE)
db.ack_patch(app.CONN, HOST, [PY312], int(now))
one = patches(3, THREE)
# The whole point. python312 carries a pkg audit hit with no upstream fix, and
# the rest of the list churns around it -- under the old whole-state ack that
# lapsed python312 too, and it had to be acknowledged again over a decision
# nothing had changed about.
churned = patches(3, ", ".join([PY312, GIF, "curl-8.9.0"]))
db.ack_patch(app.CONN, HOST, [GIF, SETUP], int(now))
all_acked = patches(3, THREE)
# A newly vulnerable package still speaks up, and disturbs nothing already
# reviewed...
newcomer = patches(4, THREE + ", curl-8.9.0")
# ...and once it is fixed the host goes quiet again with nothing re-acknowledged.
fixed_again = patches(3, THREE)

db.unack_patch(app.CONN, HOST, GIF)
after_one_unack = patches(3, THREE)
db.unack_patch(app.CONN, HOST)
after_unack = patches(3, THREE)

# The six-name cap this replaces made everything past the sixth one anonymous
# group, keyed on how many there were. Which packages fell into it was
# positional -- no check sorts its output -- so the count could hold still
# while the membership changed underneath, and an ack made about one set of
# packages silently covered another. Nine names, all of them ackable.
BIG = "bighost"
NINE = ", ".join("abcdefghi")
big_before = patches(9, NINE, BIG)
db.ack_patch(app.CONN, BIG, [i["package"] for i in big_before["entries"]], int(now))
big_acked = patches(9, NINE, BIG)
# One package swapped for a different one, count unchanged. Under the group ack
# this host stayed silent; the newcomer was never reviewed by anybody.
big_swapped = patches(9, ", ".join("abcdefgh") + ", zzz", BIG)

# A collector upgrades on its own schedule, so the capped form keeps arriving
# for a while yet. Its "(+3 more)" tail is a count, not a package, and must not
# become an ack target again on the way in.
LEG = "legacyhost"
legacy = patches(9, "a, b, c, d, e, f (+3 more)", LEG)
# Six names against a count of nine: three pending items nobody can review.
# Acknowledging the six must not read as "this host has been reviewed" -- that
# is the blind ack coming back through the side door.
db.ack_patch(app.CONN, LEG, [i["package"] for i in legacy["entries"]], int(now))
legacy_all_acked = patches(9, "a, b, c, d, e, f (+3 more)", LEG)
# openSUSE Leap's patch-check: a count and no names at all, ever.
leap = patches(2, None, "leaphost")

# A check old enough to read as unknown is not a security state, so there is
# nothing to acknowledge and nothing unnamed to complain about -- but the names
# it last reported are still worth showing on the card.
STALE = "stalehost"
patches(3, THREE, STALE)
stale_sample = sample(3, THREE, STALE)
stale_sample["patch_checked_at"] = now - 90 * 3600
stale = app.summarize(stale_sample, now)["patches"]

# A FreeBSD PORTEPOCH is part of the package name, not a separator. Splitting
# on a bare comma broke gimp-2.10.38,2 on a live host into a package and a
# stray "2", each with its own acknowledge link and neither meaning anything.
EPOCH = "chromium-151.0.7922.169, python312-3.12.14, gimp-2.10.38,2"
epoch = patches(3, EPOCH, "freebsd15dot0package")

# An ack for a package that is no longer pending is not silencing anything, it
# is a landmine: left behind, it would silence that package's *next* advisory
# months from now on nobody's decision.
db.ack_patch(app.CONN, HOST, [PY312, GIF], int(now))
db.insert_sample(app.CONN, {"host":HOST,"ts":now,"os":"x",
                            "patches":{"security":1,"other":0,"checked_at":now,
                                       "source":"pkg-audit-pkgbase","packages":PY312}})
db.prune(app.CONN, 24)
after_prune = sorted(db.get_patch_acks(app.CONN, HOST))

# Upgrading a live box must not turn every acknowledged host red: the old
# whole-state rows carry over, split into the packages they were made about.
path = os.path.join(tempfile.mkdtemp(), "old.db")
c = sqlite3.connect(path)
c.execute("""CREATE TABLE patch_acks (host TEXT PRIMARY KEY, security INTEGER NOT NULL,
                                      packages TEXT NOT NULL, acked_at INTEGER NOT NULL)""")
c.execute("INSERT INTO patch_acks VALUES (?,?,?,?)", (HOST, 3, THREE, 1000))
c.commit(); c.close()
migrated = sorted(db.get_patch_acks(db.connect(path), HOST))

print(json.dumps({
  "before_status":      before["status"],
  "before_acked":       before["acknowledged"],
  "one_acked":          acked(one),
  "one_whole_host":     one["acknowledged"],
  "one_count":          one["acked_count"],
  "churned_acked":      acked(churned),
  "all_acked":          all_acked["acknowledged"],
  "all_acked_at":       all_acked["acked_at"] == int(now),
  "newcomer_acked":     newcomer["acknowledged"],
  "newcomer_kept":      acked(newcomer),
  "fixed_again_acked":  fixed_again["acknowledged"],
  "one_unack_whole":    after_one_unack["acknowledged"],
  "one_unack_kept":     acked(after_one_unack),
  "after_unack_acked":  acked(after_unack),
  "big_entries":        [i["package"] for i in big_before["entries"]],
  "big_acked":          big_acked["acknowledged"],
  "big_swapped_acked":  big_swapped["acknowledged"],
  "big_swapped_new":    [i["package"] for i in big_swapped["entries"]
                         if not i["acknowledged"]],
  "legacy_entries":     [i["package"] for i in legacy["entries"]],
  "legacy_unnamed":     legacy["unnamed_count"],
  "legacy_all_acked":   legacy_all_acked["acknowledged"],
  "legacy_acked_count": legacy_all_acked["acked_count"],
  "leap_entries":       [i["package"] for i in leap["entries"]],
  "leap_unnamed":       leap["unnamed_count"],
  "leap_acked":         leap["acknowledged"],
  "stale_status":       stale["status"],
  "stale_entries":      [i["package"] for i in stale["entries"]],
  "stale_unnamed":      stale["unnamed_count"],
  "stale_packages":     stale["packages"],
  "epoch_entries":      [i["package"] for i in epoch["entries"]],
  "after_prune":        after_prune,
  "migrated":           migrated,
}))
PY
) || J=''
  check "an unacknowledged security state reads security, not acknowledged" \
        "assert d['before_status']=='security' and d['before_acked'] is False, d" "$J"
  check "acknowledging one package silences that package and nothing else" \
        "assert d['one_acked']==['python312-3.12.14'] and d['one_count']==1, d" "$J"
  # One unreviewed package is still a reason to look, however much of the list
  # has been dealt with.
  check "and leaves the host itself unsilenced while others are pending" \
        "assert d['one_whole_host'] is False, d" "$J"
  # The complaint this replaced the whole-state ack over.
  check "the rest of the list changing does not disturb that ack" \
        "assert d['churned_acked']==['python312-3.12.14'], d" "$J"
  check "acknowledging every pending package silences the host" \
        "assert d['all_acked'] is True and d['all_acked_at'] is True, d" "$J"
  check "a newly vulnerable package un-silences the host on its own" \
        "assert d['newcomer_acked'] is False, d" "$J"
  check "without costing the acks already made" \
        "assert d['newcomer_kept']==['giflib-6.1.3','py312-setuptools-63.1.0_3','python312-3.12.14'], d" "$J"
  check "and when it is fixed the host falls quiet with nothing re-acknowledged" \
        "assert d['fixed_again_acked'] is True, d" "$J"
  check "un-acknowledging one package brings back only that one" \
        "assert d['one_unack_whole'] is False and d['one_unack_kept']==['py312-setuptools-63.1.0_3','python312-3.12.14'], d" "$J"
  check "un-acknowledging the host clears the lot" \
        "assert d['after_unack_acked']==[], d" "$J"
  # An ack is a person saying they reviewed a specific thing, so there has to
  # be a specific thing. Nothing is summarised into a group you cannot inspect.
  check "every pending package is named, however many there are" \
        "assert d['big_entries']==list('abcdefghi'), d" "$J"
  check "acknowledging all nine silences the host" \
        "assert d['big_acked'] is True, d" "$J"
  # The group ack was keyed on the count alone, and membership was positional,
  # so this swap kept the host silent about a package nobody had ever seen.
  check "a package swapped in at an unchanged count is not silently covered" \
        "assert d['big_swapped_acked'] is False and d['big_swapped_new']==['zzz'], d" "$J"
  # A collector still on the old build keeps sending "(+3 more)". It is a
  # count, not a package, and it is not something anyone can acknowledge.
  check "a not-yet-upgraded collector's (+N more) tail never becomes an entry" \
        "assert d['legacy_entries']==list('abcdef') and d['legacy_unnamed']==3, d" "$J"
  # The blind ack coming back through the side door: six of nine ticked is not
  # a reviewed host, and a card that went quiet here would be the original bug.
  check "acknowledging every *named* package still leaves the host loud" \
        "assert d['legacy_all_acked'] is False and d['legacy_acked_count']==6, d" "$J"
  # openSUSE Leap counts security patches and can name none of them. There is
  # nothing to key an ack on, so it stays loud rather than offering a group.
  check "a check that names nothing at all offers nothing to acknowledge" \
        "assert d['leap_entries']==[] and d['leap_unnamed']==2 and d['leap_acked'] is False, d" "$J"
  # A stale check is "unknown", never a security state -- so it offers no acks,
  # and must not claim packages went unnamed when it simply went unread.
  check "a stale check still shows what it last named, and complains of nothing" \
        "assert d['stale_status']=='unknown' and d['stale_entries']==[] and d['stale_unnamed']==0, d" "$J"
  check "and keeps those names on the card" \
        "assert d['stale_packages']==', '.join(['python312-3.12.14','giflib-6.1.3','py312-setuptools-63.1.0_3']), d" "$J"
  # Otherwise a package fixed in March comes back vulnerable in July already
  # silenced, by a decision made about a different advisory.
  check "pruning drops acks for packages that are no longer pending" \
        "assert d['after_prune']==['python312-3.12.14'], d" "$J"
  check "a package name carrying an epoch stays one package" \
        "assert d['epoch_entries']==['chromium-151.0.7922.169','python312-3.12.14','gimp-2.10.38,2'], d" "$J"
  check "an old whole-host ack upgrades into the packages it was made about" \
        "assert d['migrated']==['giflib-6.1.3','py312-setuptools-63.1.0_3','python312-3.12.14'], d" "$J"
fi

if want reachability; then
  echo "reachability (a locked host must read down, not stale)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json,time; sys.path.insert(0,f"{sys.argv[1]}/server")
import app
app.CFG={"thresholds":{"cpu":{"warn":80,"crit":95},"mem":{"warn":85,"crit":95},
                       "disk":{"warn":85,"crit":95}},
         "stale_after_seconds":180,"patch_stale_hours":48,
         "eol":{"enabled":False},
         "reachability":{"enabled":True,"checks":["icmp"],"failures_before_down":2,
                         "down_after_seconds":900},
         "hosts":{"thelaptop":{"expect_up":False}}}
now=time.time()
def s(host, age, reach=None):
    app.REACH.clear()
    if reach: app.REACH[host]=reach
    row={"host":host,"ts":now-age,"os":"x","cpu_pct":10.0,"mem_used_bytes":10,
         "mem_total_bytes":100,"uptime_seconds":1,"disks":[],"virt":None,
         "patch_security":None,"patch_other":None,"patch_checked_at":None,
         "patch_source":None,"patch_detail":None,"patch_reboot":None,
         "patch_packages":None,"collector_version":None,"peer_addr":"10.0.0.9"}
    return app.summarize(row, now)
DOWN={"state":"down","probe":"down","failures":2,"detail":"no reply in 2s",
      "address":"10.0.0.42","address_source":"config","via":"icmp","checked_at":now}
UP  ={"state":"up","probe":"up","failures":0,"detail":"icmp reply",
      "address":"10.0.0.42","address_source":"config","via":"icmp","checked_at":now}
UNK ={"state":"unknown","probe":"error","failures":0,"detail":"cannot run ping",
      "address":"10.0.0.42","address_source":"config","via":"icmp","checked_at":now}
print(json.dumps({
  "locked":        s("hassium", 600, DOWN)["status"],
  "locked_reason": s("hassium", 600, DOWN)["down_reason"],
  "fresh_nonreply":s("hassium",   5, DOWN)["status"],
  "answering":     s("hassium", 600, UP)["status"],
  "answering_long":s("hassium",4000, UP)["status"],
  "unprobed":      s("hassium", 600)["status"],
  "unprobed_long": s("hassium",4000)["status"],
  "cant_probe":    s("hassium", 600, UNK)["status"],
  "cant_probe_long":s("hassium",4000, UNK)["status"],
  "laptop":        s("thelaptop", 4000, DOWN)["status"],
  "laptop_expect": s("thelaptop", 4000, DOWN)["expect_up"],
  "rank_vs_crit":  app._RANK["down"] > app._RANK["critical"],
  "detail_exposed":s("hassium", 600, DOWN)["reachability"]["detail"],
}))
PYEOF
) || J=''
  # The bug this exists for: a VM whose CPUs stopped scheduling. Push-only
  # monitoring sees the same silence as a broken cron entry and paints both a
  # muted grey, which is the colour you scan past on a wall panel.
  check "a stale host that does not answer reads down, not stale" \
        "assert (d['locked'],d['locked_reason'])==('down','unreachable'), d" "$J"
  # A failed probe against a host that reported seconds ago is far more likely
  # to be our own hiccup than an outage. Staleness has to agree first.
  check "a fresh host is never down, whatever a probe says" \
        "assert d['fresh_nonreply']=='ok', d" "$J"
  # The other half of the distinction, and the reason the probe is worth having
  # at all: box alive, collector dead. Amber, and never red however long it
  # lasts -- shouting DOWN at a machine you are talking to right now is how a
  # dashboard trains you to ignore it.
  check "a host that answers stays stale no matter how long it is silent" \
        "assert (d['answering'],d['answering_long'])==('stale','stale'), d" "$J"
  # The clock is the backstop for when probing cannot reach a verdict -- no
  # route to that subnet, no ping binary, a host that answers nothing.
  check "silence beyond down_after_seconds is down even with no probe verdict" \
        "assert (d['unprobed'],d['unprobed_long'])==('stale','down'), d" "$J"
  check "a probe that errored is treated as no verdict, not as a failure" \
        "assert (d['cant_probe'],d['cant_probe_long'])==('stale','down'), d" "$J"
  # A laptop that is closed for the night is not an outage.
  check "expect_up:false keeps a host out of down entirely" \
        "assert d['laptop']=='stale' and d['laptop_expect'] is False, d" "$J"
  check "down outranks critical in the rollup" \
        "assert d['rank_vs_crit'] is True, d" "$J"
  # A red card has to be explainable from the API, not taken on faith.
  check "the probe detail reaches the API" \
        "assert d['detail_exposed']=='no reply in 2s', d" "$J"
fi

if want lost-host; then
  echo "lost-host (a host that goes down must not go missing)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import json, os, sys, tempfile, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import db, app

conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
now = int(time.time())

# The exact shape of the bug: one host reporting normally, one that stopped
# seventeen days ago. ubuntu22dot04server locked up on a Friday, was red for a
# day, and by Saturday the pruner had deleted its last sample -- which deleted
# the host too, because the fleet list was built out of the samples.
for q in range(40):
    db.insert_sample(conn, {"host": "alive", "ts": now - q * 60, "cpu_pct": 4.0,
                            "mem_used_bytes": 1, "mem_total_bytes": 2,
                            "disks": [{"mount": "/", "used_bytes": 1,
                                       "total_bytes": 2}]}, peer="10.0.0.7")
db.insert_sample(conn, {"host": "ubuntu22dot04server", "ts": now - 17 * 86400,
                        "os": "Ubuntu 22.04.5 LTS", "cpu_pct": 12.0,
                        "mem_used_bytes": 1, "mem_total_bytes": 2,
                        "patches": {"security": 1, "other": 0, "source": "apt",
                                    "checked_at": now - 17 * 86400,
                                    "packages": "openssl"},
                        "disks": [{"mount": "/", "used_bytes": 1,
                                   "total_bytes": 2}]}, peer="10.0.0.42")
db.ack_patch(conn, "ubuntu22dot04server", ["openssl"], now - 17 * 86400)
db.record_event(conn, "ubuntu22dot04server", "status", "ok", "down", "no icmp reply")

db.prune(conn, 24)

app.CONN = conn
app.CFG = {"thresholds": {"cpu": {"warn": 80, "crit": 95}, "mem": {"warn": 85, "crit": 95},
                          "disk": {"warn": 85, "crit": 95}},
           "stale_after_seconds": 180, "patch_stale_hours": 48,
           "eol": {"enabled": False},
           "reachability": {"enabled": True, "checks": ["icmp"],
                            "failures_before_down": 2, "down_after_seconds": 900},
           "hosts": {}}

rows = {s["host"]: s for s in db.latest_per_host(conn)}
lost = rows.get("ubuntu22dot04server")

app.REACH.clear()
by_clock = app.summarize(lost, now) if lost else None
app.REACH["ubuntu22dot04server"] = {
    "state": "down", "probe": "down", "failures": 2, "detail": "no reply in 2s",
    "address": "10.0.0.42", "address_source": "ingest", "via": "icmp",
    "checked_at": now}
view = app.summarize(lost, now) if lost else None
acked_while_down = sorted(db.get_patch_acks(conn, "ubuntu22dot04server"))

forgot = db.forget_host(conn, "ubuntu22dot04server")
again = db.forget_host(conn, "ubuntu22dot04server")
db.prune(conn, 24)

print(json.dumps({
  "hosts": sorted(rows),
  "status": view and view["status"],
  "reason": view and view["down_reason"],
  "no_samples": view and view["no_samples"],
  "clock_status": by_clock and by_clock["status"],
  "clock_reason": by_clock and by_clock["down_reason"],
  "cpu": view and view["cpu"]["pct"],
  "cpu_status": view and view["cpu"]["status"],
  "mounts": view and len(view["disk"]["mounts"]),
  "os": view and view["os"],
  "age_days": view and view["age_seconds"] / 86400.0,
  "peer": lost and lost["peer_addr"],
  "acked_while_down": acked_while_down,
  "alive": rows["alive"]["cpu_pct"] if "alive" in rows else None,
  "forgot": forgot,
  "again": again,
  "after": [s["host"] for s in db.latest_per_host(conn)],
  "after_known": db.known_hosts(conn),
  "acks_after": [r[0] for r in conn.execute("SELECT host FROM patch_acks")],
  "samples_after": conn.execute(
      "SELECT COUNT(*) FROM samples WHERE host='ubuntu22dot04server'").fetchone()[0],
  "events_after": conn.execute(
      "SELECT COUNT(*) FROM events WHERE host='ubuntu22dot04server'").fetchone()[0],
}))
PYEOF
) || J=''
  # The bug itself. A dashboard that drops a host once it has been down for a
  # day is worse than one that never watched it: it shows a complete fleet,
  # all green, with a machine quietly missing from it.
  check "a host whose samples have all aged out is still in the fleet" \
        "assert 'ubuntu22dot04server' in d['hosts'], d['hosts']" "$J"
  check "and reads as DOWN, with the reason the probe gave" \
        "assert (d['status'], d['reason'])==('down','unreachable'), d" "$J"
  # The clock is the backstop here exactly as it is for a merely stale host: a
  # machine absent for seventeen days is down whether or not anything confirmed
  # it, and must not sit amber for ever waiting to be told.
  check "and is down on the clock alone when nothing could probe it" \
        "assert (d['clock_status'], d['clock_reason'])==('down','absent'), d" "$J"
  # Null, not zero. "We have no readings" and "it reported 0%" are different
  # claims and a card that draws the second is lying about the first.
  check "its readings are gone and read as unknown, never as zero" \
        "assert d['cpu'] is None and d['cpu_status']=='unknown' and d['mounts']==0 and d['no_samples'] is True, d" "$J"
  # Everything needed to keep watching it outlives the data: the icon on the
  # card, the age under it, and the address the prober aims at.
  check "what it last told us about itself outlives its samples" \
        "assert d['os']=='Ubuntu 22.04.5 LTS' and d['peer']=='10.0.0.42' and 16.9 < d['age_days'] < 17.1, d" "$J"
  # An ack is a decision somebody made about a package. Being offline is not a
  # reason to throw it away and turn the host red again on its way back up.
  check "an acknowledgement survives the host's data ageing out" \
        "assert d['acked_while_down']==['openssl'], d" "$J"
  check "a host that is reporting normally is untouched by any of it" \
        "assert d['alive']==4.0, d" "$J"
  # Nothing expires a host any more, so something has to end one deliberately.
  check "forgetting a host is what removes it, and it stays removed" \
        "assert d['forgot'] is True and d['again'] is False and d['after']==['alive'] and d['after_known']==['alive'], d" "$J"
  check "and takes its samples and acknowledgements with it" \
        "assert d['samples_after']==0 and d['acks_after']==[], d" "$J"
  # The one thing a forgotten host leaves behind. "It went down on the 4th"
  # stays true after the machine itself is gone.
  check "but not the record that it happened" \
        "assert d['events_after']==1, d" "$J"
fi

if want reach-probe; then
  echo "reach-probe (a probe that cannot run is never evidence of down)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json; sys.path.insert(0,f"{sys.argv[1]}/server")
import reach
# A closed port answers with a RST, and generating that RST is work the host's
# kernel had to schedule -- so a refusal is proof of life, not a failure. This
# is what makes tcp:22 a usable check on hosts with no sshd.
refused = reach.tcp("127.0.0.1", 9, 1)
print(json.dumps({
  "refused":       refused[0],
  "loopback_icmp": reach.icmp("127.0.0.1", 2)[0],
  "blackhole":     reach.probe("192.0.2.99", ["icmp"], 1)[0],
  "blackhole_tcp": reach.probe("192.0.2.99", ["tcp:22"], 1)[0],
  "unresolvable":  reach.probe("no.such.host.invalid", ["icmp"], 1)[0],
  "no_checks":     reach.probe("127.0.0.1", [], 1)[0],
  "bad_check":     reach.probe("127.0.0.1", ["ftp"], 1)[0],
  # One check answering is enough; they are alternative ways of asking, not a
  # checklist a host has to pass.
  "any_success":   reach.probe("127.0.0.1", ["tcp:9"], 1)[0],
  "error_beats_down": reach.probe("192.0.2.99", ["tcp:22","ftp"], 1)[0],
  "parse_tcp":     list(reach.parse_check("tcp:22")),
  "parse_icmp":    list(reach.parse_check("icmp")),
}))
PYEOF
) || J=''
  check "a refused connection counts as up -- the host's kernel answered" \
        "assert d['refused']=='up', d" "$J"
  check "loopback answers icmp" \
        "assert d['loopback_icmp']=='up', d" "$J"
  check "a black-holed address is down by both icmp and tcp" \
        "assert (d['blackhole'],d['blackhole_tcp'])==('down','down'), d" "$J"
  # Every one of these is a fact about this server, not the host being probed.
  # A monitor that paints hosts red because of its own broken plumbing is one
  # you learn to stop believing.
  check "an unresolvable name is an error, never down" \
        "assert d['unresolvable']=='error', d" "$J"
  check "no checks configured is an error, never down" \
        "assert d['no_checks']=='error', d" "$J"
  check "a malformed check is an error, never down" \
        "assert d['bad_check']=='error', d" "$J"
  check "one check answering is enough" \
        "assert d['any_success']=='up', d" "$J"
  check "a broken check outweighs a failed one in the verdict" \
        "assert d['error_beats_down']=='error', d" "$J"
  check "check specs parse" \
        "assert d['parse_tcp']==['tcp',22] and d['parse_icmp']==['icmp',None], d" "$J"
fi

if want compact-down; then
  echo "compact-down (a red row must still say which host it is)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import re,sys,json; root=sys.argv[1]
js  = open(f"{root}/server/static/app.js").read()
css = open(f"{root}/server/static/style.css").read()

# The down branch of compactRow returns early with its own set of children,
# so the row no longer matches the seven-column template the other rows use.
branch = re.search(r"if \(h\.status === 'down'\) \{(.*?)\n  \}", js, re.S).group(1)
# Children on a normal row: dot, icon, name, then whatever the branch adds.
kids = 3 + len(re.findall(r"a\.appendChild", branch))

tracks = re.search(r"\.crow\.isdown \{[^}]*grid-template-columns:([^;]+);", css).group(1)
# Split on top-level spaces, keeping minmax(...) intact.
cols = re.findall(r"minmax\([^)]*\)|\S+", tracks.strip())

print(json.dumps({
  "children": kids,
  "tracks": len(cols),
  "name_track": cols[2] if len(cols) > 2 else None,
  "reason_track": cols[3] if len(cols) > 3 else None,
  "name_rendered": "el('span', 'cname', h.host)" in js,
  # The reason is abbreviated on this row; the sentence lives in the title.
  "uses_short": "reachShort(h)" in branch,
  "keeps_title": "w.title" in branch,
}))
PYEOF
) || J=''
  # The bug: .cdown spanned to the default template's trailing `auto` track,
  # which sizes to its content's full untruncated width. A long reason grew it
  # until the minmax(0, 1fr) name column was squeezed to zero and the row went
  # out as a red bar with no hostname -- it told you something was down but
  # not what, which is the one thing a row must never do.
  check "the down row's template has one track per child" \
        "assert d['children']==d['tracks']==4, d" "$J"
  # Content-sized, so the name can never be the part that gets truncated.
  check "the name track is content-sized, not a shrinkable fr" \
        "assert 'auto' in d['name_track'] and 'fr' not in d['name_track'], d" "$J"
  check "the reason track is the one that takes leftover space and ellipsizes" \
        "assert 'fr' in d['reason_track'], d" "$J"
  check "the row still renders the hostname" \
        "assert d['name_rendered'] is True, d" "$J"
  check "the row abbreviates and keeps the full reason on hover" \
        "assert d['uses_short'] and d['keeps_title'], d" "$J"
fi

if want duration-wording; then
  echo "duration-wording (a length of time is not a moment in the past)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import re,sys,json; root=sys.argv[1]
js = open(f"{root}/server/static/app.js").read()
def body(name):
    return re.search(r"function %s\(s\) \{(.*?)\n\}" % name, js, re.S).group(1)
reach = re.search(r"function reachText\(h\) \{(.*?)\n\}", js, re.S).group(1)
short = re.search(r"function reachShort\(h\) \{(.*?)\n\}", js, re.S).group(1)
print(json.dumps({
  "age_has_ago":  "ago" in body("fmtAge"),
  "dur_has_ago":  "ago" in body("fmtDur"),
  # "no sample for 3h ago" is not English. Anywhere a duration follows "for"
  # or "silent", it has to be fmtDur.
  "text_for_dur": "for ' + fmtDur" in reach,
  "short_for_dur":"fmtDur" in short and "fmtAge" not in short,
}))
PYEOF
) || J=''
  check "fmtAge still says 'ago' -- it names a moment" \
        "assert d['age_has_ago'] is True, d" "$J"
  check "fmtDur does not, because it names a length" \
        "assert d['dur_has_ago'] is False, d" "$J"
  check "the down reason measures silence as a duration, not as 'Xh ago'" \
        "assert d['text_for_dur'] is True, d" "$J"
  check "the compact form does too" \
        "assert d['short_for_dur'] is True, d" "$J"
fi

if want wedged; then
  echo "wedged (a kernel that answers is not a machine that works)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json,socket,threading; sys.path.insert(0,f"{sys.argv[1]}/server")
import reach

def serve(mode):
    """A listener on loopback. 'greet' writes a banner, 'mute' accepts and
    never speaks -- which is what a host whose userspace has stopped being
    scheduled looks like from outside: the kernel completes the handshake onto
    the backlog all by itself."""
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
    port = srv.getsockname()[1]
    def run():
        while True:
            try: c, _ = srv.accept()
            except OSError: return
            if mode == "greet": c.sendall(b"SSH-2.0-OpenSSH_10.0p2 Debian-5\r\n")
            if mode == "mute":  continue          # hold it open, say nothing
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return port

greet, mute = serve("greet"), serve("mute")
closed = socket.socket(); closed.bind(("127.0.0.1", 0))
shut = closed.getsockname()[1]; closed.close()   # nothing listening here

print(json.dumps({
  "greet_banner":  reach.banner("127.0.0.1", greet, 2)[0],
  "mute_banner":   reach.banner("127.0.0.1", mute, 2)[0],
  "mute_detail":   reach.banner("127.0.0.1", mute, 2)[1],
  # tcp cannot tell these apart -- that is the whole point.
  "greet_tcp":     reach.tcp("127.0.0.1", greet, 2)[0],
  "mute_tcp":      reach.tcp("127.0.0.1", mute, 2)[0],
  "closed_banner": reach.banner("127.0.0.1", shut, 2)[0],
  # Precedence: a service check that fails outranks kernel checks that pass.
  "wedged_mixed":  reach.probe("127.0.0.1", ["icmp","tcp:%d" % mute,"banner:%d" % mute], 2)[0],
  "wedged_via":    reach.probe("127.0.0.1", ["icmp","banner:%d" % mute], 2)[1],
  "healthy_mixed": reach.probe("127.0.0.1", ["icmp","banner:%d" % greet], 2)[0],
  # A host with no sshd must not be called down by a check that cannot answer.
  "nosshd_mixed":  reach.probe("127.0.0.1", ["icmp","banner:%d" % shut], 2)[0],
  "parse_banner":  list(reach.parse_check("banner:22")),
  "levels":        [reach.LEVEL["icmp"], reach.LEVEL["tcp"], reach.LEVEL["banner"]],
}))
PYEOF
) || J=''
  # The outage that prompted all of this: a VM whose kernel still answers ICMP
  # and still completes handshakes onto sshd's backlog, while sshd itself is
  # never scheduled to accept them. Every kernel-level check calls it up.
  check "a port that accepts and then says nothing reads down" \
        "assert d['mute_banner']=='down', d" "$J"
  check "and says why, in words that name the failure" \
        "assert 'userspace wedged' in d['mute_detail'], d" "$J"
  check "a service that greets reads up" \
        "assert d['greet_banner']=='up', d" "$J"
  # This is the flaw the banner check exists to cover.
  check "tcp alone cannot tell a wedged host from a healthy one" \
        "assert d['greet_tcp']==d['mute_tcp']=='up', d" "$J"
  check "a failed service check outranks kernel checks that passed" \
        "assert d['wedged_mixed']=='down', d" "$J"
  check "and the verdict is attributed to the check that proved it" \
        "assert d['wedged_via'].startswith('banner:'), d" "$J"
  check "a healthy host is still up with the same check list" \
        "assert d['healthy_mixed']=='up', d" "$J"
  # A refusal means the check cannot answer, not that the host is broken --
  # a machine with no sshd is not a machine that is down.
  check "no listener means the service check abstains rather than fails" \
        "assert d['closed_banner']=='error' and d['nosshd_mixed']=='up', d" "$J"
  check "banner specs parse and carry the service level" \
        "assert d['parse_banner']==['banner',22] and d['levels']==['kernel','kernel','service'], d" "$J"
fi

if want host-checks; then
  echo "host-checks (the right probe is a property of the machine)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json; sys.path.insert(0,f"{sys.argv[1]}/server")
import app
app.CFG={"reachability":{"checks":["icmp","tcp:22"]},
         "hosts":{"hassium":{"checks":["icmp","banner:22"]},"plain":{}}}
print(json.dumps({
  "override": app.checks_for("hassium"),
  "inherited": app.checks_for("plain"),
  "unlisted": app.checks_for("nobody"),
}))
PYEOF
) || J=''
  check "a host can name its own checks" \
        "assert d['override']==['icmp','banner:22'], d" "$J"
  check "hosts without an override inherit the global list" \
        "assert d['inherited']==d['unlisted']==['icmp','tcp:22'], d" "$J"
fi

if want eol-ack; then
  echo "eol-ack (acknowledging a warning is not consent to be silent later)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json,time,tempfile,os; sys.path.insert(0,f"{sys.argv[1]}/server")
import app, db, eol
tmp = os.path.join(tempfile.mkdtemp(), "t.db")
app.CONN = db.connect(tmp)
app.CFG={"thresholds":{"cpu":{"warn":80,"crit":95},"mem":{"warn":85,"crit":95},
                       "disk":{"warn":85,"crit":95}},
         "stale_after_seconds":180,"patch_stale_hours":48,
         "reachability":{"enabled":False},"eol":{"enabled":True},"hosts":{}}
now=time.time()

STATE={}
eol.lookup = lambda os_string, cfg: dict(STATE)          # drive it directly
def sample():
    return {"host":"box","ts":now,"os":"Debian GNU/Linux 12 (bookworm)","cpu_pct":1.0,
            "mem_used_bytes":1,"mem_total_bytes":100,"uptime_seconds":1,"disks":[],
            "virt":None,"patch_security":None,"patch_other":None,
            "patch_checked_at":None,"patch_source":None,"patch_detail":None,
            "patch_reboot":None,"patch_packages":None,"collector_version":None,
            "peer_addr":None}
db.insert_sample(app.CONN, {"host":"box","os":"Debian GNU/Linux 12 (bookworm)"})
def st(**kw):
    STATE.clear(); STATE.update(kw)
    return app.eol_for(sample())
def ack():
    e = app.eol_for(sample())
    db.ack_eol(app.CONN,"box",e["status"],e.get("product"),e.get("cycle"),
               e.get("eol_date"),int(now))

SOON=dict(status="eol_soon",source="endoflife.date",product="debian",
          cycle="12",eol_date="2026-09-30",days_left=27)
PAST=dict(SOON, status="eol", days_left=-1)

before = st(**SOON)
ack()
after  = st(**SOON)
# The transition the whole design turns on.
now_past = st(**PAST)
# A different release is a decision nobody made.
upgraded = st(**dict(SOON, cycle="13", eol_date="2028-06-10"))
# Upstream moving the date is new information about the old decision.
moved    = st(**dict(SOON, eol_date="2027-01-15"))
# Re-acknowledging the new phase silences the red too.
st(**PAST); ack()
past_acked = st(**PAST)
db.unack_eol(app.CONN,"box")
after_unack = st(**PAST)
# Nothing to silence on a healthy release.
supported = st(status="supported",source="endoflife.date",product="debian",
               cycle="13",eol_date="2030-06-01",days_left=900)

print(json.dumps({
  "before":       [before["status"], before["acknowledged"], before["ackable"]],
  "after":        [after["status"], after["acknowledged"]],
  "now_past":     [now_past["status"], now_past["acknowledged"]],
  "upgraded":     upgraded["acknowledged"],
  "moved":        moved["acknowledged"],
  "past_acked":   past_acked["acknowledged"],
  "after_unack":  after_unack["acknowledged"],
  "supported":    [supported["acknowledged"], supported["ackable"]],
  "acked_at_set": past_acked["acked_at"] is not None,
}))
PYEOF
) || J=''
  check "an unacknowledged eol_soon reads as a live warning" \
        "assert d['before']==['eol_soon',False,True], d" "$J"
  check "acknowledging eol_soon silences it" \
        "assert d['after']==['eol_soon',True], d" "$J"
  # The rule the feature exists for. Acknowledging "goes EOL in three weeks"
  # is a statement that you know and have a plan. It is not consent to be
  # silent on the day the release actually stops getting fixes, which is a
  # materially worse fact and gets to interrupt again with no action needed.
  check "that ack does NOT carry over when the release goes properly past EOL" \
        "assert d['now_past']==['eol',False], d" "$J"
  check "past EOL can be acknowledged in its own right" \
        "assert d['past_acked'] is True and d['acked_at_set'], d" "$J"
  check "un-acknowledging brings the warning back" \
        "assert d['after_unack'] is False, d" "$J"
  # Same shape as the patch ack: the ack pins a state, not a host.
  check "upgrading to another release un-silences it" \
        "assert d['upgraded'] is False, d" "$J"
  check "upstream moving the date un-silences it" \
        "assert d['moved'] is False, d" "$J"
  check "a supported release has nothing to acknowledge" \
        "assert d['supported']==[False,False], d" "$J"
fi

if want eol-ack-ui; then
  echo "eol-ack-ui (an acknowledged warning leaves the All view alone)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import re,sys,json; root=sys.argv[1]
js  = open(f"{root}/server/static/app.js").read()
py  = open(f"{root}/server/app.py").read()
row = re.search(r"function compactRow\(h\) \{(.*?)\n\}", js, re.S).group(1)
badge = re.search(r"function eolBadge\(e\) \{(.*?)\n\}", js, re.S).group(1)
ovr = re.search(r"var eolCount = .*?\}\)\.length;", js, re.S).group(0)
print(json.dumps({
  # The literal ask: no triangle on the All view once acknowledged.
  "row_hides":    "!h.eol.acknowledged" in row,
  # The card keeps the words and drops the colour, like an acked patch count.
  "badge_mutes":  "e.acknowledged ? 'var(--st-none)'" in badge,
  "badge_shows":  "return null" in badge,
  "count_skips":  "!h.eol.acknowledged" in ovr,
  # Old links must keep working.
  "legacy_route": bool(re.search(r"host/\(\[\^/\]\+\)/\(\?:\(patches\|eol\)/\)\?", py)),
  "kind_routed":  "m.group(2) or \"patches\"" in py,
}))
PYEOF
) || J=''
  check "the compact row drops the triangle when acknowledged" \
        "assert d['row_hides'] is True, d" "$J"
  # A muted triangle in a forty-row scan list is the worst of both: it still
  # pulls the eye, just less well. The card has room to say "acknowledged".
  check "the card badge mutes rather than vanishing" \
        "assert d['badge_mutes'] and d['badge_shows'], d" "$J"
  check "the header's past-EOL count skips acknowledged hosts" \
        "assert d['count_skips'] is True, d" "$J"
  check "/api/host/<name>/ack still means patches" \
        "assert d['legacy_route'] and d['kind_routed'], d" "$J"
fi

if want render; then
  # The one case that needs a JS engine. node is not in the suite's stated
  # dependencies (sh, awk, python3), so its absence skips rather than fails --
  # but when it is there, this is the only thing that runs the dashboard code
  # rather than reading it. `node --check` proves app.js parses, and the bug
  # that prompted this parsed perfectly: a `var` inside renderDetail shadowing
  # a top-level function of the same name, which killed the detail view of
  # every host with an end-of-life warning and reported itself to the user as
  # a server outage.
  if ! command -v node >/dev/null 2>&1; then
    echo "render (SKIPPED -- node not installed)"
  else
    echo "render (the dashboard actually draws, not merely parses)"
    J=$(node "$ROOT/tests/js/render.test.js" 2>&1) || J=''
    check "the detail view renders for a host nearing end of life" \
          "assert d['detail_eol_soon'] is None, d['detail_eol_soon']" "$J"
    check "and for one already past it" \
          "assert d['detail_eol_past'] is None, d['detail_eol_past']" "$J"
    check "and for one whose warning is acknowledged" \
          "assert d['detail_eol_acked'] is None, d['detail_eol_acked']" "$J"
    # Both ack rows on one page is what tripped the shadowing.
    check "and with a patch ack and an eol ack on the same page" \
          "assert d['detail_both_ack_rows'] is None, d['detail_both_ack_rows']" "$J"
    # What the whole-state ack could not express: python312 acknowledged on its
    # own, and still acknowledged when the rest of the list has moved on.
    check "every pending package gets its own acknowledge link" \
          "assert d['every_pending_package_gets_its_own_ack_row'] is None, d['every_pending_package_gets_its_own_ack_row']" "$J"
    # The page used to carry a row for the packages the capped list never
    # named, with an acknowledge link beside it.
    check "a check from before the boot says so, and drops the reboot flag" \
          "assert d['a_check_from_before_the_boot_says_so_and_drops_the_reboot_flag'] is None, d['a_check_from_before_the_boot_says_so_and_drops_the_reboot_flag']" "$J"
    check "a check that names nothing at all still explains its red badge" \
          "assert d['a_check_that_names_nothing_still_explains_its_red_badge'] is None, d['a_check_that_names_nothing_still_explains_its_red_badge']" "$J"
    check "a counted-but-unnamed remainder is stated, with no way to ack it" \
          "assert d['a_counted_but_unnamed_remainder_is_stated_and_not_ackable'] is None, d['a_counted_but_unnamed_remainder_is_stated_and_not_ackable']" "$J"
    check "and nothing on the page asks for an ack on a package it cannot show" \
          "assert d['every_listed_package_is_named_and_separately_ackable'] is None, d['every_listed_package_is_named_and_separately_ackable']" "$J"
    check "and for a host that is down" \
          "assert d['detail_down'] is None, d['detail_down']" "$J"
    check "and for one that is away" \
          "assert d['detail_away'] is None, d['detail_away']" "$J"
    check "the compact overview renders" \
          "assert d['overview_compact'] is None, d['overview_compact']" "$J"
    check "the card overview renders" \
          "assert d['overview_cards'] is None, d['overview_cards']" "$J"
    check "an empty fleet renders" \
          "assert d['overview_empty'] is None, d['overview_empty']" "$J"
    # What was actually asked for: quiet on the All view once acknowledged.
    check "acknowledging eol_soon takes its triangle off the All view" \
          "assert d['acked_eol_soon_has_no_triangle'] is None, d['acked_eol_soon_has_no_triangle']" "$J"
    check "acknowledging past EOL takes its triangle off too" \
          "assert d['acked_eol_past_has_no_triangle'] is None, d['acked_eol_past_has_no_triangle']" "$J"
    check "an unacknowledged past EOL still shows red there" \
          "assert d['unacked_eol_past_has_a_triangle'] is None, d['unacked_eol_past_has_a_triangle']" "$J"
    check "the console detail page renders its adopted fleet" \
          "assert d['detail_fleet'] is None, d['detail_fleet']" "$J"
    check "and renders when every device is online" \
          "assert d['detail_fleet_all_online'] is None, d['detail_fleet_all_online']" "$J"
    check "the console card renders with a fleet badge" \
          "assert d['overview_fleet_cards'] is None, d['overview_fleet_cards']" "$J"
    check "the badge names the offline device while there is room to" \
          "assert d['fleet_card_names_the_offline_device'] is None, d['fleet_card_names_the_offline_device']" "$J"
    check "and falls back to a count when there are too many to name" \
          "assert d['a_big_fleet_falls_back_to_a_count'] is None, d['a_big_fleet_falls_back_to_a_count']" "$J"
    check "and a host with no fleet grows no fleet badge" \
          "assert d['a_host_with_no_fleet_renders_no_fleet_row'] is None, d['a_host_with_no_fleet_renders_no_fleet_row']" "$J"
    check "a 30-day view renders from hourly rollups" \
          "assert d['detail_hourly_range'] is None, d['detail_hourly_range']" "$J"
    check "and says hourly rather than implying per-sample detail" \
          "assert d['an_hourly_range_says_so_in_the_caption'] is None, d['an_hourly_range_says_so_in_the_caption']" "$J"
    check "a disk projection reaches the page" \
          "assert d['a_projection_reaches_the_page'] is None, d['a_projection_reaches_the_page']" "$J"
    check "the event log renders" \
          "assert d['detail_events'] is None, d['detail_events']" "$J"
    check "and an outage that already ended is still on it" \
          "assert d['an_outage_that_ended_still_shows'] is None, d['an_outage_that_ended_still_shows']" "$J"
    check "while a host with no events grows no panel" \
          "assert d['no_events_panel_when_nothing_has_happened'] is None, d['no_events_panel_when_nothing_has_happened']" "$J"
    check "the disk panel draws usage over time" \
          "assert d['detail_disk_history'] is None, d['detail_disk_history']" "$J"
    check "with a full panel per mount, not a sliver under a bar" \
          "assert d['every_mount_gets_its_own_full_panel'] is None, d['every_mount_gets_its_own_full_panel']" "$J"
    check "each disk trace uses a real colour, the same green cpu uses" \
          "assert d['disk_traces_are_coloured_like_cpu_and_memory'] is None, d['disk_traces_are_coloured_like_cpu_and_memory']" "$J"
    check "and an axis caption on each, exactly like cpu and memory" \
          "assert d['a_disk_panel_carries_a_caption_like_cpu_does'] is None, d['a_disk_panel_carries_a_caption_like_cpu_does']" "$J"
    check "a mount with no history keeps its panel and says so" \
          "assert d['a_mount_with_no_history_still_gets_its_panel'] is None, d['a_mount_with_no_history_still_gets_its_panel']" "$J"
    check "and an appliance with no filesystem shows an honest blank" \
          "assert d['a_host_with_no_mounts_says_so'] is None, d['a_host_with_no_mounts_says_so']" "$J"
    check "and a long range labels the disk buckets daily" \
          "assert d['a_daily_disk_range_says_daily'] is None, d['a_daily_disk_range_says_daily']" "$J"
    check "the polled appliances get their own icons, not a question mark" \
          "assert d['appliances_get_their_own_icons'] is None, d['appliances_get_their_own_icons']" "$J"
    check "and those icons actually draw shapes with a colour" \
          "assert d['an_appliance_icon_actually_draws_shapes'] is None, d['an_appliance_icon_actually_draws_shapes']" "$J"
    # A host that has been down for longer than the retention window. Every
    # panel on the page is reading a null it used to be guaranteed a number
    # for, because until now such a host simply left the dashboard.
    check "the detail view renders for a host with no readings left" \
          "assert d['detail_no_samples'] is None, d['detail_no_samples']" "$J"
    check "and both overviews render it too" \
          "assert d['overview_no_samples'] is None, d['overview_no_samples']" "$J"
    # An empty page under a red banner reads as a page that failed to load.
    check "the empty panels say the readings are gone, not nothing at all" \
          "assert d['a_lost_host_says_its_readings_are_gone'] is None, d['a_lost_host_says_its_readings_are_gone']" "$J"
    # Nothing takes a host off the dashboard on its own any more.
    check "a silent host offers a way to be forgotten" \
          "assert d['a_silent_host_can_be_forgotten'] is None, d['a_silent_host_can_be_forgotten']" "$J"
    check "and a reporting one does not, since it would come straight back" \
          "assert d['a_reporting_host_is_not_offered_forgetting'] is None, d['a_reporting_host_is_not_offered_forgetting']" "$J"
    check "a down row still names its host" \
          "assert d['down_row_names_the_host'] is None, d['down_row_names_the_host']" "$J"
  fi
fi

if want ingest-race; then
  echo "ingest-race (two collectors posting at once must not swap disk rows)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import sys,json,threading,tempfile,os; sys.path.insert(0,f"{sys.argv[1]}/server")
import db
tmp = os.path.join(tempfile.mkdtemp(), "t.db")
conn = db.connect(tmp)

# The shape that actually bit: every collector's cron fires on the same second,
# so the server handles their posts on overlapping threads against one shared
# sqlite connection. Each host here carries a mount list only it could have, so
# a row landing on the wrong sample is visible by name, not merely by count.
HOSTS = 24
START = threading.Barrier(HOSTS)
got, errs = {}, []
def post(i):
    payload = {"host": "h%02d" % i, "os": "test",
               "disks": [{"mount": "/m%02d-%d" % (i, k),
                          "used_bytes": 1, "total_bytes": 100} for k in range(3)]}
    try:
        START.wait()
        got[i] = db.insert_sample(conn, payload)
    except Exception as e:
        errs.append(repr(e))

ts = [threading.Thread(target=post, args=(i,)) for i in range(HOSTS)]
for t in ts: t.start()
for t in ts: t.join()

wrong = []
for i, sid in sorted(got.items()):
    mounts = sorted(r["mount"] for r in
                    conn.execute("SELECT mount FROM disks WHERE sample_id=?", (sid,)))
    want = sorted("/m%02d-%d" % (i, k) for k in range(3))
    if mounts != want:
        wrong.append({"host": "h%02d" % i, "want": want, "got": mounts})

orphans = conn.execute(
    "SELECT COUNT(*) FROM disks WHERE sample_id NOT IN (SELECT id FROM samples)"
).fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM disks").fetchone()[0]

print(json.dumps({"errs": errs, "inserted": len(got), "wrong": wrong,
                  "orphans": orphans, "total": total}))
PYEOF
)
  check "every post is stored" \
        "assert not d['errs'] and d['inserted']==24, d" "$J"
  check "no sample gets another host's mounts, or loses its own" \
        "assert d['wrong']==[], d['wrong']" "$J"
  check "and no disk row is left pointing at nothing" \
        "assert d['orphans']==0 and d['total']==72, d" "$J"
fi

# --------------------------------------------------------------- Hubitat ----
if want hubitat; then
  echo "hubitat (a load average is not a percentage)"
  J=$(python3 - "$ROOT" "$FIX" <<'PYEOF'
import json, sys, os
root, fix = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(root, "server"))
import hubitat

F = os.path.join(fix, "hubitat")
SERVED = {
    "/hub2/hubData":                  "hubData.json",
    "/hub/advanced/freeOSMemory":     "freeOSMemory.txt",
    "/hub/advanced/freeOSMemoryLast": "freeOSMemoryLast.txt",
    "/hub/cpuInfo":                   "cpuInfo.txt",
    "/hub/cloud/checkForUpdate":      "checkForUpdate.json",
}

def serve(files):
    def _get(cfg, path, timeout=10):
        name = files.get(path)
        if name is None:
            raise hubitat.HubitatError("GET %s -> HTTP 404" % path)
        return open(os.path.join(F, name)).read()
    return _get

def fresh():
    """Each scenario starts with empty caches, as a new process would."""
    hubitat._CORES["value"] = None
    hubitat._PATCH_CACHE.update(ts=0.0, value=None, error=None)

cfg = {"host": "10.0.0.76", "display_name": "palladium",
       "mem_total_bytes": 2147483648}

fresh(); hubitat._get = serve(SERVED)
ok = hubitat.collect(cfg)

# The hub reports free memory and never a total, so a hub with no declared
# total must report nothing rather than a guess.
fresh(); hubitat._get = serve(SERVED)
notot = hubitat.collect({k: v for k, v in cfg.items() if k != "mem_total_bytes"})

# Hub security on: every endpoint serves the login page with a 200.
fresh(); hubitat._get = serve({k: "login-page.html" for k in SERVED})
try:
    hubitat.collect(cfg)
    secured = None
except hubitat.HubitatError as e:
    secured = str(e)

# A hub that answers hubData but nothing else still has an identity worth
# showing -- metrics are best effort, the card is not.
fresh(); hubitat._get = serve({"/hub2/hubData": "hubData.json"})
partial = hubitat.collect(cfg)

# An up-to-date hub answers with the status alone and no "upgrade" key.
fresh(); hubitat._get = serve(dict(SERVED, **{
    "/hub/cloud/checkForUpdate": "checkForUpdate-none.json"}))
current = hubitat.collect(cfg)

print(json.dumps({"ok": ok, "notot": notot, "secured": secured,
                  "partial": partial, "current": current}))
PYEOF
)
  check "cpu is the load average over the core count, not the raw column" \
        "assert d['ok']['cpu_pct']==9.2, d['ok']['cpu_pct']" "$J"
  check "and is emphatically not the 0.37 the hub printed" \
        "assert d['ok']['cpu_pct']!=0.37, d['ok']" "$J"
  check "memory is the declared total minus the measured free" \
        "assert d['ok']['mem_used_bytes']==779747328, d['ok']" "$J"
  check "an undeclared total reports unknown rather than a guess" \
        "assert d['notot']['mem_used_bytes'] is None and d['notot']['mem_total_bytes'] is None, d['notot']" "$J"
  check "an available update is one uncategorised patch, never zero security" \
        "assert d['ok']['patches']['other']==1 and d['ok']['patches']['security'] is None, d['ok']['patches']" "$J"
  check "an up-to-date hub is checked with nothing pending, not left unchecked" \
        "p=d['current']['patches']; assert p and p['other']==0 and p['security'] is None and p['checked_at'], p" "$J"
  check "no uptime and no invented disk row" \
        "assert d['ok']['uptime_seconds'] is None and d['ok']['disks']==[], d['ok']" "$J"
  check "the os string carries model and platform version" \
        "assert d['ok']['os']=='Hubitat C-8 Pro 2.5.1.174', d['ok']['os']" "$J"
  check "hub security is named as the cause, not reported as bad data" \
        "assert d['secured'] and 'hub security' in d['secured'], d['secured']" "$J"
  check "a hub that answers only hubData still reports its identity" \
        "assert d['partial']['host']=='palladium' and d['partial']['cpu_pct'] is None, d['partial']" "$J"
fi


# ----------------------------------------------------------------- UniFi ----
if want unifi; then
  echo "unifi (a fleet is not five more hosts)"
  J=$(python3 - "$ROOT" "$FIX" <<'PYEOF'
import json, sys, os
root, fix = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(root, "server"))
import unifi, db, app

F = os.path.join(fix, "unifi")
S = "00000000-0000-0000-0000-0000000000aa"
API = "/proxy/network/integration/v1"
SERVED = {
    "/api/system":                                  "system.json",
    API + "/sites":                                 "sites.json",
    API + "/sites/%s/devices" % S:                  "devices.json",
    API + "/sites/%s/devices/d-gw/statistics/latest" % S: "stats-gw.json",
    API + "/sites/%s/devices/d-sw/statistics/latest" % S: "stats-sw.json",
    # d-ap is OFFLINE: the controller serves no statistics for it at all.
}

def serve(files):
    def _request(cfg, path, timeout=15, auth=True):
        name = files.get(path)
        if name is None:
            raise unifi.UniFiError("GET %s -> HTTP 404" % path)
        return json.load(open(os.path.join(F, name)))
    return _request

cfg = {"host": "10.0.0.1", "display_name": "lanthanum", "api_key": "x"}

unifi._request = serve(SERVED)
p = unifi.collect(cfg)

# The console must be picked out by the MAC /api/system reports, not by
# position and not by the operator having to name it.
noname = dict(p)

# With /api/system unavailable, model prefix matching still finds the gateway.
unifi._request = serve({k: v for k, v in SERVED.items() if k != "/api/system"})
nosys = unifi.collect(cfg)

# End to end: through the database and out of summarize(), which is where the
# fleet has to survive a round trip rather than merely be built correctly.
import tempfile
conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
unifi._request = serve(SERVED)
db.insert_sample(conn, unifi.collect(cfg), source="unifi")
row = [r for r in db.latest_per_host(conn) if r["host"] == "lanthanum"][0]
app.CFG = {"thresholds": {"cpu": {"warn": 80, "crit": 95},
                          "mem": {"warn": 85, "crit": 95},
                          "disk": {"warn": 85, "crit": 95}},
           "stale_after_seconds": 180, "patch_stale_hours": 48,
           "hosts": {}, "eol": {"enabled": False}, "reachability": {}}
summ = app.summarize(row)

print(json.dumps({"p": p, "nosys": nosys, "summ": summ,
                  "row_mem_pct": row["mem_pct"]}))
PYEOF
)
  check "the console is found by the mac /api/system reports" \
        "assert d['p']['host']=='lanthanum' and d['p']['os']=='UniFi OS 5.1.31', d['p']" "$J"
  check "and by model prefix when /api/system will not answer" \
        "assert d['nosys']['os']=='UniFi OS 5.1.31', d['nosys']" "$J"
  check "the console is not listed among its own adopted devices" \
        "assert sorted(m['name'] for m in d['p']['fleet'])==['Nano HD','USW-16-PoE'], d['p']['fleet']" "$J"
  check "and the stored fleet comes back ordered by name, not by health" \
        "assert [m['name'] for m in d['summ']['fleet']['members']]==['Nano HD','USW-16-PoE'], d['summ']['fleet']['members']" "$J"
  check "memory is a percentage, with no bytes invented for it" \
        "assert d['p']['mem_pct']==63.2 and d['p']['mem_used_bytes'] is None, d['p']" "$J"
  check "and survives the round trip through the database" \
        "assert d['row_mem_pct']==63.2 and abs(d['summ']['mem']['pct']-63.2)<0.01, d['summ']['mem']" "$J"
  check "no disk row is invented for a console that reports no storage" \
        "assert d['p']['disks']==[], d['p']['disks']" "$J"
  check "an offline device still appears, with no stats rather than zeroes" \
        "m={x['name']: x for x in d['summ']['fleet']['members']}['Nano HD']; assert m['online'] is False and m['cpu_pct'] is None and m['mem_pct'] is None, m" "$J"
  check "the rollup counts and names what is offline" \
        "assert d['summ']['fleet']['offline']==1 and d['summ']['fleet']['offline_names']==['Nano HD'] and d['summ']['fleet']['online']==1, d['summ']['fleet']" "$J"
  check "a downed fleet device does not turn the console's own card red" \
        "assert d['summ']['status']=='ok', (d['summ']['status'], d['summ']['fleet'])" "$J"
  check "fleet firmware updates are counted but stay off the console's patch badge" \
        "assert d['summ']['fleet']['updatable']==1 and d['summ']['patches']['other']==0, d['summ']['patches']" "$J"
fi


# --------------------------------------------------------------- history ----
if want history; then
  echo "history (a dashboard that forgets cannot answer when it started)"
  J=$(python3 - "$ROOT" <<'PYEOF'
import json, os, sys, tempfile, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import db, app

conn = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
now = int(time.time())
# 40 days: /data fills at 1 point a day, / sits still.
for d in range(40, 0, -1):
    for q in range(96):
        ts = now - d * 86400 + q * 900
        pct = 40 + (40 - d) * 1.0
        db.insert_sample(conn, {"host": "cerium", "ts": ts, "cpu_pct": 10 + (q % 20),
            "mem_used_bytes": int(4e9), "mem_total_bytes": int(8e9),
            "disks": [{"mount": "/data", "used_bytes": int(1e12 * pct / 100),
                       "total_bytes": int(1e12)},
                      {"mount": "/", "used_bytes": int(5e10), "total_bytes": int(1e11)}]})
# An appliance that reports a percentage and no bytes must roll up too.
for q in range(200):
    db.insert_sample(conn, {"host": "lanthanum", "ts": now - q * 900, "cpu_pct": 20.0,
                            "mem_pct": 63.2, "disks": []}, source="unifi")

db.rollup(conn, 40 * 24)
before = conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0]
db.rollup(conn, 40 * 24)
after = conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0]

app.CONN = conn
app.CFG = {"history": {"project_days": 30}}
proj = app.disk_projection("cerium", "/data")
flat = app.disk_projection("cerium", "/")

lan = db.rollup_series(conn, "lanthanum", now - 4 * 3600)
one = db.rollup_series(conn, "cerium", now - 40 * 86400)[0]

db.record_event(conn, "cerium", "status", "ok", "down", "no icmp reply")
db.record_event(conn, "cerium", "status", "down", "ok", None)
seeded = db.last_states(conn)

db.prune(conn, 48, rollup_days=30, event_days=None)
kept = {
    "hourly": conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0],
    "daily":  conn.execute("SELECT COUNT(*) FROM disk_rollups").fetchone()[0],
    "raw":    conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
    "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
}
oldest_raw = conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
oldest_roll = conn.execute("SELECT MIN(bucket) FROM rollups").fetchone()[0]
cerium_buckets = conn.execute(
    "SELECT COUNT(*) FROM rollups WHERE host='cerium'").fetchone()[0]

dh = db.disk_history(conn, "cerium", now - 86400, db.HOUR)
disk_pts_24h = len(dh.get("/data", []))

print(json.dumps({"proj": proj, "flat": flat, "before": before, "after": after,
                  "disk_pts_24h": disk_pts_24h,
                  "lan_mem": lan[0]["mem_pct"] if lan else None,
                  "one": one, "seeded": seeded, "kept": kept,
                  "cerium_buckets": cerium_buckets,
                  "raw_age_h": (now - oldest_raw) / 3600.0,
                  "roll_age_d": (now - oldest_roll) / 86400.0}))
PYEOF
)
  check "a filling mount projects a date, from a slope not a snapshot" \
        "assert d['proj']['days_to_full']==21 and abs(d['proj']['slope_per_day']-1.0)<0.02, d['proj']" "$J"
  check "and a flat mount refuses to forecast at all" \
        "assert d['flat'] is None, d['flat']" "$J"
  check "a bucket keeps min and max, not just the average that hides them" \
        "o=d['one']; assert o['cpu_min'] < o['cpu_pct'] < o['cpu_max'] and o['samples']==4, o" "$J"
  check "rolling up twice changes nothing" \
        "assert d['before']==d['after'], (d['before'], d['after'])" "$J"
  check "a source reporting a percentage and no bytes still rolls up" \
        "assert abs(d['lan_mem']-63.2)<0.01, d['lan_mem']" "$J"
  check "raw ages out at its own window" \
        "assert d['raw_age_h'] <= 48.1, d['raw_age_h']" "$J"
  # Not an exact bucket count: the cutoff is now-30d and buckets are hour
  # aligned, so the oldest one survives or not depending on the minute this
  # runs. Asserting 720 exactly failed roughly once an hour, for no reason.
  check "while rollups outlive it by weeks" \
        "assert 29 <= d['roll_age_d'] <= 30.1 and 719 <= d['cerium_buckets'] <= 721, (d['roll_age_d'], d['cerium_buckets'])" "$J"
  check "disk history is bucketed, never one point per sample" \
        "assert d['disk_pts_24h'] <= 25 and d['disk_pts_24h'] >= 20, d['disk_pts_24h']" "$J"
  check "and events are never pruned when no event window is set" \
        "assert d['kept']['events']==2, d['kept']" "$J"
  check "the watcher can be reseeded from the log after a restart" \
        "assert d['seeded']=={'cerium': 'ok'}, d['seeded']" "$J"
fi


# ----------------------------------------------------------------- icons ----
if want icons; then
  # Geometry only. Whether an icon LOOKS right is a question only a person
  # looking at the PNG can answer -- see tests/icons/rasterize.py, which exists
  # because three icon bugs shipped that all read fine as coordinates.
  if ! command -v node >/dev/null 2>&1; then
    echo "icons (SKIPPED -- node not installed)"
  else
    echo "icons (every glyph parses and stays inside its box)"
    J=$(python3 "$ROOT/tests/icons/rasterize.py" --check 2>/dev/null) || J=''
    check "every icon family has drawable geometry, inside the viewBox" \
          "assert d['errors']==[], d['errors']" "$J"
    check "and the whole set is present" \
          "assert d['families'] >= 12, d['families']" "$J"
  fi
fi


echo
echo "passed $PASS, failed $FAIL"
[ "$FAIL" -eq 0 ]
