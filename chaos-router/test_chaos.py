import contextlib
import io
import json
import signal
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from router.chaos import PacketScheduler, make_callback, run
from router.control import ControlServer
from router.log import RouterLog
from router.state import RouterState


class FakePacket:
	"""Stand-in for a NetfilterQueue packet that records which verdict methods were called."""

	def __init__(self) -> None:
		self.accepted = False
		self.dropped = False
		self.retained = False
		# 2048 is 0x0800, the EtherType for IPv4.
		self.hw_protocol = 2048

	def accept(self) -> None:
		self.accepted = True

	def drop(self) -> None:
		self.dropped = True

	def retain(self) -> None:
		self.retained = True

	def get_payload_len(self) -> int:
		# Fixed length so tests can assert on the logged "length" field.
		return 4


class FakeScheduler:
	"""Records schedule() calls instead of delaying, so callback tests run instantly with no thread."""

	def __init__(self) -> None:
		self.scheduled = []

	def schedule(self, packet, delay_seconds: float, seen: int) -> None:
		# Mirrors the real scheduler, which retains the packet before queueing it.
		packet.retain()
		self.scheduled.append((packet, delay_seconds, seen))


class FakeQueue:
	"""Stand-in for NetfilterQueue that makes run()'s main loop execute exactly once."""

	def __init__(self, signal_handlers) -> None:
		# Handlers captured from the patched signal.signal (see the run() test below).
		self.signal_handlers = signal_handlers
		self.bind_args = None
		# Starts True so the test can prove run() switched it to non-blocking.
		self.blocking = True
		self.run_calls = 0
		self.unbound = False

	def bind(self, queue_num, callback, max_len=1024) -> None:
		# Signature matches the real binding's bind(queue_num, callback, max_len=...).
		self.bind_args = (queue_num, callback, max_len)

	def run(self, block=False) -> None:
		self.blocking = block
		self.run_calls += 1
		# Simulate SIGTERM arriving during the first poll so run()'s
		# `while not stopping.is_set()` loop exits after one iteration.
		self.signal_handlers[signal.SIGTERM](signal.SIGTERM, None)

	def unbind(self) -> None:
		self.unbound = True


class ChaosRouterTests(unittest.TestCase):
	def test_pass_callback_accepts_and_counts_packet(self) -> None:
		# Default settings (no loss, no delay): the packet is accepted immediately.
		state = RouterState()
		packet = FakePacket()
		# Temp dir keeps the log file out of the real filesystem and auto-cleans.
		with tempfile.TemporaryDirectory() as directory:
			callback = make_callback(state, FakeScheduler(), RouterLog(str(Path(directory) / "router.jsonl")))
			callback(packet)

			self.assertTrue(packet.accepted)
			self.assertEqual(state.stats()["seen"], 1)
			self.assertEqual(state.stats()["passed"], 1)

	def test_loss_decision_drops_packet(self) -> None:
		state = RouterState()
		state.update_settings({"loss": 1})
		packet = FakePacket()
		with tempfile.TemporaryDirectory() as directory:
			callback = make_callback(state, FakeScheduler(), RouterLog(str(Path(directory) / "router.jsonl")))
			# Pin the RNG so the outcome is deterministic: 0.5 < loss(1.0) means "drop".
			# (This patches random.random process-wide for the duration of the block.)
			with patch("router.effects.random.random", return_value=0.5):
				callback(packet)

		self.assertTrue(packet.dropped)
		self.assertEqual(state.stats()["dropped"], 1)

	def test_jitter_is_included_in_delay_decision(self) -> None:
		state = RouterState()
		state.update_settings({"delay_ms": 20, "jitter_ms": 10})

		# random() = 0.5 is above loss (0.0), so the packet isn't dropped.
		# uniform() is pinned to +5 ms of jitter, so the expected delay is
		# (20 + 5) ms = 0.025 s.
		with (patch("router.effects.random.random", return_value=0.5), patch("router.effects.random.uniform", return_value=5)):
			delay = state.decide()

		self.assertEqual(delay, 0.025)

	def test_delay_decision_retains_without_blocking_callback(self) -> None:
		state = RouterState()
		state.update_settings({"delay_ms": 50})
		scheduler = FakeScheduler()
		packet = FakePacket()
		with tempfile.TemporaryDirectory() as directory:
			callback = make_callback(state, scheduler, RouterLog(str(Path(directory) / "router.jsonl")))
			callback(packet)

		# The callback must return without a verdict: the packet is held (retained)
		# and handed to the scheduler, which accepts it later.
		self.assertTrue(packet.retained)
		self.assertFalse(packet.accepted)
		# Scheduled with (packet, delay in seconds, "seen" number 1).
		self.assertEqual(scheduler.scheduled, [(packet, 0.05, 1)])
		self.assertEqual(state.stats()["delayed"], 1)

	def test_scheduler_releases_retained_packets_on_shutdown(self) -> None:
		state = RouterState()
		packet = FakePacket()
		with tempfile.TemporaryDirectory() as directory:
			# Real scheduler (with its worker thread). A 60 s delay would stall the
			# test if close() waited it out; close() must flush immediately instead.
			scheduler = PacketScheduler(state, RouterLog(str(Path(directory) / "router.jsonl")))
			scheduler.schedule(packet, 60, 1)
			scheduler.close()

		self.assertTrue(packet.retained)
		# Accepted despite the unexpired delay, and counted as passed.
		self.assertTrue(packet.accepted)
		self.assertEqual(state.stats()["passed"], 1)

	def test_run_uses_supported_bind_api_and_stops_cleanly(self) -> None:
		signal_handlers = {}
		queue = FakeQueue(signal_handlers)
		# Fake netfilterqueue module so the test needs neither the real binding nor root.
		module = SimpleNamespace(NetfilterQueue=lambda: queue)
		# Three patches in one block:
		# - sys.modules: makes run()'s lazy `from netfilterqueue import ...` get the fake.
		# - signal.signal: records handlers in a dict instead of installing them,
		#   so FakeQueue can "deliver" SIGTERM by calling the handler directly.
		# - redirect_stdout: silences the startup banner and final stats.
		with (patch.dict(sys.modules, {"netfilterqueue": module}), patch("router.chaos.signal.signal", side_effect=lambda signum, handler: signal_handlers.__setitem__(signum, handler)), contextlib.redirect_stdout(io.StringIO())):
			# log_path=None disables logging; control_port=0 lets the OS pick a free port.
			run(queue_num=3, queue_maxlen=64, log_path=None, control_host="127.0.0.1", control_port=0)

		# bind() received the queue number and max_len we passed in.
		self.assertEqual(queue.bind_args[0], 3)
		self.assertEqual(queue.bind_args[2], 64)
		# run() polls non-blockingly, ran the loop once, and unbound during cleanup.
		self.assertFalse(queue.blocking)
		self.assertEqual(queue.run_calls, 1)
		self.assertTrue(queue.unbound)

	def test_control_server_updates_effects_with_acknowledgment(self) -> None:
		state = RouterState()
		# Port 0 picks a free port; server_address holds the one actually bound.
		server = ControlServer(("127.0.0.1", 0), state)
		thread = threading.Thread(target=server.serve_forever)
		thread.start()
		try:
			with socket.create_connection(server.server_address) as connection:
				# Read/write file-like wrapper over the socket for line-based I/O.
				stream = connection.makefile("rwb")
				stream.write(b'{"cmd":"loss","value":20}\n')
				stream.flush()
				reply = json.loads(stream.readline())
		finally:
			# Always stop the server, even if an assertion-worthy error occurred,
			# so the non-daemon thread can't hang the test run.
			server.shutdown()
			server.server_close()
			thread.join()

		# The reply echoes the value as sent (20, a percent) and reports the active mode.
		self.assertEqual(reply["mode"], "loss")
		self.assertEqual(reply["value"], 20)
		# State stores loss as a fraction: 20% -> 0.2.
		self.assertEqual(state.settings()["loss"], 0.2)

	def test_packet_log_contains_action_and_length(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "router.jsonl"
			RouterLog(str(path)).write(FakePacket(), "passed")

			# One write produces one JSONL line.
			record = json.loads(path.read_text(encoding="utf-8").strip())

		self.assertEqual(record["action"], "passed")
		# Length comes from FakePacket.get_payload_len().
		self.assertEqual(record["length"], 4)
		self.assertEqual(record["hw_protocol"], 2048)


if __name__ == "__main__":
	unittest.main()