#!/usr/bin/env bash
# Teardown counterpart to the setup script: removes the NFQUEUE rule that
# steers SOURCE -> DESTINATION traffic to the Python chaos router.
# Must use the same QUEUE_NUM, SOURCE, and DESTINATION as the setup script,
# since iptables only removes a rule that matches exactly.
# Usage: sudo SOURCE=10.0.1.2 DESTINATION=10.0.2.2 ./teardown.sh
# Exit on any error (-e), unset variable (-u), or failure in a pipeline (pipefail).
set -euo pipefail

# Defaults mirror the setup script; override via environment variables.
QUEUE_NUM="${QUEUE_NUM:-0}"
SOURCE="${SOURCE:-10.0.1.2}"
DESTINATION="${DESTINATION:-10.0.2.2}"

# iptables needs root.
if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

# -C exits 0 while a matching rule exists, so this loops until none remain.
# Looping (rather than a single -D) also cleans up duplicates left by manual
# inserts. Each -D removes only the first match. The -C check's failure is
# safe under set -e because it's part of the while condition.
while iptables -C FORWARD -s "${SOURCE}" -d "${DESTINATION}" -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass >/dev/null 2>&1; do
    iptables -D FORWARD -s "${SOURCE}" -d "${DESTINATION}" -j NFQUEUE --queue-num "${QUEUE_NUM}" --queue-bypass
done

# Prints even if no rule existed, so it confirms the end state, not that something was deleted.
# Note: net.ipv4.ip_forward is left enabled; this script only removes the rule.
echo "Chaos Router NFQUEUE rule removed."