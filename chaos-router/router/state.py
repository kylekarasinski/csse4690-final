from __future__ import annotations

import math
import threading
from collections import Counter
from collections.abc import Mapping

from .effects import DEFAULTS, decide

_COUNTERS = ("seen", "passed", "dropped", "delayed", "corrupted")


class RouterState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings = dict(DEFAULTS)
        self._counts: Counter[str] = Counter()

    def settings(self) -> dict[str, float]:
        with self._lock:
            return dict(self._settings)

    def update_settings(self, updates: Mapping[str, object]) -> dict[str, float]:
        unknown = set(updates) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")

        validated: dict[str, float] = {}
        for key, raw_value in updates.items():
            if isinstance(raw_value, bool):
                raise ValueError(f"{key} must be a number")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a number") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and non-negative")
            if key == "loss" and value > 1:
                raise ValueError("loss must be between 0 and 1")
            validated[key] = value

        with self._lock:
            self._settings.update(validated)
            return dict(self._settings)

    def decide(self) -> float | None:
        return decide(self.settings())

    def record(self, counter: str) -> int:
        if counter not in _COUNTERS:
            raise ValueError(f"Unknown counter: {counter}")
        with self._lock:
            self._counts[counter] += 1
            return self._counts[counter]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {key: self._counts[key] for key in _COUNTERS}

    def mode(self) -> str:
        settings = self.settings()
        active = [key.removesuffix("_ms") for key, value in settings.items() if value > 0]
        return "+".join(active) if active else "pass"
