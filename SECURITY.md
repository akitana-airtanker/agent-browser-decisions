# Security & Operational Boundaries

This document separates what the `agent-browser-decisions` CLI enforces in code from what operators and agent skills must enforce in workflow rules.

## Summary: CLI-enforced vs operator-enforced

| Enforced by CLI (`agent-browser-decisions`) | Enforced by operator / agent skill |
| --- | --- |
| Tier 2 (Decisions API) is **disabled by default** — no external API calls unless explicitly opted in | Human review & approval of `steps`, `verify`, and recovery diffs on logged-in or side-effect pages |
| `--expect-url` checks current tab URL before Step 1 or `--from-step N` (stops without acting on mismatch) | Never put irreversible actions (payment, publish, order submission, permanent delete) in plans; confirm with a human and click once directly |
| `value_env` keeps secrets out of plan JSON and redacts known secret strings (`[REDACTED]`) from structured outputs and Tier 2 payloads | Only known resolved secret strings are redacted; `stopped_snapshot` and Tier 2 payloads (candidate labels, visible values, attributes, surrounding context) can still include **unknown page secrets, tokens, or PII**. Never share JSON output unconditionally, and verify page contents and destination provider/endpoint before enabling Tier 2 |
| Implicit `./.env` loading is restricted to an explicit allowlist of API keys, `DECISION_PROVIDER`, and `DECISION_MODEL` | Never run `agent-browser` / `agent-browser-decisions` inside an untrusted directory containing `./agent-browser.json` |
| `click` / `press` / `upload` are single-shot on the same element by default (`retry: false`); `once: true` disables all retries | Resume safety: inspect actual applied state before choosing `--from-step`, and never resume across non-contiguous partial batch writes |

---

## Detailed boundaries

### 1. Tier 2 opt-in & provider verification status
- Tier 2 (external Decisions API) is **off by default**. Even if API keys exist in the environment, `run` never sends URLs, top-level `goal`, step `describe` (or `target` when `describe` is omitted), candidate labels, visible input values, element attributes, or surrounding context to external endpoints unless one of `--decisions`, `--provider`, `--model`, `--base-url`, or `--mode decide` is passed. Because redaction only scrubs known resolved secret strings (`value_env` / `secret: true`), **unknown secrets, tokens, or PII already present on the page can be sent to the external API** when Tier 2 is enabled; confirm both the page contents and the destination provider/endpoint before opting in.
- `--mode decide` implicitly enables Tier 2 and skips Tier 1 (if exactly one compatible candidate exists, it is selected without label check or API call). Step-level `"decide": true` alone does not enable Tier 2 without an opt-in CLI flag.
- **Verification status (`v0.9.10`)**: All 5 provider wire formats (`openai`, `openrouter`, `gemini`, `typesafe`, `liquid`) — including the OpenAI Decisions GA schema — are validated with mock and property tests (`tests/test_core.py`, `tests/test_lifecycle.py`). Live remote endpoint checks were not re-run in `v0.9.10`.

### 2. Implicit `.env` allowlist & untrusted directories
- When `--env-file` or `AGENT_BROWSER_DECISIONS_ENV_FILE` is specified explicitly, **only that file** is loaded (with all keys allowed, without falling back to `./.env` and without overriding existing shell environment variables).
- When neither is specified, implicit `.env` discovery (`./.env` then repo-root `.env`) loads **only** `IMPLICIT_DOTENV_ALLOWED_KEYS` (known API keys, `DECISION_PROVIDER`, `DECISION_MODEL`). `AGENT_BROWSER_*` and `*_BASE_URL` are ignored during implicit loading.
- **Caveats**:
  - An invalid `DECISION_PROVIDER` value in an implicit `./.env` causes `run` to fail closed with exit code `2` even when Tier 2 is disabled.
  - If you run with `--decisions` inside an untrusted directory and have no matching key already set in your shell environment, a key in `./.env` could be used to send candidate data under that key.
- **`./agent-browser.json` (outside CLI control)**: `agent-browser` itself automatically reads `./agent-browser.json` in the current working directory (project-level browser settings). Do not run browser commands inside untrusted cloned repositories.

### 3. `--expect-url` matching semantics
- Verified **once** before executing the first step of a run (before Step 1 with `--no-open`, immediately after `open` on fresh runs, or before Step `N` on `--from-step N`). It does not prevent the initial `open` when `--no-open` is omitted, nor does it verify post-step navigation targets or SPA modal/tab state on the same URL.
- **Comparison rule**: case-insensitive equality on scheme and authority with explicit ports kept distinct (e.g., explicit `:443` and omitted `:443` do **not** match), path-segment boundary prefix match (e.g., `/orders` matches `/orders`, `/orders/123`, and `/orders/123/delete`, but not `/orders-delete`), and exact match on query/fragment **only when specified** in `--expect-url` (omitting query/fragment does not assert that they are empty).

### 4. Secret redaction (`value_env` & `secret: true`)
- Values loaded via `value_env` default to `secret: true` (unless explicitly set to `"secret": false`).
- Known secret strings (including prior-step `value_env` secrets whose environment variables remain set during `--from-step`) are replaced with `[REDACTED]` in:
  - error output, `stopped_snapshot`, `verify_value` (including nested dict keys/values and list items), `url`, `steps[].chose`, and Tier 2 candidate payloads.
- **Not redacted / remaining exposure**:
  - `steps[].target`, `steps[].reason`, and API-returned `confidence` are not globally redacted.
  - Single-step (non-batched) `fill` passes the value as a child-process argument to `agent-browser` (batched writes pass JSON over stdin).
  - Never pass secrets via `--var`: values remain in shell history, CLI process arguments, and `verify` child-process arguments (even when expanded into a `secret: true` fill step, redaction applies only to structured output JSON). Switching to direct `agent-browser` commands also bypasses redaction.
  - Values reformatted by the browser (e.g., masked/spaced differently) may not match string replacement. Do not treat the entire JSON result as guaranteed secret-free for unrestricted sharing.

### 5. `verify` JavaScript & prompt injection
- `verify` executes arbitrary JavaScript in the page's origin (equivalent to same-origin XSS in a logged-in session). Read-only behavior is a convention, not enforced by a sandbox. During tail Tier-1 batch fusion (`append_batch_tail`), if the readback commands succeed, `verify` can be evaluated before Python checks whether readback values matched (and may also run repeatedly during polling); always keep `verify` side-effect free.
- Because untrusted page text (user names, tickets, comments) appears verbatim in `stopped_snapshot`, always have a human review `steps`, `verify`, and any recovery diff (`target`, `--from-step N`, `--expect-url`) before running or resuming on logged-in or side-effecting pages.

### 6. Irreversible actions, `once: true`, and normalization edge cases
- `click`, `press`, and `upload` are single-shot on the same element by default (`retry: false`). `"once": true` further disables batch fusion and Tier 2 alternate-ref retries, but **cannot guarantee server-side idempotency**. Keep irreversible submissions out of plan files; stop before the final button, confirm with a human, and click once directly.
- Tier 1 normalizes case, Unicode NFKC width, and Latin diacritics. If the original element disappears and another element whose normalized label collides remains as the sole match, Tier 1 will match it.

### 7. Session/profile cleanup & Resume 3 Principles
- On failure, `reset: true` keeps the browser session open (`session_open: true`) and returns the startup-created temporary `profile` path in JSON. Note that the headless `agent-browser` daemon automatically closes idle sessions after 1 hour by default (adjust via `--idle-timeout` / `AGENT_BROWSER_IDLE_TIMEOUT_MS` when human approval may take longer). On automatic close (e.g., `reset: true` success or `--close`), `agent-browser-decisions` verifies both `close` exit status and closure in the same daemon registry (`session info --json` with `active: false`, `pid: null`, readable `socketDir`, matching `namespace`, plus `session list --json` string-array absence) before removing only the startup-created temporary `profile` (leaving `profile` in the returned JSON if closure cannot be confirmed or removal fails). When cleaning up manually, close only your own session (`agent-browser --session <id> close` returning `0`), confirm closure via the same registry checks (`session info --json` + `session list --json`), and remove only the startup-recorded temporary `profile` with `rm -rf`. Never run `agent-browser close --all`.
- **Stopping is a pause, not a rollback.** A step that timed out or reported `no_effect` may still have been processed server-side. Moreover, `stopped_at_step` reports the first unconfirmed step, **not** a guarantee that every later step was untouched (for example, after an internal batch partial failure, step `N` may be unapplied while step `N+1` was already applied).
- Before resuming, inspect `stopped_snapshot` and actual target state, then apply:
  1. **Unapplied & safe from `N` onward**: Confirm that **every step from `N` onward** is safe to execute (no later step was already applied with side effects), then review diffs and resume with `--from-step N --expect-url "<intended-url>"` (add `--no-open` if `N = 1`).
  2. **Applied & safe from `N+1` onward**: If step `N` is already applied and remaining steps from `N+1` onward are all safe to execute, resume with `--from-step $((N+1)) --expect-url "<intended-url>"`. If the last step is applied or only `verify` failed (`stopped_at_step: null`), do not re-run steps — verify completion and close.
  3. **Non-contiguous partial state or unknown side effect**: If step `N` is unapplied while a later step (`N+1`, etc.) is already applied, **do not use `--from-step N`** (it executes every step sequentially from `N` onward and would re-run already-applied steps). Switch to direct `agent-browser` commands or a trimmed plan containing only the remaining unapplied steps. If side effects cannot be determined, stop and ask a human.
