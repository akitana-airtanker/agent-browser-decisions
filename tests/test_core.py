"""Property-based and contract tests for pure core logic."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from agent_browser_decisions.core import (
    MAX_CHOICES,
    NONE_CHOICE,
    PROVIDER_ORDER,
    PROVIDERS,
    ROLES,
    apply_plan_vars,
    build_batch_commands,
    chosen_ref,
    command_for,
    compatible,
    decision_request,
    evaluate_verify_output,
    extract_input_tokens,
    hash_route,
    infer_provider_from_model,
    match_label,
    normalize,
    parse_dotenv,
    parse_snapshot,
    parse_var_args,
    redact_text,
    resolve_decision_config,
    resolve_step_values,
    same_field_value,
    select_tier1_batch,
    validate_plan,
    validate_run_options,
    verify_batch_results,
    wrap_verify_with_vars,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SNAPSHOT = """
- combobox "Change ticket type." [ref=e75]: Round trip
- textbox "Where from?" [ref=e77]: Tokyo
- textbox "Where to?" [ref=e79]
- button "Search" [ref=e42]
- checkbox "Bacon" [checked=false, ref=e8]
- link "More" [ref=e11]
- link "More info" [ref=e12]
- button "Café" [ref=e13]
"""

JSON_VALUE = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=12),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=16,
)

ELEMENT = st.builds(
    lambda ref_id, role, name: {
        "ref": f"e{ref_id}",
        "role": role,
        "name": name,
        "attrs": "",
        "value": "",
        "context": [],
    },
    st.integers(1, 5000),
    st.sampled_from(["button", "link", "textbox", "checkbox", "combobox", "tab"]),
    st.text(alphabet="abcABC 0123é日本語削除保存ログイン検索", min_size=0, max_size=18),
)


# ---------- Unicode normalization & label matching ----------


@given(st.text(max_size=60))
def test_normalize_is_idempotent(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once


@given(st.text(alphabet="あいうえおがぎぐげごカキクケコ削除保存ログインВойти登录Ελληνικά", min_size=1, max_size=20))
def test_normalize_never_erases_non_ascii_scripts(text: str) -> None:
    assert normalize(text) != ""


def test_normalize_preserves_japanese_dakuten_and_kanji() -> None:
    assert normalize("削除") != normalize("保存")
    assert normalize("が") != normalize("か")
    assert normalize("パ") != normalize("ハ")
    assert normalize("Ｌｏｇｉｎ") == "login"
    assert normalize("Café") == "cafe"
    assert normalize("Zürich") == "zurich"


@given(
    st.text(alphabet="削除保存ログイン検索追加編集戻る完了", min_size=1, max_size=8),
    st.text(alphabet="削除保存ログイン検索追加編集戻る完了", min_size=1, max_size=8),
)
def test_distinct_japanese_labels_never_collide_unless_prefix(target: str, element_name: str) -> None:
    el = {"ref": "e1", "role": "button", "name": element_name, "attrs": "", "value": "", "context": []}
    matched, status = match_label({"action": "click", "target": target}, [el])
    if normalize(element_name).startswith(normalize(target)):
        assert (matched, status) == (el, "hit")
    else:
        assert (matched, status) == (None, "missing")


def test_label_match_folds_accents_prefix_and_escalates_ambiguity() -> None:
    elements = parse_snapshot(SNAPSHOT)
    assert match_label({"action": "click", "target": "cafe"}, elements)[0]["ref"] == "e13"
    assert match_label({"action": "click", "target": "More"}, elements)[0]["ref"] == "e11"
    assert match_label({"action": "fill", "target": "Where"}, elements) == (None, "ambiguous")
    assert match_label({"action": "fill", "target": "Where to"}, elements)[0]["ref"] == "e79"
    assert match_label({"action": "click", "target": "   "}, elements) == (None, "missing")


@given(
    st.lists(ELEMENT, min_size=1, max_size=12, unique_by=lambda e: e["ref"]),
    st.text(alphabet="abcABC 0123é日本語削除保存ログイン", min_size=0, max_size=12),
    st.sampled_from(["click", "fill", "check", "select"]),
)
def test_label_hit_is_unique_compatible_nonempty_and_equal_or_prefix(
    elements: list[dict],
    target: str,
    action: str,
) -> None:
    step = {"action": action, "target": target}
    matched, status = match_label(step, elements)
    pool = compatible(step, elements)
    label = normalize(target)
    if status == "hit":
        assert label != ""
        assert matched in pool
        exact = [e for e in pool if normalize(e["name"]) == label]
        prefix = [e for e in pool if normalize(e["name"]).startswith(label)]
        assert (len(exact) == 1 and matched == exact[0]) or (not exact and len(prefix) == 1 and matched == prefix[0])
    elif not label:
        assert matched is None and status == "missing"


@given(
    st.integers(1, 6),
    st.integers(1, 8),
    st.booleans(),
)
def test_nth_selects_exact_position_or_returns_missing(count: int, nth: int, use_prefix: bool) -> None:
    suffix = " details" if use_prefix else ""
    text = "\n".join(f'- link "Item{suffix}" [ref=e{i}]' for i in range(1, count + 1))
    elements = parse_snapshot(text)
    matched, status = match_label({"action": "click", "target": "Item", "nth": nth}, elements)
    if nth <= count:
        assert status == "hit" and matched["ref"] == f"e{nth}"
    else:
        assert (matched, status) == (None, "missing")


def test_regex_nth_selects_positionally_in_document_order() -> None:
    text = "\n".join(f'- link "{n} comments" [ref=e{i}]' for i, n in enumerate([12, 4, 88], 1))
    text += '\n- link "comments" [ref=e9]\n- link "discuss" [ref=e10]'
    elements = parse_snapshot(text)
    step = {"action": "click", "target": r"^(\d+\s*comments?|discuss)$", "match": "regex", "nth": 3}
    assert match_label(step, elements)[0]["ref"] == "e3"
    assert match_label(step | {"nth": 6}, elements) == (None, "missing")
    assert match_label(step | {"nth": 4}, elements)[0]["ref"] == "e10"


def test_match_label_rejects_bool_nth_directly() -> None:
    elements = parse_snapshot('- button "Add" [ref=e1]')
    assert match_label({"action": "click", "target": "Add", "nth": True}, elements) == (None, "missing")
    assert match_label({"action": "click", "target": "Add", "nth": False}, elements) == (None, "missing")


# ---------- snapshot parsing: injection immunity, context, linearity ----------


@given(
    st.sampled_from(["link", "button", "textbox"]),
    st.text(alphabet="abcXYZ削除ログイン ", min_size=0, max_size=15),
    st.text(alphabet='abcXYZ: []"', min_size=0, max_size=15),
    st.integers(1, 500),
    st.integers(1, 500),
    st.booleans(),
)
def test_parse_snapshot_immune_to_ref_spoofing_in_escaped_names(
    role: str,
    prefix: str,
    suffix: str,
    real_id: int,
    fake_id: int,
    structured_attack: bool,
) -> None:
    raw_name = f'{prefix}" [ref=e{fake_id}]: {suffix}' if structured_attack else f'{prefix}"{suffix}'
    escaped = raw_name.replace("\\", "\\\\").replace('"', '\\"')
    line = f'- {role} "{escaped}" [ref=e{real_id}]'
    parsed = parse_snapshot(line)
    assert len(parsed) == 1
    assert parsed[0]["ref"] == f"e{real_id}"
    assert parsed[0]["name"] == raw_name.strip()
    assert parsed[0]["value"] == ""


def test_parse_keeps_role_name_value_and_attrs() -> None:
    parsed = {e["ref"]: e for e in parse_snapshot(SNAPSHOT)}
    assert parsed["e75"]["value"] == "Round trip" and parsed["e75"]["role"] == "combobox"
    assert parsed["e79"]["name"] == "Where to?"
    assert "checked=false" in parsed["e8"]["attrs"]
    assert len(parsed) == 8


def test_same_name_elements_carry_context_and_occurrence() -> None:
    text = """- link "Turing machine" [ref=e85]
- table
  - cell "Known for Enigma"
    - list
      - link "Turing machine" [ref=e1529]"""
    a_el, b_el = parse_snapshot(text)
    assert a_el["context"] == [] and a_el["occurrence"] == "1 of 2 with this name"
    assert b_el["context"] == ["cell 'Known for Enigma'"] and b_el["occurrence"] == "2 of 2 with this name"


def test_parse_snapshot_is_linear_on_large_duplicate_pages() -> None:
    raw = "\n".join(f'- button "追加" [ref=e{i}]' for i in range(4000))
    started = time.perf_counter()
    parsed = parse_snapshot(raw)
    elapsed = time.perf_counter() - started
    assert len(parsed) == 4000 and parsed[-1]["occurrence"] == "4000 of 4000 with this name"
    assert elapsed < 0.5


# ---------- decision_request, chosen_ref, command_for, same_field_value ----------


@given(st.lists(ELEMENT, min_size=2, max_size=350, unique_by=lambda e: e["ref"]), st.text(max_size=10))
def test_decision_choices_are_bounded_observed_refs(elements: list[dict], target: str) -> None:
    request = decision_request({"action": "click", "target": target}, "goal", elements, "https://x.test")
    values = [c["value"] for c in request["questions"][0]["choices"]]
    observed = {e["ref"] for e in elements}
    assert 0 < len(values) <= MAX_CHOICES
    assert set(values) <= observed
    assert len(values) == len(set(values))


@given(JSON_VALUE)
def test_chosen_ref_never_raises_on_arbitrary_api_payload(payload: object) -> None:
    ref, info = chosen_ref(payload)
    assert ref is None or isinstance(ref, str)
    if ref is None:
        assert info is not None


@given(st.text(alphabet=" abc\n\r\t日本語", max_size=20), st.sampled_from(["", "\n", "\r\n"]))
def test_same_field_value_handles_single_cli_newline_and_preserves_whitespace(expected: str, cli_nl: str) -> None:
    assert same_field_value(expected + cli_nl, expected)
    assert not same_field_value(expected + "\n\n", expected)
    if expected:
        assert not same_field_value("", expected)


def test_command_for_maps_actions_cleanly() -> None:
    assert command_for({"action": "click", "target": "Go"}, "e3") == ["click", "@e3"]
    assert command_for({"action": "check", "target": "Agree"}, "e4") == ["check", "@e4"]
    assert command_for({"action": "fill", "target": "User", "value": "alice"}, "e1") == ["fill", "@e1", "alice"]
    assert command_for({"action": "select", "target": "Country", "value": "JP"}, "e2") == ["select", "@e2", "JP"]


# ---------- validate_plan & validate_run_options ----------


@given(JSON_VALUE)
def test_validate_plan_never_raises_on_arbitrary_json(payload: object) -> None:
    problems = validate_plan(payload)
    assert isinstance(problems, list)
    assert all(isinstance(p, str) for p in problems)


@given(st.one_of(st.booleans(), st.integers(max_value=0), st.floats(), st.text(max_size=6), st.none()))
def test_validate_plan_rejects_invalid_nth(bad_nth: object) -> None:
    problems = validate_plan({"steps": [{"action": "click", "target": "Go", "nth": bad_nth}]})
    assert any("nth" in p for p in problems)


@given(
    st.one_of(
        st.booleans(),
        st.floats(max_value=-0.01),
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
        st.text(max_size=5),
    )
)
def test_validate_plan_rejects_invalid_verify_timeout(bad_timeout: object) -> None:
    problems = validate_plan({"steps": [{"action": "click", "target": "Go"}], "verify_timeout_s": bad_timeout})
    assert any("verify_timeout_s" in p for p in problems)


@given(st.sampled_from(["decide", "once", "navigates", "secret"]), st.sampled_from(["true", "false", 0, 1, None, []]))
def test_validate_plan_rejects_non_bool_flags(flag: str, bad_value: object) -> None:
    step = {"action": "fill", "target": "User", "value": "a", flag: bad_value}
    problems = validate_plan({"steps": [step]})
    assert any(flag in p for p in problems)


def test_validate_plan_enforces_structural_and_value_env_invariants() -> None:
    assert validate_plan({"steps": [{"action": "click", "target": "Go"}]}) == []
    assert (
        validate_plan({"$schema": "../schema/plan.schema.json", "steps": [{"action": "click", "target": "Go"}]}) == []
    )
    assert validate_plan({"steps": [{"action": "fill", "target": "Pw", "value_env": "APP_PW"}]}) == []
    assert validate_plan({"steps": [{"action": "fill", "target": "Pw", "value": "x", "value_env": "APP_PW"}]})
    assert validate_plan({"steps": [{"action": "fill", "target": "Pw", "value_env": "123BAD"}]})
    assert validate_plan({"steps": [{"action": "click", "target": "Go", "value_env": "APP_PW"}]})
    assert validate_plan({"steps": [{"action": "click", "target": "Go", "secret": True}]})
    assert validate_plan({"reset": True, "steps": [{"action": "click", "target": "Go"}]})
    assert validate_plan({"steps": [{"action": "click", "target": "Go", "role": "heading"}]})
    assert validate_plan({"steps": [{"action": "click", "target": "Go", "decide": True, "nth": 2}]})
    assert validate_plan({"steps": [{"action": "click", "target": "Go", "decide": True, "match": "regex"}]})
    assert validate_plan({"steps": [{"action": "press", "value": "Enter", "target": "ignored"}]})
    assert validate_plan({"steps": [{"action": "press", "value": "Enter", "once": True}]}) == []
    assert validate_run_options(
        {"reset": True, "url": "https://x", "steps": [{"action": "click", "target": "Go"}]}, session="s1"
    )
    assert validate_run_options({"steps": [{"action": "click", "target": "Go"}]}, session=None, from_step=2)
    assert validate_run_options({"steps": [{"action": "click", "target": "Go"}]}, session="s1", from_step=2)


# ---------- evaluate_verify_output, dotenv, secrets, hash_route ----------


@given(
    st.sampled_from(
        ["", "   ", "undefined", "NaN", "SyntaxError: bad", "TypeError: x", "{broken", "false", "0", "null", '""']
    )
)
def test_evaluate_verify_rejects_non_json_and_errors(stdout: str) -> None:
    passed, _ = evaluate_verify_output(stdout)
    assert passed is False


@given(st.dictionaries(st.text(alphabet="abcxyz", min_size=1, max_size=5), st.integers(), max_size=3))
def test_evaluate_verify_treats_plain_js_objects_as_truthy(obj: dict) -> None:
    passed, value = evaluate_verify_output(json.dumps(obj))
    assert passed is True and value == obj


@given(
    st.one_of(st.just(False), st.integers(0, 1), st.text(max_size=6), st.none(), st.lists(st.booleans(), max_size=2))
)
def test_evaluate_verify_requires_strict_true_for_passed_field(val: object) -> None:
    passed, _ = evaluate_verify_output(json.dumps({"passed": val}))
    assert passed is False


def test_evaluate_verify_accepts_passed_true_and_empty_array() -> None:
    assert evaluate_verify_output('{"passed": true}')[0] is True
    assert evaluate_verify_output("[]")[0] is True
    assert evaluate_verify_output('{"passed": false}')[0] is False
    assert math.isnan(evaluate_verify_output("NaN")[1]) if isinstance(evaluate_verify_output("NaN")[1], float) else True


def test_parse_dotenv_handles_comments_quotes_and_export() -> None:
    text = "# comment\nOPENAI_API_KEY=\nDECISION_MODEL='gpt-test'\nexport EXTRA=\"hello world\"\n"
    assert parse_dotenv(text) == {"OPENAI_API_KEY": "", "DECISION_MODEL": "gpt-test", "EXTRA": "hello world"}


@given(
    st.text(alphabet="abcXYZ0123!@#", min_size=4, max_size=16),
    st.text(alphabet="prefix suffix error: ", min_size=0, max_size=20),
)
def test_redact_text_always_removes_resolved_secrets(secret: str, noise: str) -> None:
    message = f"{noise}{secret} middle {secret}{noise}"
    redacted = redact_text(message, {secret})
    assert secret not in redacted
    assert "[REDACTED]" in redacted


def test_resolve_step_values_tracks_secrets_and_missing_vars() -> None:
    steps = [
        {"action": "fill", "target": "User", "value_env": "U", "secret": False},
        {"action": "fill", "target": "Pass", "value_env": "P"},
        {"action": "fill", "target": "Token", "value": "tok-123", "secret": True},
    ]
    resolved, secrets, problems = resolve_step_values(steps, {"U": "alice", "P": "s3cr3t"})
    assert problems == []
    assert [s["value"] for s in resolved] == ["alice", "s3cr3t", "tok-123"]
    assert secrets == {"s3cr3t", "tok-123"}

    _, _, missing = resolve_step_values(steps, {"U": "alice"})
    assert len(missing) == 1 and "P" in missing[0]


def test_hash_route_links_are_recognised_by_their_href() -> None:
    assert hash_route("#/completed") and hash_route("#active")
    assert not hash_route("#") and not hash_route("/page/2") and not hash_route("https://example.com/#top")


def test_examples_and_json_schema_match_core_contracts() -> None:
    examples = sorted((REPO_ROOT / "examples").glob("*.json"))
    assert len(examples) >= 3
    for example in examples:
        plan = json.loads(example.read_text(encoding="utf-8"))
        assert validate_plan(plan) == [], f"{example.name} failed validation"

    schema = json.loads((REPO_ROOT / "schema" / "plan.schema.json").read_text(encoding="utf-8"))
    by_action = {variant["title"]: variant for variant in schema["$defs"]["step"]["oneOf"]}
    assert set(schema["$defs"]) == {"step"}
    for action, roles in ROLES.items():
        assert set(by_action[action]["properties"]["role"]["enum"]) == roles
        assert "if" in by_action[action] and "then" in by_action[action]


# ---------- Multi-provider (OpenAI / System One / Gemini) properties ----------


@given(
    st.sampled_from(PROVIDER_ORDER),
    st.one_of(
        st.integers(min_value=2, max_value=20),
        st.integers(min_value=MAX_CHOICES - 2, max_value=MAX_CHOICES + 15),
    ),
    st.text(max_size=12),
)
def test_decision_request_bounds_and_wire_shape_across_all_providers(
    provider: str,
    count: int,
    target: str,
) -> None:
    elements = [
        {
            "role": "button",
            "name": f"Item {i}",
            "ref": f"e{i}",
            "attrs": "",
            "value": "",
            "context": [],
            "occurrence": "",
        }
        for i in range(1, count + 1)
    ]
    req = decision_request({"action": "click", "target": target}, "goal", elements, "https://x.test", provider=provider)
    observed = {e["ref"] for e in elements}
    spec = PROVIDERS[provider]
    assert req["model"] == spec.default_model

    if spec.protocol == "openai":
        values = [c["value"] for c in req["questions"][0]["choices"]]
        assert "context" in req
        assert req["questions"][0]["allow_abstention"] is True
        assert 0 < len(values) <= MAX_CHOICES
        assert set(values) <= observed
        assert len(values) == len(set(values))
    else:
        context_key = "input" if spec.protocol == "gemini" else "state"
        assert context_key in req
        q = req["questions"]["element"]
        assert q["type"] == "choice"
        criteria = q["criteria"]
        assert 2 <= len(criteria) <= MAX_CHOICES
        assert NONE_CHOICE in criteria
        real_refs = set(criteria) - {NONE_CHOICE}
        assert 0 < len(real_refs) <= MAX_CHOICES - 1
        assert real_refs <= observed


@given(
    st.from_regex(r"e[0-9]{1,4}", fullmatch=True),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    st.booleans(),
)
def test_chosen_ref_round_trips_openai_systemone_and_gemini_payloads(
    ref: str,
    confidence: float,
    abstain: bool,
) -> None:
    openai_payload = (
        {"decisions": [{"id": "element", "status": "abstained", "abstention_reason": "none_match"}]}
        if abstain
        else {"decisions": [{"id": "element", "status": "answered", "choice": ref, "confidence": confidence}]}
    )
    got_ref, got_info = chosen_ref(openai_payload)
    assert (got_ref is None and got_info == "none_match") if abstain else (got_ref == ref and got_info == confidence)

    s1_choice = NONE_CHOICE if abstain else ref
    s1_payload = {"answers": {"element": {"type": "choice", "choice": s1_choice, "confidence": confidence}}}
    got_ref2, got_info2 = chosen_ref(s1_payload)
    assert (got_ref2 is None and got_info2 == "abstained") if abstain else (got_ref2 == ref and got_info2 == confidence)

    blocked = {"answers": {}, "prompt_feedback": {"block_reason": "SAFETY"}}
    assert chosen_ref(blocked) == (None, "blocked:SAFETY")

    refused = {"answers": {"element": {"type": "refusal", "reason": "policy"}}}
    assert chosen_ref(refused) == (None, "policy")


@given(
    st.dictionaries(
        st.sampled_from(
            [
                "OPENAI_API_KEY",
                "OPENROUTER_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "TYPESAFE_API_KEY",
                "LIQUID_API_KEY",
            ]
        ),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=12),
    ),
    st.sampled_from([None, *PROVIDER_ORDER]),
    st.sampled_from(
        [None, "gpt-6-luna", "liquid/d1.2", "google/gemini-decision-1", "gemini-decision-1", "jev-1.14.0", "d1.2-ultra"]
    ),
    st.sampled_from(
        [
            None,
            "https://proxy.test/v1",
            "https://proxy.test/v1/",
            "https://openrouter.ai/api/alpha/decisions",
            "https://self.test/systemone",
        ]
    ),
)
def test_resolve_decision_config_precedence_and_endpoint_invariants(
    env: dict[str, str],
    explicit_provider: str | None,
    explicit_model: str | None,
    explicit_base_url: str | None,
) -> None:
    cfg = resolve_decision_config(
        provider=explicit_provider,
        model=explicit_model,
        base_url=explicit_base_url,
        environ=env,
    )
    assert cfg.provider in PROVIDERS
    spec = PROVIDERS[cfg.provider]
    assert cfg.protocol == spec.protocol
    assert cfg.auth_header == spec.auth_header
    assert not cfg.endpoint_url.endswith(
        ("/decisions/decisions", "/systemone/systemone", "/decisions/systemone", "/systemone/decisions")
    )
    if explicit_provider is not None:
        assert cfg.provider == explicit_provider
    elif explicit_model is not None:
        assert cfg.provider == infer_provider_from_model(explicit_model)
    if explicit_base_url is not None and explicit_base_url.rstrip("/").endswith(("/decisions", "/systemone")):
        assert cfg.endpoint_url == explicit_base_url.rstrip("/")
    else:
        assert cfg.endpoint_url.endswith(spec.endpoint_path)


@given(st.integers(min_value=0, max_value=10**7), st.sampled_from(["input_tokens", "total_input_tokens"]))
def test_extract_input_tokens_reads_openai_systemone_and_gemini_usage(tokens: int, field: str) -> None:
    assert extract_input_tokens({"usage": {field: tokens}}) == tokens


@given(JSON_VALUE)
def test_extract_input_tokens_never_raises_on_arbitrary_json(payload: object) -> None:
    got = extract_input_tokens(payload)
    assert isinstance(got, int) and not isinstance(got, bool) and got >= 0


@given(st.text(min_size=1, max_size=20).filter(lambda s: s.strip() and s.strip().lower() not in PROVIDERS))
def test_validate_run_options_rejects_unknown_provider(bad_provider: str) -> None:
    problems = validate_run_options({"steps": [{"action": "click", "target": "Go"}]}, provider=bad_provider)
    assert any("unknown provider" in p for p in problems)


# ---------- plan vars & Tier-1 same-screen batching ----------

VAR_NAME = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
VAR_VAL = st.text(max_size=40).filter(lambda s: "{{" not in s and "}}" not in s)


@given(st.dictionaries(VAR_NAME, VAR_VAL, min_size=1, max_size=5), st.dictionaries(VAR_NAME, VAR_VAL, max_size=3))
def test_apply_plan_vars_expands_and_overrides_cleanly(defaults: dict[str, str], overrides: dict[str, str]) -> None:
    keys = list(defaults)
    plan = {
        "url": f"https://example.com/?q={{{{{keys[0]}}}}}",
        "vars": defaults,
        "steps": [{"action": "fill", "target": "Name", "value": f"{{{{{k}}}}}"} for k in keys],
    }
    expanded, merged, problems = apply_plan_vars(plan, overrides)
    assert problems == []
    expected = {**defaults, **overrides}
    assert merged == expected
    assert expanded["url"] == f"https://example.com/?q={expected[keys[0]]}"
    for step, k in zip(expanded["steps"], keys, strict=True):
        assert step["value"] == expected[k]


def test_apply_plan_vars_reports_undefined_placeholder() -> None:
    plan = {"steps": [{"action": "fill", "target": "Name", "value": "{{missing_var}}"}]}
    _, _, problems = apply_plan_vars(plan, {"other": "ok"})
    assert any("missing_var" in p for p in problems)


@given(st.dictionaries(VAR_NAME, st.text(max_size=30), min_size=1, max_size=4))
def test_parse_var_args_and_wrap_verify_roundtrip(mapping: dict[str, str]) -> None:
    raw = [f"{k}={v}" for k, v in mapping.items()]
    parsed, problems = parse_var_args(raw)
    assert problems == []
    assert parsed == mapping
    wrapped = wrap_verify_with_vars("VARS.x === 'ok'", parsed)
    assert wrapped.startswith("((VARS) => (VARS.x === 'ok'))(")
    assert wrap_verify_with_vars("true", {}) == "true"


def test_parse_var_args_rejects_malformed_entries() -> None:
    _, problems = parse_var_args(["no_equals", "123bad=val"])
    assert len(problems) == 2


def test_select_tier1_batch_batches_form_steps_and_stops_at_dynamic_or_duplicate() -> None:
    elements = [
        {"role": "textbox", "name": "Name", "ref": "e1", "context": ""},
        {"role": "textbox", "name": "Email", "ref": "e2", "context": ""},
        {"role": "radio", "name": "Large", "ref": "e3", "context": ""},
        {"role": "combobox", "name": "Where to?", "ref": "e4", "context": ""},
        {"role": "button", "name": "Submit", "ref": "e5", "context": ""},
    ]
    steps = [
        {"action": "fill", "target": "Name", "value": "Hanako"},
        {"action": "fill", "target": "Email", "value": "h@example.com"},
        {"action": "check", "target": "Large", "role": "radio"},
        {"action": "click", "target": "Submit", "role": "button"},
    ]
    batch = select_tier1_batch(steps, elements)
    assert len(batch) == 3
    cmds, check_map = build_batch_commands(batch)
    assert cmds[:3] == [["fill", "@e1", "Hanako"], ["fill", "@e2", "h@example.com"], ["check", "@e3"]]
    assert cmds[3:] == [["get", "value", "@e1"], ["get", "value", "@e2"], ["is", "checked", "@e3"]]

    # All verified -> 3
    rows_ok = [
        {"index": 0, "success": True, "result": {"filled": "@e1"}},
        {"index": 1, "success": True, "result": {"filled": "@e2"}},
        {"index": 2, "success": True, "result": {"checked": "@e3"}},
        {"index": 3, "success": True, "result": {"value": "Hanako"}},
        {"index": 4, "success": True, "result": {"value": "h@example.com"}},
        {"index": 5, "success": True, "result": {"checked": True}},
    ]
    assert verify_batch_results(batch, rows_ok, check_map) == 3

    # Effect check fails on step index 1 -> returns 1 so runner falls back at step 2
    rows_bad_val = list(rows_ok)
    rows_bad_val[4] = {"index": 4, "success": True, "result": {"value": "wrong"}}
    assert verify_batch_results(batch, rows_bad_val, check_map) == 1

    # Combobox fill is never batched
    combo_steps = [
        {"action": "fill", "target": "Where to?", "value": "London"},
        {"action": "fill", "target": "Name", "value": "Hanako"},
    ]
    assert select_tier1_batch(combo_steps, elements) == []

    # Duplicate ref stops batching
    dup_steps = [
        {"action": "fill", "target": "Name", "value": "A"},
        {"action": "fill", "target": "Name", "value": "B"},
    ]
    assert select_tier1_batch(dup_steps, elements) == []


def test_validate_run_options_no_open_and_close_rules() -> None:
    plan = {"url": "https://example.com", "reset": True, "steps": [{"action": "click", "target": "Go"}]}
    assert validate_run_options(plan, session=None, no_open=True)
    assert validate_run_options(plan, session=None, close_on_success=True)
    assert validate_run_options(plan, session="s1", no_open=False)  # reset=true + session without no_open rejected
    assert validate_run_options(plan, session="s1", no_open=True, close_on_success=True) == []


def test_validate_run_options_keep_open_and_reload_rules() -> None:
    plan = {"url": "https://example.com", "reset": True, "steps": [{"action": "click", "target": "Go"}]}
    assert validate_run_options(plan, keep_open=True) == []
    assert validate_run_options(plan, session="s1", reload_page=True) == []
    assert validate_run_options(plan, session="s1", reload_page=True, close_on_success=True) == []
    assert validate_run_options(plan, session=None, reload_page=True)
    assert validate_run_options(plan, session="s1", no_open=True, reload_page=True)
    assert validate_run_options(plan, session="s1", keep_open=True, close_on_success=True)


def test_upload_validation_and_command() -> None:
    from agent_browser_decisions.core import apply_plan_vars, command_for, format_stopped_snapshot

    valid_plan = {
        "url": "https://example.com/upload",
        "vars": {"doc": "/tmp/resume.pdf"},
        "steps": [{"action": "upload", "target": "Resume file", "value": "{{doc}}"}],
    }
    expanded, merged, var_problems = apply_plan_vars(valid_plan)
    assert var_problems == []
    assert merged == {"doc": "/tmp/resume.pdf"}
    assert validate_plan(expanded) == []
    assert command_for(expanded["steps"][0], "e2") == ["upload", "@e2", "/tmp/resume.pdf"]

    # Missing/empty value or value_env/secret rejected
    bad_empty = {"steps": [{"action": "upload", "target": "Resume file", "value": "   "}]}
    assert any("upload requires a non-empty string file path value" in p for p in validate_plan(bad_empty))
    bad_env = {"steps": [{"action": "upload", "target": "Resume file", "value_env": "DOC_PATH"}]}
    assert any("value_env is only valid for fill" in p for p in validate_plan(bad_env))
    bad_secret = {"steps": [{"action": "upload", "target": "Resume file", "value": "/a", "secret": True}]}
    assert any("secret is only valid for fill" in p for p in validate_plan(bad_secret))

    # Iframe ancestor context preserved for Tier-2 disambiguation
    snap = """- Iframe "Payment frame" [ref=e3]
  - textbox "Card number" [ref=e8]
  - button "Pay Now" [ref=e7]"""
    elems = parse_snapshot(snap)
    card = next(e for e in elems if e["ref"] == "e8")
    assert card["context"] == ["Iframe 'Payment frame'"]

    # format_stopped_snapshot redacts secrets and bounds length
    redacted = format_stopped_snapshot("- textbox 'Pass' [ref=e1]: super-secret-pw", {"super-secret-pw"})
    assert redacted is not None and "super-secret-pw" not in redacted and "[REDACTED]" in redacted
    long_snap = format_stopped_snapshot("x" * 100, set(), max_chars=20)
    assert long_snap is not None and long_snap.endswith("... (truncated)")
    assert format_stopped_snapshot("   ", set()) is None


@given(
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)),
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)),
    st.one_of(st.none(), st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)),
)
def test_resolve_step_settle_s_precedence(
    step_timeout: float | None,
    cli_settle: float | None,
    plan_settle: float | None,
) -> None:
    from agent_browser_decisions.core import resolve_step_settle_s

    step: dict[str, object] = {"action": "click", "target": "Go"}
    if step_timeout is not None:
        step["timeout_s"] = step_timeout
    got = resolve_step_settle_s(step, plan_settle_s=plan_settle, cli_settle_s=cli_settle, default=3.0)
    if step_timeout is not None:
        assert got == step_timeout
    elif cli_settle is not None:
        assert got == cli_settle
    elif plan_settle is not None:
        assert got == plan_settle
    else:
        assert got == 3.0


def test_validate_plan_settle_s_and_step_timeout_s() -> None:
    valid = {
        "settle_s": 0.5,
        "steps": [
            {"action": "click", "target": "Open", "timeout_s": 0},
            {"action": "fill", "target": "Name", "value": "A", "timeout_s": 2.5},
        ],
    }
    assert validate_plan(valid) == []
    assert any("settle_s" in p for p in validate_plan({"settle_s": -1, "steps": [{"action": "click", "target": "Go"}]}))
    assert any(
        "settle_s" in p for p in validate_plan({"settle_s": True, "steps": [{"action": "click", "target": "Go"}]})
    )
    assert any(
        "timeout_s" in p for p in validate_plan({"steps": [{"action": "click", "target": "Go", "timeout_s": -0.1}]})
    )
    assert any(
        "press" in p for p in validate_plan({"steps": [{"action": "press", "value": "Enter", "timeout_s": 0.5}]})
    )
