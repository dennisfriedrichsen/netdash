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


# What a check actually proves, which is not the same for all of them.
#
# icmp and tcp are KERNEL checks. An echo reply and a completed handshake are
# both produced by the network stack in softirq context, with no userspace
# process involved -- the kernel accepts a connection onto a listening socket's
# backlog whether or not anything ever calls accept() on it.
#
# banner is a SERVICE check. It requires a userspace process to be scheduled,
# to accept the connection and to write to it.
#
# The distinction is not academic. A machine whose kernel is alive while
# userspace has stopped being scheduled answers every kernel check and is
# nonetheless completely useless -- which is exactly the outage this module was
# written for, and which the kernel checks alone happily report as "up".
KERNEL, SERVICE = "kernel", "service"


def parse_check(spec):
    """"icmp" / "tcp:22" / "banner:22" -> (kind, port). Raises on nonsense."""
    spec = str(spec).strip().lower()
    if spec == "icmp":
        return ("icmp", None)
    for prefix, kind in (("tcp:", "tcp"), ("banner:", "banner")):
        if spec.startswith(prefix):
            port = int(spec[len(prefix):])
            if not 1 <= port <= 65535:
                raise ValueError("port out of range: %s" % spec)
            return (kind, port)
    raise ValueError(
        'unknown check %r (want "icmp", "tcp:<port>" or "banner:<port>")' % spec)


LEVEL = {"icmp": KERNEL, "tcp": KERNEL, "banner": SERVICE}


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


def banner(addr, port, timeout):
    """Connect and require the service to speak first.

    Only meaningful for protocols where the server greets -- SSH, SMTP, FTP,
    IMAP. HTTP waits for a request and would time out here, which would read as
    a dead host; that is why this is a separate check kind you opt into per
    port rather than something tcp does automatically.

    The case it exists for: a connection that is accepted and then silent. The
    kernel completed that handshake on its own and parked it on sshd's backlog;
    if no banner ever arrives, sshd is not being scheduled. Kernel alive,
    userspace wedged -- a machine that answers every ping and cannot do a
    single useful thing.
    """
    try:
        s = socket.create_connection((addr, port), timeout)
    except ConnectionRefusedError:
        # Nothing is listening, so this check cannot answer the question it was
        # asked. Not DOWN -- the host clearly has a kernel, and a host with no
        # sshd is not a broken host. Let a kernel check speak instead.
        return ERROR, "nothing listening on %d" % port
    except ConnectionResetError:
        return ERROR, "reset on %d before any banner" % port
    except (socket.timeout, TimeoutError):
        return DOWN, "no answer on %d in %gs" % (port, timeout)
    except socket.gaierror:
        return ERROR, "cannot resolve"
    except OSError as e:
        if e.errno in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
            return DOWN, "host unreachable"
        return ERROR, "%s: %s" % (errno.errorcode.get(e.errno, e.errno), e)

    try:
        s.settimeout(timeout)
        data = s.recv(128)
    except (socket.timeout, TimeoutError):
        return DOWN, ("port %d accepted the connection but sent nothing in %gs "
                      "-- kernel alive, userspace wedged" % (port, timeout))
    except OSError as e:
        return ERROR, "%s reading from %d: %s" % (type(e).__name__, port, e)
    finally:
        s.close()

    if not data:
        return DOWN, ("port %d closed without a banner -- kernel alive, "
                      "userspace wedged" % port)
    text = data.decode("utf-8", "replace").splitlines()[0].strip()
    return UP, "banner from %d: %s" % (port, text[:60])


def probe(addr, checks, timeout):
    """Weigh every check and return the strongest evidence. (state, via, detail).

    Among checks at the same level these are alternatives, not a checklist: a
    host that answers ICMP but refuses connections is up, so any single UP
    wins. DOWN requires a check to have run cleanly and come back empty, and if
    a check could not run at all the verdict is ERROR -- a fleet-wide red
    caused by this server losing its ping binary is worse than a fleet-wide
    grey caused by the same thing.

    The one asymmetry: a SERVICE check that fails outranks a KERNEL check that
    passes, because it proves strictly more. A completed handshake says the
    network stack is running; a banner says a process was scheduled to write
    it. When those two disagree, the machine is one whose kernel is up and
    whose userspace is not, and reporting that as "up" because it answered a
    ping is the false reassurance this whole module exists to prevent.

    That asymmetry is also why this can no longer return early on the first UP:
    a later service check may still overturn it.
    """
    ip = resolve(addr)
    if ip is None:
        return ERROR, None, "cannot resolve %s" % addr

    # Tracked separately rather than as one "worst so far", because the
    # precedence is not a severity ordering and collapsing them got it wrong:
    # any check that could not run outranks any that ran and failed, no matter
    # which came first in the list.
    service_down = None
    up = None
    err = None
    down = None
    for spec in checks:
        try:
            kind, port = parse_check(spec)
        except ValueError as e:
            err = err or (ERROR, spec, str(e))
            continue
        if kind == "icmp":
            state, detail = icmp(ip, timeout)
        elif kind == "tcp":
            state, detail = tcp(ip, port, timeout)
        else:
            state, detail = banner(ip, port, timeout)

        if state == DOWN and LEVEL[kind] == SERVICE:
            service_down = service_down or (DOWN, spec, detail)
        elif state == UP:
            up = up or (UP, spec, detail)
        elif state == ERROR:
            err = err or (ERROR, spec, detail)
        else:
            down = down or (DOWN, spec, detail)

    # Order is the precedence: proven-wedged, then any sign of life, then an
    # inability to tell, then a plain no-answer.
    return (service_down or up or err or down
            or (ERROR, None, "no checks configured"))
