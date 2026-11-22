#!/usr/bin/env bash
set -euo pipefail

# Strict guarded demo for agent-browser-decisions (macOS BSD mktemp & Linux compatible).
# 1. Runs a 7-step httpbin.org form plan whose step 7 has an outdated label (expects exit 1 at step 7).
# 2. Verifies step 1-6 inputs are still preserved in the open browser session.
# 3. Patches only step 7 and resumes with --from-step 7 --expect-url --close.
# 4. Always cleans up ONLY the abd-* session and temp profile/dir created by this script.

TMPDIR_DEMO="$(mktemp -d /tmp/httpbin-demo.XXXXXX)"
PLAN="$TMPDIR_DEMO/plan.json"
SID=""
PROF=""

cleanup() {
  if [ -n "${SID:-}" ]; then
    agent-browser --session "$SID" close >/dev/null 2>&1 || true
  fi
  if [ -n "${PROF:-}" ] && [ -d "$PROF" ]; then
    rm -rf "$PROF"
  fi
  rm -rf "$TMPDIR_DEMO"
}
trap cleanup EXIT

cat > "$PLAN" <<'JSON'
{
  "url": "https://httpbin.org/forms/post",
  "reset": true,
  "steps": [
    {"action": "fill", "target": "Customer name:", "value": "{{name}}"},
    {"action": "fill", "target": "Telephone:", "value": "03-1234-5678"},
    {"action": "fill", "target": "E-mail address:", "value": "taro@example.com"},
    {"action": "check", "target": "Large", "role": "radio"},
    {"action": "check", "target": "Bacon"},
    {"action": "check", "target": "Extra Cheese"},
    {"action": "fill", "target": "Delivery notes", "value": "Leave at front door"}
  ],
  "verify": "document.querySelector('input[name=custname]').value === 'Taro Yamada' && document.querySelector('textarea[name=comments]').value === 'Leave at front door'"
}
JSON

set +e
OUT="$(agent-browser-decisions run "$PLAN" --var name="Taro Yamada" --settle-s 0.5)"
RC=$?
set -e

# Record only a session/profile genuinely created and left open by this run.
SID="$(printf '%s' "$OUT" | jq -r 'select(.session_open == true and (.session | type == "string") and (.session | startswith("abd-"))) | .session')"
PROF="$(printf '%s' "$OUT" | jq -r 'select(.profile | type == "string") | .profile')"

if [ "$RC" -ne 1 ]; then
  printf 'Expected exit code 1 (stopped at step 7), got %s
%s
' "$RC" "$OUT" >&2
  exit 1
fi
if [ -z "$SID" ]; then
  printf 'Expected an open abd-* session, got:
%s
' "$OUT" >&2
  exit 1
fi
if [ "$(printf '%s' "$OUT" | jq -r '.stopped_at_step')" != "7" ]; then
  printf 'Expected stopped_at_step == 7, got:
%s
' "$OUT" >&2
  exit 1
fi
if [ "$(printf '%s' "$OUT" | jq -r '.url')" != "https://httpbin.org/forms/post" ]; then
  printf 'Unexpected URL on stop:
%s
' "$OUT" >&2
  exit 1
fi

printf '%s
' "$OUT" | jq '{ok, stopped_at_step, session, session_open, url, step7: .steps[-1], stopped_snapshot}'

# Confirm step 1 input is still in the open browser session.
VAL="$(agent-browser --session "$SID" eval "document.querySelector('input[name=custname]').value")"
printf 'Preserved custname: %s
' "$VAL"

# Patch only step 7 and resume without re-running steps 1-6.
jq '.steps[6].target = "Delivery instructions:"' "$PLAN" > "$PLAN.tmp" && mv "$PLAN.tmp" "$PLAN"
RES="$(agent-browser-decisions run "$PLAN"   --session "$SID" --from-step 7   --expect-url "https://httpbin.org/forms/post"   --var name="Taro Yamada" --settle-s 0.5 --close)"

printf '%s
' "$RES" | jq '{ok, verified, session_open, elapsed_ms, steps: [.steps[] | {step, tier, chose, effect}]}'

if [ "$(printf '%s' "$RES" | jq -r '.ok == true and .verified == true and .session_open == false')" != "true" ]; then
  printf 'Resume did not complete cleanly:
%s
' "$RES" >&2
  exit 1
fi
SID=""
