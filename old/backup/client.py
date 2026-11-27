"""Client. Sends numbered, timestamped UDP messages to the MIM, then asks the server for its report.

Run:  python client.py --count 200 --rate 50
Baseline (no proxy): python client.py --target 127.0.0.1:9001
"""
import argparse, json, socket, time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="127.0.0.1:9000", help="MIM address (or server for baseline)")
    p.add_argument("--report", default="127.0.0.1:9002", help="server report address")
    p.add_argument("--count", type=int, default=100)
    p.add_argument("--rate", type=float, default=50, help="messages per second")
    p.add_argument("--settle", type=float, default=1.0, help="seconds to wait for delayed messages")
    a = p.parse_args()
    th, tp = a.target.split(":")

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for seq in range(a.count):
        s.sendto(json.dumps({"type": "data", "seq": seq, "sent": time.time()}).encode(), (th, int(tp)))
        time.sleep(1 / a.rate)
    time.sleep(a.settle)

    rh, rp = a.report.split(":")
    with socket.create_connection((rh, int(rp))) as c:
        c.sendall((json.dumps({"cmd": "report", "sent": a.count}) + "\n").encode())
        report = json.loads(c.makefile().readline())
    m = report.pop("missing")
    print(json.dumps(report, indent=2))
    print("missing seqs:", m[:20], "..." if len(m) > 20 else "")


if __name__ == "__main__":
    main()
