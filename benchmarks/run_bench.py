"""Fair same-endpoint benchmark suite for agent-browser-decisions.

Compares Playwright (local Chrome via `--chrome`), wait-tuned `agent-browser batch --bail`,
and `agent-browser-decisions` (`agent-browser`'s default headless browser) using identical
verify expressions, measuring wall-clock time from process launch through each method's
close step.

Usage:
    uv run --with playwright python benchmarks/run_bench.py \
        --suite all --rounds 5 --out benchmarks/results-v0.9.10.json
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from agent_browser_decisions.browser import AgentBrowser

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
PLANS_DIR = BENCH_DIR / "plans"
SCRIPTS_DIR = BENCH_DIR / "scripts"
WIZARD_SERVER = ROOT / "examples" / "wizard" / "server.py"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

HTTPBIN_URL = "https://httpbin.org/forms/post"
HTTPBIN_VERIFY_JS = (
    "(() => {"
    " const v = n => document.querySelector(`[name=${n}]`)?.value;"
    " const sz = document.querySelector('input[name=size]:checked')?.parentElement.textContent.trim();"
    " const tp = [...document.querySelectorAll('input[name=topping]:checked')]"
    ".map(e => e.parentElement.textContent.trim());"
    " return v('custname') === 'Taro Yamada'"
    " && v('custtel') === '090-1234-5678'"
    " && v('custemail') === 'taro@example.com'"
    " && sz === 'Medium'"
    " && tp.length === 2 && tp.includes('Bacon') && tp.includes('Extra Cheese')"
    " && v('comments') === 'Leave at front door';"
    "})()"
)


def wizard_verify_js(code: str = "INVITE-001") -> str:
    return (
        "(() => {"
        " return !document.getElementById('screen-3').classList.contains('hidden')"
        " && document.getElementById('summary-name').textContent === 'Taro Yamada'"
        " && document.getElementById('summary-email').textContent === 'taro@example.com'"
        f" && document.getElementById('summary-code').textContent === {json.dumps(code)}"
        " && document.getElementById('summary-token').textContent.startsWith('tok-')"
        " && document.getElementById('summary-workspace').textContent === 'core-app'"
        " && document.getElementById('summary-region').textContent === 'tokyo'"
        " && document.getElementById('summary-audit').textContent === 'enabled'"
        " && document.getElementById('note').value === 'Ready for staging review';"
        "})()"
    )


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "TYPESAFE_API_KEY",
        "LIQUID_API_KEY",
        "DECISION_API_KEY",
        "DECISION_PROVIDER",
        "DECISION_MODEL",
        "DECISION_BASE_URL",
    ):
        env.pop(key, None)
    return env


def _strip_profiles(obj: object) -> object:
    if isinstance(obj, dict):
        obj.pop("profile", None)
        for v in obj.values():
            _strip_profiles(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_profiles(item)
    return obj


def _cleanup_session_and_profile(sid: str | None, profile: str | None = None) -> None:
    closed = False
    if sid:
        browser = AgentBrowser(session=sid)
        registry = browser._capture_registry()
        closed = browser._confirm_closed(expected_registry=registry) or browser.close()
    if profile and closed:
        tmp_root = Path(os.path.realpath(tempfile.gettempdir()))
        prof_path = Path(profile)
        if not prof_path.is_symlink() and prof_path.is_dir():
            resolved = Path(os.path.realpath(prof_path))
            if resolved != tmp_root and tmp_root in resolved.parents:
                shutil.rmtree(resolved)


@contextlib.contextmanager
def local_wizard_server(delay_ms: int = 250) -> Iterator[str]:
    proc = subprocess.Popen(
        [sys.executable, str(WIZARD_SERVER), "--port", "0", "--delay-ms", str(delay_ms)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        url = proc.stdout.readline().strip()
        if not url.startswith("http://127.0.0.1:"):
            raise RuntimeError(f"Wizard server failed to start (got {url!r})")
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"{url}health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def reset_wizard_server(base_url: str) -> None:
    req = urllib.request.Request(f"{base_url}api/reset", data=b"{}", method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        assert r.status == 200


def run_abd(
    plan_path: Path,
    extra_args: list[str] | None = None,
    *,
    expect_closed: bool = True,
) -> dict:
    cmd = ["agent-browser-decisions", "run", str(plan_path), "--no-decisions", *(extra_args or [])]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, check=False, text=True, timeout=60, env=_clean_env())
    dt = time.perf_counter() - t0
    data: dict = {}
    with contextlib.suppress(json.JSONDecodeError):
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    steps = data.get("steps") or []
    closed_ok = (data.get("session_open") is False) if expect_closed else True
    return {
        "ok": r.returncode == 0 and bool(data.get("ok")) and bool(data.get("verified")) and closed_ok,
        "rc": r.returncode,
        "wall_s": round(dt, 3),
        "internal_ms": data.get("elapsed_ms"),
        "tiers": [s.get("tier") for s in steps],
        "batched_steps": sum(1 for s in steps if s.get("batched")),
        "stopped_at_step": data.get("stopped_at_step"),
        "stopped_effect": steps[-1].get("effect") if steps else None,
        "stopped_reason": steps[-1].get("reason") if steps else None,
        "session": data.get("session"),
        "session_open": data.get("session_open"),
        "profile": data.get("profile"),
        "error": data.get("error"),
    }


def run_batch(cmds: list[list[str]], session_prefix: str, *, close_after: bool = True) -> dict:
    sess = f"{session_prefix}-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    payload = json.dumps(cmds)
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            ["agent-browser", "--session", sess, "batch", "--bail", "--json"],
            input=payload,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        dt = time.perf_counter() - t0
        ok = False
        err = None
        if r.returncode == 0 and r.stdout.strip():
            try:
                rows = json.loads(r.stdout)
                eval_rows = [row for row in rows if row.get("command", [""])[0] == "eval"]
                all_ok = all(row.get("success") for row in rows)
                eval_true = bool(eval_rows) and eval_rows[-1].get("result", {}).get("result") is True
                ok = all_ok and eval_true
                if not ok:
                    failed = [row for row in rows if not row.get("success")]
                    err = failed[-1].get("error") if failed else f"verify={eval_rows}"
            except json.JSONDecodeError:
                err = r.stdout[:200]
        else:
            err = (r.stderr or r.stdout).strip()[:200]
        return {"ok": ok, "wall_s": round(dt, 3), "session": sess, "error": err}
    finally:
        if close_after:
            subprocess.run(["agent-browser", "--session", sess, "close"], capture_output=True, check=False)


def httpbin_batch_cmds(size: str = "Medium") -> list[list[str]]:
    return [
        ["open", HTTPBIN_URL],
        ["find", "label", "Customer name:", "fill", "Taro Yamada"],
        ["find", "label", "Telephone:", "fill", "090-1234-5678"],
        ["find", "label", "E-mail address:", "fill", "taro@example.com"],
        ["find", "role", "radio", "check", "--name", size],
        ["find", "role", "checkbox", "check", "--name", "Bacon"],
        ["find", "role", "checkbox", "check", "--name", "Extra Cheese"],
        ["find", "label", "Delivery instructions:", "fill", "Leave at front door"],
        ["eval", HTTPBIN_VERIFY_JS],
        ["close"],
    ]


def wizard_batch_cmds(url: str, code: str = "INVITE-001") -> list[list[str]]:
    return [
        ["open", url],
        ["find", "label", "Display name:", "fill", "Taro Yamada"],
        ["find", "label", "Work email:", "fill", "taro@example.com"],
        ["find", "label", "One-time code:", "fill", code],
        ["find", "role", "button", "click", "--name", "Verify & continue"],
        ["wait", "--text", "Step 2 of 3"],
        ["find", "label", "Workspace name:", "fill", "core-app"],
        ["find", "role", "radio", "check", "--name", "Tokyo"],
        ["find", "role", "checkbox", "check", "--name", "Enable audit log"],
        ["find", "role", "button", "click", "--name", "Continue"],
        ["wait", "--text", "Step 3 of 3"],
        ["find", "label", "Release note:", "fill", "Ready for staging review"],
        ["eval", wizard_verify_js(code)],
        ["close"],
    ]


def run_playwright(script_path: Path, args: list[str], py_exe: str) -> dict:
    t0 = time.perf_counter()
    r = subprocess.run([py_exe, str(script_path), *args], capture_output=True, check=False, text=True, timeout=60)
    dt = time.perf_counter() - t0
    return {
        "ok": r.returncode == 0,
        "wall_s": round(dt, 3),
        "error": r.stderr.strip().splitlines()[-1][:200] if r.returncode != 0 and r.stderr.strip() else None,
    }


def summarize(results: dict[str, list[dict]]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for k, rows in results.items():
        ok_times = [x["wall_s"] for x in rows if x["ok"]]
        all_times = [x["wall_s"] for x in rows]
        summary[k] = {
            "passed": sum(1 for x in rows if x["ok"]),
            "total": len(rows),
            "median_ok_s": round(statistics.median(ok_times), 2) if ok_times else None,
            "min_ok_s": round(min(ok_times), 2) if ok_times else None,
            "max_ok_s": round(max(ok_times), 2) if ok_times else None,
            "median_all_s": round(statistics.median(all_times), 2) if all_times else None,
        }
    return summary


def run_main_suite(rounds: int, chrome_bin: str, py_exe: str) -> dict:
    hb_plan = PLANS_DIR / "httpbin.json"
    pw_hb = SCRIPTS_DIR / "pw_httpbin.py"
    pw_wiz = SCRIPTS_DIR / "pw_wizard.py"
    wiz_template = json.loads((PLANS_DIR / "wizard.json").read_text(encoding="utf-8"))

    results: dict[str, list[dict]] = {
        "httpbin_playwright": [],
        "httpbin_batch": [],
        "httpbin_abd": [],
        "wizard_playwright": [],
        "wizard_batch_wait": [],
        "wizard_abd": [],
    }
    with local_wizard_server(delay_ms=250) as wiz_url, tempfile.TemporaryDirectory() as td:
        wiz_plan = Path(td) / "wizard.json"
        wiz_template["url"] = wiz_url
        wiz_plan.write_text(json.dumps(wiz_template, ensure_ascii=False, indent=2), encoding="utf-8")

        for r in range(rounds):
            rot = r % 3
            hb_methods = [
                ("httpbin_playwright", lambda: run_playwright(pw_hb, [chrome_bin], py_exe)),
                ("httpbin_batch", lambda: run_batch(httpbin_batch_cmds(), "bench-hb-batch")),
                ("httpbin_abd", lambda: run_abd(hb_plan)),
            ]
            for name, fn in hb_methods[rot:] + hb_methods[:rot]:
                res = fn()
                results[name].append(res)
                print(f"[main R{r + 1}] {name}: ok={res['ok']} wall_s={res['wall_s']}", flush=True)

            wiz_methods = [
                (
                    "wizard_playwright",
                    lambda: (reset_wizard_server(wiz_url), run_playwright(pw_wiz, [chrome_bin, wiz_url], py_exe))[1],
                ),
                (
                    "wizard_batch_wait",
                    lambda: (reset_wizard_server(wiz_url), run_batch(wizard_batch_cmds(wiz_url), "bench-wiz-batch"))[1],
                ),
                (
                    "wizard_abd",
                    lambda: (reset_wizard_server(wiz_url), run_abd(wiz_plan))[1],
                ),
            ]
            for name, fn in wiz_methods[rot:] + wiz_methods[:rot]:
                res = fn()
                results[name].append(res)
                print(f"[main R{r + 1}] {name}: ok={res['ok']} wall_s={res['wall_s']}", flush=True)
    return {"rounds": rounds, "summary": summarize(results), "raw": results}


def run_reuse_suite(rounds: int) -> dict:
    hb_plan = PLANS_DIR / "httpbin.json"
    results: dict[str, list[dict]] = {
        "abd_cold": [],
        "abd_reload": [],
        "abd_no_open": [],
    }
    for i in range(rounds):
        res = run_abd(hb_plan)
        results["abd_cold"].append(res)
        print(f"[reuse cold {i + 1}] wall_s={res['wall_s']} internal_ms={res['internal_ms']}", flush=True)

    seed = run_abd(hb_plan, ["--keep-open"], expect_closed=False)
    sid = seed.get("session")
    try:
        for i in range(rounds):
            args = ["--session", sid, "--reload", "--var", "name=Hanako Sato", "--var", "size=Large"]
            is_last = i == rounds - 1
            if is_last:
                args.append("--close")
            res = run_abd(hb_plan, args, expect_closed=is_last)
            results["abd_reload"].append(res)
            print(f"[reuse reload {i + 1}] wall_s={res['wall_s']} internal_ms={res['internal_ms']}", flush=True)
    finally:
        _cleanup_session_and_profile(sid, seed.get("profile"))

    for i in range(rounds):
        sess = f"bench-noopen-{os.getpid()}-{i}"
        subprocess.run(["agent-browser", "--session", sess, "open", HTTPBIN_URL], capture_output=True, check=True)
        try:
            res = run_abd(hb_plan, ["--session", sess, "--no-open", "--close"])
            results["abd_no_open"].append(res)
            print(f"[reuse no_open {i + 1}] wall_s={res['wall_s']} internal_ms={res['internal_ms']}", flush=True)
        finally:
            _cleanup_session_and_profile(sess)

    return {"rounds": rounds, "summary": summarize(results), "raw": results}


def run_drift_suite(chrome_bin: str, py_exe: str) -> dict:
    hb_plan = PLANS_DIR / "httpbin.json"
    pw_hb = SCRIPTS_DIR / "pw_httpbin.py"

    pw_res = run_playwright(pw_hb, [chrome_bin, "Extra Large", "3000"], py_exe)

    batch_res = run_batch(httpbin_batch_cmds(size="Extra Large"), "bench-drift-batch", close_after=False)
    batch_sess = batch_res["session"]
    try:
        chk = subprocess.run(
            ["agent-browser", "--session", batch_sess, "eval", "document.querySelector('[name=custname]').value"],
            capture_output=True,
            check=False,
            text=True,
        )
        batch_res["session_kept_custname"] = chk.stdout.strip()
    finally:
        subprocess.run(["agent-browser", "--session", batch_sess, "close"], capture_output=True, check=False)

    stop_res = run_abd(hb_plan, ["--var", "size=Extra Large"], expect_closed=False)
    sid = stop_res.get("session")
    kept_name = None
    resume_res = None
    try:
        if sid:
            chk = subprocess.run(
                ["agent-browser", "--session", sid, "eval", "document.querySelector('[name=custname]').value"],
                capture_output=True,
                check=False,
                text=True,
            )
            kept_name = chk.stdout.strip()
            resume_res = run_abd(
                hb_plan,
                ["--session", sid, "--from-step", "4", "--expect-url", HTTPBIN_URL, "--var", "size=Large", "--close"],
            )
    finally:
        _cleanup_session_and_profile(sid, stop_res.get("profile"))

    stop_res2 = run_abd(hb_plan, ["--var", "size=Extra Large"], expect_closed=False)
    sid2 = stop_res2.get("session")
    resume_step5 = None
    try:
        if sid2:
            subprocess.run(
                ["agent-browser", "--session", sid2, "find", "role", "radio", "check", "--name", "Large"],
                capture_output=True,
                check=True,
            )
            resume_step5 = run_abd(
                hb_plan,
                ["--session", sid2, "--from-step", "5", "--expect-url", HTTPBIN_URL, "--var", "size=Large", "--close"],
            )
    finally:
        _cleanup_session_and_profile(sid2, stop_res2.get("profile"))

    out = {
        "playwright_default_timeout3s": pw_res,
        "batch_bail": batch_res,
        "abd_stop": stop_res,
        "abd_kept_custname": kept_name,
        "abd_resume_from_step_4": resume_res,
        "abd_resume_from_step_5_after_direct_op": resume_step5,
    }
    print("[drift]", json.dumps(_strip_profiles(json.loads(json.dumps(out))), indent=2, ensure_ascii=False), flush=True)
    return out


def run_wizard_resume_suite(rounds: int) -> dict:
    """Compare live-session resume (--from-step 8) vs restart on the local 3-screen single-use-code wizard."""
    fixed_data = json.loads((PLANS_DIR / "wizard.json").read_text(encoding="utf-8"))
    broken_data = json.loads(json.dumps(fixed_data))
    broken_data["steps"][7]["target"] = "Next"

    results: dict[str, list[dict]] = {
        "wizard_stop_step8": [],
        "wizard_restart_same_code_rejected": [],
        "wizard_resume_step8": [],
        "wizard_restart_fresh_code": [],
    }
    with local_wizard_server(delay_ms=250) as wiz_url, tempfile.TemporaryDirectory() as td:
        broken_data["url"] = wiz_url
        fixed_data["url"] = wiz_url
        broken_path = Path(td) / "wizard_broken.json"
        fixed_path = Path(td) / "wizard_fixed.json"
        broken_path.write_text(json.dumps(broken_data, ensure_ascii=False, indent=2), encoding="utf-8")
        fixed_path.write_text(json.dumps(fixed_data, ensure_ascii=False, indent=2), encoding="utf-8")

        for r in range(rounds):
            order = ["resume", "restart"] if r % 2 == 0 else ["restart", "resume"]
            for mode in order:
                if mode == "resume":
                    reset_wizard_server(wiz_url)
                    stop_res = run_abd(broken_path, ["--settle-s", "0.5"], expect_closed=False)
                    sid = stop_res.get("session")
                    stop_ok = (
                        stop_res.get("stopped_at_step") == 8
                        and str(stop_res.get("stopped_reason") or "").startswith("no label match")
                        and stop_res.get("session_open") is True
                        and bool(sid)
                    )
                    results["wizard_stop_step8"].append({**stop_res, "ok": stop_ok})
                    try:
                        # Prove restarting from step 1 with the already-consumed INVITE-001 fails (HTTP 409)
                        same_res = run_abd(fixed_path, ["--settle-s", "0.5"], expect_closed=False)
                        same_sid = same_res.get("session")
                        try:
                            rejected_ok = (
                                same_res.get("rc") == 1
                                and same_res.get("stopped_at_step") == 4
                                and same_res.get("stopped_effect") == "no_effect"
                            )
                            results["wizard_restart_same_code_rejected"].append({**same_res, "ok": rejected_ok})
                        finally:
                            _cleanup_session_and_profile(same_sid, same_res.get("profile"))

                        if sid and stop_ok:
                            res = run_abd(
                                fixed_path,
                                ["--session", sid, "--from-step", "8", "--expect-url", wiz_url, "--close"],
                            )
                        else:
                            res = {"ok": False, "wall_s": 0.0, "error": "did not stop cleanly at step 8"}
                        results["wizard_resume_step8"].append(res)
                        print(
                            f"[wizard_resume R{r + 1}] stop={stop_res['wall_s']}s "
                            f"same_code_rejected={rejected_ok} resume_step8: ok={res['ok']} wall_s={res['wall_s']}",
                            flush=True,
                        )
                    finally:
                        _cleanup_session_and_profile(sid, stop_res.get("profile"))
                else:
                    reset_wizard_server(wiz_url)
                    res = run_abd(fixed_path, ["--var", f"code=INVITE-FRESH-{r + 1}"])
                    results["wizard_restart_fresh_code"].append(res)
                    print(
                        f"[wizard_resume R{r + 1}] restart_fresh_code: ok={res['ok']} wall_s={res['wall_s']}",
                        flush=True,
                    )
    return {"rounds": rounds, "summary": summarize(results), "raw": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["main", "reuse", "drift", "wizard_resume", "all"], default="all")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--chrome", type=str, default=DEFAULT_CHROME)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    abd_ver = subprocess.run(
        ["agent-browser-decisions", "--version"], capture_output=True, check=False, text=True
    ).stdout.strip()
    ab_ver = subprocess.run(["agent-browser", "--version"], capture_output=True, check=False, text=True).stdout.strip()
    try:
        pw_ver = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        pw_ver = None

    out: dict = {
        "chrome_for_playwright": args.chrome,
        "abd_version": abd_ver,
        "agent_browser_version": ab_ver,
        "playwright_version": pw_ver,
    }
    if args.suite in ("main", "all"):
        out["main"] = run_main_suite(args.rounds, args.chrome, sys.executable)
    if args.suite in ("reuse", "all"):
        out["reuse"] = run_reuse_suite(args.rounds)
    if args.suite in ("drift", "all"):
        out["drift"] = run_drift_suite(args.chrome, sys.executable)
    if args.suite in ("wizard_resume", "all"):
        out["wizard_resume"] = run_wizard_resume_suite(args.rounds)

    _strip_profiles(out)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved results to {args.out}")


if __name__ == "__main__":
    main()
