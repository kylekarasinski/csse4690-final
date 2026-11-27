import json
import tempfile
import unittest
from pathlib import Path

from chaos import PacketCounter, make_callback


class FakePacket:
    def __init__(self) -> None:
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True

    def __bytes__(self) -> bytes:
        return b"test"


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


if __name__ == "__main__":
    unittest.main()
