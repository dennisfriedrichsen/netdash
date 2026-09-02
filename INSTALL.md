# Installation

Netdash has one central server and a collector on each monitored host. Linux
and BSD collectors are installed from this repository; macOS uses Homebrew.
TrueNAS is polled by the server and does not need a collector.

Examples below use `https://netdash.example/api/ingest`. Replace it with the
URL clients on your network use to reach the netdash server. `YOUR_INGEST_TOKEN` is the
`ingest_token` from the server configuration.

## Server

The server requires Python 3 and systemd. From a repository checkout, run:

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

## Verify and troubleshoot

```sh
netdash-collector --print                         # payload without posting
systemctl list-timers netdash-collector.timer     # Linux schedule
crontab -l | grep netdash                         # BSD/Alpine schedule
journalctl -u netdash -n 50                       # server log
curl https://netdash.example/api/overview         # received hosts
```

The most common causes of a missing or stale host are a stopped scheduler, an
incorrect ingest token, or a firewall blocking the collector from reaching the
server. An incorrect token produces an HTTP 401 response and a non-zero
collector exit status.
