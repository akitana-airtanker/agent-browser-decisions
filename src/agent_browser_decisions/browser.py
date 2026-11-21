"""Thin adapter over the `agent-browser` CLI and Chrome profile management."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

from agent_browser_decisions.core import parse_snapshot, redact_text

# Chrome's password manager (save prompt, leak warning) appears after a successful login and swallows all mouse and
# keyboard events to the page (JavaScript still works). A fresh profile with both turned off keeps automation quiet.
QUIET_PREFERENCES: dict[str, Any] = {
    "credentials_enable_service": False,
    "profile": {"password_manager_enabled": False, "password_manager_leak_detection": False},
}


def quiet_profile() -> str:
    """Create a fresh Chrome profile directory with the password manager disabled. Caller removes it when done."""
    root = tempfile.mkdtemp(prefix="abd-profile-")
    os.makedirs(os.path.join(root, "Default"))
    with open(os.path.join(root, "Default", "Preferences"), "w", encoding="utf-8") as file:
        json.dump(QUIET_PREFERENCES, file)
    return root


def detect_agent_browser(timeout_s: float = 10.0) -> dict[str, Any]:
    """Check whether `agent-browser` is available on PATH and report its version."""
    binary = shutil.which("agent-browser")
    if not binary:
        return {"installed": False, "path": None, "version": None}
    version: str | None = None
    try:
        res = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if res.returncode == 0:
            version = (res.stdout.strip() or res.stderr.strip()) or None
    except (subprocess.TimeoutExpired, OSError):
        version = None
    return {"installed": True, "path": binary, "version": version}


class AgentBrowser:
    """Session-scoped wrapper around the `agent-browser` subprocess CLI."""

    def __init__(
        self,
        session: str | None = None,
        *,
        timeout_s: float = 60.0,
        secrets: set[str] | frozenset[str] = frozenset(),
        on_metric: Callable[[str, float], None] | None = None,
    ) -> None:
        self.session = session
        self.timeout_s = timeout_s
        self.secrets = secrets
        self._on_metric = on_metric

    def call(self, *args: str, check: bool = True, category: str = "browser") -> str:
        started = time.perf_counter()
        env = {**os.environ, "AGENT_BROWSER_SESSION": self.session} if self.session else None
        try:
            result = subprocess.run(
                ["agent-browser", *args],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                env=env,
            )
        except FileNotFoundError as err:
            raise RuntimeError(
                "agent-browser CLI not found on PATH. Install it with: npm i -g agent-browser && agent-browser install"
            ) from err
        if self._on_metric is not None:
            self._on_metric(category, time.perf_counter() - started)
        if check and result.returncode != 0:
            detail = redact_text(result.stderr.strip() or result.stdout.strip(), self.secrets)
            raise RuntimeError(f"agent-browser {' '.join(args[:2])} failed: {detail}")
        return result.stdout

    def observe(self, category: str = "observe") -> dict[str, Any]:
        payload = json.loads(self.call("snapshot", "-i", "--json", category=category))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("snapshot"), str):
            raise RuntimeError(f"unexpected snapshot payload: {str(payload)[:200]}")
        raw = data["snapshot"]
        origin = data.get("origin", "")
        return {"url": origin if isinstance(origin, str) else "", "raw": raw, "elements": parse_snapshot(raw)}

    def open(self, url: str, *, profile: str | None = None) -> None:
        if profile:
            self.call("--profile", profile, "open", url)
        else:
            self.call("open", url)

    def current_url(self) -> str:
        return self.call("get", "url", check=False, category="check").strip()

    def batch(self, commands: list[list[str]], *, bail: bool = True, category: str = "act") -> list[dict[str, Any]]:
        started = time.perf_counter()
        env = {**os.environ, "AGENT_BROWSER_SESSION": self.session} if self.session else None
        args = ["agent-browser", "batch", "--json", "--omit-command"]
        if bail:
            args.append("--bail")
        try:
            result = subprocess.run(
                args,
                input=json.dumps(commands, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                env=env,
            )
        except FileNotFoundError as err:
            raise RuntimeError(
                "agent-browser CLI not found on PATH. Install it with: npm i -g agent-browser && agent-browser install"
            ) from err
        if self._on_metric is not None:
            self._on_metric(category, time.perf_counter() - started)
        if not result.stdout.strip():
            detail = redact_text(result.stderr.strip() or "empty batch output", self.secrets)
            raise RuntimeError(f"agent-browser batch failed: {detail}")
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as err:
            detail = redact_text(result.stdout[:200], self.secrets)
            raise RuntimeError(f"unexpected batch payload: {detail}") from err
        if not isinstance(parsed, list):
            raise RuntimeError(f"unexpected batch payload type: {type(parsed).__name__}")
        return parsed

    def close(self) -> None:
        self.call("close", check=False)
