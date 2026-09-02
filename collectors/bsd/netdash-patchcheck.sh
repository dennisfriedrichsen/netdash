#!/bin/sh
# netdash patch check -- FreeBSD, OpenBSD and NetBSD.
#
# Runs DAILY, not at collector cadence. Writes a small JSON file that
# netdash-collector reads back and inlines. See PATCH-CHECKS.md.
#
#   netdash-patchcheck.sh            # check, write the state file
#   netdash-patchcheck.sh --print    # check, print, write nothing
#
# The three BSDs answer three different questions, and two of them cannot
# answer at all for part of the system:
#   FreeBSD  packages (pkg audit, CVE-based) AND base (freebsd-update)
#   OpenBSD  base only (syspatch); packages carry no security classification
#   NetBSD   packages only (pkg_admin audit); base has no patch mechanism
set -eu

MODE="${1:-}"

PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/pkg/sbin:/usr/pkg/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
export LC_ALL=C

CONF="${NETDASH_CONF:-}"
if [ -z "$CONF" ]; then
  for c in /usr/local/etc/netdash/collector.conf /etc/netdash/collector.conf \
           /usr/pkg/etc/netdash/collector.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
  done
  CONF="${CONF:-/usr/local/etc/netdash/collector.conf}"
fi
[ -f "$CONF" ] && . "$CONF"

# A directory of the collector's own; see the note in the Linux patch check.
STATE="${NETDASH_PATCH_STATE:-/var/db/netdash-collector/patches.json}"
REFRESH="${NETDASH_PATCH_REFRESH:-yes}"

OSNAME=$(uname -s)
NOW=$(date +%s)
SEC=null
OTH=null
SRC=unknown
DET=""
CHECKED=""

run() { "$@" 2>/dev/null || true; }

# BSD stat(1), not GNU: -f %m, not -c %Y.
mtime() { [ -e "$1" ] && stat -f %m "$1" 2>/dev/null || true; }

case "$OSNAME" in
FreeBSD)
  SRC=pkg-audit
  # pkg audit reads /var/db/pkg/vuln.xml and touches the network only with -F.
  # The base system already refreshes it daily through
  # /usr/local/etc/periodic/security/410.pkg-audit when
  # daily_status_security_pkgaudit_enable="YES"; -F here covers hosts where
  # that is switched off.
  #
  # Its exit status is NOT a refresh signal: pkg audit exits 1 both when the
  # fetch failed and when it succeeded and found vulnerable packages. The
  # honest freshness signal is vuln.xml's own mtime, which a failed fetch
  # leaves untouched -- so a host that loses network ages into "unknown"
  # rather than reporting a stale count as current.
  [ "$REFRESH" = yes ] && run pkg audit -F -q >/dev/null
  [ "$REFRESH" = yes ] && run pkg update -q >/dev/null

  # pkg audit ends with an authoritative summary:
  #   14 problem(s) in 9 package(s) found.
  # Take the PACKAGE count, not the problem count. One chromium carrying 300
  # CVEs is a single upgrade, and counting advisories would let one busy package
  # swamp the badge -- the verified host showed 14 problems across 9 packages.
  AUDIT=$(run pkg audit)
  SEC=$(printf '%s\n' "$AUDIT" \
        | sed -n 's/^[0-9][0-9]* problem(s) in \([0-9][0-9]*\) package(s) found\..*/\1/p' | head -1)
  # Fallback for a release that words the summary differently: one
  # "<pkg> is vulnerable:" header is printed per affected package.
  [ -n "$SEC" ] || SEC=$(printf '%s\n' "$AUDIT" | grep -c 'is vulnerable' || true)

  # Out-of-date packages, a different question from "vulnerable" -- a package
  # can be behind with no advisory against it.
  OTH=$(run pkg version -vRL= | grep -c . || true)

  CHECKED=$(mtime /var/db/pkg/vuln.xml)

  # Base system. On a pkgbase host the base IS packages -- FreeBSD-runtime and
  # friends -- so pkg audit and pkg version above already cover it, and
  # freebsd-update manages nothing here. Running its fetch would download
  # binary patches for a base it does not own, so pkgbase is detected and that
  # whole branch skipped. (Verified: the test host runs pkgbase.)
  if pkg info -e FreeBSD-runtime >/dev/null 2>&1; then
    DET="pkgbase host: base system is covered by pkg, not freebsd-update"
    SRC="pkg-audit-pkgbase"

  # Otherwise base is separate. updatesready needs no network and exits 2 when
  # nothing is staged, 0 when fetched updates are ready.
  elif command -v freebsd-update >/dev/null 2>&1; then
    if [ "$REFRESH" = yes ]; then
      # What `freebsd-update cron` does, minus its random 1-3600s sleep, which
      # exists to spread a fleet across the hour and is wrong for a job that is
      # already on its own timer.
      run freebsd-update --not-running-from-cron fetch >/dev/null
    fi
    if freebsd-update updatesready >/dev/null 2>&1; then
      # Counted as one pending security item. The count is packages plus this,
      # so the detail line says where the extra one came from.
      SEC=$((SEC + 1))
      DET="includes 1 staged base system update (freebsd-update)"
      SRC="pkg-audit+freebsd-update"
    fi
  fi
  ;;

OpenBSD)
  SRC=syspatch
  # syspatch ships only security and reliability errata, so every patch it
  # lists counts as security. It REQUIRES root -- the source permits only -l
  # and usage unprivileged -- and fetches the patch index from the mirror on
  # every run. There is no local cache to read and /etc/daily does not run it,
  # so this daily job is the only thing that ever will, and the check time is
  # therefore genuinely now.
  if [ "$(id -u)" != 0 ]; then
    echo "netdash-patchcheck: syspatch -c needs root" >&2
    exit 1
  fi
  if ! OUT=$(syspatch -c 2>/dev/null); then
    echo "netdash-patchcheck: syspatch -c failed; keeping the previous result" >&2
    exit 1
  fi
  SEC=$(printf '%s' "$OUT" | grep -c . || true)
  CHECKED=$NOW

  # Packages are unclassified and need PKG_PATH pointing at a package set --
  # on -release that must be the -stable build, or security fixes never appear
  # at all. Skipped unless the admin has set PKG_PATH, because pkg_add -un
  # against an unset or wrong PKG_PATH is a slow way to learn nothing.
  if [ -n "${PKG_PATH:-}" ]; then
    OTH=$(run pkg_add -un | grep -c . || true)
  else
    DET="packages not checked (PKG_PATH unset)"
  fi
  ;;

NetBSD)
  SRC=pkg_admin-audit
  # No vulnerability database ships with the system, and on a stock NetBSD 11.0
  # install nothing ever fetches one: /etc/daily.conf does not set
  # fetch_pkg_vulnerabilities=YES by default (verified on the test host, where
  # the file was simply absent). So this daily job is normally the only thing
  # that will ever populate it, which is why the fetch runs before the audit
  # rather than trusting the system to have done it.
  [ "$REFRESH" = yes ] && run pkg_admin fetch-pkg-vulnerabilities >/dev/null

  # The file's mtime is the freshness signal, as with vuln.xml on FreeBSD, so a
  # failed fetch ages out instead of reporting a stale count as current.
  for f in /usr/pkg/pkgdb/pkg-vulnerabilities /var/db/pkg/pkg-vulnerabilities; do
    CHECKED=$(mtime "$f"); [ -n "$CHECKED" ] && break
  done
  # Checked before the audit, not after: `pkg_admin audit` against a missing
  # database prints "Cannot open ..." and exits, which counts as zero
  # vulnerabilities and is indistinguishable from a clean host. Bail with
  # something the admin can act on instead.
  if [ -z "$CHECKED" ]; then
    echo "netdash-patchcheck: no pkg-vulnerabilities database on this host, so" >&2
    echo "  packages cannot be audited. Fix either way:" >&2
    echo "    - run this as root with NETDASH_PATCH_REFRESH=yes (the default)" >&2
    echo "    - or set fetch_pkg_vulnerabilities=YES in /etc/daily.conf" >&2
    exit 1
  fi

  # Audits installed packages against that local file; no network. One line per
  # finding:
  #   Package perl-5.42.3 has a symlink-attack vulnerability, see https://...
  # Deduplicated by package name so this counts the same thing FreeBSD's
  # "N package(s) found" does -- packages needing an upgrade, not advisories.
  # A package carrying three CVEs is still one action, and on FreeBSD one
  # chromium alone accounts for hundreds.
  #
  # Match on 'vulnerability' exactly. A lenient 'vulnerabilit' also matches
  # pkg-vulnerabilities in the "Cannot open" error, turning a host with no
  # database into a host with one finding.
  SEC=$(run pkg_admin audit | awk '/vulnerability/ {print $2}' | sort -u | grep -c . || true)

  # NetBSD has no syspatch or freebsd-update equivalent: base security fixes
  # mean rebuilding from source or installing new sets. Saying so beats a
  # silent zero that reads as "base is clean".
  DET="base system not checked (NetBSD ships no binary patch mechanism)"

  # pkgin is optional and its summary wording varies between versions, so an
  # unmatched line leaves the count null rather than guessing at zero.
  if command -v pkgin >/dev/null 2>&1; then
    N=$(run pkgin -n upgrade | sed -n 's/^\([0-9][0-9]*\) packages* to upgrade.*/\1/p' | head -1)
    [ -n "$N" ] && OTH=$N
  fi
  ;;
esac

if [ "$SRC" = unknown ]; then
  echo "netdash-patchcheck: unsupported BSD: $OSNAME" >&2
  exit 1
fi

# ---- Reboot required ----
# null means "no mechanism to ask", never "no reboot needed".
# FreeBSD: freebsd-version -k reports the INSTALLED kernel, uname -r the
# RUNNING one, so a difference means a kernel was updated and not yet booted --
# which is the whole point on a pkgbase host, where the kernel arrives as an
# ordinary package upgrade with nothing to announce it.
# OpenBSD and NetBSD have no equivalent, so they stay null.
REBOOT=null
if [ "$OSNAME" = FreeBSD ] && command -v freebsd-version >/dev/null 2>&1; then
  KINST=$(freebsd-version -k 2>/dev/null || true)
  KRUN=$(uname -r)
  if [ -n "$KINST" ]; then
    if [ "$KINST" = "$KRUN" ]; then REBOOT=false; else REBOOT=true; fi
  fi
fi

# Never write a reading that cannot be honestly dated: leaving the previous
# state file in place lets it age into "unknown" on the dashboard, which is the
# truthful outcome. Writing `now` over it would claim a stale count is current.
if [ -z "$CHECKED" ]; then
  echo "netdash-patchcheck: no vulnerability database found; keeping the previous result" >&2
  exit 1
fi

JSON=$(printf '{"security":%s,"other":%s,"reboot_required":%s,"checked_at":%d,"source":"%s","detail":"%s"}' \
  "$SEC" "$OTH" "$REBOOT" "$CHECKED" "$SRC" "$DET")

if [ "$MODE" = "--print" ]; then printf '%s\n' "$JSON"; exit 0; fi

mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$JSON" > "$STATE.tmp"
mv "$STATE.tmp" "$STATE"
chmod 644 "$STATE"
