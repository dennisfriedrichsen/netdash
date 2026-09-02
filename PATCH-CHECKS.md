# Patch status: what each platform can actually tell you

Notes behind the **Patches** indicator on each card. Recorded here because the
per-platform differences are not guessable, several of the obvious commands are
wrong in ways that fail silently, and the useful answer on three of these
platforms is "the OS cannot tell you that".

Everything below is from man pages and source unless marked **verified**, which
means it was run against a real host and the output pasted in.

## "Up to date with security patches" is three different questions

No single number is comparable across these platforms, because they do not
answer the same question:

| question | platforms that answer it |
|---|---|
| pending updates, classified as security | Debian, Ubuntu, Raspbian, Fedora, openSUSE Leap, macOS |
| installed packages with known CVEs | FreeBSD, NetBSD, Arch (with `arch-audit`) |
| base-system patches pending, separate from packages | FreeBSD, OpenBSD |
| "N packages behind", no security classification at all | Alpine, Arch (base), openSUSE Tumbleweed, OpenBSD packages |

A host reporting `security: 0` therefore means something different on Alpine
(nothing knows how to classify) than on Debian (apt looked, and there are none).
The payload carries `source` so the UI can say which was meant.

Two of these are worth stating plainly because they are gaps, not oversights:

- **NetBSD has no base-system patch mechanism.** There is no `syspatch` or
  `freebsd-update` equivalent; base security fixes mean rebuilding from source
  or installing new sets. netdash reports NetBSD *packages* and says nothing
  about base, rather than implying base is clean.
- **openSUSE Tumbleweed ships no patch metadata.** Patches are a Leap/SLE
  concept, so `zypper list-patches` is empty there by design — not because the
  host is current. Tumbleweed can only ever report a count.

## Two failure modes this design exists to avoid

**A stale check reads as green.** "0 pending" from package metadata last
refreshed in March is the worst possible output for a security indicator: it is
indistinguishable from a patched host. Every check therefore records
`checked_at`, and the server renders anything older than `patch_stale_hours`
as *unknown* — never as OK. On macOS the same trap has a second door:
`AutomaticCheckEnabled` being false freezes the cached counts forever, so that
key gates the whole reading.

**The check is far too expensive for the collector.** Every mechanism below
either hits the network or parses the entire package database; the fast ones
still take seconds. At a 30–60s collector cadence that is untenable. So the
work is split: a separate daily `netdash-patchcheck` writes a small JSON file,
and the collector — which must stay fast — only reads that file back.

```
netdash-patchcheck  (daily, root, network)  ->  patches.json
netdash-collector   (every 30-60s)          ->  reads patches.json, inlines it
```

The state file lives at `/var/lib/netdash/patches.json` on Linux,
`/var/db/netdash/patches.json` on the BSDs, and `<brew --prefix>/var/netdash/`
on macOS. All three collectors search every one of those paths, so a
mis-guessed platform convention degrades to *unknown*, not to a wrong reading.

## Per platform

The division that matters is which half is cheap. Where the OS already
refreshes on its own schedule, netdash reads what is there and schedules
nothing.

| platform | cheap local read | refresh (daily job) | already refreshed by the OS? |
|---|---|---|---|
| Debian / Ubuntu / Raspbian | `apt-get --just-print upgrade` | `apt-get update` | only with `unattended-upgrades` |
| Fedora | `dnf5 check-update --security` | `dnf5 makecache` | dnf4 yes; dnf5 **check the host** |
| Arch | — | `checkupdates`, `arch-audit` | no |
| openSUSE Tumbleweed | `zypper --no-refresh list-updates` **(verified)** | `zypper refresh` | no |
| Alpine | `apk list --upgradable` | `apk update` | no |
| FreeBSD | `pkg audit -q`, `freebsd-update updatesready` | none needed | **yes, both** |
| OpenBSD | — | `syspatch -c` | no |
| NetBSD | `pkg_admin audit` | `pkg_admin fetch-pkg-vulnerabilities` | yes, if enabled |
| macOS | read `com.apple.SoftwareUpdate.plist` | none needed | **yes, every 6h** |
| TrueNAS CORE | `update.check_available` (server-side) | n/a | n/a |

### Debian, Ubuntu, Raspbian — **verified** on Debian 13

The security classification is in the origin annotation of the `Inst` line, not
anywhere in `apt list --upgradable`:

```
Inst openssl (3.5.7-1~deb13u2 Debian-Security:13/stable-security [amd64])
Inst zlib1g (1:1.3.dfsg+really1.3.1-1+b1 Debian:13.6/stable [amd64])
```

Ubuntu spells it `Ubuntu:24.04/noble-security`, so testing the annotation for
`-security` covers both. **Match inside the parentheses, not the whole line** —
a package whose name contains `security` is otherwise counted as a security
update. The parse is `sub(/^[^(]*\(/, "")` and then the test, which is safe
because a package name cannot contain `(`.

`apt-get --just-print upgrade` works fine unprivileged; only the refresh needs
root. Metadata age comes from `/var/lib/apt/periodic/update-success-stamp` when
it exists (it is written by `unattended-upgrades`, not by apt itself) and
otherwise from the mtime of `/var/lib/apt/lists`.

Not used: `/usr/lib/update-notifier/apt-check`, which prints a tidy
`updates;security` pair but is Ubuntu-only — **verified** absent on Debian 13.

### Fedora

`dnf5 check-update --security` exits 100 when security updates are pending, 0
when none are, 1 on error — so the exit status is the signal and the output is
only for the detail string. `dnf5 advisory list --available --security` gives
the itemised view.

dnf4 shipped `dnf-makecache.timer`, which kept metadata fresh roughly every
three hours (hourly timer, gated by `metadata_timer_sync`). Under dnf5 that is
no longer guaranteed to be present. Check `systemctl list-timers '*makecache*'`
on the actual host before assuming the cache is fresh; where it is absent the
daily job's `dnf5 makecache` is what keeps the reading honest.

### Arch

Arch has no security classification in the repositories, so this needs two
tools and neither is in base:

- `checkupdates` (from `pacman-contrib`) — count of pending updates. Syncs to a
  temporary database rather than `/var/lib/pacman/sync`, so it does not
  interfere with a later `pacman -Syu`, and needs no root.
- `arch-audit` — installed packages with advisories from the Arch Security
  Team's tracker.

Without `arch-audit` the host reports a count with no security figure, which is
the honest reading rather than a zero.

### openSUSE — **verified** on Tumbleweed

On Leap or SLE, `zypper --no-refresh --quiet list-patches --category security`
is the right query. On **Tumbleweed** — which is what is in the fleet — there
are no patches to list, so the check uses `zypper --no-refresh list-updates`
and reports a count only.

The captured host makes the trap concrete. It reports:

```
$ zypper --non-interactive --no-refresh patch-check
0 patches needed (0 security patches)

$ zypper --non-interactive --no-refresh --quiet list-patches --category security
(nothing)
```

while `list-updates` on that same box lists **23 pending updates**, among them
`kernel-default` and `libseccomp2`. Patch metadata is a Leap/SLE concept and
Tumbleweed ships none, so `patch-check` answers 0 because there is nothing to
count — not because the host is current. Believing it would paint the card
green on a machine with a pending kernel update.

This is why the branch is chosen by `ID` from `/etc/os-release`
(`opensuse-tumbleweed`, `opensuse-slowroll`) rather than by whether
`patch-check` produced any output: both a fully-patched Leap host and a badly
out-of-date Tumbleweed host print exactly the same `0 patches needed`.
`tests/run.sh patches-zypper` pins it against the real capture.

Rows are counted with `$1=="v"`, which excludes the `S | Repository | ...`
header and the `---+---------+---` separator; those made the naive count 25.

`--no-refresh` matters on both spellings: without it every invocation hits the
network.

### Alpine

`apk list --upgradable` after `apk update`. Alpine publishes secdb JSON at
`secdb.alpinelinux.org`, but nothing in base consumes it, so there is no
security classification available on the host. Count only.

### FreeBSD

The best-behaved platform here: both reads are local, cheap, and fed by jobs
the base system already schedules. netdash reads and schedules nothing.

- `pkg audit -q` reads `/var/db/pkg/vuln.xml` with **no network** — `-F` is what
  fetches, and running that from a 60s collector would hammer VuXML. Exit 0
  means clean, 1 means vulnerable packages were found.
  `/usr/local/etc/periodic/security/410.pkg-audit` refreshes the database daily
  when `daily_status_security_pkgaudit_enable="YES"` in `periodic.conf`.
- `freebsd-update updatesready` needs **no network** and exits 2 when nothing is
  pending, 0 when fetched updates are ready to install. The fetching is
  `freebsd-update cron`, which is what belongs in nightly cron — its whole
  purpose is a random 1–3600s sleep so a fleet does not stampede the mirror.

If neither scheduled job is enabled on a given host, both reads still answer,
just from an ageing database — which is exactly what `checked_at` is for.

### OpenBSD — **verified** on OpenBSD 7.9

The worst-behaved, on three counts:

1. `syspatch -c` **requires root**. The source is explicit that listing
   installed patches (`-l`) and usage are the only actions permitted to an
   unprivileged user.
2. It **fetches the patch index from the mirror on every run** — there is no
   local cache to consult.
3. Nothing runs it for you. `/etc/daily` does not invoke syspatch (checked
   against the current source), so unlike FreeBSD there is no OS-scheduled
   result sitting on disk. netdash's own daily job is the only thing running it.

On the test host, a box with nothing pending:

```
$ doas syspatch -c ; echo "exit=$?"
exit=0
```

No output at all, exit 0. That confirms empty output is a genuine all-clear —
but only *together with* the exit status. An unreachable mirror also prints
nothing to stdout, and syspatch(8) exits 0 on success and >0 on error, so the
status is the only thing separating "nothing to patch" from "could not check".
The check tests it and keeps the previous result on failure, letting the badge
age into *unknown* rather than reporting an unreachable box as fully patched.
`tests/run.sh patches-openbsd` pins that; swallowing the status with
`|| true` fails it.

The whole check on that host:

```json
{"security":0,"other":null,"checked_at":1788371193,"source":"syspatch",
 "detail":"packages not checked (PKG_PATH unset)"}
```

Packages are separate, unclassified, and awkward: `pkg_add -un` is the
documented dry run, it needs `PKG_PATH` set, and on `-release` it only sees
security fixes if `PKG_PATH` points at the `-stable` package build. The
syspatch count is the signal worth reporting; packages are best-effort, and
`other` stays null until `PKG_PATH` is set rather than claiming a zero.

### NetBSD — **verified** end to end on NetBSD 11.0

`pkg_admin audit` checks installed packages against the local
`pkg-vulnerabilities` file in pkgdb — cheap, no network. The fetch is
`pkg_admin fetch-pkg-vulnerabilities`, which `/etc/daily` performs when
`fetch_pkg_vulnerabilities=YES` is set in `/etc/daily.conf`.

**On the test host that file did not exist, and nothing was going to create
it.** `/etc/daily.conf` did not set `fetch_pkg_vulnerabilities`, so the daily
fetch was off, and the audit simply failed:

```
$ pkg_admin audit
pkg_admin: Cannot open /usr/pkg/pkgdb/pkg-vulnerabilities: No such file or directory
```

Two consequences, both handled. First, netdash's own daily job runs the fetch
rather than assuming the system did — otherwise this platform would never
report at all. Second, the database's presence is checked *before* the audit
runs: `pkg_admin audit` against a missing file prints that error and exits, and
counting its output gives zero, which is indistinguishable from a host with no
vulnerable packages. A missing database is *unknown*, and the script says which
of the two fixes to apply.

The error string cannot be miscounted, incidentally: the grep looks for
`vulnerability` and the path is `pkg-vulnerabilities`, which does not contain
it.

Once the database was fetched, the audit produced one line per finding:

```
$ pkg_admin audit
Package perl-5.42.3 has a symlink-attack vulnerability, see https://nvd.nist.gov/vuln/detail/CVE-2011-4116
```

and the whole check, run as root on that host:

```json
{"security":1,"other":null,"checked_at":1788368590,"source":"pkg_admin-audit",
 "detail":"base system not checked (NetBSD ships no binary patch mechanism)"}
```

The format is `Package <pkg> has a <type> vulnerability, see <url>`, one line
per *finding* — so a package carrying three advisories contributes three. The
badge counts issues, not packages, which is the right reading for a security
indicator.

Match on `vulnerability` exactly. The tempting leniency — `vulnerabilit`, to
cover a possible plural — matches `pkg-vulnerabilities` in the missing-database
error instead, turning a host with no data into a host with one finding.
`tests/run.sh patches-netbsd` pins both directions against the real captures.

Pending package *updates* need `pkgin` (`pkgin -n upgrade`, root only —
unprivileged it answers *"You don't have enough rights for this operation"*);
with plain `pkg_add` there is nothing built in. **The pkgin parse is still
unconfirmed**: on the test host, which has pkgin installed, the run above
returned `other: null`, meaning the `N packages to upgrade` sed matched
nothing. That is the designed degradation rather than a wrong number, but it
means NetBSD currently reports security findings and no update count.

That count would come from pkgin's own database, whose age is not reflected in
`checked_at`, so `other` on NetBSD is a softer number than `security` even once
it works. Base system: nothing, as above.

### macOS — **verified** on macOS 26.6.2 (Apple Silicon)

**Do not run `softwareupdate -l` from the collector.** It triggers a full
network scan that takes tens of seconds, and at a 60s cadence the scans overlap.

Read `/Library/Preferences/com.apple.SoftwareUpdate.plist` instead, via
`plutil -convert json -o -` — it is world-readable, so this works from the
launchd job running as your user rather than root. The keys that matter:

| key | use |
|---|---|
| `LastSuccessfulDate` | freshness — this is `checked_at` |
| `AutomaticCheckEnabled` | advisory only — absent on macOS 26 |
| `LastRecommendedUpdatesAvailable` | **do not use** — see below |
| `LastUpdatesAvailable` | **do not use** |

The counts come from `softwareupdate --list --no-scan`, which reads the cache
the system already refreshed and so costs nothing.

**The plist's integer counters are unreliable.** On the macOS 26 test host,
`LastRecommendedUpdatesAvailable` read `1` while every other source said the
machine was fully patched:

```
$ defaults read /Library/Preferences/com.apple.SoftwareUpdate RecommendedUpdates
(
)
$ softwareupdate --list --no-scan
No new software available.
$ softwareupdate --list            # forced fresh scan
No new software available.
$ defaults read /Library/Preferences/com.apple.SoftwareUpdate LastRecommendedUpdatesAvailable
1
```

Note the last two lines: a forced fresh scan did not reset it. Nothing does.
An earlier version of this check trusted that integer and pinned a fully
patched Mac at "1 security" indefinitely.

That is a false *positive*, which is the safer direction to fail — it nags
about a patched machine rather than blessing an unpatched one. It is still
wrong, and a badge that cries wolf is one that gets ignored, which costs the
real alert later.

macOS scans on its own every six hours, so the cache is normally fresher than
a daily job would manage. On the test host `LastSuccessfulDate` was four
minutes old, and `date -j -f "%Y-%m-%d %H:%M:%S %z"` parsed
`2026-09-02 17:04:55 +0000` correctly.

**`AutomaticCheckEnabled` does not exist on macOS 26.** `defaults read` answers
*"The domain/default pair ... does not exist"*. An earlier draft leaned on that
key to detect a Mac that had stopped scanning, which would have been a check
that silently never fired on the newest release in the fleet.

The durable signal is the *age of the last successful scan*. If the system has
not managed one in 24 hours, something is stopping it — whatever the preference
is called on that release — and only then is a `softwareupdate -l` scan worth
its tens of seconds and network round trip. The key is still consulted when
present, but nothing depends on it.

A neglected Mac is therefore caught twice over: the forced rescan tries to fix
it, and if that fails `checked_at` stops advancing and the badge ages into
*unknown* rather than reporting a confident zero.

**Capture both streams.** `softwareupdate` splits its output: only the
`Software Update Tool` banner goes to stdout, while `No new software
available.` — and the update list — arrive on **stderr**. Redirecting stderr to
/dev/null leaves a lone banner, which is neither an all-clear nor a parseable
entry, so every Mac in the fleet read as *unknown*. Found because the check
refused to report a zero it could not justify, which is the guard below working
exactly as intended.

**The prefix is the classification.** From softwareupdate(8): recommended
updates are *"prefixed with a `*` character"*, non-recommended ones *"with a
`-` character"*. An earlier version counted only `*`-prefixed lines, which
hides every non-recommended update and would have pinned `other` at 0 forever.
Entries are counted with `^[*-][[:space:]]`; the `Title:`/`Version:`
continuation lines are tab-indented and cannot be mistaken for entries.

`Recommended: YES` is the security marker on modern releases; older ones tag
the title line `[recommended]` instead, and if neither marker is present the
count falls back to the `*` prefix. Rapid Security Responses appear in the same
list.

**The populated-output parse is still unverified.** Both Macs in the fleet are
fully patched, so no capture of a real pending update exists; only the
all-clear path has been exercised on hardware. The shape above is from
softwareupdate(8) rather than from a host.

Output that is neither the all-clear string nor a single parseable entry is
reported as *unknown*, not zero: both cases count zero entries, so the count
alone cannot tell "nothing pending" from "nothing understood", and only the
explicit `No new software available` string makes zero safe to report.

Homebrew packages (`brew outdated`) are a separate question and deliberately
not reported, as are `--include-config-data` items (XProtect and friends),
which macOS installs silently and never shows in the Software Update pane —
reporting something the user cannot act on is noise.

### TrueNAS CORE

Polled server-side like every other TrueNAS metric, so there is no collector
involved. `POST /api/v2.0/update/check_available` returns:

```json
{ "status": "AVAILABLE", "version": "13.0-U6.9" }
```

`UNAVAILABLE` means current. This is an OS-image check with no per-package
security classification, and it contacts the update server, so it is polled on
its own slow interval (`patch_poll_seconds`, default hourly) rather than at the
metric poll rate.

Worth knowing for later: `update.check_available` was **removed in TrueNAS 25.x**
in favour of `update.status`. CORE 13 is unaffected, but this call is the reason
the poller is CORE-specific.

## Payload

Collectors add one optional `patches` object; hosts that have never run a check
simply omit it and read as *unknown*.

```json
"patches": {
  "status":     "ok | security | updates | unknown",
  "security":   3,
  "other":      41,
  "checked_at": 1788300000,
  "source":     "apt",
  "detail":     ""
}
```

`security` is **null**, not 0, on any platform that cannot classify — Alpine,
Arch without `arch-audit`, openSUSE Tumbleweed, TrueNAS. Zero would claim the
host was checked for security updates and found clean; null says nobody looked.
The two render differently, and `tests/run.sh patches-stale` pins the
distinction.

`status` is derived by the *server*, not the collector, because staleness
depends on `patch_stale_hours` from the server config — a collector cannot know
the threshold it will be judged against. Collectors report facts
(`security`, `other`, `checked_at`, `source`) and the server decides.

Patch status deliberately **does not feed the overall host status**. Resource
pressure is live and self-clearing; pending patches sit amber for a week and
train you to ignore amber. It renders as its own badge, and the "needs
attention" count in the header stays a statement about CPU, memory and disk.

## Verifying on a real host

Only the Debian path above has been run against real hardware. Everything else
is documentation, and this repository's history is mostly cases where the
documentation and the behaviour disagreed — `df -l` on NetBSD, `vm.uvmexp` on
OpenBSD, BusyBox `df` on Alpine.

`collectors/probe.sh` has a `patch status` section that dumps exactly what these
checks read. Run it on a host before trusting that host's badge:

```sh
sh collectors/probe.sh > probe-$(hostname).txt
```

Captures that disagree with the above belong in `tests/fixtures/` with a note in
its README, and this file should be corrected to match the host rather than the
manual.
