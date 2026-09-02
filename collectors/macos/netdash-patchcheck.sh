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

SUPLIST=/Library/Preferences/com.apple.SoftwareUpdate

# World-readable, so this works without root. `defaults read` exits non-zero
# for a key that is not present -- which is the normal state for a Mac that has
# never found an update -- so every read is guarded.
sud() { defaults read "$SUPLIST" "$1" 2>/dev/null || true; }

AUTO=$(sud AutomaticCheckEnabled)

# A Mac with automatic checks switched off never updates these counts, so a
# neglected machine would otherwise report a confident, permanent zero. There
# is no cached answer to read, so this is the one case where a scan is worth
# its cost -- once a day, and only here.
if [ "$AUTO" = "0" ] && [ "$REFRESH" = yes ]; then
  softwareupdate -l >/dev/null 2>&1 || true
fi

ALL=$(sud LastUpdatesAvailable)
REC=$(sud LastRecommendedUpdatesAvailable)
DATE=$(sud LastSuccessfulDate)

# LastSuccessfulDate IS checked_at: it is the moment the system last completed
# a scan. When automatic checks are disabled it simply stops advancing, and the
# dashboard ages the host into "unknown" with no special case needed here.
CHECKED=""
if [ -n "$DATE" ]; then
  # `defaults` prints "2026-08-30 06:12:44 +0000"; BSD date parses it back.
  CHECKED=$(date -j -f "%Y-%m-%d %H:%M:%S %z" "$DATE" +%s 2>/dev/null || true)
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

JSON=$(printf '{"security":%s,"other":%s,"checked_at":%d,"source":"%s","detail":"%s"}' \
  "$SEC" "$OTH" "$CHECKED" "softwareupdate" "$DET")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
