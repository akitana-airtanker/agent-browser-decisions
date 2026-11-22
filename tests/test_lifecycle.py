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
    plan = {"steps": [{"action": "click", "target": "Result", "match": "prefix", "nth": 3}]}

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


class BatchFakeBrowser:
    """Fake browser supporting `batch()` to test Tier-1 batching, fallback, vars, and --no-open/--close."""

    def __init__(self, snapshot: str, *, fail_check_at: int | None = None, verify_result: str = "true") -> None:
        self.snapshot = snapshot
        self.fail_check_at = fail_check_at
        self.verify_result = verify_result
        self.opened: list[str] = []
        self.batched_calls: list[list[list[str]]] = []
        self.single_calls: list[tuple[str, ...]] = []
        self.verify_exprs: list[str] = []
        self.closed = False

    def open(self, url: str, *, profile: str | None = None) -> None:
        self.opened.append(url)

    def observe(self, category: str = "observe") -> dict[str, Any]:
        from agent_browser_decisions.core import parse_snapshot

        return {"url": "https://example.com/form", "raw": self.snapshot, "elements": parse_snapshot(self.snapshot)}

    def batch(self, commands: list[list[str]], *, bail: bool = True, category: str = "act") -> list[dict[str, Any]]:
        self.batched_calls.append(commands)
        rows: list[dict[str, Any]] = []
        values: dict[str, str] = {}
        checked: set[str] = set()
        for idx, cmd in enumerate(commands):
            if cmd[0] == "fill":
                values[cmd[1]] = cmd[2]
                rows.append({"index": idx, "success": True, "result": {"filled": cmd[1]}})
            elif cmd[0] == "check":
                checked.add(cmd[1])
                rows.append({"index": idx, "success": True, "result": {"checked": cmd[1]}})
            elif cmd[0] == "get" and cmd[1] == "value":
                val = "CORRUPTED" if self.fail_check_at == idx else values.get(cmd[2], "")
                rows.append({"index": idx, "success": True, "result": {"value": val}})
            elif cmd[0] == "is" and cmd[1] == "checked":
                rows.append({"index": idx, "success": True, "result": {"checked": cmd[2] in checked}})
        return rows

    def call(self, *args: str, check: bool = True, category: str = "browser") -> str:
        self.single_calls.append(args)
        if args[0] == "eval":
            self.verify_exprs.append(args[1])
            return self.verify_result
        if args[0] == "get" and args[1] == "value":
            return "Hanako"
        if args[0] == "is" and args[1] == "checked":
            return "true"
        return "ok"

    def current_url(self) -> str:
        return "https://example.com/form"

    def close(self) -> None:
        self.closed = True


def test_tier1_batching_vars_and_no_open_close_invariants() -> None:
    snap = '- textbox "Name" [ref=e1]\n- textbox "Email" [ref=e2]\n- checkbox "News" [ref=e3]\n'
    fb = BatchFakeBrowser(snap)
    runner = PlanRunner(
        browser_factory=lambda *a, **kw: fb,  # type: ignore[arg-type]
        settle_s=0.05,
        poll_s=0.001,
        verify_poll_s=0.001,
    )
    plan = {
        "url": "https://example.com/form",
        "reset": True,
        "vars": {"name": "Taro", "email": "t@example.com"},
        "steps": [
            {"action": "fill", "target": "Name", "value": "{{name}}"},
            {"action": "fill", "target": "Email", "value": "{{email}}"},
            {"action": "check", "target": "News", "role": "checkbox"},
        ],
        "verify": "VARS.name === 'Hanako'",
    }
    res = runner.run_plan_file(
        plan,
        session="inspect-1",
        no_open=True,
        close_on_success=True,
        vars={"name": "Hanako"},
    )
    assert res["ok"] is True
    assert res["verified"] is True
    assert res["session_open"] is False
    assert fb.closed is True
    assert fb.opened == []  # --no-open skipped redundant navigation
    assert len(fb.batched_calls) == 1
    assert all(s.get("batched") is True and s["tier"] == 1 for s in res["steps"])
    assert fb.verify_exprs and '"name": "Hanako"' in fb.verify_exprs[0]


def test_close_on_success_keeps_session_open_on_failure_and_falls_back_from_partial_batch() -> None:
    snap = '- textbox "Name" [ref=e1]\n- textbox "Email" [ref=e2]\n'
    # Batch has 2 fills (idx 0,1) + 2 get values (idx 2,3). Fail get value at idx 3 (step 2) and fail verify.
    fb = BatchFakeBrowser(snap, fail_check_at=3, verify_result="false")
    runner = PlanRunner(
        browser_factory=lambda *a, **kw: fb,  # type: ignore[arg-type]
        settle_s=0.05,
        poll_s=0.001,
        verify_poll_s=0.001,
        effect_wait_s=0.02,
    )
    plan = {
        "url": "https://example.com/form",
        "reset": True,
        "steps": [
            {"action": "fill", "target": "Name", "value": "Hanako"},
            {"action": "fill", "target": "Email", "value": "Hanako"},
        ],
        "verify": "false",
        "verify_timeout_s": 0.01,
    }
    res = runner.run_plan_file(plan, session="inspect-2", no_open=True, close_on_success=True)
    # Step 1 batched; step 2 fell back to _run_step; verify failed -> session stays OPEN.
    assert res["ok"] is False
    assert res["verified"] is False
    assert res["session_open"] is True
    assert fb.closed is False
    assert res["steps"][0].get("batched") is True
    assert res["steps"][1].get("batched") is None


def test_keep_open_and_reload_warm_session_lifecycle(tmp_path: Path) -> None:
    snap = '- textbox "Customer name:" [ref=e1]\n- textbox "Telephone:" [ref=e2]'
    fb = BatchFakeBrowser(snap)
    prof_dir = tmp_path / "warm-prof"
    prof_dir.mkdir()
    runner = PlanRunner(
        browser_factory=lambda *a, **kw: fb,
        profile_factory=lambda: str(prof_dir),
        settle_s=0.05,
        poll_s=0.001,
    )
    plan = {
        "url": "https://example.com/form",
        "reset": True,
        "vars": {"name": "Taro", "tel": "090"},
        "steps": [
            {"action": "fill", "target": "Customer name:", "value": "{{name}}"},
            {"action": "fill", "target": "Telephone:", "value": "{{tel}}"},
        ],
    }
    # Run 1: --keep-open leaves auto-created session warm & profile alive on disk
    r1 = runner.run_plan_file(plan, keep_open=True)
    assert r1["ok"] is True
    assert r1["session_open"] is True
    assert fb.closed is False
    assert prof_dir.exists()

    # Run 2: --session <id> --reload --close navigates in warm session and closes on success
    fb2 = BatchFakeBrowser(snap)
    runner2 = PlanRunner(browser_factory=lambda *a, **kw: fb2, settle_s=0.05, poll_s=0.001)
    r2 = runner2.run_plan_file(
        plan,
        session=r1["session"],
        reload_page=True,
        close_on_success=True,
        vars={"name": "Hanako"},
    )
    assert r2["ok"] is True
    assert r2["session_open"] is False
    assert fb2.opened == ["https://example.com/form"]
    assert fb2.closed is True


def test_cli_and_runner_do_not_eagerly_import_httpx() -> None:
    import subprocess
    import sys

    code = (
        "import sys; "
        "import agent_browser_decisions.cli, agent_browser_decisions.runner; "
        "assert 'httpx' not in sys.modules, 'httpx was eagerly imported'"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_tier2_disabled_by_default_even_when_api_key_in_env() -> None:
    """Default runs never call Tier 2 Decisions API even if API keys exist in environ."""
    ambiguous_snap = '- button "Save" [ref=e1]\n- button "Save" [ref=e2]\n- button "Delete" [ref=e3]'
    decisions_clients: list[FailingDecisionsClient] = []

    def make_decisions(**kwargs: Any) -> FailingDecisionsClient:
        d = FailingDecisionsClient(**kwargs)
        decisions_clients.append(d)
        return d

    runner = PlanRunner(
        browser_factory=lambda session=None, **kw: SequenceBrowser([ambiguous_snap], session=session, **kw),
        decisions_factory=make_decisions,
        settle_s=0.05,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    env = {"OPENAI_API_KEY": "sk-ambient-key", "OPENROUTER_API_KEY": "sk-or-ambient"}

    # Case 1: label miss with multiple candidates -> Tier 3 handoff, 0 Tier 2 calls
    res_miss = runner.run_plan_file(
        {"steps": [{"action": "click", "target": "Close", "role": "button"}]},
        session="s-miss",
        environ=env,
    )
    assert res_miss["ok"] is False
    assert res_miss["session_open"] is True
    assert res_miss["stopped_at_step"] == 1
    assert "stopped_snapshot" in res_miss
    assert res_miss["steps"][0]["tier"] == 3
    assert "--decisions" in res_miss["steps"][0]["reason"]
    assert all(d.calls == 0 for d in decisions_clients)

    # Case 2: ambiguous label match -> Tier 3 handoff, 0 Tier 2 calls
    res_amb = runner.run_plan_file(
        {"steps": [{"action": "click", "target": "Save", "role": "button"}]},
        session="s-amb",
        environ=env,
    )
    assert res_amb["ok"] is False
    assert res_amb["steps"][0]["tier"] == 3
    assert "ambiguous" in res_amb["steps"][0]["reason"]
    assert all(d.calls == 0 for d in decisions_clients)

    # Case 3: step with decide: True in plan JSON -> blocked unless explicitly opted in
    res_step_decide = runner.run_plan_file(
        {"steps": [{"action": "click", "target": "Save", "role": "button", "decide": True}]},
        session="s-step",
        environ=env,
    )
    assert res_step_decide["ok"] is False
    assert res_step_decide["steps"][0]["tier"] == 3
    assert "--decisions" in res_step_decide["steps"][0]["reason"]
    assert all(d.calls == 0 for d in decisions_clients)


def test_cli_decisions_opt_in_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI defaults to allow_decisions=False and enables it when --decisions or --provider is passed."""
    from agent_browser_decisions import cli

    plan_path = tmp_path / "p.json"
    plan_path.write_text('{"steps": [{"action": "click", "target": "Go"}]}', encoding="utf-8")
    recorded: list[bool | None] = []

    def fake_run_plan_file(plan: Any, **kwargs: Any) -> dict[str, Any]:
        recorded.append(kwargs.get("allow_decisions"))
        return {"ok": True, "session": "s", "session_open": False, "verified": None, "steps": []}

    monkeypatch.setattr(cli, "run_plan_file", fake_run_plan_file)
    monkeypatch.setattr(cli, "load_dotenv_files", lambda _=None: None)

    assert cli.main(["run", str(plan_path)]) == 0
    assert cli.main(["run", str(plan_path), "--decisions"]) == 0
    assert cli.main(["run", str(plan_path), "--provider", "openrouter"]) == 0
    assert cli.main(["run", str(plan_path), "--mode", "decide"]) == 0
    capsys.readouterr()
    assert recorded == [False, True, True, True]


def test_fail_fast_settle_s_zero_stops_on_first_snapshot() -> None:
    """settle_s=0 or step timeout_s=0 stops on the first snapshot without waiting 3s and preserves session."""
    snap = '- button "Submit" [ref=e1]\n- button "Cancel" [ref=e2]'
    browsers: list[SequenceBrowser] = []

    def make_browser(session=None, **kw: Any) -> SequenceBrowser:
        b = SequenceBrowser([snap], session=session, **kw)
        browsers.append(b)
        return b

    runner = PlanRunner(
        browser_factory=make_browser,
        decisions_factory=FailingDecisionsClient,
        profile_factory=lambda: "/tmp/unused",
        settle_s=3.0,
        poll_s=0.05,
    )

    res_cli = runner.run_plan_file(
        {"steps": [{"action": "click", "target": "Missing"}]},
        session="s-ff-cli",
        settle_s=0.0,
    )
    assert res_cli["ok"] is False
    assert res_cli["session_open"] is True
    assert res_cli["stopped_at_step"] == 1
    assert "Submit" in res_cli["stopped_snapshot"]
    assert browsers[-1]._idx == 1

    res_step = runner.run_plan_file(
        {"settle_s": 5.0, "steps": [{"action": "click", "target": "Missing", "timeout_s": 0}]},
        session="s-ff-step",
    )
    assert res_step["ok"] is False
    assert res_step["session_open"] is True
    assert browsers[-1]._idx == 1


def test_cli_settle_s_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI forwards --settle-s (including 0) to run_plan_file and rejects negative values."""
    from agent_browser_decisions import cli

    plan_path = tmp_path / "p.json"
    plan_path.write_text('{"steps": [{"action": "click", "target": "Go"}]}', encoding="utf-8")
    recorded: list[float | None] = []

    def fake_run_plan_file(plan: Any, **kwargs: Any) -> dict[str, Any]:
        recorded.append(kwargs.get("settle_s"))
        return {"ok": True, "session": "s", "session_open": False, "verified": None, "steps": []}

    monkeypatch.setattr(cli, "run_plan_file", fake_run_plan_file)
    monkeypatch.setattr(cli, "load_dotenv_files", lambda _=None: None)

    assert cli.main(["run", str(plan_path)]) == 0
    assert cli.main(["run", str(plan_path), "--settle-s", "0"]) == 0
    assert cli.main(["run", str(plan_path), "--settle-s", "0.5"]) == 0
    capsys.readouterr()
    assert recorded == [None, 0.0, 0.5]

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", str(plan_path), "--settle-s", "-1"])
    assert exc.value.code == 2


def test_default_exact_match_stops_instead_of_clicking_prefix_button() -> None:
    """Without match='prefix', target 'Save' must not click 'Save and publish'."""
    snaps = ['- button "Save and publish" [ref=e9]']
    browsers: list[SequenceBrowser] = []

    def make_browser(session: str | None = None, **kwargs: Any) -> SequenceBrowser:
        b = SequenceBrowser(snaps, session=session, **kwargs)
        browsers.append(b)
        return b

    runner = PlanRunner(
        browser_factory=make_browser,
        decisions_factory=FailingDecisionsClient,
        settle_s=0.0,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    res_exact = runner.run_plan_file({"url": "https://example.test", "steps": [{"action": "click", "target": "Save"}]})
    assert res_exact["ok"] is False
    assert res_exact["stopped_at_step"] == 1
    assert res_exact["session_open"] is True
    assert browsers[0].act_calls == []

    res_prefix = runner.run_plan_file(
        {"url": "https://example.test", "steps": [{"action": "click", "target": "Save", "match": "prefix"}]}
    )
    assert res_prefix["ok"] is True
    assert browsers[1].act_calls == [("click", "@e9")]


def test_default_click_and_link_never_retry_or_send_enter_without_opt_in() -> None:
    """Default click (button or link) fires at most 1 activation on the same element."""

    class StaticLinkBrowser(FakeBrowser):
        def observe(self, category: str = "observe") -> dict[str, Any]:
            raw = '- link "Delete item" [ref=e1]\n- button "Approve" [ref=e2]'
            return {"url": self.url, "raw": raw, "elements": parse_snapshot(raw)}

    def make_runner(store: list[StaticLinkBrowser]) -> PlanRunner:
        def factory(session: str | None = None, **kw: Any) -> StaticLinkBrowser:
            b = StaticLinkBrowser(session, effect_ok=False, **kw)
            store.append(b)
            return b

        return PlanRunner(
            browser_factory=factory,
            decisions_factory=FailingDecisionsClient,
            settle_s=0.0,
            poll_s=0.005,
            effect_wait_s=0.02,
        )

    # 1) Default link click with no navigation must NOT send Enter or second click
    link_browsers: list[StaticLinkBrowser] = []
    res_link = make_runner(link_browsers).run_plan_file(
        {"url": "https://example.test/items", "steps": [{"action": "click", "target": "Delete item"}]}
    )
    assert res_link["ok"] is False
    assert link_browsers[0].act_calls == [("click", "@e1")]

    # 2) Default button click with no effect must NOT retry a second click
    btn_browsers: list[StaticLinkBrowser] = []
    res_btn = make_runner(btn_browsers).run_plan_file(
        {"url": "https://example.test/items", "steps": [{"action": "click", "target": "Approve"}]}
    )
    assert res_btn["ok"] is False
    assert btn_browsers[0].act_calls == [("click", "@e2")]

    # 3) Opt-in retry=True on link tries click + focus/Enter once (at most 2 activations, never 4)
    retry_browsers: list[StaticLinkBrowser] = []
    res_retry = make_runner(retry_browsers).run_plan_file(
        {"url": "https://example.test/items", "steps": [{"action": "click", "target": "Delete item", "retry": True}]}
    )
    assert res_retry["ok"] is False
    assert retry_browsers[0].act_calls == [("click", "@e1"), ("press", "Enter")]


def test_expect_url_blocks_resume_on_wrong_page_without_executing_steps() -> None:
    """--expect-url aborts before step execution when live session drifted to another page."""
    browsers: list[FakeBrowser] = []

    def make_browser(session: str | None = None, **kwargs: Any) -> FakeBrowser:
        fb = FakeBrowser(session, open_ok=True, effect_ok=True, **kwargs)
        fb.url = "https://example.test/orders-delete"
        browsers.append(fb)
        return fb

    runner = PlanRunner(
        browser_factory=make_browser,
        decisions_factory=FailingDecisionsClient,
        settle_s=0.0,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    plan = {
        "url": "https://example.test/orders",
        "steps": [
            {"action": "click", "target": "Go"},
            {"action": "click", "target": "Go"},
        ],
    }
    res_bad = runner.run_plan_file(
        plan,
        session="live-sess",
        from_step=2,
        expect_url="https://example.test/orders",
    )
    assert res_bad["ok"] is False
    assert res_bad["session_open"] is True
    assert res_bad["stopped_at_step"] == 2
    assert res_bad["steps"] == []
    assert "expect_url mismatch" in (res_bad.get("error") or "")
    assert browsers[0].act_calls == []
    assert "live-sess" not in browsers[0].closed_sessions

    res_ok = runner.run_plan_file(
        plan,
        session="live-sess",
        from_step=2,
        expect_url="https://example.test/orders-delete",
    )
    assert res_ok["ok"] is True
    assert browsers[1].act_calls == [("click", "@e1")]


def test_cli_expect_url_flag_forwarding(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    from agent_browser_decisions import cli

    plan_path = tmp_path / "p.json"
    plan_path.write_text(json.dumps({"steps": [{"action": "click", "target": "Go"}]}), encoding="utf-8")
    recorded: list[str | None] = []

    def fake_run_plan_file(*args: Any, **kwargs: Any) -> dict[str, Any]:
        recorded.append(kwargs.get("expect_url"))
        return {"ok": True, "session": "s", "session_open": False, "verified": None, "steps": []}

    monkeypatch.setattr(cli, "run_plan_file", fake_run_plan_file)
    monkeypatch.setattr(cli, "load_dotenv_files", lambda _=None: None)

    assert cli.main(["run", str(plan_path), "--session", "s", "--expect-url", "https://example.test/orders"]) == 0
    capsys.readouterr()
    assert recorded == ["https://example.test/orders"]


def test_resume_redacts_prior_step_secrets_across_outputs_and_tier2() -> None:
    secret_env = "sk-prior-step-secret-777"
    secret_lit = "lit-pin-secret-888"

    class RecordingDecisions(DecisionsClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.payloads: list[dict[str, Any]] = []

        def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.payloads.append(payload)
            return {
                "decisions": [
                    {"question_id": "element", "status": "answered", "choice": "e2", "confidence": 0.9}
                ]
            }

    # Case A: resume fails on step 3 -> prior-step env + literal secrets redacted from stopped_snapshot & url
    def make_fail_browser(session: str | None = None, **kwargs: Any) -> FakeBrowser:
        b = FakeBrowser(session, **kwargs)
        b.url = f"https://example.test/form?k={secret_env}"
        b.snap = (
            f'- textbox "API Key" [ref=e1]: {secret_env}\n'
            f'- textbox "PIN" [ref=e2]: {secret_lit}\n'
            '- button "Other" [ref=e3]'
        )
        return b

    plan_fail = {
        "url": "https://example.test/form",
        "steps": [
            {"action": "fill", "target": "API Key", "value_env": "PRIOR_TOKEN"},
            {"action": "fill", "target": "PIN", "value": secret_lit, "secret": True},
            {"action": "click", "target": "Missing Button"},
        ],
    }
    runner_fail = PlanRunner(
        browser_factory=make_fail_browser,
        decisions_factory=RecordingDecisions,
        settle_s=0.0,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    res_fail = runner_fail.run_plan_file(
        plan_fail,
        session="live-sess",
        from_step=3,
        environ={"PRIOR_TOKEN": secret_env},
    )
    assert res_fail["ok"] is False
    dumped_fail = json.dumps(res_fail)
    assert secret_env not in dumped_fail
    assert secret_lit not in dumped_fail
    assert "[REDACTED]" in (res_fail.get("stopped_snapshot") or "")

    # Case B: unset prior env var does NOT fail resume, and verify_value + Tier 2 payload are redacted
    recorded_clients: list[RecordingDecisions] = []

    def make_decisions(**kwargs: Any) -> RecordingDecisions:
        c = RecordingDecisions(**kwargs)
        recorded_clients.append(c)
        return c

    def make_t2_browser(session: str | None = None, **kwargs: Any) -> FakeBrowser:
        b = FakeBrowser(
            session,
            verify_out=json.dumps({"ok": True, "echo": secret_lit}),
            **kwargs,
        )
        b.url = f"https://example.test/form?pin={secret_lit}"
        b.snap = (
            f'- button "Save {secret_lit}" [ref=e1]\n'
            '- button "Confirm" [ref=e2]'
        )
        return b

    plan_t2 = {
        "url": "https://example.test/form",
        "steps": [
            {"action": "fill", "target": "API Key", "value_env": "UNSET_PRIOR_TOKEN"},
            {"action": "fill", "target": "PIN", "value": secret_lit, "secret": True},
            {"action": "click", "target": "Submit"},
        ],
        "verify": "({ok: true})",
    }
    runner_t2 = PlanRunner(
        browser_factory=make_t2_browser,
        decisions_factory=make_decisions,
        settle_s=0.0,
        poll_s=0.005,
        effect_wait_s=0.02,
    )
    res_t2 = runner_t2.run_plan_file(
        plan_t2,
        session="live-sess",
        from_step=3,
        allow_decisions=True,
        environ={},
    )
    assert res_t2["ok"] is True
    assert secret_lit not in json.dumps(res_t2)
    assert res_t2["verify_value"] == {"ok": True, "echo": "[REDACTED]"}
    assert len(recorded_clients) == 1 and len(recorded_clients[0].payloads) == 1
    dumped_req = json.dumps(recorded_clients[0].payloads[0])
    assert secret_lit not in dumped_req
    assert "[REDACTED]" in dumped_req
