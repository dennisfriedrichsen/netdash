#!/bin/sh
# netdash patch check -- Linux (apt, dnf, pacman, zypper, apk).
#
# Runs DAILY, not at collector cadence: every backend below either hits the
# network or parses the whole package database, which is untenable every 30s.
# It writes a small JSON file that netdash-collector reads back and inlines.
#
#   netdash-patchcheck.sh            # check, write the state file
#   netdash-patchcheck.sh --print    # check, print, write nothing
#
# See PATCH-CHECKS.md for what each package manager can and cannot answer.
set -eu

MODE="${1:-}"

PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
export DEBIAN_FRONTEND=noninteractive LC_ALL=C

CONF="${NETDASH_CONF:-/etc/netdash/collector.conf}"
[ -f "$CONF" ] && . "$CONF"

STATE="${NETDASH_PATCH_STATE:-/var/lib/netdash/patches.json}"
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
REFRESH_OK=1

# Backends legitimately fail (no network, a repo down, a lock held by the
# admin's own upgrade). Under `set -e` a bare failing command aborts the whole
# script, so every one of them is guarded.
run() { "$@" 2>/dev/null || true; }

mtime() { [ -e "$1" ] && stat -c %Y "$1" 2>/dev/null || true; }

# ---------------------------------------------------------------- apt ----
if command -v apt-get >/dev/null 2>&1; then
  SRC=apt
  if [ "$REFRESH" = yes ]; then
    # NoLocking so a concurrent unattended-upgrades run cannot make this fail,
    # and -qq to keep a daily cron job silent.
    apt-get -qq -o Debug::NoLocking=true update >/dev/null 2>&1 || REFRESH_OK=0
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
  set -- $(run apt-get --just-print -o Debug::NoLocking=true dist-upgrade | awk '
    /^Inst / { n++; s=$0; sub(/^[^(]*\(/,"",s); if (s ~ /-security/) sec++ }
    END { printf "%d %d\n", sec+0, n-sec+0 }')
  SEC=${1:-0}; OTH=${2:-0}

# ---------------------------------------------------------------- dnf ----
elif command -v dnf5 >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
  if command -v dnf5 >/dev/null 2>&1; then DNF=dnf5; else DNF=dnf; fi
  SRC=$DNF
  if [ "$REFRESH" = yes ]; then
    # dnf4 kept metadata fresh with dnf-makecache.timer; under dnf5 that timer
    # is not guaranteed to exist, so refresh rather than assume.
    $DNF -q makecache >/dev/null 2>&1 || REFRESH_OK=0
  fi
  # check-update exits 100 when updates are pending and 0 when none are, so the
  # exit status carries the answer and the output is only good for counting.
  # Rows are "name.arch  version  repo"; blank lines and the "Obsoleting
  # Packages" section are dropped by requiring three fields.
  count_dnf() { run $DNF -q check-update "$@" | awk 'NF==3 && $1 ~ /\./ {n++} END{print n+0}'; }
  for d in /var/cache/libdnf5 /var/cache/dnf; do
    META=$(mtime "$d"); [ -n "$META" ] && break
  done
  SEC=$(count_dnf --security)
  ALL=$(count_dnf)
  OTH=$((ALL - SEC)); [ "$OTH" -ge 0 ] || OTH=0

# ------------------------------------------------------------- pacman ----
elif command -v pacman >/dev/null 2>&1; then
  SRC=pacman
  # Arch publishes no security classification in the repositories, so this
  # needs two tools and neither is in base. checkupdates syncs to a temporary
  # database rather than /var/lib/pacman/sync, so it cannot leave a partial
  # -Sy behind for a later -Su to turn into a broken upgrade.
  if command -v checkupdates >/dev/null 2>&1; then
    OTH=$(run checkupdates | grep -c . || true)
  else
    DET="install pacman-contrib for update counts"
  fi
  # arch-audit reports installed packages carrying Arch Security Team
  # advisories. Absent, SEC stays null -- "nobody classified this" -- rather
  # than 0, which would claim the host had been checked and found clean.
  if command -v arch-audit >/dev/null 2>&1; then
    SEC=$(run arch-audit -uq | grep -c . || true)
    SRC="pacman+arch-audit"
  elif [ -z "$DET" ]; then
    DET="install arch-audit to classify security updates"
  fi

# ------------------------------------------------------------- zypper ----
elif command -v zypper >/dev/null 2>&1; then
  ID=$( . /etc/os-release 2>/dev/null; echo "${ID:-}" )
  if [ "$REFRESH" = yes ]; then
    zypper --non-interactive --quiet refresh >/dev/null 2>&1 || REFRESH_OK=0
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
    apk update >/dev/null 2>&1 || REFRESH_OK=0
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
fi

if [ "$SRC" = unknown ]; then
  echo "netdash-patchcheck: no supported package manager found" >&2
  exit 1
fi

# checked_at means "when was this answer last known current", which is a
# property of the package metadata rather than of when this script happened to
# run. So the metadata's own timestamp always wins where one exists: after a
# successful refresh it is a second ago and nothing changes, but when the
# refresh failed -- or was never attempted, because
# NETDASH_PATCH_REFRESH="no" -- it is the truth, and the reading ages out into
# "unknown" on the dashboard instead of posing as current.
if [ -n "$META" ]; then
  CHECKED=$META
elif [ "$REFRESH_OK" -eq 1 ]; then
  # No timestamp to read, but the refresh did just succeed, so now is honest.
  CHECKED=$NOW
else
  # Neither a fresh refresh nor a datable database: leaving the previous state
  # file in place lets it age out, which beats overwriting it with a lie.
  echo "netdash-patchcheck: refresh failed; keeping the previous result" >&2
  exit 1
fi
[ "$REFRESH_OK" -eq 0 ] && DET="${DET:+$DET; }metadata refresh failed"

JSON=$(printf '{"security":%s,"other":%s,"checked_at":%d,"source":"%s","detail":"%s"}' \
  "$SEC" "$OTH" "$CHECKED" "$SRC" "$DET")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

# Written whole, then moved into place: the collector reads this file every
# 30-60s and must never catch a half-written one.
mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
