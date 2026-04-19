#!/usr/bin/env bash
# Raise kernel limits so the host can tolerate large `ueran.py up -n N` runs.
#
# Tuned for N up to the 500-pair hard ceiling (≈1015 veths on the Docker
# bridge, which itself caps at BR_MAX_PORTS=1024 — see docker/README.md).
# Two kernel tables are the bottlenecks below that ceiling:
#
#   1. ARP/neighbor cache (gc_thresh3)
#        Default 1024. At N ≳ 240 the host drops new entries and its own
#        outbound traffic (curl, ping) also hangs — recovers only on
#        `ueran.py down`. dmesg: "neighbour: arp_cache: neighbor table
#        overflow!".
#
#   2. inotify instances (fs.inotify.max_user_instances)
#        Default 128. Each container's containerd-shim/runc plus any
#        log-tailing tool consumes inotify instances; large N fails with
#        "No space left on device" during docker-compose up even though
#        disk is fine.
#
# Scope: runtime only (not written to /etc/sysctl.d) — values reset on
# reboot. Re-run after each boot, or before any large-N launch.
set -euo pipefail

SUDO=""
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

$SUDO sysctl -w \
    net.ipv4.neigh.default.gc_thresh1=4096 \
    net.ipv4.neigh.default.gc_thresh2=8192 \
    net.ipv4.neigh.default.gc_thresh3=16384 \
    net.ipv6.neigh.default.gc_thresh1=4096 \
    net.ipv6.neigh.default.gc_thresh2=8192 \
    net.ipv6.neigh.default.gc_thresh3=16384 \
    fs.inotify.max_user_instances=8192 \
    fs.inotify.max_user_watches=1048576

echo
echo "OK. Current thresholds:"
echo "  ARP v4/v6 gc_thresh3:   $(sysctl -n net.ipv4.neigh.default.gc_thresh3) / $(sysctl -n net.ipv6.neigh.default.gc_thresh3)"
echo "  inotify instances:      $(sysctl -n fs.inotify.max_user_instances)"
echo "  inotify watches:        $(sysctl -n fs.inotify.max_user_watches)"
echo
echo "Verify during a large launch:"
echo "  watch -n 1 'ip -4 neigh | wc -l'"
echo "  sudo dmesg -wT | grep -iE 'neighbor table overflow|No space left|inotify'"
