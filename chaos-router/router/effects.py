from __future__ import annotations

import random
from collections.abc import Mapping

# Baseline "no effect" settings: no loss, no delay, no jitter.
# The "pass" control command resets to these. Units: loss is a fraction
# (0.0-1.0), delay_ms and jitter_ms are milliseconds.
DEFAULTS: dict[str, float] = {"loss": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}


def decide(settings: Mapping[str, float]) -> float | None:
	"""Return a delay in seconds, or None when this packet should be dropped."""
	# Loss is checked first: each packet is dropped independently with
	# probability settings["loss"], so a dropped packet is never delayed.
	if random.random() < settings["loss"]:
		return None
	# Base delay plus uniform jitter in [-jitter_ms, +jitter_ms], so delay
	# varies per packet (and packets can be reordered by the scheduler).
	delay_ms = settings["delay_ms"] + random.uniform(-settings["jitter_ms"], settings["jitter_ms"])
	# Jitter can push the total below zero; clamp to 0 (no delay), then
	# convert ms -> seconds, which is what PacketScheduler expects.
	return max(0.0, delay_ms) / 1000