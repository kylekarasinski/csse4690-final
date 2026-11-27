from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import argparse
import json
import signal
import sys
import threading

class PacketCounter:
    """Thread-safe packet counters and decision log."""

    def __init__(self, log_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_pass(self, packet: Any) -> None:
        summary = summarize_packet(packet)
        with self._lock:
            self._counts["seen"] += 1
            self._counts["passed"] += 1
            seen = self._counts["seen"]
            passed = self._counts["passed"]

        if self.log_path:
            record = {
                "time": datetime.now(timezone.utc).isoformat(),
                "action": "pass",
                "seen": seen,
                "passed": passed,
                **summary,
            }
            append_json_line(self.log_path, record)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "seen": self._counts["seen"],
                "passed": self._counts["passed"],
                "dropped": self._counts["dropped"],
                "delayed": self._counts["delayed"],
                "corrupted": self._counts["corrupted"],
            }


def append_json_line(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, sort_keys=True) + "\n")


def summarize_packet(packet: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"length": 0}

    try:
        summary["length"] = len(bytes(packet))
    except (AttributeError, TypeError):
        pass

    if hasattr(packet, "hw_protocol"):
        summary["hw_protocol"] = str(packet.hw_protocol)

    return summary


def make_callback(counter: PacketCounter):
    def handle(packet: Any) -> None:
        counter.record_pass(packet)
        packet.accept()

    return handle


def print_stats(counter: PacketCounter) -> None:
    print(json.dumps(counter.snapshot(), sort_keys=True), flush=True)


def run(queue_num: int = 0, queue_maxlen: int = 4096, log_path: str | None = None) -> None:
    try:
        from netfilterqueue import NetfilterQueue
    except ImportError as exc:
        raise RuntimeError(
            "NetfilterQueue is not installed in this Python environment. "
            "Install the Python binding and libnetfilter_queue on the router."
        ) from exc

    counter = PacketCounter(log_path)
    queue = NetfilterQueue()
    queue.bind(queue_num, make_callback(counter), max_len=queue_maxlen)
    stopping = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print("\nStopping NFQUEUE passthrough...", flush=True)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGUSR1, lambda _signum, _frame: print_stats(counter))

    print(
        json.dumps({
            "status": "running",
            "queue": queue_num,
            "maxlen": queue_maxlen,
            "log": str(log_path) if log_path else None
        }), flush=True
    )

    try:
        while not stopping.is_set():
            queue.run(block=False)
            stopping.wait(0.01)
    finally:
        queue.unbind()
        print_stats(counter)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NFQUEUE passthrough with packet counting")
    parser.add_argument("--queue-num", type=int, default=0)
    parser.add_argument("--queue-maxlen", type=int, default=4096)
    parser.add_argument("--log", default="/tmp/chaos-router.jsonl")
    parser.add_argument("--checksum", choices=("fix", "break"), default="fix", help="Reserved for milestone packet edits")
    parser.add_argument("--resize", action="store_true", help="Reserved for milestone 8b TCP length-changing edits")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.queue_num, args.queue_maxlen, args.log)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())