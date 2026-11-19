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
