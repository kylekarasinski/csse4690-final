#!/usr/bin/env bash
# Sets up a Linux box as a router and steers one direction of traffic
# (SOURCE -> DESTINATION) into NFQUEUE so the Python chaos router can
# drop/delay those packets. Configurable via environment variables.
# Usage: sudo SOURCE=10.0.1.2 DESTINATION=10.0.2.2 ./script.sh
# Exit on any error (-e), unset variable (-u), or failure in a pipeline (pipefail).
set -euo pipefail

# NFQUEUE number; must match --queue-num passed to the Python router.
QUEUE_NUM="${QUEUE_NUM:-0}"
# Only packets from SOURCE to DESTINATION are queued; the reverse direction is not.
SOURCE="${SOURCE:-10.0.1.2}"
DESTINATION="${DESTINATION:-10.0.2.2}"

# sysctl and iptables both need root.
if [[ "${EUID}" -ne 0 ]]; then
	echo "Run this script with sudo." >&2
	exit 1
fi

# Enable IPv4 forwarding so this machine routes between the two networks.
# Not persistent: this resets on reboot.
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# Idempotent rule install: -C checks whether the rule already exists, and the
# insert after || only runs if it doesn't, so re-running won't create duplicates.
# Inserted at position 1 of FORWARD so it matches before any other rule.
# --queue-bypass: if no process is bound to the queue, packets are accepted
# instead of dropped, so traffic keeps flowing when the Python router isn't running.
iptables -C FORWARD -s "${SOURCE}" -d "${DESTINATION}" -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass >/dev/null 2>&1 ||
iptables -I FORWARD 1 -s "${SOURCE}" -d "${DESTINATION}" -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass

echo "Router forwarding enabled."
echo "NFQUEUE rule: ${SOURCE} -> ${DESTINATION}, queue ${QUEUE_NUM}."