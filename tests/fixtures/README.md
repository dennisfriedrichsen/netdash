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

`cp_time.txt` holds two lines: two successive reads, so the mock can return a
delta rather than a single sample.
