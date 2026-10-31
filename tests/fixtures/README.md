# Test fixtures

Every file here is **real output captured from a real host**, not invented. That
matters: most of the bugs these tests lock in came from a command behaving
differently than its documentation or its sibling platforms implied, and a
fixture written from imagination would have encoded the same wrong assumption
the code had.

Hostnames and addresses have been replaced with generic ones; the shape and the
numbers are untouched.

| file | captured from | what it pins down |
|---|---|---|
| `openbsd/df-k-l.txt` | OpenBSD 7.9 | `df -l` output; `-T`, `-B1` and `-x` are all rejected there |
| `openbsd/vmstat-s.txt` | OpenBSD 7.9 | page counters — `sysctl vm.uvmexp` is refused, answering "use vmstat" |
| `openbsd/cp_time.txt` | OpenBSD 7.9 | six fields, comma-separated |
| `openbsd/pkg_add-un-none.txt` | OpenBSD 7.9 | the quirks signature line pkg_add always prints — not an update |
| `openbsd/pkg_add-un-quirks-same.txt` | OpenBSD 8.0 | signature plus `quirks-7.227->7.227: ok` — quirks at the version it has, not an update |
| `netbsd/df-k-l.txt` | NetBSD 11.0 | `-l` does **not** exclude tmpfs/kernfs/ptyfs/procfs here |
| `netbsd/vmstat-s.txt` | NetBSD 11.0 | page counters; both `vm.uvmexp` and `vm.uvmexp2` are refused |
| `netbsd/cp_time.txt` | NetBSD 11.0 | `user = N, nice = N, ...` key/value pairs |
| `macos/df-k-l-intel.txt` | macOS 15.7.9, Intel | five APFS volumes sharing one container's free space |
| `macos/mount-intel.txt` | macOS 15.7.9, Intel | the `read-only` / `nobrowse` flags the filter depends on |
| `macos/df-k-l-applesilicon.txt` | macOS 26.6.2, Apple Silicon | Xcode CoreSimulator runtimes, permanently ~97% full |
| `macos/mount-applesilicon.txt` | macOS 26.6.2, Apple Silicon | `Hardware`/`xarts`/`iSCPreboot` are read-**write** but `nobrowse` |
| `linux/proc-mounts-opensuse.txt` | openSUSE layout | eight btrfs subvolumes on one device, plus ZFS and NFS |
| `linux/df-P-k-opensuse.txt` | openSUSE layout | the same filesystem repeated once per subvolume |
| `freebsd/df-k-T-l.txt` | FreeBSD 15.1 | `-T` is accepted here and nowhere else |
| `freebsd/zfs-list.txt`, `netbsd/zfs-list.txt` | — | `zfs list -Hp -d 0` pool roots |
| `freebsd/cp_time.txt` | FreeBSD 15.1 | five fields, space-separated |
| `freebsd/df-k-root.txt` | FreeBSD 15.1 | `df -k /`, which is all a jail reports of disk; captured on a host, since a jail's columns are the same |
| `linux/apt-dist-upgrade.txt` | Debian 13 | the security origin sits *inside* the parentheses |
| `linux/zypper-list-updates-tumbleweed.txt` | openSUSE Tumbleweed | 23 real pending updates; header and `---+---` separator must not count |
| `linux/zypper-patch-check-tumbleweed.txt` | openSUSE Tumbleweed | the same host answering *"0 patches needed"* |
| `netbsd/pkg_admin-audit.txt` | NetBSD 11.0 | `Package X has a Y vulnerability, see URL` — one line per finding |
| `linux/dnf5-check-update.txt` | Fedora 43 | dnf5's three-column rows; 11 pending |
| `linux/dnf5-check-update-security.txt` | Fedora 43 | the 6 of them that are security — kernel is *not* among them |
| `freebsd/pkg-audit.txt` | FreeBSD 15.1 | `14 problem(s) in 9 package(s) found.` — the package count is the one to use |
| `netbsd/pkg_admin-audit-nodb.txt` | NetBSD 11.0 | the same command with no database: must count zero, not one |
| `netbsd/pkgin-upgrade-none.txt` | NetBSD 11.0 | `nothing to do.` — a real zero, not a parse failure |
| `macos/softwareupdate-none.txt` | macOS 26.6.2 | both streams combined: the all-clear string is what makes a zero safe to report |
| `macos/softwareupdate-stdout-only.txt` | macOS 26.6.2 | the same command with stderr discarded — a lone banner, and not an all-clear |
| `services/plex-identity.xml` | Plex Media Server 1.41.3 | `/identity`: one `MediaContainer` start tag, no token needed (machineIdentifier replaced) |
| `services/hass-manifest.json` | Home Assistant | `/manifest.json`, the unauthenticated proof the frontend is HA's |
| `services/transmission-rpc-409.*` | Transmission, RPC 6.0.1 | the CSRF 409 and its `X-Transmission-Session-Id` (id replaced) |
| `services/transmission-session-stats.json` | Transmission, RPC 6.0.1 | `session-stats` from an idle daemon: zero torrents is a real answer |
| `ipmi/channel-auth-reply.hex` | Supermicro BMC (mercury's) | the pre-session Get Channel Authentication Capabilities reply: completion 00, IPMI 2.0 |
| `slackware/slackpkg-upgrade-all.txt` | Slackware 15.0 | `upgrade-all` declined in batch mode: 157 pending, and the `Total package(s)` line the count is checked against |
| `slackware/ChangeLog-excerpt.txt` | Slackware 15.0 mirror | `(* Security fix *)` under a *group* of package lines (the kernel's five), a bugfix entry directly above a security one, and the release line |

`apt-dist-upgrade.txt` is real `apt-get --just-print` output, captured with
`-o Dir::State::status=/dev/null` because the host had nothing pending — that
makes apt resolve every dependency as a fresh install, so the `Inst` lines and
their origin annotations are exactly what a real pending upgrade produces. It
deliberately includes `debian-security-support`, a genuine package whose *name*
contains `-security` but which ships from the plain archive: a naive whole-line
match counts 4 security updates where there are 3.

The two `zypper-*-tumbleweed.txt` files are one capture from one host at one
moment, and they only mean anything as a pair: `patch-check` says 0 patches and
0 security patches while `list-updates` lists 23, including `kernel-default`.
Tumbleweed ships no patch metadata, so the zero is an absence of data rather
than a clean bill of health. Routing a rolling release through the patch-check
branch would report that host as up to date.

`freebsd/pkg-audit.txt` is the real capture with its `CVE:` lines removed —
one chromium entry alone listed over 300 of them, and nothing in the parse reads
them. Every remaining line is verbatim, including all nine `is vulnerable`
headers and the summary line the parse actually depends on.

`slackware/ChangeLog-excerpt.txt` is six verbatim runs of the real 2.4MB
`slackware64-15.0/ChangeLog.txt`, cut at entry boundaries, newest first as
upstream writes it. The upgrade-all capture is from a 15.0 host that had never
been patched, which is why it pends nearly every package in `patches/`.

`cp_time.txt` holds two lines: two successive reads, so the mock can return a
delta rather than a single sample.

Not here, on purpose: `ipmitool sdr elist` and `chassis status` output. No BMC
credentials were available to capture any, so `tests/ipmi.py` carries
format-derived samples inline and says so. Replace them with a real capture
once there is one.
