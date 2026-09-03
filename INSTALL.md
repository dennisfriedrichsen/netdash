# Installation

Netdash has one central server and a collector on each monitored host. Linux
and BSD collectors are installed from this repository; macOS uses Homebrew.
TrueNAS is polled by the server and does not need a collector.

Examples below use `https://netdash.example/api/ingest`. Replace it with the
URL clients on your network use to reach the netdash server. `YOUR_INGEST_TOKEN` is the
`ingest_token` from the server configuration.

This document is for a human working through the install by hand. Handing the
job to an LLM agent instead? Give it [INSTALL-PROMPT.md](INSTALL-PROMPT.md) --
it's written as a runbook the agent executes, not prose for a person to read.

## Server

The server itself is Python 3 standard library only -- no pip packages -- so
the only platform-specific part of installing it is service management.
Linux uses systemd; FreeBSD uses rc.d and pw(8) instead, via a separate
deploy script.

### Linux (systemd)

Requires Python 3 and systemd. From a repository checkout, run:

```sh
sudo server/deploy.sh
```

This installs the application under `/opt/netdash`, creates a `netdash` system
user, enables `netdash.service`, and stores persistent data in
`/var/lib/netdash`. On the first deployment, edit
`/etc/netdash/server.json`; later deployments preserve that file.

Check the service before installing collectors:

```sh
systemctl status netdash
curl http://localhost:8080/api/health
```

### FreeBSD (rc.d)

Requires Python 3 (`pkg install python3`). From a repository checkout, run:

```sh
sudo server/deploy.freebsd.sh
```

This installs the application under `/usr/local/netdash`, creates a `netdash`
system user via `pw(8)`, enables it in `rc.conf` (`netdash_enable=YES`), and
stores persistent data in `/var/db/netdash`. On the first deployment, edit
`/usr/local/etc/netdash/server.json`; later deployments preserve that file.
The rc.d script sends the server's log output to syslog under the `netdash`
tag.

Check the service before installing collectors:

```sh
service netdash status
curl http://localhost:8080/api/health
```

To manage it directly instead of through the deploy script:

```sh
sysrc netdash_enable=YES
service netdash start   # or: stop, restart, status
```

### Either platform

Do not expose an unencrypted netdash instance directly to the internet. Keep it
on a trusted network or put it behind an HTTPS reverse proxy.

## Linux and BSD collectors

Install Git and an HTTP client first:

| Platform | Prerequisites |
|---|---|
| Debian, Ubuntu, Raspberry Pi OS | `sudo apt-get install -y git curl` |
| Fedora | `sudo dnf install -y git curl` |
| Arch Linux | `sudo pacman -S git curl` |
| openSUSE | `sudo zypper install -y git curl` |
| Alpine | `sudo apk add git curl` |
| FreeBSD | `sudo pkg install git curl` |
| OpenBSD | `doas pkg_add git curl` |
| NetBSD | `sudo pkgin install git curl` |

Then clone the repository and run the installer:

```sh
git clone https://github.com/dennisfriedrichsen/netdash.git
cd netdash/collectors
sudo ./install.sh --url https://netdash.example/api/ingest --token YOUR_INGEST_TOKEN
```

On OpenBSD, use `doas ./install.sh ...` instead if `sudo` is not installed.

The installer prints the collected JSON and verifies a real post before it
schedules anything. It installs the following schedule:

- Linux with systemd: `netdash-collector.timer`, every 30 seconds
- Alpine/OpenRC: root crontab, every 60 seconds; `crond` is enabled and started
- FreeBSD, OpenBSD, and NetBSD: root crontab, every 60 seconds

It also installs `netdash-patchcheck` and schedules it **daily** — as
`netdash-patchcheck.timer` under systemd, otherwise a root crontab entry at
03:xx — then runs it once so the patch badge is populated immediately. A
failure there is not fatal: the host still reports CPU, memory, and disk, and
its patch badge reads "not checked" until the check succeeds.

The daily job refreshes package metadata (`apt-get update`, `dnf5 makecache`,
`zypper refresh`, `apk update`, `pkg audit -F`, `syspatch -c`,
`pkg_admin fetch-pkg-vulnerabilities`). To forbid that, add
`NETDASH_PATCH_REFRESH="no"` to `collector.conf`; the badge then reports the
age of the metadata already on disk. See [PATCH-CHECKS.md](PATCH-CHECKS.md).

Configuration is stored with mode 600 at `/etc/netdash/collector.conf` on
Linux, OpenBSD, and NetBSD, or `/usr/local/etc/netdash/collector.conf` on
FreeBSD.

FreeBSD is tested. OpenBSD and NetBSD support is implemented but has not been
verified on real hardware. Before relying on memory figures from either, run:

```sh
sh collectors/probe.sh > probe-$(hostname).txt
```

### Update or remove

An update preserves the existing collector configuration:

```sh
git pull
sudo ./collectors/install.sh
```

To remove the collector while retaining its configuration:

```sh
sudo ./collectors/uninstall.sh
```

## macOS collector

Install from the Homebrew tap:

```sh
brew install dennisfriedrichsen/tap/netdash-collector
$EDITOR "$(brew --prefix)/etc/netdash/collector.conf"
netdash-collector --print
brew services start netdash-collector
```

Set the server URL and token in `collector.conf` before starting the service.
Homebrew does not start the service during installation. After the one-time
`brew services start`, `brew upgrade netdash-collector` upgrades the collector
and restarts its launchd job.

### Patch check on macOS

`collectors/macos/netdash-patchcheck.sh` reads the software-update scan macOS
already performs every six hours, so it is fast and needs no root. **The tap
formula does not install or schedule it yet** — until it does, a Mac shows
"not checked". To wire it up by hand:

```sh
sudo cp collectors/macos/netdash-patchcheck.sh \
        "$(brew --prefix)/bin/netdash-patchcheck"
netdash-patchcheck --print          # confirm it reads the cached scan
(crontab -l 2>/dev/null; echo "17 3 * * * $(brew --prefix)/bin/netdash-patchcheck >/dev/null 2>&1") | crontab -
```

If `AutomaticCheckEnabled` is off on that Mac, the cached counts never advance;
the check reports the age of the last scan and the badge ages into "unknown"
rather than showing a stale zero as up to date.

## TrueNAS

Nothing is installed on the NAS. Create an API key in the TrueNAS UI, then add
its details to `/etc/netdash/server.json`:

```json
"truenas": {
  "enabled": true,
  "host": "truenas.example",
  "api_key": "1-xxxxx",
  "poll_seconds": 60
}
```

Test the configuration before restarting the server:

```sh
sudo python3 /opt/netdash/server/truenas.py /etc/netdash/server.json
sudo systemctl restart netdash
```

## Optional: end-of-life warnings

The server can flag hosts whose OS release is past end of life, using
[endoflife.date](https://endoflife.date). It is on by default, adds one outbound
request per product per day, and sends nothing about your fleet. To turn it off,
or to widen the warning window, edit `/etc/netdash/server.json`:

```json
"eol": { "enabled": true, "refresh_hours": 24, "warn_days": 30,
         "overrides": { "TrueNAS CORE": true } }
```

`overrides` covers products endoflife.date does not track — its `truenas`
product is SCALE-only, so CORE has to be stated here. The match is on the start
of the reported OS string, and the value is either a date or `true`.

Existing installations keep their config: `deploy.sh` never rewrites it, so this
block is absent on an upgraded server and the defaults above apply silently. Add
it only to change something, then restart:

```sh
sudo systemctl restart netdash
```

## Verify and troubleshoot

```sh
netdash-collector --print                         # payload without posting
netdash-patchcheck --print                        # patch check, writing nothing
curl -s localhost:8080/api/overview | python3 -m json.tool | grep -A6 '"eol"'
systemctl list-timers netdash-collector.timer     # Linux schedule
systemctl list-timers netdash-patchcheck.timer    # daily patch check
crontab -l | grep netdash                         # BSD/Alpine schedule
journalctl -u netdash -n 50                       # server log
curl https://netdash.example/api/overview         # received hosts
```

The most common causes of a missing or stale host are a stopped scheduler, an
incorrect ingest token, or a firewall blocking the collector from reaching the
server. An incorrect token produces an HTTP 401 response and a non-zero
collector exit status.
