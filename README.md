# agent-browser-decisions

Run a written browser plan through [agent-browser](https://github.com/vercel-labs/agent-browser), one step at a time.
Each step is resolved to an observed element, executed, and checked for its effect.

```text
resolve → act → check effect
  tier 1  label match on the agent-browser snapshot (no model)
  tier 2  OpenAI Decisions API chooses among observed, action-compatible refs
  tier 3  unresolved → stop and hand back to the main agent
```

The model only chooses an observed ref. It never writes selectors, text, or code. Field values come from the plan.

## Plan files

```json
{
  "url": "https://quotes.toscrape.com/",
  "goal": "Open the 'inspirational' tag, then go to its second page.",
  "reset": true,
  "steps": [
    {"action": "click", "target": "inspirational", "role": "link"},
    {"action": "click", "target": "Next", "role": "link"}
  ],
  "verify": "location.pathname === '/tag/inspirational/page/2/'"
}
```

```bash
agent-browser-decisions validate plan.json   # schema check, no browser
agent-browser-decisions run plan.json        # JSON result; exit 0 only if every step ran and verify passed
```

`reset` runs the plan in a fresh, isolated agent-browser session (closed on success, kept open on failure so a caller can continue with `agent-browser --session <session>`). Clearing cookies and storage inside one long-lived browser was unreliable: logins stopped sticking. `--session NAME` runs in an existing session.

Sessions launched by `reset` use a fresh Chrome profile with the password manager turned off: after a successful login, Chrome's save-password prompt or leak warning otherwise swallows every mouse and keyboard event to the page (JavaScript still works), so later clicks silently do nothing. For direct agent-browser use, `agent-browser --session NAME --profile "$(agent-browser-decisions profile)" open URL` launches the same way. `verify` is a JavaScript expression polled in the
page for up to `verify_timeout_s` (default 10); truthy, or an object with `passed: true`, counts as verified.
The result reports `stopped_at_step` and per-step `tier`, `chose`, and `effect`, so a caller can take over from the
failed step.

From another directory: `uv run --project /path/to/agent-browser-decisions --env-file /path/to/.env
agent-browser-decisions run plan.json`.

## Plan steps

```python
{
    "action": "click" | "fill" | "select" | "check" | "press",
    "target": "Search",  # label; match="regex" for a pattern
    "value": "Zurich",  # fill / select / press
    "role": "button",  # optional; also a wait condition (see below)
    "nth": 3,  # optional positional pick among label hits
    "describe": "the comments link of the THIRD story",  # text given to the decision model
    "expect": "navigation",  # optional effect to verify
    "decide": True,
}  # skip label matching for this step
```

Write `role` only for elements seen during reconnaissance. A role means "wait until elements of this role exist";
it is relaxed only if none appear within 3 s.

## Effect checks

| Action | Check | On failure |
| --- | --- | --- |
| fill | field value equals the plan value | repeat once |
| check | element is checked | repeat once |
| link click, `expect="navigation"` | document URL (without fragment) changes | keyboard activation, then repeat or exclude a model-chosen ref |
| other click, press | snapshot changes within 1 s | repeat once |

Steps act only on a settled page: two consecutive identical snapshots.

## Agent skill

This repository bundles an agent skill at [`skills/agent-browser-decisions/SKILL.md`](skills/agent-browser-decisions/SKILL.md) (in Japanese) for Claude Code, Codex, and other agents supporting the `SKILL.md` convention.

It teaches the agent when to write a plan vs. drive `agent-browser` directly, enforces safe plan rules (omit unseen `role`, use `nth` for ordinal choices, keep irreversible submissions out of plans), and handles takeover from `stopped_at_step`.

Install with the Skills CLI:

```bash
npx skills add akitana-airtanker/agent-browser-decisions
```

Or copy/symlink `skills/agent-browser-decisions` from a local clone into `~/.agents/skills/agent-browser-decisions` (Codex / Pi) or `~/.claude/skills/agent-browser-decisions` (Claude Code). Set `AGENT_BROWSER_DECISIONS_DIR` if your clone is not at `~/src/agent-browser-decisions`.

## Requirements

- [agent-browser](https://github.com/vercel-labs/agent-browser) on `PATH`, connected to a browser
  (for example `agent-browser connect 9222` to a Chrome started with `--remote-debugging-port=9222`).
- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- An OpenAI API key that can call the Decisions API. Tier 1 steps need no key.

## Run

```bash
uv sync
uv run pytest
cp .env.example .env   # set OPENAI_API_KEY
uv run --env-file .env python eval_runs.py --mode label --runs 3
uv run python summarize.py results.jsonl
```

`--mode decide` forces tier 2 for every step, which estimates behaviour when plan labels are unknown.

## Evaluation (2026-10-26, local Chrome via CDP, 3 runs per task and mode)

Outcomes are judged by deterministic URL/DOM checks, not by the model. Times exclude page setup.

| Task | label first | decide only |
| --- | --- | --- |
| flights (Google Flights, 12 steps) | 2 passed, 1 site error · 7.8 s | 2 passed, 1 site error · 13.7 s |
| form (httpbin, 8 steps) | 3/3 · 8.7 s | 3/3 · 9.4 s |
| hn (third story, positional) | 3/3 · 1.7 s | **0/3** · 1.9 s |
| github | 3/3 · 3.0 s | 3/3 · 3.4 s |
| dropdown | 3/3 · 2.2 s | 3/3 · 2.5 s |
| wiki | 3/3 · 9.9 s | 3/3 · 6.8 s |
| saucedemo (unseen) | 3/3 · 8.4 s | 3/3 · 6.1 s |
| todomvc (unseen) | 3/3 · 5.4 s | 3/3 · 2.9 s |
| books (unseen) | 3/3 · 2.0 s | 3/3 · 2.5 s |
| pypi (unseen, Japanese UI) | 3/3 · 5.2 s | 3/3 · 5.2 s |
| quotes (unseen) | 3/3 · 5.2 s | 3/3 · 4.2 s |

Medians. A site error is Google Flights returning "Oops, something went wrong" after a correctly set search.
The machine's load average was 5–17 during runs, so times vary by about 2×; an idle run took form 3.1 s, flights 6.0 s.

Limits:

- Positional choices ("the third story") are unreliable for the decision model; plans should use `nth`.
- Identical labels repeated per item (each product's "Add to cart") carry no item name in the snapshot, so neither
  label matching nor the decision model can tell them apart; use `nth` or act on that step directly.
- Labels for the known sites came from earlier exploration. Unseen-site plans used one start-page snapshot; the
  saucedemo cart role guess was removed after a failure, and the "omit unseen roles" rule came from that failure.
- Same-name elements are told apart by tree context and occurrence; a wrapped link whose box centre misses the text is
  opened by keyboard.
- Shadow DOM, iframes, file uploads, and counting widgets were not evaluated.

## License

MIT
