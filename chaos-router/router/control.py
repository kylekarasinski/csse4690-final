from __future__ import annotations

import json
import socketserver
import time
from typing import Any

from .effects import DEFAULTS
from .state import RouterState

_DIRECT_SETTINGS = {"loss": "loss", "delay": "delay_ms", "jitter": "jitter_ms"}


class ControlRequestHandler(socketserver.StreamRequestHandler):
    server: ControlServer

    def handle(self) -> None:
        while line := self.rfile.readline(65537):
            if len(line) > 65536:
                self._reply({"ok": False, "error": "command line too long"})
                return
            try:
                request = json.loads(line)
                reply = self._dispatch(request)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc: 
                reply = {"ok": False, "error": str(exc)}
            self._reply(reply)

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "command must be a JSON object"}

        command = request.get("cmd")
        state = self.server.router_state
        if command == "set":
            updates = {key: value for key, value in request.items() if key != "cmd"}
            if not updates:
                return {"ok": False, "error": "no settings supplied"}
            settings = state.update_settings(updates)
            return {"ok": True, "settings": settings}
        if command in _DIRECT_SETTINGS:
            if "value" not in request:
                return {"ok": False, "error": "value is required"}
            value = request["value"]
            if isinstance(value, bool):
                raise ValueError(f"{command} value must be a number")
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{command} value must be a number") from exc
            response_value = value
            if command == "loss":
                value /= 100
            settings = state.update_settings({_DIRECT_SETTINGS[command]: value})
            return {"ok": True, "mode": state.mode(), "value": response_value}
        if command == "pass":
            settings = state.update_settings(DEFAULTS)
            return {"ok": True, "mode": "pass", "settings": settings}
        if command == "stats":
            return {"ok": True, "stats": state.stats(), "settings": state.settings()}
        if command == "ping":
            return {"ok": True, "mode": state.mode(), "pong": time.time()}
        return {"ok": False, "error": "unknown command"}

    def _reply(self, response: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    router_state: RouterState

    def __init__(self, address: tuple[str, int], state: RouterState) -> None:
        self.router_state = state
        super().__init__(address, ControlRequestHandler)
