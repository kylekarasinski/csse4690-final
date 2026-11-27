from __future__ import annotations

import math
import threading
from collections import Counter
from collections.abc import Mapping

from .effects import DEFAULTS, decide

# The only valid counter names. "corrupted" isn't recorded anywhere yet
# (likely reserved for the packet-editing milestone) but still appears in stats().
_COUNTERS = ("seen", "passed", "dropped", "delayed", "corrupted")


class RouterState:
    """Thread-safe holder for the live effect settings and packet counters.

    Shared by the NFQUEUE callback, the scheduler thread, and the control
    server's handler threads, so every access goes through one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Copy so mutating live settings never alters the module-level DEFAULTS.
        self._settings = dict(DEFAULTS)
        # Counter returns 0 for missing keys, so counters need no initialization.
        self._counts: Counter[str] = Counter()

    def settings(self) -> dict[str, float]:
        """Return a snapshot copy of the current settings."""
        with self._lock:
            # Copy so callers can't mutate shared state or see it change mid-use.
            return dict(self._settings)

    def update_settings(self, updates: Mapping[str, object]) -> dict[str, float]:
        """Validate and apply a partial settings update; return the full new settings.

        All-or-nothing: if any key or value is invalid, ValueError is raised
        and nothing is changed.
        """
        # Reject unknown keys up front so typos don't silently do nothing.
        unknown = set(updates) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")

        # Validate everything before taking the lock or touching state.
        validated: dict[str, float] = {}
        for key, raw_value in updates.items():
            # bool is a subclass of int, so float(True) would pass; reject it explicitly.
            if isinstance(raw_value, bool):
                raise ValueError(f"{key} must be a number")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a number") from exc
            # Rejects NaN/inf (which would break random comparisons) and negatives.
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and non-negative")
            # loss is a probability, so it's capped at 1 (100%).
            if key == "loss" and value > 1:
                raise ValueError("loss must be between 0 and 1")
            validated[key] = value

        # Apply the whole batch under one lock so readers never see a half-applied update.
        with self._lock:
            self._settings.update(validated)
            return dict(self._settings)

    def decide(self) -> float | None:
        """Pick a verdict for one packet from a snapshot of the current settings.

        Returns a delay in seconds, or None to drop (see effects.decide).
        """
        # Snapshot first, then decide outside the lock so the RNG work doesn't block other threads.
        return decide(self.settings())

    def record(self, counter: str) -> int:
        """Increment a named counter and return its new value (used as the "seen" sequence number)."""
        if counter not in _COUNTERS:
            raise ValueError(f"Unknown counter: {counter}")
        with self._lock:
            self._counts[counter] += 1
            # Returned under the same lock, so concurrent callers get unique values.
            return self._counts[counter]

    def stats(self) -> dict[str, int]:
        """Return a snapshot of all counters, including ones still at zero."""
        with self._lock:
            return {key: self._counts[key] for key in _COUNTERS}

    def mode(self) -> str:
        """Describe active effects as a label, e.g. "loss+delay", or "pass" if none are active."""
        settings = self.settings()
        # Any setting above zero is active; strip "_ms" so keys read "delay"/"jitter".
        active = [key.removesuffix("_ms") for key, value in settings.items() if value > 0]
        return "+".join(active) if active else "pass"