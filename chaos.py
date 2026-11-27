"""Chaos engine. Decides what happens to each message. mim.py calls decide() once per message.

Contract with mim.py:
    DEFAULTS                          every setting that exists, with its starting value
    decide(settings) -> None          drop the message
    decide(settings) -> float >= 0    forward it after this many seconds
"""
import random

DEFAULTS = {"loss": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}


def decide(settings):
    if random.random() < settings["loss"]:
        return None
    delay_ms = settings["delay_ms"] + random.uniform(-settings["jitter_ms"], settings["jitter_ms"])
    return max(0.0, delay_ms) / 1000
