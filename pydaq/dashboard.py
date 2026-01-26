"""Minimal HTTP dashboard (stdlib only).

This module provides a tiny read-only web API that can be enabled in the station configuration.
It is intentionally dependency-free and suitable for Raspberry Pi deployments.

Endpoints:
- ``GET /health``: liveness check
- ``GET /instruments``: list configured instruments
- ``GET /latest``: last-known sample per instrument (best effort)

Notes:
- This is *not* meant as a full-featured dashboard UI. It is a thin JSON surface that you can
  scrape, feed into Grafana/Prometheus exporters, or display in a simple web page.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the dashboard server.

    The handler reads state from a shared in-memory mapping, typically:

    .. code-block:: python

        state_reference = {"instruments": {"tei49c": instrument_instance, ...}}

    The mapping is updated by the orchestrator in the main process.
    """

    state_reference: Dict[str, Any] = {}

    def _send_json(self, code: int, payload: Any) -> None:
        """Send a JSON response.

        Args:
            code: HTTP status code.
            payload: Any JSON-serializable object.
        """
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        """Handle ``GET`` requests."""
        if self.path in ("/", "/health"):
            return self._send_json(200, {"ok": True})

        if self.path == "/instruments":
            instruments = list((self.state_reference.get("instruments", {}) or {}).keys())
            return self._send_json(200, {"instruments": instruments})

        if self.path == "/latest":
            latest = {}
            for instrument_name, instrument in (self.state_reference.get("instruments", {}) or {}).items():
                latest[instrument_name] = {
                    "enabled": instrument.state.enabled,
                    "last_sample_timestamp": instrument.state.last_sample_ts,
                    "last_error": instrument.state.last_error,
                    "latest": instrument.state.latest,
                }
            return self._send_json(200, latest)

        self._send_json(404, {"error": "not found", "path": self.path})

    def log_message(self, fmt: str, *args) -> None:
        """Silence the default HTTP server logging.

        The orchestrator already logs events; duplicating request logs tends to be noisy.
        """
        return


def start_dashboard(host: str, port: int, state_reference: Dict[str, Any]) -> ThreadingHTTPServer:
    """Create a configured dashboard server instance.

    Args:
        host: Bind address (e.g. ``0.0.0.0``).
        port: TCP port (e.g. ``8088``).
        state_reference: Shared mapping containing orchestrator state.

    Returns:
        A configured ``ThreadingHTTPServer``. Call ``serve_forever()`` in a daemon thread.
    """
    handler_class = type("Handler", (_Handler,), {})
    handler_class.state_reference = state_reference
    return ThreadingHTTPServer((host, port), handler_class)
