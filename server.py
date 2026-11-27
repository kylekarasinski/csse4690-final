"""Server. Records UDP arrivals; on request, sends a diagnostic report over a separate TCP port.

Run:  python server.py
"""
import argparse, json, socket, threading, time

arrivals = []            # list of (seq, sent_time, arrival_time) in arrival order
bad = [0]                # datagrams that were not a valid message (list so the thread can change it)
lock = threading.Lock()


def udp_loop(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    while True:
        data, _ = s.recvfrom(65535)
        now = time.time()
        try:
            msg = json.loads(data)
            entry = (int(msg["seq"]), float(msg["sent"]), now)
        except (ValueError, KeyError, TypeError):
            with lock:
                bad[0] += 1        # count it and keep running instead of crashing the thread
            continue
        with lock:
            arrivals.append(entry)


def build_report(total_sent):
    with lock:
        got = list(arrivals)
        arrivals.clear()     # reset for the next run
        n_bad, bad[0] = bad[0], 0
    seqs = [a[0] for a in got]
    delays = [(arr - sent) * 1000 for _, sent, arr in got]
    # Out of order: arrived after a higher sequence number had already arrived.
    out_of_order, highest = 0, -1
    for s in seqs:
        if s < highest:
            out_of_order += 1
        highest = max(highest, s)
    # Jitter: mean absolute change in one-way delay between consecutive arrivals (cf. RFC 3550 D(i,j)).
    diffs = [abs(delays[i] - delays[i - 1]) for i in range(1, len(delays))]
    missing = sorted(set(range(total_sent)) - set(seqs))
    return {
        "sent": total_sent,
        "received": len(got),
        "bad": n_bad,
        "missing": missing,
        "loss_pct": round(100 * len(missing) / total_sent, 2) if total_sent else 0,
        "out_of_order": out_of_order,
        "avg_delay_ms": round(sum(delays) / len(delays), 3) if delays else None,
        "jitter_ms": round(sum(diffs) / len(diffs), 3) if diffs else None,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-port", type=int, default=9001)
    p.add_argument("--report-port", type=int, default=9002)
    a = p.parse_args()
    threading.Thread(target=udp_loop, args=(a.data_port,), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.report_port))
    srv.listen()
    print(f"Server: data UDP {a.data_port}, report TCP {a.report_port}")
    while True:
        conn, _ = srv.accept()
        with conn:
            req = json.loads(conn.makefile().readline())   # {"cmd":"report","sent":N}
            conn.sendall((json.dumps(build_report(req["sent"])) + "\n").encode())


if __name__ == "__main__":
    main()
