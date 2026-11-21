"""Stateful lifecycle invariants (TLA+ parity), adapter resilience, secret redaction, and CLI tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule

from agent_browser_decisions import __version__
from agent_browser_decisions.browser import AgentBrowser, quiet_profile
from agent_browser_decisions.cli import load_dotenv_files, main
from agent_browser_decisions.core import parse_snapshot
from agent_browser_decisions.decisions import DecisionsClient, DecisionsError, MissingKey
from agent_browser_decisions.runner import PlanRunner


class FakeBrowser(AgentBrowser):
    """Deterministic in-memory browser adapter for lifecycle, secret, and interrupt tests."""

    def __init__(
        self,
        session: str | None = None,
        *,
        open_ok: bool = True,
        effect_ok: bool = True,
        verify_out: str = "true",
        fail_fill_with_secret_echo: bool = False,
        interrupt_on_step: bool = False,
        secrets: set[str] | frozenset[str] = frozenset(),
        **kwargs: Any,
    ) -> None:
        super().__init__(session, secrets=secrets, **kwargs)
        self.open_ok = open_ok
        self.effect_ok = effect_ok
        self.verify_out = verify_out
        self.fail_fill_with_secret_echo = fail_fill_with_secret_echo
        self.interrupt_on_step = interrupt_on_step
        self.url = "https://example.test/"
        self.snap = '- button "Go" [ref=e1]\n- textbox "Password" [ref=e2]'
        self.closed_sessions: list[str | None] = []
        self.act_calls: list[tuple[str, ...]] = []

    def call(self, *args: str, check: bool = True, category: str = "browser") -> str:
        cmd = args[0] if args else ""
        if cmd == "--profile":
            if not self.open_ok:
                raise RuntimeError("open failed")
            self.url = args[-1]
            return ""
        if cmd == "open":
            if not self.open_ok:
                raise RuntimeError("open failed")
            self.url = args[-1]
            return ""
        if cmd == "snapshot":
            return json.dumps({"success": True, "data": {"origin": self.url, "snapshot": self.snap}})
        if cmd in ("click", "fill", "press"):
            self.act_calls.append(args)
            if self.interrupt_on_step:
                raise KeyboardInterrupt("simulated SIGINT")
            if cmd == "fill" and self.fail_fill_with_secret_echo:
                from agent_browser_decisions.core import redact_text

                raw_err = f"agent-browser fill @e2 failed: bad value {args[2]}"
                raise RuntimeError(redact_text(raw_err, self.secrets))
            if self.effect_ok:
                self.snap = '- button "Done" [ref=e2]'
            return ""
        if cmd == "get" and len(args) >= 2 and args[1] == "url":
            return self.url
        if cmd == "get" and len(args) >= 2 and args[1] == "value":
            return (self.act_calls[-1][2] + "\n") if (self.act_calls and self.effect_ok) else "\n"
        if cmd == "eval":
            return self.verify_out
        if cmd == "close":
            self.closed_sessions.append(self.session)
            return ""
        return ""

    def observe(self, category: str = "observe") -> dict[str, Any]:
        payload = json.loads(self.call("snapshot", "-i", "--json", category=category))
        raw = payload["data"]["snapshot"]
        return {"url": payload["data"]["origin"], "raw": raw, "elements": parse_snapshot(raw)}


# ---------- Stateful TLA+ parity machine ----------


class PlanLifecycleMachine(RuleBasedStateMachine):
    """Drives real PlanRunner across all reset/session/url/open/once/effect/verify combinations."""

    @rule(
        reset=st.booleans(),
        caller_session=st.booleans(),
        has_url=st.booleans(),
        open_ok=st.booleans(),
        once=st.booleans(),
        effect_ok=st.booleans(),
        verify_mode=st.sampled_from(["none", "true", "false", "syntax_error"]),
    )
    def run_plan(
        self,
        reset: bool,
        caller_session: bool,
        has_url: bool,
        open_ok: bool,
        once: bool,
        effect_ok: bool,
        verify_mode: str,
    ) -> None:
        created_profiles: list[str] = []
        created_browsers: list[FakeBrowser] = []

        def make_profile() -> str:
            d = tempfile.mkdtemp(prefix="abd-test-prof-")
            created_profiles.append(d)
            return d

        verify_stdout = {"none": "true", "true": "true", "false": "false", "syntax_error": "SyntaxError: bad"}[
            verify_mode
        ]

        def make_browser(session: str | None = None, **kwargs: Any) -> FakeBrowser:
            fb = FakeBrowser(session, open_ok=open_ok, effect_ok=effect_ok, verify_out=verify_stdout, **kwargs)
            created_browsers.append(fb)
            return fb

        plan: dict[str, Any] = {
            "reset": reset,
            "steps": [{"action": "click", "target": "Go", "once": once}],
        }
        if has_url:
            plan["url"] = "https://example.test/"
        if verify_mode != "none":
            plan["verify"] = "true"
            plan["verify_timeout_s"] = 0.02

        runner = PlanRunner(
            browser_factory=make_browser,
            profile_factory=make_profile,
            settle_s=0.05,
            poll_s=0.005,
            verify_poll_s=0.005,
            effect_wait_s=0.02,
        )
        session_arg = "caller-sess" if caller_session else None
        result = runner.run_plan_file(plan, session=session_arg)

        # Invariant 1 (CallerSessionNeverClosed)
        for fb in created_browsers:
            assert "caller-sess" not in fb.closed_sessions

        # Invariant 2 (NoUnattachedProfileLeak)
        for prof in created_profiles:
            if not result["session_open"]:
                assert not os.path.exists(prof), f"leaked profile dir {prof}"
            else:
                os.rmdir(prof)

        # Invariant 3 (ReportedSessionOpenAccurate)
        if not caller_session and (not has_url or not open_ok):
            assert result["session_open"] is False

        # Invariant 4 (NoSilentSuccessOnFailedEffect)
        if not effect_ok:
            assert result["ok"] is False
        if verify_mode in ("false", "syntax_error"):
            assert result["ok"] is False

        # Invariant 5 (OnceStepExecutesAtMostOnce)
        if once:
            total_clicks = sum(len(fb.act_calls) for fb in created_browsers)
            assert total_clicks <= 1


TestPlanLifecycle = PlanLifecycleMachine.TestCase
TestPlanLifecycle.settings = settings(max_examples=40, stateful_step_count=4, deadline=None)


# ---------- DecisionsClient transient retries & secret safety ----------


def test_decisions_client_retries_429_and_503_then_succeeds() -> None:
    calls = 0
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, text="rate limited")
        if calls == 2:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            json={
                "decisions": [{"id": "element", "status": "answered", "choice": "e1", "confidence": 0.99}],
                "usage": {"input_tokens": 42},
            },
        )

    transport = httpx.MockTransport(handler)
    with DecisionsClient(
        api_key="sk-secret-do-not-leak",
        client=httpx.Client(transport=transport),
        sleep=slept.append,
        max_retries=2,
        base_backoff_s=0.1,
    ) as client:
        body = client.decide({"context": "x", "questions": []})

    assert calls == 3
    assert slept == [0.25, 0.2]
    assert body["decisions"][0]["choice"] == "e1"


def test_decisions_client_never_retries_401_and_never_leaks_api_key() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="invalid api key")

    with (
        DecisionsClient(
            api_key="sk-super-secret-value-999",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
            max_retries=2,
        ) as client,
        pytest.raises(DecisionsError) as exc_info,
    ):
        client.decide({"context": "x", "questions": []})

    assert calls == 1
    assert "401" in str(exc_info.value)
    assert "sk-super-secret-value-999" not in str(exc_info.value)


def test_decisions_client_retries_network_timeout_and_raises_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with DecisionsClient(sleep=lambda _: None) as client, pytest.raises(MissingKey):
        client.decide({"context": "x", "questions": []})

    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out")

    with (
        DecisionsClient(
            api_key="sk-test",
            client=httpx.Client(transport=httpx.MockTransport(flaky)),
            sleep=lambda _: None,
            max_retries=2,
        ) as client,
        pytest.raises(DecisionsError),
    ):
        client.decide({"context": "x", "questions": []})
    assert attempts == 3


# ---------- Secret hygiene (value_env) & SIGINT cleanup ----------


def test_value_env_fails_fast_without_browser_and_redacts_errors() -> None:
    browsers: list[FakeBrowser] = []

    def factory(session: str | None = None, **kwargs: Any) -> FakeBrowser:
        fb = FakeBrowser(session, fail_fill_with_secret_echo=True, **kwargs)
        browsers.append(fb)
        return fb

    plan = {
        "url": "https://example.test/login",
        "steps": [{"action": "fill", "target": "Password", "value_env": "MY_SECRET_PW"}],
    }
    runner = PlanRunner(browser_factory=factory, settle_s=0.05, poll_s=0.005)

    # Missing env var -> fails fast with problems, zero browser created
    res_missing = runner.run_plan_file(plan, environ={})
    assert res_missing["ok"] is False and res_missing["problems"]
    assert browsers == []

    # Present env var -> secret is passed to fill, and scrubbed from error output
    res_err = runner.run_plan_file(plan, environ={"MY_SECRET_PW": "TopSecret!2026"})
    assert res_err["ok"] is False
    assert browsers[0].act_calls[0] == ("fill", "@e2", "TopSecret!2026")
    serialized = json.dumps(res_err)
    assert "TopSecret!2026" not in serialized
    assert "[REDACTED]" in serialized


def test_sigint_mid_run_closes_owned_session_and_cleans_profile() -> None:
    created_profiles: list[str] = []
    created_browsers: list[FakeBrowser] = []

    def make_profile() -> str:
        d = tempfile.mkdtemp(prefix="abd-int-prof-")
        created_profiles.append(d)
        return d

    def make_browser(session: str | None = None, **kwargs: Any) -> FakeBrowser:
        fb = FakeBrowser(session, interrupt_on_step=True, **kwargs)
        created_browsers.append(fb)
        return fb

    plan = {
        "url": "https://example.test/",
        "reset": True,
        "steps": [{"action": "click", "target": "Go"}],
    }
    runner = PlanRunner(browser_factory=make_browser, profile_factory=make_profile, settle_s=0.05, poll_s=0.005)
    result = runner.run_plan_file(plan)
    assert result["ok"] is False
    assert result["error"] == "interrupted"
    assert result["session_open"] is False
    assert len(created_browsers[0].closed_sessions) == 1
    assert not os.path.exists(created_profiles[0])


# ---------- CLI & dotenv ----------


def test_load_dotenv_files_never_clobbers_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-from-file\nDECISION_MODEL='gpt-from-file'\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    monkeypatch.delenv("DECISION_MODEL", raising=False)
    loaded = load_dotenv_files(str(env_file))
    assert loaded == env_file
    assert os.environ["OPENAI_API_KEY"] == "sk-live"
    assert os.environ["DECISION_MODEL"] == "gpt-from-file"


def test_cli_validate_doctor_profile_and_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --version
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out

    # validate valid plan -> rc 0
    good_plan = tmp_path / "good.json"
    good_plan.write_text(json.dumps({"steps": [{"action": "click", "target": "Go"}]}), encoding="utf-8")
    assert main(["validate", str(good_plan)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # validate bad plan -> rc 2
    bad_plan = tmp_path / "bad.json"
    bad_plan.write_text(json.dumps({"reset": True, "steps": [{"action": "click", "target": "Go"}]}), encoding="utf-8")
    assert main(["validate", str(bad_plan)]) == 2
    assert json.loads(capsys.readouterr().out)["problems"]

    # run invalid option combo -> rc 2 (not rc 1)
    combo_plan = tmp_path / "combo.json"
    combo_plan.write_text(
        json.dumps({"reset": True, "url": "https://x.test", "steps": [{"action": "click", "target": "Go"}]}),
        encoding="utf-8",
    )
    assert main(["run", "--session", "existing", str(combo_plan)]) == 2
    assert json.loads(capsys.readouterr().out)["problems"]

    # doctor reports JSON cleanly
    monkeypatch.setenv("OPENAI_API_KEY", "sk-doctor-test")
    rc = main(["doctor"])
    doc = json.loads(capsys.readouterr().out)
    assert rc in (0, 1)
    assert doc["version"] == __version__
    assert doc["decisions_api"]["api_key_set"] is True
    assert "sk-doctor-test" not in json.dumps(doc)

    # profile creates quiet Chrome preferences
    assert main(["profile"]) == 0
    prof_dir = Path(capsys.readouterr().out.strip())
    prefs = json.loads((prof_dir / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False
    # clean up
    import shutil

    shutil.rmtree(prof_dir)
    p2 = Path(quiet_profile())
    assert (p2 / "Default" / "Preferences").is_file()
    shutil.rmtree(p2)


def test_agent_browser_missing_binary_gives_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def raise_fnf(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("agent-browser")

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    browser = AgentBrowser()
    with pytest.raises(RuntimeError, match="agent-browser CLI not found on PATH"):
        browser.call("snapshot")


def test_cli_non_utf8_plan_returns_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary_file = tmp_path / "binary.json"
    binary_file.write_bytes(b"\xff\xfe\x00invalid")
    assert main(["validate", str(binary_file)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["problems"]


# ---------- Settle-loop resolution invariants ----------


class SequenceBrowser(FakeBrowser):
    """FakeBrowser that replays a scripted sequence of snapshots across observe() calls."""

    def __init__(self, snapshots: list[str], session: str | None = None, **kwargs: Any) -> None:
        super().__init__(session, **kwargs)
        self._snapshots = snapshots
        self._idx = 0
        self.snap = snapshots[-1]

    def observe(self, category: str = "observe") -> dict[str, Any]:
        if category == "observe":
            raw = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
            self._idx += 1
        else:
            raw = self.snap
        return {"url": self.url, "raw": raw, "elements": parse_snapshot(raw)}


class FailingDecisionsClient:
    """Stub DecisionsClient that fails the test if Tier 2 is unexpectedly invoked."""

    def __init__(self, **_kwargs: Any) -> None:
        self.calls = 0

    def __enter__(self) -> FailingDecisionsClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError(f"Tier 2 Decisions API should not be called: {payload}")


def test_resolve_label_waits_for_delayed_dynamic_target() -> None:
    """In label mode, unrelated stable candidates must not abort waiting before settle_s."""
    snaps = [
        '- button "Cancel" [ref=e1]\n- button "Help" [ref=e2]',
        '- button "Cancel" [ref=e1]\n- button "Help" [ref=e2]',
        '- button "Cancel" [ref=e1]\n- button "Help" [ref=e2]\n- option "Zürich, Switzerland" [ref=e9]',
        '- button "Cancel" [ref=e1]\n- button "Help" [ref=e2]\n- option "Zürich, Switzerland" [ref=e9]',
    ]
    browsers: list[SequenceBrowser] = []
    decisions_clients: list[FailingDecisionsClient] = []

    def make_browser(session: str | None = None, **kwargs: Any) -> SequenceBrowser:
        b = SequenceBrowser(snaps, session=session, **kwargs)
        browsers.append(b)
        return b

    def make_decisions(**kwargs: Any) -> FailingDecisionsClient:
        d = FailingDecisionsClient(**kwargs)
        decisions_clients.append(d)
        return d

    runner = PlanRunner(
        browser_factory=make_browser,
        decisions_factory=make_decisions,
        settle_s=0.3,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    plan = {"steps": [{"action": "click", "target": "Zürich, Switzerland"}]}

    result = runner.run_plan_file(plan)
    assert result["ok"] is True
    assert result["steps"][0]["tier"] == 1
    assert all(d.calls == 0 for d in decisions_clients)
    assert browsers[0].act_calls == [("click", "@e9")]


def test_resolve_label_hits_on_settle_expiry_when_page_keeps_mutating() -> None:
    """A continuously animating page with a unique label hit should resolve at Tier 1 on expiry."""

    class TickingBrowser(FakeBrowser):
        def __init__(self, session: str | None = None, **kwargs: Any) -> None:
            super().__init__(session, **kwargs)
            self.ticks = 0

        def observe(self, category: str = "observe") -> dict[str, Any]:
            if category == "observe":
                self.ticks += 1
                raw = f'- button "Clock {self.ticks}" [ref=e1]\n- button "Search" [ref=e5]'
            else:
                raw = self.snap
            return {"url": self.url, "raw": raw, "elements": parse_snapshot(raw)}

    browsers: list[TickingBrowser] = []
    decisions_clients: list[FailingDecisionsClient] = []

    def make_browser(session: str | None = None, **kwargs: Any) -> TickingBrowser:
        b = TickingBrowser(session=session, **kwargs)
        browsers.append(b)
        return b

    def make_decisions(**kwargs: Any) -> FailingDecisionsClient:
        d = FailingDecisionsClient(**kwargs)
        decisions_clients.append(d)
        return d

    runner = PlanRunner(
        browser_factory=make_browser,
        decisions_factory=make_decisions,
        settle_s=0.04,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    plan = {"steps": [{"action": "click", "target": "Search"}]}

    result = runner.run_plan_file(plan)
    assert result["ok"] is True
    assert result["steps"][0]["tier"] == 1
    assert all(d.calls == 0 for d in decisions_clients)
    assert browsers[0].act_calls == [("click", "@e5")]


def test_resolve_nth_waits_for_kth_item_and_stops_at_tier_3_when_missing() -> None:
    """nth=k waits for lazy-loaded items up to settle_s, then stops at Tier 3 if still absent."""
    # Case A: 3rd result arrives after initial two snapshots are already stable
    delayed_snaps = [
        '- button "Result A" [ref=e11]\n- button "Result B" [ref=e12]',
        '- button "Result A" [ref=e11]\n- button "Result B" [ref=e12]',
        '- button "Result A" [ref=e11]\n- button "Result B" [ref=e12]\n- button "Result C" [ref=e13]',
        '- button "Result A" [ref=e11]\n- button "Result B" [ref=e12]\n- button "Result C" [ref=e13]',
    ]
    browsers_ok: list[SequenceBrowser] = []
    decisions_ok: list[FailingDecisionsClient] = []

    def make_browser_ok(session: str | None = None, **kwargs: Any) -> SequenceBrowser:
        b = SequenceBrowser(delayed_snaps, session=session, **kwargs)
        browsers_ok.append(b)
        return b

    def make_decisions_ok(**kwargs: Any) -> FailingDecisionsClient:
        d = FailingDecisionsClient(**kwargs)
        decisions_ok.append(d)
        return d

    runner = PlanRunner(
        browser_factory=make_browser_ok,
        decisions_factory=make_decisions_ok,
        settle_s=0.3,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    plan = {"steps": [{"action": "click", "target": "Result", "nth": 3}]}

    res_ok = runner.run_plan_file(plan)
    assert res_ok["ok"] is True
    assert res_ok["steps"][0]["tier"] == 1
    assert all(d.calls == 0 for d in decisions_ok)
    assert browsers_ok[0].act_calls == [("click", "@e13")]

    # Case B: 3rd result never appears -> stops at Tier 3 without invoking Decisions API
    short_snaps = ['- button "Result A" [ref=e11]\n- button "Result B" [ref=e12]']
    decisions_miss: list[FailingDecisionsClient] = []

    def make_browser_miss(session: str | None = None, **kwargs: Any) -> SequenceBrowser:
        return SequenceBrowser(short_snaps, session=session, **kwargs)

    def make_decisions_miss(**kwargs: Any) -> FailingDecisionsClient:
        d = FailingDecisionsClient(**kwargs)
        decisions_miss.append(d)
        return d

    runner_fast = PlanRunner(
        browser_factory=make_browser_miss,
        decisions_factory=make_decisions_miss,
        settle_s=0.04,
        poll_s=0.005,
        effect_wait_s=0.02,
    )

    res_miss = runner_fast.run_plan_file(plan)
    assert res_miss["ok"] is False
    assert res_miss["stopped_at_step"] == 1
    assert res_miss["steps"][0]["tier"] == 3
    assert all(d.calls == 0 for d in decisions_miss)
    assert "nth out of range" in res_miss["steps"][0]["reason"]


# ---------- Multi-provider HTTP adapter, PlanRunner, and CLI tests ----------


def test_decisions_client_supports_gemini_typesafe_liquid_and_custom_endpoint() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []
    recorded_tokens: list[int] = []
    ts_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ts_calls
        payload = json.loads(request.content.decode("utf-8"))
        seen.append(
            (
                str(request.url),
                request.headers.get("x-goog-api-key") or request.headers.get("authorization") or "",
                payload,
            )
        )

        if "generativelanguage.googleapis.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "answers": {"element": {"type": "choice", "choice": "e2", "confidence": 0.97}},
                    "usage": {"total_input_tokens": 181, "total_output_tokens": 0},
                },
            )
        if "api.typesafe.ai" in str(request.url):
            ts_calls += 1
            if ts_calls == 1:
                return httpx.Response(529, text="overloaded")
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.14.0",
                    "answers": {"element": {"type": "choice", "choice": "e1", "confidence": 0.94}},
                    "usage": {"input_tokens": 210, "output_tokens": 0},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "d1.2",
                "answers": {"element": {"type": "choice", "choice": "__none__", "confidence": 0.88}},
                "usage": {"input_tokens": 145, "output_tokens": 0},
            },
        )

    transport = httpx.MockTransport(handler)

    def metrics_cb(_cat: str, _sec: float, tok: int) -> None:
        recorded_tokens.append(tok)

    # 1) Gemini Decisions API
    with DecisionsClient(
        provider="gemini",
        api_key="gem-secret-key",
        environ={"DECISION_MODEL": "gpt-6-luna"},
        client=httpx.Client(transport=transport),
        on_metric=metrics_cb,
    ) as client:
        res_gem = client.decide({"input": "ctx", "questions": {"element": {"type": "choice"}}})
    assert res_gem["answers"]["element"]["choice"] == "e2"

    # 2) TypeSafe Jev with HTTP 529 retry
    with DecisionsClient(
        provider="typesafe",
        api_key="ts-secret-key",
        environ={},
        client=httpx.Client(transport=transport),
        sleep=lambda _: None,
        on_metric=metrics_cb,
    ) as client:
        res_ts = client.decide({"state": "ctx", "questions": {"element": {"type": "choice"}}})
    assert res_ts["answers"]["element"]["choice"] == "e1"

    # 3) Liquid d1.2 default endpoint
    with DecisionsClient(
        provider="liquid",
        api_key="liq-secret-key",
        environ={},
        client=httpx.Client(transport=transport),
        on_metric=metrics_cb,
    ) as client:
        res_liq = client.decide({"state": "ctx", "questions": {"element": {"type": "choice"}}})
    assert res_liq["answers"]["element"]["choice"] == "__none__"

    # 4) First-class OpenRouter Decisions API (auto-routed to /api/alpha/decisions)
    with DecisionsClient(
        provider="openrouter",
        api_key="or-secret-key",
        model="google/gemini-decision-1",
        environ={},
        client=httpx.Client(transport=transport),
        on_metric=metrics_cb,
    ) as client:
        client.decide({"state": "ctx", "questions": {"element": {"type": "choice"}}})

    assert seen[0][0] == "https://generativelanguage.googleapis.com/v1beta/decisions"
    assert seen[0][1] == "gem-secret-key"
    assert seen[0][2]["model"] == "gemini-decision-1"

    assert seen[1][0] == "https://api.typesafe.ai/v1/systemone"
    assert seen[1][1] == "Bearer ts-secret-key"
    assert ts_calls == 2

    assert seen[3][0] == "https://api.liquid.ai/decisions/v1/systemone"
    assert seen[3][1] == "Bearer liq-secret-key"
    assert seen[3][2]["model"] == "d1.2"

    assert seen[4][0] == "https://openrouter.ai/api/alpha/decisions"
    assert seen[4][1] == "Bearer or-secret-key"
    assert seen[4][2]["model"] == "google/gemini-decision-1"

    assert [t for t in recorded_tokens if t > 0] == [181, 210, 145, 145]


def test_decisions_client_gemini_401_never_leaks_api_key() -> None:
    secret = "AIzaSySecretKeyShouldNeverLeak12345"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"unauthorized for key {secret}")

    with (
        DecisionsClient(
            provider="gemini",
            api_key=secret,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
        ) as client,
        pytest.raises(DecisionsError) as exc_info,
    ):
        client.decide({"input": "x", "questions": {}})

    assert "401" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_plan_runner_end_to_end_with_gemini_and_liquid_abstention() -> None:
    ambiguous_snap = '- link "Learn more" [ref=e1]\n- link "Learn more" [ref=e2]'
    sent_payloads: list[dict[str, Any]] = []

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "answers": {"element": {"type": "choice", "choice": "e2", "confidence": 0.96}},
                "usage": {"total_input_tokens": 120},
            },
        )

    def liquid_abstain_handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "answers": {"element": {"type": "choice", "choice": "__none__", "confidence": 0.91}},
                "usage": {"input_tokens": 95},
            },
        )

    browsers_ok: list[SequenceBrowser] = []

    def make_browser_ok(session: str | None = None, **kwargs: Any) -> SequenceBrowser:
        b = SequenceBrowser([ambiguous_snap], session=session, **kwargs)
        browsers_ok.append(b)
        return b

    runner_gem = PlanRunner(
        browser_factory=make_browser_ok,
        decisions_factory=lambda **kw: DecisionsClient(
            client=httpx.Client(transport=httpx.MockTransport(gemini_handler)),
            **kw,
        ),
        provider="gemini",
        settle_s=0.05,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    res_gem = runner_gem.run_plan_file(
        {
            "goal": "Open the second Learn more link",
            "steps": [{"action": "click", "target": "Learn more", "role": "link", "navigates": False}],
            "verify": "true",
        },
        environ={"GEMINI_API_KEY": "gem-test"},
    )
    assert res_gem["ok"] is True
    assert res_gem["steps"][0]["tier"] == 2
    assert browsers_ok[0].act_calls == [("click", "@e2")]
    assert "input" in sent_payloads[0]
    assert "__none__" in sent_payloads[0]["questions"]["element"]["criteria"]

    runner_liq = PlanRunner(
        browser_factory=lambda session=None, **kw: SequenceBrowser([ambiguous_snap], session=session, **kw),
        decisions_factory=lambda **kw: DecisionsClient(
            client=httpx.Client(transport=httpx.MockTransport(liquid_abstain_handler)),
            **kw,
        ),
        provider="liquid",
        settle_s=0.05,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    res_liq = runner_liq.run_plan_file(
        {
            "goal": "Click the checkout button",
            "steps": [{"action": "click", "target": "Checkout", "role": "link", "navigates": False}],
        },
        session="existing-sess",
        environ={"LIQUID_API_KEY": "liq-test"},
    )
    assert res_liq["ok"] is False
    assert res_liq["stopped_at_step"] == 1
    assert res_liq["session_open"] is True
    assert res_liq["steps"][0]["tier"] == 3
    assert res_liq["steps"][0]["reason"] == "decision abstained"
    assert "state" in sent_payloads[1]


def test_cli_doctor_and_run_multi_provider_flags_and_invalid_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for var in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "TYPESAFE_API_KEY",
        "LIQUID_API_KEY",
        "DECISION_PROVIDER",
        "DECISION_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-cli-test")

    rc_doc = main(["doctor", "--env-file", str(tmp_path / "missing.env")])
    report = json.loads(capsys.readouterr().out)
    assert rc_doc in (0, 1)
    assert report["decisions_api"]["provider"] == "gemini"
    assert report["decisions_api"]["protocol"] == "gemini"
    assert report["decisions_api"]["model"] == "gemini-decision-1"
    assert report["decisions_api"]["api_key_set"] is True
    assert report["decisions_api"]["key_env"] == "GEMINI_API_KEY"
    assert report["decisions_api"]["available_providers"]["gemini"] is True
    assert report["decisions_api"]["available_providers"]["openai"] is False

    plan_path = tmp_path / "p.json"
    plan_path.write_text(json.dumps({"steps": [{"action": "click", "target": "Go"}]}), encoding="utf-8")
    monkeypatch.setenv("DECISION_PROVIDER", "bogus")
    rc_bad = main(["run", str(plan_path), "--env-file", str(tmp_path / "missing.env")])
    bad_out = json.loads(capsys.readouterr().out)
    assert rc_bad == 2
    assert any("unknown provider" in p for p in bad_out["problems"])
