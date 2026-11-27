from __future__ import annotations

import random
from collections.abc import Mapping

DEFAULTS: dict[str, float] = {"loss": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}


def decide(settings: Mapping[str, float]) -> float | None:
    """Return a delay in seconds, or None when this packet should be dropped."""
    if random.random() < settings["loss"]:
        return None
    delay_ms = settings["delay_ms"] + random.uniform(-settings["jitter_ms"], settings["jitter_ms"])
    return max(0.0, delay_ms) / 1000
