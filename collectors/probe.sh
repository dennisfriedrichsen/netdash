#!/bin/sh
# netdash platform probe -- read-only. Dumps the raw output the collector parses,
# so a new OS can be supported against real output instead of an assumption.
#
#   sh probe.sh            # print
#   sh probe.sh > out.txt  # capture and send
#
# Reads nothing sensitive: no config, no tokens, no file contents beyond
# /etc/os-release and kernel counters.
set -u

sec() { printf '\n===== %s =====\n' "$1"; }
try() { printf '\n--- $ %s\n' "$*"; "$@" 2>&1 | head -40 || echo "(failed: $?)"; }

sec "identity"
try uname -a
try uname -s
try uname -m
try uname -r
[ -r /etc/os-release ] && { printf '\n--- /etc/os-release\n'; cat /etc/os-release; }
[ -r /etc/alpine-release ] && { printf '\n--- /etc/alpine-release\n'; cat /etc/alpine-release; }

sec "shell + tools"
for t in sh awk sed tr df mount sysctl curl wget fetch ftp crontab systemctl rc-update zfs; do
  p=$(command -v "$t" 2>/dev/null) && printf '  %-10s %s\n' "$t" "$p" || printf '  %-10s -\n' "$t"
done
printf '\n--- awk implementation\n'; awk --version 2>&1 | head -2 || awk -W version 2>&1 | head -2
printf '\n--- 32-bit awk check (must print 4110864384, not -2147483648)\n'
echo | awk 'END{ printf "  %%d   -> %d\n  %%.0f -> %.0f\n", 4014516*1024, 4014516*1024 }'

sec "cpu"
try cat /proc/stat
try sysctl -n kern.cp_time
try sysctl kern.cp_time

sec "memory"
try cat /proc/meminfo
try sysctl -n hw.physmem
try sysctl -n hw.physmem64
try sysctl -n hw.usermem
try sysctl vm.uvmexp
try sysctl vm.uvmexp2
try sysctl -n vm.stats.vm.v_page_size
try sysctl -n vm.stats.vm.v_free_count
try sysctl -n vm.stats.vm.v_inactive_count

# OpenBSD refuses vm.uvmexp through sysctl and says to use vmstat; NetBSD is
# expected to be the same shape. These are the fields the collector parses.
try vmstat
try vmstat -s
try top -b
printf '\n--- $ sysctl -a | grep -i uvm (available uvm nodes)\n'
sysctl -a 2>/dev/null | grep -i uvm | head -30 || echo "(none)"

sec "uptime / boottime"
try cat /proc/uptime
try sysctl -n kern.boottime
try sysctl kern.boottime
try date +%s

sec "disk"
try df -P -k
try df -k
try df -k -l
try df -k -T
try df -k -T -l
try df -P -B1
printf '\n--- which df options are rejected?\n'
for o in "-l" "-x tmpfs" "-T" "-B1" "-P"; do
  printf '  df %-10s ' "$o"
  df $o >/dev/null 2>&1 && echo OK || echo "REJECTED"
done
try cat /proc/self/mounts
try mount

sec "hardware model (SBC detection)"
for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
  [ -r "$f" ] && { printf '\n--- %s\n' "$f"; tr -d '\000' < "$f"; echo; }
done

sec "patch status"
# Read-only, exactly like the rest of this probe: nothing here refreshes package
# metadata, fetches a vulnerability database or downloads a patch. The daily
# netdash-patchcheck does those; this only shows what they would read.
#
# What each of these is meant to reveal is in PATCH-CHECKS.md. The point of
# capturing them is that the documented behaviour and the real behaviour have
# disagreed on this project before -- df -l on NetBSD, vm.uvmexp on OpenBSD.

printf '\n--- package managers present\n'
for t in apt-get dnf5 dnf pacman checkupdates arch-audit zypper apk \
         pkg freebsd-update syspatch pkg_admin pkgin softwareupdate; do
  p=$(command -v "$t" 2>/dev/null) && printf '  %-14s %s\n' "$t" "$p" || printf '  %-14s -\n' "$t"
done

# --- Debian/Ubuntu/Raspbian: the security marker is inside the parentheses ---
if command -v apt-get >/dev/null 2>&1; then
  printf '\n--- $ apt-get --just-print dist-upgrade | grep ^Inst   (first 25)\n'
  I=$(apt-get --just-print -o Debug::NoLocking=true dist-upgrade 2>/dev/null | grep '^Inst' || true)
  [ -n "$I" ] && printf '%s\n' "$I" | head -25 || echo "(none pending)"
  printf '\n--- apt metadata age\n'
  for f in /var/lib/apt/periodic/update-success-stamp /var/lib/apt/lists; do
    [ -e "$f" ] && printf '  %-48s %s\n' "$f" "$(stat -c %Y "$f" 2>/dev/null)"
  done
  printf '  %-48s %s\n' "now" "$(date +%s)"
  printf '\n--- is apt-check present? (Ubuntu only, not used, checking the claim)\n'
  ls -l /usr/lib/update-notifier/apt-check 2>&1 | head -1
fi

# --- Fedora: is anything keeping the metadata cache warm under dnf5? ---
if command -v dnf5 >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
  D=$(command -v dnf5 2>/dev/null || command -v dnf)
  try "$D" -q --security check-update
  printf '  (exit %s -- 100 means security updates pending, 0 means none)\n' "$?"
  try "$D" -q check-update
  printf '\n--- makecache timer present? (dnf4 had one; dnf5 may not)\n'
  systemctl list-timers '*makecache*' --no-pager 2>/dev/null | head -5 || echo "(no systemd)"
fi

# --- Arch: neither tool is in base ---
command -v checkupdates >/dev/null 2>&1 && try checkupdates
command -v arch-audit   >/dev/null 2>&1 && try arch-audit -uq

# --- openSUSE: Tumbleweed has no patch metadata at all, Leap does ---
if command -v zypper >/dev/null 2>&1; then
  printf '\n--- os-release ID (tumbleweed/slowroll => rolling, count only)\n'
  ( . /etc/os-release 2>/dev/null; echo "  ID=${ID:-?}  NAME=${NAME:-?}" )
  try zypper --non-interactive --no-refresh patch-check
  try zypper --non-interactive --no-refresh --quiet list-patches --category security
  try zypper --non-interactive --no-refresh --quiet list-updates
fi

# --- Alpine: which spelling does this apk understand? ---
if command -v apk >/dev/null 2>&1; then
  try apk list --upgradable
  try apk version -l '<'
fi

# --- FreeBSD: both reads are local; -F is NOT run here ---
if command -v pkg >/dev/null 2>&1; then
  try pkg audit
  try pkg version -vRL=
  printf '\n--- vuln.xml age (this is checked_at on FreeBSD)\n'
  ls -l /var/db/pkg/vuln.xml 2>&1 | head -1
fi
if command -v freebsd-update >/dev/null 2>&1; then
  printf '\n--- $ freebsd-update updatesready\n'
  freebsd-update updatesready 2>&1 | head -5
  printf '  (exit %s -- 0 means updates are staged, 2 means none)\n' "$?"
fi

# --- OpenBSD: needs root AND hits the mirror, so it may well fail here ---
if command -v syspatch >/dev/null 2>&1; then
  try syspatch -l
  printf '\n--- $ syspatch -c   (needs root; fetches the index from the mirror)\n'
  syspatch -c 2>&1 | head -20
fi

# --- NetBSD: audit is local, the fetch is not and is not run here ---
if command -v pkg_admin >/dev/null 2>&1; then
  try pkg_admin audit
  printf '\n--- pkg-vulnerabilities age (this is checked_at on NetBSD)\n'
  for f in /usr/pkg/pkgdb/pkg-vulnerabilities /var/db/pkg/pkg-vulnerabilities; do
    ls -l "$f" 2>/dev/null
  done
  printf '\n--- is the daily fetch enabled?\n'
  grep -i 'fetch_pkg_vulnerabilities' /etc/daily.conf 2>/dev/null || echo "(not set in /etc/daily.conf)"
fi
command -v pkgin >/dev/null 2>&1 && try pkgin -n upgrade

# --- macOS: read the cached scan; softwareupdate -l is NOT run (it is slow) ---
if [ -r /Library/Preferences/com.apple.SoftwareUpdate.plist ]; then
  printf '\n--- com.apple.SoftwareUpdate cached scan\n'
  for k in AutomaticCheckEnabled LastUpdatesAvailable \
           LastRecommendedUpdatesAvailable LastSuccessfulDate; do
    printf '  %-32s %s\n' "$k" \
      "$(defaults read /Library/Preferences/com.apple.SoftwareUpdate "$k" 2>&1 | head -1)"
  done
  printf '\n--- RecommendedUpdates\n'
  defaults read /Library/Preferences/com.apple.SoftwareUpdate RecommendedUpdates 2>&1 | head -30
fi

printf '\n===== end =====\n'
