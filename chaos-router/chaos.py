#!/usr/bin/env python3
"""Compatibility launcher for the NFQUEUE router package."""

from router.chaos import PacketScheduler, make_callback, main, run

__all__ = ["PacketScheduler", "make_callback", "main", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
