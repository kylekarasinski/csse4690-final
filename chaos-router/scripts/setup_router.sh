#!/usr/bin/env bash
set -euo pipefail

QUEUE_NUM="${QUEUE_NUM:-0}"
SOURCE="${SOURCE:-10.0.1.2}"
DESTINATION="${DESTINATION:-10.0.2.2}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

sysctl -w net.ipv4.ip_forward=1 >/dev/null

iptables -C FORWARD -s "${SOURCE}" -d "${DESTINATION}" \
  -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass >/dev/null 2>&1 ||
iptables -I FORWARD 1 -s "${SOURCE}" -d "${DESTINATION}" \
  -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass

echo "Router forwarding enabled."
echo "NFQUEUE rule: ${SOURCE} -> ${DESTINATION}, queue ${QUEUE_NUM}."
