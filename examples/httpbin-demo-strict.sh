#!/usr/bin/env bash
set -euo pipefail

# Strict guarded demo for agent-browser-decisions (run with bash; macOS BSD mktemp & Linux compatible).
# Standalone strict guarded demo on httpbin.org (7-step form):
# 1. Runs a 7-step httpbin.org form plan whose step 7 has an intentionally broken label (expects rc=1 at step 7).
# 2. Programmatically asserts exit code, stopped_at_step, label-mismatch reason, URL, and all 6 preserved form fields.
# 3. Patches only step 7 and resumes with --from-step 7 --expect-url --close (asserts ok, verified, session_open=false, steps=[7]).
# 4. Removes the startup-recorded temp profile only after close succeeds and the same daemon registry confirms
#    closure via session info --json (active=false, readable socketDir) and session list --json; otherwise leaves it intact and exits non-zero.

# Prevent an explicit env-file from altering AGENT_BROWSER_NAMESPACE inside the CLI differently from this shell.
unset AGENT_BROWSER_DECISIONS_ENV_FILE

for cmd in python3 agent-browser agent-browser-decisions jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$cmd" >&2
    exit 1
  fi
done

SYS_TMPDIR_REAL="$(python3 -c 'import os, tempfile; print(os.path.realpath(tempfile.gettempdir()))')"
if [ -z "$SYS_TMPDIR_REAL" ] || [ ! -d "$SYS_TMPDIR_REAL" ]; then
  printf 'Failed to resolve system temp directory\n' >&2
  exit 1
fi

capture_registry() {
  local sid="${1:-}"
  local info=""
  if [ -n "$sid" ]; then
    info="$(agent-browser --session "$sid" session info --json 2>/dev/null)" || return 1
  else
    info="$(agent-browser session info --json 2>/dev/null)" || return 1
  fi
  printf '%s\n' "$info" | jq -e '
    .success == true and
    (.data.socketDir | type == "string" and length > 0) and
    ((.data.namespace == null) or (.data.namespace | type == "string"))
  ' >/dev/null 2>&1 || return 1
  printf '%s\n' "$info" | jq -r '[.data.socketDir, (.data.namespace | tojson)] | @tsv'
}

BASE_REG_TSV="$(capture_registry)" || {
  printf 'Failed to inspect agent-browser session registry via session info --json\n' >&2
  exit 1
}
IFS=$'\t' read -r BASE_REG_DIR BASE_REG_NS_JSON <<< "$BASE_REG_TSV"

TMPDIR_DEMO="$(mktemp -d /tmp/httpbin-demo.XXXXXX)"
if [ -z "$TMPDIR_DEMO" ] || [ ! -d "$TMPDIR_DEMO" ]; then
  printf 'Failed to create temporary directory\n' >&2
  exit 1
fi

PLAN="$TMPDIR_DEMO/plan.json"
SID=""
PROF=""
REG_DIR=""
REG_NS_JSON=""
CLEANUP_FAILED=0

is_owned_temp_profile() {
  local prof="$1"
  [ -n "$prof" ] && [ ! -L "$prof" ] && [ -d "$prof" ] && [ -f "$prof/Default/Preferences" ] || return 1
  case "$(basename "$prof")" in
    abd-profile-*) ;;
    *) return 1 ;;
  esac
  local parent_real=""
  parent_real="$(python3 -c 'import os, sys; print(os.path.realpath(os.path.dirname(os.path.abspath(sys.argv[1]))))' "$prof" 2>/dev/null)" || return 1
  [ "$parent_real" = "$SYS_TMPDIR_REAL" ]
}

session_is_gone() {
  local sid="$1"
  local expected_sdir="$2"
  local expected_ns_json="$3"
  local info=""
  local sdir=""
  local slist=""
  [ -n "$sid" ] && [ -n "$expected_sdir" ] && [ -n "$expected_ns_json" ] || return 1
  info="$(agent-browser --session "$sid" session info --json 2>/dev/null)" || return 1
  printf '%s\n' "$info" | jq -e \
    --arg sid "$sid" \
    --arg sdir "$expected_sdir" \
    --arg ns_json "$expected_ns_json" '
      .success == true and
      .data.session == $sid and
      .data.active == false and
      .data.pid == null and
      .data.runtime == null and
      .data.runtimeError == null and
      .data.socketDir == $sdir and
      ((.data.namespace | tojson) == $ns_json)
    ' >/dev/null 2>&1 || return 1
  sdir="$(printf '%s\n' "$info" | jq -r '.data.socketDir')"
  [ -d "$sdir" ] && [ -r "$sdir" ] && [ -x "$sdir" ] && ls -A "$sdir" >/dev/null 2>&1 || return 1
  slist="$(agent-browser session list --json 2>/dev/null)" || return 1
  printf '%s\n' "$slist" | jq -e --arg sid "$sid" '
    .success == true and
    (.data.sessions | type == "array") and
    (.data.sessions | all(type == "string")) and
    ((.data.sessions | index($sid)) == null)
  ' >/dev/null 2>&1
}

cleanup() {
  local exit_status=$?
  set +e
  local gone=1
  local expected_sdir="${REG_DIR:-$BASE_REG_DIR}"
  local expected_ns_json="${REG_NS_JSON:-$BASE_REG_NS_JSON}"
  if [ -n "${SID:-}" ]; then
    if agent-browser --session "$SID" close >/dev/null 2>&1 && session_is_gone "$SID" "$expected_sdir" "$expected_ns_json"; then
      gone=0
    else
      CLEANUP_FAILED=1
      printf 'Warning: could not confirm session %s closed in registry %s; leaving profile intact: %s\n' "$SID" "${expected_sdir:-<unknown>}" "${PROF:-<none>}" >&2
    fi
  fi
  if [ -n "${PROF:-}" ] && [ -e "$PROF" ]; then
    if [ -z "${SID:-}" ]; then
      printf 'Warning: missing session ID for profile %s; leaving intact\n' "$PROF" >&2
      CLEANUP_FAILED=1
    elif [ "$gone" -ne 0 ]; then
      :
    elif ! is_owned_temp_profile "$PROF"; then
      printf 'Warning: refusing to remove unexpected profile path %s\n' "$PROF" >&2
      CLEANUP_FAILED=1
    elif ! rm -rf "$PROF" 2>/dev/null; then
      printf 'Warning: failed to remove profile %s\n' "$PROF" >&2
      CLEANUP_FAILED=1
    fi
  fi
  if [ -n "${TMPDIR_DEMO:-}" ] && [ -d "$TMPDIR_DEMO" ]; then
    if ! rm -rf "$TMPDIR_DEMO" 2>/dev/null; then
      printf 'Warning: failed to remove temp dir %s\n' "$TMPDIR_DEMO" >&2
      CLEANUP_FAILED=1
    fi
  fi
  if [ "$exit_status" -eq 0 ] && [ "$CLEANUP_FAILED" -ne 0 ]; then
    exit 1
  fi
}
trap 'exit 130' INT
trap 'exit 143' TERM
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

# Record any abd-* session, temp profile, and daemon registry returned by this startup run before exit-code checks.
SID="$(printf '%s\n' "$OUT" | jq -r 'select((.session | type == "string") and (.session | startswith("abd-"))) | .session')"
PROF="$(printf '%s\n' "$OUT" | jq -r 'select(.profile | type == "string") | .profile')"
if [ -n "$SID" ] && REG_TSV="$(capture_registry "$SID")"; then
  IFS=$'\t' read -r REG_DIR REG_NS_JSON <<< "$REG_TSV"
fi

if [ "$RC" -ne 1 ]; then
  printf 'Expected exit code 1 (stopped at step 7), got %s\n%s\n' "$RC" "$OUT" >&2
  exit 1
fi
if [ -z "$SID" ] || ! printf '%s\n' "$OUT" | jq -e '.ok == false and .session_open == true and .stopped_at_step == 7 and ((.steps[-1].reason // "") | contains("no label match"))' >/dev/null; then
  printf 'Unexpected stop payload at step 7:\n%s\n' "$OUT" >&2
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
RES="$(agent-browser-decisions run "$PLAN" --session "$SID" --from-step 7 --expect-url "https://httpbin.org/forms/post" --close)"
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
