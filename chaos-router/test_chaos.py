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
    def __init__(self) -> None:
        self.accepted = False
        self.dropped = False
        self.retained = False
        self.hw_protocol = 2048

    def accept(self) -> None:
        self.accepted = True

    def drop(self) -> None:
        self.dropped = True

    def retain(self) -> None:
        self.retained = True

    def get_payload_len(self) -> int:
        return 4


class FakeScheduler:
    def __init__(self) -> None:
        self.scheduled = []

    def schedule(self, packet, delay_seconds: float, seen: int) -> None:
        packet.retain()
        self.scheduled.append((packet, delay_seconds, seen))


class FakeQueue:
    def __init__(self, signal_handlers) -> None:
        self.signal_handlers = signal_handlers
        self.bind_args = None
        self.blocking = True
        self.run_calls = 0
        self.unbound = False

    def bind(self, queue_num, callback, max_len=1024) -> None:
        self.bind_args = (queue_num, callback, max_len)

    def run(self, block=False) -> None:
        self.blocking = block
        self.run_calls += 1
        self.signal_handlers[signal.SIGTERM](signal.SIGTERM, None)

    def unbind(self) -> None:
        self.unbound = True


class ChaosRouterTests(unittest.TestCase):
    def test_pass_callback_accepts_and_counts_packet(self) -> None:
        state = RouterState()
        packet = FakePacket()
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
            with patch("router.effects.random.random", return_value=0.5):
                callback(packet)

        self.assertTrue(packet.dropped)
        self.assertEqual(state.stats()["dropped"], 1)

    def test_jitter_is_included_in_delay_decision(self) -> None:
        state = RouterState()
        state.update_settings({"delay_ms": 20, "jitter_ms": 10})

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

        self.assertTrue(packet.retained)
        self.assertFalse(packet.accepted)
        self.assertEqual(scheduler.scheduled, [(packet, 0.05, 1)])
        self.assertEqual(state.stats()["delayed"], 1)

    def test_scheduler_releases_retained_packets_on_shutdown(self) -> None:
        state = RouterState()
        packet = FakePacket()
        with tempfile.TemporaryDirectory() as directory:
            scheduler = PacketScheduler(state, RouterLog(str(Path(directory) / "router.jsonl")))
            scheduler.schedule(packet, 60, 1)
            scheduler.close()

        self.assertTrue(packet.retained)
        self.assertTrue(packet.accepted)
        self.assertEqual(state.stats()["passed"], 1)

    def test_run_uses_supported_bind_api_and_stops_cleanly(self) -> None:
        signal_handlers = {}
        queue = FakeQueue(signal_handlers)
        module = SimpleNamespace(NetfilterQueue=lambda: queue)
        with (patch.dict(sys.modules, {"netfilterqueue": module}), patch("router.chaos.signal.signal", side_effect=lambda signum, handler: signal_handlers.__setitem__(signum, handler)), contextlib.redirect_stdout(io.StringIO())):
            run(queue_num=3, queue_maxlen=64, log_path=None, control_host="127.0.0.1", control_port=0)

        self.assertEqual(queue.bind_args[0], 3)
        self.assertEqual(queue.bind_args[2], 64)
        self.assertFalse(queue.blocking)
        self.assertEqual(queue.run_calls, 1)
        self.assertTrue(queue.unbound)

    def test_control_server_updates_effects_with_acknowledgment(self) -> None:
        state = RouterState()
        server = ControlServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with socket.create_connection(server.server_address) as connection:
                stream = connection.makefile("rwb")
                stream.write(b'{"cmd":"loss","value":20}\n')
                stream.flush()
                reply = json.loads(stream.readline())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertEqual(reply["mode"], "loss")
        self.assertEqual(reply["value"], 20)
        self.assertEqual(state.settings()["loss"], 0.2)

    def test_packet_log_contains_action_and_length(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "router.jsonl"
            RouterLog(str(path)).write(FakePacket(), "passed")

            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(record["action"], "passed")
        self.assertEqual(record["length"], 4)
        self.assertEqual(record["hw_protocol"], 2048)


if __name__ == "__main__":
    unittest.main()
