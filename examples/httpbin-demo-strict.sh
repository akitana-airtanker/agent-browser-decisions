#!/usr/bin/env bash
set -euo pipefail

# Strict guarded demo for agent-browser-decisions (run with bash; macOS BSD mktemp & Linux compatible).
# Matches the article demo plan:
# 1. Runs a 7-step httpbin.org form plan whose step 7 has an intentionally broken label (expects rc=1 at step 7).
# 2. Programmatically asserts exit code, stopped_at_step, URL, and all 6 preserved form fields in the live session.
# 3. Patches only step 7 and resumes with --from-step 7 --expect-url --close (asserts ok, verified, session_open=false, steps=[7]).
# 4. Removes the temp profile only after confirming our abd-* session is no longer listed; otherwise leaves it and prints its path.

for cmd in agent-browser agent-browser-decisions jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$cmd" >&2
    exit 1
  fi
done

TMPDIR_DEMO="$(mktemp -d /tmp/httpbin-demo.XXXXXX)"
if [ -z "$TMPDIR_DEMO" ] || [ ! -d "$TMPDIR_DEMO" ]; then
  printf 'Failed to create temporary directory\n' >&2
  exit 1
fi

PLAN="$TMPDIR_DEMO/plan.json"
SID=""
PROF=""

session_is_gone() {
  local sid="$1"
  local slist
  slist="$(agent-browser session list 2>/dev/null)" || return 1
  ! printf '%s\n' "$slist" | grep -Fq "$sid"
}

cleanup() {
  local prof_safe=true
  if [ -n "${SID:-}" ]; then
    agent-browser --session "$SID" close >/dev/null 2>&1 || true
    if ! session_is_gone "$SID"; then
      prof_safe=false
      printf 'Warning: session %s may still be running; leaving profile intact: %s\n' "$SID" "${PROF:-<none>}" >&2
    fi
  fi
  if [ "$prof_safe" = true ] && [ -n "${PROF:-}" ] && [ -d "$PROF" ]; then
    rm -rf "$PROF"
  fi
  rm -rf "$TMPDIR_DEMO"
}
trap cleanup EXIT

cat > "$PLAN" <<'JSON'
{
  "url": "https://httpbin.org/forms/post",
  "reset": true,
  "vars": {"customer": "Taro Yamada", "note": "Leave at front door"},
  "steps": [
    {"action": "fill",  "target": "Customer name:",  "value": "{{customer}}"},
    {"action": "fill",  "target": "Telephone:",      "value": "03-1234-5678"},
    {"action": "fill",  "target": "E-mail address:", "value": "taro@example.com"},
    {"action": "check", "target": "Medium"},
    {"action": "check", "target": "Bacon"},
    {"action": "check", "target": "Extra Cheese"},
    {"action": "fill",  "target": "Special notes:",  "value": "{{note}}"}
  ],
  "verify": "document.querySelector('[name=custname]').value === VARS.customer && document.querySelector('[name=custtel]').value === '03-1234-5678' && document.querySelector('[name=custemail]').value === 'taro@example.com' && document.querySelector('input[name=size][value=medium]').checked && document.querySelector('input[name=topping][value=bacon]').checked && document.querySelector('input[name=topping][value=cheese]').checked && document.querySelector('[name=comments]').value === VARS.note"
}
JSON

set +e
OUT="$(agent-browser-decisions run "$PLAN" --settle-s 0)"
RC=$?
set -e

if ! printf '%s\n' "$OUT" | jq -e . >/dev/null 2>&1; then
  printf 'Expected JSON output from run (rc=%s), got:\n%s\n' "$RC" "$OUT" >&2
  exit 1
fi

# Record only a session/profile genuinely created and left open by this run.
SID="$(printf '%s\n' "$OUT" | jq -r 'select(.session_open == true and (.session | type == "string") and (.session | startswith("abd-"))) | .session')"
PROF="$(printf '%s\n' "$OUT" | jq -r 'select(.profile | type == "string") | .profile')"

if [ "$RC" -ne 1 ]; then
  printf 'Expected exit code 1 (stopped at step 7), got %s\n%s\n' "$RC" "$OUT" >&2
  exit 1
fi
if [ -z "$SID" ]; then
  printf 'Expected an open abd-* session, got:\n%s\n' "$OUT" >&2
  exit 1
fi
if [ "$(printf '%s\n' "$OUT" | jq -r '.stopped_at_step')" != "7" ]; then
  printf 'Expected stopped_at_step == 7, got:\n%s\n' "$OUT" >&2
  exit 1
fi
if [ "$(printf '%s\n' "$OUT" | jq -r '.url')" != "https://httpbin.org/forms/post" ]; then
  printf 'Unexpected URL on stop:\n%s\n' "$OUT" >&2
  exit 1
fi

printf '%s\n' "$OUT" | jq '{ok, stopped_at_step, session, session_open, url, step7: .steps[-1], stopped_snapshot}'

# Programmatically assert all 6 inputs from steps 1-6 are still preserved in the open browser session.
PRESERVED="$(agent-browser --session "$SID" eval "document.querySelector('[name=custname]').value === 'Taro Yamada' && document.querySelector('[name=custtel]').value === '03-1234-5678' && document.querySelector('[name=custemail]').value === 'taro@example.com' && document.querySelector('input[name=size][value=medium]').checked && document.querySelector('input[name=topping][value=bacon]').checked && document.querySelector('input[name=topping][value=cheese]').checked")"
printf 'All 6 fields preserved: %s\n' "$PRESERVED"
if [ "$PRESERVED" != "true" ]; then
  printf 'Expected all 6 prior fields to remain preserved, got: %s\n' "$PRESERVED" >&2
  exit 1
fi

# Patch only step 7 and resume without re-running steps 1-6.
jq '.steps[6].target = "Delivery instructions:"' "$PLAN" > "$PLAN.tmp"
mv "$PLAN.tmp" "$PLAN"

set +e
RES="$(agent-browser-decisions run "$PLAN"   --session "$SID" --from-step 7   --expect-url "https://httpbin.org/forms/post" --close)"
RES_RC=$?
set -e

if ! printf '%s\n' "$RES" | jq -e . >/dev/null 2>&1; then
  printf 'Expected JSON output from resume (rc=%s), got:\n%s\n' "$RES_RC" "$RES" >&2
  exit 1
fi

printf '%s\n' "$RES" | jq '{ok, verified, session_open, elapsed_ms, steps: [.steps[] | {step, tier, chose, effect}]}'

if [ "$RES_RC" -ne 0 ] || [ "$(printf '%s\n' "$RES" | jq -r '.ok == true and .verified == true and .session_open == false and ([.steps[].step] == [7])')" != "true" ]; then
  printf 'Resume did not complete cleanly (rc=%s):\n%s\n' "$RES_RC" "$RES" >&2
  exit 1
fi
