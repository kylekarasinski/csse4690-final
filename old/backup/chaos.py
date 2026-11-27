"""Compatibility import for the archived UDP MIM fallback."""

from __future__ import annotations

import sys
from pathlib import Path

_ROUTER_DIR = str(Path(__file__).resolve().parents[2] / "chaos-router")
_ADDED_PATH = _ROUTER_DIR not in sys.path
if _ADDED_PATH:
    sys.path.insert(0, _ROUTER_DIR)

try:
    from router.effects import DEFAULTS, decide
finally:
    if _ADDED_PATH:
        sys.path.remove(_ROUTER_DIR)

__all__ = ["DEFAULTS", "decide"]
