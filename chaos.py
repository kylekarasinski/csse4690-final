"""Chaos engine. Decides what happens to each message. mim.py calls decide() once per message.

Contract with mim.py:
    DEFAULTS                          every setting that exists, with its starting value
    decide(settings) -> None          drop the message
    decide(settings) -> float >= 0    forward it after this many seconds
"""
import random

DEFAULTS = {"loss": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}   # loss is a percent, 0 to 100


def decide(settings):
    if random.random() < settings["loss"] / 100:
        return None
    # Jitter adds 0 to jitter_ms on top of the base delay (plan section 6).
    delay_ms = settings["delay_ms"] + random.uniform(0, settings["jitter_ms"])
    return delay_ms / 1000
