"""Is a host actually reachable? -- what separates "stale" from "down".

A push-only dashboard cannot tell those apart. No sample arrived; that is all it
knows. A hard-locked VM and a collector whose cron entry was commented out look
identical, so both read as a muted "Stale" -- which is the right colour for one
of them and badly wrong for the other.

This module answers the narrow question the ingest path cannot: does anything on
that address still respond? It is deliberately not a health check. A host that
answers is not necessarily well, it is merely alive, and that is exactly the
distinction the dashboard is missing.

Three outcomes, and the third is the important one:

  UP      something answered. The machine's kernel is scheduling.
  DOWN    the probe ran correctly and nothing answered.
  ERROR   the probe could not be carried out at all.

ERROR is never DOWN. A missing ping binary, a hardened unit that cannot open an
ICMP socket, a name that no longer resolves -- all of those say something about
*this server*, not about the host being probed, and a monitor that paints hosts
red because of its own broken plumbing teaches you to stop believing red.
"""

import errno
import shutil
import socket
import subprocess

UP, DOWN, ERROR = "up", "down", "error"

_PING = shutil.which("ping")


def parse_check(spec):
    """"icmp" or "tcp:22" -> ("icmp", None) / ("tcp", 22). Raises on nonsense."""
    spec = str(spec).strip().lower()
    if spec == "icmp":
        return ("icmp", None)
    if spec.startswith("tcp:"):
        port = int(spec[4:])
        if not 1 <= port <= 65535:
            raise ValueError("port out of range: %s" % spec)
        return ("tcp", port)
    raise ValueError('unknown check %r (want "icmp" or "tcp:<port>")' % spec)


def resolve(addr):
    """Address -> literal IP, or None if the name does not resolve.

    Done here rather than left to ping so that an unresolvable name lands as
    ERROR instead of being buried in ping's exit code, and so the ICMP and TCP
    checks are aimed at the same address rather than each resolving separately.
    """
    try:
        infos = socket.getaddrinfo(addr, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


def tcp(addr, port, timeout):
    """Connect and hang up. A refusal counts as UP.

    That is the point of using TCP at all: a closed port answers with a RST,
    and generating that RST is work the host's kernel had to schedule. "Nothing
    is listening on 22" is proof of life, and it is proof from a host that
    filters ICMP entirely.
    """
    try:
        socket.create_connection((addr, port), timeout).close()
        return UP, "connected on %d" % port
    except ConnectionRefusedError:
        return UP, "refused on %d (host answered)" % port
    except ConnectionResetError:
        return UP, "reset on %d (host answered)" % port
    except (socket.timeout, TimeoutError):
        return DOWN, "no answer on %d in %gs" % (port, timeout)
    except socket.gaierror:
        return ERROR, "cannot resolve"
    except OSError as e:
        # EHOSTUNREACH on a LAN is an unanswered ARP: the host is not there.
        # ENETUNREACH and EACCES are this server's own routing and firewall,
        # which say nothing about the host -- those stay ERROR.
        if e.errno in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
            return DOWN, "host unreachable"
        return ERROR, "%s: %s" % (errno.errorcode.get(e.errno, e.errno), e)


def icmp(addr, timeout):
    """One echo request via the system ping binary.

    The binary rather than a raw socket on purpose: raw ICMP needs CAP_NET_RAW,
    which the shipped systemd unit deliberately does not grant, and ping is
    already installed, already setuid or capability-tagged, and already correct
    about the parts of ICMP that are fiddly.

    Exit status alone does not separate "no reply" from "could not send", so the
    discriminator is stderr: every ping implementation reports "no packets
    received" on stdout in its summary, and reserves stderr for its own
    failures -- unknown host, permission denied, socket errors.
    """
    if not _PING:
        return ERROR, "no ping binary on this server"
    try:
        p = subprocess.run(
            [_PING, "-n", "-c", "1", addr],
            timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return DOWN, "no reply in %gs" % timeout
    except OSError as e:
        return ERROR, "cannot run ping: %s" % e
    if p.returncode == 0:
        return UP, "icmp reply"
    err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    if err:
        return ERROR, "ping: %s" % err[0]
    return DOWN, "no icmp reply"


def probe(addr, checks, timeout):
    """Run checks until one says UP. Returns (state, via, detail).

    Any single UP wins -- the checks are alternative ways of asking the same
    question, and a host that answers one and ignores another is still up.
    DOWN requires every check to have run cleanly and come back empty; if any
    of them could not run, the verdict is ERROR, because a fleet-wide red
    caused by this server losing its ping binary is worse than a fleet-wide
    grey caused by the same thing.
    """
    ip = resolve(addr)
    if ip is None:
        return ERROR, None, "cannot resolve %s" % addr

    # Tracked separately rather than as one "worst so far", because the
    # precedence is not a severity ordering and collapsing them got it wrong:
    # any check that could not run outranks any that ran and failed, no matter
    # which came first in the list.
    err = None
    down = None
    for spec in checks:
        try:
            kind, port = parse_check(spec)
        except ValueError as e:
            err = err or (ERROR, spec, str(e))
            continue
        state, detail = (icmp(ip, timeout) if kind == "icmp"
                         else tcp(ip, port, timeout))
        if state == UP:
            return UP, spec, detail
        if state == ERROR:
            err = err or (ERROR, spec, detail)
        elif down is None:
            down = (DOWN, spec, detail)
    return err or down or (ERROR, None, "no checks configured")
