# Benchmarks

Same-endpoint comparison scripts for `agent-browser-decisions`, `Playwright` (Python `sync_playwright`), and wait-tuned `agent-browser batch --bail`.

## Measurement scope

Every method in the `main` suite is measured end-to-end as wall-clock time from process launch through browser startup, task execution, identical JavaScript `verify` evaluation, and clean browser close, with `Playwright` using local Chrome headless (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`) and `agent-browser` / `agent-browser-decisions` using `agent-browser`'s default headless Chromium. Execution order rotates across methods on every round to avoid order bias.

### Tasks

1. **Static 7-field form (`https://httpbin.org/forms/post`, no submit)**
   - Identical 7-field `verify` expression confirming all input values, checked radio label, and exact count/labels of checked toppings.
   - Plans & scripts: `plans/httpbin.json`, `scripts/pw_httpbin.py`, and `httpbin_batch_cmds()` in `run_bench.py`.
2. **Dynamic SPA (`https://www.google.com/travel/flights?hl=en`, 12 steps)**
   - Identical 4-part `verify` expression confirming navigation to `/travel/flights/search`, `Where from?` containing `Zürich`, `Where to?` containing `London`, `Departure` containing `Dec 15`, and no visible `Return` field (one-way).
   - Plans & scripts:
     - `plans/flights.json`: 12 declarative steps, 0 waits, 0 CSS selectors.
     - `scripts/pw_flights.py`: tuned for 5/5 reliability with explicit waits (`tfs=` query wait, visible/hidden popup waits, `data-iso` date cell + Departure value reflection check, `[role="dialog"]:visible` hidden wait, and 1 retry on Search re-render) and DOM-specific visible locators.
     - `flights_batch_cmds()` in `run_bench.py`: 21 commands including 6 hand-tuned `wait` steps (`wait --text` ×4, fixed `wait 600`, `wait --fn`).

### Suites

- `main`: 3-way rotating benchmark on `httpbin` and Google Flights (`--rounds 5`).
- `reuse`: `agent-browser-decisions` CLI cold start vs `--keep-open` + `--reload` vs `--no-open --close` on `httpbin`.
- `drift`: Label-drift test changing `httpbin` step 4 to non-existent `"Extra Large"` across Playwright (default `timeout=3000`, without custom recovery wrapper), `agent-browser batch --bail` (verifying whether the daemon session still holds step 1–3 input), and `agent-browser-decisions` (`stopped_at_step`, `stopped_snapshot`, and `--from-step 4` / `--from-step 5` resume).

## Running

```bash
uv run --with playwright python benchmarks/run_bench.py --suite all --rounds 5 --out benchmarks/results-v0.6.0.json
```

> Note: Keep automated requests to third-party public pages (such as Google Flights) to a small number of benchmark runs and respect each site's terms of service in production use.
