#!/bin/sh
# netdash patch check -- macOS.
#
# Runs DAILY, not at collector cadence. Writes a small JSON file that
# netdash-collector reads back and inlines. See PATCH-CHECKS.md.
#
#   netdash-patchcheck.sh            # check, write the state file
#   netdash-patchcheck.sh --print    # check, print, write nothing
#
# `softwareupdate -l` is NOT run here by default: it triggers a full network
# scan that takes tens of seconds, and macOS already scans every six hours on
# its own. This reads the result the system cached from that scan.
set -eu

MODE="${1:-}"

PATH="$PATH:/usr/local/sbin:/usr/local/bin:/opt/homebrew/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
export LC_ALL=C

CONF="${NETDASH_CONF:-/usr/local/etc/netdash/collector.conf}"
[ -f "$CONF" ] || CONF="/opt/homebrew/etc/netdash/collector.conf"
[ -f "$CONF" ] && . "$CONF"

# Under `brew services` this runs as the user, not root, so the state file goes
# in Homebrew's prefix rather than /var/lib.
if [ -z "${NETDASH_PATCH_STATE:-}" ]; then
  PREFIX=$(brew --prefix 2>/dev/null || echo /usr/local)
  STATE="$PREFIX/var/netdash/patches.json"
else
  STATE="$NETDASH_PATCH_STATE"
fi
REFRESH="${NETDASH_PATCH_REFRESH:-yes}"
NOW=$(date +%s)

SUPLIST=/Library/Preferences/com.apple.SoftwareUpdate

# World-readable, so this works without root. `defaults read` exits non-zero
# for a key that is not present -- which is the normal state for a Mac that has
# never found an update, and on macOS 26 is also the normal state for
# AutomaticCheckEnabled -- so every read is guarded.
sud() { defaults read "$SUPLIST" "$1" 2>/dev/null || true; }

# LastSuccessfulDate IS checked_at: the moment the system last completed a scan.
# macOS runs one every six hours on its own, so this is normally minutes to
# hours old, and it stops advancing the moment scanning stops -- which is what
# ages a neglected Mac into "unknown" on the dashboard.
read_su() {
  ALL=$(sud LastUpdatesAvailable)
  REC=$(sud LastRecommendedUpdatesAvailable)
  DATE=$(sud LastSuccessfulDate)
  CHECKED=""
  # `defaults` prints "2026-09-02 17:04:55 +0000"; BSD date parses it back.
  [ -n "$DATE" ] && CHECKED=$(date -j -f "%Y-%m-%d %H:%M:%S %z" "$DATE" +%s 2>/dev/null || true)
}
read_su

# Whether to force a scan. `softwareupdate -l` costs tens of seconds and a
# network round trip, so it runs only when there is no usable cached answer.
#
# AutomaticCheckEnabled is checked but NOT relied on: it does not exist at all
# on macOS 26 (verified), where its absence says nothing either way. The
# durable signal is the age of the last successful scan -- if the system has
# not managed one in a day, something is stopping it, whatever the preference
# is called on that release, and this is the one case where the scan earns its
# cost.
AUTO=$(sud AutomaticCheckEnabled)
NEED_SCAN=0
[ "$AUTO" = "0" ] && NEED_SCAN=1
[ -z "$CHECKED" ] && NEED_SCAN=1
[ -n "$CHECKED" ] && [ $(( NOW - CHECKED )) -gt 86400 ] && NEED_SCAN=1

if [ "$NEED_SCAN" -eq 1 ] && [ "$REFRESH" = yes ]; then
  softwareupdate -l >/dev/null 2>&1 || true
  read_su
fi

if [ -z "$CHECKED" ]; then
  echo "netdash-patchcheck: no LastSuccessfulDate; has this Mac ever checked?" >&2
  exit 1
fi

# "Recommended" is the closest thing macOS has to a security classification;
# Rapid Security Responses appear in the same list. Homebrew packages are a
# separate question and deliberately not reported.
SEC=${REC:-0}
OTH=$(( ${ALL:-0} - SEC )); [ "$OTH" -ge 0 ] || OTH=0

DET=""
[ "$AUTO" = "0" ] && DET="automatic update checks are disabled on this Mac"
[ $(( NOW - CHECKED )) -gt 86400 ] && DET="${DET:+$DET; }last successful scan was over a day ago"

JSON=$(printf '{"security":%s,"other":%s,"checked_at":%d,"source":"%s","detail":"%s"}' \
  "$SEC" "$OTH" "$CHECKED" "softwareupdate" "$DET")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
