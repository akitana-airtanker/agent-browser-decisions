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
        self._opened_registry: tuple[str, str | None] | None = None

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

    def _parse_registry(self, data: dict[str, Any]) -> tuple[str, str | None] | None:
        socket_dir = data.get("socketDir")
        namespace = data.get("namespace")
        if not isinstance(socket_dir, str) or not socket_dir:
            return None
        if namespace is not None and not isinstance(namespace, str):
            return None
        return (os.path.realpath(socket_dir), namespace)

    def session_info(self) -> dict[str, Any] | None:
        raw = self.call("session", "info", "--json", check=True, category="check")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def _capture_registry(self) -> tuple[str, str | None] | None:
        try:
            info = self.session_info()
        except (RuntimeError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return None
        return self._parse_registry(info) if info is not None else None

    def _confirm_closed(self, *, expected_registry: tuple[str, str | None] | None) -> bool:
        if expected_registry is None:
            return False
        expected_session = self.session or os.environ.get("AGENT_BROWSER_SESSION") or "default"
        try:
            info = self.session_info()
            if info is None:
                return False
            if info.get("session") != expected_session:
                return False
            if (
                info.get("active") is not False
                or info.get("pid") is not None
                or info.get("runtime") is not None
                or info.get("runtimeError") is not None
            ):
                return False
            registry = self._parse_registry(info)
            if registry != expected_registry:
                return False
            socket_dir, _namespace = registry
            if not os.path.isdir(socket_dir) or not os.access(socket_dir, os.R_OK | os.X_OK):
                return False
            os.listdir(socket_dir)

            raw_list = self.call("session", "list", "--json", check=True, category="check")
            list_payload = json.loads(raw_list)
            if not isinstance(list_payload, dict) or list_payload.get("success") is not True:
                return False
            list_data = list_payload.get("data")
            sessions = list_data.get("sessions") if isinstance(list_data, dict) else None
            if not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
                return False
            return expected_session not in sessions
        except (RuntimeError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return False

    def open(self, url: str, *, profile: str | None = None) -> None:
        if self._opened_registry is None:
            self._opened_registry = self._capture_registry()
        if profile:
            self.call("--profile", profile, "open", url)
        else:
            self.call("open", url)
        captured = self._capture_registry()
        if captured is not None:
            self._opened_registry = captured

    def current_url(self) -> str:
        try:
            return self.call("get", "url", check=True, category="check").strip()
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            return ""

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

    def close(self) -> bool:
        expected_registry = self._opened_registry or self._capture_registry()
        try:
            self.call("close", check=True)
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            return False
        return self._confirm_closed(expected_registry=expected_registry)
