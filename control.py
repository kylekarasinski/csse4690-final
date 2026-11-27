"""Control interface (text menu). Changes MIM settings live.

Run:  python control.py
Commands:  loss 20 (percent) | delay 100 | jitter 20 | stats | ping | quit
"""
import argparse, json, socket

KEYS = {"loss": "loss", "delay": "delay_ms", "jitter": "jitter_ms"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mim", default="127.0.0.1:9003")
    a = p.parse_args()
    h, port = a.mim.split(":")
    conn = socket.create_connection((h, int(port)))
    f = conn.makefile()
    while True:
        parts = input("> ").split()
        if not parts:
            continue
        if parts[0] == "quit":
            break
        if parts[0] in KEYS and len(parts) == 2:
            cmd = {"cmd": "set", KEYS[parts[0]]: parts[1]}
        elif parts[0] in ("stats", "ping"):
            cmd = {"cmd": parts[0]}
        else:
            print(__doc__)
            continue
        conn.sendall((json.dumps(cmd) + "\n").encode())
        print(f.readline().strip())     # the MIM's acknowledgment


if __name__ == "__main__":
    main()
