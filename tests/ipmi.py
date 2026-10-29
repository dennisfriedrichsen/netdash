"""The management-controller check: reachability without credentials, and
hardware sensors only with them. Driven by the `ipmi` case in tests/run.sh.

The pre-session reply in tests/fixtures/ipmi is real, captured from mercury's
Supermicro BMC. The ipmitool output below is NOT: no credentials were
available to capture any, so it is written to ipmitool's documented format
(ipmi_sdr.c, ipmi_chassis.c) and stands in until a real capture replaces it.
Everything here runs on 127.0.0.1; nothing reaches a BMC.
"""
import json, os, socket, stat, sys, tempfile, threading, time
root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "server"))
import services

TMP = tempfile.mkdtemp()
REPLY = bytes.fromhex(open(os.path.join(
    root, "tests", "fixtures", "ipmi", "channel-auth-reply.hex")).read().strip())
out = {}

# ---------- the pre-session ping ----------

out["request"] = services.channel_auth_request(0).hex()
out["parsed"] = services.parse_channel_auth(REPLY)
out["garbage"] = services.parse_channel_auth(b"\x06\x00\xff\x06" + b"\x00" * 30)


def udp_bmc(reply):
    """A UDP listener that answers every datagram with `reply` (or nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    seen = []

    def loop():
        while True:
            try:
                data, peer = s.recvfrom(512)
            except OSError:
                return
            seen.append(data)
            if reply is not None:
                s.sendto(reply, peer)
    threading.Thread(target=loop, daemon=True).start()
    return s.getsockname()[1], seen


def ping(port, timeout=1):
    services.IPMI_PORT = port
    try:
        return list(services.ipmi_ping("127.0.0.1", timeout))
    finally:
        services.IPMI_PORT = 623


port, seen = udp_bmc(REPLY)
out["ping_ok"] = ping(port)
out["ping_sent_is_a_read"] = [d[3] == 0x07 and d[19] == 0x38 for d in seen]
port, _ = udp_bmc(None)
t0 = time.monotonic()
out["ping_silent"] = ping(port)
out["ping_silent_s"] = time.monotonic() - t0
port, _ = udp_bmc(b"HTTP/1.0 400 Bad Request\r\n\r\n")
out["ping_not_ipmi"] = ping(port)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 0))
closed = s.getsockname()[1]
s.close()
out["ping_closed"] = ping(closed)

# ---------- the whole check, credentials or none ----------


def ipmi(**kw):
    c = dict({"kind": "ipmi", "host": "mercury", "address": "127.0.0.1",
              "enabled": True, "timeout_seconds": 1, "https": False}, **kw)
    return services.parse_config({"checks": [c]})[0]


port, _ = udp_bmc(REPLY)
services.IPMI_PORT = port
out["reach_only"] = list(services.run(ipmi(), {})[:3])
port2, _ = udp_bmc(None)
services.IPMI_PORT = port2
out["not_answering"] = list(services.run(ipmi(), {})[:2])
services.IPMI_PORT = port

# ---------- ipmitool output ----------

SDR = """\
CPU Temp         | 01h | ok  |  3.1 | 45 degrees C
System Temp      | 11h | ok  |  7.1 | 31 degrees C
Peripheral Temp  | 12h | ok  |  7.2 | 38 degrees C
DIMMA1 Temp      | B0h | unc | 32.64 | 86 degrees C
FAN1             | 41h | ok  | 29.1 | 1500 RPM
FAN2             | 42h | ok  | 29.2 | 1400 RPM
FAN3             | 43h | ns  | 29.3 | No Reading
FAN4             | 44h | lcr | 29.4 | 200 RPM
12V              | 30h | ok  |  7.17 | 12.06 Volts
PS1 Status       | C8h | ok  | 10.1 | Presence detected
PS2 Status       | C9h | ok  | 10.2 | Presence detected, Failure detected
Chassis Intru    | AAh | ok  | 23.1 |
"""
CHASSIS = """\
System Power         : on
Power Overload       : false
Power Interlock      : inactive
Main Power Fault     : false
Power Control Fault  : false
Power Restore Policy : always-off
Last Power Event     :
Chassis Intrusion    : inactive
Front-Panel Lockout  : inactive
Drive Fault          : false
Cooling/Fan Fault    : true
"""
sensors = services.parse_sdr(SDR)
out["sensors"] = [[x["name"], x["category"], x["severity"]] for x in sensors]
chassis = services.parse_chassis(CHASSIS)
out["chassis"] = chassis
hw = services.summarize_hardware(sensors, chassis)
out["hw"] = [hw["status"], sorted(p["name"] for p in hw["problems"]), hw["max_temp_c"],
             hw["no_reading"], hw["power"], hw["counts"].get("fan")]
healthy = "\n".join(l for l in SDR.splitlines()
                    if not l.startswith(("DIMMA1", "FAN4", "PS2")))
out["hw_ok"] = services.summarize_hardware(
    services.parse_sdr(healthy),
    services.parse_chassis(CHASSIS.replace("Cooling/Fan Fault    : true",
                                           "Cooling/Fan Fault    : false")))["status"]
# Nothing readable is unknown, not ok -- the case this module exists to get right.
out["hw_empty"] = services.summarize_hardware(
    services.parse_sdr("FAN1 | 41h | ns | 29.1 | No Reading\nnonsense line"), None)
out["chassis_junk"] = services.parse_chassis("Error: no response")


# A stand-in ipmitool: records its argv, answers per subcommand.
def fake_ipmitool(name, body):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(p, stat.S_IRWXU)
    return p


argv_log = os.path.join(TMP, "argv.log")
sdr_file = os.path.join(TMP, "sdr.txt")
chassis_file = os.path.join(TMP, "chassis.txt")
open(sdr_file, "w").write(SDR)
open(chassis_file, "w").write(CHASSIS)
good = fake_ipmitool("ipmitool-good", """
echo "$@" >> %s
case "$*" in
  *"sdr elist"*) cat %s ;;
  *"chassis status"*) cat %s ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
""" % (argv_log, sdr_file, chassis_file))
denied = fake_ipmitool("ipmitool-denied", """
echo "$@" >> %s
echo "> RAKP 2 message indicates an error : unauthorized name" >&2
echo "Error: Unable to establish IPMI v2 / RMCP+ session" >&2
exit 1
""" % argv_log)
slow = fake_ipmitool("ipmitool-slow", "sleep 30\n")

PASSWORD = "bmc-p4ss-not-for-argv"
pwfile = os.path.join(TMP, "bmc.pw")
open(pwfile, "w").write(PASSWORD + "\n")
os.chmod(pwfile, 0o600)

c = ipmi(username="netdash-ro", password_file=pwfile, ipmitool_path=good)
o, d, facts, _ = services.run(c, {})
out["with_creds"] = [o, d, facts["hardware"]["status"], facts["hardware"]["power"]]
argv = open(argv_log).read()
out["argv"] = {"user_privilege": "-L USER" in argv, "password_from_file": "-f " + pwfile in argv,
               "password_absent": PASSWORD not in argv,
               "only_reads": sorted({" ".join(l.split()[-2:]) for l in argv.splitlines()})}
v = services.view(c, services.fold(None, c, o, d, facts, 1, time.time()), time.time())
out["view_hw"] = [v["state"], v["severity"], v["hardware"]["status"]]

# Sensors read on their own clock: a second round inside the interval reuses them.
memo = {}
open(argv_log, "w").close()
services.run(c, memo)
services.run(c, memo)
out["sensor_reads_in_two_rounds"] = len(open(argv_log).read().splitlines())

c = ipmi(username="netdash-ro", password_file=pwfile, ipmitool_path=denied)
memo = {}
open(argv_log, "w").close()
first = services.run(c, memo)
memo["hw"]["checked_at"] = 0          # past the sensor interval: due again
second = services.run(c, memo)
out["denied"] = [first[0], first[2]["hardware"]["error"], second[2]["hardware"]["reason"],
                 len(open(argv_log).read().splitlines())]
v = services.view(c, services.fold(None, c, *second[:3], 1, time.time()), time.time())
out["denied_view"] = [v["state"], v["severity"]]
nc = ipmi()
v = services.view(nc, services.fold(None, nc, *services.run(nc, {})[:3], 1, time.time()),
                  time.time())
out["no_creds_view"] = [v["state"], v["severity"], v["hardware"]["status"]]

c = ipmi(username="netdash-ro", password_file=pwfile, ipmitool_path=slow,
         ipmitool_timeout_seconds=5)
t0 = time.monotonic()
r = services.run(c, {})
out["slow"] = [r[0], r[2]["hardware"], round(time.monotonic() - t0)]

c = ipmi(username="netdash-ro", password_file=pwfile,
         ipmitool_path=os.path.join(TMP, "not-installed"))
out["no_tool"] = services.run(c, {})[2]["hardware"]
c = ipmi(username="netdash-ro", password_file=os.path.join(TMP, "nope"), ipmitool_path=good)
out["no_pw_file"] = services.run(c, {})[2]["hardware"]
out["secret_in_output"] = PASSWORD in json.dumps(out)

print(json.dumps(out))
