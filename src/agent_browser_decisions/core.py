"""Pure domain logic for snapshot parsing, matching, validation, verification, and secret hygiene.

No network, subprocess, filesystem, or mutable global state belongs in this module.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from typing import Any

ROLES: dict[str, set[str]] = {
    "click": {
        "button",
        "link",
        "option",
        "checkbox",
        "radio",
        "tab",
        "gridcell",
        "combobox",
        "menuitem",
        "menuitemradio",
        "menuitemcheckbox",
        "switch",
        "textbox",
        "searchbox",
    },
    "fill": {"textbox", "searchbox", "combobox"},
    "select": {"combobox", "listbox"},
    "check": {"checkbox", "radio", "switch"},
}
MAX_CHOICES = 255
DEFAULT_DECISION_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

QUOTED = r'"(?P<name>(?:[^"\\]|\\.)*)"'
LINE = re.compile(
    rf"^\s*- (?P<role>[\w-]+)(?: {QUOTED})? \[(?P<attrs>[^\]]*?)ref=(?P<ref>e\d+)[^\]]*\](?::\s?(?P<value>.*))?$"
)
ANY = re.compile(rf"^(?P<indent>\s*)- (?P<role>[\w-]+)(?: {QUOTED})?")
ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONTEXT_ROLES = {"cell", "row", "listitem", "region", "navigation", "main", "complementary", "dialog", "banner"}
PLAN_KEYS = {"url", "goal", "steps", "verify", "reset", "verify_timeout_s", "$schema"}
STEP_KEYS = {
    "action",
    "target",
    "value",
    "value_env",
    "secret",
    "role",
    "nth",
    "match",
    "describe",
    "expect",
    "navigates",
    "once",
    "decide",
}
PRESS_KEYS = {"action", "value", "expect", "once"}
ALL_ACTIONS = tuple(sorted([*ROLES, "press"]))


def _strip_latin_marks(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch)
    if len(decomposed) > 1 and "LATIN" in unicodedata.name(decomposed[0], ""):
        return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return ch


def normalize(text: str) -> str:
    """Case-, width-, and Latin-diacritic-insensitive normalisation that preserves non-Latin scripts."""
    nfkc = unicodedata.normalize("NFKC", text)
    folded = "".join(_strip_latin_marks(c) for c in nfkc)
    return " ".join(folded.lower().split())


def _unescape_name(raw: str | None) -> str:
    if not raw:
        return ""
    return re.sub(r'\\(["\\])', r"\1", raw).strip()


def parse_snapshot(text: str) -> list[dict[str, Any]]:
    """Parse an accessibility tree snapshot into ref-bearing elements with ancestor context and occurrence."""
    elements: list[dict[str, Any]] = []
    ancestors: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        if match := ANY.match(line):
            indent = len(match.group("indent"))
            while ancestors and ancestors[-1][0] >= indent:
                ancestors.pop()
            ancestors.append((indent, match.group("role"), _unescape_name(match.group("name"))))
        if match := LINE.match(line):
            # Nearest named enclosing regions, from innermost out, tell same-name elements apart.
            context = [
                f"{role} '{name[:40]}'" for _, role, name in reversed(ancestors[:-1]) if name and role in CONTEXT_ROLES
            ][:2]
            elements.append(
                {
                    "ref": match.group("ref"),
                    "role": match.group("role"),
                    "name": _unescape_name(match.group("name")),
                    "attrs": match.group("attrs").strip(),
                    "value": (match.group("value") or "").strip(),
                    "context": context,
                }
            )
    counts = Counter((e["role"], normalize(e["name"])) for e in elements if normalize(e["name"]))
    seen: Counter[tuple[str, str]] = Counter()
    for element in elements:
        key = (element["role"], normalize(element["name"]))
        total = counts.get(key, 0)
        if total > 1:
            seen[key] += 1
            element["occurrence"] = f"{seen[key]} of {total} with this name"
    return elements


def compatible(
    step: Mapping[str, Any],
    elements: list[dict[str, Any]],
    *,
    use_role: bool = True,
) -> list[dict[str, Any]]:
    allowed = {step["role"]} if (use_role and step.get("role")) else ROLES[step["action"]]
    return [e for e in elements if e["role"] in allowed]


def match_label(
    step: Mapping[str, Any],
    elements: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Return (element | None, 'hit' | 'ambiguous' | 'missing').

    Default: exact normalised label, then unique prefix. match='regex' matches the raw name instead.
    nth=k picks the 1-based k-th hit in document order, for positional plans ('the third result').
    """
    pool = compatible(step, elements)
    if step.get("match") == "regex":
        pattern = re.compile(step["target"], re.IGNORECASE)
        predicates = [lambda e: pattern.search(e["name"])]
    else:
        label = normalize(step["target"])
        if not label:
            return None, "missing"
        predicates = [
            lambda e: normalize(e["name"]) == label,
            lambda e: normalize(e["name"]).startswith(label),
        ]
    nth = step.get("nth")
    if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
        return None, "missing"
    for predicate in predicates:
        hits = [e for e in pool if predicate(e)]
        if nth is not None:
            if len(hits) >= nth:
                return hits[nth - 1], "hit"
            if hits:
                return None, "missing"
            continue
        if len(hits) == 1:
            return hits[0], "hit"
        if len(hits) > 1:
            return None, "ambiguous"
    return None, "missing"


def describe(element: Mapping[str, Any]) -> str:
    parts = [element["role"], element["name"]]
    if element["value"]:
        parts.append(f"value={element['value']}")
    parts += [a.strip().rstrip(",") for a in element["attrs"].split(",") if "=" in a]
    if element.get("context"):
        parts.append("in: " + " < ".join(element["context"]))
    else:
        parts.append("in: top level of the page content")
    if element.get("occurrence"):
        parts.append(element["occurrence"])
    return " | ".join(p for p in parts if p)


def decision_request(
    step: Mapping[str, Any],
    goal: str,
    candidates: list[dict[str, Any]],
    url: str,
    *,
    model: str = DEFAULT_DECISION_MODEL,
) -> dict[str, Any]:
    """Build a /v1/decisions request whose choices are bounded to observed refs only."""
    return {
        "model": model,
        "context": f"Page URL: {url}\nGoal: {goal}",
        "questions": [
            {
                "id": "element",
                "prompt": (
                    f"Action: {step['action']}\n"
                    f"Target: {step.get('describe') or step['target']}\n"
                    "Which observed element should this step act on?"
                ),
                "choices": [{"value": e["ref"], "description": describe(e)} for e in candidates[:MAX_CHOICES]],
                "allow_abstention": True,
            }
        ],
    }


def chosen_ref(response: Any) -> tuple[str | None, Any]:
    """Extract (ref, confidence) from a /v1/decisions payload, or (None, reason) on abstention/malformed output."""
    if not isinstance(response, dict):
        return None, "malformed_response"
    decisions = response.get("decisions")
    if not isinstance(decisions, list) or not decisions or not isinstance(decisions[0], dict):
        return None, "malformed_response"
    decision = decisions[0]
    if decision.get("status") != "answered":
        return None, decision.get("abstention_reason") or decision.get("status") or "unanswered"
    choice = decision.get("choice")
    if not isinstance(choice, str) or not choice:
        return None, "missing_choice"
    return choice, decision.get("confidence")


def command_for(step: Mapping[str, Any], ref: str) -> list[str]:
    """Translate a step and observed ref into agent-browser CLI arguments."""
    action = step["action"]
    if action in ("click", "check"):
        return [action, f"@{ref}"]
    return [action, f"@{ref}", step["value"]]


def same_field_value(raw_stdout: str, expected: str) -> bool:
    """Compare raw `agent-browser get value` stdout against expected text, stripping at most one trailing newline."""
    return (
        raw_stdout == expected
        or raw_stdout.removesuffix("\r\n") == expected
        or raw_stdout.removesuffix("\n") == expected
    )


def evaluate_verify_output(stdout: str | None) -> tuple[bool, Any]:
    """Parse stdout from a zero-exit `agent-browser eval` into (passed: bool, value).

    Valid JS truthy results serialize as JSON (`true`, `1`, `"ok"`, `{}`, `[]`).
    Non-JSON output (such as `undefined`, `NaN`, or a printed JS error) never passes.
    """
    text = (stdout or "").strip()
    if not text:
        return False, ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return False, text
    if isinstance(value, dict):
        if "passed" in value:
            return value["passed"] is True, value
        return True, value
    if isinstance(value, list):
        return True, value
    if isinstance(value, float) and math.isnan(value):
        return False, value
    return bool(value), value


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from a .env file (pure)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def document(url: str) -> str:
    return url.split("#", 1)[0]


def hash_route(href: str) -> bool:
    """Fragment-only links (#/completed, #foo) stay on the same document; the fragment itself is the navigation."""
    href = href.strip()
    return href.startswith("#") and len(href) > 1


def redact_text(text: str, secrets: set[str] | frozenset[str]) -> str:
    """Scrub any resolved secret values from strings before logging or returning JSON."""
    if not text or not secrets:
        return text
    out = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_nonneg_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def validate_plan(plan: Any) -> list[str]:
    """Total schema check over arbitrary JSON -> list of problems (empty means valid)."""
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    problems: list[str] = []
    if unknown := set(plan) - PLAN_KEYS:
        problems.append(f"unknown plan keys: {sorted(unknown)}")
    if "$schema" in plan and not isinstance(plan["$schema"], str):
        problems.append("$schema must be a string")
    if "url" in plan and (not isinstance(plan["url"], str) or not plan["url"].strip()):
        problems.append("url must be a non-empty string")
    if "goal" in plan and not isinstance(plan["goal"], str):
        problems.append("goal must be a string")
    if "verify" in plan and (not isinstance(plan["verify"], str) or not plan["verify"].strip()):
        problems.append("verify must be a non-empty string")
    if "reset" in plan and not isinstance(plan["reset"], bool):
        problems.append("reset must be a boolean")
    if plan.get("reset") is True and (not isinstance(plan.get("url"), str) or not plan["url"].strip()):
        problems.append("reset=true requires a valid url")
    if "verify_timeout_s" in plan and not _is_finite_nonneg_number(plan["verify_timeout_s"]):
        problems.append("verify_timeout_s must be a finite non-negative number")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return [*problems, "steps must be a non-empty list"]
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            problems.append(f"step {index}: must be an object")
            continue
        if unknown := set(step) - STEP_KEYS:
            problems.append(f"step {index}: unknown keys {sorted(unknown)}")
        action = step.get("action")
        if not isinstance(action, str) or (action != "press" and action not in ROLES):
            problems.append(f"step {index}: action must be one of {list(ALL_ACTIONS)}")
            continue
        if action == "press":
            if disallowed := set(step) - PRESS_KEYS:
                problems.append(f"step {index}: keys {sorted(disallowed)} are not valid for press")
            if not isinstance(step.get("value"), str) or not step["value"].strip():
                problems.append(f"step {index}: press requires a non-empty string value")
        else:
            if not isinstance(step.get("target"), str) or not step["target"].strip():
                problems.append(f"step {index}: target label is required")
            has_value = "value" in step
            has_value_env = "value_env" in step
            if action == "fill":
                if has_value and has_value_env:
                    problems.append(f"step {index}: fill cannot specify both value and value_env")
                elif not has_value and not has_value_env:
                    problems.append(f"step {index}: fill requires value or value_env")
                elif has_value and not isinstance(step.get("value"), str):
                    problems.append(f"step {index}: fill requires a string value")
                elif has_value_env and (
                    not isinstance(step.get("value_env"), str) or not ENV_VAR_NAME.match(step["value_env"])
                ):
                    problems.append(f"step {index}: value_env must be a valid environment variable name")
            elif action == "select":
                if has_value_env:
                    problems.append(f"step {index}: value_env is only valid for fill")
                if not isinstance(step.get("value"), str):
                    problems.append(f"step {index}: select requires a string value")
            else:
                if has_value or has_value_env:
                    problems.append(f"step {index}: {action} does not accept value or value_env")
        if "secret" in step:
            if not isinstance(step["secret"], bool):
                problems.append(f"step {index}: secret must be a boolean")
            elif action != "fill":
                problems.append(f"step {index}: secret is only valid for fill")
        if "role" in step and action != "press":
            role = step["role"]
            if not isinstance(role, str) or role not in ROLES[action]:
                problems.append(f"step {index}: role for {action} must be one of {sorted(ROLES[action])}")
        if "describe" in step and not isinstance(step["describe"], str):
            problems.append(f"step {index}: describe must be a string")
        if "match" in step:
            if step["match"] != "regex":
                problems.append(f"step {index}: match must be 'regex'")
            elif isinstance(step.get("target"), str):
                try:
                    re.compile(step["target"])
                except re.error as err:
                    problems.append(f"step {index}: invalid regex: {err}")
        if "nth" in step and (not _is_int(step["nth"]) or step["nth"] < 1):
            problems.append(f"step {index}: nth must be a positive integer")
        if "expect" in step and step["expect"] != "navigation":
            problems.append(f"step {index}: expect must be 'navigation'")
        for flag in ("decide", "once", "navigates"):
            if flag in step and not isinstance(step[flag], bool):
                problems.append(f"step {index}: {flag} must be a boolean")
        if step.get("decide") is True and ("nth" in step or "match" in step):
            problems.append(f"step {index}: decide=true cannot be combined with nth or match")
    return problems


def validate_run_options(
    plan: Any,
    *,
    session: str | None = None,
    from_step: int = 1,
) -> list[str]:
    """Runtime option checks over an already schema-valid plan (pure)."""
    problems: list[str] = []
    if not _is_int(from_step) or from_step < 1:
        problems.append("from_step must be a positive integer")
    elif isinstance(plan, dict) and isinstance(plan.get("steps"), list) and from_step > len(plan["steps"]):
        problems.append(f"from_step ({from_step}) exceeds step count ({len(plan['steps'])})")
    if _is_int(from_step) and from_step > 1 and not session:
        problems.append("--from-step > 1 requires --session")
    if isinstance(plan, dict) and plan.get("reset") is True and session and from_step == 1:
        problems.append("cannot combine reset=true with --session")
    return problems


def resolve_step_values(
    steps: list[dict[str, Any]],
    environ: Mapping[str, str],
    *,
    start_index: int = 1,
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Resolve `value_env` references in fill steps -> (resolved_steps, secrets_to_redact, problems)."""
    resolved: list[dict[str, Any]] = []
    secrets: set[str] = set()
    problems: list[str] = []
    for index, step in enumerate(steps, start_index):
        if "value_env" in step:
            var_name = step["value_env"]
            val = environ.get(var_name)
            if not val:
                problems.append(f"step {index}: environment variable '{var_name}' is not set or empty")
                continue
            resolved.append({**step, "value": val, "secret": step.get("secret", True)})
            if step.get("secret", True):
                secrets.add(val)
        else:
            resolved.append(dict(step))
            if step.get("secret") is True and isinstance(step.get("value"), str) and step["value"]:
                secrets.add(step["value"])
    return resolved, secrets, problems
