#!/usr/bin/env python3
"""Compatibility launcher for the NFQUEUE router package."""

# Thin shim so the router can still be started as `python chaos.py` (as the
# namespace test script does) now that the real code lives in the router package.
# Re-exporting the names below also keeps older `from chaos import ...` imports working.
from router.chaos import PacketScheduler, make_callback, main, run

# Declares the public API of this shim; also stops linters from flagging the
# imports as unused.
__all__ = ["PacketScheduler", "make_callback", "main", "run"]


if __name__ == "__main__":
    # Run the CLI and exit with its return code (0 ok, 1 on error).
    raise SystemExit(main())