#!/usr/bin/env python3
"""Minimal localhost-only 3-screen onboarding wizard for agent-browser-decisions demos and benchmarks.

Simulates a multi-screen SPA with single-use one-time codes (consuming a code on Step 1
so restarting from Step 1 with the same code returns HTTP 409) and async transitions.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_bytes()


class WizardState:
    def __init__(self, delay_ms: int) -> None:
        self.delay_s = max(0.0, delay_ms / 1000.0)
        self.lock = threading.Lock()
        self.used_codes: set[str] = set()
        self.tokens: dict[str, dict[str, str]] = {}

    def verify_code(self, code: str, name: str, email: str) -> tuple[int, dict[str, object]]:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        code = code.strip()
        if not code:
            return 400, {"ok": False, "error": "One-time code is required"}
        with self.lock:
            if code in self.used_codes:
                return 409, {
                    "ok": False,
                    "error": f"Code {code} was already used — restarting requires a fresh code",
                }
            self.used_codes.add(code)
            token = f"tok-{secrets.token_hex(4)}"
            self.tokens[token] = {"code": code, "name": name, "email": email}
        return 200, {"ok": True, "token": token, "code": code}

    def save_workspace(self, token: str) -> tuple[int, dict[str, object]]:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        with self.lock:
            if token not in self.tokens:
                return 401, {"ok": False, "error": "Invalid or expired session token"}
        return 200, {"ok": True, "token": token}

    def reset(self) -> None:
        with self.lock:
            self.used_codes.clear()
            self.tokens.clear()


def make_handler(state: WizardState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(INDEX_HTML)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(INDEX_HTML)
                return
            if path == "/health":
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
                return
            if path == "/api/verify-code":
                status, payload = state.verify_code(
                    str(data.get("code", "")),
                    str(data.get("name", "")),
                    str(data.get("email", "")),
                )
                self._send_json(status, payload)
                return
            if path == "/api/save-workspace":
                status, payload = state.save_workspace(str(data.get("token", "")))
                self._send_json(status, payload)
                return
            if path == "/api/reset":
                state.reset()
                self._send_json(200, {"ok": True})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local 3-screen onboarding wizard.")
    parser.add_argument("--port", type=int, default=8765, help="Localhost port (default: 8765)")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=250,
        help="Simulated async API delay in milliseconds (default: 250)",
    )
    args = parser.parse_args()
    state = WizardState(delay_ms=args.delay_ms)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    actual_port = server.server_address[1]
    print(f"http://127.0.0.1:{actual_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
