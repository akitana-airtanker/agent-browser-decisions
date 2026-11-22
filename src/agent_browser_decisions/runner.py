"""Plan execution orchestrator enforcing formally checked session, profile, retry, and verify invariants."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from agent_browser_decisions.browser import AgentBrowser, quiet_profile
from agent_browser_decisions.core import (
    DEFAULT_PROVIDER,
    append_batch_tail,
    apply_plan_vars,
    build_batch_commands,
    chosen_ref,
    collect_redaction_secrets,
    command_for,
    compatible,
    decision_request,
    document,
    evaluate_verify_output,
    extract_batch_tail,
    format_stopped_snapshot,
    hash_route,
    match_label,
    redact_text,
    redact_value,
    resolve_step_settle_s,
    resolve_step_values,
    same_field_value,
    select_tier1_batch,
    should_retry_step,
    url_matches_expected,
    validate_plan,
    validate_run_options,
    verify_batch_results,
    wrap_verify_with_vars,
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


def _remove_owned_profile(profile: str | None, created_profile: str | None) -> str | None:
    """Remove only the dedicated temp profile directory created at startup for this run."""
    if not profile:
        return None
    if profile != created_profile or os.path.islink(profile) or not os.path.isdir(profile):
        return profile
    try:
        shutil.rmtree(profile)
    except OSError:
        return profile
    return None


def poll(predicate: Callable[[], bool], wait_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.perf_counter() + wait_s
    while True:
        if predicate():
            return True
        if time.perf_counter() >= deadline:
            return False
        time.sleep(interval_s)


class _SnapshotTracker:
    """Tracks the latest observed raw snapshot and whether browser actions ran after it."""

    def __init__(self) -> None:
        self.raw: str | None = None
        self.stale: bool = False

    def record(self, page: dict[str, Any] | None) -> None:
        if page is not None and isinstance(page.get("raw"), str):
            self.raw = page["raw"]
            self.stale = False

    def mark_stale(self) -> None:
        self.stale = True


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
        allow_decisions: bool | None = None,
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
        self.allow_decisions = allow_decisions
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
        *,
        allow_decisions: bool = False,
        settle_s: float | None = None,
        initial_page: dict[str, Any] | None = None,
        snapshot_tracker: _SnapshotTracker | None = None,
        secrets: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[dict[str, Any] | None, int, dict[str, Any], dict[str, Any]]:
        if mode == "decide" and not allow_decisions:
            page = initial_page if initial_page is not None else browser.observe()
            if snapshot_tracker is not None:
                snapshot_tracker.record(page)
            return None, 3, {"reason": "Tier 2 decisions disabled (pass --decisions to enable)"}, page

        effective_settle = self.settle_s if settle_s is None else settle_s
        deadline = time.perf_counter() + effective_settle
        previous: str | None = initial_page["raw"] if initial_page is not None else None
        last_status: str | None = None
        while True:
            page = browser.observe()
            if snapshot_tracker is not None:
                snapshot_tracker.record(page)
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
                last_status = status
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
        if not allow_decisions:
            reason = (
                "ambiguous label match (Tier 2 disabled; pass --decisions to enable)"
                if last_status == "ambiguous"
                else "no label match (Tier 2 disabled; pass --decisions to enable)"
            )
            return None, 3, {"reason": reason}, page

        provider = getattr(decisions, "provider", DEFAULT_PROVIDER)
        model = getattr(decisions, "model", None)
        response = decisions.decide(
            decision_request(
                step,
                goal,
                candidates,
                page["url"],
                model=model,
                provider=provider,
                secrets=secrets,
            )
        )
        ref, info = chosen_ref(response)
        if ref:
            chosen = next((e for e in candidates if e["ref"] == ref), None)
            if chosen is not None:
                return chosen, 2, {"confidence": info}, page
            return None, 3, {"reason": f"decision unseen ref {ref}"}, page
        return None, 3, {"reason": f"decision {info}"}, page

    def _changed_since(
        self,
        browser: AgentBrowser,
        page: dict[str, Any],
        observed_out: list[dict[str, Any]] | None = None,
        snapshot_tracker: _SnapshotTracker | None = None,
    ) -> Callable[[], bool]:
        def check() -> bool:
            now = browser.observe(category="check")
            if snapshot_tracker is not None:
                snapshot_tracker.record(now)
            if now["raw"] != page["raw"] or now["url"] != page["url"]:
                if observed_out is not None:
                    observed_out.append(now)
                return True
            return False

        return check

    def _effect(
        self,
        browser: AgentBrowser,
        step: dict[str, Any],
        element: dict[str, Any],
        page: dict[str, Any],
        *,
        allow_keyboard: bool = True,
        snapshot_tracker: _SnapshotTracker | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        action, ref = step["action"], f"@{element['ref']}"
        if action == "fill":
            expected = step["value"]

            def value_matches() -> bool:
                got = browser.call("get", "value", ref, check=False, category="check")
                return same_field_value(got, expected)

            return ("ok" if poll(value_matches, min(0.5, self.effect_wait_s), self.poll_s) else "no_effect"), None
        if action == "check":
            checked = poll(
                lambda: browser.call("is", "checked", ref, check=False, category="check").strip() == "true",
                min(0.5, self.effect_wait_s),
                self.poll_s,
            )
            return ("ok" if checked else "no_effect"), None
        if action == "select":
            return "ok", None
        if step.get("expect") == "navigation" or (element["role"] == "link" and step.get("navigates", True)):
            whole = element["role"] == "link" and hash_route(
                browser.call("get", "attr", ref, "href", check=False, category="check")
            )

            def moved() -> bool:
                now = browser.current_url()
                if not now:
                    return False
                return now != page["url"] if whole else document(now) != document(page["url"])

            if poll(moved, 1.5, 0.1):
                return "ok", None
            if allow_keyboard and element["role"] == "link":
                # A wrapped inline link can have its box centre between lines; keyboard activation is geometry-free.
                browser.call("focus", ref, check=False, category="act")
                browser.call("press", "Enter", check=False, category="act")
                if poll(moved, 3.0, 0.1):
                    return "ok_keyboard", None
            return "wrong_target", None
        observed: list[dict[str, Any]] = []
        changed = poll(
            self._changed_since(browser, page, observed, snapshot_tracker=snapshot_tracker),
            self.effect_wait_s,
            self.poll_s,
        )
        return ("ok" if changed else "no_effect"), (observed[-1] if observed else None)

    def _wait_stable_page(
        self,
        browser: AgentBrowser,
        *,
        settle_s: float | None = None,
        initial_page: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_settle = self.settle_s if settle_s is None else settle_s
        deadline = time.perf_counter() + effective_settle
        previous: str | None = initial_page["raw"] if initial_page is not None else None
        while True:
            page = browser.observe()
            if (previous is not None and page["raw"] == previous) or time.perf_counter() >= deadline:
                return page
            previous = page["raw"]
            time.sleep(self.poll_s)

    def _try_tier1_batch(
        self,
        browser: AgentBrowser,
        metrics: Metrics,
        start_index: int,
        remaining_steps: list[dict[str, Any]],
        log: list[dict[str, Any]],
        *,
        verify_expr: str | None = None,
        settle_s: float | None = None,
        initial_page: dict[str, Any] | None = None,
        snapshot_tracker: _SnapshotTracker | None = None,
        secrets: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[int, tuple[bool, Any] | None, str | None]:
        if len(remaining_steps) < 2 or not hasattr(browser, "batch"):
            return 0, None, None
        if (
            remaining_steps[0].get("action") not in ("fill", "check", "select")
            or remaining_steps[1].get("action") not in ("fill", "check", "select")
            or remaining_steps[0].get("decide") is True
            or remaining_steps[1].get("decide") is True
        ):
            return 0, None, None
        started = time.perf_counter()
        metrics.reset()
        page = self._wait_stable_page(browser, settle_s=settle_s, initial_page=initial_page)
        if snapshot_tracker is not None:
            snapshot_tracker.record(page)
        selected = select_tier1_batch(remaining_steps, page["elements"])
        if len(selected) < 2:
            return 0, None, None
        commands, check_map = build_batch_commands(selected)
        covers_tail = len(selected) == len(remaining_steps)
        verify_idx: int | None = None
        url_idx: int | None = None
        if covers_tail:
            commands, verify_idx, url_idx = append_batch_tail(
                commands,
                verify_expr=verify_expr,
                include_url=True,
            )
        if snapshot_tracker is not None:
            snapshot_tracker.mark_stale()
        try:
            rows = browser.batch(commands, bail=True, category="act")
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            return 0, None, None
        verified_count = verify_batch_results(selected, rows, check_map)
        if verified_count <= 0:
            return 0, None, None

        fused_verified: tuple[bool, Any] | None = None
        fused_url: str | None = None
        # Backward simulation guard: only consume fused tail outputs if every batched step & effect check passed.
        if covers_tail and verified_count == len(selected):
            v_pass, v_val, tail_url = extract_batch_tail(rows, verify_idx, url_idx)
            if v_pass is True:
                fused_verified = (True, v_val)
            if tail_url:
                fused_url = tail_url

        total_ms = round((time.perf_counter() - started) * 1000)
        per_step_ms = max(1, round(total_ms / verified_count))
        for i in range(verified_count):
            step, el = selected[i]
            log.append(
                {
                    "step": start_index + i,
                    "tier": 1,
                    "tiers": [1],
                    "target": step["target"],
                    "url": page["url"],
                    "ok": True,
                    "chose": redact_text(el["name"], secrets)[:80],
                    "effect": "ok",
                    "batched": True,
                    "ms": per_step_ms,
                    "metrics": metrics.summary() if i == 0 else {},
                }
            )
        return verified_count, fused_verified, fused_url

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
        *,
        allow_decisions: bool = False,
        settle_s: float | None = None,
        initial_page: dict[str, Any] | None = None,
        snapshot_tracker: _SnapshotTracker | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        started = time.perf_counter()
        metrics.reset()
        excluded: set[str] = set()
        tiers: list[int] = []
        repeats = 0
        once = bool(step.get("once"))
        allow_retry = should_retry_step(step)

        if step["action"] == "press":
            page = initial_page if initial_page is not None else browser.observe()
            if snapshot_tracker is not None:
                snapshot_tracker.record(page)
                snapshot_tracker.mark_stale()
            browser.call("press", step["value"], category="act")
            if step.get("expect") == "navigation":

                def moved() -> bool:
                    now = browser.current_url()
                    return bool(now) and document(now) != document(page["url"])

                changed = poll(moved, 1.5, 0.1)
                outcome = "ok" if changed else "wrong_target"
                step_ok = outcome == "ok"
                next_press_page: dict[str, Any] | None = None
            else:
                observed: list[dict[str, Any]] = []
                changed = poll(
                    self._changed_since(browser, page, observed, snapshot_tracker=snapshot_tracker),
                    self.effect_wait_s,
                    self.poll_s,
                )
                outcome = "ok" if changed else "no_effect"
                step_ok = outcome != "no_effect"
                next_press_page = observed[-1] if observed else None
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
            return step_ok, next_press_page

        last_attempt: dict[str, Any] = {}
        next_page: dict[str, Any] | None = None
        seed_page = initial_page
        while True:
            step_mode = "decide" if step.get("decide") else mode
            element, tier, info, page = self._resolve(
                browser,
                decisions,
                step,
                goal,
                step_mode,
                excluded,
                allow_decisions=allow_decisions,
                settle_s=settle_s,
                initial_page=seed_page,
                snapshot_tracker=snapshot_tracker,
                secrets=secrets,
            )
            seed_page = None
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
                return False, None
            if snapshot_tracker is not None:
                snapshot_tracker.mark_stale()
            try:
                browser.call(*command_for(step, element["ref"]), category="act")
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
                log.append(
                    entry
                    | {
                        "ok": False,
                        "chose": redact_text(element["name"], secrets)[:80],
                        "error": redact_text(str(error), secrets)[:200],
                        "ms": round((time.perf_counter() - started) * 1000),
                        "metrics": metrics.summary(),
                    }
                )
                return False, None

            allow_kb = allow_retry and repeats == 0
            outcome, next_page = self._effect(
                browser, step, element, page, allow_keyboard=allow_kb, snapshot_tracker=snapshot_tracker
            )
            last_attempt = {"chose": redact_text(element["name"], secrets)[:80], "effect": outcome}
            if once:
                break
            if allow_retry and outcome == "no_effect" and repeats < 1:
                repeats += 1
                continue
            if (
                allow_retry
                and outcome == "wrong_target"
                and (tier == 1 or step.get("expect") == "navigation")
                and element["role"] != "link"
                and repeats < 1
            ):
                repeats += 1
                continue
            if outcome == "wrong_target" and tier == 2 and len(excluded) < self.retries:
                excluded.add(element["ref"])
                continue
            break

        step_ok = outcome not in ("no_effect", "wrong_target")
        entry |= {
            "ok": step_ok,
            "chose": redact_text(element["name"], secrets)[:80],
            "effect": outcome,
            "excluded": len(excluded),
            "repeats": repeats,
            "ms": round((time.perf_counter() - started) * 1000),
            "metrics": metrics.summary(),
        }
        log.append(entry)
        return step_ok, next_page

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
        vars: Mapping[str, str] | None = None,
        no_open: bool = False,
        reload_page: bool = False,
        keep_open: bool = False,
        close_on_success: bool = False,
        allow_decisions: bool | None = None,
        settle_s: float | None = None,
        expect_url: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate, resolve vars/env secrets, execute steps, verify, and enforce session/profile cleanup invariants."""
        env_map = os.environ if environ is None else environ
        explicit_flag = allow_decisions if allow_decisions is not None else self.allow_decisions
        if explicit_flag is not None:
            use_decisions = bool(explicit_flag)
        else:
            use_decisions = bool(mode == "decide" or self.provider or self.model or self.base_url)
        effective_provider = self.provider if self.provider is not None else env_map.get("DECISION_PROVIDER")
        merged_vars: dict[str, str] = {}
        var_problems: list[str] = []
        effective_plan = plan
        if isinstance(plan, dict) and (plan.get("vars") is not None or vars):
            effective_plan, merged_vars, var_problems = apply_plan_vars(plan, vars)
        problems = (
            validate_plan(effective_plan)
            + var_problems
            + validate_run_options(
                effective_plan,
                session=session,
                from_step=from_step,
                provider=effective_provider,
                no_open=no_open,
                reload_page=reload_page,
                keep_open=keep_open,
                close_on_success=close_on_success,
                expect_url=expect_url,
            )
        )
        if problems:
            return {
                "ok": False,
                "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
                "session_open": bool(session),
                "verified": None,
                "problems": problems,
            }
        plan = effective_plan
        resolved_steps, secrets, env_problems = resolve_step_values(
            plan["steps"][from_step - 1 :],
            env_map,
            start_index=from_step,
        )
        if from_step > 1:
            secrets |= collect_redaction_secrets(plan["steps"][: from_step - 1], env_map)
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
        created_profile: str | None = None
        if owns_session:
            session = f"abd-{int(time.time() * 1000)}-{os.getpid()}"
            profile = self._profile_factory()
            created_profile = profile

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
        snapshot_tracker = _SnapshotTracker()
        expect_url_failed = False
        completed = False
        error: str | None = None
        verified: bool | None = None
        verify_value: Any = None
        fused_url: str | None = None
        closed = False
        open_attempted = False

        try:
            with decisions:
                if plan.get("url") and from_step == 1 and not no_open:
                    open_attempted = True
                    try:
                        browser.open(plan["url"], profile=profile)
                        session_opened = True
                    except (RuntimeError, subprocess.TimeoutExpired, OSError) as failure:
                        error = redact_text(str(failure), secrets)[:300]
                        if owns_session:
                            try:
                                close_res = browser.close()
                                closed = close_res is not False
                            except (RuntimeError, subprocess.TimeoutExpired, OSError):
                                closed = False
                            session_opened = not closed
                        if profile and not session_opened:
                            profile = _remove_owned_profile(profile, created_profile)
                        res_fail: dict[str, Any] = {
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
                        if profile:
                            res_fail["profile"] = profile
                        return res_fail

                verify_expr = wrap_verify_with_vars(plan["verify"], merged_vars) if plan.get("verify") else None
                if expect_url:
                    try:
                        current = browser.current_url()
                    except (RuntimeError, subprocess.TimeoutExpired, OSError) as failure:
                        current = ""
                        error = redact_text(f"expect_url check failed: {failure}", secrets)[:300]
                        expect_url_failed = True
                    else:
                        if not url_matches_expected(current, expect_url):
                            error = redact_text(
                                f"expect_url mismatch: expected {expect_url!r}, got {current!r}",
                                secrets,
                            )[:300]
                            expect_url_failed = True
                completed = not expect_url_failed
                carried_page: dict[str, Any] | None = None
                fused_verified: tuple[bool, Any] | None = None
                fused_url: str | None = None
                offset = 0
                while completed and offset < len(resolved_steps):
                    index = from_step + offset
                    step = resolved_steps[offset]
                    try:
                        if mode == "label":
                            batched, batch_verified, batch_url = self._try_tier1_batch(
                                browser,
                                metrics,
                                index,
                                resolved_steps[offset:],
                                log,
                                verify_expr=verify_expr,
                                settle_s=resolve_step_settle_s(
                                    step,
                                    plan_settle_s=plan.get("settle_s"),
                                    cli_settle_s=settle_s,
                                    default=self.settle_s,
                                ),
                                initial_page=carried_page,
                                snapshot_tracker=snapshot_tracker,
                                secrets=secrets,
                            )
                            if batched > 0:
                                carried_page = None
                                fused_verified = batch_verified
                                fused_url = batch_url
                                offset += batched
                                continue
                        fused_verified = None
                        fused_url = None
                        step_ok, carried_page = self._run_step(
                            browser,
                            decisions,
                            metrics,
                            index,
                            step,
                            plan.get("goal", ""),
                            mode,
                            secrets,
                            log,
                            allow_decisions=use_decisions,
                            settle_s=resolve_step_settle_s(
                                step,
                                plan_settle_s=plan.get("settle_s"),
                                cli_settle_s=settle_s,
                                default=self.settle_s,
                            ),
                            initial_page=carried_page,
                            snapshot_tracker=snapshot_tracker,
                        )
                        if not step_ok:
                            completed = False
                            break
                    except Exception as failure:
                        httpx_mod = sys.modules.get("httpx")
                        expected_errors: tuple[type[BaseException], ...] = (
                            MissingKey,
                            DecisionsError,
                            RuntimeError,
                            json.JSONDecodeError,
                            subprocess.TimeoutExpired,
                            OSError,
                        ) + ((httpx_mod.HTTPError,) if httpx_mod is not None else ())
                        if not isinstance(failure, expected_errors):
                            raise
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
                    offset += 1

                if completed and verify_expr:
                    if fused_verified is not None:
                        verified, verify_value = fused_verified
                    else:
                        fused_url = None
                        verified, verify_value = self._verify_page(
                            browser, verify_expr, plan.get("verify_timeout_s", 10), secrets
                        )
                    if verified is False:
                        snapshot_tracker.mark_stale()
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
                if ok and fused_url:
                    url = fused_url
                else:
                    try:
                        url = browser.current_url()
                    except (RuntimeError, subprocess.TimeoutExpired, OSError):
                        url = ""

            # Close on success unless --keep-open was requested; always close owned sessions on interrupt.
            should_close_on_ok = (owns_session and not keep_open) or close_on_success
            should_close_on_interrupt = owns_session or close_on_success
            has_live_session = session_opened or (interrupted and owns_session and open_attempted)
            if ((ok and should_close_on_ok) or (interrupted and should_close_on_interrupt)) and has_live_session:
                try:
                    close_res = browser.close()
                    closed = close_res is not False
                except (RuntimeError, subprocess.TimeoutExpired, OSError):
                    closed = False
            session_open = has_live_session and not closed
            if not ok and session_open and not interrupted and not expect_url_failed and snapshot_tracker.stale:
                with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                    snapshot_tracker.record(browser.observe(category="observe"))
            if profile and not session_open:
                profile = _remove_owned_profile(profile, created_profile)

        stopped = next((e["step"] for e in log if not e.get("ok")), None)
        if stopped is None and not completed and error and error.startswith("expect_url "):
            stopped = from_step
        result: dict[str, Any] = {
            "ok": ok,
            "session": session or env_map.get("AGENT_BROWSER_SESSION", "default"),
            "session_open": session_open,
            "verified": verified,
            "verify_value": redact_value(verify_value, secrets),
            "stopped_at_step": stopped,
            "error": error,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "url": redact_text(url, secrets) if isinstance(url, str) else url,
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
                        "batched",
                        "ms",
                    )
                    if e.get(k) is not None or k in ("step", "tier", "target", "chose", "effect", "ms")
                }
                for e in log
            ],
        }
        if profile:
            result["profile"] = profile
        if not ok and session_open and snapshot_tracker.raw:
            stopped_snap = format_stopped_snapshot(snapshot_tracker.raw, secrets)
            if stopped_snap:
                result["stopped_snapshot"] = stopped_snap
        return result


def run_plan_file(
    plan: Any,
    mode: str = "label",
    session: str | None = None,
    from_step: int = 1,
    *,
    vars: Mapping[str, str] | None = None,
    no_open: bool = False,
    reload_page: bool = False,
    keep_open: bool = False,
    close_on_success: bool = False,
    browser_timeout_s: float = 60.0,
    api_timeout_s: float = 30.0,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    allow_decisions: bool | None = None,
    settle_s: float | None = None,
    expect_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convenience entry point using the default PlanRunner."""
    return PlanRunner(
        browser_timeout_s=browser_timeout_s,
        api_timeout_s=api_timeout_s,
        provider=provider,
        model=model,
        base_url=base_url,
        allow_decisions=allow_decisions,
    ).run_plan_file(
        plan,
        mode=mode,
        session=session,
        from_step=from_step,
        vars=vars,
        no_open=no_open,
        reload_page=reload_page,
        keep_open=keep_open,
        close_on_success=close_on_success,
        allow_decisions=allow_decisions,
        settle_s=settle_s,
        expect_url=expect_url,
        environ=environ,
    )
