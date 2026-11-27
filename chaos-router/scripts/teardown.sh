#!/usr/bin/env bash
set -euo pipefail

QUEUE_NUM="${QUEUE_NUM:-0}"
SOURCE="${SOURCE:-10.0.1.2}"
DESTINATION="${DESTINATION:-10.0.2.2}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

while iptables -C FORWARD -s "${SOURCE}" -d "${DESTINATION}" \
  -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass >/dev/null 2>&1; do
  iptables -D FORWARD -s "${SOURCE}" -d "${DESTINATION}" \
    -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass
done

echo "Chaos Router NFQUEUE rule removed."
