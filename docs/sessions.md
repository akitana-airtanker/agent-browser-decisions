# Sessions, Effect Checks & Internal Batch Fusion

Reference for session lifecycle rules, effect checks, retry policy, internal Tier-1 batch fusion, and one-step direct takeover.

## 1. Session & temporary profile lifecycle

| Invocation | Startup | Normal completion | Caught interrupt (`SIGINT`/`SIGTERM`) | Step / `verify` / runtime failure |
| --- | --- | --- | --- | --- |
| Standalone (`reset: true`) | Launches fresh isolated session + temp profile | Attempts to close session & delete temp profile (`--keep-open` keeps both) | Attempts to close owned session & delete temp profile (even with `--keep-open`) | Keeps session open (`session_open: true`) and returns `profile` path |
| Standalone (no `reset`) | Opens `url` in default session | Leaves session open (`--close` alone exits `2`) | Leaves session open | Leaves session open |
| `--session <id> --no-open` | Uses existing tab (`reset` ignored) | Leaves open (`--close` attempts to close) | Leaves open (`--close` attempts to close) | Leaves session open |
| `--session <id> --reload` | Reloads `url` in existing session | Leaves open (`--close` attempts to close) | Leaves open (`--close` attempts to close) | Leaves session open |
| `--session <id> --from-step N` (`N >= 2`) | Skips `open`/`reset`, runs from step `N` | Leaves open (`--close` attempts to close) | Leaves open (`--close` attempts to close) | Leaves session open |

**Notes:**
- `--from-step 1` does not preserve the current page; pair `--session <id>` with `--no-open` (passing `--session` alone on a `reset: true` plan exits `2`). `--close` requires `--session`.
- If `open` fails, the runner attempts to close the session; before deleting a startup-created temporary profile, the runner checks both `close` exit status (`0`) and closure in the same daemon registry (`session info --json` + `session list --json`). If close confirmation fails, `session_open` remains `true` and `profile` is left on disk and returned in JSON; if profile deletion fails with `OSError`, `profile` is also preserved in JSON. Note that on normal completion, `ok: true` and `session_open: true` can coexist if the final `close` fails. For manual cleanup, confirm closure in the same daemon registry (`session info --json` + `session list --json`) before running `rm -rf "$PROF"` (see [`SECURITY.md`](../SECURITY.md#7-sessionprofile-cleanup--resume-3-principles)).
- If a `reset: true` run failed (or used `--keep-open`) and a **separate process** later closes it via `--session <id> --close`, the original temporary profile is not auto-deleted by the second process — remove the `profile` path from the first run's JSON after verifying the session closed.

## 2. Effect checks & retry policy

Before acting, non-`press` steps wait until the element appears and two consecutive `snapshot -i` observations (50 ms apart) match (up to `settle_s`, default `3.0` s; the runner may reuse a fresh post-effect snapshot from the previous step instead of always issuing two new calls). If the deadline expires with a single matching element, that match is used.

| Action | Post-action effect check | Default on failure | Opt-in / override |
| --- | --- | --- | --- |
| `fill` | Field read-back equals expected value (0.5 s poll) | Retry same element once | `once: true` or `retry: false` disables retry |
| `check` | `is checked` returns `true` (0.5 s poll) | Retry same element once | `once: true` or `retry: false` disables retry |
| `select` | Command exit status only (selected value **unchecked**, reported `ok`) | No retry; assert state in `verify` | — |
| `link` (`navigates != false`) or `click`/`upload`/`press` with `expect: "navigation"` | Document URL changes (1.5 s poll; fragment changes allowed for `#route` links) | Stop immediately (`wrong_target`; single-shot on same element) | `retry: true` tries `focus` + `Enter` once for links (3 s poll) or retries once for non-link `click`/`upload`. Note: `focus` failure is not checked before `Enter`, so avoid `retry: true` on side-effect links. `press` disallows `retry`. |
| Other `click`, `upload`, `press` | Snapshot or URL changes within 1 s | Stop immediately (`no_effect`) | `retry: true` retries `click`/`upload` once (`press` disallows `retry`). |

- **Tier 2 alternate-ref retry**: When Tier 2 is enabled and a Tier-2-chosen element fails with `wrong_target`, the runner may exclude that ref and ask Tier 2 for another candidate even when `retry: false`. Pass `"once": true` to limit execution to at most 1 attempt across all tiers and candidates.
- **End-of-plan `verify`**: Evaluated only after all steps finish (polled up to `verify_timeout_s`, default `10` s). A `verify` failure returns `ok: false`, `verified: false`, `stopped_at_step: null`.

## 3. Internal Tier-1 batch fusion

When 2 or more consecutive `fill` / `check` / `select` steps appear on the same screen and all resolve uniquely in Tier 1 (excluding `fill` steps targeting `combobox`, duplicate element refs, `once: true`, `retry: false`, `decide: true`, and `expect: "navigation"`), the runner fuses them into one `agent-browser batch --bail` call followed by read-only verification checks (and tail `verify` + `get url` when the batch reaches the end of the plan).

**Partial-failure behavior (`runner.py`):**
- All write commands in the batch are issued before reading back verification results, and the runner credits the **longest leading contiguous prefix** of confirmed steps.
- If `verified_count >= 1` and 2+ batchable steps remain from the first unconfirmed step, the runner attempts batch fusion again on the remaining suffix; once `verified_count == 0`, it falls back to step-by-step execution starting at the first unconfirmed step.
- Consequently, on a partial failure (e.g., dynamic/linked fields that reset each other), already-written preceding steps (if `--bail` skipped trailing verification), an unconfirmed step, and subsequent steps in the batch can be executed more than once, and a later step (`N+1`) may already be applied while step `N` is unapplied. See [`SECURITY.md`](../SECURITY.md#7-sessionprofile-cleanup--resume-3-principles) for safe resume rules when partial state is non-contiguous.

## 4. One-step direct takeover route

Instead of editing the plan JSON when only one step drifted, follow the same Resume 3 Principles in [`SECURITY.md`](../SECURITY.md#7-sessionprofile-cleanup--resume-3-principles):

1. **Verify unapplied state, side effects, and fresh ref before acting**: Do not click blindly using a ref from `stopped_snapshot` (a stale snapshot can remain if re-observation failed). First inspect the live session and actual page/backend state with a fresh `agent-browser --session "$SID" snapshot -i` (and readback/eval if needed) to confirm step `N` is still unapplied, has no unknown side effect, and matches the target element's current ref (obtain human confirmation first on logged-in or side-effecting pages). Then execute the single drifted step directly with that fresh ref:
   ```bash
   agent-browser --session "$SID" snapshot -i
   agent-browser --session "$SID" click @e12
   ```
2. **Resume from `N + 1` only when every remaining step from `N + 1` onward is safe to run**: Before calling `--from-step $((N + 1))`, confirm that every step from `N + 1` through the end of the plan is still unapplied and safe to execute (after a partial batch failure, step `N + 1` or later may already have been applied, in which case `--from-step $((N + 1))` would re-execute it; switch to direct `agent-browser` commands or a trimmed plan containing only unapplied steps instead). When all remaining steps from `N + 1` onward are confirmed safe (and human-approved on logged-in/side-effecting pages):
   ```bash
   agent-browser-decisions run plan.json --session "$SID" --from-step $((N + 1)) --expect-url "http://127.0.0.1:8765/" --close
   ```
   (Do not pass `--from-step` past the final step; out-of-range values exit `2`.)
3. **If step `N` was the final step**, do not call `run`; check the page state directly with `agent-browser --session "$SID" eval "..."` (note: plan `verify` expressions reference `VARS`, which is only injected by `agent-browser-decisions`, so write literal values when calling `agent-browser eval` directly). Then close the session with `agent-browser --session "$SID" close` (verify exit code `0`), confirm closure in the **same daemon registry** via both `agent-browser --session "$SID" session info --json` (`active: false`, `pid: null`, `runtime: null`, `runtimeError: null`, matching readable `socketDir` and `namespace`) and `agent-browser session list --json` (string array without `$SID`), and only then remove the startup-recorded temporary `$PROF` directory if present.
