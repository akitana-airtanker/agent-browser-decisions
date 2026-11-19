"""Command-line interface for agent-browser-decisions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from agent_browser_decisions import __version__
from agent_browser_decisions.browser import detect_agent_browser, quiet_profile
from agent_browser_decisions.core import (
    DEFAULT_BASE_URL,
    DEFAULT_DECISION_MODEL,
    parse_dotenv,
    validate_plan,
)
from agent_browser_decisions.runner import run_plan_file


def _default_dotenv_candidates(explicit: str | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    if env_override := os.environ.get("AGENT_BROWSER_DECISIONS_ENV_FILE"):
        return [Path(env_override)]
    # Check CWD first (works with `uv tool install`), then editable repo root if present.
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [Path.cwd() / ".env"]
    if (repo_root / "pyproject.toml").is_file() and (repo_root / ".env") != candidates[0]:
        candidates.append(repo_root / ".env")
    return candidates


def load_dotenv_files(explicit: str | None = None) -> Path | None:
    """Load the first existing .env file without clobbering already-set environment variables."""
    for candidate in _default_dotenv_candidates(explicit):
        if not candidate.is_file():
            continue
        try:
            pairs = parse_dotenv(candidate.read_text(encoding="utf-8"))
        except OSError:
            continue
        for key, val in pairs.items():
            if val and not os.environ.get(key):
                os.environ[key] = val
        return candidate
    return None


def doctor_report(explicit_env_file: str | None = None) -> dict[str, Any]:
    """Inspect runtime prerequisites and return a JSON-serialisable health report."""
    had_env_key = bool(os.environ.get("OPENAI_API_KEY"))
    loaded_dotenv = load_dotenv_files(explicit_env_file)
    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    key_source = "env" if had_env_key else ("dotenv" if has_key else None)
    browser_info = detect_agent_browser()
    tier1_ready = bool(browser_info["installed"])
    tier2_ready = tier1_ready and has_key
    return {
        "ok": tier1_ready,
        "version": __version__,
        "python": sys.version.split()[0],
        "agent_browser": browser_info,
        "decisions_api": {
            "api_key_set": has_key,
            "key_source": key_source,
            "dotenv_loaded": str(loaded_dotenv) if loaded_dotenv else None,
            "model": os.environ.get("DECISION_MODEL") or DEFAULT_DECISION_MODEL,
            "base_url": os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
        },
        "tier1_ready": tier1_ready,
        "tier2_ready": tier2_ready,
    }


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from err
    if parsed <= 0 or parsed != parsed or parsed == float("inf"):
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-browser-decisions",
        description=(
            "Run written browser plans through agent-browser with label matching and bounded Decisions API fallback."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profile", help="create a Chrome profile with the password manager off and print its path")

    doc = sub.add_parser("doctor", help="check agent-browser installation and Decisions API configuration")
    doc.add_argument("--env-file", help="optional .env file to check")

    val = sub.add_parser("validate", help="check a plan file without touching the browser")
    val.add_argument("plan", help="plan JSON file, or - for stdin")

    run = sub.add_parser("run", help="run a plan file")
    run.add_argument("plan", help="plan JSON file, or - for stdin")
    run.add_argument(
        "--mode",
        choices=["label", "decide"],
        default="label",
        help="decide: resolve every step with the decision model",
    )
    run.add_argument("--session", help="run in this agent-browser session (e.g. to continue a failed run)")
    run.add_argument(
        "--from-step",
        type=int,
        default=1,
        help="resume from 1-based step N in --session (skips url open and reset)",
    )
    run.add_argument("--env-file", help="optional .env file to load missing environment variables from")
    run.add_argument(
        "--timeout",
        type=_positive_float,
        default=60.0,
        help="per-command agent-browser timeout in seconds (default: 60)",
    )
    run.add_argument(
        "--api-timeout",
        type=_positive_float,
        default=30.0,
        help="per-request Decisions API timeout in seconds (default: 30)",
    )

    args = parser.parse_args(argv)

    if args.command == "profile":
        print(quiet_profile())
        return 0

    if args.command == "doctor":
        report = doctor_report(args.env_file)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    try:
        if args.plan == "-":
            plan = json.load(sys.stdin)
        else:
            with open(args.plan, encoding="utf-8") as source:
                plan = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "problems": [f"cannot read plan: {error}"]}, ensure_ascii=False, indent=2))
        return 2

    if args.command == "validate":
        problems = validate_plan(plan)
        print(json.dumps({"ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 2

    load_dotenv_files(args.env_file)
    result = run_plan_file(
        plan,
        mode=args.mode,
        session=args.session,
        from_step=args.from_step,
        browser_timeout_s=args.timeout,
        api_timeout_s=args.api_timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("problems"):
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
