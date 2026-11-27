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
	"""Holds delayed packets and releases (accepts) each one when its delay expires.

	A single background thread sleeps on a min-heap keyed by release time, so
	delays don't block the main NFQUEUE loop.
	"""

	def __init__(self, state: RouterState, router_log: RouterLog) -> None:
		self._state = state
		self._log = router_log
		# Guards _pending/_closing and lets the worker sleep until woken or timed out.
		self._condition = threading.Condition()
		# Heap entries: (release_at, sequence, packet, delay_seconds, seen).
		# release_at uses time.monotonic(), so wall-clock changes don't affect it.
		self._pending: list[tuple[float, int, Any, float, int]] = []
		# Monotonic tie-breaker: when release times are equal, the heap never
		# falls through to comparing packet objects, and order stays FIFO.
		self._sequence = itertools.count()
		self._closing = False
		self._thread = threading.Thread(target=self._run, name="packet-timer")
		self._thread.start()

	def schedule(self, packet: Any, delay_seconds: float, seen: int) -> None:
		"""Queue a packet to be accepted after delay_seconds."""
		# Keep the packet valid after the NFQUEUE callback returns.
		packet.retain()
		release_at = time.monotonic() + delay_seconds
		with self._condition:
			if self._closing:
				raise RuntimeError("cannot schedule a packet while shutting down")
			heapq.heappush(self._pending, (release_at, next(self._sequence), packet, delay_seconds, seen))
			# Wake the worker: the new packet may be due sooner than what it's waiting on.
			self._condition.notify()

	def close(self) -> None:
		"""Stop accepting new packets, flush everything pending immediately, and join the worker."""
		with self._condition:
			self._closing = True
			# Rewrite every release time to "now" so remaining packets are
			# released right away instead of waiting out their delays.
			# Sequence numbers are kept, so original order is preserved.
			now = time.monotonic()
			self._pending = [(now, sequence, packet, delay, seen) for _, sequence, packet, delay, seen in self._pending]
			heapq.heapify(self._pending)
			self._condition.notify_all()
		self._thread.join()

	def _run(self) -> None:
		"""Worker loop: sleep until the earliest packet is due, then accept it."""
		while True:
			with self._condition:
				# Idle until there is work or we're told to shut down.
				while not self._pending and not self._closing:
					self._condition.wait()
				# Shutting down with nothing left to flush: exit the thread.
				if self._closing and not self._pending:
					return

				# Peek at the earliest-due packet without removing it.
				release_at, _, packet, delay_seconds, seen = self._pending[0]
				remaining = release_at - time.monotonic()
				if remaining > 0:
					# Not due yet. Sleep until due, or until woken early by a
					# new (possibly sooner) packet or by close(); then re-check.
					# min() caps the wait at the platform's maximum timeout.
					self._condition.wait(min(remaining, threading.TIMEOUT_MAX))
					continue
				heapq.heappop(self._pending)

			# Lock released before doing I/O so schedule() isn't blocked.
			packet.accept()
			self._state.record("passed")
			self._log.write(packet, "passed", delay_seconds * 1000, seen)


def make_callback(state: RouterState, scheduler: PacketScheduler, router_log: RouterLog):
	"""Build the per-packet NFQUEUE callback, closed over shared state, scheduler, and log."""
	def handle(packet: Any) -> None:
		# "seen" returns this packet's sequence number among all packets seen.
		seen = state.record("seen")
		# decide() returns None => drop, a positive float => delay (seconds), 0 => pass.
		delay_seconds = state.decide()
		if delay_seconds is None:
			packet.drop()
			state.record("dropped")
			router_log.write(packet, "dropped", seen=seen)
		elif delay_seconds > 0:
			# Verdict is deferred: the scheduler accepts the packet later.
			state.record("delayed")
			router_log.write(packet, "delayed", delay_seconds * 1000, seen)
			scheduler.schedule(packet, delay_seconds, seen)
		else:
			packet.accept()
			state.record("passed")
			router_log.write(packet, "passed", seen=seen)

	return handle


def print_stats(state: RouterState) -> None:
	"""Print current counters as one line of sorted-key JSON (also used on SIGUSR1 and exit)."""
	print(json.dumps(state.stats(), sort_keys=True), flush=True)


def run(queue_num: int = 0, queue_maxlen: int = 4096, log_path: str | None = None, control_host: str = "0.0.0.0", control_port: int = 9003) -> None:
	"""Run the router until SIGINT/SIGTERM: bind the queue, start the control server, process packets."""
	# Imported lazily so the module can load (e.g. for tests/--help) without the binding installed.
	try:
		from netfilterqueue import NetfilterQueue
	except ImportError as exc:
		raise RuntimeError("NetfilterQueue is not installed in this Python environment. Install the Python binding and libnetfilter_queue on the router.") from exc

	state = RouterState()
	router_log = RouterLog(log_path)
	queue = NetfilterQueue()
	scheduler = PacketScheduler(state, router_log)
	# Set by the signal handler to tell the main loop to exit.
	stopping = threading.Event()
	control_server: ControlServer | None = None
	control_thread: threading.Thread | None = None
	# Tracks whether bind() succeeded, so cleanup only unbinds a bound queue.
	bound = False

	def stop(_signum: int, _frame: Any) -> None:
		# Idempotent: repeated Ctrl+C / SIGTERM won't re-print or re-trigger.
		if stopping.is_set():
			return
		stopping.set()
		print("\nStopping NFQUEUE router...", flush=True)

	signal.signal(signal.SIGINT, stop)
	signal.signal(signal.SIGTERM, stop)
	# SIGUSR1 dumps stats on demand without stopping the router.
	signal.signal(signal.SIGUSR1, lambda _signum, _frame: print_stats(state))

	try:
		queue.bind(queue_num, make_callback(state, scheduler, router_log), max_len=queue_maxlen)
		bound = True
		# Control server runs in a daemon thread so it can't block process exit.
		control_server = ControlServer((control_host, control_port), state)
		control_thread = threading.Thread(target=control_server.serve_forever, name="control-server", daemon=True)
		control_thread.start()

		# Startup banner; reports the actual bound port (useful if control_port was 0).
		print(json.dumps({"status": "running", "queue": queue_num, "maxlen": queue_maxlen, "control": f"{control_host}:{control_server.server_address[1]}", "log": log_path}), flush=True)

		# Poll non-blockingly so the loop can notice `stopping` promptly;
		# the short wait avoids busy-spinning and exits early when stopping is set.
		while not stopping.is_set():
			queue.run(block=False)
			stopping.wait(0.01)
	finally:
		# Cleanup order: stop the control server, flush the scheduler (accepting
		# any still-delayed packets), unbind the queue, then print final stats.
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
	# --checksum and --resize are parsed but not yet passed to run(); placeholders for later milestones.
	parser.add_argument("--checksum", choices=("fix", "break"), default="fix", help="Used by the future packet-editing milestone")
	parser.add_argument("--resize", action="store_true", help="Enable length-changing TCP edits in a later milestone")
	return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
	"""CLI entry point. Returns a process exit code (0 ok, 1 on runtime/setup error)."""
	args = parse_args(argv)
	try:
		run(args.queue_num, args.queue_maxlen, args.log, args.control_host, args.control_port)
	except RuntimeError as exc:
		print(f"error: {exc}", file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())