from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RouterLog:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, packet: Any, action: str, delay_ms: float | None = None, seen: int | None = None) -> None:
        if self.path is None:
            return

        if hasattr(packet, "get_payload_len"):
            length = packet.get_payload_len()
        else:
            length = len(bytes(packet))

        record: dict[str, Any] = {"time": datetime.now(timezone.utc).isoformat(), "action": action, "length": length}
        if hasattr(packet, "hw_protocol"):
            record["hw_protocol"] = packet.hw_protocol
        if delay_ms is not None:
            record["delay_ms"] = round(delay_ms, 3)
        if seen is not None:
            record["seen"] = seen

        with self._lock:
            with self.path.open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps(record, sort_keys=True) + "\n")
