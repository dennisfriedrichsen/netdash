#!/bin/sh
# netdash patch check -- Linux (apt, dnf, pacman, zypper, apk, slackpkg).
#
# Runs DAILY and once at every boot, not at collector cadence: every backend
# below either hits the network or parses the whole package database, which is
# untenable every 30s. It writes a small JSON file that netdash-collector reads
# back and inlines.
#
# The boot run is not an optimisation. Nothing but this script updates that
# file, so after a patch-and-reboot the dashboard keeps reporting what was
# pending beforehand -- including reboot_required, which is read from
# /var/run/reboot-required and is exactly what the reboot cleared.
#
#   netdash-patchcheck.sh            # check, write the state file
#   netdash-patchcheck.sh --print    # check, print, write nothing
#
# See PATCH-CHECKS.md for what each package manager can and cannot answer.
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.5.1"

MODE="${1:-}"

PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
export DEBIAN_FRONTEND=noninteractive LC_ALL=C

CONF="${NETDASH_CONF:-/etc/netdash/collector.conf}"
[ -f "$CONF" ] && . "$CONF"

# Deliberately NOT /var/lib/netdash: on a host that also runs the netdash
# server that directory belongs to the server, is mode 750 netdash:netdash, and
# deploy.sh does `chown -R netdash:netdash` over it -- so collector state landed
# in another component's data directory and changed owner on the next deploy.
STATE="${NETDASH_PATCH_STATE:-/var/lib/netdash-collector/patches.json}"
# Refreshing package metadata is what makes this indicator mean anything on a
# host with no unattended-upgrades: without it the counts come from whenever
# the admin last ran an update by hand, and "0 pending" from March reads
# identically to a patched host. Set NETDASH_PATCH_REFRESH="no" to forbid it.
REFRESH="${NETDASH_PATCH_REFRESH:-yes}"

NOW=$(date +%s)
SEC=null        # security-classified count; null means "cannot classify"
OTH=null        # everything else pending
SRC=unknown
DET=""
META=""         # when the package metadata was last known current, if knowable
REFRESHED=0     # 1 once this run has brought the metadata up to date
REFRESH_FAILED=0

# Backends legitimately fail (no network, a repo down, a lock held by the
# admin's own upgrade). Under `set -e` a bare failing command aborts the whole
# script, so every one of them is guarded.
run() { "$@" 2>/dev/null || true; }

mtime() { [ -e "$1" ] && stat -c %Y "$1" 2>/dev/null || true; }

# Names of the security-relevant items -- every one of them, uncapped. An
# acknowledgement in netdash is made against a package by name, so a name that
# never arrives is a package nobody can review: the old six-name cap with its
# "(+N more)" tail did not just hide the rest, it made them acknowledgeable
# only as an anonymous group keyed on how many there were. The cap bought
# nothing worth that -- the server wrote this string into every sample and only
# ever read the newest, and it keeps the list per host now.
#
# Reads names on stdin, one per line, and strips anything that could break out
# of the JSON string: the payload is assembled by printf in shell, so a quote or
# a backslash in a package name would produce malformed JSON that the server
# then rejects, losing the whole sample -- metrics included -- over this field.
join_names() {
  tr -cd 'A-Za-z0-9._+:~,()\- \n' | awk '
    NF { out = out (out ? ", " : "") $0 }
    END { printf "%s", out }'
}
SECPKGS=""

# ---------------------------------------------------------------- apt ----
if command -v apt-get >/dev/null 2>&1; then
  SRC=apt
  if [ "$REFRESH" = yes ]; then
    # NoLocking so a concurrent unattended-upgrades run cannot make this fail,
    # and -qq to keep a daily cron job silent.
    if apt-get -qq -o Debug::NoLocking=true update >/dev/null 2>&1
      then REFRESHED=1; else REFRESH_FAILED=1; fi
  fi
  # update-success-stamp is written by unattended-upgrades, not by apt itself,
  # so it is often absent; the lists directory mtime is the universal fallback.
  META=$(mtime /var/lib/apt/periodic/update-success-stamp)
  [ -n "$META" ] || META=$(mtime /var/lib/apt/lists)

  # dist-upgrade, not upgrade: a security fix that arrives in a renamed package
  # -- a new kernel ABI on Ubuntu is the usual case -- is invisible to `upgrade`,
  # which never installs a package that is not already present. Only Inst lines
  # are counted, so the removals dist-upgrade may also propose are ignored.
  #
  # The security classification lives in the origin annotation inside the
  # parentheses and nowhere else:
  #   Inst openssl (3.5.7-1~deb13u2 Debian-Security:13/stable-security [amd64])
  #   Inst zlib1g  (1:1.3.dfsg+really1.3.1-1+b1 Debian:13.6/stable [amd64])
  # Ubuntu spells it Ubuntu:24.04/noble-security, so testing for "-security"
  # covers both. The test is applied to the parenthesised part alone: matching
  # the whole line counts any package whose *name* contains "security". A
  # package name cannot contain "(", so the cut is unambiguous.
  UPG=$(run apt-get --just-print -o Debug::NoLocking=true dist-upgrade)
  set -- $(printf '%s\n' "$UPG" | awk '
    /^Inst / { n++; s=$0; sub(/^[^(]*\(/,"",s); if (s ~ /-security/) sec++ }
    END { printf "%d %d\n", sec+0, n-sec+0 }')
  SEC=${1:-0}; OTH=${2:-0}
  SECPKGS=$(printf '%s\n' "$UPG" | awk '
    /^Inst / { s=$0; sub(/^[^(]*\(/,"",s); if (s ~ /-security/) print $2 }' | join_names)

# ---------------------------------------------------------------- dnf ----
elif command -v dnf5 >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
  if command -v dnf5 >/dev/null 2>&1; then DNF=dnf5; else DNF=dnf; fi
  SRC=$DNF
  if [ "$REFRESH" = yes ]; then
    # dnf4 kept metadata fresh with dnf-makecache.timer; under dnf5 that timer
    # is not guaranteed to exist, so refresh rather than assume.
    if $DNF -q makecache >/dev/null 2>&1
      then REFRESHED=1; else REFRESH_FAILED=1; fi
  fi
  # check-update exits 100 when updates are pending and 0 when none are, so the
  # exit status carries the answer and the output is only good for counting.
  # Rows are "name.arch  version  repo"; blank lines and the "Obsoleting
  # Packages" section are dropped by requiring three fields.
  count_dnf() { run $DNF -q check-update "$@" | awk 'NF==3 && $1 ~ /\./ {n++} END{print n+0}'; }
  for d in /var/cache/libdnf5 /var/cache/dnf; do
    META=$(mtime "$d"); [ -n "$META" ] && break
  done
  SECOUT=$(run $DNF -q check-update --security)
  SEC=$(printf '%s\n' "$SECOUT" | awk 'NF==3 && $1 ~ /\./ {n++} END{print n+0}')
  SECPKGS=$(printf '%s\n' "$SECOUT" | awk 'NF==3 && $1 ~ /\./ {print $1}' | join_names)
  ALL=$(count_dnf)
  OTH=$((ALL - SEC)); [ "$OTH" -ge 0 ] || OTH=0

# ------------------------------------------------------------- pacman ----
elif command -v pacman >/dev/null 2>&1; then
  SRC=pacman
  # Arch publishes no security classification in the repositories and ships
  # NEITHER tool in base, so with both missing there is nothing to report and
  # no amount of retrying will change that. Say which package to install
  # rather than reporting a generic refresh failure.
  HAVE_CU=0; HAVE_AA=0
  command -v checkupdates >/dev/null 2>&1 && HAVE_CU=1
  command -v arch-audit   >/dev/null 2>&1 && HAVE_AA=1
  if [ "$HAVE_CU" -eq 0 ] && [ "$HAVE_AA" -eq 0 ]; then
    echo "netdash-patchcheck: Arch ships no update-checking tool in base, and" >&2
    echo "  neither checkupdates nor arch-audit is installed. Add at least one:" >&2
    echo "    pacman -S pacman-contrib   # checkupdates: how many updates are pending" >&2
    echo "    pacman -S arch-audit       # which installed packages carry advisories" >&2
    echo "  This host reads as 'not checked' until then, which is the honest" >&2
    echo "  answer: nothing here has looked." >&2
    exit 1
  fi

  # checkupdates syncs into a temporary database rather than /var/lib/pacman/sync,
  # so it cannot leave a partial -Sy behind for a later -Su to turn into a broken
  # partial upgrade. It is therefore the refresh as well as the query, and exits
  # 2 when there is simply nothing to upgrade -- success, not failure.
  if [ "$HAVE_CU" -eq 1 ]; then
    CU=""; rc=0
    CU=$(checkupdates 2>/dev/null) || rc=$?
    case "$rc" in
      0|2) OTH=$(printf '%s' "$CU" | grep -c . || true); REFRESHED=1 ;;
      *)   REFRESH_FAILED=1 ;;
    esac
  else
    DET="install pacman-contrib for update counts"
  fi

  # arch-audit reports installed packages carrying Arch Security Team
  # advisories. Absent, SEC stays null -- "nobody classified this" -- rather
  # than 0, which would claim the host had been checked and found clean.
  # -u restricts to advisories with a fix available, which is the actionable
  # set; unfixed advisories are not counted.
  if [ "$HAVE_AA" -eq 1 ]; then
    AA=""; rc=0
    AA=$(arch-audit -uq 2>/dev/null) || rc=$?
    # Some builds exit non-zero when they find something, so output counts as
    # success too.
    if [ "$rc" -eq 0 ] || [ -n "$AA" ]; then
      SEC=$(printf '%s' "$AA" | grep -c . || true)
      SECPKGS=$(printf '%s\n' "$AA" | join_names)
      REFRESHED=1
      SRC="pacman+arch-audit"
    else
      REFRESH_FAILED=1
    fi
  elif [ -z "$DET" ]; then
    DET="install arch-audit to classify security updates"
  fi

# ------------------------------------------------------------- zypper ----
elif command -v zypper >/dev/null 2>&1; then
  ID=$( . /etc/os-release 2>/dev/null; echo "${ID:-}" )
  if [ "$REFRESH" = yes ]; then
    if zypper --non-interactive --quiet refresh >/dev/null 2>&1
      then REFRESHED=1; else REFRESH_FAILED=1; fi
  fi
  case "$ID" in
    *tumbleweed*|*slowroll*)
      # Rolling snapshots ship no patch metadata at all -- patches are a
      # Leap/SLE concept -- so `zypper list-patches` is empty here because the
      # concept does not exist, not because the host is current. A count is the
      # only honest answer; SEC stays null.
      SRC=zypper-rolling
      OTH=$(run zypper --non-interactive --no-refresh --quiet list-updates | awk '$1=="v"{n++} END{print n+0}')
      DET="rolling release: no security classification available"
      ;;
    *)
      SRC=zypper-patches
      # "5 patches needed (2 security patches)"
      OUT=$(run zypper --non-interactive --no-refresh patch-check)
      TOTP=$(printf '%s\n' "$OUT" | sed -n 's/^\([0-9][0-9]*\) patches* needed.*/\1/p')
      SECP=$(printf '%s\n' "$OUT" | sed -n 's/.*(\([0-9][0-9]*\) security patch.*/\1/p')
      SEC=${SECP:-0}; TOTP=${TOTP:-0}
      OTH=$((TOTP - SEC)); [ "$OTH" -ge 0 ] || OTH=0
      ;;
  esac
  META=$(mtime /var/cache/zypp/raw)

# ---------------------------------------------------------------- apk ----
elif command -v apk >/dev/null 2>&1; then
  SRC=apk
  if [ "$REFRESH" = yes ]; then
    if apk update >/dev/null 2>&1
      then REFRESHED=1; else REFRESH_FAILED=1; fi
  fi
  # Alpine publishes secdb JSON at secdb.alpinelinux.org, but nothing in the
  # base system consumes it, so there is no security classification on the host.
  # `apk list --upgradable` is the modern spelling; `apk version -l '<'` is the
  # one that works everywhere, and its first line is a header.
  if apk list --upgradable >/dev/null 2>&1; then
    OTH=$(run apk list --upgradable | grep -c . || true)
  else
    OTH=$(run apk version -l '<' | tail -n +2 | grep -c . || true)
  fi
  META=$(mtime /var/cache/apk)
  DET="no security classification available on Alpine"

# ----------------------------------------------------------- slackpkg ----
elif command -v slackpkg >/dev/null 2>&1; then
  SRC=slackpkg
  SPDIR=/var/lib/slackpkg
  # Every slackpkg call runs with -batch=on -default_answer=n and stdin closed.
  # Batch mode answers each prompt with default_answer instead of reading one,
  # so the "Do you wish to upgrade selected packages (Y/n)?" at the end of
  # upgrade-all is declined, never accepted. For update the only prompt is
  # "No changes in ChangeLog.txt ... download all other files (y/N)?", where
  # n skips a redundant download and the metadata is current either way.
  if [ "$REFRESH" = yes ]; then
    if slackpkg -batch=on -default_answer=n update </dev/null >/dev/null 2>&1
      then REFRESHED=1; else REFRESH_FAILED=1; fi
  fi
  META=$(mtime "$SPDIR/ChangeLog.txt")

  # There is no query verb: upgrade-all, declined, is the list. It applies the
  # blacklist and repository priority exactly as a real upgrade would, which a
  # hand-rolled pkglist diff would have to reimplement. Exit 20 is slackpkg's
  # "no package found", which here means nothing is pending; 0 is what a
  # declined upgrade with 157 pending returned on the 15.0 host.
  rc=0
  UPG=$(slackpkg -dialog=off -batch=on -default_answer=n upgrade-all </dev/null 2>/dev/null) || rc=$?
  case "$rc" in
    0|20)
        PEND=$(printf '%s\n' "$UPG" | grep -E '^[^ ]+-[^-]+-[^-]+-[^-]+\.t[gxlb]z$' || true)
        TOTAL=$(printf '%s\n' "$UPG" | sed -n 's/^Total package(s): *\([0-9][0-9]*\).*/\1/p')
        # Nothing pending prints "No packages match the pattern for upgrade"
        # and no total at all.
        [ -z "$PEND$TOTAL" ] && printf '%s\n' "$UPG" | grep -q 'No packages match' && TOTAL=0
        # A list that disagrees with slackpkg's own total was misparsed, and a
        # wrong count is worse than the previous one ageing out.
        if [ "$(printf '%s' "$PEND" | grep -c . || true)" != "${TOTAL:-x}" ]; then
          echo "netdash-patchcheck: slackpkg listed ${TOTAL:-no} total, parsed a different count" >&2
          exit 1
        fi ;;
    *)  echo "netdash-patchcheck: slackpkg upgrade-all exited $rc; keeping the previous result" >&2
        exit 1 ;;
  esac

  # Slackware's only security classification is "(* Security fix *)" in the
  # ChangeLog, under a group of package lines (the kernel's five share one
  # description). A pending package is security if any entry for it NEWER than
  # the installed build carries the mark: the log is newest first, so each
  # name is read until its installed build turns up. One installed from the
  # release media never turns up, and the walk stops at the release line --
  # everything above that is a patch it has not had.
  #
  # testing/ and pasture/ are ignored: upgrade-all never takes from them, and
  # a fix to a same-named testing package says nothing about the stable one.
  # Each read is guarded: under set -e a failing one ends the group, awk sees
  # nothing, and every pending package silently vanishes from the count.
  CLASS=$( { ls /var/log/packages 2>/dev/null || true; echo @@PEND@@
             printf '%s\n' "$PEND"; echo @@LOG@@
             cat "$SPDIR/ChangeLog.txt" 2>/dev/null || true; } | awk '
    function pname(f,   n, a, s, i) {
      sub(/^.*\//, "", f); sub(/\.t[gxlb]z$/, "", f)
      n = split(f, a, "-"); s = a[1]
      for (i = 2; i <= n - 3; i++) s = s "-" a[i]
      return s
    }
    function close_group(   i, f, b, k) {
      for (i = 1; i <= ng; i++) {
        f = grp[i]; b = f; sub(/^.*\//, "", b); sub(/\.t[gxlb]z$/, "", b)
        k = pname(f)
        if (!(k in pend) || (k in done)) continue
        if (b == inst[k]) done[k] = 1
        else if (gsec) sec[k] = 1
      }
      ng = 0; gsec = 0; indesc = 0
    }
    /^@@PEND@@$/ { part = 1; next }
    /^@@LOG@@$/  { part = 2; next }
    part == 0 && NF { inst[pname($0)] = $0; next }
    part == 1 && NF { k = pname($0); pend[k] = 1; order[++np] = k; next }
    part == 2 && stop { next }
    part == 2 && /stable is released!/ { close_group(); stop = 1; next }
    part == 2 && /^[^ ]+\.t[gxlb]z:/ {
      if (indesc) close_group()
      if ($0 ~ /^(patches|extra)\//) { f = $0; sub(/:.*/, "", f); grp[++ng] = f }
      next
    }
    part == 2 && /^[^ ]/ { close_group(); next }
    part == 2 { indesc = 1; if (index($0, "(* Security fix *)")) gsec = 1 }
    END {
      close_group()
      for (i = 1; i <= np; i++) print ((order[i] in sec) ? "S " : "O ") order[i]
    }' )
  SEC=$(printf '%s\n' "$CLASS" | grep -c '^S ' || true)
  OTH=$(printf '%s\n' "$CLASS" | grep -c '^O ' || true)
  SECPKGS=$(printf '%s\n' "$CLASS" | sed -n 's/^S //p' | join_names)
  # No ChangeLog means nothing was classified, so every package landed in
  # "other". That is a count, not a finding of zero security fixes.
  if [ ! -s "$SPDIR/ChangeLog.txt" ]; then
    SEC=null; SECPKGS=""
    DET="no slackpkg ChangeLog to classify from; run slackpkg update"
  fi
fi

if [ "$SRC" = unknown ]; then
  echo "netdash-patchcheck: no supported package manager found" >&2
  exit 1
fi

# ---- Reboot required ----
# The one false green the package database cannot see: security updates that
# are installed but not yet running. A Fedora host here finished `dnf update`
# with a clean database while still running the old kernel and the old
# libbluez, both of which were on its security list.
#
# null means "this host has no mechanism to ask", never "no reboot needed".
REBOOT=null
case "$SRC" in
apt)
  # /run/reboot-required is created by a helper script, NOT by apt, so its
  # absence on a host without that helper says nothing at all.
  if [ -e /run/reboot-required ] || [ -e /var/run/reboot-required ]; then
    REBOOT=true

  # Test for the SCRIPT, not a package name: it comes from
  # update-notifier-common on Ubuntu and from reboot-notifier on Debian 13,
  # which dropped update-notifier-common entirely (apt-config-auto-update
  # replaces only its APT config half, not this). Same path either way.
  elif [ -x /usr/share/update-notifier/notify-reboot-required ]; then
    REBOOT=false

  else
    # No flag mechanism installed. The running kernel can still be compared
    # against the newest one on disk, which needs no package at all and catches
    # the case that matters most.
    #
    # sort -V, not sort: 6.12.94 sorts AFTER 6.12.107 as text, which would call
    # a freshly-booted host stale on every Debian point release.
    #
    # This can only prove a reboot IS needed. An unchanged kernel says nothing
    # about an openssl update wanting its services restarted, so it stays null
    # rather than claiming false -- a weaker signal must not masquerade as the
    # complete one above.
    KRUN=$(uname -r)
    KNEW=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's|.*/vmlinuz-||' | sort -V | tail -1)
    if [ -n "$KNEW" ] && [ "$KNEW" != "$KRUN" ]; then
      REBOOT=true
      DET="${DET:+$DET; }running kernel $KRUN, newest installed $KNEW"
    fi
  fi
  ;;
dnf5|dnf)
  # Verified on Fedora 43: exit 1 with "Reboot is required to fully utilize
  # these updates", 0 when none is needed. It loads repository metadata first,
  # which is why this belongs in the daily job and nowhere near the collector.
  rc=0; $DNF -q needs-restarting -r >/dev/null 2>&1 || rc=$?
  case "$rc" in 0) REBOOT=false ;; 1) REBOOT=true ;; esac
  ;;
zypper-*)
  # NOT `zypper needs-restarting`. The zypper-needs-restarting package ships a
  # standalone /usr/bin/needs-restarting -- a dnf compatibility shim -- and no
  # zypper subcommand at all, so `zypper needs-restarting` answers "Unknown
  # command" even with the package installed. Verified on the Tumbleweed host,
  # where rpm -ql lists exactly the binary and its man page.
  #
  # Being a dnf shim, it takes dnf's convention: -r exits 0 for "no reboot
  # needed" and 1 for "reboot required". Any other status leaves this null.
  if command -v needs-restarting >/dev/null 2>&1; then
    rc=0; needs-restarting -r >/dev/null 2>&1 || rc=$?
    case "$rc" in 0) REBOOT=false ;; 1) REBOOT=true ;; esac
  fi
  ;;
slackpkg)
  # /boot/vmlinuz-* is no use here: Slackware names them vmlinuz-generic-5.15.19
  # and vmlinuz-huge-5.15.19, so the comparison the apt branch makes would call
  # every host stale. The installed kernel package carries the version cleanly
  # (kernel-generic-5.15.223-x86_64-1; -smp on 32-bit, which uname -r repeats).
  # As there, this can only prove a reboot IS needed, so a match stays null.
  KRUN=$(uname -r | sed 's/-smp$//')
  KNEW=$(ls /var/log/packages 2>/dev/null | awk -F- '
    /^kernel-(generic|huge)(-smp)?-[0-9]/ { print $(NF-2) }' | sort -V | tail -1)
  if [ -n "$KNEW" ] && [ "$KNEW" != "$KRUN" ]; then
    REBOOT=true
    DET="${DET:+$DET; }running kernel $KRUN, newest installed $KNEW"
  fi
  ;;
esac

# checked_at means "when was this answer last known current".
#
# A successful refresh settles that: it is now, whether or not anything on disk
# changed. Using the cache's own mtime instead was wrong for dnf, which leaves
# /var/cache/libdnf5 untouched when the metadata it fetched turns out to be
# unchanged -- a Fedora host checking hourly reported checked_at as 344 minutes
# old, and at 48h would have started showing "unknown" while working perfectly.
#
# The metadata timestamp is the fallback for the cases where this run did NOT
# bring anything up to date: NETDASH_PATCH_REFRESH="no", or a refresh that
# failed. There the counts really are as old as the cache, and dating them to it
# lets them age into "unknown" rather than posing as current.
if [ "$REFRESHED" -eq 1 ]; then
  CHECKED=$NOW
elif [ -n "$META" ]; then
  CHECKED=$META
else
  # Neither a fresh refresh nor a datable cache: leaving the previous state file
  # in place lets it age out, which beats overwriting it with a lie.
  echo "netdash-patchcheck: refresh failed; keeping the previous result" >&2
  exit 1
fi
[ "$REFRESH_FAILED" -eq 1 ] && DET="${DET:+$DET; }metadata refresh failed"

JSON=$(printf '{"security":%s,"other":%s,"reboot_required":%s,"checked_at":%d,"source":"%s","detail":"%s","packages":"%s"}' \
  "$SEC" "$OTH" "$REBOOT" "$CHECKED" "$SRC" "$DET" "$SECPKGS")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

# Written whole, then moved into place: the collector reads this file every
# 30-60s and must never catch a half-written one.
mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
