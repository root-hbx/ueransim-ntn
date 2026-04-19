#!/usr/bin/env bash
# Raise kernel ARP/neighbor table thresholds so the host can tolerate
# the ~700 veths created by `ueran.py up -n 350`.
#
# Scope: runtime only (not written to /etc/sysctl.d) — values reset on
# reboot. Re-run after each boot (or before a large-N launch).
#
# Default Ubuntu gc_thresh3=1024; a 350-pair launch blows past it and
# causes "neighbour: arp_cache: neighbor table overflow!" in dmesg,
# wedging the host's entire outbound network until teardown.
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
    net.ipv6.neigh.default.gc_thresh3=16384

echo
echo "OK. Current gc_thresh3 (v4/v6):"
sysctl -n net.ipv4.neigh.default.gc_thresh3 net.ipv6.neigh.default.gc_thresh3
echo
echo "Verify during a large launch:  watch -n 1 'ip -4 neigh | wc -l'"
echo "Overflow watchdog:             sudo dmesg -wT | grep -iE 'neighbor table overflow'"
