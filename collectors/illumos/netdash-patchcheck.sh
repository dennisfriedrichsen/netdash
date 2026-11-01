#!/bin/sh
# netdash patch check -- illumos (OmniOS, and other IPS distributions).
#
# Runs DAILY, not at collector cadence. Writes a small JSON file that
# netdash-collector reads back and inlines. See PATCH-CHECKS.md.
#
#   netdash-patchcheck.sh            # check, write the state file
#   netdash-patchcheck.sh --print    # check, print, write nothing
#
# IPS answers "how many packages would an update change", and whether the
# machine is running the boot environment it will boot next. It does not
# classify anything as security: OmniOS ships its security fixes in the
# weekly updates to each release, alongside everything else.
set -eu

# Kept in sync with the repository VERSION file by tests/run.sh.
NETDASH_VERSION="0.5.1"

MODE="${1:-}"

PATH="$PATH:/usr/sbin:/usr/bin:/sbin:/opt/ooce/bin:/usr/local/bin"
export PATH
export LC_ALL=C

CONF="${NETDASH_CONF:-}"
if [ -z "$CONF" ]; then
  for c in /etc/netdash/collector.conf /usr/local/etc/netdash/collector.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
  done
  CONF="${CONF:-/etc/netdash/collector.conf}"
fi
[ -f "$CONF" ] && . "$CONF"

STATE="${NETDASH_PATCH_STATE:-/var/lib/netdash-collector/patches.json}"
NOW=$(date +%s)
if command -v nawk >/dev/null 2>&1; then AWK=nawk; else AWK=awk; fi

# ---- Packages behind ----
# `pkg update -n` plans the update without making it: it refreshes the
# publishers' catalogues, runs the solver, and says what it would change.
# Not `pkg list -u`, which lists every installed package that has any newer
# version in the catalogue -- including versions the installed `entire`
# incorporation will never let it move to, so it can count updates that
# `pkg update` would not make.
#
# Exit 4 is pkg(1)'s "nothing to do"; with it, OmniOS r151054 prints "No
# updates available for this image." (seen on the test host). Exit 0 is a plan,
# whose summary carries the count:
#            Packages to update:  12
# Anything else -- 1, an unreachable publisher, a locked image -- is a check
# that did not happen: the previous result is kept to age into unknown.
#
# It always refreshes the catalogues first. NETDASH_PATCH_REFRESH has nothing
# to switch off here.
rc=0
OUT=$(pkg update -n 2>&1) || rc=$?
case "$rc" in
  4) OTH=0 ;;
  0)
    OTH=$(printf '%s\n' "$OUT" | $AWK -F: '
      $1 ~ /^[ \t]*Packages to update$/ { gsub(/[ \t]/, "", $2); n = $2 }
      END { if (n ~ /^[0-9]+$/) print n; else print "null" }')
    ;;
  *)
    echo "netdash-patchcheck: pkg update -n failed (exit $rc); keeping the previous result" >&2
    printf '%s\n' "$OUT" | tail -3 >&2
    exit 1
    ;;
esac

# ---- Reboot required ----
# An IPS update to the OS lands in a new boot environment, activated for the
# next boot; the running one is untouched until then. beadm marks the BE in use
# now with N and the one the machine will boot with R. `beadm list -H` is
# "name;uuid;flags;mountpoint;space;policy;created". One BE flagged NR: nothing
# pending. An R anywhere without the N: an update (or `beadm activate`) is
# waiting for a reboot. No beadm, or flags it does not recognise: null.
REBOOT=null
if command -v beadm >/dev/null 2>&1; then
  REBOOT=$(beadm list -H 2>/dev/null | $AWK -F';' '
    $3 ~ /R/ && $3 ~ /N/ { nr = 1 }
    $3 ~ /R/ && $3 !~ /N/ { r = 1 }
    END { if (r) print "true"; else if (nr) print "false"; else print "null" }' || echo null)
fi

DET="IPS has no security classification: pending updates are counted, none as security"
JSON=$(printf '{"security":null,"other":%s,"reboot_required":%s,"checked_at":%d,"source":"ips-update","detail":"%s","packages":""}' \
  "$OTH" "$REBOOT" "$NOW" "$DET")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
