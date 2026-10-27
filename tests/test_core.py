"""Offline properties of the pure core. No browser, no paid API."""

from hypothesis import given
from hypothesis import strategies as st

from ab_decide import MAX_CHOICES, compatible, decision_request, match_label, normalize, parse_snapshot

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


def test_parse_keeps_role_name_value_and_attrs():
    parsed = {e["ref"]: e for e in parse_snapshot(SNAPSHOT)}
    assert parsed["e75"]["value"] == "Round trip" and parsed["e75"]["role"] == "combobox"
    assert parsed["e79"]["name"] == "Where to?"
    assert "checked=false" in parsed["e8"]["attrs"]
    assert len(parsed) == 8


def test_label_match_folds_accents_prefix_and_escalates_ambiguity():
    elements = parse_snapshot(SNAPSHOT)
    assert match_label({"action": "click", "target": "Zurich, Switzerland"}, elements)[0]["ref"] == "e4"
    assert (
        match_label({"action": "click", "target": "Change ticket type.", "role": "combobox"}, elements)[0]["ref"]
        == "e75"
    )
    assert match_label({"action": "click", "target": "Turing machine"}, elements) == (None, "ambiguous")
    assert match_label({"action": "click", "target": "Flights"}, elements) == (None, "missing")  # heading


@given(st.lists(ELEMENT, max_size=40), st.text(max_size=10), st.sampled_from(["click", "fill", "check"]))
def test_label_hit_is_unique_compatible_and_equal_or_prefix(elements, target, action):
    step = {"action": action, "target": target}
    hit, status = match_label(step, elements)
    if hit:
        assert status == "hit" and hit in compatible(step, elements)
        assert normalize(hit["name"]).startswith(normalize(target))


@given(st.lists(ELEMENT, min_size=2, max_size=400, unique_by=lambda e: e["ref"]), st.text(max_size=10))
def test_decision_choices_are_bounded_observed_refs(elements, target):
    request = decision_request({"action": "click", "target": target}, "goal", elements, "https://x.test")
    values = [c["value"] for c in request["questions"][0]["choices"]]
    assert len(values) <= MAX_CHOICES and len(set(values)) == len(values)
    assert set(values) <= {e["ref"] for e in elements}


def test_same_name_elements_carry_context_and_occurrence():
    text = """- link "Turing machine" [ref=e85]
- table
  - cell "Known for Enigma"
    - list
      - link "Turing machine" [ref=e1529]"""
    a, b = parse_snapshot(text)
    assert a["context"] == [] and a["occurrence"] == "1 of 2 with this name"
    assert b["context"] == ["cell 'Known for Enigma'"] and b["occurrence"] == "2 of 2 with this name"
    from ab_decide import describe

    assert "in: cell 'Known for Enigma'" in describe(b) and "top level" in describe(a)


def test_regex_nth_selects_positionally_in_document_order():
    text = "\n".join(f'- link "{n}comments" [ref=e{i}]' for i, n in enumerate([42, 39, 37, 60], 1))
    text += '\n- link "comments" [ref=e9]\n- link "discuss" [ref=e10]'
    elements = parse_snapshot(text)
    step = {"action": "click", "target": r"^(\d+\s*comments?|discuss)$", "match": "regex", "nth": 3}
    assert match_label(step, elements)[0]["ref"] == "e3"
    assert match_label(step | {"nth": 6}, elements) == (None, "missing")
    assert match_label(step | {"nth": 5}, elements)[0]["ref"] == "e10"
