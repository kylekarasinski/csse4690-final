# Chaos Router: Project Plan

ELEE 4680/4690 final project (Computer Networking). Plan v2: merged with the team proposal.

## 1. Goal

Build a Linux router that sits between vm1 and vm2 and, on command, intercepts packets and drops, delays, reorders, corrupts or edits them before forwarding. It must work for UDP and TCP. vm2 reports back to vm1 whether each message arrived intact, and the team compares three views (what vm1 sent, what the router did, what vm2 saw) to measure loss, delay, jitter and reordering. This plan merges the team proposal (four members, demo Dec 14) with the packet-level router design the professor drew.

## 2. Decisions

- **A1. Control:** live, on command, while traffic flows. A separate control interface sends the commands (section 16). CLI first; a GUI is optional later.
- **A2. Router:** a real Layer 3 hop working on packets (NFQUEUE). This replaces the proposal's application-level proxy: the client addresses vm2 directly, not the proxy.
- **A3. Platform:** Linux for the router. Windows endpoints later only if compatible. NFQUEUE is Linux-only, so a Windows router is out of scope.
- **A4. Reports:** vm1 sends data to vm2, and vm2 reports back to vm1 whether each message was good or bad. Only the vm1 to vm2 data is chaos-controlled. The report travels on a separate management network (section 15), so the router never alters it. Under `--resize` the router still rewrites ACK numbers on the data connection (section 7).
- **A5. Environment:** VMs preferred. Network namespaces on one Linux host are the fallback and the development setup (one shared clock).
- **A6. Corruption:** both valid-checksum and invalid-checksum modes, chosen with `--checksum fix|break`.
- **A7. TCP edits:** same-length and length-changing edits, chosen with `--resize`.

## 3. To confirm

| # | Question | Default if unanswered |
| --- | --- | --- |
| Q1 | Does the professor accept the packet-level router replacing the application-level proxy? | Yes, his diagram describes it. Keep the proposal's UDP proxy only as a fallback if NFQUEUE blocks us |
| Q2 | Is a demo on VMs or namespaces on one machine acceptable instead of three physical laptops? | Yes. Use laptops only if time allows (the router laptop needs two NICs) |
| Q3 | Does every member have a Linux environment (VM, dual boot or live USB) for the router? | Everyone develops in a Linux VM |
| Q4 | Can the Nov 30 progress deliverables be rebaselined to this design? | Yes, see section 18 |

## 4. Topology

```
data path (chaos applied):
[vm1 client] 10.0.1.2 --- 10.0.1.1 [router] 10.0.2.1 --- 10.0.2.2 [vm2 server]
                subnet A                       subnet B

management network 10.0.9.0/24 (never chaos-controlled):
vm1 .9.2   router .9.1   vm2 .9.3   control interface .9.4
carries: control commands (control -> router), good/bad reports (vm2 -> vm1)
```

- Router has three NICs: one per data subnet and one on the management network. vm1, vm2 and the control machine each add a management NIC.
- Router: `sysctl -w net.ipv4.ip_forward=1`.
- vm1: default route via 10.0.1.1. vm2: default route via 10.0.2.1.
- On every VM, disable NIC offloading so checksums and packet sizes are what you expect in captures: `ethtool -K <iface> tx off rx off tso off gso off gro off`.

**Check:** `ping 10.0.2.2` from vm1 works, and `tcpdump` on the router shows the packets.

**Clock.** One-way delay needs one clock. Develop in network namespaces on one Linux host, where client and server share the kernel clock. Across separate VMs, do not subtract timestamps unless the clocks are synced; measure the router's added delay from its own ingress and egress times instead.

**Physical laptops (optional).** The router laptop needs two NICs plus the management link, and client and server must be separate VMs so their traffic leaves the laptop and crosses the router.

## 5. Architecture

The router uses Linux netfilter to hand every forwarded packet to a Python program, which decides its fate.

```
vm1 -> [NIC] -> iptables FORWARD -> NFQUEUE -> Python chaos engine -> verdict -> [NIC] -> vm2
                                                   ^
                                                   |
                                            operator CLI (thread)
```

**Packet hook (router):**

```
iptables -A FORWARD -s 10.0.1.2 -d 10.0.2.2 -j NFQUEUE --queue-num 0 --queue-bypass
```

`--queue-bypass` means traffic still flows if the Python program is not running, so you can't accidentally kill your own network.

**Libraries:** `netfilterqueue` (receive packets and set verdicts), `scapy` (parse, edit, rebuild packets).

**Threads:**

1. **Queue thread:** receives each packet, parses it, applies the current mode, and either sets a verdict immediately or stores the packet for later.
2. **Control thread:** receives JSON commands from the control interface (section 16), changes the mode or acts on held packets, and acknowledges each command. A local CLI on the router can use the same handler.
3. **Timer thread:** releases delayed packets when their time is up.

Shared state (current mode, filters, held-packet table) is protected by a lock.

**Important rule:** never sleep inside the queue callback. That blocks all traffic. Store the packet and set its verdict later from the timer or CLI thread. A held `NFQUEUE` packet can be accepted or dropped after the callback returns.

## 6. Operator commands

| Command | Effect |
| --- | --- |
| `pass` | Forward everything untouched (default mode) |
| `intercept` | Hold matching packets in a table instead of forwarding |
| `loss <percent>` | Drop each matching packet with this probability |
| `drop [n \| <seconds>s]` | Discard the next n packets, or all matching packets for N seconds |
| `delay <ms>` | Hold each matching packet, then forward after the delay |
| `jitter <ms>` | Add a random extra delay of 0 to N ms to each delayed packet |
| `reorder <n>` | Hold each matching packet until n later packets have passed, then release it |
| `corrupt <mode>` | Alter matching packets then forward (see section 8) |
| `bandwidth <kbps>` | Release matching packets no faster than this rate and queue the rest |
| `stall <seconds>` | Hold all matching packets for N seconds, then release them in order |
| `reset` | Inject a TCP RST toward both ends of the matching connection |
| `filter proto=<udp\|tcp> port=<n>` | Limit which packets the mode applies to |
| `list` | Show held packets: id, protocol, src, dst, length, age |
| `show <id>` | Show a held packet's headers and payload (hex and text) |
| `edit <id>` | Change the payload of a held packet |
| `release <id>` | Forward a held packet |
| `drop <id>` | Discard a held packet |
| `release all` | Forward every held packet in order |
| `stats` | Counters: seen, passed, dropped, delayed, corrupted |
| `ping` | Health check: the router replies `OK` with its current mode |
| `quit` | Restore `pass` mode, release held packets, exit |

**Flags.** `--checksum fix|break` sets whether an edited packet gets a valid checksum (default `fix`) or an invalid one; this is the difference between `flip` and `flip-bad` in section 8. `--resize` allows length-changing TCP edits (section 7).

Example intercept flow:

```
chaos> filter proto=tcp port=5000
chaos> intercept
[#12] TCP 10.0.1.2:41022 -> 10.0.2.2:5000 len=64 flags=PA
chaos> show 12
chaos> edit 12
chaos> release 12
```

## 7. Protocol behavior (what you should see)

**UDP**

- Dropped or held packets are gone from the receiver's point of view. No retransmit, no error.
- Delayed packets can arrive out of order relative to later packets.
- Easy to edit: change the payload, fix lengths and checksums, forward.

**TCP**

- A held or dropped segment causes vm1 to retransmit after its timeout. You will see duplicate segments in captures.
- Releasing a held segment late may result in a duplicate at vm2, which TCP ignores.
- Long holds stall the connection. Very long holds can end with the connection resetting.
- Same-length payload edits are safe and need no sequence changes. Length-changing edits are supported through the `--resize` flag. The router keeps an offset per connection: after an edit that changes the payload by N bytes, it adds N to the sequence number of every later vm1 to vm2 segment and subtracts N from the ACK number of every vm2 to vm1 packet. It also remembers each edit, so a retransmit of the same segment gets the same change. This needs the reverse direction in NFQUEUE, so the iptables rule covers both directions when the flag is on. Disable TCP SACK and timestamps on vm1 and vm2 (`sysctl -w net.ipv4.tcp_sack=0 net.ipv4.tcp_timestamps=0`) so there are no SACK ranges to rewrite.

**Checksums:** after any edit, delete the `chksum` field on the IP, TCP, and UDP layers in Scapy so it recomputes them on rebuild. Then call `set_payload(bytes(pkt))` and accept.

## 8. Corruption modes

| Mode | What it does | Expected result |
| --- | --- | --- |
| `flip` | Flip random bits in the payload, **recompute** checksum | vm2's app receives wrong data (OS accepts the packet) |
| `flip-bad` | Flip random bits, **leave** the old checksum | vm2's OS discards the packet. TCP retransmits; UDP loses it |
| `rewrite <old> <new>` | Replace bytes in the payload (same length), recompute checksum | App receives modified message |

The contrast between `flip` and `flip-bad` is a strong demo of why checksums exist and what they do not protect against.

## 9. Test applications

Build these two small apps first; they are what the router is tested with. They are our own programs, so every message can be verified. A packet-level router also works with any third-party app that only needs its default route pointed at the router, so such apps are optional demo extras.

**Message format (JSON).** UDP: one message per datagram. TCP: one message per line. Every message carries `seq`, `sent_at` (send time) and `hash` (SHA-256 of the body). Field names and ports follow the format the team agreed (due Nov 25); copy them here once written down.

**Good/bad report (vm2 to vm1).** Every message carries a sequence number and a SHA-256 of its body. vm2 recomputes the hash and sends vm1 a report: `GOOD seq=17` or `BAD seq=17 (hash mismatch)`. vm1 prints each report. If no report arrives within a timeout, vm1 prints `NO REPORT`. For UDP that means the packet was dropped, or vm2's OS discarded it for a bad checksum. For TCP the stack retransmits first, so a missing report means the connection stalled. The report travels on the management network, so the router never touches it.

**UDP test app**

- Client sends numbered messages (for example `seq=17 hello`) at a steady rate.
- Server prints each message it receives with a timestamp.
- Missing numbers show drops. Out-of-order numbers show delay. Changed text shows corruption.

**TCP test app**

- Client sends a file (or chat messages) to the server.
- Both sides print a SHA-256 of the data.
- Matching hashes show TCP survived the chaos. Different hashes show corruption got through.

## 10. Milestones

Each milestone has a pass check. Do not start the next one until the check passes.

| # | Milestone | Verify |
| --- | --- | --- |
| 1 | Topology up, router forwards | vm1 pings vm2. `tcpdump` on router shows traffic |
| 2 | Test apps working through router with no chaos | vm1 receives a GOOD report for every message. UDP numbers arrive in order. TCP hashes match |
| 3 | NFQUEUE passthrough in Python with a packet counter | Counter matches packets sent |
| 4 | CLI plus `drop` and `delay` on UDP | UDP server shows missing and late numbers matching the command |
| 5 | `intercept` with `list`, `show`, `release`, `drop <id>` on UDP | A held packet does not reach vm2 until released |
| 6 | `edit` on UDP | vm2 prints the altered payload |
| 7 | `drop`, `delay`, `intercept` on TCP | File transfer completes with matching hashes. Captures show retransmits. A held packet visibly stalls the transfer |
| 8 | `corrupt flip` and `flip-bad` on TCP and UDP | `flip`: wrong data at the app. `flip-bad`: packet discarded and TCP retransmits |
| 9 | Polish: `stats`, filters, clean `quit`, logging to a file | Running `quit` restores normal forwarding |
| 10 | Demo script and write-up | Section 11 runs start to finish |

**Milestone 8b: length-changing TCP edits (`--resize`).** Verify: the router grows or shrinks one message. vm2 reports `BAD` for that message (its hash no longer matches), then `GOOD` for every later message, and the connection does not reset. Do this after milestone 8 and before polish.

## 11. Demo script (for the professor)

1. Show the topology and `pass` mode. Traffic flows. Show UDP and TCP apps running.
2. UDP: run `drop 5`. Show five missing sequence numbers.
3. UDP: run `delay 2000`. Show late and out-of-order arrivals.
4. UDP: `intercept`, `show`, `edit`, `release`. Show the changed payload at vm2.
5. TCP: run `drop 3`. Show the transfer still completes with matching hashes, and point out the retransmits in `tcpdump`.
6. TCP: `intercept`, wait, then `release`. Show the stall and recovery.
7. Corruption: `flip` versus `flip-bad`. Explain the checksum difference.
8. Finish with the side-by-side summary: UDP loses silently, TCP recovers.

At every step, read the result from vm1's report output (`GOOD`, `BAD`, or `NO REPORT`), then confirm it against vm2's log and `tcpdump` on the router.

Run steps 2 to 6 from the separate control interface, with the client sending a steady stream, so the class sees each setting change live and each change confirmed. After each step, show the three views side by side: client log, router log and vm2's report.

## 12. Risks and fixes

| Risk | Fix |
| --- | --- |
| Python crashes and the network dies | Use `--queue-bypass` on the iptables rule |
| Held packets overflow the queue | Raise the queue length with `set_queue_maxlen`, and cap the held table size |
| Captures show wrong checksums because of NIC offload | Disable offloading on all VMs (section 4) |
| Router drops packets in the wrong direction | Restrict the iptables rule to vm1 to vm2 by source and destination |
| TCP connection resets during long holds | Keep holds short during demos, or show the reset on purpose |
| VM networking misconfigured | Confirm with ping and `tcpdump` at milestone 1 before any Python |
| New scope lands four days before the Nov 30 progress update | Rebaseline Nov 30 to passthrough plus UDP loss and delay (section 18). Keep the UDP proxy as a fallback |
| A member has no Linux environment | Everyone uses a Linux VM. Namespaces need no extra hardware |
| Management network not ready in time | Send reports back through the router and exclude the report port from the NFQUEUE rule |

## 13. Suggested file layout

```
chaos-router/
  router/
    chaos.py        # NFQUEUE loop and verdict logic
    effects.py      # loss, delay, jitter, reorder, corrupt, bandwidth, stall, reset
    control.py      # JSON command server on the management network
    state.py        # shared state and lock
    log.py          # one log line per packet decision
  control/
    control_cli.py  # control interface (GUI optional later)
  apps/
    client.py       # numbered, timestamped, hashed messages (UDP and TCP)
    server.py       # records arrivals, sends good/bad report
  analysis/
    compare.py      # joins client, router and server logs by seq; graphs
  scripts/
    setup_router.sh # ip_forward and iptables rules
    teardown.sh     # remove rules
  README.md
```

## 14. Out of scope (unless the professor asks)

- A Windows-based router (NFQUEUE is Linux-only)
- IPv6
- Chaos on the vm2 to vm1 direction
- Encrypted traffic (TLS) inspection
- Stretch only, after the Dec 6 core is done: live graph, burst loss, per-client settings, OS TCP statistics in the report, a client that adapts to reports, a GUI

## 15. Measurement and the good/bad report

Every run produces three logs joined by `seq`, so the team can check that the chaos matched its settings.

| View | Written by | Records |
| --- | --- | --- |
| Sent | vm1 client | `seq`, send time, hash |
| Router | router | `seq`, action taken (forwarded, dropped, delayed by N ms, held, edited), time in and out |
| Received | vm2 server | `seq`, arrival time, whether the hash matched, whether the JSON parsed |

**Metrics.** Loss: sent but never received. Delay: arrival time minus send time (one clock), or router time out minus time in. Jitter: variation in delay between consecutive messages. Reordering: a message that arrives after one with a higher `seq`. Definitions follow RFC 7680 (loss), RFC 7679 (delay), RFC 3393 (jitter) and RFC 4737 (reordering).

**Report channel.** vm2 sends its reports to vm1 over TCP on the management network, so the router never alters them. Two kinds:

- A per-message verdict: `GOOD`, `BAD` (hash mismatch or unparseable JSON), or `NO REPORT` if vm1 hears nothing within a timeout.
- An end-of-run summary: messages missing, out of order, and jitter.

**Three-way check.** If the router log says it dropped `seq` 12, vm2 must not have received 12. If it says it edited `seq` 12, vm2 must report `BAD` for it. Any disagreement is a bug in the router or in the measurement. `analysis/compare.py` joins the three logs and draws the UDP versus TCP graphs under identical settings.

## 16. Control interface

A separate program on the management network sends one JSON command per action to the router, and the router acknowledges each one, so the team knows exactly when conditions changed.

```
send:  {"cmd": "loss", "value": 20}
reply: {"ok": true, "mode": "loss", "value": 20}
```

- **Commands:** the same set as section 6. The very next packet uses the new setting.
- **Live stats:** the interface sends `stats` on a timer and shows the router's counters.
- **Health check:** the interface sends `ping` every few seconds and flags the router if no reply arrives in time.
- **Transport:** TCP is the simpler choice because it is reliable. The proposal used UDP with acknowledgments; either works if the team agrees.
- **Path:** commands go to the router's management address, never through the chaos-controlled data path.
- **Form:** CLI first. A GUI is optional once the core works.

## 17. Team and owners

Owners come from the proposal, with the router changes added. Everyone writes the report section for their own part and can explain it at the exam.

| Person | Owns | Done when |
| --- | --- | --- |
| Elijah Jorgensen | Router: NFQUEUE hook, intercept, hold, edit and release, UDP and TCP including `--resize`, router log, integration of all parts | Traffic flows through the router in both protocols and the router log matches what happened |
| Kyle Karasinski | Chaos effects (loss, delay, jitter, reorder, corrupt, bandwidth, stall, reset), server and good/bad report | Each effect measurably matches its setting; a report arrives after every run |
| Christopher Wrona | Client, `compare.py` (three views), experiments, graphs and analysis | Graphs explain how the app degrades under each condition, UDP versus TCP |
| Cassius Williams | Control interface: live settings, acknowledgments, live stats, health check | Settings change live during the demo and each change is confirmed |

**Interface between Elijah and Kyle.** Agree one function early: the router calls it once per packet and gets back an action (forward, drop, hold for N ms, edit). Kyle writes the effects behind it in `effects.py`; Elijah writes the router around it.

## 18. Schedule

Dates are the proposal's course dates. Deliverables are rebased to the router design; milestone numbers refer to section 10. Today is Nov 26, so the Nov 30 row is four days out.

| Date | Deliverable | Owner |
| --- | --- | --- |
| Wed Nov 25 | JSON message format, ports and management addresses written down (was due; confirm done) | Everyone |
| Mon Nov 30 | Progress update (course milestone): Gantt chart, status, first test | Everyone |
| Mon Nov 30 | Topology up in VMs or namespaces; router forwards; NFQUEUE passthrough with packet counter (milestones 1, 3) | Elijah |
| Mon Nov 30 | Client sends numbered, timestamped, hashed messages; no-router baseline recorded (milestone 2) | Christopher |
| Mon Nov 30 | UDP `loss` and `delay` modes working (milestone 4) | Kyle |
| Mon Nov 30 | Control interface sends one command to the router and shows the acknowledgment | Cassius |
| Sun Dec 6 | `intercept`, `edit`, `release` on UDP; TCP drop, delay and intercept (milestones 5 to 7) | Elijah |
| Sun Dec 6 | `--resize` length-changing TCP edits (milestone 8b), the riskiest item; if it slips, demo UDP and same-length TCP only | Elijah |
| Sun Dec 6 | Jitter, reorder and corrupt (`flip`, `flip-bad`) on UDP and TCP; server and good/bad report (milestone 8) | Kyle |
| Sun Dec 6 | `compare.py` joins the three logs | Christopher |
| Sun Dec 6 | Control protocol with acknowledgments, live stats, health check | Cassius |
| Sun Dec 6 | Full system runs end to end (milestone 9) | Elijah |
| Wed Dec 9 | Feature freeze (bug fixes only after this); experiments complete | Everyone / Christopher |
| Sun Dec 13 | Graphs and analysis; poster; demo video (backup and extra credit); two rehearsals | Christopher / Everyone |
| Mon Dec 14 | Project demo (course milestone) | Everyone |
| Thu Dec 17 | Project exam (course milestone): each person explains their own part | Everyone |
| To be confirmed | Technical report, source code, deployment manual, references | Everyone (own sections) |
