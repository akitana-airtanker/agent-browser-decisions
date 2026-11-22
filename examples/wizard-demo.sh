#!/usr/bin/env bash
# End-to-end local demo for agent-browser-decisions on the 3-screen onboarding wizard:
#   1. Stops at Step 8 ("Next" -> "Continue") while preserving Screen 2 visibility & token
#   2. Proves Step-1 restart with the same one-time code (INVITE-001) fails at Step 4 (no_effect; server returns HTTP 409)
#   3. Patches only Step 8 and resumes live with --from-step 8 --expect-url --close ([8, 9])
#   4. Reuses the fixed plan for a second user (INVITE-002) via --var
#   5. Performs strict cleanup of the server, tracked abd-* sessions, and startup-recorded temp profiles on exit/interrupt.

set -euo pipefail

# Prevent an explicit env-file from altering AGENT_BROWSER_NAMESPACE inside the CLI differently from this shell.
unset AGENT_BROWSER_DECISIONS_ENV_FILE

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${WIZARD_PORT:-0}"

for cmd in python3 agent-browser agent-browser-decisions jq curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$cmd" >&2
    exit 127
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

TMPDIR_DEMO="$(mktemp -d /tmp/wizard-demo.XXXXXX)"
PLAN="$TMPDIR_DEMO/wizard.json"
SERVER_PID=""
SID=""
PROF=""
REG_DIR=""
REG_NS_JSON=""
SID_RESTART=""
PROF_RESTART=""
REG_DIR_RESTART=""
REG_NS_JSON_RESTART=""
SID_REUSE=""
PROF_REUSE=""
CLEANUP_FAILED=0

extract_owned_session() {
  printf '%s\n' "$1" | jq -r 'select((.session | type == "string") and (.session | startswith("abd-"))) | .session' 2>/dev/null || true
}

extract_profile() {
  printf '%s\n' "$1" | jq -r 'select(.profile | type == "string") | .profile' 2>/dev/null || true
}

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

close_owned_session() {
  local sid="$1"
  local prof="$2"
  local expected_sdir="${3:-$BASE_REG_DIR}"
  local expected_ns_json="${4:-$BASE_REG_NS_JSON}"
  local gone=1
  if [ -n "$sid" ]; then
    if agent-browser --session "$sid" close >/dev/null 2>&1 && session_is_gone "$sid" "$expected_sdir" "$expected_ns_json"; then
      gone=0
    else
      printf 'Warning: could not confirm session %s closed in registry %s; keeping profile %s\n' "$sid" "${expected_sdir:-<unknown>}" "${prof:-<none>}" >&2
      CLEANUP_FAILED=1
    fi
  fi
  if [ -n "$prof" ] && [ -e "$prof" ]; then
    if [ -z "$sid" ]; then
      printf 'Warning: missing session ID for profile %s; keeping intact\n' "$prof" >&2
      CLEANUP_FAILED=1
    elif [ "$gone" -ne 0 ]; then
      :
    elif ! is_owned_temp_profile "$prof"; then
      printf 'Warning: refusing to remove unexpected profile path %s\n' "$prof" >&2
      CLEANUP_FAILED=1
    elif ! rm -rf "$prof" 2>/dev/null; then
      printf 'Warning: failed to remove profile %s\n' "$prof" >&2
      CLEANUP_FAILED=1
    fi
  fi
}

cleanup() {
  local exit_status=$?
  set +e
  close_owned_session "$SID_REUSE" "$PROF_REUSE" "$BASE_REG_DIR" "$BASE_REG_NS_JSON"
  close_owned_session "$SID_RESTART" "$PROF_RESTART" "$REG_DIR_RESTART" "$REG_NS_JSON_RESTART"
  close_owned_session "$SID" "$PROF" "$REG_DIR" "$REG_NS_JSON"
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "$TMPDIR_DEMO" ] && [ -d "$TMPDIR_DEMO" ]; then
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

python3 "$SCRIPT_DIR/wizard/server.py" --port "$PORT" >"$TMPDIR_DEMO/server.log" 2>&1 &
SERVER_PID=$!

BASE_URL=""
for _ in $(seq 1 50); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    printf 'Wizard server failed to start (port=%s). Log:\n' "$PORT" >&2
    cat "$TMPDIR_DEMO/server.log" >&2 || true
    exit 1
  fi
  if [ -s "$TMPDIR_DEMO/server.log" ]; then
    FIRST_LINE="$(head -n 1 "$TMPDIR_DEMO/server.log" | tr -d '\r')"
    case "$FIRST_LINE" in
      http://127.0.0.1:*/)
        BASE_URL="$FIRST_LINE"
        break
        ;;
    esac
  fi
  sleep 0.1
done

if [ -z "$BASE_URL" ] || ! kill -0 "$SERVER_PID" 2>/dev/null; then
  printf 'Wizard server did not report a bound URL (port=%s). Log:\n' "$PORT" >&2
  cat "$TMPDIR_DEMO/server.log" >&2 || true
  exit 1
fi
if ! curl --noproxy '*' -sf --max-time 2 "${BASE_URL}health" >/dev/null; then
  printf 'Wizard server health check failed at %shealth. Log:\n' "$BASE_URL" >&2
  cat "$TMPDIR_DEMO/server.log" >&2 || true
  exit 1
fi

jq --arg url "$BASE_URL" '.url = $url' "$SCRIPT_DIR/wizard/wizard.json" > "$PLAN"

printf '=== 1. Run v1 plan against updated v2 UI ("Next" -> "Continue") ===\n'
RC=0
OUT="$(agent-browser-decisions run "$PLAN" --settle-s 0.5)" || RC=$?
SID="$(extract_owned_session "$OUT")"
PROF="$(extract_profile "$OUT")"
if [ -n "$SID" ] && REG_TSV="$(capture_registry "$SID")"; then
  IFS=$'\t' read -r REG_DIR REG_NS_JSON <<< "$REG_TSV"
fi
if [ "$RC" -ne 1 ]; then
  printf 'Expected rc=1 at Step 8, got rc=%s\n%s\n' "$RC" "$OUT" >&2
  exit 1
fi
printf '%s\n' "$OUT" | jq --arg rc "$RC" '{rc: ($rc | tonumber), ok, session, session_open, stopped_at_step, reason: .steps[-1].reason}'

if [ -z "$SID" ] || ! printf '%s\n' "$OUT" | jq -e '.ok == false and .stopped_at_step == 8 and .session_open == true and ((.steps[-1].reason // "") | contains("no label match"))' >/dev/null; then
  printf 'Unexpected stop payload\n' >&2
  exit 1
fi
SCREEN2_VISIBLE="$(agent-browser --session "$SID" eval "!document.getElementById('screen-2').classList.contains('hidden')")"
TOKEN_BEFORE="$(agent-browser --session "$SID" eval "document.getElementById('badge-token').textContent")"
printf 'Live session preserved Screen 2 (visible=%s) token: %s\n' "$SCREEN2_VISIBLE" "$TOKEN_BEFORE"
if [ "$SCREEN2_VISIBLE" != "true" ]; then
  printf 'Expected Screen 2 to be visible on stop, got: %s\n' "$SCREEN2_VISIBLE" >&2
  exit 1
fi
case "$TOKEN_BEFORE" in
  '"tok-'*'"'|tok-*) ;;
  *)
    printf 'Expected preserved Screen 2 token starting with tok-, got: %s\n' "$TOKEN_BEFORE" >&2
    exit 1
    ;;
esac

printf '\n=== 2. Prove Step-1 restart with the same one-time code (INVITE-001) fails ===\n'
RESTART_RC=0
RESTART_OUT="$(agent-browser-decisions run "$PLAN" --settle-s 0.5)" || RESTART_RC=$?
SID_RESTART="$(extract_owned_session "$RESTART_OUT")"
PROF_RESTART="$(extract_profile "$RESTART_OUT")"
if [ -n "$SID_RESTART" ] && REG_TSV_RESTART="$(capture_registry "$SID_RESTART")"; then
  IFS=$'\t' read -r REG_DIR_RESTART REG_NS_JSON_RESTART <<< "$REG_TSV_RESTART"
fi
if [ "$RESTART_RC" -ne 1 ] || [ -z "$SID_RESTART" ]; then
  printf 'Expected Step-1 restart to stop with rc=1 and an open session, got rc=%s\n%s\n' "$RESTART_RC" "$RESTART_OUT" >&2
  exit 1
fi
printf '%s\n' "$RESTART_OUT" | jq --arg rc "$RESTART_RC" '{rc: ($rc | tonumber), stopped_at_step, effect: .steps[-1].effect, reason: .steps[-1].reason}'
if ! printf '%s\n' "$RESTART_OUT" | jq -e '.ok == false and .stopped_at_step == 4 and .session_open == true and .steps[-1].effect == "no_effect"' >/dev/null; then
  printf 'Expected Step-1 restart to fail at Step 4 with no_effect\n' >&2
  exit 1
fi
SCREEN1_VISIBLE="$(agent-browser --session "$SID_RESTART" eval "!document.getElementById('screen-1').classList.contains('hidden')")"
ERR_MSG="$(agent-browser --session "$SID_RESTART" eval "document.getElementById('error-1').textContent")"
printf 'Server rejection shown on Screen 1 (visible=%s): %s\n' "$SCREEN1_VISIBLE" "$ERR_MSG"
if [ "$SCREEN1_VISIBLE" != "true" ]; then
  printf 'Expected Screen 1 to remain visible after rejection, got: %s\n' "$SCREEN1_VISIBLE" >&2
  exit 1
fi
case "$ERR_MSG" in
  *"already used"*) ;;
  *)
    printf 'Expected Screen 1 duplicate-code error message, got: %s\n' "$ERR_MSG" >&2
    exit 1
    ;;
esac
close_owned_session "$SID_RESTART" "$PROF_RESTART" "$REG_DIR_RESTART" "$REG_NS_JSON_RESTART"
SID_RESTART=""
PROF_RESTART=""
REG_DIR_RESTART=""
REG_NS_JSON_RESTART=""
if [ "$CLEANUP_FAILED" -ne 0 ]; then
  exit 1
fi

printf '\n=== 3. Fix Step 8 ("Next" -> "Continue") and resume from Step 8 ===\n'
jq '.steps[7].target = "Continue"' "$PLAN" > "${PLAN}.fixed" && mv "${PLAN}.fixed" "$PLAN"
RES_RC=0
RES="$(agent-browser-decisions run "$PLAN" --session "$SID" --from-step 8 --expect-url "$BASE_URL" --close)" || RES_RC=$?
printf '%s\n' "$RES" | jq --arg rc "$RES_RC" '{rc: ($rc | tonumber), ok, session_open, verified, elapsed_ms, steps: [.steps[].step]}'
if [ "$RES_RC" -ne 0 ] || ! printf '%s\n' "$RES" | jq -e '.ok == true and .verified == true and .session_open == false and ([.steps[].step] == [8, 9])' >/dev/null; then
  printf 'Resume check failed\n' >&2
  exit 1
fi
close_owned_session "$SID" "$PROF" "$REG_DIR" "$REG_NS_JSON"
SID=""
PROF=""
REG_DIR=""
REG_NS_JSON=""
if [ "$CLEANUP_FAILED" -ne 0 ]; then
  exit 1
fi

printf '\n=== 4. Reuse fixed plan for another user (INVITE-002) via --var ===\n'
REUSE_RC=0
REUSE_OUT="$(agent-browser-decisions run "$PLAN" --var name="Hanako Sato" --var email="hanako@example.com" --var code="INVITE-002" --var workspace="billing" --var note="Approved")" || REUSE_RC=$?
SID_REUSE="$(extract_owned_session "$REUSE_OUT")"
PROF_REUSE="$(extract_profile "$REUSE_OUT")"
if [ "$REUSE_RC" -ne 0 ] || ! printf '%s\n' "$REUSE_OUT" | jq -e '.ok == true and .verified == true and .session_open == false and (.profile == null) and ([.steps[].step] == [1,2,3,4,5,6,7,8,9])' >/dev/null; then
  printf 'Reuse failed (rc=%s)\n%s\n' "$REUSE_RC" "$REUSE_OUT" >&2
  exit 1
fi
printf '%s\n' "$REUSE_OUT" | jq '{ok, session_open, verified, elapsed_ms, steps: [.steps[].step]}'
close_owned_session "$SID_REUSE" "$PROF_REUSE" "$BASE_REG_DIR" "$BASE_REG_NS_JSON"
SID_REUSE=""
PROF_REUSE=""
if [ "$CLEANUP_FAILED" -ne 0 ]; then
  exit 1
fi
printf 'Demo completed cleanly.\n'
