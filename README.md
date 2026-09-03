# netdash

A small, self-hosted CPU / memory / disk dashboard for a trusted network. Collectors
push JSON over HTTP; the server keeps a rolling window in SQLite and serves two
views: an overview grid and a per-host detail page.

No agents to compile, no third-party service, no node cap. Server is Python 3
standard library only; collectors are POSIX `sh` plus the tools already on each OS.

Network throughput is deliberately not collected.

## Layout

```
server/       central service + dashboard UI
collectors/   per-OS collector scripts
  linux/      /proc/stat, /proc/meminfo, /proc/mounts, df   + apt/dnf/pacman/zypper/apk
  bsd/        sysctl, zfs list, df                          + pkg audit/syspatch/pkg_admin
  macos/      top, vm_stat, sysctl, df, mount               + com.apple.SoftwareUpdate
  install.sh  installer for Linux + BSD hosts
  probe.sh    read-only dump of what a new OS reports, for adding support
homebrew/     sample config packaged by the Homebrew formula
```

Each platform directory holds two scripts: `netdash-collector.sh`, which runs
every 30–60s, and `netdash-patchcheck.sh`, which runs daily. See
[PATCH-CHECKS.md](PATCH-CHECKS.md) for why they are separate.

## Server

```sh
sudo server/deploy.sh          # install/refresh + enable systemd unit
```

Copies the code to `/opt/netdash`, config to `/etc/netdash/server.json`, database
to `/var/lib/netdash/netdash.db`, and runs it as the `netdash` system user.
Re-running `deploy.sh` is the upgrade path: it copies the new code and restarts
the service, and it never overwrites an existing config.

See [INSTALL.md](INSTALL.md) for server setup and platform-specific collector
instructions, or [INSTALL-PROMPT.md](INSTALL-PROMPT.md) to hand the install to
an LLM agent instead of doing it by hand.

### Config (`/etc/netdash/server.json`)

| key | meaning |
|---|---|
| `bind_host` / `bind_port` | listen address (trusted network only; do not expose directly) |
| `ingest_token` | shared secret; collectors send it as `X-Netdash-Token` |
| `retention_hours` | rolling window; older samples are pruned every 10 min |
| `stale_after_seconds` | a host with no sample for this long shows as **Stale** |
| `patch_stale_hours` | a patch check older than this shows as **unknown**, not OK |
| `reachability` | probing that turns silence into **DOWN** (see below) |
| `eol` | end-of-life lookups via endoflife.date (see below) |
| `thresholds` | warn/crit percentages per metric |
| `hosts` | per-host thresholds, staleness, address and `virt` overrides (see below) |
| `truenas` | API polling for the NAS (see below) |
| `hubitat` | polling for a Hubitat Elevation hub (see below) |
| `unifi` | polling for a UniFi OS console and the devices it has adopted (see below) |

### Thresholds

Starting points — tune in the config, no code change needed:

| metric | warn | crit |
|---|---|---|
| CPU | 80% | 95% |
| Memory | 85% | 95% |
| Disk | 85% | 95% |

The detail page plots CPU and memory against these thresholds rather than on a
bare axis: the dashed guides are this host's own warn and crit lines, and the
trace is filled down to zero. A line at 14% floating in an empty panel says
nothing you cannot read from the number above it — the useful question is never
"what is the value" but "how much room is left", and that needs the limits drawn
in. The scale stays fixed at 0–100 for the same reason: fitting it to the data
would make two points of idle CPU jitter fill the panel and read as an event.

A host's overall status is the worst of its three metrics. CPU is the noisiest
signal at a 30–60s sample rate — if the grid flickers amber, raise the CPU warn
threshold rather than lowering the sample interval.

### Per-host overrides

Some hosts are legitimately different. `hosts` in the server config carries
per-host settings, keyed by the name the collector reports:

```json
"hosts": {
  "netbsd11dot0": { "thresholds": { "mem": { "warn": 92 } } },
  "argon":        { "stale_after_seconds": 900, "expect_up": false },
  "hassium":      { "address": "10.0.0.42" },
  "truenas-core": { "virt": "none" }
}
```

Overrides **merge**, per metric and per level, so naming just `mem.warn` leaves
`mem.crit` and every other metric at the global value. Replacing whole blocks
would mean restating numbers you did not want to change, which is how a fleet's
thresholds drift apart.

The two cases above are the ones this fleet actually has. NetBSD and OpenBSD
count active file cache as used, so they read 10–15 points higher than Linux for
the same workload — raising the warn threshold there is more honest than
pretending the number means something different. And a laptop that leaves the
LAN is not stale at 180 seconds; it is asleep.

`stale_after_seconds`, `expect_up`, `address` and `down_after_seconds` are
overridable the same way. The effective values come
back per host in `/api/overview`, so a card being a colour you did not expect is
explainable without guessing, and the server logs at startup any configured host
that has never reported — a typo'd hostname otherwise does nothing, silently and
indistinguishably from a host not installed yet.

### Down, stale and away

A push-only dashboard has one blind spot, and it is the outage you most need to
see. Nothing arrives from a host whose kernel has stopped scheduling, and
nothing arrives from a host whose collector was never installed properly. Both
are the same silence. Both used to read as the same muted grey **Stale** — the
colour you scan past — so a hard-locked VM sat on the wall panel looking exactly
like a machine nobody had finished setting up.

Silence is now three states, not one:

| state | colour | means |
|---|---|---|
| **Stale** | amber | expected to report, and has not. Something is wrong; we do not yet know what |
| **DOWN** | red | confirmed not answering, or absent far past any innocent explanation |
| **Away** | grey | `expect_up: false` — a laptop that is closed for the night |

**DOWN** outranks **Crit** in the rollup: a host at 99% disk is a problem you can
still log in and fix, and a host that is not answering is not.

Two independent routes reach it, so neither is a single point of failure:

- **the probe** — the server tries the host's own address and gets a clean
  no-answer. Fast and specific: red within a sweep of going stale.
- **the clock** — absence beyond `down_after_seconds`, for when the probe cannot
  reach a verdict at all: no route to that subnet, no `ping` binary, a host that
  answers nothing by design.

A probe that *answers* vetoes the clock. That case is the other half of what
this buys you: the box is fine and its collector is broken, which stays amber
and says so — `host answers (icmp) but is not reporting`. Shouting DOWN at a
machine you are currently talking to is how a dashboard loses its credibility.

```json
"reachability": {
  "enabled": true,
  "checks": ["icmp", "tcp:22"],
  "interval_seconds": 30,
  "timeout_seconds": 2,
  "failures_before_down": 2,
  "down_after_seconds": 900
}
```

Checks are alternatives, not a checklist: any one answering means up. ICMP
covers hosts that refuse connections, and a TCP port covers hosts that drop
ICMP. Port 22 is the pragmatic default because **a refused connection counts as
up** — generating that RST is work the host's kernel had to schedule — so it
works even where nothing is listening on it.

Only hosts that have gone quiet are probed, starting at half the staleness
window so the verdict is in hand the moment a host crosses into stale. A host
that reported four seconds ago is up, and no ping makes that truer.

**A probe that cannot run is never evidence of down.** A missing `ping`, an
unresolvable name, a firewall blocking the server's own outbound traffic — all
of those are facts about the server, not about the host, and they leave the
host amber rather than painting it red. A monitor that goes red because of its
own broken plumbing is one you learn to stop believing. The server says at
startup if ICMP probes are unusable, rather than leaving it to be discovered
during an outage.

The address is chosen in this order, and reported in `/api/overview` as
`reachability.address_source` so the choice is auditable rather than inferred:

1. `address` in the host's config block
2. the reported hostname, if it resolves on the server
3. the address the collector's own pushes came from

Third is a last resort on purpose: behind any NAT that is a gateway, and a
gateway answers a probe cheerfully while the host behind it is dead. A false
green is the one answer a down-detector must never give.

ICMP uses the system `ping`, which needs either setuid (BSD) or file
capabilities (Linux). The shipped systemd unit sets `NoNewPrivileges=yes`,
which drops those; on most Linux distributions `ping` still works through
unprivileged ICMP sockets (`net.ipv4.ping_group_range`). Where it does not, the
`tcp:` checks carry the feature on their own, or grant the capability:

```sh
systemctl edit netdash    # [Service] / AmbientCapabilities=CAP_NET_RAW
```

### API

| route | purpose |
|---|---|
| `POST /api/ingest` | collector push (requires `X-Netdash-Token`) |
| `GET /api/overview` | all hosts, latest sample + status, newest collector version seen |
| `GET /api/host/<name>?minutes=60` | one host + recent history |
| `GET /api/health` | liveness |

Payload shape:

```json
{ "host": "example-host", "os": "Ubuntu 24.04 (x86_64)", "cpu_pct": 12.4,
  "mem_used_bytes": 1234, "mem_total_bytes": 5678, "uptime_seconds": 99,
  "disks": [ { "mount": "/", "used_bytes": 1, "total_bytes": 2 } ],
  "patches": { "security": 3, "other": 41, "reboot_required": false,
               "checked_at": 1788300000, "source": "apt", "detail": "",
               "packages": "openssl-provider-legacy, libssl3t64, openssl" },
  "collector_version": "0.3.2" }
```

`patches` is optional: a host whose patch check has never run omits it, or sends
`null`, and reads as **unknown**.

## Supported platforms

Every version below is one a host actually reported from, not one believed to
work.

| family | tested on | notes |
|---|---|---|
| Linux (glibc) | Debian 13, Ubuntu 22.04 / 24.04 / 26.04, Raspbian 13 | systemd timer, 30s |
| Linux (other) | Fedora 43, Arch (rolling), openSUSE Tumbleweed | systemd timer, 30s |
| Linux (musl) | Alpine 3.24 | OpenRC + BusyBox crond, 60s |
| FreeBSD | 15.1 | cron, 60s |
| OpenBSD | 7.9 | cron, 60s |
| NetBSD | 11.0 | cron, 60s |
| macOS | 15 (Intel), 26 (Apple Silicon) | Homebrew + launchd, 60s |
| TrueNAS | CORE 13.0 | server-side API poll, no collector installed |

Architectures covered: `x86_64` (`amd64` as the BSDs spell it), `aarch64`
(Debian), `armv7l` (Pi 3 B+), `arm64` (Apple Silicon). The collectors pass
`uname -m` through untranslated, so the label follows the platform's own naming.

Three portability traps this had to work around, all of them silent failures
rather than errors:

- **BusyBox `df` rejects `-l` and `-x`**, so the old GNU-flavoured invocation
  returned *nothing* on Alpine. Disks now come from `/proc/mounts` (universal on
  Linux), which also supplies the filesystem type and source device the filtering
  actually needs.
- **BusyBox `awk` uses 32-bit signed ints for `printf "%d"`.** Any host with more
  than 2 GiB reported `mem_total_bytes: -2147483648`. All money values are `%.0f`.
- **btrfs subvolumes** (the Fedora and openSUSE default layouts) repeat one
  filesystem across many mount points. Entries are grouped by source device, so
  openSUSE's eight subvolumes report once.

**OpenBSD** needed three things FreeBSD does not: `kern.cp_time` has six fields
and is comma-separated (idle is last on all of them, so the parser sums every
field and takes the last rather than indexing position 5); `kern.boottime` is a
bare epoch rather than the `{ sec = N }` struct; and memory comes from
`vmstat -s`, because OpenBSD refuses to expose `vm.uvmexp` through sysctl and
answers *"use vmstat or systat"*. Its counters cross-check against `top` — 51501
free pages is 201 MiB against top's `Free: 201M`. Parse the `vmstat -s` fields
exactly: a loose `/pages free/` also matches `pages freed by pagedaemon`, a
lifetime counter, which is a 30% error.

**NetBSD** renders the same counters a third way: `kern.cp_time` comes back as
`user = N, nice = N, ...` key/value pairs, where FreeBSD is space-separated and
OpenBSD comma-separated. All three are stripped to bare numbers before parsing.
It refuses both `vm.uvmexp` and `vm.uvmexp2` through sysctl, so memory also comes
from `vmstat -s`; `kern.boottime` is a bare epoch as on OpenBSD, not the struct.
Its `df -l` does **not** exclude tmpfs/kernfs/ptyfs/procfs — the output is
identical to plain `df` — so the `/dev/` device test is what filters them. NetBSD
does ship ZFS, and its pools are reported.

**Memory on OpenBSD and NetBSD reads higher than on Linux for a comparable
workload.** Used is `physmem - (free + inactive)`, so active file cache counts as
used: the NetBSD test host reports 74.5% while holding 236 MB of file cache that
would largely be reclaimed under pressure. The counters that would let cache be
subtracted cleanly overlap the active/inactive pools, so subtracting them would
double-count. Tune the memory thresholds per host if this matters — the numbers
are consistent and conservative, not wrong.

## Collectors — Linux and BSD

```sh
git clone https://github.com/dennisfriedrichsen/netdash.git && cd netdash/collectors
sudo ./install.sh --url https://netdash.example/api/ingest --token YOUR_INGEST_TOKEN
```

The installer verifies the collector runs *and* that a real post succeeds before
it schedules anything, so a broken host fails immediately rather than silently.

- **Linux with systemd** → `netdash-collector.timer`, every 30s
- **Alpine / OpenRC** → root crontab every 60s, and `crond` is enabled and
  started, since it does not run by default on a minimal install and scheduling
  would otherwise silently do nothing
- **FreeBSD / OpenBSD / NetBSD** → root crontab, every 60s

It also installs `netdash-patchcheck` on a **daily** schedule
(`netdash-patchcheck.timer`, or a root crontab entry at 03:xx) and runs it once
so the card shows something before tomorrow.

The installer refuses to proceed if the host has no `curl`, `wget` or `fetch`,
and names the right command for that package manager (`apk`, `dnf`, `pacman`,
`zypper`, `apt-get`, `pkg_add`, `pkgin`, `pkg`).

Updating later: `git pull && sudo ./install.sh` — it overwrites the script and
keeps the existing config. `sudo ./uninstall.sh` removes it.

Config lives at `/etc/netdash/collector.conf` (Linux, OpenBSD, NetBSD) or
`/usr/local/etc/netdash/collector.conf` (FreeBSD), mode 600. The collector
searches all of those plus `/usr/pkg/etc`, so it finds its config either way.

### What counts as a disk

Filesystems that share free space are reported per *pool*, not per filesystem —
otherwise no single number is the disk's fullness.

On **macOS**, APFS volumes in a container share its free space: each reports the
container's total but only its own used. One entry per container is reported as
`total - available`.

A container is then reported only if you could actually free space on it — it
must have at least one read-write volume *and* at least one non-`nobrowse` one.
Xcode's CoreSimulator runtimes are read-only APFS images, permanently ~97% full
because that is what a packed image is; Apple's xarts/iSCPreboot/Hardware
container is read-write but entirely `nobrowse`. Both tests are needed: `Data` is
`nobrowse` and `/` is read-only, so each is rescued by the other volume in its
container. If `mount` returns nothing, every container is reported rather than
none.

On ZFS hosts one entry is reported **per pool**, not per dataset — datasets
share their pool's free space, so listing them all reports the same pool many
times over. The figure is `used + available`, the usable view,
which is also how the TrueNAS pools are reported.

Only **local** filesystems are reported. An NFS or SMB mount is storage owned by
another host, which already reports it — counting it on the client double-counts
the same bytes on two cards, and lets a full fileserver show up as the *client*
being critical while masking that client's real local disk pressure.

Memory excludes reclaimable cache on every platform, so the hosts read alike:
`MemAvailable` on Linux, and the ZFS ARC subtracted on FreeBSD and TrueNAS.

### Failure behaviour

The collector distinguishes two kinds of failure, because it runs every 30-60s
and its log is never rotated:

| condition | behaviour |
|---|---|
| server unreachable — DNS, connect, timeout, TLS (curl 6/7/28/35) | silent, exit 0 |
| anything else, notably HTTP 4xx/5xx from a bad token (curl 22) | message on stderr, non-zero exit |

The first is expected and self-correcting: a laptop off the LAN, or the server
restarting. Logging it would add ~1440 lines a day. The second will not fix
itself, so it is reported. The host shows as **Stale** on the dashboard in both
cases — and stays amber rather than going red, because the server can still
reach it.

On Linux the collector uses `curl` if present and falls back to `wget`, since
Alpine ships BusyBox `wget` and no `curl`. BusyBox `wget` exits 1 for every
failure, so the message text is the only signal for telling a network problem
from an HTTP error; GNU `wget`'s codes (4 network, 8 server) are used when
available. The BSD `fetch(1)` fallback has no granular exit codes and stays
loud — install `curl` on any BSD host that changes networks.

## Collectors — macOS

Via the [Homebrew tap](https://github.com/dennisfriedrichsen/homebrew-tap/blob/main/Formula/netdash-collector.rb):

```sh
brew install dennisfriedrichsen/tap/netdash-collector
$EDITOR $(brew --prefix)/etc/netdash/collector.conf   # set URL + token
brew services start netdash-collector                 # launchd, every 60s
```

Homebrew never auto-starts a service on install, so `brew services start` is a
one-time extra step. After that, `brew upgrade netdash-collector` is enough —
it restarts the job for you.

## End of life

A host can be perfectly patched and still be running something nobody ships
fixes for. That is a different question from the patch badge, and one the
machine cannot answer about itself — TrueNAS CORE will never report a pending
update again, because there will never be another one.

The server matches each host's reported OS string against
[endoflife.date](https://endoflife.date) and marks a card **EOL** when its
release is past end of life, or **EOL soon** within `warn_days` (default 30).
Thirty rather than ninety because Fedora's ~13-month cycle would otherwise leave
those hosts amber for three months twice a year, and a warning that is always on
is one you stop reading. The header
counts how many are past EOL. Nothing is shown for a healthy host: a badge on
every card reading "supported" is noise, and the value is spotting the one box
that is not.

**Nothing about the fleet leaves the network.** The server fetches public
product files (`/api/v1/products/debian`) on a slow timer and matches OS strings
locally; it never tells endoflife.date what you run. Set `"enabled": false` to
make no outbound request at all.

```json
"eol": { "enabled": true, "refresh_hours": 24, "warn_days": 30,
         "overrides": { "TrueNAS CORE": true } }
```

`overrides` is matched as a prefix of the OS string, longest match winning, and
takes precedence over the API. It exists because endoflife.date does not cover
everything: its `truenas` product tracks SCALE only (23.10 onward), so CORE 13
has no cycle to match and would read *unknown* forever — on the very box whose
abandonment makes the feature worth having. The value is either a date
(`"2025-03-31"`) or `true` to say "EOL, date unstated".

Rolling releases report `rolling`, not unknown: Arch and Tumbleweed have no
cycle that can expire. Anything unrecognised, or any product not yet fetched,
reports **unknown** rather than an optimistic "supported".

The lookup runs on a background thread and the request path only reads its
cache, so a slow or unreachable endoflife.date can never delay the dashboard —
it just leaves the badge unknown. A failed refresh keeps the previous cache;
release dates move on the scale of months, so yesterday's copy is still right.

**Debian's own eol date is a handover, not an ending.** endoflife.date's
`eolFrom` for Debian marks the Security Team stepping back — Debian's own LTS
Team then covers the release for two more years, free and on by default for
every install ([announcement](https://www.debian.org/News/2026/20260712)), so
netdash treats *that* date (`eoesFrom`) as Debian's real EOL. This is a
Debian-specific allowlist, not a general rule: Ubuntu's equivalent field is
Extended Security Maintenance, paid and opt-in via Ubuntu Pro, so a stock
install gets none of it — trusting it there would read an unpatched-since-2025
box as supported until 2030. Absent a product-specific reason to trust it, an
`eolFrom` release reads EOL.

## Bare metal or VM

Each collector reports whether it is virtualised, and the dashboard splits the
fleet on it.

| platform | how |
|---|---|
| Linux (systemd) | `systemd-detect-virt` — prints `none` and exits **1** on bare metal, so the word is the signal, not the status |
| Linux (no systemd) | the CPUID hypervisor flag, then DMI `sys_vendor`/`product_name` for the name |
| FreeBSD / TrueNAS | `sysctl -n kern.vm_guest` |
| OpenBSD / NetBSD | DMI strings via sysctl, matched against known hypervisor vendors |
| macOS | `sysctl -n kern.hv_vmm_present` |

`virt` is `"none"`, a hypervisor name (`bhyve`, `kvm`, `vmware`, …), or **null**
when the host cannot tell. `is_vm` is tri-state for the same reason everything
else here is: a host that cannot answer is not a bare-metal host, and collapsing
null to false would file every pre-0.4.0 collector under "bare metal" — the one
answer nobody checked.

It detects the hypervisor *type*, not which machine is running it. Two `bhyve`
guests may be on different hosts, so this does not give you a topology.

Some hosts can never answer. TrueNAS is polled over its API and runs no
collector at all, and on OpenBSD and NetBSD the detection is a DMI heuristic you
may simply know better than. A per-host `virt` in the config settles it:

```json
"hosts": { "truenas-core": { "virt": "none" } }
```

Config wins over the collector, and `virt_source` in the API reports which
answered — `"config"` or `"host"` — so a value someone typed is never
indistinguishable from one a machine measured. The card's hover says so too.

## Tabs

- **All** — one line per host, sized so roughly forty fit on a screen without
  scrolling. Colour carries urgency, the number carries the value, and hovering
  carries everything else. No meters: a bar needs vertical space to read, and at
  this density the number *is* the signal.
- **Bare metal** / **VMs** — the full cards, filtered. These lists are short
  enough to afford the detail, and they are where you go to look at a machine
  rather than scan for the one that is wrong.

Hosts that cannot report virtualisation appear under **All** only, with the
count called out beside the tabs. Guessing would file them under the wrong tab;
a host missing from a list is easier to notice than one silently misfiled.

## Collector versions

Each collector reports its own version, and `/api/overview` returns the highest
one any host is currently sending. A host below that is marked
`collector_outdated`, its version turns amber on the card, and the header
counts how many are behind.

The baseline is the newest version *seen in this fleet*, not the server's own:
the server does not know what has been released, only what its hosts run, so
upgrading one host is what makes the rest visibly stale. A host that has not
upgraded far enough to report a version at all shows nothing rather than being
counted as behind — those two states are different, and only one of them is
fixed by re-running `install.sh`.

Versions compare per numeric component, so `0.3.10` is newer than `0.3.9`; as
strings that is backwards, which would mark an entire fleet outdated the first
time a minor number reached double digits.

The version lives in `VERSION` and is stamped into all six collector and patch
check scripts, which `tests/run.sh version` keeps in sync — six hardcoded
copies drift silently otherwise, and a wrong version is worse than none.

## Patch status

Each card carries a **Patches** badge: security updates pending, a reboot
required, plain updates pending, up to date, or unknown. The full per-platform research — including
which commands are wrong in ways that fail silently — is in
[PATCH-CHECKS.md](PATCH-CHECKS.md). The short version:

| platform | what is actually checked | classifies security? |
|---|---|---|
| Debian / Ubuntu / Raspbian | `apt-get --just-print dist-upgrade`, origin annotation | yes |
| Fedora | `dnf5 check-update --security` | yes |
| Arch | `checkupdates` + `arch-audit` (neither in base) | only with `arch-audit` |
| openSUSE Leap | `zypper patch-check` | yes |
| openSUSE Tumbleweed | `zypper list-updates` | **no — rolling, ships no patch metadata** |
| Alpine | `apk list --upgradable` | **no — nothing on the host consumes secdb** |
| FreeBSD | `pkg audit`; `freebsd-update updatesready` unless pkgbase | yes, and base separately |
| OpenBSD | `syspatch -c` (base) + `pkg_add -u -n` (packages) | yes, for base |
| NetBSD | `pkg_admin audit` | yes for packages; **base has no patch mechanism** |
| macOS | `softwareupdate --list --no-scan` | `Recommended: YES` |
| TrueNAS CORE | `update.check_available` | no, OS image only |

Two rules hold everywhere:

**The check is daily, never per-sample.** Every mechanism above either hits the
network or parses the whole package database — `apt-get --just-print
dist-upgrade` alone takes ~3s on an idle Debian box. So `netdash-patchcheck`
runs once a day and writes `/var/lib/netdash-collector/patches.json`
(`/var/db/netdash-collector` on the BSDs); the collector only reads that file
back. That is deliberately not `/var/lib/netdash`, which belongs to the server
on a host running both.

**Installed is not the same as running.** A host that has applied its security
updates but not rebooted has a clean package database and is still executing the
old code — a Fedora box here reached exactly that state with the kernel and
`libbluez`. Where the platform can answer (`dnf needs-restarting -r`,
`/run/reboot-required`, `zypper needs-restarting`, a FreeBSD kernel version
mismatch) that shows as **reboot required**; where it cannot, the field is null
rather than a comforting "no".

**An old check is never green.** A count of zero from metadata last refreshed
in March is indistinguishable from a genuinely patched host, so a check older
than `patch_stale_hours` (default 48) renders as **unknown**, as does a host
that has never been checked. `security` is likewise `null` rather than `0` on
platforms that cannot classify — zero would claim someone looked.

The daily job refreshes package metadata (`apt-get update` and its equivalents),
because on a host without unattended-upgrades the counts are otherwise as old as
the last manual update. Set `NETDASH_PATCH_REFRESH="no"` in `collector.conf` to
forbid that; the badge then reports the age of the existing metadata and ages
into **unknown** on its own rather than pretending to be current.

Patch status is deliberately **not** part of a host's overall status. Resource
pressure is live and self-clearing; pending patches sit amber for a week and
train you to ignore amber. The "needs attention" count in the header stays a
statement about CPU, memory and disk.

### Acknowledging a security state

Not every red **security** badge is waiting on you: `pkg audit` on FreeBSD (and
`arch-audit`, and others) can flag a package for months with no upstream fix
yet, and the badge has no way to tell "not fixed" from "not looked at" apart.
The host's detail page has an **acknowledge** button for exactly that case —
it silences this security count against this exact package list, muting the
badge to grey while leaving its glyph in place, since the issue is still real.

It is *not* a snooze on the host. The ack is keyed to that count and that
package list, both — a package on the list getting fixed, or a different one
turning up vulnerable, changes one of them, and the badge reverts to red on
its own at the next check. Nothing needs to notice the ack has gone stale and
clear it by hand.

Only offered for **security** — a **reboot required** badge clears itself the
moment the host reboots, so there is nothing there worth silencing.

## TrueNAS

Nothing is installed on the NAS. The server polls its REST API on a schedule.
Create an API key in the TrueNAS UI (Settings → API Keys), then in
`/etc/netdash/server.json`:

```json
"truenas": { "enabled": true, "host": "truenas.example", "api_key": "1-xxxxx", "poll_seconds": 60 }
```

Check it before enabling:

```sh
sudo python3 /opt/netdash/server/truenas.py /etc/netdash/server.json
```

That prints exactly what the dashboard will show, or the API error.

## Hubitat

Nothing is installed on the hub, and with its own **hub security** switched off
there is no credential to configure either — the local endpoints answer anyone
on the LAN. In `/etc/netdash/server.json`:

```json
"hubitat": { "enabled": true, "host": "10.0.0.76", "display_name": "palladium",
             "mem_total_bytes": 2147483648, "poll_seconds": 60 }
```

Check it before enabling:

```sh
sudo python3 /opt/netdash/server/hubitat.py /etc/netdash/server.json
```

That prints exactly what the dashboard will show, or the error.

If hub security is on, every endpoint serves the login page instead of data.
That is reported as itself — "if hub security was turned on, these endpoints
now serve the login page" — rather than as a hub that has gone quiet, because
the two look identical from here and only one of them is a problem.

### What the hub can and cannot answer

| | |
|---|---|
| CPU | the 5-minute load average over the core count |
| memory | measured free against a **declared** total (see below) |
| disk | nothing — the hub has no filesystem it will report |
| uptime | nothing — it is nowhere in the HTTP surface |
| patches | one platform image update, from Hubitat's cloud |

Two of those blanks are permanent, and the card shows them as unknown rather
than filling them. The hub's database size is not a mount, and turning it into
a disk row to keep the card symmetrical would put a number on the dashboard
that means nothing.

`mem_total_bytes` is **declared, not measured**: the hub reports how much
memory is free and never how much exists. A C-8 Pro has 2 GiB
(`2147483648`), a C-8 has 1 GiB. Get it wrong and the percentage is wrong in
proportion — and if you leave it out, memory reads as unknown rather than
being guessed at.

### CPU is converted, not copied

`/hub/advanced/freeOSMemoryLast` has a column headed **5m CPU avg**. It is a
load average, not a percentage — Hubitat's own Hub Information driver divides
it by the core count and multiplies by 100. netdash does the same, reading the
core count from `/hub/cpuInfo` rather than assuming four.

This matters because the raw number looks like a plausible percentage. A hub
sitting at a load of `0.37` on four cores is at 9% CPU, and reporting `0.37%`
would show a hub under real load as permanently idle.

### Patch status comes from the cloud, not the hub

`/hub2/hubData` carries `alerts.platformUpdateAvailable`, which looks like the
same fact for free — one request, no round trip off the LAN. It is a cache the
hub refreshes on its own schedule, and it has been observed reading `false`
while an update was genuinely available.

So the answer comes from `/hub/cloud/checkForUpdate` on `patch_poll_seconds`
(hourly by default), with the last good answer kept across metric polls. Like
TrueNAS, Hubitat ships one platform image and classifies nothing, so
`security` is null rather than 0 — see [PATCH-CHECKS.md](PATCH-CHECKS.md) for
why that distinction is worth keeping.

### Reachability

Unlike TrueNAS, the hub **is** probed. Knowing it answers on the interface you
actually use it on is most of the reason it is on the dashboard at all, so its
silence gets the same treatment as a push host's rather than being left to the
journal.

It runs no `sshd`, so the default `tcp:22` check can only ever time out. Give
it the port you reach it on:

```json
"hosts": { "palladium": { "checks": ["icmp", "tcp:8080"] } }
```

### A note on an open hub

An unauthenticated hub is what makes all of the above possible, and it is
worth knowing what else it means: `/hub/shutdown` and `/factory/recovery` are
unauthenticated GETs, and `/hub2/hubData` hands any LAN client a live
dashboard access token along with the registered owner's email address. This
poller only ever `GET`s a fixed list of paths and never logs a response body,
but that is netdash being careful with something already open to everyone on
the network.

## UniFi

Nothing is installed on the console. The server polls the Network **Integration
API** with an API key — created in the Network application under Settings →
Control Plane → Integrations, or on UniFi OS 5.x under Settings → System →
Advanced → API.

The key inherits the permissions of the account that created it; Ubiquiti
offers no genuinely read-only key, so it is worth creating a dedicated admin
for it. An account with MFA enabled cannot be used this way.

```json
"unifi": { "enabled": true, "host": "10.0.0.1", "display_name": "gateway",
           "verify_tls": false, "api_key": "xxxxx", "poll_seconds": 60 }
```

Check it before enabling:

```sh
sudo python3 /opt/netdash/server/unifi.py /etc/netdash/server.json
```

`device_name` and `site` are optional. netdash finds the console among its own
adopted devices by asking the unauthenticated `/api/system` for the console's
MAC and matching it; set `device_name` only if that fails. Set `"fleet": false`
to poll the console alone and skip its adopted devices.

### What the console can and cannot answer

| | |
|---|---|
| CPU | `cpuUtilizationPct` |
| memory | `memoryUtilizationPct` — a **percentage, with no byte counts at all** |
| disk | nothing in this API |
| uptime | `uptimeSec` |
| patches | `firmwareUpdatable`, from the controller itself |

Memory has no total to report a fraction of, so these samples carry a
`mem_pct` column instead. Every source that does report bytes leaves it null
and the percentage is derived from used ÷ total as before, so the two can
never disagree.

The classic `/proxy/network/api/s/{site}/stat/device` endpoint does report a
storage array, byte counts and temperatures — but only to a session logged in
with an admin password. That trade was declined: a revocable key is worth more
than a disk figure on a device whose disk you were never going to act on.

### The adopted fleet

Switches and access points appear as one badge on the console's card — *5
online*, or the names of whatever is not — and in full on its detail page, with
per-device memory, uptime and firmware.

They are **not hosts**. They never push, run no collector, and cannot be probed
apart from the console that reports them, so they live in a `fleet` table
hanging off the console's sample in the same way disk rows do. Putting them in
`samples` would drag them through the reachability sweep, the EOL lookups and
every host listing, to be filtered back out again at render time.

A downed device **does not change the console's status colour.** That dot
answers "is this gateway healthy", and a gateway running perfectly while two
access points are offline is a true statement that a red dot would turn into a
lie. The count gets its own badge instead — the same separation this dashboard
already makes for patch status, and for the same reason.

Two limits worth stating plainly:

* **Online state is the controller's word, not a netdash probe.** If the
  controller is wrong about a switch, netdash is wrong about it too. What it is
  not is a gap: when the controller itself stops answering, the poll fails and
  the console's own card goes stale and then down on its own evidence.
* **Fleet firmware updates stay off the console's patch badge.** They are
  counted, and shown per device on the detail page, but the badge keeps
  answering a question about the console.

### The gateway's own card cannot page you about the gateway

If netdash runs behind this console, a real outage takes the dashboard with it
and every other host goes red at once. The card is still worth having — it
catches a wedged or rebooting gateway, and it explains a blast radius after the
fact — but nothing hosted behind a gateway can alert on that gateway.

### Two traps

`ipAddress` for a gateway is its **WAN** address (`192.168.0.25` on a console
reached at `10.0.0.1`). netdash never uses it: the configured `host` is the
only address that means anything for reachability, and probing the one in the
payload would aim at the wrong interface entirely.

Every path under `/proxy/network` returns **401 when unauthenticated,
including paths that cannot exist**. A 401 therefore says nothing about
whether the Integration API is enabled — only that UniFi OS rejected the
request before routing it. Test with a real key or the answer is meaningless.

## Tests

```sh
sh tests/run.sh              # everything
sh tests/run.sh openbsd      # just the cases matching a name
```

108 cases, no network, no root, nothing installed — `sh`, `awk` and `python3`.
The BSD and macOS cases run the real collector end to end against mocked
`sysctl`/`df`/`mount`/`vmstat`; the Linux cases drive its awk programs directly,
since `/proc` reads cannot be intercepted through `PATH`.

Every case pins a bug that actually shipped, and the fixtures are real captures
from real hosts (see `tests/fixtures/README.md`). Confirmed the suite fails when
the boottime anchor, the `nobrowse` container test, or the 64-bit `%.0f` are
reverted — a test that has never failed proves nothing.

## Troubleshooting

```sh
systemctl status netdash                      # server
journalctl -u netdash -n 50                   # server log
systemctl list-timers netdash-collector.timer # collector schedule (Linux)
netdash-collector --print                     # what this host would send
netdash-patchcheck --print                    # patch check, without writing state
systemctl list-timers netdash-patchcheck.timer
curl -s https://netdash.example/api/overview | python3 -m json.tool
```

A host showing **Stale** is now genuinely reachable-but-silent — the server has
probed it and something answered. Its timer stopped, or the token is wrong (the
server answers 401 and the collector exits non-zero). A host showing **DOWN** is
not answering at all; the card and `/api/overview` say which check failed, at
which address, and where that address came from.

## License

BSD 2-Clause. See [LICENSE](LICENSE).
