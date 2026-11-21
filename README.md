# agent-browser-decisions

[![CI](https://github.com/akitana-airtanker/agent-browser-decisions/actions/workflows/ci.yml/badge.svg)](https://github.com/akitana-airtanker/agent-browser-decisions/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Deterministic, verifiable browser automation on top of [agent-browser](https://github.com/vercel-labs/agent-browser) and decision models (OpenAI Decisions API, Google Gemini Decisions API, TypeSafe AI Jev, and Liquid AI d1 / d1.2).

Write a declarative JSON plan once, validate it before touching a browser, and execute it step by step:

```text
resolve → act → check effect → verify
  tier 1  deterministic label / regex / nth match on the accessibility snapshot (no model, no API key)
  tier 2  Decision model (OpenAI / Gemini / TypeSafe Jev / Liquid d1) chooses among observed refs (bounded retries on 429/5xx)
  tier 3  unresolved or failed effect → stop cleanly with structured JSON and hand back to caller
```

The model only ever chooses an observed `ref`. It never emits selectors, text, or code.

## Quick start

### Prerequisites

- [agent-browser](https://github.com/vercel-labs/agent-browser) on `PATH` (`npm i -g agent-browser && agent-browser install`)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Any one Tier 2 API key (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `TYPESAFE_API_KEY`, or `LIQUID_API_KEY`) — only needed when a step escalates to Tier 2; Tier 1 plans need no key

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

See [`examples/`](examples/) for ready-to-run plans (`quotes.json`, `saucedemo-login.json`, `hn-positional.json`).

### Step fields

| Field | Applies to | Description |
| --- | --- | --- |
| `action` | all (required) | `click`, `fill`, `select`, `check`, or `press` |
| `target` | all except `press` | Accessible label from `agent-browser snapshot -i` (Unicode NFKC exact → prefix match), or regex when `match: "regex"` |
| `value` | `fill`, `select`, `press` | Literal text to enter, option to select, or key name to press |
| `value_env` | `fill` (alt. to `value`) | Environment variable name resolved at runtime; missing vars fail fast (`exit 2`) before opening a browser |
| `secret` | `fill` | Redact the value (`[REDACTED]`) from any error output. Defaults to `true` when `value_env` is used |
| `role` | all except `press` | Expected ARIA role (must be compatible with `action`); also waits up to 3 s for that role to appear. Omit for unseen elements |
| `nth` | all except `press` | 1-based positional pick among matching elements. Never falls back to Tier 2 or unrelated elements |
| `match` | all except `press` | `"regex"` to match `target` as a case-insensitive regex against raw element names |
| `describe` | all except `press` | Human-readable hint sent to Tier 2 when disambiguating |
| `expect` | all | `"navigation"` to require a document URL change |
| `navigates` | `click` on `link` | Set `false` for SPA links (modals, tabs) that do not change the URL |
| `once` | all | Execute at most once; disables retries and keyboard Enter fallback |
| `decide` | all except `press` | `true` skips Tier 1 and sends candidates directly to Tier 2 (cannot combine with `nth` or `match`) |

### Top-level fields

- `steps` (required): non-empty list of step objects.
- `url`: initial URL to open before step 1 (required when `reset: true`; omit to continue from the current page in `--session`).
- `goal`: context string passed to Tier 2.
- `reset`: `true` launches a fresh isolated session with Chrome's password manager disabled (closed on success or interrupt; kept open on failure for takeover). Cannot combine with `--session`.
- `verify`: JavaScript expression polled for up to `verify_timeout_s` (default `10`). JSON-serialisable truthy results (including `{}` and `[]`) or `{"passed": true}` pass; non-JSON output (`undefined`, `NaN`, syntax errors), non-zero exits, and `{"passed": false}` fail.

## Output & takeover

`run` prints structured JSON to stdout. On success with `reset: true`, the session is closed automatically (`session_open: false`). On failure, the session stays open (`session_open: true`) so you or an agent can inspect and continue from `stopped_at_step`:

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

Continue from the failed step without re-running completed steps:

```bash
# Option 1: inspect & finish directly with agent-browser
agent-browser --session abd-1763574000000-12345 snapshot -i
agent-browser --session abd-1763574000000-12345 click @e12
agent-browser --session abd-1763574000000-12345 close

# Option 2: fix the plan and resume from step 4 in the same session
agent-browser-decisions run plan.json --session abd-1763574000000-12345 --from-step 4
```

## CLI reference

```bash
agent-browser-decisions doctor [--env-file PATH] [--provider PROVIDER] [--model MODEL] [--base-url URL]
agent-browser-decisions validate <plan.json | ->
agent-browser-decisions run <plan.json | -> [--mode label|decide] [--session NAME] [--from-step N] \
                                            [--provider openai|gemini|typesafe|liquid] [--model MODEL] [--base-url URL] \
                                            [--env-file PATH] [--timeout 60] [--api-timeout 30]
agent-browser-decisions profile
agent-browser-decisions --version
python -m agent_browser_decisions --help
```

### Exit codes

- `0` — all steps ran and verified (`verified: null` when `verify` is omitted), or `validate` / `doctor` passed.
- `1` — step failure, `verify` failure, runtime/browser/API error, interrupt, or `doctor` missing `agent-browser`.
- `2` — invalid plan JSON, schema violation, incompatible CLI options (`reset: true` + `--session`, `--from-step > 1` without `--session`), or missing `value_env`.

### Environment variables

`run` and `doctor` load `--env-file` (or `AGENT_BROWSER_DECISIONS_ENV_FILE`, else `./.env`, else repo `.env`) without overriding variables already set in the shell environment.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | OpenAI Decisions API key (`/v1/decisions`, default model `gpt-6-luna`) |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Google Gemini Decisions API key (`/v1beta/decisions`, default model `gemini-decision-1`) |
| `TYPESAFE_API_KEY` | — | TypeSafe AI System One API key (`/v1/systemone`, default model `jev-latest`) |
| `LIQUID_API_KEY` | — | Liquid AI System One API key (`/decisions/v1/systemone`, default model `d1.2`) |
| `DECISION_PROVIDER` | auto | `openai`, `gemini`, `typesafe`, or `liquid` (auto-detected from `--model` / available key when omitted) |
| `DECISION_MODEL` | per provider | Override Tier 2 model ID (e.g. `gpt-6-luna`, `gemini-decision-1`, `jev-latest`, `d1.2-ultra`) |
| `DECISION_BASE_URL` | per provider | Override Tier 2 base URL or full `/decisions` / `/systemone` endpoint (e.g. OpenRouter or self-hosted `d1`) |
| `AGENT_BROWSER_DECISIONS_ENV_FILE` | — | Default `.env` file path when `--env-file` is omitted |
| `AGENT_BROWSER_SESSION` | `default` | Fallback session name used by `agent-browser` when neither `reset: true` nor `--session` is set |

### Supported Tier 2 decision providers

| Provider | Protocol | Default model | Endpoint | Auth header | Abstention |
| --- | --- | --- | --- | --- | --- |
| `openai` | OpenAI Decisions | `gpt-6-luna` | `https://api.openai.com/v1/decisions` | `Authorization: Bearer` | Native `allow_abstention: true` |
| `gemini` | Gemini Decisions | `gemini-decision-1` | `https://generativelanguage.googleapis.com/v1beta/decisions` | `x-goog-api-key` | Explicit `__none__` option + `prompt_feedback` |
| `typesafe` | System One | `jev-latest` | `https://api.typesafe.ai/v1/systemone` | `Authorization: Bearer` | Explicit `__none__` option |
| `liquid` | System One | `d1.2` | `https://api.liquid.ai/decisions/v1/systemone` | `Authorization: Bearer` | Explicit `__none__` option |

## Effect checks

Steps act only on a settled page (two consecutive identical snapshots). Exhausted `no_effect` or `wrong_target` steps stop the plan with `ok: false` at `stopped_at_step`.

| Action | Verification | On failure (unless `once: true`) |
| --- | --- | --- |
| `fill` | Field read-back equals expected value (preserves whitespace & newlines) | Repeat once |
| `check` | `is checked` returns `true` | Repeat once |
| `select` | Unchecked (`ok`) — assert selected state in `verify` when critical | — |
| `link` click (`navigates` ≠ `false`), `expect: "navigation"` | Document URL changes (or fragment changes for `#route` links) | Keyboard `focus` + `Enter`, then repeat once (Tier 1) or exclude ref (Tier 2) |
| Other `click`, `press` | Snapshot or URL changes within 1 s | Repeat once |

## Safety & operational guarantees

- **Bounded model surface**: Tier 2 only selects from observed, action-compatible `ref` IDs (`<= 255`); unseen refs and malformed API responses fail closed to Tier 3.
- **Unicode & injection safety**: Normalisation preserves non-ASCII scripts (Japanese, CJK, Cyrillic, Greek, dakuten) and rejects empty labels. Snapshot parsing uses escape-aware quoting so embedded `\" [ref=eN]` strings in page text cannot spoof element refs.
- **No silent misclicks**: Label misses and out-of-range `nth` never fall back to a sole unrelated element.
- **Secret hygiene**: `value_env` keeps credentials out of plan JSON; resolved secrets are redacted (`[REDACTED]`) from error output.
- **Fault tolerance & cleanup**: Tier 2 retries transient HTTP `429` (honouring `Retry-After`), `5xx`, and network/timeout errors with bounded backoff. `SIGINT`/`SIGTERM` closes owned sessions and deletes temporary Chrome profiles cleanly.
- **Formally verified lifecycle**: Session ownership, profile cleanup, `session_open` reporting, `once` at-most-once execution, and non-silent failure invariants are verified against both TLA+/TLC and Hypothesis stateful tests.

## Agent skill

This repository bundles an agent skill at [`skills/agent-browser-decisions/SKILL.md`](skills/agent-browser-decisions/SKILL.md) (in Japanese) for Claude Code, Codex, Pi, and other agents supporting the `SKILL.md` convention.

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
- Shadow DOM, iframes, file uploads, and counting widgets are best handled directly with `agent-browser`.

## License

MIT
