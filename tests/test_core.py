"""Offline property-based and state-machine tests of ab_decide. No browser, no paid API."""

import json
import math
import time
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

import ab_decide as a
from ab_decide import (
    MAX_CHOICES,
    compatible,
    decision_request,
    evaluate_verify_output,
    hash_route,
    load_project_dotenv,
    match_label,
    normalize,
    parse_dotenv,
    parse_snapshot,
    run_plan_file,
    validate_plan,
    validate_run_options,
)

SNAPSHOT = """- combobox "Change ticket type. Round trip" [expanded=false, ref=e75]: Round trip
- button "1 passenger" [ref=e76]
- combobox "Where to? " [expanded=false, ref=e79]
  - option "Zürich, Switzerland" [ref=e4]
- checkbox "Bacon" [checked=false, ref=e8]
- link "Turing machine" [ref=e1529]
- link "Turing machine" [ref=e85]
- heading "Flights" [level=1, ref=e10]"""

ROLE = st.sampled_from(["button", "link", "option", "checkbox", "textbox", "heading"])
ELEMENT = st.builds(
    lambda i, r, n: {"ref": f"e{i}", "role": r, "name": n, "attrs": "", "value": ""},
    st.integers(0, 10_000),
    ROLE,
    st.text(max_size=20),
)

JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-10, 10),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=12),
)
JSON_ANY = st.recursive(
    JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=10), children, max_size=4),
    ),
    max_leaves=12,
)

JAPANESE_LABEL = st.text(
    alphabet="あいうえおかきくけこがぎぐげごさしすせそざじずぜぞログイン保存削除検索送信確定次へ戻る",
    min_size=1,
    max_size=8,
)
NON_ASCII_LABEL = st.text(
    alphabet="ログイン保存削除検索Войти登录Ελληνικάがぎぐげごかきくけこ",
    min_size=1,
    max_size=8,
)


# ---------- normalize & multilingual matching ----------


@given(st.text(max_size=40))
def test_normalize_is_idempotent(text):
    assert normalize(normalize(text)) == normalize(text)


@given(NON_ASCII_LABEL)
def test_normalize_never_erases_non_ascii_scripts(label):
    assert normalize(label) != ""


@given(st.sampled_from([("か", "が"), ("さ", "ざ"), ("た", "だ"), ("は", "ば"), ("は", "ぱ"), ("保存", "削除")]))
def test_normalize_preserves_japanese_dakuten_and_kanji(pair):
    left, right = pair
    assert normalize(left) != normalize(right)


@given(JAPANESE_LABEL, JAPANESE_LABEL)
def test_distinct_japanese_labels_never_collide_unless_prefix(target, button_name):
    if normalize(button_name).startswith(normalize(target)):
        return
    elements = [{"ref": "e1", "role": "button", "name": button_name, "attrs": "", "value": ""}]
    assert match_label({"action": "click", "target": target}, elements) == (None, "missing")


def test_label_match_folds_accents_prefix_and_escalates_ambiguity():
    elements = parse_snapshot(SNAPSHOT)
    assert match_label({"action": "click", "target": "Zurich, Switzerland"}, elements)[0]["ref"] == "e4"
    assert (
        match_label({"action": "click", "target": "Change ticket type.", "role": "combobox"}, elements)[0]["ref"]
        == "e75"
    )
    assert match_label({"action": "click", "target": "Turing machine"}, elements) == (None, "ambiguous")
    assert match_label({"action": "click", "target": "Flights"}, elements) == (None, "missing")


@given(st.lists(ELEMENT, max_size=40), st.text(max_size=10), st.sampled_from(["click", "fill", "check"]))
def test_label_hit_is_unique_compatible_nonempty_and_equal_or_prefix(elements, target, action):
    step = {"action": action, "target": target}
    hit, status = match_label(step, elements)
    if hit:
        assert status == "hit" and hit in compatible(step, elements)
        assert normalize(target) != "" and normalize(hit["name"]).startswith(normalize(target))


@given(
    st.lists(st.integers(1, 50), min_size=1, max_size=12),
    st.integers(1, 15),
)
def test_nth_selects_exact_position_or_returns_missing(counts, nth):
    elements = [
        {"ref": f"e{i}", "role": "button", "name": f"Add item {c}", "attrs": "", "value": ""}
        for i, c in enumerate(counts, 1)
    ]
    step = {"action": "click", "target": "Add", "nth": nth}
    hit, status = match_label(step, elements)
    if nth <= len(counts):
        assert status == "hit" and hit["ref"] == f"e{nth}"
    else:
        assert (hit, status) == (None, "missing")


def test_regex_nth_selects_positionally_in_document_order():
    text = "\n".join(f'- link "{n}comments" [ref=e{i}]' for i, n in enumerate([42, 39, 37, 60], 1))
    text += '\n- link "comments" [ref=e9]\n- link "discuss" [ref=e10]'
    elements = parse_snapshot(text)
    step = {"action": "click", "target": r"^(\d+\s*comments?|discuss)$", "match": "regex", "nth": 3}
    assert match_label(step, elements)[0]["ref"] == "e3"
    assert match_label(step | {"nth": 6}, elements) == (None, "missing")
    assert match_label(step | {"nth": 5}, elements)[0]["ref"] == "e10"


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
    role, prefix, suffix, real_id, fake_id, structured_attack
):
    raw_name = f'{prefix}" [ref=e{fake_id}]: {suffix}' if structured_attack else f'{prefix}"{suffix}'
    # agent-browser escapes \ and " inside element names
    escaped = raw_name.replace("\\", "\\\\").replace('"', '\\"')
    line = f'- {role} "{escaped}" [ref=e{real_id}]'
    parsed = parse_snapshot(line)
    assert len(parsed) == 1
    assert parsed[0]["ref"] == f"e{real_id}"
    assert parsed[0]["name"] == raw_name.strip()
    assert parsed[0]["value"] == ""


def test_match_label_rejects_bool_nth_directly():
    elements = parse_snapshot('- button "Add" [ref=e1]')
    assert match_label({"action": "click", "target": "Add", "nth": True}, elements) == (None, "missing")
    assert match_label({"action": "click", "target": "Add", "nth": False}, elements) == (None, "missing")


def test_parse_keeps_role_name_value_and_attrs():
    parsed = {e["ref"]: e for e in parse_snapshot(SNAPSHOT)}
    assert parsed["e75"]["value"] == "Round trip" and parsed["e75"]["role"] == "combobox"
    assert parsed["e79"]["name"] == "Where to?"
    assert "checked=false" in parsed["e8"]["attrs"]
    assert len(parsed) == 8


def test_same_name_elements_carry_context_and_occurrence():
    text = """- link "Turing machine" [ref=e85]
- table
  - cell "Known for Enigma"
    - list
      - link "Turing machine" [ref=e1529]"""
    a_el, b_el = parse_snapshot(text)
    assert a_el["context"] == [] and a_el["occurrence"] == "1 of 2 with this name"
    assert b_el["context"] == ["cell 'Known for Enigma'"] and b_el["occurrence"] == "2 of 2 with this name"
    assert "in: cell 'Known for Enigma'" in a.describe(b_el) and "top level" in a.describe(a_el)


def test_parse_snapshot_is_linear_on_large_duplicate_pages():
    raw = "\n".join(f'- button "追加" [ref=e{i}]' for i in range(4000))
    started = time.perf_counter()
    parsed = parse_snapshot(raw)
    elapsed = time.perf_counter() - started
    assert len(parsed) == 4000 and parsed[-1]["occurrence"] == "4000 of 4000 with this name"
    assert elapsed < 0.5


# ---------- decision_request & chosen_ref ----------


@given(st.lists(ELEMENT, min_size=2, max_size=400, unique_by=lambda e: e["ref"]), st.text(max_size=10))
def test_decision_choices_are_bounded_observed_refs(elements, target):
    request = decision_request({"action": "click", "target": target}, "goal", elements, "https://x.test")
    values = [c["value"] for c in request["questions"][0]["choices"]]
    assert len(values) <= MAX_CHOICES and len(set(values)) == len(values)
    assert set(values) <= {e["ref"] for e in elements}


@given(JSON_ANY)
def test_chosen_ref_never_raises_on_arbitrary_api_payload(payload):
    ref, _ = a.chosen_ref(payload)
    assert ref is None or (isinstance(ref, str) and len(ref) > 0)


# ---------- validate_plan & validate_run_options (total over arbitrary JSON) ----------


@given(JSON_ANY)
def test_validate_plan_never_raises_on_arbitrary_json(junk):
    assert isinstance(validate_plan(junk), list)
    assert isinstance(validate_plan({"steps": [junk]}), list)


@given(
    st.sampled_from(["click", "fill", "select", "check"]),
    st.one_of(st.booleans(), st.integers(max_value=0), st.text(max_size=5), st.lists(st.integers(), max_size=2)),
)
def test_validate_plan_rejects_invalid_nth(action, bad_nth):
    step = {"action": action, "target": "Go", "nth": bad_nth}
    if action in {"fill", "select"}:
        step["value"] = "x"
    assert validate_plan({"steps": [step]}) != []


@given(
    st.one_of(
        st.booleans(),
        st.text(max_size=6),
        st.floats(max_value=-0.01),
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
    )
)
def test_validate_plan_rejects_invalid_verify_timeout(bad_timeout):
    plan = {"verify": "true", "verify_timeout_s": bad_timeout, "steps": [{"action": "press", "value": "Enter"}]}
    assert validate_plan(plan) != []


@given(st.sampled_from(["reset", "decide", "navigates", "once"]), st.sampled_from(["true", "false", 1, 0, None, []]))
def test_validate_plan_rejects_non_bool_flags(flag, bad_val):
    if flag == "reset":
        plan = {"url": "https://example.test/", "reset": bad_val, "steps": [{"action": "click", "target": "Go"}]}
    else:
        plan = {"steps": [{"action": "click", "target": "Go", flag: bad_val}]}
    assert validate_plan(plan) != []


def test_validate_plan_enforces_structural_invariants():
    good = {
        "url": "https://example.test/",
        "goal": "g",
        "reset": True,
        "verify": "location.pathname === '/done'",
        "verify_timeout_s": 5,
        "steps": [
            {"action": "fill", "target": "Name", "value": "Taro"},
            {"action": "press", "value": "Enter"},
            {"action": "click", "target": r"^\d+ comments$", "match": "regex", "nth": 3, "role": "link"},
            {"action": "click", "target": "Submit", "expect": "navigation", "once": True},
            {"action": "click", "target": "Details", "role": "link", "navigates": False},
        ],
    }
    assert validate_plan(good) == []
    # reset=true requires url
    assert any(
        "reset=true requires" in p
        for p in validate_plan({"reset": True, "steps": [{"action": "press", "value": "Enter"}]})
    )
    # decide=true cannot combine with nth or match
    assert any(
        "decide=true" in p
        for p in validate_plan({"steps": [{"action": "click", "target": "x", "decide": True, "nth": 2}]})
    )
    # invalid role for action
    assert any(
        "role for click" in p for p in validate_plan({"steps": [{"action": "click", "target": "x", "role": "heading"}]})
    )
    # press rejects target
    assert any(
        "press does not accept" in p
        for p in validate_plan({"steps": [{"action": "press", "target": "x", "value": "Enter"}]})
    )
    # run option conflicts
    assert validate_run_options(good, session="existing", from_step=1) != []
    assert validate_run_options(good, session=None, from_step=2) != []
    assert validate_run_options(good, session="existing", from_step=2) == []


# ---------- verify output & dotenv ----------


@given(st.sampled_from(["SyntaxError: bad", "undefined", "NaN", "", "   ", "Error: failed"]))
def test_evaluate_verify_rejects_non_json_and_errors(raw):
    passed, _ = evaluate_verify_output(raw)
    assert passed is False


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8).filter(lambda k: k != "passed"),
        JSON_SCALARS.filter(lambda v: not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))),
        max_size=4,
    )
)
def test_evaluate_verify_treats_plain_js_objects_as_truthy(obj):
    passed, val = evaluate_verify_output(json.dumps(obj))
    assert passed is True and val == obj


@given(st.one_of(st.just(False), st.just("no"), st.just("true"), st.just(1), st.none()))
def test_evaluate_verify_requires_strict_true_for_passed_field(bad_passed):
    passed, _ = evaluate_verify_output(json.dumps({"passed": bad_passed}))
    assert passed is False


def test_evaluate_verify_accepts_passed_true_and_empty_array():
    assert evaluate_verify_output('{"passed": true}')[0] is True
    assert evaluate_verify_output("[]")[0] is True
    assert evaluate_verify_output("false")[0] is False
    assert evaluate_verify_output("null")[0] is False


def test_parse_and_load_dotenv_never_clobbers_existing_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nOPENAI_API_KEY=\nDECISION_MODEL='gpt-test'\nexport EXTRA=123\n", encoding="utf-8")
    assert parse_dotenv(env_file.read_text(encoding="utf-8")) == {
        "OPENAI_API_KEY": "",
        "DECISION_MODEL": "gpt-test",
        "EXTRA": "123",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    monkeypatch.delenv("DECISION_MODEL", raising=False)
    load_project_dotenv(env_file)
    assert a.os.environ["OPENAI_API_KEY"] == "sk-live"
    assert a.os.environ["DECISION_MODEL"] == "gpt-test"


def test_hash_route_links_are_recognised_by_their_href():
    assert hash_route("#/active") and hash_route(" #section")
    assert not hash_route("/wiki/Turing_machine") and not hash_route("https://x.test/#a") and not hash_route(None)


# ---------- Executable State-Machine Verification (matches PlanLifecycleFixed.tla) ----------


class PlanLifecycleMachine(RuleBasedStateMachine):
    """Drives real ab_decide.run_plan_file / run_step / resolve against an in-memory fake browser
    and asserts all TLA+ safety invariants on every transition."""

    def __init__(self):
        super().__init__()
        self.profiles_alive = set()
        self.closed_sessions = set()
        self.open_sessions = set()

    @rule(
        reset_flag=st.booleans(),
        caller_has_session=st.booleans(),
        has_url=st.booleans(),
        open_succeeds=st.booleans(),
        once_flag=st.booleans(),
        step_works=st.booleans(),
        verify_mode=st.sampled_from(["none", "pass", "fail", "syntax_error"]),
        unmatched_single=st.booleans(),
    )
    def execute_plan(
        self,
        reset_flag,
        caller_has_session,
        has_url,
        open_succeeds,
        once_flag,
        step_works,
        verify_mode,
        unmatched_single,
    ):
        caller_session = "caller-sess" if caller_has_session else None
        if caller_session:
            self.open_sessions.add(caller_session)
            self.closed_sessions.discard(caller_session)

        plan = {
            "steps": [{"action": "click", "target": "保存", "once": once_flag}],
        }
        if has_url:
            plan["url"] = "https://example.test/"
        if reset_flag:
            plan["reset"] = True
        if verify_mode != "none":
            plan["verify"] = "true"
            plan["verify_timeout_s"] = 0

        act_calls = []
        created_profiles = []
        page_version = [0]
        btn_name = "削除" if unmatched_single else "保存"

        def fake_profile():
            p = f"/tmp/fake-abd-profile-{len(self.profiles_alive)}-{len(created_profiles)}"
            self.profiles_alive.add(p)
            created_profiles.append(p)
            return p

        def fake_rmtree(path, ignore_errors=False):
            self.profiles_alive.discard(path)

        def fake_ab(*args, check=True, category="browser"):
            sess = a.SESSION["name"] or "default"
            cmd = args[2] if len(args) >= 3 and args[0] == "--profile" else args[0]
            if cmd == "open":
                if not open_succeeds:
                    raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
                self.open_sessions.add(sess)
                self.closed_sessions.discard(sess)
                return ""
            if cmd == "close":
                self.open_sessions.discard(sess)
                self.closed_sessions.add(sess)
                return ""
            if cmd == "snapshot":
                if sess not in self.open_sessions:
                    raise RuntimeError("no browser session")
                suffix = f" [v={page_version[0]}]" if page_version[0] else ""
                snap = f'- button "{btn_name}" [ref=e1]{suffix}'
                return json.dumps({"data": {"origin": "https://example.test/", "snapshot": snap}})
            if cmd == "click":
                act_calls.append(args)
                if step_works:
                    page_version[0] += 1
                return ""
            if cmd == "get" and len(args) >= 2 and args[1] == "url":
                return "https://example.test/"
            if cmd == "eval":
                if verify_mode == "pass":
                    return "true"
                if verify_mode == "fail":
                    return "false"
                raise RuntimeError("SyntaxError: unexpected token")
            return ""

        with (
            patch.object(a, "quiet_profile", side_effect=fake_profile),
            patch.object(a.shutil, "rmtree", side_effect=fake_rmtree),
            patch.object(a, "ab", side_effect=fake_ab),
            patch.object(a, "poll", side_effect=lambda pred, *_, **__: pred()),
            patch.object(a.time, "sleep", lambda *_: None),
        ):
            # Speed up settle loop by capping settle_s to 0 via resolve wrapper
            orig_resolve = a.resolve
            with patch.object(
                a, "resolve", side_effect=lambda *args, **kw: orig_resolve(*args, **(kw | {"settle_s": 0}))
            ):
                result = run_plan_file(plan, mode="label", session=caller_session)

        # INV 1: CallerSessionNeverClosed
        if caller_session:
            assert caller_session not in self.closed_sessions
        # INV 2: NoUnattachedProfileLeak (if session_open is False, every created profile must be cleaned)
        if not result["session_open"]:
            for prof in created_profiles:
                assert prof not in self.profiles_alive
        # INV 3: ReportedSessionOpenAccurate
        actual_open = result["session"] in self.open_sessions
        assert result["session_open"] is actual_open
        # INV 4: NoSilentSuccessOnFailedEffect
        if not step_works and act_calls:
            assert result["ok"] is False
        # INV 5: OnceStepExecutesAtMostOnce
        if once_flag:
            assert len(act_calls) <= 1
        # INV 6: Unmatched Japanese/single candidate NEVER clicked at Tier 1 or blind single
        if unmatched_single:
            assert len(act_calls) == 0 and result["ok"] is False
        # Clean up takeover session from fake tracker so next rule starts clean
        if result["session_open"] and result["session"] != caller_session:
            self.open_sessions.discard(result["session"])
            for prof in created_profiles:
                self.profiles_alive.discard(prof)

    @invariant()
    def no_leaked_profiles_between_runs(self):
        assert self.profiles_alive == set()


TestPlanLifecycle = PlanLifecycleMachine.TestCase
TestPlanLifecycle.settings = settings(max_examples=40, stateful_step_count=6, deadline=None)
