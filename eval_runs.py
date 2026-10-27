"""uv run --env-file ../jev-ultrafast/.env python eval_runs.py --mode label|decide --runs 3 [--task X]"""

import argparse
import json
import time
from pathlib import Path

from ab_decide import ab, run_plan

FLIGHTS_JS = """(() => {
  const u = new URL(location.href);
  let decoded = '';
  try { decoded = atob((u.searchParams.get('tfs') || '').replace(/-/g, '+').replace(/_/g, '/')); } catch (e) {}
  const flights = [...document.querySelectorAll('[aria-label]')].map(e => e.getAttribute('aria-label'))
    .filter(l => l.includes('Select flight') && l.includes('Leaves'));
  const from = document.querySelector('input[aria-label="Where from?"]')?.value;
  const to = document.querySelector('input[aria-label^="Where to?"]')?.value;
  const oneWay = [...document.querySelectorAll('[role=combobox]')].some(e => (e.textContent || '').includes('One way'));
  const checks = {search_page: u.pathname === '/travel/flights/search', one_way: oneWay, origin: from === 'Zürich',
    destination: to === 'London', date: decoded.includes('2026-11-20'),
    results: flights.length > 0 && flights.every(l => l.includes('November 20'))};
  const siteError = /Oops, something went wrong|No results returned/.test(document.body.innerText);
  return {passed: Object.values(checks).every(Boolean), checks, flights: flights.length, site_error: siteError};
})()"""

FORM_JS = """(() => {
  let form = {};
  try { form = JSON.parse(document.body.innerText).form || {}; }
  catch (e) { return {passed: false, checks: {json: false}}; }
  const t = [].concat(form.topping || []);
  const checks = {name: form.custname === 'Taro Test', tel: form.custtel === '0312345678',
    email: form.custemail === 'taro@example.com', size: form.size === 'medium',
    toppings: t.length === 2 && t.includes('bacon') && t.includes('onion'), comments: form.comments === 'Leave at the door'};
  return {passed: Object.values(checks).every(Boolean), checks};
})()"""


def S(action, target, value=None, **extra):
    return {"action": action, "target": target, **({"value": value} if value is not None else {}), **extra}


TASKS = {
    "flights": {
        "url": "https://www.google.com/travel/flights?hl=en",
        "goal": "One-way flight search from Zurich to London on Friday, November 20, 2026.",
        "steps": [
            S("click", "Change ticket type.", role="combobox"),
            S("click", "One way", role="option"),
            S("click", "Where from?", role="combobox"),
            S("fill", "Where else?", "Zurich"),
            S("click", "Zürich, Switzerland", role="option"),
            S("click", "Where to?", role="combobox"),
            S("fill", "Where to?", "London"),
            S("click", "London, United Kingdom", role="option"),
            S("click", "Departure", role="textbox"),
            S("click", "Friday, November 20, 2026", role="button"),
            S("click", "Done.", role="button"),
            S("click", "Search", role="button", expect="navigation"),
        ],
        "verify": FLIGHTS_JS,
    },
    "form": {
        "url": "https://httpbin.org/forms/post",
        "goal": "Order a medium pizza with bacon and onion for Taro Test.",
        "steps": [
            S("fill", "Customer name:", "Taro Test"),
            S("fill", "Telephone:", "0312345678"),
            S("fill", "E-mail address:", "taro@example.com"),
            S("check", "Medium", role="radio"),
            S("check", "Bacon", role="checkbox"),
            S("check", "Onion", role="checkbox"),
            S("fill", "Delivery instructions:", "Leave at the door"),
            S("click", "Submit order", role="button", expect="navigation"),
        ],
        "verify": FORM_JS,
    },
    "hn": {
        "url": "https://news.ycombinator.com/",
        "goal": "Open the discussion page of the third story on the front page.",
        "prepare": "document.querySelectorAll('tr.athing')[2].id",
        "steps": [
            S(
                "click",
                "the comments link (e.g. '23 comments') of the THIRD story in the list",
                role="link",
                decide=True,
            )
        ],
        "verify": "(expected) => ({passed: location.pathname === '/item' && "
        "new URLSearchParams(location.search).get('id') === expected})",
    },
    "github": {
        "url": "https://github.com/browser-use/browser-use",
        "goal": "List the repository's closed pull requests.",
        "steps": [S("click", "Pull requests", role="link"), S("click", "Closed", role="link")],
        "verify": "(() => { const q = decodeURIComponent(location.search).replace(/\\+/g, ' ');"
        " return {passed: location.pathname.endsWith('/pulls') && /(is|state):closed/.test(q)}; })()",
    },
    "dropdown": {
        "url": "https://the-internet.herokuapp.com/",
        "goal": "Select Option 2 on the Dropdown example page.",
        "steps": [
            S("click", "Dropdown", role="link"),
            S("select", "the Dropdown List select box", "Option 2", decide=True),
        ],
        "verify": "(() => ({passed: location.pathname === '/dropdown' && "
        "document.querySelector('#dropdown')?.value === '2'}))()",
    },
    "wiki": {
        "url": "https://en.wikipedia.org/wiki/Main_Page",
        "goal": "Search Wikipedia for Alan Turing, then open the Turing machine article from his article.",
        "steps": [
            S("fill", "Search Wikipedia", "Alan Turing"),
            S("click", "Search", role="button", expect="navigation"),
            S("click", "Turing machine", role="link"),
        ],
        "verify": "(() => ({passed: location.pathname === '/wiki/Turing_machine'}))()",
    },
}


def evaluate(expression):
    out = ab("eval", expression, check=False).strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"passed": False, "raw": out[:200]}


def verify(task, expected, budget_s=20.0):
    expression = task["verify"]
    if expression.startswith("(expected)"):
        expression = f"({expression})({json.dumps(expected)})"
    started = time.perf_counter()
    while True:
        value = evaluate(expression)
        value = value if isinstance(value, dict) else {"passed": False, "raw": value}
        if value.get("passed") or time.perf_counter() - started >= budget_s:
            return value, round((time.perf_counter() - started) * 1000)
        time.sleep(0.5)


def run_once(name, task, mode):
    ab("open", task["url"])
    ab("wait", "1500")
    expected = evaluate(task["prepare"]) if task.get("prepare") else None
    started = time.perf_counter()
    log, completed = run_plan(task["steps"], task["goal"], mode)
    exec_ms = round((time.perf_counter() - started) * 1000)
    result, verify_ms = verify(task, expected)
    totals = {}
    for entry in log:
        for category, ms in entry.get("metrics", {}).get("ms", {}).items():
            totals[category] = totals.get(category, 0) + ms
    checks = result.get("checks") or {}
    operations_ok = bool(checks) and all(v for k, v in checks.items() if k != "results")
    if result.get("passed"):
        outcome = "passed"
    elif result.get("site_error") and operations_ok:
        outcome = "site_error"
    else:
        outcome = "failed"
    return {
        "task": name,
        "mode": mode,
        "passed": bool(result.get("passed")),
        "outcome": outcome,
        "completed": completed,
        "breakdown_ms": totals,
        "tokens": sum(e.get("metrics", {}).get("tokens", 0) for e in log),
        "exec_ms": exec_ms,
        "verify_wait_ms": verify_ms,
        "verify": result,
        "tiers": [e["tier"] for e in log],
        "decisions": sum(e["tier"] == 2 and not e.get("single") for e in log),
        "log": log,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["label", "decide"], default="label")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--task", action="append", choices=sorted(TASKS))
    parser.add_argument("--output", default="results.jsonl")
    args = parser.parse_args()
    for name in args.task or list(TASKS):
        for index in range(args.runs):
            try:
                row = {"run": index + 1, **run_once(name, TASKS[name], args.mode)}
            except Exception as error:  # noqa: BLE001 - one failed run must not stop the evaluation
                row = {"run": index + 1, "task": name, "mode": args.mode, "passed": False, "error": str(error)[:300]}
            with Path(args.output).open("a") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"{name:9} {args.mode:6} run{row['run']} {row.get('outcome', 'error'):10} "
                f"exec={row.get('exec_ms', 0) / 1000:5.1f}s tiers={row.get('tiers')} err={row.get('error')}",
                flush=True,
            )


if __name__ == "__main__":
    main()
