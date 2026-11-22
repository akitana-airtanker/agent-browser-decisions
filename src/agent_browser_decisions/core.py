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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
    "upload": {"button"},
}
MAX_CHOICES = 255
NONE_CHOICE = "__none__"
VALID_MATCH_MODES: frozenset[str] = frozenset({"exact", "prefix", "regex"})
DEFAULT_PROVIDER = "openai"
DEFAULT_DECISION_MODEL = "gpt-6-luna"
DEFAULT_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Canonical wire-protocol specification for a decision model provider."""

    name: str
    protocol: str
    default_model: str
    default_base_url: str
    endpoint_path: str
    api_key_envs: tuple[str, ...]
    base_url_env: str
    auth_header: str
    auth_prefix: str


@dataclass(frozen=True, slots=True)
class DecisionConfig:
    """Resolved provider, endpoint, model, and credentials for Tier 2."""

    provider: str
    protocol: str
    model: str
    base_url: str
    endpoint_url: str
    api_key: str | None
    api_key_env: str | None
    auth_header: str
    auth_prefix: str


PROVIDER_ORDER: tuple[str, ...] = ("openai", "openrouter", "gemini", "typesafe", "liquid")
PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        protocol="openai",
        default_model=DEFAULT_DECISION_MODEL,
        default_base_url=DEFAULT_BASE_URL,
        endpoint_path="/decisions",
        api_key_envs=("OPENAI_API_KEY",),
        base_url_env="OPENAI_BASE_URL",
        auth_header="Authorization",
        auth_prefix="Bearer ",
    ),
    "openrouter": ProviderSpec(
        name="openrouter",
        protocol="systemone",
        default_model="liquid/d1.2",
        default_base_url="https://openrouter.ai/api/alpha",
        endpoint_path="/decisions",
        api_key_envs=("OPENROUTER_API_KEY",),
        base_url_env="OPENROUTER_BASE_URL",
        auth_header="Authorization",
        auth_prefix="Bearer ",
    ),
    "gemini": ProviderSpec(
        name="gemini",
        protocol="gemini",
        default_model="gemini-decision-1",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        endpoint_path="/decisions",
        api_key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        base_url_env="GEMINI_BASE_URL",
        auth_header="x-goog-api-key",
        auth_prefix="",
    ),
    "typesafe": ProviderSpec(
        name="typesafe",
        protocol="systemone",
        default_model="jev-latest",
        default_base_url="https://api.typesafe.ai/v1",
        endpoint_path="/systemone",
        api_key_envs=("TYPESAFE_API_KEY",),
        base_url_env="TYPESAFE_BASE_URL",
        auth_header="Authorization",
        auth_prefix="Bearer ",
    ),
    "liquid": ProviderSpec(
        name="liquid",
        protocol="systemone",
        default_model="d1.2",
        default_base_url="https://api.liquid.ai/decisions/v1",
        endpoint_path="/systemone",
        api_key_envs=("LIQUID_API_KEY",),
        base_url_env="LIQUID_BASE_URL",
        auth_header="Authorization",
        auth_prefix="Bearer ",
    ),
}

QUOTED = r'"(?P<name>(?:[^"\\]|\\.)*)"'
LINE = re.compile(
    rf"^\s*- (?P<role>[\w-]+)(?: {QUOTED})? \[(?P<attrs>[^\]]*?)ref=(?P<ref>e\d+)[^\]]*\](?::\s?(?P<value>.*))?$"
)
ANY = re.compile(rf"^(?P<indent>\s*)- (?P<role>[\w-]+)(?: {QUOTED})?")
ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

CONTEXT_ROLES = {
    "cell",
    "row",
    "listitem",
    "region",
    "navigation",
    "main",
    "complementary",
    "dialog",
    "banner",
    "Iframe",
    "iframe",
}
PLAN_KEYS = {"url", "goal", "steps", "verify", "reset", "verify_timeout_s", "settle_s", "vars", "$schema"}
VAR_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
BATCHABLE_ACTIONS = frozenset({"fill", "check", "select"})
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
    "retry",
    "decide",
    "timeout_s",
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

    Default (or match='exact'): exact normalised label only (no silent prefix fallback).
    match='prefix' matches normalised prefix; match='regex' matches the raw name (case-insensitive).
    nth=k picks the 1-based k-th hit in document order, for positional plans ('the third result').
    """
    pool = compatible(step, elements)
    mode = step.get("match") or "exact"
    if mode not in VALID_MATCH_MODES:
        return None, "missing"
    nth = step.get("nth")
    if nth is not None and (isinstance(nth, bool) or not isinstance(nth, int) or nth < 1):
        return None, "missing"
    if mode == "regex":
        pattern = re.compile(step["target"], re.IGNORECASE)
        hits = [e for e in pool if pattern.search(e["name"])]
    else:
        label = normalize(step["target"])
        if not label:
            return None, "missing"
        if mode == "prefix":
            hits = [e for e in pool if normalize(e["name"]).startswith(label)]
        else:
            hits = [e for e in pool if normalize(e["name"]) == label]
    if nth is not None:
        if len(hits) >= nth:
            return hits[nth - 1], "hit"
        return None, "missing"
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


def infer_provider_from_model(model: str | None) -> str | None:
    """Infer a provider name from a well-known decision model ID, or return None."""
    if not model:
        return None
    low = model.strip().lower()
    if not low:
        return None
    # OpenRouter uses `vendor/model` slugs across all 40+ hosted decision models.
    if "/" in low:
        return "openrouter"
    if low.startswith("gemini"):
        return "gemini"
    if low.startswith("jev"):
        return "typesafe"
    if low == "d1" or low.startswith(("d1.", "d1-", "d1:")):
        return "liquid"
    if low.startswith("gpt-"):
        return "openai"
    return None


def resolve_decision_config(
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DecisionConfig:
    """Resolve Tier 2 provider, endpoint URL, model, and API key purely from args + env."""
    env = environ if environ is not None else {}
    raw_provider = (provider if provider is not None else env.get("DECISION_PROVIDER") or "").strip().lower()
    arg_model = (model or "").strip()
    env_model = (env.get("DECISION_MODEL") or "").strip()
    raw_model = arg_model or env_model

    if raw_provider:
        chosen_provider = raw_provider
    elif inferred := infer_provider_from_model(raw_model):
        chosen_provider = inferred
    else:
        chosen_provider = next(
            (name for name in PROVIDER_ORDER if any(env.get(k) for k in PROVIDERS[name].api_key_envs)),
            DEFAULT_PROVIDER,
        )

    spec = PROVIDERS.get(chosen_provider)
    if spec is None:
        allowed = ", ".join(PROVIDER_ORDER)
        raise ValueError(f"unknown provider {chosen_provider!r}; expected one of: {allowed}")

    if arg_model:
        resolved_model = arg_model
    elif env_model:
        env_model_provider = infer_provider_from_model(env_model)
        # Ignore an inherited DECISION_MODEL from .env when it belongs to another provider.
        resolved_model = (
            spec.default_model if (env_model_provider and env_model_provider != chosen_provider) else env_model
        )
    else:
        resolved_model = spec.default_model
    resolved_base = (
        (base_url if base_url is not None else "")
        or (env.get("DECISION_BASE_URL") or "")
        or (env.get(spec.base_url_env) or "")
        or spec.default_base_url
    ).rstrip("/")
    if resolved_base.endswith(("/decisions", "/systemone")):
        endpoint_url = resolved_base
    else:
        endpoint_url = f"{resolved_base}{spec.endpoint_path}"

    resolved_key: str | None
    key_env: str | None = None
    if api_key is not None:
        resolved_key = api_key or None
    else:
        resolved_key = None
        for env_name in (*spec.api_key_envs, "DECISION_API_KEY"):
            val = env.get(env_name)
            if val:
                resolved_key = val
                key_env = env_name
                break

    return DecisionConfig(
        provider=spec.name,
        protocol=spec.protocol,
        model=resolved_model,
        base_url=resolved_base,
        endpoint_url=endpoint_url,
        api_key=resolved_key,
        api_key_env=key_env,
        auth_header=spec.auth_header,
        auth_prefix=spec.auth_prefix,
    )


def decision_request(
    step: Mapping[str, Any],
    goal: str,
    candidates: list[dict[str, Any]],
    url: str,
    *,
    model: str | None = None,
    provider: str = DEFAULT_PROVIDER,
) -> dict[str, Any]:
    """Build a bounded decision request whose choices are restricted to observed refs only."""
    spec = PROVIDERS.get(provider.strip().lower())
    if spec is None:
        allowed = ", ".join(PROVIDER_ORDER)
        raise ValueError(f"unknown provider {provider!r}; expected one of: {allowed}")
    resolved_model = model or spec.default_model
    context_text = f"Page URL: {url}\nGoal: {goal}"
    prompt_text = (
        f"Action: {step['action']}\n"
        f"Target: {step.get('describe') or step['target']}\n"
        "Which observed element should this step act on?"
    )
    if spec.protocol == "openai":
        return {
            "model": resolved_model,
            "context": context_text,
            "questions": [
                {
                    "id": "element",
                    "prompt": prompt_text,
                    "choices": [{"value": e["ref"], "description": describe(e)} for e in candidates[:MAX_CHOICES]],
                    "allow_abstention": True,
                }
            ],
        }

    # System One (TypeSafe Jev, Liquid d1/d1.2) and Gemini Decisions lack a native
    # allow_abstention flag on `choice`, so reserve 1 slot of MAX_CHOICES for NONE_CHOICE.
    criteria: dict[str, str] = {e["ref"]: describe(e) for e in candidates[: MAX_CHOICES - 1]}
    criteria[NONE_CHOICE] = "None of the observed elements matches this step"
    context_key = "input" if spec.protocol == "gemini" else "state"
    return {
        "model": resolved_model,
        context_key: context_text,
        "questions": {
            "element": {
                "type": "choice",
                "instructions": f"{prompt_text} Choose {NONE_CHOICE} if none matches.",
                "criteria": criteria,
            }
        },
    }


def chosen_ref(response: Any) -> tuple[str | None, Any]:
    """Extract (ref, confidence) from OpenAI, System One, or Gemini decision payloads."""
    if not isinstance(response, dict):
        return None, "malformed_response"
    if "decisions" in response:
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

    if "answers" in response or "prompt_feedback" in response:
        answers = response.get("answers")
        if not isinstance(answers, dict) or not answers:
            feedback = response.get("prompt_feedback")
            if isinstance(feedback, dict) and feedback.get("block_reason"):
                return None, f"blocked:{feedback['block_reason']}"
            return None, "malformed_response"
        answer = answers.get("element")
        if not isinstance(answer, dict):
            return None, "malformed_response"
        if answer.get("type") == "refusal":
            return None, answer.get("reason") or answer.get("refusal") or "refusal"
        choice = answer.get("choice")
        if not isinstance(choice, str) or not choice:
            return None, "missing_choice"
        if choice == NONE_CHOICE:
            return None, "abstained"
        return choice, answer.get("confidence")

    return None, "malformed_response"


def extract_input_tokens(response: Any) -> int:
    """Extract input token count from OpenAI/System One (`input_tokens`) or Gemini (`total_input_tokens`)."""
    if not isinstance(response, dict):
        return 0
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in ("input_tokens", "total_input_tokens"):
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
    return 0


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


def evaluate_verify_value(value: Any) -> tuple[bool, Any]:
    """Evaluate an already-parsed JSON result from `eval` into (passed: bool, value) (pure)."""
    if isinstance(value, dict):
        if "passed" in value:
            return value["passed"] is True, value
        return True, value
    if isinstance(value, list):
        return True, value
    if isinstance(value, float) and math.isnan(value):
        return False, value
    return bool(value), value


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
    return evaluate_verify_value(value)


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


def should_retry_step(step: Mapping[str, Any]) -> bool:
    """Decide whether a step may retry on the same element (pure).

    Idempotent form inputs (fill/check/select) retry once by default unless once=true or retry=false.
    Click/upload/press execute at most once on the same element by default; set retry=true to opt in.
    """
    if step.get("once") is True:
        return False
    if "retry" in step:
        return bool(step["retry"])
    return step.get("action") in BATCHABLE_ACTIONS


def validate_expect_url(expect_url: str) -> str | None:
    """Validate an --expect-url precondition (must be a well-formed http(s) URL with a host)."""
    if not isinstance(expect_url, str) or not expect_url.strip():
        return "expect_url must be a non-empty http(s) URL"
    if any(ch.isspace() for ch in expect_url):
        return f"expect_url must not contain whitespace: {expect_url!r}"
    parsed = urlsplit(expect_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return f"expect_url must be an absolute http(s) URL with a host: {expect_url!r}"
    return None


def url_matches_expected(actual_url: str, expected_url: str) -> bool:
    """Origin-safe, path-segment-boundary check between a live browser URL and an expected URL prefix."""
    if not actual_url or validate_expect_url(expected_url) is not None:
        return False
    actual = urlsplit(actual_url.strip())
    expected = urlsplit(expected_url.strip())
    if actual.scheme.lower() != expected.scheme.lower():
        return False
    if actual.netloc.lower() != expected.netloc.lower():
        return False
    exp_path = expected.path
    act_path = actual.path or "/"
    if exp_path and exp_path != "/":
        if exp_path.endswith("/"):
            if not act_path.startswith(exp_path):
                return False
        else:
            if act_path != exp_path and not act_path.startswith(exp_path + "/"):
                return False
    if expected.query and actual.query != expected.query:
        return False
    return not (expected.fragment and actual.fragment != expected.fragment)


def redact_text(text: str, secrets: set[str] | frozenset[str]) -> str:
    """Scrub any resolved secret values from strings before logging or returning JSON."""
    if not text or not secrets:
        return text
    out = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


MAX_STOPPED_SNAPSHOT_CHARS = 4000


def format_stopped_snapshot(
    raw: str | None,
    secrets: set[str] | frozenset[str] = frozenset(),
    *,
    max_chars: int = MAX_STOPPED_SNAPSHOT_CHARS,
) -> str | None:
    """Redact secrets and bound the live counterexample snapshot returned on failure (pure)."""
    if not raw:
        return None
    cleaned = redact_text(raw.strip(), secrets)
    if not cleaned:
        return None
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n... (truncated)"


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
    if "settle_s" in plan and not _is_finite_nonneg_number(plan["settle_s"]):
        problems.append("settle_s must be a finite non-negative number")
    if "vars" in plan:
        raw_vars = plan["vars"]
        if not isinstance(raw_vars, dict):
            problems.append("vars must be an object mapping variable names to strings")
        else:
            for key, val in raw_vars.items():
                if not isinstance(key, str) or not ENV_VAR_NAME.match(key):
                    problems.append(f"vars: invalid variable name {key!r}")
                if not isinstance(val, str):
                    problems.append(f"vars.{key}: value must be a string")
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
            elif action == "upload":
                if has_value_env:
                    problems.append(f"step {index}: value_env is only valid for fill")
                if not isinstance(step.get("value"), str) or not step["value"].strip():
                    problems.append(f"step {index}: upload requires a non-empty string file path value")
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
        if "timeout_s" in step and not _is_finite_nonneg_number(step["timeout_s"]):
            problems.append(f"step {index}: timeout_s must be a finite non-negative number")
        if "match" in step:
            if not isinstance(step["match"], str) or step["match"] not in VALID_MATCH_MODES:
                problems.append(f"step {index}: match must be one of 'exact', 'prefix', 'regex'")
            elif step["match"] == "regex" and isinstance(step.get("target"), str):
                try:
                    re.compile(step["target"])
                except re.error as err:
                    problems.append(f"step {index}: invalid regex: {err}")
        if "nth" in step and (not _is_int(step["nth"]) or step["nth"] < 1):
            problems.append(f"step {index}: nth must be a positive integer")
        if "expect" in step and step["expect"] != "navigation":
            problems.append(f"step {index}: expect must be 'navigation'")
        for flag in ("decide", "once", "retry", "navigates"):
            if flag in step and not isinstance(step[flag], bool):
                problems.append(f"step {index}: {flag} must be a boolean")
        if step.get("retry") is True and step.get("once") is True:
            problems.append(f"step {index}: retry=true cannot be combined with once=true")
        if step.get("decide") is True and ("nth" in step or "match" in step):
            problems.append(f"step {index}: decide=true cannot be combined with nth or match")
    return problems


def validate_run_options(
    plan: Any,
    *,
    session: str | None = None,
    from_step: int = 1,
    provider: str | None = None,
    no_open: bool = False,
    reload_page: bool = False,
    keep_open: bool = False,
    close_on_success: bool = False,
    expect_url: str | None = None,
) -> list[str]:
    """Runtime option checks over an already schema-valid plan (pure)."""
    problems: list[str] = []
    if provider is not None and provider.strip():
        norm_provider = provider.strip().lower()
        if norm_provider not in PROVIDERS:
            allowed = ", ".join(PROVIDER_ORDER)
            problems.append(f"provider: unknown provider {provider!r}; expected one of: {allowed}")
    if not _is_int(from_step) or from_step < 1:
        problems.append("from_step must be a positive integer")
    elif isinstance(plan, dict) and isinstance(plan.get("steps"), list) and from_step > len(plan["steps"]):
        problems.append(f"from_step ({from_step}) exceeds step count ({len(plan['steps'])})")
    if _is_int(from_step) and from_step > 1 and not session:
        problems.append("--from-step > 1 requires --session")
    if no_open and not session:
        problems.append("--no-open requires --session")
    if reload_page and not session:
        problems.append("--reload requires --session")
    if reload_page and no_open:
        problems.append("cannot combine --reload with --no-open")
    if close_on_success and not session:
        problems.append("--close requires --session")
    if keep_open and close_on_success:
        problems.append("cannot combine --keep-open with --close")
    if (
        isinstance(plan, dict)
        and plan.get("reset") is True
        and session
        and from_step == 1
        and not no_open
        and not reload_page
    ):
        problems.append(
            "cannot combine reset=true with --session (pass --no-open or --reload to reuse an open session)"
        )
    if expect_url is not None:
        url_problem = validate_expect_url(expect_url)
        if url_problem:
            problems.append(url_problem)
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


def parse_var_args(raw_vars: list[str] | None) -> tuple[dict[str, str], list[str]]:
    """Parse repeatable CLI `--var KEY=VALUE` flags -> (mapping, problems)."""
    parsed: dict[str, str] = {}
    problems: list[str] = []
    for item in raw_vars or []:
        if "=" not in item:
            problems.append(f"--var must be KEY=VALUE, got {item!r}")
            continue
        key, value = item.split("=", 1)
        if not ENV_VAR_NAME.match(key):
            problems.append(f"--var: invalid variable name {key!r}")
            continue
        parsed[key] = value
    return parsed, problems


def _subst_vars(text: str, merged: Mapping[str, str], field: str, problems: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in merged:
            problems.append(f"{field}: undefined plan variable {{{{{name}}}}}")
            return match.group(0)
        return merged[name]

    return VAR_PLACEHOLDER.sub(repl, text)


def apply_plan_vars(
    plan: dict[str, Any],
    overrides: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Merge plan defaults with CLI overrides and expand `{{var}}` placeholders (pure)."""
    base_vars = plan.get("vars")
    merged: dict[str, str] = {}
    problems: list[str] = []
    if isinstance(base_vars, dict):
        for k, v in base_vars.items():
            if isinstance(k, str) and isinstance(v, str):
                merged[k] = v
    if overrides:
        for k, v in overrides.items():
            if not isinstance(k, str) or not ENV_VAR_NAME.match(k):
                problems.append(f"var: invalid variable name {k!r}")
            elif not isinstance(v, str):
                problems.append(f"var.{k}: value must be a string")
            else:
                merged[k] = v

    expanded = dict(plan)
    for top_field in ("url", "goal"):
        if isinstance(expanded.get(top_field), str):
            expanded[top_field] = _subst_vars(expanded[top_field], merged, top_field, problems)

    if isinstance(plan.get("steps"), list):
        new_steps: list[Any] = []
        for idx, step in enumerate(plan["steps"], 1):
            if not isinstance(step, dict):
                new_steps.append(step)
                continue
            step_copy = dict(step)
            for sfield in ("target", "value", "describe"):
                if isinstance(step_copy.get(sfield), str):
                    step_copy[sfield] = _subst_vars(step_copy[sfield], merged, f"step {idx}.{sfield}", problems)
            new_steps.append(step_copy)
        expanded["steps"] = new_steps

    return expanded, merged, problems


def wrap_verify_with_vars(expression: str, merged_vars: Mapping[str, str]) -> str:
    """Expose merged plan vars as a read-only JS `VARS` object inside verify expressions (pure)."""
    if not merged_vars:
        return expression
    encoded = json.dumps(dict(merged_vars), ensure_ascii=False)
    return f"((VARS) => ({expression}))({encoded})"


def select_tier1_batch(
    steps: list[dict[str, Any]],
    elements: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Select a leading run of >=2 same-screen pure form steps that uniquely match by Tier 1 label."""
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_refs: set[str] = set()
    for step in steps:
        action = step.get("action")
        if action not in BATCHABLE_ACTIONS:
            break
        if (
            step.get("decide") is True
            or step.get("expect") == "navigation"
            or not should_retry_step(step)
        ):
            break
        element, status = match_label(step, elements)
        if status != "hit" or element is None:
            break
        # Autocomplete combobox fills can open dynamic dropdowns; keep them on per-step stability path.
        if action == "fill" and element.get("role") == "combobox":
            break
        ref = element["ref"]
        if ref in seen_refs:
            break
        seen_refs.add(ref)
        selected.append((step, element))
    return selected if len(selected) >= 2 else []


def build_batch_commands(
    selected: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[list[list[str]], list[tuple[int, str, int]]]:
    """Build `agent-browser batch` command list: actions followed by per-step effect checks (pure)."""
    commands: list[list[str]] = [command_for(step, el["ref"]) for step, el in selected]
    check_map: list[tuple[int, str, int]] = []
    for offset, (step, el) in enumerate(selected):
        action = step["action"]
        ref_arg = f"@{el['ref']}"
        if action == "fill":
            check_map.append((offset, "fill", len(commands)))
            commands.append(["get", "value", ref_arg])
        elif action == "check":
            check_map.append((offset, "check", len(commands)))
            commands.append(["is", "checked", ref_arg])
    return commands, check_map


def verify_batch_results(
    selected: list[tuple[dict[str, Any], dict[str, Any]]],
    rows: Any,
    check_map: list[tuple[int, str, int]],
) -> int:
    """Return count of leading steps whose batched execution AND effect verification both passed (pure)."""
    if not isinstance(rows, list) or not selected:
        return 0
    n = len(selected)
    # First check action rows 0..n-1
    for i in range(n):
        if i >= len(rows) or not isinstance(rows[i], dict) or rows[i].get("success") is not True:
            return i

    # All n actions succeeded; verify every checked step in order.
    failed_at = n
    for offset, action, row_idx in check_map:
        if offset >= failed_at:
            break
        if row_idx >= len(rows) or not isinstance(rows[row_idx], dict) or rows[row_idx].get("success") is not True:
            failed_at = offset
            break
        res = rows[row_idx].get("result")
        if action == "fill":
            val = res.get("value") if isinstance(res, dict) else None
            if not isinstance(val, str) or val != selected[offset][0]["value"]:
                failed_at = offset
                break
        elif action == "check":
            checked = res.get("checked") if isinstance(res, dict) else None
            if checked is not True:
                failed_at = offset
                break
    return failed_at


def append_batch_tail(
    commands: list[list[str]],
    *,
    verify_expr: str | None = None,
    include_url: bool = False,
) -> tuple[list[list[str]], int | None, int | None]:
    """Fuse read-only tail commands (`eval` verify and/or `get url`) onto a terminal Tier-1 batch (pure)."""
    extended = list(commands)
    verify_idx: int | None = None
    url_idx: int | None = None
    if verify_expr:
        verify_idx = len(extended)
        extended.append(["eval", verify_expr])
    if include_url:
        url_idx = len(extended)
        extended.append(["get", "url"])
    return extended, verify_idx, url_idx


def extract_batch_tail(
    rows: Any,
    verify_idx: int | None,
    url_idx: int | None,
) -> tuple[bool | None, Any, str | None]:
    """Extract (verify_passed, verify_value, url) from tail-fused batch rows (pure).

    Only valid after all preceding batched steps and per-step checks have passed.
    """
    if not isinstance(rows, list):
        return None, None, None
    verify_passed: bool | None = None
    verify_value: Any = None
    url: str | None = None

    if verify_idx is not None and verify_idx < len(rows):
        row = rows[verify_idx]
        if isinstance(row, dict) and row.get("success") is True:
            res = row.get("result")
            if isinstance(res, dict) and "result" in res:
                verify_passed, verify_value = evaluate_verify_value(res["result"])
                if isinstance(res.get("origin"), str) and res["origin"]:
                    url = res["origin"]

    if url_idx is not None and url_idx < len(rows):
        row = rows[url_idx]
        if isinstance(row, dict) and row.get("success") is True:
            res = row.get("result")
            if isinstance(res, dict) and isinstance(res.get("url"), str) and res["url"]:
                url = res["url"]
            elif isinstance(res, str) and res:
                url = res

    return verify_passed, verify_value, url


def resolve_step_settle_s(
    step: Mapping[str, Any],
    *,
    plan_settle_s: float | None = None,
    cli_settle_s: float | None = None,
    default: float = 3.0,
) -> float:
    """Resolve effective element-settle timeout: step timeout_s > CLI --settle-s > plan settle_s > default."""
    if step.get("timeout_s") is not None:
        return float(step["timeout_s"])
    if cli_settle_s is not None:
        return float(cli_settle_s)
    if plan_settle_s is not None:
        return float(plan_settle_s)
    return float(default)
