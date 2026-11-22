"""Fair same-endpoint benchmark suite for agent-browser-decisions.

Compares Playwright, wait-tuned `agent-browser batch --bail`, and `agent-browser-decisions`
using the same local Chrome binary and identical verify expressions from process launch
through clean close.

Usage:
    uv run --with playwright python benchmarks/run_bench.py \
        --suite all --rounds 5 --out benchmarks/results-v0.6.0.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLANS_DIR = ROOT / "plans"
SCRIPTS_DIR = ROOT / "scripts"

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HTTPBIN_URL = "https://httpbin.org/forms/post"
FLIGHTS_URL = "https://www.google.com/travel/flights?hl=en"
FLIGHTS_DATE = "Tuesday, December 15, 2026"

HTTPBIN_VERIFY_JS = (
    "(() => {"
    " const v = n => document.querySelector(`[name=${n}]`)?.value;"
    " const sz = document.querySelector('input[name=size]:checked')?.parentElement.textContent.trim();"
    " const tp = [...document.querySelectorAll('input[name=topping]:checked')]"
    "   .map(e => e.parentElement.textContent.trim());"
    " return v('custname') === 'Taro Yamada'"
    " && v('custtel') === '090-1234-5678'"
    " && v('custemail') === 'taro@example.com'"
    " && sz === 'Medium'"
    " && tp.length === 2 && tp.includes('Bacon') && tp.includes('Extra Cheese')"
    " && v('comments') === 'Leave at front door';"
    "})()"
)

FLIGHTS_VERIFY_JS = (
    "(() => {"
    " if (!location.href.includes('/travel/flights/search')) return false;"
    " const val = name => [...document.querySelectorAll('input')]"
    "   .filter(e => e.offsetParent !== null && (e.getAttribute('aria-label') || '').trim() === name)"
    "   .map(e => e.value)[0] || '';"
    " const retVisible = [...document.querySelectorAll('input[aria-label=\"Return\"]')]"
    "   .some(e => e.offsetParent !== null);"
    " return val('Where from?').includes('Zürich')"
    "   && val('Where to?').includes('London')"
    "   && val('Departure').includes('Dec 15')"
    "   && !retVisible;"
    "})()"
)


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}


def run_abd(plan_path: Path, extra_args: list[str] | None = None) -> dict:
    cmd = ["agent-browser-decisions", "run", str(plan_path), *(extra_args or [])]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, check=False, text=True, env=clean_env(), timeout=60)
    dt = time.perf_counter() - t0
    data = json.loads(r.stdout) if r.stdout.strip() else {}
    if not extra_args and data.get("session_open") and data.get("session"):
        subprocess.run(["agent-browser", "--session", data["session"], "close"], capture_output=True, check=False)
    return {
        "ok": r.returncode == 0 and data.get("ok") is True and data.get("verified") is True,
        "wall_s": round(dt, 3),
        "internal_ms": data.get("elapsed_ms"),
        "stopped_at_step": data.get("stopped_at_step"),
        "session": data.get("session"),
        "session_open": data.get("session_open"),
        "stopped_snapshot_lines": len(data["stopped_snapshot"].splitlines()) if data.get("stopped_snapshot") else 0,
        "error": data.get("error") or (r.stderr.strip()[:200] if r.returncode != 0 else None),
    }


def run_batch(cmds: list[list[str]], sess_prefix: str, *, close_after: bool = True) -> dict:
    sess = f"{sess_prefix}-{os.getpid()}-{int(time.time() * 1000)}"
    env = {**clean_env(), "AGENT_BROWSER_SESSION": sess}
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            ["agent-browser", "batch", "--json", "--bail"],
            input=json.dumps(cmds),
            capture_output=True, check=False,
            text=True,
            env=env,
            timeout=60,
        )
        dt = time.perf_counter() - t0
        rows = json.loads(r.stdout) if r.stdout.strip() else []
        all_ok = len(rows) == len(cmds) and all(x.get("success") for x in rows)
        verify_res = rows[-2].get("result", {}).get("result") if len(rows) >= 2 and all_ok else None
        return {
            "ok": all_ok and verify_res is True,
            "wall_s": round(dt, 3),
            "verify": verify_res,
            "steps_ran": len(rows),
            "steps_total": len(cmds),
            "session": sess,
            "error": next((x.get("error") for x in rows if not x.get("success")), None),
        }
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


def flights_batch_cmds() -> list[list[str]]:
    return [
        ["open", FLIGHTS_URL],
        ["find", "role", "combobox", "click", "--name", "Change ticket type."],
        ["wait", "--text", "One way"],
        ["find", "role", "option", "click", "--name", "One way"],
        ["find", "role", "combobox", "click", "--name", "Where from?"],
        ["find", "role", "combobox", "fill", "Zurich", "--name", "Where else?"],
        ["wait", "--text", "Zürich, Switzerland"],
        ["find", "role", "option", "click", "--name", "Zürich, Switzerland"],
        ["find", "role", "combobox", "click", "--name", "Where to?"],
        ["find", "role", "combobox", "fill", "London", "--name", "Where to?"],
        ["wait", "--text", "London, United Kingdom"],
        ["find", "role", "option", "click", "--name", "London, United Kingdom"],
        ["find", "role", "textbox", "click", "--name", "Departure"],
        ["wait", "--text", "Done"],
        ["find", "role", "button", "click", "--name", FLIGHTS_DATE],
        ["find", "role", "button", "click", "--name", "Done."],
        ["wait", "600"],
        ["find", "role", "button", "click", "--name", "Search"],
        ["wait", "--fn", FLIGHTS_VERIFY_JS],
        ["eval", FLIGHTS_VERIFY_JS],
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
    fl_plan = PLANS_DIR / "flights.json"
    pw_hb = SCRIPTS_DIR / "pw_httpbin.py"
    pw_fl = SCRIPTS_DIR / "pw_flights.py"

    results: dict[str, list[dict]] = {
        "httpbin_playwright": [],
        "httpbin_batch": [],
        "httpbin_abd": [],
        "flights_playwright": [],
        "flights_batch_wait": [],
        "flights_abd": [],
    }
    for r in range(rounds):
        rot = r % 3
        hb_methods = [
            ("httpbin_playwright", lambda: run_playwright(pw_hb, [chrome_bin], py_exe)),
            ("httpbin_batch", lambda: run_batch(httpbin_batch_cmds(), "bench-hb-batch")),
            ("httpbin_abd", lambda: run_abd(hb_plan)),
        ]
        fl_methods = [
            ("flights_playwright", lambda: run_playwright(pw_fl, [chrome_bin], py_exe)),
            ("flights_batch_wait", lambda: run_batch(flights_batch_cmds(), "bench-fl-batch")),
            ("flights_abd", lambda: run_abd(fl_plan)),
        ]
        for name, fn in hb_methods[rot:] + hb_methods[:rot]:
            res = fn()
            results[name].append(res)
            print(f"[main R{r + 1}] {name}: ok={res['ok']} wall_s={res['wall_s']}", flush=True)
        for name, fn in fl_methods[rot:] + fl_methods[:rot]:
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

    seed = run_abd(hb_plan, ["--keep-open"])
    sid = seed.get("session")
    try:
        for i in range(rounds):
            args = ["--session", sid, "--reload", "--var", "name=Hanako Sato", "--var", "size=Large"]
            if i == rounds - 1:
                args.append("--close")
            res = run_abd(hb_plan, args)
            results["abd_reload"].append(res)
            print(f"[reuse reload {i + 1}] wall_s={res['wall_s']} internal_ms={res['internal_ms']}", flush=True)
    finally:
        if sid:
            subprocess.run(["agent-browser", "--session", sid, "close"], capture_output=True, check=False)

    for i in range(rounds):
        sess = f"bench-noopen-{os.getpid()}-{i}"
        subprocess.run(["agent-browser", "--session", sess, "open", HTTPBIN_URL], capture_output=True, check=True)
        try:
            res = run_abd(hb_plan, ["--session", sess, "--no-open", "--close"])
            results["abd_no_open"].append(res)
            print(f"[reuse no_open {i + 1}] wall_s={res['wall_s']} internal_ms={res['internal_ms']}", flush=True)
        finally:
            subprocess.run(["agent-browser", "--session", sess, "close"], capture_output=True, check=False)

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
            capture_output=True, check=False,
            text=True,
        )
        batch_res["session_kept_custname"] = chk.stdout.strip()
    finally:
        subprocess.run(["agent-browser", "--session", batch_sess, "close"], capture_output=True, check=False)

    stop_res = run_abd(hb_plan, ["--var", "size=Extra Large"])
    sid = stop_res.get("session")
    kept_name = None
    resume_res = None
    try:
        if sid:
            chk = subprocess.run(
                ["agent-browser", "--session", sid, "eval", "document.querySelector('[name=custname]').value"],
                capture_output=True, check=False,
                text=True,
            )
            kept_name = chk.stdout.strip()
            resume_res = run_abd(
                hb_plan,
                ["--session", sid, "--from-step", "4", "--var", "size=Large", "--close"],
            )
    finally:
        if sid:
            subprocess.run(["agent-browser", "--session", sid, "close"], capture_output=True, check=False)

    stop_res2 = run_abd(hb_plan, ["--var", "size=Extra Large"])
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
                ["--session", sid2, "--from-step", "5", "--var", "size=Large", "--close"],
            )
    finally:
        if sid2:
            subprocess.run(["agent-browser", "--session", sid2, "close"], capture_output=True, check=False)

    out = {
        "playwright_default_timeout3s": pw_res,
        "batch_bail": batch_res,
        "abd_stop": stop_res,
        "abd_kept_custname": kept_name,
        "abd_resume_from_step_4": resume_res,
        "abd_resume_from_step_5_after_direct_op": resume_step5,
    }
    print("[drift]", json.dumps(out, indent=2, ensure_ascii=False), flush=True)
    return out




def run_flights_resume_suite(rounds: int) -> dict:
    """Compare live-session resume (--from-step 12) vs full restart from step 1 on Google Flights."""
    fl_plan_data = json.loads((PLANS_DIR / "flights.json").read_text(encoding="utf-8"))
    broken_data = json.loads(json.dumps(fl_plan_data))
    broken_data["steps"][-1]["target"] = "Find flights"

    results: dict[str, list[dict]] = {
        "flights_stop_step12": [],
        "flights_resume_step12": [],
        "flights_restart_step1": [],
    }
    with tempfile.TemporaryDirectory() as td:
        broken_path = Path(td) / "flights_broken.json"
        fixed_path = Path(td) / "flights_fixed.json"
        broken_path.write_text(json.dumps(broken_data, ensure_ascii=False, indent=2), encoding="utf-8")
        fixed_path.write_text(json.dumps(fl_plan_data, ensure_ascii=False, indent=2), encoding="utf-8")

        for r in range(rounds):
            # Alternate order per round to avoid order bias
            order = ["resume", "restart"] if r % 2 == 0 else ["restart", "resume"]
            for mode in order:
                if mode == "resume":
                    stop_res = run_abd(broken_path, ["--keep-open"])
                    sid = stop_res.get("session")
                    stop_ok = (
                        stop_res.get("stopped_at_step") == 12
                        and stop_res.get("session_open") is True
                        and bool(sid)
                    )
                    results["flights_stop_step12"].append({**stop_res, "ok": stop_ok})
                    try:
                        if sid and stop_ok:
                            res = run_abd(
                                fixed_path,
                                ["--session", sid, "--from-step", "12", "--close"],
                            )
                        else:
                            res = {"ok": False, "wall_s": 0.0, "error": "did not stop cleanly at step 12"}
                        results["flights_resume_step12"].append(res)
                        print(
                            f"[flights_resume R{r + 1}] stop={stop_res['wall_s']}s "
                            f"resume_step12: ok={res['ok']} wall_s={res['wall_s']}",
                            flush=True,
                        )
                    finally:
                        if sid:
                            subprocess.run(
                                ["agent-browser", "--session", sid, "close"],
                                capture_output=True,
                                check=False,
                            )
                else:
                    res = run_abd(fixed_path)
                    results["flights_restart_step1"].append(res)
                    print(
                        f"[flights_resume R{r + 1}] restart_step1: ok={res['ok']} wall_s={res['wall_s']}",
                        flush=True,
                    )
    return {"rounds": rounds, "summary": summarize(results), "raw": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["main", "reuse", "drift", "flights_resume", "all"], default="all")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--chrome", type=str, default=DEFAULT_CHROME)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    ver = subprocess.run(
        ["agent-browser-decisions", "--version"], capture_output=True, check=False, text=True
    ).stdout.strip()
    out: dict = {"chrome": args.chrome, "abd_version": ver}
    if args.suite in ("main", "all"):
        out["main"] = run_main_suite(args.rounds, args.chrome, sys.executable)
    if args.suite in ("reuse", "all"):
        out["reuse"] = run_reuse_suite(args.rounds)
    if args.suite in ("drift", "all"):
        out["drift"] = run_drift_suite(args.chrome, sys.executable)
    if args.suite in ("flights_resume", "all"):
        out["flights_resume"] = run_flights_resume_suite(args.rounds)

    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved results to {args.out}")


if __name__ == "__main__":
    main()
