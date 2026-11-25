# agent-browser-decisions

[![CI](https://github.com/akitana-airtanker/agent-browser-decisions/actions/workflows/ci.yml/badge.svg)](https://github.com/akitana-airtanker/agent-browser-decisions/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Deterministic, verifiable browser automation on top of [agent-browser](https://github.com/vercel-labs/agent-browser) and decision models (OpenAI Decisions API, OpenRouter Decisions API, Google Gemini Decisions API, TypeSafe AI Jev, and Liquid AI d1 / d1.2).

Write a declarative JSON plan once, validate it before touching a browser, and execute it step by step:

```text
resolve → act → check effect → verify
  tier 1  deterministic label / regex / nth match on the accessibility snapshot (no model, no API key)
  tier 2  (auto when API key set; --no-decisions to disable) Bounded decision model with workflow context,
          majority probability gate (p >= 0.50), deterministic safety/ambiguity guards, and optional --heal
  tier 3  unresolved or failed effect → stop cleanly with structured JSON and hand back to caller
```

The model only ever chooses an observed `ref`. It never emits selectors, text, or code.

## Quick start

### Prerequisites

- [agent-browser](https://github.com/vercel-labs/agent-browser) on `PATH` (`npm i -g agent-browser && agent-browser install`)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Optional Tier 2 API key (`OPENROUTER_API_KEY`, `LIQUID_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `TYPESAFE_API_KEY`) — Tier 2 is **auto-enabled when a provider key is configured** (skipped cleanly with zero API calls when no key is set, or disabled via `--no-decisions`). Default OpenRouter/Liquid model is `liquid/d1.2-ultra` / `d1.2-ultra`

### Option A: Install globally as a CLI

```bash
uv tool install git+https://github.com/akitana-airtanker/agent-browser-decisions.git
agent-browser-decisions doctor

# Validate & run a Tier 1 plan from stdin (no API key required)
agent-browser-decisions run - <<'EOF'
{
  "url": "https://quotes.toscrape.com/",
  "reset": true,
  "steps": [
    {"action": "click", "target": "inspirational", "role": "link", "nth": 1},
    {"action": "click", "target": "Next", "role": "link"}
  ],
  "verify": "location.pathname === '/tag/inspirational/page/2/'"
}
EOF
```

### Option B: Run from a local clone

```bash
git clone https://github.com/akitana-airtanker/agent-browser-decisions.git
cd agent-browser-decisions && uv sync

uv run agent-browser-decisions doctor
uv run agent-browser-decisions validate examples/quotes.json
uv run agent-browser-decisions run examples/quotes.json
```

## Plan format

Plans are JSON files validated against [`schema/plan.schema.json`](schema/plan.schema.json) (reference it via `"$schema"` for editor autocomplete):

```json
{
  "$schema": "./schema/plan.schema.json",
  "url": "https://www.saucedemo.com/",
  "goal": "Log in with credentials from environment variables and open the cart.",
  "reset": true,
  "steps": [
    {"action": "fill", "target": "Username", "value_env": "SAUCEDEMO_USER", "secret": false},
    {"action": "fill", "target": "Password", "value_env": "SAUCEDEMO_PASSWORD"},
    {"action": "click", "target": "Login", "role": "button", "expect": "navigation", "once": true},
    {"action": "click", "target": "Cart", "describe": "the shopping cart icon in the header"}
  ],
  "verify": "location.pathname === '/cart.html'"
}
```

See [`examples/`](examples/) for a self-contained local 3-screen wizard demo (`wizard/` + `wizard-demo.sh`), a strict `httpbin` demo (`httpbin-demo-strict.sh`), and ready-to-run plans (`quotes.json`, `saucedemo-login.json`, `hn-positional.json`).

### Step fields

| Field | Applies to | Description |
| --- | --- | --- |
| `action` | all (required) | `click`, `fill`, `select`, `check`, `press`, or `upload` |
| `target` | all except `press` | Accessible label from `agent-browser snapshot -i` (Unicode NFKC exact match by default; use `match: "prefix"` or `match: "regex"` to opt in) |
| `value` | `fill`, `select`, `press`, `upload` | Literal text to enter, option to select, key name to press, or local file path to upload |
| `value_env` | `fill` (alt. to `value`) | Environment variable name resolved at runtime; missing vars fail fast (`exit 2`) before opening a browser |
| `secret` | `fill` | Redact the value (`[REDACTED]`) from error output, `stopped_snapshot`, `verify_value`, `url`, `steps[].chose`, and Tier 2 candidate payloads (including prior steps on `--from-step` when env vars remain set). Defaults to `true` when `value_env` is used |
| `role` | all except `press` | Expected ARIA role (must be compatible with `action`); also waits up to 3 s for that role to appear. Omit for unseen elements |
| `nth` | all except `press` | 1-based positional pick among matching elements. Never falls back to Tier 2 or unrelated elements |
| `match` | all except `press` | `"exact"` (default), `"prefix"` (normalised prefix match for dynamic label suffixes), or `"regex"` (case-insensitive regex against raw names) |
| `describe` | all except `press` | Human-readable hint sent to Tier 2 when disambiguating |
| `expect` | `click`, `press`, `upload` | `"navigation"` to require a document URL change (ignored on `fill`, `check`, `select`) |
| `navigates` | `click` on `link` | Set `false` for SPA links (modals, tabs) that do not change the URL |
| `once` | all | Execute at most once across all tiers (also disables Tier 2 alternate-ref retries and `fill`/`check` retries; cannot combine with `retry: true`) |
| `retry` | all except `press` | Opt-in same-element retry (`true` enables 1 retry on `no_effect`, or keyboard `focus` + `Enter` once on a wrapped `link`). Default is `false` for `click`/`upload`/`press` (at-most-once on the same element) and `true` for `fill`/`check`/`select` |
| `decide` | all except `press` | `true` skips Tier 1 and sends candidates directly to Tier 2 (cannot combine with `nth` or `match`) |
| `timeout_s` | all except `press` | Optional per-step override (seconds, `>= 0`) for element appearance/settle wait |

### Top-level fields

- `steps` (required): non-empty list of step objects.
- `url`: initial URL to open before step 1 (required when `reset: true`; omit to continue from the current page in `--session`).
- `goal`: context string passed to Tier 2.
- `vars`: optional object of default template variables (`{{key}}` in `url`/`goal`/`target`/`value`/`describe`, exposed as `VARS` in `verify`; overridden by CLI `--var key=value`).
- `reset`: `true` launches a fresh isolated session with Chrome's password manager disabled (attempts to close and delete the temp profile on success unless `--keep-open` is passed, and on caught `SIGINT`/`SIGTERM` even with `--keep-open`; kept open on failure for takeover). When reusing an existing `--session`, pass `--no-open` (keep current page) or `--reload` (reload `url` inside the warm Chrome without relaunching).
- `settle_s`: optional max seconds to wait for each element to appear and settle before failing to Tier 2/3 (default `3.0`; `0` fails fast on the first snapshot). Individual non-`press` steps can also set `timeout_s` to override.
- `verify`: JavaScript expression polled for up to `verify_timeout_s` (default `10`). JSON-serialisable truthy results (including `{}` and `[]`) or `{"passed": true}` pass; non-JSON output (`undefined`, `NaN`, syntax errors), non-zero exits, and `{"passed": false}` fail.

## Output & takeover

`run` prints structured JSON to stdout. On success with `reset: true`, the runner attempts to close the session automatically (`session_open: false`; if `close` itself fails, `ok: true` and `session_open: true` can coexist). On failure, the session stays open (`session_open: true`) so you or an agent can inspect actual page state and decide how to continue:

```json
{
  "ok": false,
  "session": "abd-1763574000000-12345",
  "session_open": true,
  "verified": null,
  "verify_value": null,
  "stopped_at_step": 4,
  "error": null,
  "elapsed_ms": 2140,
  "url": "https://www.saucedemo.com/inventory.html",
  "steps": [
    {"step": 1, "tier": 1, "tiers": [1], "target": "Username", "chose": "Username", "effect": "ok", "ms": 310},
    {"step": 4, "tier": 3, "tiers": [3], "target": "Cart", "chose": null, "effect": null, "reason": "no candidates", "ms": 820}
  ]
}
```

Before resuming, confirm the actual applied state from `stopped_snapshot` (especially after partial batch writes). Use `--from-step N` only when every step from `N` onward is safe to run — if step `N` is unapplied while a later step (`N+1`, etc.) was already applied, finish only the unapplied steps directly or with a trimmed plan (see [`SECURITY.md`](SECURITY.md) and [`docs/sessions.md`](docs/sessions.md)):

```bash
# Option 1: inspect live state first, then act using the ref from that fresh snapshot
agent-browser --session abd-1763574000000-12345 snapshot -i
# Confirm the step is still unapplied (and get human approval on side-effecting pages),
# then replace <ref> with the element ref shown above:
agent-browser --session abd-1763574000000-12345 click @<ref>
agent-browser --session abd-1763574000000-12345 close

# Option 2: fix the plan and resume from step 4 in the same session
agent-browser-decisions run plan.json --session abd-1763574000000-12345 --from-step 4 --expect-url "https://example.com/form"
```

## CLI reference

```bash
agent-browser-decisions doctor [--env-file PATH] [--provider PROVIDER] [--model MODEL] [--base-url URL]
agent-browser-decisions validate <plan.json | -> [--var KEY=VALUE]...
agent-browser-decisions run <plan.json | -> [--decisions | --no-decisions] [--heal] [--mode label|decide] [--session NAME] [--from-step N] \
                                            [--expect-url URL_PREFIX] [--var KEY=VALUE]... [--no-open] [--reload] [--keep-open] [--close] \
                                            [--provider openai|openrouter|gemini|typesafe|liquid] [--model MODEL] [--base-url URL] \
                                            [--env-file PATH] [--settle-s 3.0] [--timeout 60] [--api-timeout 30]
agent-browser-decisions profile
agent-browser-decisions --version
python -m agent_browser_decisions --help
```

### Exit codes

- `0` — all steps ran and verified (`verified: null` when `verify` is omitted), or `validate` / `doctor` passed.
- `1` — step failure, `verify` failure, runtime/browser/API error, interrupt, or `doctor` missing `agent-browser`.
- `2` — invalid plan JSON, schema violation (checked by `validate_plan`, matching `schema/plan.schema.json`), undefined `{{var}}` (when `vars` or `--var` is present), incompatible CLI options (`reset: true` + `--session` without `--no-open`/`--reload`, `--from-step > 1` / `--no-open` / `--close` without `--session`), or missing `value_env`.

### Environment variables

`run` and `doctor` load `--env-file` (or `AGENT_BROWSER_DECISIONS_ENV_FILE`, else `./.env`, else repo `.env`) without overriding variables already set in the shell environment.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | OpenAI Decisions API key (`/v1/decisions`, default model `gpt-6-luna`) |
| `OPENROUTER_API_KEY` | — | OpenRouter Decisions API key (`/api/alpha/decisions`, default `liquid/d1.2-ultra`; supports `liquid/d1.2`, `google/gemini-decision-1`, `openai/gpt-6-luna-decisions`, `typesafe/jev-1.14`, etc.) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Google Gemini Decisions API key (`/v1beta/decisions`, default model `gemini-decision-1`) |
| `TYPESAFE_API_KEY` | — | TypeSafe AI System One API key (`/v1/systemone`, default model `jev-latest`) |
| `LIQUID_API_KEY` | — | Liquid AI System One API key (`/decisions/v1/systemone`, default model `d1.2-ultra`) |
| `DECISION_PROVIDER` | auto | `openai`, `openrouter`, `gemini`, `typesafe`, or `liquid` (auto-detected from `--model` / available key when omitted; `vendor/model` IDs infer `openrouter`) |
| `DECISION_MODEL` | per provider | Override Tier 2 model ID (e.g. `gpt-6-luna`, `gemini-decision-1`, `jev-latest`, `d1.2-ultra`) |
| `DECISION_API_KEY` | — | Generic Tier 2 API key fallback when a provider-specific key is not set |
| `DECISION_BASE_URL` | per provider | Override Tier 2 base URL or full `/decisions` / `/systemone` endpoint (e.g. OpenRouter or self-hosted `d1`) |
| `AGENT_BROWSER_DECISIONS_ENV_FILE` | — | Default `.env` file path when `--env-file` is omitted |
| `AGENT_BROWSER_SESSION` | `default` | Fallback session name used by `agent-browser` when neither `reset: true` nor `--session` is set |

### Supported Tier 2 decision providers

| Provider | Protocol | Default model | Endpoint | Auth header | Abstention |
| --- | --- | --- | --- | --- | --- |
| `openai` | OpenAI Decisions (GA) | `gpt-6-luna` | `https://api.openai.com/v1/decisions` | `Authorization: Bearer` | Explicit `__none__` option + `refusal` |
| `openrouter` | System One | `liquid/d1.2-ultra` | `https://openrouter.ai/api/alpha/decisions` | `Authorization: Bearer` | Explicit `__none__` option + `refusal` |
| `gemini` | Gemini Decisions | `gemini-decision-1` | `https://generativelanguage.googleapis.com/v1beta/decisions` | `x-goog-api-key` | Explicit `__none__` option + `prompt_feedback` |
| `typesafe` | System One | `jev-latest` | `https://api.typesafe.ai/v1/systemone` | `Authorization: Bearer` | Explicit `__none__` option |
| `liquid` | System One | `d1.2-ultra` | `https://api.liquid.ai/decisions/v1/systemone` | `Authorization: Bearer` | Explicit `__none__` option |

> Note: OpenRouter (`liquid/d1.2-ultra`, `liquid/d1.2`, `openai/gpt-6-luna-decisions`, `microsoft/microsoft-decision-1.1`, `google/gemini-decision-1`, `typesafe/jev-1.14`) is verified against live endpoints (28-case benchmark + live 3-screen wizard E2E); all 5 provider wire formats are also covered by mock and property tests.

## Effect checks

Non-`press` steps wait for a settled page (two consecutive identical snapshots, or a single match when `settle_s` expires; post-effect snapshots may be reused across steps). Exhausted `no_effect` or `wrong_target` steps stop the plan with `ok: false` at `stopped_at_step` (see [`docs/sessions.md`](docs/sessions.md) for full lifecycle details).

| Action | Verification | On failure (unless `once: true`) |
| --- | --- | --- |
| `fill` | Field read-back equals expected value (preserves whitespace & newlines) | Repeat once |
| `check` | `is checked` returns `true` | Repeat once |
| `select` | Unchecked (`ok`) — assert selected state in `verify` when critical | — |
| `link` click (`navigates` ≠ `false`), `expect: "navigation"` (`click`/`press`/`upload`) | Document URL changes (or fragment changes for `#route` links) | Stop immediately by default (single-shot on same element). With `retry: true`, tries `focus` + `Enter` once for links or repeats once for non-link `click`/`upload` (`press` disallows `retry`). When Tier 2 chose the ref (unless `once: true`), may exclude ref and try another candidate |
| Other `click`, `upload`, `press` | Snapshot or URL changes within 1 s | Stop immediately by default (`click`/`upload` retries once only when `retry: true`; `press` disallows `retry`) |

## Safety & operational guarantees

- **Guarded Tier 2 & bounded model surface**: Tier 2 auto-enables when a decision API key is configured and can be force-disabled with `--no-decisions` (without a key, runs stay on Tier 1 with zero external calls). Implicit `./.env` loading is restricted to known API keys, `DECISION_PROVIDER`, and `DECISION_MODEL`; pass `--env-file` or `AGENT_BROWSER_DECISIONS_ENV_FILE` explicitly to load all keys. Every Tier 2 choice must pass three layers: (1) workflow-context prompt (prev/next step summaries without raw filled values), (2) majority probability gate (`p >= 0.50` when probabilities are returned), and (3) deterministic guards blocking unrequested destructive/reset/publish/payment escalations and indistinguishable duplicate siblings. Optional `--heal` writes updated labels back to the plan JSON only when a `verify` expression exists and passes, never overwriting `{{var}}` templates, `regex`/`prefix` steps, or secrets. Because redaction only scrubs known resolved `value_env` / `secret: true` strings, unknown page secrets or PII can still appear in candidate payloads — pass `--no-decisions` on pages where external API calls are undesired.
- **Unicode & injection safety**: Normalisation preserves non-ASCII scripts (Japanese, CJK, Cyrillic, Greek, dakuten) and rejects empty labels. Snapshot parsing uses escape-aware quoting so embedded `\" [ref=eN]` strings in page text cannot spoof element refs.
- **Fresh `stopped_snapshot` on stop**: When a run fails after executing an action or on `verify` failure (excluding `--expect-url` mismatches) and the latest observation is stale, the runner re-observes the open session once before returning `stopped_snapshot` (falling back to the last observed snapshot if unreachable) so resuming agents see post-action state.
- **No silent misclicks or double-clicks**: Label misses and out-of-range `nth` never fall back to a sole unrelated element. `click` and `link` steps execute at most once on the same element by default (no automatic `Enter` fallback or second click unless `retry: true`). Pass `--expect-url` with `--from-step` / `--no-open` to abort before touching any step if the live session drifted to another origin or path.
- **Secret hygiene**: `value_env` keeps credentials out of plan JSON; resolved secrets (including prior-step secrets whose env vars remain set on `--from-step`) are redacted (`[REDACTED]`) from error output, `stopped_snapshot`, `verify_value`, `url`, `steps[].chose`, and Tier 2 candidate payloads.
- **Fault tolerance & cleanup**: Tier 2 retries transient HTTP `429` (honouring `Retry-After`), `5xx`, and network/timeout errors with bounded backoff. Caught `SIGINT`/`SIGTERM` attempts to close owned sessions and delete temporary Chrome profiles. When a `reset: true` run stops and keeps the browser open (`session_open: true`), the result JSON includes `profile` with the temporary directory path so callers can delete it after confirming session exit. Full security boundaries live in [`SECURITY.md`](SECURITY.md).
- **Formally checked lifecycle**: Session ownership, profile cleanup, `session_open` reporting, `once` at-most-once execution, and non-silent failure invariants are checked by the Python Kripke/CTL model checker (`tests/test_formal_model.py`) and Hypothesis stateful tests (`tests/test_lifecycle.py`).

## Agent skill

This repository bundles an agent skill at [`skills/agent-browser-decisions/SKILL.md`](skills/agent-browser-decisions/SKILL.md) (in Japanese) for Claude Code, Codex, Pi, and other agents supporting the `SKILL.md` convention. It guides agents to reuse saved plans from trusted locations (`~/.local/share/agent-browser-decisions/plans/` or explicitly verified repo `plans/`) via `--var`, author dynamic-UI plans in stages, and review diffs before updating shared plans after `--from-step` recovery.

```bash
npx skills add akitana-airtanker/agent-browser-decisions
```

Or symlink `skills/agent-browser-decisions` into `~/.agents/skills/agent-browser-decisions` / `~/.claude/skills/agent-browser-decisions`.

## Architecture & development

```text
src/agent_browser_decisions/
├── core.py       # Pure domain logic (parsing, matching, validation, verify, redaction) — zero I/O
├── browser.py    # Session-scoped agent-browser CLI adapter & quiet Chrome profile helper
├── decisions.py  # Context-managed OpenAI / Gemini / System One client with bounded 429/5xx/529 retries
├── runner.py     # PlanRunner orchestrator (resolve → act → effect → verify, SIGINT cleanup)
├── cli.py        # CLI entry point (run, validate, doctor, profile)
└── __main__.py   # `python -m agent_browser_decisions` entry point
```

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

## Limits

- Positional choices ("the third item") should use `nth` rather than relying on the decision model to count.
- Repeated identical labels without per-item names in the snapshot (e.g., identical "Add to cart" buttons) require `nth` or direct `agent-browser` control.
- Cross-origin `frame` switches, inspecting values inside closed Shadow DOM via `verify`, and counting/conditional widgets are best handled directly with `agent-browser` (standard file `upload`, auto-inlined same-origin `iframe`, and open/closed Shadow DOM actions are supported).

## License

MIT

## Warm-session reuse & verification

- **Warm-session reuse**: Pass `--keep-open` on the first run to keep the browser alive, then reuse it with `--session <id> --reload --var ...` (resets page DOM without Chrome cold-start, ~0.38 s/run on 7-field forms) or `--no-open` (same page, ~0.34 s/run), and pass `--close` on the final run.
- **Optimization soundness**: Contiguous retryable Tier-1 form writes (excluding `once: true` and `retry: false`) fuse into `agent-browser batch --bail` (with tail `verify` — keep expressions side-effect free — and `get url`), and dynamic steps forward post-effect snapshots across steps while preserving the 2-consecutive-snapshot stability invariant. `tests/test_formal_model.py` verifies the runner state machine with a Kripke/CTL model checker (safety, termination via Tarjan non-trivial SCC, deadlock freedom) and Hypothesis-driven forward/backward simulation checks against conservative step-by-step execution.
