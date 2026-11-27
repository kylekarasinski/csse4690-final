#!/usr/bin/env bash
# End-to-end test for the chaos router using three network namespaces:
#   client (10.0.1.2) <-> router (10.0.1.1 / 10.0.2.1) <-> server (10.0.2.2)
# It pings through the router, switches the live router to 100% loss over the
# control socket, confirms the next ping is dropped, then checks the router's log.
# Usage: sudo ./scripts/test_namespaces.sh
# Exit on any error (-e), unset variable (-u), or failure in a pipeline (pipefail).
set -euo pipefail

# Resolve paths relative to this script so it works from any working directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
# Needs the venv that has NetfilterQueue installed; override with PYTHON=...
PYTHON="${PYTHON:-${ROUTER_DIR}/.venv/bin/python}"
# This shell's PID makes every name unique, so concurrent runs don't collide.
# Interface names are limited to 15 characters, hence the short prefixes.
PID="$$"
CLIENT_NS="cr-client-${PID}"
ROUTER_NS="cr-router-${PID}"
SERVER_NS="cr-server-${PID}"
CLIENT_VETH="c${PID}"
ROUTER_LEFT_VETH="rl${PID}"
ROUTER_RIGHT_VETH="rr${PID}"
SERVER_VETH="s${PID}"
# Temp files and the router PID; empty until created so cleanup() knows what exists.
QUEUE_LOG=""
ROUTER_OUTPUT=""
ROUTER_PID=""
# "Created" flags let cleanup() undo only the setup steps that actually ran,
# even if the script fails partway through.
CLIENT_NS_CREATED=0
ROUTER_NS_CREATED=0
SERVER_NS_CREATED=0
CLIENT_VETH_CREATED=0
SERVER_VETH_CREATED=0

# Runs on every exit (success, failure, or signal); every step is best-effort
# (|| true) so one failed step doesn't stop the rest of the teardown.
cleanup() {
	# Stop the router if it's still running: ask politely (SIGTERM), wait up to
	# ~2s (20 x 0.1s) for it to exit, then force-kill, then reap it.
	if [[ -n "${ROUTER_PID}" ]] && kill -0 "${ROUTER_PID}" 2>/dev/null; then
		kill -TERM "${ROUTER_PID}" 2>/dev/null || true
		for ((attempt = 0; attempt < 20; attempt++)); do
			kill -0 "${ROUTER_PID}" 2>/dev/null || break
			sleep 0.1
		done
		if kill -0 "${ROUTER_PID}" 2>/dev/null; then
			kill -KILL "${ROUTER_PID}" 2>/dev/null || true
		fi
		wait "${ROUTER_PID}" 2>/dev/null || true
	fi

	# Deleting a namespace also destroys the veth ends that were moved into it.
	if (( CLIENT_NS_CREATED )); then
		ip netns del "${CLIENT_NS}" 2>/dev/null || true
	fi
	if (( ROUTER_NS_CREATED )); then
		ip netns del "${ROUTER_NS}" 2>/dev/null || true
	fi
	if (( SERVER_NS_CREATED )); then
		ip netns del "${SERVER_NS}" 2>/dev/null || true
	fi
	# Only matters if the script died after creating a veth pair but before
	# moving it into a namespace. Deleting one end removes its peer too.
	if (( CLIENT_VETH_CREATED )); then
		ip link del "${CLIENT_VETH}" 2>/dev/null || true
	fi
	if (( SERVER_VETH_CREATED )); then
		ip link del "${ROUTER_RIGHT_VETH}" 2>/dev/null || true
	fi
	# Remove temp files.
	if [[ -n "${QUEUE_LOG}" ]]; then
		rm -f -- "${QUEUE_LOG}"
	fi
	if [[ -n "${ROUTER_OUTPUT}" ]]; then
		rm -f -- "${ROUTER_OUTPUT}"
	fi
}

# EXIT covers normal and error exits. INT/TERM convert signals into exits with
# the conventional 128+signal codes (130, 143), which then fire the EXIT trap.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Preflight checks: fail early with a clear message before touching the network.
# Namespaces and iptables both need root.
if [[ "${EUID}" -ne 0 ]]; then
	echo "Run this test with sudo: sudo ./scripts/test_namespaces.sh" >&2
	exit 1
fi

for command_name in ip iptables ping; do
	if ! command -v "${command_name}" >/dev/null 2>&1; then
		echo "Required command not found: ${command_name}" >&2
		exit 1
	fi
done

if [[ ! -x "${PYTHON}" ]]; then
	echo "Python interpreter not found: ${PYTHON}" >&2
	echo "Create chaos-router/.venv and install NetfilterQueue in it first." >&2
	exit 1
fi

# Import check; the traceback is shown on failure so the cause is visible.
"${PYTHON}" -c 'from netfilterqueue import NetfilterQueue' || {
	echo "NetfilterQueue cannot be imported by ${PYTHON}." >&2
	exit 1
}

# Temp files for the router's JSONL log and its stdout/stderr (removed in cleanup).
QUEUE_LOG="$(mktemp /tmp/chaos-router-test-log.XXXXXX)"
ROUTER_OUTPUT="$(mktemp /tmp/chaos-router-test-output.XXXXXX)"

# Create the three namespaces, setting each flag right after so cleanup() tracks it.
ip netns add "${CLIENT_NS}"
CLIENT_NS_CREATED=1
ip netns add "${ROUTER_NS}"
ROUTER_NS_CREATED=1
ip netns add "${SERVER_NS}"
SERVER_NS_CREATED=1

# Wire it up with two veth pairs (virtual cables):
# client <-> router-left, and router-right <-> server.
ip link add "${CLIENT_VETH}" type veth peer name "${ROUTER_LEFT_VETH}"
CLIENT_VETH_CREATED=1
ip link set "${CLIENT_VETH}" netns "${CLIENT_NS}"
ip link set "${ROUTER_LEFT_VETH}" netns "${ROUTER_NS}"
ip link add "${ROUTER_RIGHT_VETH}" type veth peer name "${SERVER_VETH}"
SERVER_VETH_CREATED=1
ip link set "${ROUTER_RIGHT_VETH}" netns "${ROUTER_NS}"
ip link set "${SERVER_VETH}" netns "${SERVER_NS}"

# One /24 subnet per link; the router sits at .1 on each. These addresses must
# match the NFQUEUE rule below.
ip -n "${CLIENT_NS}" address add 10.0.1.2/24 dev "${CLIENT_VETH}"
ip -n "${ROUTER_NS}" address add 10.0.1.1/24 dev "${ROUTER_LEFT_VETH}"
ip -n "${ROUTER_NS}" address add 10.0.2.1/24 dev "${ROUTER_RIGHT_VETH}"
ip -n "${SERVER_NS}" address add 10.0.2.2/24 dev "${SERVER_VETH}"

# Intended to bring up loopback and the veth in each namespace. Each
# "namespace interface" pair is read from a string. (See the note after this script.)
for item in "${CLIENT_NS} lo ${CLIENT_NS} ${CLIENT_VETH} ${ROUTER_NS} lo ${ROUTER_NS} ${ROUTER_LEFT_VETH} ${ROUTER_NS} ${ROUTER_RIGHT_VETH} ${SERVER_NS} lo ${SERVER_NS} ${SERVER_VETH}"; do
	read -r namespace interface <<<"${item}"
	ip -n "${namespace}" link set "${interface}" up
done

# The client and server send everything off-subnet through the router.
ip -n "${CLIENT_NS}" route add default via 10.0.1.1
ip -n "${SERVER_NS}" route add default via 10.0.2.1
# Turn the router namespace into a router, and queue client -> server packets to
# NFQUEUE 0. --queue-bypass accepts packets if nothing is bound to the queue.
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.ip_forward=1
ip netns exec "${ROUTER_NS}" iptables -I FORWARD -s 10.0.1.2 -d 10.0.2.2 -j NFQUEUE --queue-num 0 --queue-bypass

# Start the Python router inside the router namespace, in the background, with a
# small queue and its output captured. `ip netns exec` replaces itself with the
# command, so $! is the Python process and signals reach it directly.
ip netns exec "${ROUTER_NS}" "${PYTHON}" "${ROUTER_DIR}/chaos.py" --queue-num 0 --queue-maxlen 128 --log "${QUEUE_LOG}" >"${ROUTER_OUTPUT}" 2>&1 &
ROUTER_PID=$!

# Wait up to ~5s (50 x 0.1s) for the startup banner that run() prints
# (the "running" status line); bail out early if the process dies first.
for ((attempt = 0; attempt < 50; attempt++)); do
	if grep -q '"status": "running"' "${ROUTER_OUTPUT}"; then
		break
	fi
	if ! kill -0 "${ROUTER_PID}" 2>/dev/null; then
		echo "NFQUEUE process exited before starting:" >&2
		cat "${ROUTER_OUTPUT}" >&2
		exit 1
	fi
	sleep 0.1
done

# The loop also ends when it runs out of attempts, so check the banner again.
if ! grep -q '"status": "running"' "${ROUTER_OUTPUT}"; then
	echo "Timed out waiting for NFQUEUE to start:" >&2
	cat "${ROUTER_OUTPUT}" >&2
	exit 1
fi

# Baseline: with no effects configured, three pings should pass through the
# router. Only client -> server packets are queued, so this gives 3 queued
# packets (the echo requests); replies don't go through NFQUEUE.
echo "Sending three pings through the namespace router..."
if ! ip netns exec "${CLIENT_NS}" ping -n -c 3 -W 2 10.0.2.2; then
	echo "Ping through the namespace router failed." >&2
	cat "${ROUTER_OUTPUT}" >&2
	exit 1
fi

# Switch the running router to 100% loss through its control socket. This runs
# in the router namespace because the control server listens there on port 9003.
# The heredoc is quoted ('PY') so bash doesn't expand anything inside it.
ip netns exec "${ROUTER_NS}" "${PYTHON}" - <<'PY'
import json
import socket

with socket.create_connection(("127.0.0.1", 9003), timeout=2) as connection:
    connection.sendall(b'{"cmd":"loss","value":100}\n')
    with connection.makefile("rb") as stream:
        reply = json.loads(stream.readline())
if not reply.get("ok") or reply.get("mode") != "loss":
    raise SystemExit(f"Router did not acknowledge the loss command: {reply}")
print("Router acknowledged 100% loss.")
PY

# This ping is expected to FAIL; success here means the loss setting didn't work.
echo "Checking that the live loss setting drops the next ping..."
if ip netns exec "${CLIENT_NS}" ping -n -c 1 -W 1 10.0.2.2; then
	echo "Expected the ping to be dropped at 100% loss." >&2
	exit 1
fi

# Stop the router and wait for it so the log is complete before we read it.
# Clearing ROUTER_PID tells cleanup() there's nothing left to kill.
kill -TERM "${ROUTER_PID}"
wait "${ROUTER_PID}"
ROUTER_PID=""

# Verify the log. Expect 4 packets: 3 passed, then 1 dropped, numbered 1 to 4.
"${PYTHON}" - "${QUEUE_LOG}" <<'PY'
import json
import sys
from pathlib import Path

records = [
    json.loads(line)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line
]
if len(records) != 4:
    raise SystemExit(f"Expected 4 NFQUEUE packets, got {len(records)}")
if [record.get("action") for record in records] != [
    "passed",
    "passed",
    "passed",
    "dropped",
]:
    raise SystemExit("Router actions did not match pass-then-drop test")
if [record.get("seen") for record in records] != [1, 2, 3, 4]:
    raise SystemExit("Packet counter did not advance from 1 through 4")

print("PASS: 3 pings passed, then 100% loss dropped the fourth; counter matched.")
PY