# Chaos Router

The primary router is an IPv4 Layer-3 forwarder using Linux NFQUEUE. `router/chaos.py`
owns packet verdicts and logging; `router/effects.py` contains Kyle's per-packet
loss, delay, and jitter decision function. [old/backup/mim.py](../old/backup/mim.py)
is the UDP proxy fallback, not the primary router.

## Local namespace test

On CachyOS/Arch, install `iproute2`, `iptables`, and `iputils`, then install
`NetfilterQueue` in the project virtual environment. From this directory run:

```bash
sudo ./scripts/test_namespaces.sh
```

The test creates isolated client, router, and server network namespaces, sends
three pings through NFQUEUE, changes the live loss setting over the TCP control
port, verifies the next ping is dropped, checks the router log and counter, then
removes the namespaces. It requires root privileges but does not change the
host's network interfaces or firewall rules.

Run the unit tests without root:

```bash
.venv/bin/python -m unittest -v test_chaos.py
```

## Run on a router VM

Configure the data interfaces and routes as described in
[`../Chaos Router Project Plan.md`](../Chaos%20Router%20Project%20Plan.md), then
install the forwarding rule and start the queue program:

```bash
sudo ./scripts/setup_router.sh
sudo .venv/bin/python chaos.py --log /tmp/chaos-router.jsonl
```

The process accepts control commands over TCP port 9003 by default. For example:

```bash
printf '%s\n' '{"cmd":"loss","value":20}' | nc ROUTER_MGMT_IP 9003
printf '%s\n' '{"cmd":"stats"}' | nc ROUTER_MGMT_IP 9003
```

The `loss` command takes a percentage; `delay` and `jitter` values are
milliseconds. The older `{"cmd":"set","loss":0.2}` form is also accepted for
compatibility with the UDP proxy control client. `SIGINT`/`SIGTERM` stop the
queue cleanly and release packets waiting on a delay timer.

This implementation covers passthrough, packet counting/logging, and the
existing loss/delay/jitter effects. Interception, packet editing, TCP sequence
rewriting, and `--resize` remain later milestones.
