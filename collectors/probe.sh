#!/bin/sh
# netdash platform probe -- read-only. Dumps the raw output the collector parses,
# so a new OS can be supported against real output instead of an assumption.
#
#   sh probe.sh            # print
#   sh probe.sh > out.txt  # capture and send
#
# Reads nothing sensitive: no config, no tokens, no file contents beyond
# /etc/os-release and kernel counters.
set -u

sec() { printf '\n===== %s =====\n' "$1"; }
try() { printf '\n--- $ %s\n' "$*"; "$@" 2>&1 | head -40 || echo "(failed: $?)"; }

sec "identity"
try uname -a
try uname -s
try uname -m
try uname -r
[ -r /etc/os-release ] && { printf '\n--- /etc/os-release\n'; cat /etc/os-release; }
[ -r /etc/alpine-release ] && { printf '\n--- /etc/alpine-release\n'; cat /etc/alpine-release; }

sec "shell + tools"
for t in sh awk sed tr df mount sysctl curl wget fetch ftp crontab systemctl rc-update zfs; do
  p=$(command -v "$t" 2>/dev/null) && printf '  %-10s %s\n' "$t" "$p" || printf '  %-10s -\n' "$t"
done
printf '\n--- awk implementation\n'; awk --version 2>&1 | head -2 || awk -W version 2>&1 | head -2
printf '\n--- 32-bit awk check (must print 4110864384, not -2147483648)\n'
echo | awk 'END{ printf "  %%d   -> %d\n  %%.0f -> %.0f\n", 4014516*1024, 4014516*1024 }'

sec "cpu"
try cat /proc/stat
try sysctl -n kern.cp_time
try sysctl kern.cp_time

sec "memory"
try cat /proc/meminfo
try sysctl -n hw.physmem
try sysctl -n hw.physmem64
try sysctl -n hw.usermem
try sysctl vm.uvmexp
try sysctl vm.uvmexp2
try sysctl -n vm.stats.vm.v_page_size
try sysctl -n vm.stats.vm.v_free_count
try sysctl -n vm.stats.vm.v_inactive_count

# OpenBSD refuses vm.uvmexp through sysctl and says to use vmstat; NetBSD is
# expected to be the same shape. These are the fields the collector parses.
try vmstat
try vmstat -s
try top -b
printf '\n--- $ sysctl -a | grep -i uvm (available uvm nodes)\n'
sysctl -a 2>/dev/null | grep -i uvm | head -30 || echo "(none)"

sec "uptime / boottime"
try cat /proc/uptime
try sysctl -n kern.boottime
try sysctl kern.boottime
try date +%s

sec "disk"
try df -P -k
try df -k
try df -k -l
try df -k -T
try df -k -T -l
try df -P -B1
printf '\n--- which df options are rejected?\n'
for o in "-l" "-x tmpfs" "-T" "-B1" "-P"; do
  printf '  df %-10s ' "$o"
  df $o >/dev/null 2>&1 && echo OK || echo "REJECTED"
done
try cat /proc/self/mounts
try mount

sec "hardware model (SBC detection)"
for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
  [ -r "$f" ] && { printf '\n--- %s\n' "$f"; tr -d '\000' < "$f"; echo; }
done

printf '\n===== end =====\n'
