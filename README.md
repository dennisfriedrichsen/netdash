# netdash

A small, self-hosted CPU / memory / disk dashboard for the home LAN. Collectors
push JSON over HTTP; the server keeps a rolling window in SQLite and serves two
views: an overview grid and a per-host detail page.

No agents to compile, no third-party service, no node cap. Server is Python 3
standard library only; collectors are POSIX `sh` plus the tools already on each OS.

Network throughput is deliberately not collected — that lives in the Unifi console.

## Layout

```
server/       central service + dashboard UI (runs on 10.0.0.3)
collectors/   per-OS collector scripts
  linux/      /proc/stat, /proc/meminfo, df
  freebsd/    sysctl, df
  macos/      top, vm_stat, sysctl, df
  install.sh  installer for Linux + FreeBSD hosts
homebrew/     Homebrew formula for the macOS hosts
```

## Server

```sh
sudo server/deploy.sh          # install/refresh + enable systemd unit
```

Copies the code to `/opt/netdash`, config to `/etc/netdash/server.json`, database
to `/var/lib/netdash/netdash.db`, and runs it as the `netdash` system user.
Re-running `deploy.sh` is the upgrade path; it never overwrites an existing config.

Dashboard: **http://10.0.0.3:8080/**

### Config (`/etc/netdash/server.json`)

| key | meaning |
|---|---|
| `bind_host` / `bind_port` | listen address (LAN only — do not port-forward) |
| `ingest_token` | shared secret; collectors send it as `X-Netdash-Token` |
| `retention_hours` | rolling window; older samples are pruned every 10 min |
| `stale_after_seconds` | a host with no sample for this long shows as **Stale** |
| `thresholds` | warn/crit percentages per metric |
| `truenas` | API polling for the NAS (see below) |

### Thresholds

Starting points — tune in the config, no code change needed:

| metric | warn | crit |
|---|---|---|
| CPU | 80% | 95% |
| Memory | 85% | 95% |
| Disk | 85% | 95% |

A host's overall status is the worst of its three metrics. CPU is the noisiest
signal at a 30–60s sample rate — if the grid flickers amber, raise the CPU warn
threshold rather than lowering the sample interval.

### API

| route | purpose |
|---|---|
| `POST /api/ingest` | collector push (requires `X-Netdash-Token`) |
| `GET /api/overview` | all hosts, latest sample + status |
| `GET /api/host/<name>?minutes=60` | one host + recent history |
| `GET /api/health` | liveness |

Payload shape:

```json
{ "host": "hermes", "os": "Ubuntu 24.04 (x86_64)", "cpu_pct": 12.4,
  "mem_used_bytes": 1234, "mem_total_bytes": 5678, "uptime_seconds": 99,
  "disks": [ { "mount": "/", "used_bytes": 1, "total_bytes": 2 } ] }
```

## Collectors — Linux and FreeBSD

```sh
git clone <repo> netdash && cd netdash/collectors
sudo ./install.sh --url http://10.0.0.3:8080/api/ingest --token <TOKEN>
```

The installer verifies the collector runs *and* that a real post succeeds before
it schedules anything, so a broken host fails immediately rather than silently.

- **Linux with systemd** → `netdash-collector.timer`, every 30s
- **FreeBSD / no systemd** → root crontab, every 60s

Updating later: `git pull && sudo ./install.sh` — it overwrites the script and
keeps the existing config. `sudo ./uninstall.sh` removes it.

Config lives at `/etc/netdash/collector.conf` (Linux) or
`/usr/local/etc/netdash/collector.conf` (FreeBSD), mode 600.

## Collectors — macOS

Via Homebrew (see `homebrew/netdash-collector.rb`):

```sh
brew install <tap>/netdash-collector
$EDITOR $(brew --prefix)/etc/netdash/collector.conf   # set URL + token
brew services start netdash-collector                 # launchd, every 60s
```

Homebrew never auto-starts a service on install, so `brew services start` is a
one-time extra step. After that, `brew upgrade netdash-collector` is enough —
it restarts the job for you.

## TrueNAS

Nothing is installed on the NAS. The server polls its REST API on a schedule.
Create an API key in the TrueNAS UI (Settings → API Keys), then in
`/etc/netdash/server.json`:

```json
"truenas": { "enabled": true, "host": "10.0.0.2", "api_key": "1-xxxxx", "poll_seconds": 60 }
```

Check it before enabling:

```sh
sudo python3 /opt/netdash/server/truenas.py /etc/netdash/server.json
```

That prints exactly what the dashboard will show, or the API error.

## Troubleshooting

```sh
systemctl status netdash                      # server
journalctl -u netdash -n 50                   # server log
systemctl list-timers netdash-collector.timer # collector schedule (Linux)
netdash-collector --print                     # what this host would send
curl -s http://10.0.0.3:8080/api/overview | python3 -m json.tool
```

A host showing **Stale** is reachable-but-silent: its timer stopped, or the token
is wrong (the server answers 401 and the collector exits non-zero).
