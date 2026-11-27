from __future__ import annotations

import json
import socketserver
import time
from typing import Any

from .effects import DEFAULTS
from .state import RouterState

# Maps the short command name to the RouterState setting it controls.
# "loss" is sent as a percentage and converted to a fraction in _dispatch.
_DIRECT_SETTINGS = {"loss": "loss", "delay": "delay_ms", "jitter": "jitter_ms"}


class ControlRequestHandler(socketserver.StreamRequestHandler):
	"""Handles one client connection speaking newline-delimited JSON.

	Each line is a command object ({"cmd": ...}); each gets exactly one JSON
	line in reply. The connection stays open for multiple commands.
	"""

	server: ControlServer

	def handle(self) -> None:
		# Read up to 64 KiB + 1 byte per line; the extra byte lets us detect
		# oversize lines without buffering unbounded input. Loop ends on EOF.
		while line := self.rfile.readline(65537):
			if len(line) > 65536:
				# Can't resync mid-line, so report the error and drop the connection.
				self._reply({"ok": False, "error": "command line too long"})
				return
			try:
				request = json.loads(line)
				reply = self._dispatch(request)
			# Bad input (malformed JSON, bad encoding, invalid values raised by
			# _dispatch or state) becomes an error reply; the connection stays open.
			except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc: 
				reply = {"ok": False, "error": str(exc)}
			self._reply(reply)

	def _dispatch(self, request: Any) -> dict[str, Any]:
		"""Execute one command and return the reply dict.

		Validation failures are either returned as {"ok": False, ...} or raised
		as ValueError, which handle() converts to the same error shape.
		"""
		if not isinstance(request, dict):
			return {"ok": False, "error": "command must be a JSON object"}

		command = request.get("cmd")
		state = self.server.router_state
		# {"cmd": "set", <setting>: <value>, ...}: update several settings at once.
		if command == "set":
			# Everything except "cmd" is treated as a setting; state validates the keys/values.
			updates = {key: value for key, value in request.items() if key != "cmd"}
			if not updates:
				return {"ok": False, "error": "no settings supplied"}
			settings = state.update_settings(updates)
			return {"ok": True, "settings": settings}
		# {"cmd": "loss"|"delay"|"jitter", "value": N}: shortcut for one setting.
		if command in _DIRECT_SETTINGS:
			if "value" not in request:
				return {"ok": False, "error": "value is required"}
			value = request["value"]
			# bool is a subclass of int, so float(True) would silently succeed; reject it.
			if isinstance(value, bool):
				raise ValueError(f"{command} value must be a number")
			try:
				value = float(value)
			except (TypeError, ValueError) as exc:
				raise ValueError(f"{command} value must be a number") from exc
			# Echo back the value as the client sent it (before any unit conversion).
			response_value = value
			if command == "loss":
				# Client sends loss as a percent (e.g. 5); state stores a fraction (0.05).
				value /= 100
			# Return value is unused; the reply reports mode() and the echoed value instead.
			settings = state.update_settings({_DIRECT_SETTINGS[command]: value})
			return {"ok": True, "mode": state.mode(), "value": response_value}
		# {"cmd": "pass"}: reset all effects to defaults (packets pass untouched).
		if command == "pass":
			settings = state.update_settings(DEFAULTS)
			return {"ok": True, "mode": "pass", "settings": settings}
		# {"cmd": "stats"}: packet counters plus current settings.
		if command == "stats":
			return {"ok": True, "stats": state.stats(), "settings": state.settings()}
		# {"cmd": "ping"}: liveness check; "pong" is the server's wall-clock time.
		if command == "ping":
			return {"ok": True, "mode": state.mode(), "pong": time.time()}
		return {"ok": False, "error": "unknown command"}

	def _reply(self, response: dict[str, Any]) -> None:
		"""Send one JSON object as a single newline-terminated line."""
		self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
		self.wfile.flush()


class ControlServer(socketserver.ThreadingTCPServer):
	"""TCP server that gives each client connection its own handler thread."""

	# Allow quick restarts without waiting for the old socket to leave TIME_WAIT.
	allow_reuse_address = True
	# Handler threads won't keep the process alive on shutdown.
	daemon_threads = True
	# Shared with every handler via self.server.router_state.
	router_state: RouterState

	def __init__(self, address: tuple[str, int], state: RouterState) -> None:
		# Set before super().__init__ so it exists as soon as the socket is bound
		# and a client could connect.
		self.router_state = state
		super().__init__(address, ControlRequestHandler)