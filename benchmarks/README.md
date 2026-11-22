# Benchmarks

Same-endpoint comparison scripts for `agent-browser-decisions`, `Playwright` (Python `sync_playwright`), and wait-tuned `agent-browser batch --bail`.

## Measurement scope

Every method in the `main` suite is measured end-to-end as wall-clock time from process launch through browser startup, task execution, identical JavaScript `verify` evaluation, and each method's close command. `Playwright` uses local Chrome headless (`--chrome`, defaulting to `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`), while `agent-browser` / `agent-browser-decisions` use `agent-browser`'s default headless browser (Chrome for Testing or auto-detected local Chrome). Execution order rotates across methods on every round to avoid order bias.

### Tasks

1. **Static 7-field form (`https://httpbin.org/forms/post`, no submit)**
   - Identical 7-field `verify` expression confirming all input values, checked radio label, and exact count/labels of checked toppings.
   - Plans & scripts: `plans/httpbin.json`, `scripts/pw_httpbin.py`, and `httpbin_batch_cmds()` in `run_bench.py`.
2. **Local 3-screen async wizard (`examples/wizard/`, 9 steps, single-use one-time code, no submit)**
   - Localhost-only multi-screen SPA (`examples/wizard/server.py`, 250 ms simulated API latency per transition) that consumes a single-use code on Step 1 and renders Screen 2 & Screen 3 asynchronously.
   - Identical `verify` expression confirming Screen 3 visibility, all 8 inputs, and a live `tok-*` session token.
   - Plans & scripts: `plans/wizard.json`, `scripts/pw_wizard.py`, and `wizard_batch_cmds()` in `run_bench.py`.

### Suites

- `main`: 3-way rotating benchmark on `httpbin` and the local 3-screen `wizard` (`--rounds 5`).
- `reuse`: `agent-browser-decisions` CLI cold start vs `--keep-open` + `--reload` vs `--no-open --close` on `httpbin`.
- `drift`: Label-drift test changing `httpbin` step 4 to non-existent `"Extra Large"` across Playwright (default `timeout=3000`, without custom recovery wrapper), `agent-browser batch --bail` (verifying whether the daemon session still holds step 1–3 input), and `agent-browser-decisions` (`stopped_at_step`, `stopped_snapshot`, and `--from-step 4` / `--from-step 5` resume with `--expect-url`).
- `wizard_resume`: 5 alternating rounds on the local 3-screen wizard where Step 8 (`"Next"` vs `"Continue"`) stops after Step 1 consumed `INVITE-001`, verifying (a) restarting from Step 1 with the same `INVITE-001` fails (HTTP 409), (b) resuming from Step 8 with `--expect-url` and `--close` succeeds in the live session, and (c) timing against a full Step-1 restart with a fresh code.

## Running

```bash
uv run --with playwright python benchmarks/run_bench.py --suite all --rounds 5 --out benchmarks/results-v0.9.10.json
```
