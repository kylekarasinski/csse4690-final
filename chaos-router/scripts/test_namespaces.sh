#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROUTER_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-${ROUTER_DIR}/.venv/bin/python}"
PID="$$"
CLIENT_NS="cr-client-${PID}"
ROUTER_NS="cr-router-${PID}"
SERVER_NS="cr-server-${PID}"
CLIENT_VETH="c${PID}"
ROUTER_LEFT_VETH="rl${PID}"
ROUTER_RIGHT_VETH="rr${PID}"
SERVER_VETH="s${PID}"
QUEUE_LOG=""
ROUTER_OUTPUT=""
ROUTER_PID=""
CLIENT_NS_CREATED=0
ROUTER_NS_CREATED=0
SERVER_NS_CREATED=0
CLIENT_VETH_CREATED=0
SERVER_VETH_CREATED=0

cleanup() {
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

  if (( CLIENT_NS_CREATED )); then
    ip netns del "${CLIENT_NS}" 2>/dev/null || true
  fi
  if (( ROUTER_NS_CREATED )); then
    ip netns del "${ROUTER_NS}" 2>/dev/null || true
  fi
  if (( SERVER_NS_CREATED )); then
    ip netns del "${SERVER_NS}" 2>/dev/null || true
  fi
  if (( CLIENT_VETH_CREATED )); then
    ip link del "${CLIENT_VETH}" 2>/dev/null || true
  fi
  if (( SERVER_VETH_CREATED )); then
    ip link del "${ROUTER_RIGHT_VETH}" 2>/dev/null || true
  fi
  if [[ -n "${QUEUE_LOG}" ]]; then
    rm -f -- "${QUEUE_LOG}"
  fi
  if [[ -n "${ROUTER_OUTPUT}" ]]; then
    rm -f -- "${ROUTER_OUTPUT}"
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

"${PYTHON}" -c 'from netfilterqueue import NetfilterQueue' || {
  echo "NetfilterQueue cannot be imported by ${PYTHON}." >&2
  exit 1
}

QUEUE_LOG="$(mktemp /tmp/chaos-router-test-log.XXXXXX)"
ROUTER_OUTPUT="$(mktemp /tmp/chaos-router-test-output.XXXXXX)"

ip netns add "${CLIENT_NS}"
CLIENT_NS_CREATED=1
ip netns add "${ROUTER_NS}"
ROUTER_NS_CREATED=1
ip netns add "${SERVER_NS}"
SERVER_NS_CREATED=1

ip link add "${CLIENT_VETH}" type veth peer name "${ROUTER_LEFT_VETH}"
CLIENT_VETH_CREATED=1
ip link set "${CLIENT_VETH}" netns "${CLIENT_NS}"
ip link set "${ROUTER_LEFT_VETH}" netns "${ROUTER_NS}"
ip link add "${ROUTER_RIGHT_VETH}" type veth peer name "${SERVER_VETH}"
SERVER_VETH_CREATED=1
ip link set "${ROUTER_RIGHT_VETH}" netns "${ROUTER_NS}"
ip link set "${SERVER_VETH}" netns "${SERVER_NS}"

ip -n "${CLIENT_NS}" address add 10.0.1.2/24 dev "${CLIENT_VETH}"
ip -n "${ROUTER_NS}" address add 10.0.1.1/24 dev "${ROUTER_LEFT_VETH}"
ip -n "${ROUTER_NS}" address add 10.0.2.1/24 dev "${ROUTER_RIGHT_VETH}"
ip -n "${SERVER_NS}" address add 10.0.2.2/24 dev "${SERVER_VETH}"

for item in \
  "${CLIENT_NS} lo" \
  "${CLIENT_NS} ${CLIENT_VETH}" \
  "${ROUTER_NS} lo" \
  "${ROUTER_NS} ${ROUTER_LEFT_VETH}" \
  "${ROUTER_NS} ${ROUTER_RIGHT_VETH}" \
  "${SERVER_NS} lo" \
  "${SERVER_NS} ${SERVER_VETH}"; do
  read -r namespace interface <<<"${item}"
  ip -n "${namespace}" link set "${interface}" up
done

ip -n "${CLIENT_NS}" route add default via 10.0.1.1
ip -n "${SERVER_NS}" route add default via 10.0.2.1
ip netns exec "${ROUTER_NS}" sysctl -q -w net.ipv4.ip_forward=1
ip netns exec "${ROUTER_NS}" iptables -I FORWARD \
  -s 10.0.1.2 -d 10.0.2.2 \
  -j NFQUEUE --queue-num 0 --queue-bypass

ip netns exec "${ROUTER_NS}" \
  "${PYTHON}" "${ROUTER_DIR}/chaos.py" \
  --queue-num 0 --queue-maxlen 128 --log "${QUEUE_LOG}" \
  >"${ROUTER_OUTPUT}" 2>&1 &
ROUTER_PID=$!

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

if ! grep -q '"status": "running"' "${ROUTER_OUTPUT}"; then
  echo "Timed out waiting for NFQUEUE to start:" >&2
  cat "${ROUTER_OUTPUT}" >&2
  exit 1
fi

echo "Sending three pings through the namespace router..."
if ! ip netns exec "${CLIENT_NS}" ping -n -c 3 -W 2 10.0.2.2; then
  echo "Ping through the namespace router failed." >&2
  cat "${ROUTER_OUTPUT}" >&2
  exit 1
fi

kill -TERM "${ROUTER_PID}"
wait "${ROUTER_PID}"
ROUTER_PID=""

"${PYTHON}" - "${QUEUE_LOG}" <<'PY'
import json
import sys
from pathlib import Path

records = [
    json.loads(line)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line
]
if len(records) != 3:
    raise SystemExit(f"Expected 3 NFQUEUE packets, got {len(records)}")
if any(record.get("action") != "pass" for record in records):
    raise SystemExit("At least one packet was not passed")
if [record.get("seen") for record in records] != [1, 2, 3]:
    raise SystemExit("Packet counter did not advance from 1 through 3")

print("PASS: all 3 pings crossed NFQUEUE and were counted as passed.")
PY
