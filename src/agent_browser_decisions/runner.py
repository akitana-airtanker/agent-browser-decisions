"""Plan execution orchestrator enforcing TLA+-verified session, profile, retry, and verify invariants."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from agent_browser_decisions.browser import AgentBrowser, quiet_profile
from agent_browser_decisions.core import (
    DEFAULT_PROVIDER,
    chosen_ref,
    command_for,
    compatible,
    decision_request,
    document,
    evaluate_verify_output,
    hash_route,
    match_label,
    redact_text,
    resolve_step_values,
    same_field_value,
    validate_plan,
    validate_run_options,
)
from agent_browser_decisions.decisions import DecisionsClient, DecisionsError, MissingKey


class Metrics:
    """Per-step timing and token breakdown."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.ms: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        self.tokens: int = 0

    def add(self, category: str, seconds: float, tokens: int = 0) -> None:
        self.ms[category] = self.ms.get(category, 0.0) + seconds * 1000
        self.calls[category] = self.calls.get(category, 0) + 1
        self.tokens += tokens

    def summary(self) -> dict[str, Any]:
        return {
            "ms": {k: round(v) for k, v in self.ms.items()},
            "calls": dict(self.calls),
            "tokens": self.tokens,
        }


def poll(predicate: Callable[[], bool], wait_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.perf_counter() + wait_s
    while True:
        if predicate():
            return True
        if time.perf_counter() >= deadline:
            return False
        time.sleep(interval_s)


class PlanRunner:
    """Stateless-per-run orchestrator: resolve -> act -> check effect -> verify."""

    def __init__(
        self,
        *,
        browser_factory: Callable[..., AgentBrowser] = AgentBrowser,
        decisions_factory: Callable[..., DecisionsClient] = DecisionsClient,
        profile_factory: Callable[[], str] = quiet_profile,
        browser_timeout_s: float = 60.0,
        api_timeout_s: float = 30.0,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        settle_s: float = 3.0,
        poll_s: float = 0.05,
        verify_poll_s: float = 0.5,
        effect_wait_s: float = 1.0,
        retries: int = 2,
    ) -> None:
        self._browser_factory = browser_factory
        self._decisions_factory = decisions_factory
        self._profile_factory = profile_factory
        self.browser_timeout_s = browser_timeout_s
        self.api_timeout_s = api_timeout_s
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.settle_s = settle_s
        self.poll_s = poll_s
        self.verify_poll_s = verify_poll_s
        self.effect_wait_s = effect_wait_s
        self.retries = retries

    def _resolve(
        self,
        browser: AgentBrowser,
        decisions: DecisionsClient,
        step: dict[str, Any],
        goal: str,
        mode: str,
        exclude: set[str],
    ) -> tuple[dict[str, Any] | None, int, dict[str, Any], dict[str, Any]]:
        deadline = time.perf_counter() + self.settle_s
        previous: str | None = None
        while True:
            page = browser.observe()
            # Two consecutive identical snapshots mean the page has settled and refs are not shifting.
            stable = previous is not None and page["raw"] == previous
            previous = page["raw"]
            remaining = [e for e in page["elements"] if e["ref"] not in exclude]
            expired = time.perf_counter() >= deadline
            if mode == "decide":
                if (stable and compatible(step, remaining)) or expired:
                    break
            else:
                element, status = match_label(step, remaining)
                if status == "hit" and element is not None and (stable or expired):
                    return element, 1, {}, page
                if (status == "ambiguous" and stable) or expired:
                    break
            time.sleep(self.poll_s)

        if step.get("nth") is not None:
            return None, 3, {"reason": "nth out of range or no match"}, page
        candidates = compatible(step, remaining) or compatible(step, remaining, use_role=False)
        if not candidates:
            return None, 3, {"reason": "no candidates"}, page
        if len(candidates) == 1:
            if mode == "label":
                return None, 3, {"reason": "no label match (single candidate)"}, page
            return candidates[0], 2, {"single": True}, page

        provider = getattr(decisions, "provider", DEFAULT_PROVIDER)
        model = getattr(decisions, "model", None)
        response = decisions.decide(
            decision_request(step, goal, candidates, page["url"], model=model, provider=provider)
        )
        ref, info = chosen_ref(response)
        if ref:
            chosen = next((e for e in candidates if e["ref"] == ref), None)
            if chosen is not None:
                return chosen, 2, {"confidence": info}, page
            return None, 3, {"reason": f"decision unseen ref {ref}"}, page
        return None, 3, {"reason": f"decision {info}"}, page

    def _changed_since(self, browser: AgentBrowser, page: dict[str, Any]) -> Callable[[], bool]:
        def check() -> bool:
            now = browser.observe(category="check")
            return now["raw"] != page["raw"] or now["url"] != page["url"]

        return check

    def _effect(
        self,
        browser: AgentBrowser,
        step: dict[str, Any],
        element: dict[str, Any],
        page: dict[str, Any],
        *,
        allow_keyboard: bool = True,
    ) -> str:
        action, ref = step["action"], f"@{element['ref']}"
        if action == "fill":
            expected = step["value"]

            def value_matches() -> bool:
                got = browser.call("get", "value", ref, check=False, category="check")
                return same_field_value(got, expected)

            return "ok" if poll(value_matches, min(0.5, self.effect_wait_s), self.poll_s) else "no_effect"
        if action == "check":
            checked = poll(
                lambda: browser.call("is", "checked", ref, check=False, category="check").strip() == "true",
                min(0.5, self.effect_wait_s),
                self.poll_s,
            )
            return "ok" if checked else "no_effect"
        if action == "select":
            return "ok"
        if step.get("expect") == "navigation" or (element["role"] == "link" and step.get("navigates", True)):
            whole = element["role"] == "link" and hash_route(
                browser.call("get", "attr", ref, "href", check=False, category="check")
            )

            def moved() -> bool:
                now = browser.current_url()
                return now != page["url"] if whole else document(now) != document(page["url"])

            if poll(moved, 1.5, 0.1):
                return "ok"
            if allow_keyboard and element["role"] == "link":
                # A wrapped inline link can have its box centre between lines; keyboard activation is geometry-free.
                browser.call("focus", ref, check=False, category="act")
                browser.call("press", "Enter", check=False, category="act")
                if poll(moved, 3.0, 0.1):
                    return "ok_keyboard"
            return "wrong_target"
        return "ok" if poll(self._changed_since(browser, page), self.effect_wait_s, self.poll_s) else "no_effect"

    def _run_step(
        self,
        browser: AgentBrowser,
        decisions: DecisionsClient,
        metrics: Metrics,
        index: int,
        step: dict[str, Any],
        goal: str,
        mode: str,
        secrets: set[str],
        log: list[dict[str, Any]],
    ) -> bool:
        started = time.perf_counter()
        metrics.reset()
        excluded: set[str] = set()
        tiers: list[int] = []
        repeats = 0
        once = bool(step.get("once"))

        if step["action"] == "press":
            page = browser.observe()
            browser.call("press", step["value"], category="act")
            outcome = "ok" if poll(self._changed_since(browser, page), self.effect_wait_s, self.poll_s) else "no_effect"
            step_ok = outcome != "no_effect"
            log.append(
                {
                    "step": index,
                    "tier": 0,
                    "tiers": [0],
                    "target": step["value"],
                    "ok": step_ok,
                    "effect": outcome,
                    "ms": round((time.perf_counter() - started) * 1000),
                    "metrics": metrics.summary(),
                }
            )
            return step_ok

        last_attempt: dict[str, Any] = {}
        while True:
            step_mode = "decide" if step.get("decide") else mode
            element, tier, info, page = self._resolve(browser, decisions, step, goal, step_mode, excluded)
            tiers.append(tier)
            entry: dict[str, Any] = {
                "step": index,
                "tier": max(tiers),
                "tiers": list(tiers),
                "target": step["target"],
                "url": page["url"],
                **last_attempt,
                **info,
            }
            if element is None:
                log.append(
                    entry
                    | {
                        "ok": False,
                        "excluded": len(excluded),
                        "repeats": repeats,
                        "ms": round((time.perf_counter() - started) * 1000),
                        "metrics": metrics.summary(),
                    }
                )
                return False
            try:
                browser.call(*command_for(step, element["ref"]), category="act")
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                log.append(
                    entry
                    | {
                        "ok": False,
                        "chose": element["name"][:80],
                        "error": redact_text(str(error), secrets)[:200],
                        "ms": round((time.perf_counter() - started) * 1000),
                        "metrics": metrics.summary(),
                    }
                )
                return False

            outcome = self._effect(browser, step, element, page, allow_keyboard=not once)
            last_attempt = {"chose": element["name"][:80], "effect": outcome}
            if once:
                break
            if outcome == "no_effect" and repeats < 1:
                repeats += 1
                continue
            if outcome == "wrong_target" and (tier == 1 or step.get("expect") == "navigation") and repeats < 1:
                repeats += 1
                continue
            if outcome == "wrong_target" and tier == 2 and len(excluded) < self.retries:
                excluded.add(element["ref"])
                continue
            break

        step_ok = outcome not in ("no_effect", "wrong_target")
        entry |= {
            "ok": step_ok,
            "chose": element["name"][:80],
            "effect": outcome,
            "excluded": len(excluded),
            "repeats": repeats,
            "ms": round((time.perf_counter() - started) * 1000),
            "metrics": metrics.summary(),
        }
        log.append(entry)
        return step_ok

    def _verify_page(
        self, browser: AgentBrowser, expression: str, timeout_s: float, secrets: set[str]
    ) -> tuple[bool, Any]:
        deadline = time.perf_counter() + timeout_s
        while True:
            try:
                out = browser.call("eval", expression, check=True, category="verify")
                passed, value = evaluate_verify_output(out)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                passed, value = False, {"error": redact_text(str(error), secrets)[:300]}
            if passed or time.perf_counter() >= deadline:
                return bool(passed), value
            time.sleep(self.verify_poll_s)

    def run_plan_file(
        self,
        plan: Any,
        *,
        mode: str = "label",
        session: str | None = None,
        from_step: int = 1,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate, resolve env secrets, execute steps, verify, and enforce session/profile cleanup invariants."""
        env_map = os.environ if environ is None else environ
        effective_provider = self.provider if self.provider is not None else env_map.get("DECISION_PROVIDER")
        problems = validate_plan(plan) + validate_run_options(
            plan, session=session, from_step=from_step, provider=effective_provider
        )
        if problems:
            return {
                "ok": False,
                "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
                "session_open": bool(session),
                "verified": None,
                "problems": problems,
            }
        resolved_steps, secrets, env_problems = resolve_step_values(
            plan["steps"][from_step - 1 :],
            env_map,
            start_index=from_step,
        )
        if env_problems:
            return {
                "ok": False,
                "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
                "session_open": bool(session),
                "verified": None,
                "problems": env_problems,
            }

        started = time.perf_counter()
        owns_session = bool(plan.get("reset") is True and not session and from_step == 1)
        session_opened = bool(session)
        profile: str | None = None
        if owns_session:
            session = f"abd-{int(time.time() * 1000)}-{os.getpid()}"
            profile = self._profile_factory()

        metrics = Metrics()
        browser = self._browser_factory(
            session,
            timeout_s=self.browser_timeout_s,
            secrets=secrets,
            on_metric=metrics.add,
        )
        decisions = self._decisions_factory(
            provider=self.provider,
            model=self.model,
            base_url=self.base_url,
            environ=env_map,
            timeout_s=self.api_timeout_s,
            on_metric=metrics.add,
        )

        interrupted = False
        prev_handlers: dict[int, Any] = {}
        is_main = threading.current_thread() is threading.main_thread()

        def _handle_signal(signum: int, _frame: Any) -> None:
            nonlocal interrupted
            interrupted = True
            raise KeyboardInterrupt(f"interrupted by signal {signum}")

        if is_main:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    prev_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, _handle_signal)
                except (ValueError, OSError):
                    pass

        log: list[dict[str, Any]] = []
        completed = False
        error: str | None = None
        verified: bool | None = None
        verify_value: Any = None
        closed = False

        try:
            with decisions:
                if plan.get("url") and from_step == 1:
                    try:
                        browser.open(plan["url"], profile=profile)
                        session_opened = True
                    except (RuntimeError, subprocess.TimeoutExpired, OSError) as failure:
                        error = redact_text(str(failure), secrets)[:300]
                        if profile:
                            shutil.rmtree(profile, ignore_errors=True)
                            profile = None
                        return {
                            "ok": False,
                            "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
                            "session_open": session_opened,
                            "verified": None,
                            "verify_value": None,
                            "stopped_at_step": 0,
                            "error": error,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000),
                            "url": "",
                            "steps": [],
                        }

                completed = True
                for offset, step in enumerate(resolved_steps):
                    index = from_step + offset
                    try:
                        if not self._run_step(
                            browser, decisions, metrics, index, step, plan.get("goal", ""), mode, secrets, log
                        ):
                            completed = False
                            break
                    except (
                        MissingKey,
                        DecisionsError,
                        RuntimeError,
                        httpx.HTTPError,
                        json.JSONDecodeError,
                        subprocess.TimeoutExpired,
                        OSError,
                    ) as failure:
                        log.append(
                            {
                                "step": index,
                                "target": step.get("target") or step.get("value"),
                                "ok": False,
                                "error": redact_text(str(failure), secrets)[:300],
                            }
                        )
                        completed = False
                        break

                if completed and plan.get("verify"):
                    verified, verify_value = self._verify_page(
                        browser, plan["verify"], plan.get("verify_timeout_s", 10), secrets
                    )
        except KeyboardInterrupt:
            interrupted = True
            completed = False
            error = "interrupted"
        finally:
            if is_main:
                for sig, prev in prev_handlers.items():
                    with contextlib.suppress(ValueError, OSError):
                        signal.signal(sig, prev)

            ok = (not interrupted) and completed and verified is not False
            url = ""
            if session_opened and not interrupted:
                try:
                    url = browser.current_url()
                except (RuntimeError, subprocess.TimeoutExpired, OSError):
                    url = ""

            # On normal success OR interrupt, close owned sessions and delete temp profiles cleanly.
            if (ok or interrupted) and owns_session and session_opened:
                with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired, OSError):
                    browser.close()
                closed = True
            session_open = session_opened and not closed
            if profile and not session_open:
                shutil.rmtree(profile, ignore_errors=True)
                profile = None

        stopped = next((e["step"] for e in log if not e.get("ok")), None)
        return {
            "ok": ok,
            "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
            "session_open": session_open,
            "verified": verified,
            "verify_value": verify_value,
            "stopped_at_step": stopped,
            "error": error,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "url": url,
            "steps": [
                {
                    k: e.get(k)
                    for k in (
                        "step",
                        "tier",
                        "tiers",
                        "target",
                        "chose",
                        "effect",
                        "confidence",
                        "reason",
                        "error",
                        "ms",
                    )
                    if e.get(k) is not None or k in ("step", "tier", "target", "chose", "effect", "ms")
                }
                for e in log
            ],
        }


def run_plan_file(
    plan: Any,
    mode: str = "label",
    session: str | None = None,
    from_step: int = 1,
    *,
    browser_timeout_s: float = 60.0,
    api_timeout_s: float = 30.0,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convenience entry point using the default PlanRunner."""
    return PlanRunner(
        browser_timeout_s=browser_timeout_s,
        api_timeout_s=api_timeout_s,
        provider=provider,
        model=model,
        base_url=base_url,
    ).run_plan_file(
        plan,
        mode=mode,
        session=session,
        from_step=from_step,
        environ=environ,
    )
