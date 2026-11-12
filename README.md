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
mcp/          read-only MCP server, so an LLM agent can ask netdash (see MCP server)
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
| `retention_hours` | how long full-resolution samples are kept (see History) |
| `history` | hourly/daily rollups and the state-transition event log (see below) |
| `stale_after_seconds` | a host with no sample for this long shows as **Stale** |
| `patch_stale_hours` | a patch check older than this shows as **unknown**, not OK |
| `reachability` | probing that turns silence into **DOWN** (see below) |
| `eol` | end-of-life lookups via endoflife.date (see below) |
| `thresholds` | warn/crit percentages per metric |
| `hosts` | per-host thresholds, staleness, address, `virt` overrides and a container's `parent` (see below) |
| `truenas` | API polling for the NAS (see below) |
| `hubitat` | polling for a Hubitat Elevation hub (see below) |
| `unifi` | polling for a UniFi OS console and the devices it has adopted (see below) |
| `services` | application, management-controller and generic http/tcp/dns checks, attached to hosts -- or giving a host with no collector a card of its own (see Service checks) |

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

A host's overall status is the worst of its three metrics, and of its ZFS pool
health where it has any (see [ZFS pool health](#zfs-pool-health)). CPU is judged on
what has **held**, not on the latest reading: it is a warning only when every
reading over `thresholds.cpu.sustain_seconds` (default 300) was over warn, and
critical only when every one was over crit. A single spike — a build, a backup
starting — is normal operation and leaves the card green; the number still
shows it. A host that has been reporting for less than the window reads ok.
Set `sustain_seconds` to 0, globally or per host, to judge every reading on
its own. Memory and disk are judged on the latest reading: they do not spike.

### Per-host overrides

Some hosts are legitimately different. `hosts` in the server config carries
per-host settings, keyed by the name the collector reports:

```json
"hosts": {
  "netbsd11dot0": { "thresholds": { "mem": { "warn": 92 } } },
  "argon":        { "stale_after_seconds": 900, "expect_up": false },
  "hassium":      { "address": "10.0.0.42" },
  "truenas-core": { "virt": "none" },
  "plexserver":   { "parent": "mercury" }
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

### A host never leaves on its own

A machine that is down stays on the dashboard, red, for as long as it is down.
That sounds obvious and it was not: the fleet list used to be *derived* from the
samples, so it expired with them. A host would go **DOWN**, sit red for
`retention_hours`, and then the pruner would delete its last sample — and with
it the only evidence the host existed. ubuntu22dot04server locked up on a
Friday and by Saturday morning the wall panel showed a complete, all-green
fleet with a machine missing from it. Nothing looked wrong, which is the worst
thing an outage can manage to look like.

Membership is now its own fact, in its own table, and it expires on no timer:

- **the register** (`hosts`) — one row per host netdash has heard from, with
  its last OS string, source and push address. Written on every ingest.
- **the samples** — what it reported, kept for `retention_hours`.

Samples age out underneath the register and the row stays. A host with no data
left is still listed, still probed, and still reads **DOWN**; its card carries
`no readings left` and every metric reads `–` rather than `0%`, because "we
have no readings" and "it reported zero" are different claims. Its
acknowledgements survive too — being offline is not a reason to discard a
decision somebody made about a package and turn the host red on its way back
up.

The one thing that removes a host is a person saying so: **Forget this host**,
at the foot of its detail page, offered only while a host is silent — one that
is still pushing would re-register itself on its next sample, so the button
would be a lie. Forgetting drops its samples, rollups and acknowledgements. Its
events are kept: "it went down on the 4th" stays true after the machine is
decommissioned, and the event log is the one part of netdash allowed to
remember things that no longer exist.

To put back a host that was lost before the register existed, give it a row and
its last-seen time; it reappears as **DOWN** and is probed from the next sweep:

```sh
sudo -u netdash python3 -c "import sys, time; sys.path.insert(0, '/opt/netdash/server'); \
  import db; c = db.connect('/var/lib/netdash/netdash.db'); \
  db.register_host(c, 'ubuntu22dot04server', int(time.time()) - 86400, 'Ubuntu 22.04'); c.commit()"
```

### API

| route | purpose |
|---|---|
| `POST /api/ingest` | collector push (requires `X-Netdash-Token`) |
| `GET /api/overview` | all hosts, latest sample + status, newest collector version seen |
| `GET /api/host/<name>?minutes=60` | one host + recent history |
| `POST /api/host/<name>/patches/ack?package=<name>` | acknowledge one pending package; without `package`, every one pending now |
| `POST /api/host/<name>/patches/unack?package=<name>` | back to pending; without `package`, the whole host |
| `POST /api/host/<name>/eol/ack`, `/eol/unack` | the same for an end-of-life warning, which is one per host |
| `POST /api/host/<name>/forget` | remove a host from the fleet, with its samples, history and acknowledgements; its events are kept |
| `GET /api/events?host=&kind=&since=&limit=100` | the state-change log, newest first; `kind=service` also matches every `service:<id>`, `since` is a unix time |
| `GET /api/ups` | every configured UPS: power, readings, flags, alarms, communication health |
| `GET /api/ups/<id>?minutes=1440` | one UPS: the above plus every raw NUT variable, history (raw, or hourly past the raw window) and its events |
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
| Linux (no systemd) | Slackware 15.0 | dcron, 60s |
| FreeBSD | 15.1 | cron, 60s |
| MidnightBSD | 4.0 | cron, 60s; tested against captured output, not yet run there |
| DragonFly | 6.4 | cron, 60s; tested against captured output, not yet run there |
| OpenBSD | 7.9 | cron, 60s |
| NetBSD | 11.0 | cron, 60s |
| OmniOS | r151054 | cron, 60s; global zone only; tested against captured output, not yet run there |
| macOS | 15 (Intel), 26 (Apple Silicon) | Homebrew + launchd, 60s |
| TrueNAS | CORE 13.0 | server-side API poll, no collector installed |
| FreeCORE | 15.1 | same server-side API poll as TrueNAS CORE |

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

**MidnightBSD** forked from FreeBSD in 2006 and has kept up with it: every
sysctl and `df -T` column the collector reads is FreeBSD's, so it takes the
FreeBSD path unchanged, minus jail listing.

**OmniOS** (illumos) has its own collector, `collectors/illumos/`: no
`sysctl`, so CPU, memory and boot time come from `kstat -p`: the per-CPU
`cpu_nsec_*` counters, `physmem` and `freemem` pages less the ZFS ARC, and
`boot_time`. `uname -sr` says `SunOS 5.11` on every illumos distribution, so the
OS is `/etc/release`'s first line. The host name is `uname -n`, short:
`hostname -s` there would *set* the host name to `-s`. In a non-global zone
`kstat` answers for the whole machine, so the collector refuses to run there
rather than report the host as the zone.

**DragonFly** is FreeBSD's sysctls of 2003: the same `kern.cp_time`, `hw.physmem`
and `vm.stats.vm.*` counters, `kern.boottime` with `nsec` where FreeBSD has
`usec`, and `df -T`. It still has the cache queue FreeBSD 12 removed, whose
pages are clean and reusable at once, so used is `physmem - (free + inactive +
cache)`. It needs the `df -T` path, not the `/dev/` test: its HAMMER2 root is
mounted as `serno/<disk>.s1d`, with no `/dev/` in front. The default install
null-mounts six directories from `/build`, each repeating `/build`'s numbers;
the `null` type drops them.

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
- **FreeBSD / MidnightBSD / DragonFly / OpenBSD / NetBSD / OmniOS** → root crontab, every 60s

It also installs `netdash-patchcheck` on a **daily schedule and at every boot**
(`netdash-patchcheck.timer`, or root crontab entries at 03:xx and `@reboot`),
and runs it once so the card shows something before tomorrow. The boot run is
what makes patch-then-reboot show up promptly: nothing else updates the state
file, so without it a rebooted host keeps reporting the pending counts — and
the `reboot required` badge — it had before it went down. The server catches
that case independently: a check older than the host's own boot reads *checked
before reboot* rather than repeating a verdict about a machine that is gone, so
a host still on the old daily-only schedule stops showing a stale `reboot
required` even before its collector is upgraded. See *Two failure modes this
design exists to avoid* in PATCH-CHECKS.md.

The installer refuses to proceed if the host has no `curl`, `wget` or `fetch`,
and names the right command for that package manager (`apk`, `dnf`, `pacman`,
`zypper`, `apt-get`, `pkg_add`, `pkgin`, `pkg`).

Updating later: `git pull && sudo ./install.sh` — it overwrites the script and
keeps the existing config. `sudo ./uninstall.sh` removes it.

Config lives at `/etc/netdash/collector.conf` (Linux, OpenBSD, NetBSD, OmniOS) or
`/usr/local/etc/netdash/collector.conf` (FreeBSD, MidnightBSD, DragonFly), mode 600. The collector
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

## Bare metal, VM or container

Each collector reports whether it is virtualised, and the dashboard splits the
fleet on it.

| platform | how |
|---|---|
| Linux (systemd) | `systemd-detect-virt` — prints `none` and exits **1** on bare metal, so the word is the signal, not the status |
| Linux (no systemd) | the CPUID hypervisor flag, then DMI `sys_vendor`/`product_name` for the name |
| FreeBSD / MidnightBSD / TrueNAS | `sysctl -n kern.vm_guest`; inside a jail, `security.jail.jailed` answers `jail` first |
| Linux containers, no systemd | `/run/.containerenv` (podman), `/.dockerenv` (docker), `container=` in pid 1's environment (LXC, nspawn) |
| DragonFly | `sysctl -n kern.vmm_guest`; `unknown` reads as null |
| OpenBSD / NetBSD | DMI strings via sysctl, matched against known hypervisor vendors |
| OmniOS | `smbios -t SMB_TYPE_SYSTEM`, Manufacturer and Product matched the same way (needs root) |
| macOS | `sysctl -n kern.hv_vmm_present` |

`virt` is `"none"`, a hypervisor name (`bhyve`, `kvm`, `vmware`, …), a container
type (`jail`, `lxc`, `docker`, `podman`, …), or **null** when the host cannot
tell. `is_container` is true for the container types, and `is_vm` is false for
them — a container shares its host's kernel, so it is neither a VM nor bare
metal. `is_vm` is tri-state for the same reason everything
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
answered — `"config"` or `"host"`, or `"parent"` for a jail known only from its
parent's list — so a value someone typed is never indistinguishable from one a
machine measured. The card's hover says so too.

The answer is remembered. A host that goes down keeps its place on the Bare
metal, VMs or Containers tab, even after its samples have aged out of the
window, rather than surviving only on All.

### Jails and containers

A FreeBSD jail or Linux container runs on its host's kernel, so most of what a
collector normally reads — CPU counters, memory page queues, boot time —
describes the **host**. Inside one, the collectors measure the guest instead:

| reading | FreeBSD jail | Linux container |
|---|---|---|
| CPU | summed `%cpu` of the jail's processes over the host's cores | the cgroup's CPU time over its quota, or its visible cores |
| memory | summed resident memory of the jail's processes, against the host's RAM | the cgroup's charge less inactive file cache, against its limit, or the host's RAM |
| uptime | age of the jail's oldest process | `/proc/uptime` when lxcfs serves it, else time since pid 1 started |
| disk | the jail's `/` | the root filesystem, including docker's overlay |
| OS | `freebsd-version -u`, the jail's userland | `/etc/os-release`, as usual |

`ps` only lists the jail's own processes, which is what makes the FreeBSD
figures possible without enabling racct on the host. Summed resident memory
counts a shared library once per process that maps it, so it reads a little
high. A jail's `/` is a dataset in the host's pool, so its fullness is that
pool's. In a jail the patch check fetches base updates for the jail's own
release rather than the host kernel's, and never reports a reboot as required,
since restarting a jail does not change its kernel.

### Jails reported by their host

A jail does not need a collector of its own: its host lists it.

| host | how it finds its jails | what each jail gets |
|---|---|---|
| FreeBSD (the collector) | `jls` for running jails; `/etc/jail.conf`, `/etc/jail.conf.d/*.conf` and `iocage list` for stopped ones | everything in the table above — the host runs the same collector inside each running jail with `jexec` — plus patch status from the daily check, run the same way |
| TrueNAS / FreeCORE (the poller) | the API's `/jail` list, running or not | state, release, address, start-at-boot, uptime from `last_started`. **No CPU, memory or disk**: the API has none per jail |

Each jail is listed as a host of its own, on the **Containers** tab, with its
parent known — no `parent` entry needed. Its address comes from the host (or,
for a vnet jail, from `ifconfig` inside it), so reachability probing works with
no config. Its release feeds the end-of-life check like any other host's.

**Stopped jails are grey, "Stopped", never down.** A jail its host lists as
stopped — or one its host listed before and no longer does, which is what
stopping a jail looks like to `jls` — is a decision, not an outage. The card
says whether it is set to start at boot. That last word stands while the host
itself is quiet: a NAS outage does not turn its stopped jails red.

A running jail is **probed every sweep**, because its samples arrive fresh
whenever its host does, which proves nothing about the jail. Running and
answering is OK even with no readings to judge (a TrueNAS jail); not answering
is down, whatever the host says.

**A jail with its own collector** (the one above) still pushes for itself, and
those pushes win: the host's report adds only where it runs and whether it is
up, never a second, emptier sample beside them. That is how a TrueNAS jail gets
CPU and memory — install the collector inside it.

**Names.** A jail is listed under its short hostname, which is also what a
collector inside it calls itself and what TrueNAS lists it as. Jail names are
unique per host, not per network, so the second host to report a name gets
`name.parent` — mercury's `transmission` and cerium's `transmission.cerium` —
and keeps it. Names outside `A-Z a-z 0-9 _ . -` are skipped.

On the FreeBSD side nothing from a jail's filesystem runs as the host's root:
`jexec` runs the host's copy of the script as the jail's root, inside the jail.
The script travels as an argument, with stdin from `/dev/null`, so pkg's
bootstrap prompt in a jail without pkg declines instead of eating the script.
Each jail's patch result is kept on the host, in
`/var/db/netdash-collector/jails/<name>.json`, never written into the jail.
Set `NETDASH_JAILS=no` in `collector.conf` to stop a host reporting its jails.

`parent` in the config still works, and wins — for a jail whose host reports
nothing, such as one on a Linux box:

```json
"hosts": { "plexserver": { "parent": "mercury" } }
```

The detail page links to the host either way (`jail on mercury`).

The in-jail collector needs cron running inside the jail; jails started with
`/etc/rc` have it. Linux containers are reported only from inside, by their own
collector, which needs cron or systemd: LXC system containers have that, most
docker images do not.

## Tabs

- **All** — one line per host, sized so roughly forty fit on a screen without
  scrolling. Colour carries urgency, the number carries the value, and hovering
  carries everything else. No meters: a bar needs vertical space to read, and at
  this density the number *is* the signal.
- **Bare metal** / **VMs** / **Containers** — the full cards, filtered.
  Containers holds FreeBSD jails and Linux containers alike. These lists are short
  enough to afford the detail, and they are where you go to look at a machine
  rather than scan for the one that is wrong.
- **Services** — one line per service check rather than per host: service,
  host (with the host's own status as a small dot beside it), address, port,
  probe, status, detail, how long it has been in that state, when it was last
  checked and how long the check took. Worst first, disabled checks last and
  dimmed. The tab's count is checks, not hosts. On narrower screens the probe,
  checked and latency columns go first, then address, port, detail and since;
  hovering a row always gives the full story, and clicking it opens the host.

- **UPS** — one card per UPS read over NUT; see [UPS](#ups). Shown only when
  a UPS is configured, and never counted among hosts.

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
| Slackware | `slackpkg upgrade-all`, declined; ChangeLog `(* Security fix *)` | yes, from the ChangeLog |
| FreeBSD | `pkg audit`; `freebsd-update updatesready` unless pkgbase | yes, and base separately |
| DragonFly | `pkg audit` | yes for packages; **base has no patch mechanism** |
| MidnightBSD | `mport audit` (online CVE feed) + `mport list updates` | yes for packages, **unknown while the feed is down**; base not checked |
| OpenBSD | `syspatch -c` (base) + `pkg_add -u -n` (packages) | yes, for base |
| NetBSD | `pkg_admin audit` | yes for packages; **base has no patch mechanism** |
| OmniOS | `pkg update -n` (a plan, not a change); reboot from `beadm` | **no — IPS has no security classification** |
| macOS | `softwareupdate --list --no-scan`, plus `brew outdated` where Homebrew is installed | `Recommended: YES`; Homebrew is unclassified |
| TrueNAS CORE | `update.check_available` | no, OS image only |

Two rules hold everywhere:

**The check is daily, never per-sample.** Every mechanism above either hits the
network or parses the whole package database — `apt-get --just-print
dist-upgrade` alone takes ~3s on an idle Debian box. So `netdash-patchcheck`
runs once a day (and once at boot, which is when the answer most often changes)
and writes `/var/lib/netdash-collector/patches.json`
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
The host's detail page lists the pending packages with an **acknowledge** link
on each, and once every one of them is acknowledged the badge mutes to grey
while keeping its glyph, since the issues are still real.

Acknowledging is per package, and that is the point. Acknowledging `python312`
is a statement about python312: the packages around it can be fixed, and
different ones can turn up vulnerable, without that decision lapsing and having
to be made again over something it was never about. An unacknowledged package
keeps the badge red until it too has been looked at, and when it goes away the
host falls quiet on its own with nothing to re-acknowledge.

It is still *not* a snooze on the host. Each ack is keyed to the package name
exactly as the check reports it, version and all — a package that changes
underneath an ack is new information about the thing that was reviewed, so it
comes back. And an ack for a package that stops being pending is dropped by the
next prune, so that package's *next* advisory arrives loud rather than
pre-silenced by a decision about an older one.

Every pending package is named and acknowledged on its own. Until 0.4.1 the
checks capped the list at six names and summarised the rest as `(+3 more)`,
which the dashboard then offered as a single acknowledge link — asking an admin
to sign off on packages it could not show them. It was also unsound: that group
was keyed on the *count*, and which packages fell into it was positional, since
no check sorts its output. One package fixed and a different one turning up
vulnerable left the count where it was, and the ack made about the first set
silently covered the second. The names are uncapped now, and the server stores
them per host rather than on every sample — see *Naming the packages* in
PATCH-CHECKS.md.

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

**FreeCORE** serves the same API and is configured the same way. More than one
box makes the block a list, each entry with its own `display_name`:

```json
"truenas": [
  { "enabled": true, "host": "truenas.example", "api_key": "1-xxxxx" },
  { "enabled": true, "host": "freecore.example", "display_name": "freecore", "api_key": "1-yyyyy" }
]
```

A FreeCORE box reports itself as `FreeCORE <version>`, not as TrueNAS CORE, so
it gets the NAS icon but not CORE's end-of-life date. endoflife.date does not
track it either, so give it its own `eol.overrides` entry if you want one.
`truenas` is the only block that takes a list.

Pool health, scrubs and SMART come from the same API key; see
[ZFS pool health](#zfs-pool-health). SMART is read every `smart_poll_seconds`
(default 3600), on its own clock like the update check.

## ZFS pool health

A pool that has lost redundancy is the worst failure a NAS can have, because
the next disk to go takes the data with it. A capacity meter can't show it: a
DEGRADED pool at 40% used still reads green. So every host with ZFS also
reports each pool's **state**, its **last scrub** and its **error counts**,
and these count toward the host's status. TrueNAS adds the latest **SMART
self-test** for each disk.

| source | how | what |
|---|---|---|
| TrueNAS CORE / FreeCORE | `GET /pool`, `/boot/get_state` every poll; `GET /disk`, `/smart/test/results` and `POST /disk/smart_attributes` every `smart_poll_seconds` | pools including the boot pool, scrubs, per-disk SMART results named by serial |
| FreeBSD, Linux, macOS, NetBSD collectors | `zpool status -p` (plain `zpool status` on an OpenZFS without `-p`), sent as text and parsed by the server | pools, scrubs, error counts. **No SMART** yet (see issue #30) |

The collector sends the text and the server parses it (`server/zfs.py`), so
fixing a parser bug needs a server update only, not a redeploy to every ZFS
host. A jail sends no pool health, because its host reports the pools. A host
whose `zpool` fails, or has no pools, sends nothing. Its last report then
ages out with staleness, rather than being replaced by a report of "no
pools".

What colours the host:

| finding | severity |
|---|---|
| pool FAULTED, UNAVAIL, SUSPENDED or REMOVED | critical |
| permanent data errors (`errors: N data errors`) | critical |
| a SMART self-test that FAILED | critical |
| pool DEGRADED or OFFLINE | warning |
| read, write or checksum errors on a device of an ONLINE pool | warning |
| last scrub found errors | warning |
| last scrub older than `scrub_max_age_days` (default 42) | warning |

What is noted on the detail page but does not colour the host: a pool that has
never been scrubbed (netdash can't tell a new pool from a neglected one), a
disk with no self-test in `smart_max_age_days` (default 35), and a disk with no
self-tests at all.

The scrub default is 42 days, not 35. TrueNAS's stock scrub task has a 35-day
threshold and is checked weekly on Sunday, so a pool on that schedule is
routinely 36–41 days past its last scrub. Both limits live under
`thresholds.zfs` and can be overridden per host:

```json
"thresholds": { "zfs": { "scrub_max_age_days": 42, "smart_max_age_days": 35 } },
"hosts": { "cerium": { "thresholds": { "zfs": { "scrub_max_age_days": 60 } } } }
```

A SMART test's age is the disk's power-on hours now minus the hours logged
with the test. The test log records power-on hours, not dates. Reading power-on
hours runs `smartctl` on the NAS, which wakes a sleeping disk. So a disk with
an HDD standby timer is not asked, and its test age shows as unknown. Only ATA
disks report the attribute, so NVMe and virtual disks have no age either.

The card shows a **Pools** row naming whichever pool or disk is at fault. The
detail page has one row per pool, giving its state, its last scrub and its
read/write/checksum counts. Failing devices are named, with the serial
printed on the drive where it is known. Each change of severity goes into the
event log as `storage:pool:<name>`, `storage:scrub:<name>` or
`storage:smart:<serial>`. A pool that is already bad when netdash first sees
it is logged too.

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

## Service checks

A host that answers is not an application that works. hassium can push a
perfect sample every minute while Home Assistant on it has crashed, and its
card would be green. Service checks are the server asking the application
itself, read-only, and showing the answer **beside** the host's own status
rather than inside it -- the same separation patches and the UniFi fleet have.
A green host with a red Plex row says exactly that; neither is allowed to
paint over the other.

Nothing runs unless it is configured, and a check is off unless it says
`"enabled": true`. A config with no `services` block behaves exactly as
before, and collectors and their payload are untouched -- every check runs on
this server.

```json
"services": {
  "interval_seconds": 60,
  "timeout_seconds": 5,
  "failures_before_fail": 2,
  "checks": [
    { "kind": "home_assistant", "host": "hassium", "enabled": true,
      "url": "http://10.0.0.113:8123" },
    { "kind": "plex", "host": "plexserver", "enabled": true,
      "url": "http://10.0.0.133:32400" },
    { "kind": "transmission", "host": "transmission", "enabled": true,
      "url": "http://10.0.0.152:9091" },
    { "kind": "ipmi", "host": "mercury", "enabled": true, "address": "10.0.0.219",
      "tls_fingerprint_sha256": "1A:24:99:54:E1:93:7F:10:...:40:33" },
    { "kind": "http", "host": "printer", "enabled": true, "url": "https://10.0.0.60/",
      "expect_status": [200, 401], "expect_body": "HP",
      "tls_fingerprint_sha256": "..." },
    { "kind": "tcp", "host": "truenas-core", "enabled": true,
      "address": "10.0.0.45", "port": 445 },
    { "kind": "dns", "host": "gateway", "enabled": true,
      "server": "10.0.0.1", "query": "example.com", "expect": "A" }
  ]
}
```

`host` is the name the host has on the dashboard; the check appears on that
card and nowhere else.

### Hosts with no collector

A printer, a camera or a switch will never run a collector. A check whose
`host` has never reported gives that host a card of its own, marked **no
collector**: no CPU, memory, disk or patch readings, just its checks. Its dot
is the reachability probe's answer, aimed at the address its first check goes
to (or `hosts.<name>.address`, which wins): **up** when the probe answers,
**down** when it does not, unknown until it has been probed. The checks stay
their own axis as everywhere else -- a printer with a dead web page is not a
printer that is off. Probes need reachability enabled; without it the dot
stays unknown.

The day a collector does report under that name, its samples take over the
card. Removing the checks from config removes the card; there is nothing to
forget.

### What each check proves -- and does not

The card and the detail page say this in the same words. None of these is
proof the service is doing its job: a web interface that loads is not a
download making progress.

| kind | asks | ok means | does **not** mean |
|---|---|---|---|
| `home_assistant`, no token | `GET /manifest.json` | the HA web frontend answers as Home Assistant | the core is running, or any automation is |
| `home_assistant`, `token_file` | `GET /api/config` (Bearer) | the core reports `state: RUNNING`, not in recovery/safe mode | integrations or automations work |
| `plex` | `GET /identity` | Plex Media Server answers with its MediaContainer | libraries are mounted, or anything plays |
| `transmission` | RPC `session-stats` (a read) | the daemon answers its RPC | any torrent is making progress |
| `ipmi` | IPMI Get Channel Auth Capabilities on 623/udp, plus HTTPS `GET /` | the management controller is alive | anything about the OS -- that is the host's own status |
| `http` | `GET <url>`, redirects not followed | the status is in `expect_status` (default any 2xx or 3xx) and, if given, the page contains `expect_body` | the application behind the page works, or that the page is the one you meant -- use `expect_body` to narrow that |
| `tcp` | a TCP connect to `address`:`port`; nothing is sent | something is listening and accepts connections | that it is the service you expect, or that it works -- an SMB port that accepts is not a share that mounts |
| `dns` | one UDP query to `server` for `query`, type `expect` (default `A`) | the server answered with at least one record of that type | the answer is the right one, or that any other name resolves |

The generic three are for what has nothing better. Each proves less than the
specific kinds, and the card says so in the same words.

* **http**: a 401 or 403 that `expect_status` does not list is **auth**
  (amber), not a failure -- the server is there and wants credentials this
  check never sends. If that is the page's normal answer, list it:
  `"expect_status": [200, 401]`. Any other status outside the list is a
  failure. `expect_body` is a plain substring, searched in the first 256 KB.
  TLS follows the rules below: pin with `tls_fingerprint_sha256` or give a
  `ca_file`; there is no way to skip verification.
* **tcp**: unlike the reachability probe, where a refused connection proves
  the host is up, here **refused is a failure**: the question is whether the
  service is listening. A name that does not resolve is this server's error.
  Named `TCP <port>` with id `tcp-<host>-<port>` by default, so one host can
  have several.
* **dns**: built by hand from the standard library -- one question, recursion
  desired, over UDP, the reply matched by id and source. NXDOMAIN, SERVFAIL,
  an empty answer and silence are failures; REFUSED is **auth** (the server
  will not answer this one). An answer is not followed through CNAMEs: asking
  for `A` and getting only a CNAME is a failure, so ask a recursive resolver.
  The lookup is `query`, not `name` -- `name` is every check's label on the
  card. `expect` is one of A, AAAA, CNAME, MX, NS, PTR, SOA, SRV, TXT;
  `port` defaults to 53. A truncated reply with no usable record reads as
  cannot check: this check does not retry over TCP.

None of the three takes credentials, and one given a `username`,
`password_file` or `token_file` is refused as misconfigured rather than
silently not logging in.

The Transmission jail shares cerium's address, 10.0.0.152, and the check uses
it; it is attached to the `transmission` host so it shows on the jail's card.

Optional per kind: `machine_identifier` (plex) pins which Plex server must
answer; `rpc_path` (transmission) if it is not `/transmission/rpc`;
`username` + `password_file` (transmission, ipmi). Per check: `id` (default
`<kind>-<host>`), `name`, `interval_seconds` (15-3600), `timeout_seconds`
(1-30), `failures_before_fail`, `stale_after_seconds` (default three intervals).

### States

| state | colour | meaning |
|---|---|---|
| answering | green | the check passed |
| failing | **red** | the service did not answer properly: timeout, refused, 5xx, a reply that is not this service's, HA not RUNNING |
| rechecking | grey | one failure, held until it repeats `failures_before_fail` times -- one dropped packet is not an outage |
| auth refused | amber | the service answered and refused the credential, or needs one |
| misconfigured | amber | the check cannot run as configured: no URL, unreadable credential file, certificate does not match its pin |
| cannot check | amber | this server's side: name does not resolve, no route, ipmitool missing |
| stale | amber | no result for `stale_after_seconds` -- the checker itself is stuck |
| not checked yet | grey | the server just started |
| disabled | grey | in config with `"enabled": false`; listed on the detail page only |

Only failing is red, for the reason reach.py gives: a dashboard that paints a
service red because of its own broken plumbing teaches you to stop believing
red. Failures are counted in the header ("1 service failing"), flagged on the
host's compact row, and every settled change lands in the host's event log
(`service:<id>`). A held single failure is not logged. The **Services** tab lists every check
on one screen -- see Tabs.

The API carries, per check, `address`, `ports` (e.g. `["623/udp", "443/tcp"]`)
and `probe` (the request in a few words), derived from the same config the
check runs from.

### Polling

One thread per check, so a service that hangs holds up only itself. Every
request has a deadline on the whole exchange, not just per read -- a server
dribbling one byte at a time is cut off at `timeout_seconds` like a silent
one. The API never waits on a check: it reads the last verdict from memory.
Verdicts live in memory, like reachability; only transitions are written.

### Credentials

Never in `server.json`, never in the API, never in a log. Each lives in its
own file, named in config and read at check time:

```sh
install -m 0440 -o root -g netdash /dev/null /usr/local/etc/netdash/hass.token
# then put the token in it with an editor, not on a command line
```

* **Home Assistant**: optional. Without one, the check is the public manifest
  and nothing under `/api/` is ever requested -- an unauthenticated `/api/`
  request is a failed login to HA, and with `ip_ban_enabled` enough of them
  ban this server. With one (Profile -> Security -> Long-lived access tokens,
  ideally for a dedicated non-admin user), set `token_file`.
* **Transmission**: only if `rpc-authentication-required` is on; then
  `username` + `password_file`.
* **IPMI sensors**: a BMC account with **USER** privilege -- created by you on
  the BMC; netdash never touches BMC settings -- plus `username` +
  `password_file`, and `ipmitool` installed on this server
  (`pkg install ipmitool` / `apt install ipmitool`).

A credential that is refused is **not sent again** until its file changes, so
a typo cannot walk into HA's ip_ban, Transmission's brute-force lockout or a
BMC's account lockout one poll at a time. Fix the file; the next round
retries.

### TLS

Always verified; there is no setting to turn that off. For a device with a
self-signed certificate -- every BMC -- pin it:

```sh
echo | openssl s_client -connect 10.0.0.219:443 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
```

and put the result in `tls_fingerprint_sha256` (colons optional). The pin is
compared right after the handshake, before a byte of the request is sent; a
certificate that stops matching is reported as misconfigured, which is what a
firmware update that regenerates the certificate looks like -- and also what
someone else answering at that address looks like. Or give `ca_file`.

### The BMC

mercury's management controller (a Supermicro at 10.0.0.219) is a check
attached to mercury, shown as a **BMC** row on its card and a section of its
detail page that says in words it is separate hardware. It is checked whether
or not mercury is up, and when mercury is DOWN its row says what the BMC says:
`DOWN · no reply from 10.0.0.2 · BMC answers, power on` is a hung OS;
`power off` is a different trip to the rack.

Two levels, and the second may stay unknown indefinitely without that being a
fault:

* **Reachable** -- no credentials. The IPMI pre-session Get Channel
  Authentication Capabilities on 623/udp (the first packet of every ipmitool
  session; it changes nothing) and the HTTPS interface, pinned. Either
  answering is enough; neither is a failure.
* **Hardware** -- with credentials and ipmitool: `ipmitool -I lanplus -L USER
  sdr elist` and `chassis status`, every `sensor_interval_seconds` (default
  300). Temperatures, fans, power supplies and voltages are graded by
  ipmitool's threshold status and by discrete event text ("Failure detected",
  "AC lost"); chassis faults (power, cooling, drive) are critical. A
  reachable BMC reporting a failed supply is a red row. An unpopulated fan
  header ("No Reading") is not a failed fan. The password goes to ipmitool as
  `-f <file>`, never in argv or the environment, and USER privilege means the
  session cannot issue a single write.

Without credentials the row reads `reachable · sensors unknown` and stays
green -- reachability-only was the choice made. Configured credentials that
fail (refused login, ipmitool missing) turn it amber. Nothing is ever shown
as `sensors ok` that was not read.

Redfish was tried first and is not usable on this BMC: even the
unauthenticated service root answers `OemLicenseNotPassed` (it wants
Supermicro's SUM/DCMS/OOB licence).

### Troubleshooting

* `services: check X cannot run: ...` at startup -- the reason is the config
  error; the card shows it as misconfigured.
* `service X on H: auth -- ... not retried until ... changes` -- fix the
  credential file; the next round picks it up.
* **cannot check / cannot resolve** -- this server cannot reach the address.
  It is not the service, and netdash does not fix it by changing networking.
* **stale** -- the check thread produced nothing for three intervals: look
  for `service X:` lines in the journal.
* To see exactly what a check does, from the server:
  `curl -s http://10.0.0.133:32400/identity`; for the BMC
  `ipmitool -I lanplus -H 10.0.0.219 -U <user> -f <file> -L USER sdr elist`.

## UPS

A UPS is read over the [Network UPS Tools](https://networkupstools.org/)
protocol from whatever `upsd` it is already attached to — a TrueNAS box, a
Raspberry Pi, anything running NUT. Nothing is installed anywhere, and
netdash never logs in.

```json
"ups": {
  "poll_seconds": 30,
  "timeout_seconds": 5,
  "failures_before_lost": 2,
  "thresholds": {
    "charge": {"warn": 50, "crit": 20},
    "runtime_seconds": {"warn": 600, "crit": 300},
    "load": {"warn": 80, "crit": 95}
  },
  "devices": [
    {"id": "mercury-ups", "name": "Back-UPS RS 1350MS", "host": "10.0.0.2",
     "port": 3493, "ups": "ups", "attached_to": "mercury", "enabled": true}
  ]
}
```

`ups` is the UPS's name in `upsd` (the `ups@host` of `upsc ups@host`). `id`
is what netdash calls it in URLs, events and the MCP server. `attached_to` is
only a label — the host the UPS powers, linked from its page. Any setting may
be given per device to override the block's. TrueNAS CORE's built-in UPS
service listens on 3493 and answers this without any change; nothing on the
NUT side needs configuring, and nothing is changed by being read.

### Monitoring only

Every poll sends one line, `LIST VAR <ups>`, and that is all netdash will
ever send: the module checks each outgoing line against an allowlist, so no
command, self-test, calibration, shutdown, `FSD`, outlet switch or `SET VAR`
can be sent by a bug or a config value, and a UPS name carrying a second
command is refused before a socket is opened. There is no place for a upsd
username or password, and a device configured with one is refused rather than
quietly ignored. Shutdown policy stays wherever it already is (`upsmon` on the
machines the UPS powers), and netdash is not part of it.

### What is shown

- **Power**: on utility power, on battery, on bypass, or output off — read
  from `ups.status`, which is treated as a set of tokens (`OL CHRG`, `OB LB`,
  `OL RB OVER`…). Every token is shown; one NUT does not document is shown as
  itself, dashed, rather than dropped.
- **Battery charge** and **runtime**, labelled as the UPS's own estimate.
- **Load**, as a percentage of rating.
- **Input/output voltage, frequency, temperature, measured watts** — only
  when the UPS reports them. `ups.realpower.nominal` is shown as the *rated*
  power and never as consumption; nothing is derived from load × rating.
- **Device and battery**: model, manufacturer, serial, firmware, driver,
  beeper, the replace-battery flag, the last self-test result, and
  `battery.date` labelled as device-reported and unverified (many UPSes report
  a factory default there; an RS 1350MS bought this decade says 2001/09/25).
- **Communication**: when the last good reading was taken, consecutive failed
  polls, and the last error — told apart as network (nothing answered),
  protocol (something answered, not with a NUT reply we trust), driver (upsd
  answered but has no current data from the UPS: `DATA-STALE`,
  `DRIVER-NOT-CONNECTED`) and config (`UNKNOWN-UPS`, `ACCESS-DENIED`).
- Every raw variable, under a fold on the detail page.

### Power and communication are separate

A UPS netdash cannot read is not on battery, and it is not fine either. One
failed poll is held (`failing`, still fresh); `failures_before_lost` in a row
is `lost`. Once the last good reading is older than `stale_after_seconds`
(default three polls) the card reads **Stale**, power reads *unknown*, and the
last reading is shown greyed with its age — never as the state now and never
as zeros. A UPS last seen critical (on battery and low, say) that then goes
quiet stays **Crit**: that silence most likely means it shut down.

Colour: **OK** on utility with nothing wrong; **Warn** on battery, on bypass,
replace battery, an `ALARM`, or a reading past a warn threshold; **Crit** for
low battery, overload, output off, forced shutdown, or past a crit threshold.
The header leads with `UPS ON BATTERY` when one is.

### History and events

Each good reading is stored (`ups_samples`, raw for `retention_hours`, then
hourly rollups for `keep_rollup_days` with min/avg/max and how many of the
hour's readings were on battery). A failed poll stores nothing, so charts show
gaps, not zeros. Events, under the UPS's `id` with kinds:

| kind | states |
|---|---|
| `ups:power` | `utility`, `battery`, `bypass`, `off` |
| `ups:comm` | `ok`, `lost` |
| `ups:alarm:<code>` | a device alarm (`LB`, `RB`, `OVER`, `FSD`, `ups.alarm`, an unknown flag…) going `ok` → `warning`/`critical`/`unknown` and back |

Each is logged when it changes and not again; restarts are seeded from the
log, so an outage spanning a deploy is neither repeated nor loses its
recovery. Crossing netdash's own thresholds colours the card but is not an
event — a runtime estimate wanders across a line every few polls, and that
is a chart, not news. After a gap with no readings, an event's time is when
netdash saw the change, and its detail says the change happened at an
unknown time inside the gap: the outage's start or end is not invented.

### Smoke test

```sh
printf 'LIST VAR ups\n' | nc -w 3 10.0.0.2 3493
python3 server/nut.py 10.0.0.2 ups        # the same query, parsed and judged
```

## History

Raw samples answer *what is it doing*. They are the wrong tool for *is this
normal* and *which way is it going*, and keeping 52-second resolution for a
year to answer those would cost 5.6 GB to say what 40 MB says just as well.

So there are three tiers with three lifetimes:

| tier | resolution | kept | why |
|---|---|---|---|
| samples | ~52 s | `retention_hours` (48) | what it is doing now |
| CPU / memory rollups | hourly, min-avg-max | `keep_rollup_days` (400) | volatile — the intra-day shape is the information |
| disk rollups | **daily**, min-avg-max | `keep_rollup_days` (400) | slow — the useful question is a slope in days |
| events | every transition | forever by default | the only record that an outage happened |

```json
"history": { "enabled": true, "keep_rollup_days": 400, "keep_event_days": null,
             "roll_seconds": 900, "project_days": 30 }
```

Rolling disk up **daily** rather than hourly is what keeps this small — 76
mounts daily is 28k rows a year where hourly would be 666k — and hourly disk
granularity tells you nothing the daily figure does not.

`min` and `max` are stored alongside `avg` because an hour that averaged 40%
CPU and an hour that alternated between 5% and 95% are different facts about a
machine, and an average alone erases the second one.

400 days rather than 365 so "this time last year" is always answerable rather
than answerable most of the time.

### The roller is idempotent on purpose

Every pass rewrites all buckets in the raw window rather than tracking a
watermark. That costs one pass over ~100k rows, which SQLite does in
milliseconds, and buys the property that matters: a missed run, a restart
mid-hour, a clock step or a bucket first written from a half-filled hour all
correct themselves on the next pass. Over a 48-hour raw window each bucket is
rewritten dozens of times before the samples behind it age out, so what finally
remains is always a complete bucket.

It runs every `roll_seconds` (15 min) rather than hourly so the right-hand end
of a chart is never more than a quarter hour stale, instead of snapping into
existence on the hour.

**SQLite stays.** At ~45 MB a year this is nowhere near stressed; its real
limits are concurrent writers and network access, and netdash has one writer
already serialised behind a lock and one process reading. Note that `DELETE`
does not return space to the filesystem without a `VACUUM` — with a rolling
window the freed pages get reused, so the file plateaus rather than growing.

### Ranges

The detail page has **1h / 24h / 7d / 30d / 1y**, and the range lives in the
URL — `#/host/cerium/30d` — so it survives a reload and can be sent to someone.

You pick a *span*, never a resolution. The server answers from raw samples when
the span fits inside `retention_hours` and from hourly rollups when it does
not, and says which in the response; the caption under the chart reads `hourly
avg` when that is what you are looking at, rather than implying per-sample
detail it does not have.

### Disk over time

Each mount gets **its own panel**, built exactly like the CPU and memory panels:
a number, a full-height trace against this host's own warn and crit lines, and
a caption saying what the axis means. Disk used to be the odd one out — a stack
of bars with no history at all — which made the one metric whose whole story is
*which way is it going* the only one you could not see going anywhere.

Per mount rather than one chart for the fullest: the fullest mount and the one
that is actually filling are routinely different, and a single trace would hide
exactly the case worth catching. A host that reports no filesystem at all still
gets one panel saying so, rather than the section quietly disappearing.

The disk chart picks its own resolution from the span, separately from CPU and
memory. It used to be hourly at every range, so a 1h view crossing 05:00 drew
two points from sixty readings:

| span                         | each point is            | ~points per mount   |
|------------------------------|--------------------------|---------------------|
| up to 6 h                    | one reading, at its own ts | 60 an hour         |
| up to 24 h                   | 5-minute maximum         | 288                 |
| up to `retention_hours`      | hourly maximum           | 48 at 48 h          |
| past `retention_hours`       | daily maximum (rollups)  | one a day           |

Then a budget: at most 400 points per mount and 3,000 per host. A tier that
would exceed either steps coarser (15 min, 1 h, 3 h, 6 h, 1 day) until it fits.
That is why a 30-second launchd Mac's 6 h view is 5-minute maxima, and 24 h of
a forty-dataset pool is hourly. A bucketed point is ~150 bytes of JSON, so disk
tops out around 450 KB per refresh.

A bucket is **one real reading**: the one with the highest percentage, with
that reading's own `used_bytes` and `total_bytes`. It is `MAX`, not the
average, because the question a disk chart is asked is how close this got.
Separate `MAX(used)` and `MAX(total)` values would describe a disk that never
existed on a mount that was resized. If the capacity changed inside a bucket,
the point says `capacity_changed` and carries `total_min`/`total_max`. Each
reading's percentage is taken against its own total. A daily rollup keeps
`pct_max` but not the reading behind it. Its bytes are given only when the
capacity held all day, and are null otherwise.

A reading with no used figure or a zero/missing total is left out, never
counted as 0%. The line breaks across a gap wider than `disk_meta.gap_seconds`
(three missed readings, five minutes at least, or one missing bucket), and a
reading alone between two gaps is drawn as a dot. The tooltip says what a point
is: a reading and the second it was taken; or a bucket as start–end, with when
its peak was read, how many readings stand behind it, and whether it is
`partial` (still filling, or older than the prune horizon). A daily row from
before the folding roller (coverage NULL) is labelled "older summary, may be
incomplete".

The response carries `disk_resolution` (`raw`, `5min`, `15min`, `hourly`,
`3hourly`, `6hourly` or `daily`) and `disk_meta`: `bucket_seconds` (null for
raw), `aggregate` (`max` or null), `source` (`raw` or `daily_rollups`),
`gap_seconds`, `points`, `retention_hours`. A bucketed point's `ts` is the
bucket start, never a reading time; `peak_ts` is the reading time.

### Disk projections

The reason to keep history at all. Under the disk panel, any mount with a
rising trend gets a fitted slope and a date:

```
/data      +1.42 pts/day      full in ~14 d
```

A least-squares fit over `project_days` (30) of daily averages. **78% full** is
a fact you can already read off the card; *78% and climbing 1.4 points a day*
is the one that tells you whether to act this week or this quarter.

It refuses to answer rather than guessing: fewer than five days of data, a flat
or falling trend, or an answer more than a decade out all produce nothing. A
projection dressed up from noise is worse than no projection.

### The event log

Everything else in netdash is a photograph of now. Samples age out in a day and
the reachability verdict lives in memory and dies on restart, so a host that
dropped for four minutes at 03:00 left no trace anywhere — nothing recorded
that it had happened, and nothing could be asked about it afterwards.

A watcher thread records status transitions to an `events` table, shown as
**Recent changes** on the detail page and available fleet-wide at
`/api/events`. Its own thread rather than a hook in the request path: the
dashboard is a wall panel nobody is looking at during the outages that matter,
and a log that only records what happened while a browser tab was open is not a
log.

It is seeded from the last recorded state per host, so a `deploy.sh` does not
break the chain. A host seen for the first time after a restart records
nothing — "the state changed" and "we have never looked before" are different
claims, and only one of them belongs in a log.

`kind` is deliberately open rather than a status enum, so a WAN link flapping
or an access point dropping off a controller can join the same log.

## MCP server

`mcp/netdash-mcp.py` lets an LLM agent — Claude Code, or anything else that
speaks the Model Context Protocol — ask netdash what is wrong and what
changed, instead of someone reading the dashboard to it: "why was mercury
amber at 3am?", "which hosts are behind on security patches?".

It is a client of the HTTP API above and nothing more: Python 3, standard
library only, no database, no state. It runs where the agent runs, usually a
laptop, and never on the netdash server — which is why it lives beside
`collectors/` rather than in `server/`, where `deploy.sh` would install it on
the one machine that does not need it.

```sh
claude mcp add netdash -- python3 /path/to/netdash/mcp/netdash-mcp.py \
    --url http://netdash.example:8080
```

`NETDASH_URL` works in place of `--url`. On a machine whose Python is only
`python3.12` (FreeBSD), name that instead.

| tool | answers |
|---|---|
| `fleet_status` | every host's status, resources, reachability, patches, EOL, failing checks and an appliance's managed devices, with why each host needs attention; `problems_only` for just those |
| `host_detail` | one host: every disk with its fill projection, CPU and memory over a window (min/avg/max, when the peak was), how complete that history is, an appliance's members, pending packages, checks, recent changes |
| `events` | the event log, by host, kind and hours back, with `has_more` and a cursor for the next page |
| `patches` | pending updates per host, which were acknowledged, reboot required, EOL |
| `services` | every service check with its state, its target, what a pass proves and does not (`checks`), what it found (`facts`), and BMC sensors where read |
| `ups` | every UPS (or one, by `id`, with history over `hours` and its events): power source, charge, runtime estimate, load, voltages and temperature where reported, flags, alarms, battery health, freshness and communication |

`fleet_status` also lists UPSes, apart from hosts, under `ups` — with
`problems_only`, just the ones on battery, alarming or not reporting. A UPS
reading that is not current says so (`fresh: false`, `power: "unknown"`, the
last reading with its age), and the core readings a UPS did not report are
named in `not_reported` rather than left out or given as zero.

The answers are shaped for an agent rather than copied from the API: a
browser wants every chart point and the thresholds block on every host, and
an agent pays for each token it reads. Timestamps are local ISO time, not
epoch seconds an agent then has to convert and gets wrong. A sleeping laptop
reads as `away`, as it does on the dashboard — the API leaves that to the UI,
and an agent rederiving it would sooner or later report one as an outage.
A host with no collector (see Hosts with no collector) reads as
`"collector": false` with its reachability and checks and nothing else: no
last report, which would be the server's clock passed off as a check-in,
and no readings or patch state it could never have had.

History says what it is. Raw readings give a peak's exact time; hourly
rollups give the hour it fell in, and an average weighted by each hour's
readings (or, for hours rolled up before those were counted, each hour
equally, and the answer says which). Each window reports how much of it the
data covers — missing hours, hours rolled up after some readings were
pruned, and legacy hours that may be only a tail — and `complete` is true
only when none of that applies. A disk's daily points are each day's peak, so
the change between the first and last is given as `peak_change_pct_points`,
not as growth.

A UniFi console's offline or over-threshold devices are listed under
`members` and in the host's `attention` reasons, but not in its `status`,
which stays the console's own health, as on the dashboard. Arguments are
held to the tool's schema: `hours: 1.9`, `hours: true` or
`problems_only: "false"` is an error, not a guess.

### Read-only, and what that does and does not mean

There is no tool that acknowledges, unacknowledges or forgets, and the tests
run a session using every tool and check the server saw no POST. An agent
that can acknowledge a security update can silence the one you needed to
see; those stay a human action on the dashboard.

That is a property of this program, not a credential. netdash's API has no
read/write split to hand an agent — the acknowledge and forget routes trust
the network, like the dashboard that calls them — so anything that can reach
the server can POST to them, with or without this. A read-only token would
mean authenticating the dashboard's own browser requests as well, and that is
a separate change. Until then, "trusted network only" covers the agent's
machine too.

## Tests

```sh
sh tests/run.sh              # everything
sh tests/run.sh openbsd      # just the cases matching a name
```

### Looking at the icons

The OS icons are the one thing here that cannot be reviewed by reading it, and
there is no SVG rasteriser or browser on a netdash box. Three icon bugs shipped
before this existed — a Linux penguin that was a snowman, and two attempted
fixes for it that rendered as a donut and as a bowtie. All three read perfectly
well as coordinates.

```sh
python3 tests/icons/rasterize.py             # every family
python3 tests/icons/rasterize.py linux hub   # just these
```

Writes `icons.png`: each glyph at 96px, then at its true 19px card size,
magnified so the actual pixels are visible. It reads the shapes out of
`app.js` through node rather than keeping a second copy, so what you are
looking at is what ships.

`--check` is the fast path, and is what `tests/run.sh` runs: it parses every
glyph's geometry and fails on a path command it cannot read, an element that
draws nothing, or a shape that leaves the 24×24 viewBox. It says nothing about
whether an icon *looks* right — only a person looking at the PNG can say that.

The service and IPMI cases stand up their own HTTP, HTTPS and UDP servers on
127.0.0.1 (HTTPS only where `openssl` exists to make a certificate); nothing
leaves the machine.

The suite needs no network, no root and nothing installed — `sh`, `awk` and `python3`.
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
