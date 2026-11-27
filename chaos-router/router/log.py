from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RouterLog:
    """Appends one JSON object per line (JSONL) describing each packet verdict.

    Logging is disabled (every write is a no-op) when constructed with no path.
    """

    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        # Serializes writes: packets are logged from both the NFQUEUE callback
        # (main thread) and the scheduler thread, so lines must not interleave.
        self._lock = threading.Lock()
        if self.path:
            # Create the log's parent directory up front rather than on first write.
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, packet: Any, action: str, delay_ms: float | None = None, seen: int | None = None) -> None:
        """Log one packet event.

        action is the verdict ("passed", "dropped", "delayed"); delay_ms and
        seen are included only when provided.
        """
        # Logging disabled: skip all the work below.
        if self.path is None:
            return

        # NetfilterQueue packets expose get_payload_len(); other packet types
        # (e.g. test doubles) fall back to measuring their raw bytes.
        if hasattr(packet, "get_payload_len"):
            length = packet.get_payload_len()
        else:
            length = len(bytes(packet))

        # UTC ISO-8601 timestamp so logs are unambiguous across timezones.
        record: dict[str, Any] = {"time": datetime.now(timezone.utc).isoformat(), "action": action, "length": length}
        # Optional fields: only present when the packet/caller supplies them.
        if hasattr(packet, "hw_protocol"):
            record["hw_protocol"] = packet.hw_protocol
        if delay_ms is not None:
            record["delay_ms"] = round(delay_ms, 3)
        if seen is not None:
            record["seen"] = seen

        with self._lock:
            # Reopened in append mode on every call: slower than keeping a handle
            # open, but needs no close()/flush handling and survives log rotation.
            with self.path.open("a", encoding="utf-8") as log_file:
                # sort_keys gives a stable field order, which keeps lines diff/grep-friendly.
                log_file.write(json.dumps(record, sort_keys=True) + "\n")