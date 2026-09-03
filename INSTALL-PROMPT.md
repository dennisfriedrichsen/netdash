# Install prompt

This file is not written for a human to read top to bottom -- it is a
self-contained prompt for an LLM agent with shell access, meant to be pasted
into a fresh session on the machine being installed. Hand it the file (or its
raw URL) with a one-line request such as "install netdash here as the server"
or "install netdash here as a collector pointing at https://netdash.example",
and it should be able to do the rest without re-deriving the decisions
already made in this repo's own docs.

Everything after this line is the prompt.

---

You are installing **netdash**, a self-hosted CPU/memory/disk dashboard. One
host runs the central server; every monitored host (the server included, if
it should monitor itself) runs a small collector that pushes JSON to it on a
schedule. Your job is to get this specific machine into the right state,
verify it worked, and say plainly what you did -- not to redesign anything
here or touch hosts you were not asked about.

## Guardrails

- **Never bind the server to a public interface without TLS in front of it.**
  If asked to expose it beyond a trusted LAN, put it behind an HTTPS reverse
  proxy and say so; do not just open the port.
- **Never overwrite an existing config file.** Both deploy scripts and the
  collector installer already refuse to; do not work around that with `-f` or
  by hand-editing over the top without being asked to change a specific key.
- **Never invent the ingest token.** It is a shared secret between the server
  and every collector. Generate one only if the user confirms this is a new
  install with no existing token to reuse (`openssl rand -hex 32` is fine),
  and never print it into a log or commit it anywhere.
- **Ask rather than guess** when: it's unclear whether this host is the
  server, a collector, or both; the server's URL or token is not given and
  can't be found in an existing `collector.conf`/`server.json`; or a step
  would delete data (`uninstall.sh`, dropping the database, `pw userdel`).
- **Confirm destructive or service-affecting commands** the same way you
  would for any other task -- installing is one-way in the sense that it
  starts a service and schedules a recurring job on this machine.

## Step 0 -- work out what this host is

Ask, if it wasn't already stated:

1. Is this host the **server**, a **collector**, or both (a server that also
   monitors itself)?
2. If it's a collector (or both): what is the server's ingest URL
   (`https://netdash.example/api/ingest`) and ingest token?
3. If it's the server: is this a fresh install, or re-running to upgrade an
   existing one? (`deploy.sh` / `deploy.freebsd.sh` are safe either way and
   never clobber an existing config -- but the *first* run needs the config's
   placeholders filled in afterward, which an upgrade does not.)

Then detect the platform -- don't ask the user for what the shell already
knows:

```sh
uname -s          # Linux, FreeBSD, OpenBSD, NetBSD, Darwin
uname -m           # x86_64/amd64, aarch64/arm64, armv7l (Pi 3B+), ...
command -v systemctl >/dev/null && echo "has systemd"
```

Match against the support table in `README.md` (`## Supported platforms`) to
confirm this OS/version combination is one netdash actually runs on, not
merely one that looks close.

## Step 1 -- server (skip if this host is collector-only)

Requires only Python 3 (standard library, no pip packages). Which deploy
script depends on init system, not distro:

| platform | script | service manager |
|---|---|---|
| Linux (any distro with systemd) | `sudo server/deploy.sh` | systemd (`netdash.service`) |
| FreeBSD | `sudo server/deploy.freebsd.sh` | rc.d (`netdash_enable=YES`) |

Linux without systemd (e.g. Alpine as a *server*, not just a collector) has
no deploy script in this repo yet -- say so rather than improvising one, and
ask whether to write an OpenRC equivalent of `netdash.rc` first.

From a checkout of this repository:

```sh
git clone https://github.com/dennisfriedrichsen/netdash.git
cd netdash
sudo server/deploy.sh            # or: sudo server/deploy.freebsd.sh
```

This copies the code, creates a `netdash` system user, installs the config
**only if none exists yet**, and starts the service. Config path and default
`db_path` differ by platform (`/etc/netdash/server.json` +
`/var/lib/netdash` on Linux; `/usr/local/etc/netdash/server.json` +
`/var/db/netdash` on FreeBSD) -- everything else in the file is identical.

**On a fresh install**, open that config now and set at minimum:

- `ingest_token` -- replace the placeholder with the real shared secret
  decided in Step 0.
- `bind_host` -- `0.0.0.0` is fine on a trusted LAN; narrow it if the network
  is shared with untrusted devices.

Leave `thresholds`, `retention_hours`, `eol`, and `hosts` at their defaults
unless the user asked for something specific -- `README.md`'s `## Config`
section explains what each one does if a change is wanted later.

Restart to pick up the edited config, then verify:

```sh
sudo systemctl restart netdash          # Linux
sudo service netdash restart            # FreeBSD

curl -s http://localhost:8080/api/health
# {"ok": true, "hosts": N}
```

If this server also monitors itself, continue to Step 2 on the same host --
nothing about running both conflicts (the patch-check state file
deliberately lives under a different path than the server's database for
exactly this case).

## Step 2 -- collector (skip if this host is server-only)

For **Linux, FreeBSD, OpenBSD, or NetBSD**:

```sh
git clone https://github.com/dennisfriedrichsen/netdash.git   # skip if already cloned in Step 1
cd netdash/collectors
sudo ./install.sh --url <NETDASH_URL>/api/ingest --token <INGEST_TOKEN>
```

It refuses to proceed without `curl`/`wget`/`fetch` present -- install one
with the platform's package manager if it says so. It schedules
`netdash-collector` (every 30s under systemd, else a 60s cron/timer entry)
and `netdash-patchcheck` (daily), and it verifies a real POST to the server
succeeds *before* scheduling anything, so a wrong URL or token fails loudly
here rather than silently later.

For **macOS**, use Homebrew instead of this repo directly:

```sh
brew install dennisfriedrichsen/tap/netdash-collector
$EDITOR $(brew --prefix)/etc/netdash/collector.conf   # set NETDASH_URL, NETDASH_TOKEN
brew services start netdash-collector
```

For **TrueNAS**, do not install a collector at all -- it's polled by the
server over its API. See `README.md`'s `## TrueNAS` section: create an API
key in the TrueNAS UI, add a `"truenas"` block to `server.json`, and verify
with `sudo python3 server/truenas.py <path-to-server.json>` before enabling
it, since that prints exactly what the dashboard will show.

Verify this host reports correctly:

```sh
netdash-collector --print          # what this host would send, without sending it
```

## Step 3 -- confirm it actually shows up

From anywhere that can reach the server:

```sh
curl -s http://<server>:8080/api/overview | python3 -m json.tool
```

Check, for the host you just installed:

- it appears in `hosts` at all (a missing host after both a successful
  `install.sh` run and a `--print` that looked right usually means a firewall
  between the two, not a config error)
- `collector_outdated` is `false`, or explain why not (an old collector
  elsewhere in the fleet, not this host, sets the bar)
- `is_vm` is `true`/`false` as expected, or `null` with a plausible reason
  (pre-0.4.0 collector, or a platform this repo doesn't detect virt on yet --
  see `README.md`'s `## Bare metal or VM`)
- `status` is not `"stale"` a minute or two after install

## If something fails

| symptom | check |
|---|---|
| server won't start | `journalctl -u netdash -n 50` (Linux) / syslog for the `netdash` tag (FreeBSD, since `netdash.rc` uses `daemon -S`) |
| collector never appears | wrong token gives HTTP 401 and a non-zero exit from `install.sh`'s verification POST -- re-check `<INGEST_TOKEN>` matches the server's config exactly |
| host shows **Stale** | its scheduler stopped, or the server was unreachable when it last tried -- `systemctl list-timers netdash-collector.timer` (Linux) or check the cron entry (BSDs), then `netdash-collector --print` by hand |
| patch badge stuck on "unknown" | `netdash-patchcheck --print` to run it without writing state, and read its own error output |

Do not guess past this table -- if the failure isn't one of these, say what
you observed and ask before trying something invasive (reinstalling,
resetting the database, regenerating the token).

## Definition of done

State plainly, not just "it's installed":

- which role(s) this host now has (server / collector / both)
- the exact config file(s) you edited and which keys you changed
- the verification output that proves it worked (`/api/health`,
  `/api/overview` excerpt for this host, or `--print` output)
- anything you skipped and why (e.g. "left `bind_host` at `0.0.0.0` since you
  said this LAN is trusted")
