import contextlib
import io
import json
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chaos import PacketCounter, make_callback, run


class FakePacket:
    def __init__(self) -> None:
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True

    def __bytes__(self) -> bytes:
        return b"test"


class FakeQueue:
    def __init__(self, signal_handlers) -> None:
        self.signal_handlers = signal_handlers
        self.bind_args = None
        self.blocking = False
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


class ChaosPassthroughTests(unittest.TestCase):
    def test_callback_accepts_and_counts_every_packet(self) -> None:
        counter = PacketCounter()
        callback = make_callback(counter)

        packet = FakePacket()
        callback(packet)

        self.assertTrue(packet.accepted)
        self.assertEqual(counter.snapshot()["seen"], 1)
        self.assertEqual(counter.snapshot()["passed"], 1)

    def test_callback_writes_packet_to_jsonl_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "router.jsonl"
            counter = PacketCounter(str(log_path))
            callback = make_callback(counter)

            callback(FakePacket())

            record = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(record["action"], "pass")
            self.assertEqual(record["seen"], 1)
            self.assertEqual(record["length"], 4)

    def test_run_uses_bind_max_len_parameter(self) -> None:
        signal_handlers = {}
        queue = FakeQueue(signal_handlers)
        module = SimpleNamespace(NetfilterQueue=lambda: queue)

        with (
            patch.dict(sys.modules, {"netfilterqueue": module}),
            patch(
                "chaos.signal.signal",
                side_effect=lambda signum, handler: signal_handlers.__setitem__(
                    signum, handler
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            run(queue_num=3, queue_maxlen=64, log_path=None)

        self.assertEqual(queue.bind_args[0], 3)
        self.assertEqual(queue.bind_args[2], 64)
        self.assertFalse(queue.blocking)
        self.assertEqual(queue.run_calls, 1)
        self.assertTrue(queue.unbound)


if __name__ == "__main__":
    unittest.main()
