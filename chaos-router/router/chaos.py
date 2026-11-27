from __future__ import annotations

import argparse
import heapq
import itertools
import json
import signal
import sys
import threading
import time
from collections.abc import Iterable
from typing import Any

from .control import ControlServer
from .log import RouterLog
from .state import RouterState


class PacketScheduler:
    def __init__(self, state: RouterState, router_log: RouterLog) -> None:
        self._state = state
        self._log = router_log
        self._condition = threading.Condition()
        self._pending: list[tuple[float, int, Any, float, int]] = []
        self._sequence = itertools.count()
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="packet-timer")
        self._thread.start()

    def schedule(self, packet: Any, delay_seconds: float, seen: int) -> None:
        packet.retain()
        release_at = time.monotonic() + delay_seconds
        with self._condition:
            if self._closing:
                raise RuntimeError("cannot schedule a packet while shutting down")
            heapq.heappush(self._pending, (release_at, next(self._sequence), packet, delay_seconds, seen))
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closing = True
            now = time.monotonic()
            self._pending = [(now, sequence, packet, delay, seen) for _, sequence, packet, delay, seen in self._pending]
            heapq.heapify(self._pending)
            self._condition.notify_all()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closing:
                    self._condition.wait()
                if self._closing and not self._pending:
                    return

                release_at, _, packet, delay_seconds, seen = self._pending[0]
                remaining = release_at - time.monotonic()
                if remaining > 0:
                    self._condition.wait(min(remaining, threading.TIMEOUT_MAX))
                    continue
                heapq.heappop(self._pending)

            packet.accept()
            self._state.record("passed")
            self._log.write(packet, "passed", delay_seconds * 1000, seen)


def make_callback(state: RouterState, scheduler: PacketScheduler, router_log: RouterLog):
    def handle(packet: Any) -> None:
        seen = state.record("seen")
        delay_seconds = state.decide()
        if delay_seconds is None:
            packet.drop()
            state.record("dropped")
            router_log.write(packet, "dropped", seen=seen)
        elif delay_seconds > 0:
            state.record("delayed")
            router_log.write(packet, "delayed", delay_seconds * 1000, seen)
            scheduler.schedule(packet, delay_seconds, seen)
        else:
            packet.accept()
            state.record("passed")
            router_log.write(packet, "passed", seen=seen)

    return handle


def print_stats(state: RouterState) -> None:
    print(json.dumps(state.stats(), sort_keys=True), flush=True)


def run(queue_num: int = 0, queue_maxlen: int = 4096, log_path: str | None = None, control_host: str = "0.0.0.0", control_port: int = 9003) -> None:
    try:
        from netfilterqueue import NetfilterQueue
    except ImportError as exc:
        raise RuntimeError("NetfilterQueue is not installed in this Python environment. Install the Python binding and libnetfilter_queue on the router.") from exc

    state = RouterState()
    router_log = RouterLog(log_path)
    queue = NetfilterQueue()
    scheduler = PacketScheduler(state, router_log)
    stopping = threading.Event()
    control_server: ControlServer | None = None
    control_thread: threading.Thread | None = None
    bound = False

    def stop(_signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print("\nStopping NFQUEUE router...", flush=True)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGUSR1, lambda _signum, _frame: print_stats(state))

    try:
        queue.bind(queue_num, make_callback(state, scheduler, router_log), max_len=queue_maxlen)
        bound = True
        control_server = ControlServer((control_host, control_port), state)
        control_thread = threading.Thread(target=control_server.serve_forever, name="control-server", daemon=True)
        control_thread.start()

        print(json.dumps({"status": "running", "queue": queue_num, "maxlen": queue_maxlen, "control": f"{control_host}:{control_server.server_address[1]}", "log": log_path}), flush=True)

        while not stopping.is_set():
            queue.run(block=False)
            stopping.wait(0.01)
    finally:
        if control_server is not None and control_thread is not None:
            control_server.shutdown()
            control_server.server_close()
            control_thread.join()
        scheduler.close()
        if bound:
            queue.unbind()
        print_stats(state)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NFQUEUE Chaos Router")
    parser.add_argument("--queue-num", type=int, default=0)
    parser.add_argument("--queue-maxlen", type=int, default=4096)
    parser.add_argument("--log", default="/tmp/chaos-router.jsonl")
    parser.add_argument("--control-host", default="0.0.0.0")
    parser.add_argument("--control-port", type=int, default=9003)
    parser.add_argument("--checksum", choices=("fix", "break"), default="fix", help="Used by the future packet-editing milestone")
    parser.add_argument("--resize", action="store_true", help="Enable length-changing TCP edits in a later milestone")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args.queue_num, args.queue_maxlen, args.log, args.control_host, args.control_port)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
