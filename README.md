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
instructions.

### Config (`/etc/netdash/server.json`)

| key | meaning |
|---|---|
| `bind_host` / `bind_port` | listen address (trusted network only; do not expose directly) |
| `ingest_token` | shared secret; collectors send it as `X-Netdash-Token` |
| `retention_hours` | rolling window; older samples are pruned every 10 min |
| `stale_after_seconds` | a host with no sample for this long shows as **Stale** |
| `patch_stale_hours` | a patch check older than this shows as **unknown**, not OK |
| `eol` | end-of-life lookups via endoflife.date (see below) |
| `thresholds` | warn/crit percentages per metric |
| `hosts` | per-host thresholds, staleness and `virt` overrides (see below) |
| `truenas` | API polling for the NAS (see below) |

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
  "argon":        { "stale_after_seconds": 900 },
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

`stale_after_seconds` is overridable the same way. The effective values come
back per host in `/api/overview`, so a card being a colour you did not expect is
explainable without guessing, and the server logs at startup any configured host
that has never reported — a typo'd hostname otherwise does nothing, silently and
indistinguishably from a host not installed yet.

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
itself, so it is reported. The host simply shows as **Stale** on the dashboard
in both cases.

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

A host showing **Stale** is reachable-but-silent: its timer stopped, or the token
is wrong (the server answers 401 and the collector exits non-zero).

## License

BSD 2-Clause. See [LICENSE](LICENSE).
