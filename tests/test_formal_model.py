"""Formal verification tests grounded in Kripke/CTL model checking and compiler simulation relations.

1. Kripke model + CTL checker (EU backward BFS, EG via Tarjan non-trivial SCC, deadlock freedom)
   exhaustively verifying PlanRunner's v0.5.0 state machine invariants and termination.
2. Small-step operational semantics + Hypothesis-driven forward/backward simulation checking
   between the conservative step-by-step runner and the v0.5.0 optimized runner (Tier-1 batching,
   tail-fused verify/url, and cross-step snapshot forwarding).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from agent_browser_decisions.core import (
    append_batch_tail,
    extract_batch_tail,
    parse_snapshot,
    select_tier1_batch,
)
from agent_browser_decisions.runner import PlanRunner

# ---------------------------------------------------------------------------
# Part 1: Pure Kripke Model & CTL Model Checker (Tarjan SCC + Backward BFS)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KripkeModel:
    worlds: frozenset[str]
    transitions: dict[str, frozenset[str]]
    initial: frozenset[str]
    labels: dict[str, frozenset[str]]

    def reverse_edges(self) -> dict[str, set[str]]:
        rev: dict[str, set[str]] = {w: set() for w in self.worlds}
        for src, dsts in self.transitions.items():
            for dst in dsts:
                rev.setdefault(dst, set()).add(src)
        return rev


def _tarjan_sccs(nodes: set[str], edges: dict[str, frozenset[str]]) -> list[set[str]]:
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[set[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index_counter
        indices[v] = index_counter
        lowlink[v] = index_counter
        index_counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, frozenset()):
            if w not in nodes:
                continue
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: set[str] = set()
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.add(w)
                if w == v:
                    break
            sccs.append(comp)

    for n in sorted(nodes):
        if n not in indices:
            strongconnect(n)
    return sccs


class CTL:
    def sat(self, model: KripkeModel) -> set[str]:
        raise NotImplementedError


@dataclass(frozen=True)
class Atom(CTL):
    prop: str

    def sat(self, model: KripkeModel) -> set[str]:
        return {w for w in model.worlds if self.prop in model.labels.get(w, frozenset())}


@dataclass(frozen=True)
class Not(CTL):
    inner: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        return set(model.worlds) - self.inner.sat(model)


@dataclass(frozen=True)
class And(CTL):
    left: CTL
    right: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        return self.left.sat(model) & self.right.sat(model)


@dataclass(frozen=True)
class Or(CTL):
    left: CTL
    right: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        return self.left.sat(model) | self.right.sat(model)


def Implies(left: CTL, right: CTL) -> CTL:
    return Or(Not(left), right)


@dataclass(frozen=True)
class EX(CTL):
    inner: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        target = self.inner.sat(model)
        return {w for w in model.worlds if any(nxt in target for nxt in model.transitions.get(w, frozenset()))}


@dataclass(frozen=True)
class EU(CTL):
    left: CTL
    right: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        target_l = self.left.sat(model)
        sat = set(self.right.sat(model))
        rev = model.reverse_edges()
        queue = list(sat)
        while queue:
            cur = queue.pop(0)
            for prev in rev.get(cur, ()):
                if prev not in sat and prev in target_l:
                    sat.add(prev)
                    queue.append(prev)
        return sat


@dataclass(frozen=True)
class EG(CTL):
    inner: CTL

    def sat(self, model: KripkeModel) -> set[str]:
        target = self.inner.sat(model)
        sccs = _tarjan_sccs(target, model.transitions)
        # Non-trivial SCC: |C| > 1, or |C| == 1 with a self-loop.
        sat: set[str] = set()
        for comp in sccs:
            if len(comp) > 1:
                sat.update(comp)
            elif len(comp) == 1:
                only = next(iter(comp))
                if only in model.transitions.get(only, frozenset()):
                    sat.add(only)
        rev = model.reverse_edges()
        queue = list(sat)
        while queue:
            cur = queue.pop(0)
            for prev in rev.get(cur, ()):
                if prev not in sat and prev in target:
                    sat.add(prev)
                    queue.append(prev)
        return sat


def EF(phi: CTL) -> CTL:
    return EU(Or(phi, Not(phi)), phi)


def AG(phi: CTL) -> CTL:
    return Not(EF(Not(phi)))


def AF(phi: CTL) -> CTL:
    return Not(EG(Not(phi)))


def verify_deadlock_freedom(model: KripkeModel, terminal_prop: str = "terminal") -> set[str]:
    """Return any reachable non-terminal world with zero outgoing transitions."""
    visited: set[str] = set()
    deadlocks: set[str] = set()
    stack = list(model.initial)
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        nxts = model.transitions.get(cur, frozenset())
        if not nxts:
            if terminal_prop not in model.labels.get(cur, frozenset()):
                deadlocks.add(cur)
        else:
            for nxt in nxts:
                if nxt not in visited:
                    stack.append(nxt)
    return deadlocks


def build_runner_kripke(
    *,
    owns_session: bool,
    keep_open: bool,
    close_on_success: bool,
    max_retries: int = 2,
    inject_unbounded_retry: bool = False,
    inject_close_on_tier3: bool = False,
    inject_missing_counterexample: bool = False,
) -> KripkeModel:
    """Build the finite Kripke model of PlanRunner v0.5.0 execution & lifecycle transitions.

    Terminal states carry a self-loop (standard Kripke totality) while non-terminal states
    capture batching, partial-batch fallback, single-step retry bounds, verify, interrupt, and close.
    """
    should_close_ok = (owns_session and not keep_open) or close_on_success
    should_close_interrupt = owns_session or close_on_success

    worlds: set[str] = set()
    transitions: dict[str, set[str]] = {}
    labels: dict[str, set[str]] = {}

    def add(name: str, props: set[str]) -> str:
        worlds.add(name)
        transitions.setdefault(name, set())
        labels.setdefault(name, set()).update(props)
        return name

    def edge(u: str, v: str) -> None:
        transitions.setdefault(u, set()).add(v)

    # Terminal states (self-looping for total Kripke relation)
    s_done_closed = add("DONE_OK_CLOSED", {"terminal", "done_ok", "closed"})
    s_done_open = add("DONE_OK_OPEN", {"terminal", "done_ok", "session_open"})
    t3_props = {"terminal", "tier3_handoff", "session_open"}
    if not inject_missing_counterexample:
        t3_props.add("has_counterexample")
    s_t3_open = add("TIER3_OPEN", t3_props)
    s_t3_closed = add("TIER3_CLOSED", {"terminal", "tier3_handoff", "closed"})
    s_int_closed = add("INT_CLOSED", {"terminal", "interrupted", "closed"})
    s_int_open = add("INT_OPEN", {"terminal", "interrupted", "session_open"})
    for term in (s_done_closed, s_done_open, s_t3_open, s_t3_closed, s_int_closed, s_int_open):
        edge(term, term)

    done_target = s_done_closed if should_close_ok else s_done_open
    t3_target = s_t3_closed if inject_close_on_tier3 else s_t3_open
    int_target = s_int_closed if should_close_interrupt else s_int_open

    s_init = add("INIT", {"init"})
    s_open = add("OPEN", {"session_open"})
    s_batch = add("BATCH_TRY", {"session_open"})
    s_batch_tail_ok = add("BATCH_TAIL_VERIFY_OK", {"session_open", "fused_verify_used"})
    s_batch_partial = add("BATCH_PARTIAL_FALLBACK", {"session_open", "batch_partial_fail", "resume_exact_index"})
    s_verify_poll = add("VERIFY_POLL", {"session_open"})

    edge(s_init, s_open)
    edge(s_open, s_batch)
    edge(s_open, int_target)

    # Batch outcomes: (1) all verified + fused verify true -> done,
    # (2) all verified + fused verify falsy -> verify poll,
    # (3) partial failure -> resume exact failing step without using fused verify,
    # (4) batch skipped -> step loop at (repeats=0, excluded=0).
    edge(s_batch, s_batch_tail_ok)
    edge(s_batch, s_verify_poll)
    edge(s_batch, s_batch_partial)
    edge(s_batch, int_target)
    edge(s_batch_tail_ok, done_target)

    # Single-step retry lattice: (repeats in {0, 1}, excluded in {0..max_retries})
    step_nodes: dict[tuple[int, int], str] = {}
    for rep in (0, 1):
        for exc in range(max_retries + 1):
            node = add(f"STEP_R{rep}_E{exc}", {"session_open", "in_step"})
            step_nodes[(rep, exc)] = node

    edge(s_batch, step_nodes[(0, 0)])
    edge(s_batch_partial, step_nodes[(0, 0)])

    for (rep, exc), node in step_nodes.items():
        # Success -> verify poll or done directly
        edge(node, s_verify_poll)
        edge(node, done_target)
        # Immediate Tier-3 handoff (missing candidates, abstention, act error, once=True failure)
        edge(node, t3_target)
        # Interrupt
        edge(node, int_target)
        # Bounded repeat on no_effect / tier1 wrong_target
        if rep < 1:
            edge(node, step_nodes[(rep + 1, exc)])
        elif inject_unbounded_retry:
            edge(node, node)  # Bug: unbounded retry loop!
        # Bounded exclusion on tier2 wrong_target
        if exc < max_retries:
            edge(node, step_nodes[(rep, exc + 1)])

    # Verify poll either passes or times out to Tier-3 handoff (never loops infinitely due to deadline)
    edge(s_verify_poll, done_target)
    edge(s_verify_poll, t3_target)
    edge(s_verify_poll, int_target)

    # Prune unreachable worlds so AG/AF checks focus cleanly on reachable state space
    reachable: set[str] = set()
    stack = [s_init]
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        for nxt in transitions.get(cur, ()):
            if nxt not in reachable:
                stack.append(nxt)

    return KripkeModel(
        worlds=frozenset(reachable),
        transitions={w: frozenset(transitions[w] & reachable) for w in reachable},
        initial=frozenset({s_init}),
        labels={w: frozenset(labels[w]) for w in reachable},
    )


def test_ctl_model_check_plan_runner_v05_all_lifecycle_modes() -> None:
    """Exhaustively model-check all valid (owns_session, keep_open, close_on_success) combinations."""
    for owns_session in (False, True):
        for keep_open in (False, True):
            for close_on_success in (False, True):
                if keep_open and close_on_success:
                    continue  # Rejected statically by validate_run_options
                if not owns_session and keep_open:
                    continue  # keep_open is equivalent to default caller-owned session
                model = build_runner_kripke(
                    owns_session=owns_session,
                    keep_open=keep_open,
                    close_on_success=close_on_success,
                )
                should_close_ok = (owns_session and not keep_open) or close_on_success
                should_close_int = owns_session or close_on_success

                # 1. Zero deadlocks in reachable non-terminal states
                assert verify_deadlock_freedom(model) == set()

                # 2. Safety: Tier-3 handoff ALWAYS leaves browser session open with a counterexample snapshot
                assert model.initial <= AG(
                    Implies(Atom("tier3_handoff"), And(Atom("session_open"), Atom("has_counterexample")))
                ).sat(model)

                # 3. Safety: Normal success respects exact close/warm-session contract
                if should_close_ok:
                    assert model.initial <= AG(Implies(Atom("done_ok"), Atom("closed"))).sat(model)
                else:
                    assert model.initial <= AG(Implies(Atom("done_ok"), Atom("session_open"))).sat(model)

                # 4. Safety: Signal interrupt never leaks owned session
                if should_close_int:
                    assert model.initial <= AG(Implies(Atom("interrupted"), Atom("closed"))).sat(model)

                # 5. Safety (Backward simulation): partial batch failure NEVER uses fused verify
                assert model.initial <= AG(
                    Implies(
                        Atom("batch_partial_fail"),
                        And(Not(Atom("fused_verify_used")), Atom("resume_exact_index")),
                    )
                ).sat(model)

                # 6. Liveness & Termination: AF(terminal) holds, and EG(!terminal) has NO non-trivial SCCs
                assert model.initial <= AF(Atom("terminal")).sat(model)
                assert EG(Not(Atom("terminal"))).sat(model) == set()


def test_ctl_model_checker_detects_injected_bugs() -> None:
    """Mutation test: verify our CTL model checker actually rejects injected safety & termination bugs."""
    buggy_loop = build_runner_kripke(
        owns_session=True,
        keep_open=False,
        close_on_success=False,
        inject_unbounded_retry=True,
    )
    assert not (buggy_loop.initial <= AF(Atom("terminal")).sat(buggy_loop))
    assert EG(Not(Atom("terminal"))).sat(buggy_loop) != set()

    buggy_close = build_runner_kripke(
        owns_session=True,
        keep_open=False,
        close_on_success=False,
        inject_close_on_tier3=True,
    )
    assert not (buggy_close.initial <= AG(Implies(Atom("tier3_handoff"), Atom("session_open"))).sat(buggy_close))

    buggy_cegar = build_runner_kripke(
        owns_session=True,
        keep_open=False,
        close_on_success=False,
        inject_missing_counterexample=True,
    )
    assert not (buggy_cegar.initial <= AG(Implies(Atom("tier3_handoff"), Atom("has_counterexample"))).sat(buggy_cegar))


# ---------------------------------------------------------------------------
# Part 2: Operational Semantics + Forward & Backward Simulation Checking
# ---------------------------------------------------------------------------


class ConservativeSimulatedBrowser:
    """Deterministic stateful browser implementing small-step DOM semantics for simulation checks."""

    def __init__(
        self,
        *,
        fields: list[tuple[str, str, str]],  # (ref, role, name)
        drop_value_refs: set[str] | None = None,
        uncheckable_refs: set[str] | None = None,
        batch_error_at_cmd: int | None = None,
        verify_ready_after_evals: int = 1,
        transient_frames_on_click: int = 0,
        enable_batch: bool = True,
    ) -> None:
        self.fields = list(fields)
        self.drop_value_refs = set(drop_value_refs or ())
        self.uncheckable_refs = set(uncheckable_refs or ())
        self.batch_error_at_cmd = batch_error_at_cmd
        self.verify_ready_after_evals = verify_ready_after_evals
        self.transient_frames_on_click = transient_frames_on_click
        self.values: dict[str, str] = {}
        self.checked: dict[str, bool] = {}
        self.clicks: list[str] = []
        self.fill_calls: list[tuple[str, str]] = []
        self.check_calls: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.url = "https://sim.example/form"
        self.closed = False
        self.eval_calls = 0
        self.observe_calls = 0
        self.call_count = 0
        self._transient_left = 0
        self._epoch = 0

    def _snapshot_text(self) -> str:
        lines = [f'- {role} "{name}" [ref={ref}]' for ref, role, name in self.fields]
        if self._transient_left > 0:
            lines.append(f'- status "loading-{self._transient_left}" [ref=e999]')
            self._transient_left -= 1
        if self._epoch > 0:
            lines.append(f'- status "epoch-{self._epoch}" [ref=e998]')
        return "\n".join(lines)

    def open(self, url: str, *, profile: str | None = None) -> None:
        self.url = url
        self.call_count += 1

    def observe(self, category: str = "observe") -> dict[str, Any]:
        self.observe_calls += 1
        self.call_count += 1
        raw = self._snapshot_text()
        return {"url": self.url, "raw": raw, "elements": parse_snapshot(raw)}

    def current_url(self) -> str:
        self.call_count += 1
        return self.url

    def close(self) -> None:
        self.closed = True
        self.call_count += 1

    def _exec_one(self, cmd: list[str]) -> tuple[bool, dict[str, Any]]:
        op = cmd[0]
        if op == "fill":
            ref, val = cmd[1].lstrip("@"), cmd[2]
            self.fill_calls.append((ref, val))
            if ref not in self.drop_value_refs:
                self.values[ref] = val
            return True, {}
        if op == "check":
            ref = cmd[1].lstrip("@")
            self.check_calls.append(ref)
            if ref not in self.uncheckable_refs:
                self.checked[ref] = True
            return True, {}
        if op == "select":
            ref, val = cmd[1].lstrip("@"), cmd[2]
            self.values[ref] = val
            return True, {}
        if op == "click":
            ref = cmd[1].lstrip("@")
            self.clicks.append(ref)
            self._epoch += 1
            self._transient_left = self.transient_frames_on_click
            return True, {}
        if op == "upload":
            ref, path = cmd[1].lstrip("@"), cmd[2]
            self.uploads.append((ref, path))
            self.values[ref] = path.rsplit("/", 1)[-1]
            self._epoch += 1
            return True, {}
        if op == "get" and cmd[1] == "value":
            ref = cmd[2].lstrip("@")
            return True, {"value": self.values.get(ref, "")}
        if op == "is" and cmd[1] == "checked":
            ref = cmd[2].lstrip("@")
            return True, {"checked": bool(self.checked.get(ref, False))}
        if op == "get" and cmd[1] == "url":
            return True, {"url": self.url}
        if op == "eval":
            self.eval_calls += 1
            ready = self.eval_calls >= self.verify_ready_after_evals
            return True, {"origin": self.url, "result": ready}
        return False, {"error": f"unsupported {cmd}"}

    def call(self, *args: str, check: bool = True, category: str = "browser") -> str:
        self.call_count += 1
        ok, res = self._exec_one(list(args))
        if not ok and check:
            raise RuntimeError(str(res))
        if args[0] == "get" and args[1] == "value":
            return str(res.get("value", ""))
        if args[0] == "is" and args[1] == "checked":
            return "true" if res.get("checked") else "false"
        if args[0] == "eval":
            return "true" if res.get("result") else "false"
        if args[0] == "get" and args[1] == "url":
            return str(res.get("url", ""))
        return ""


class SimulatedBrowser(ConservativeSimulatedBrowser):
    """Same stateful browser semantics, plus `agent-browser batch --bail` capability."""

    def batch(self, commands: list[list[str]], *, bail: bool = True, category: str = "act") -> list[dict[str, Any]]:
        self.call_count += 1
        rows: list[dict[str, Any]] = []
        for idx, cmd in enumerate(commands):
            if self.batch_error_at_cmd is not None and idx == self.batch_error_at_cmd:
                rows.append({"index": idx, "success": False, "error": "injected batch failure", "result": None})
                if bail:
                    break
                continue
            ok, res = self._exec_one(cmd)
            rows.append({"index": idx, "success": ok, "error": None if ok else "err", "result": res})
            if not ok and bail:
                break
        return rows


class DummyDecisions:
    provider = "openai"
    model = "gpt-6-luna"

    def __enter__(self) -> DummyDecisions:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"decisions": [{"status": "abstained", "abstention_reason": "no_tier2"}]}


def _make_runner(browser: ConservativeSimulatedBrowser, *, disable_batch: bool = False) -> PlanRunner:
    if disable_batch:
        browser.__class__ = ConservativeSimulatedBrowser
    return PlanRunner(
        browser_factory=lambda *a, **kw: browser,
        decisions_factory=lambda *a, **kw: DummyDecisions(),
        settle_s=0.05,
        poll_s=0.001,
        verify_poll_s=0.001,
        effect_wait_s=0.02,
    )


@given(
    n_fills=st.integers(min_value=2, max_value=5),
    add_check=st.booleans(),
    add_upload=st.booleans(),
    add_trailing_click=st.booleans(),
    drop_idx=st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
    fail_check=st.booleans(),
    verify_delay=st.integers(min_value=1, max_value=2),
    transient_frames=st.integers(min_value=0, max_value=2),
)
@settings(max_examples=40, deadline=None)
def test_forward_and_backward_simulation_conservative_vs_optimized(
    n_fills: int,
    add_check: bool,
    add_upload: bool,
    add_trailing_click: bool,
    drop_idx: int | None,
    fail_check: bool,
    verify_delay: int,
    transient_frames: int,
) -> None:
    """Compiler simulation property: optimized v0.5.0 runner matches conservative semantics on every outcome."""
    fields: list[tuple[str, str, str]] = [(f"e{i + 1}", "textbox", f"Field {i + 1}") for i in range(n_fills)]
    steps: list[dict[str, Any]] = [
        {"action": "fill", "target": f"Field {i + 1}", "value": f"val-{i + 1}"} for i in range(n_fills)
    ]
    if add_check:
        fields.append(("e50", "checkbox", "Agree"))
        steps.append({"action": "check", "target": "Agree", "role": "checkbox"})
    if add_upload:
        fields.append(("e55", "button", "Resume file"))
        steps.append({"action": "upload", "target": "Resume file", "value": "/tmp/cv.pdf"})
    if add_trailing_click:
        fields.append(("e60", "button", "Apply"))
        steps.append({"action": "click", "target": "Apply"})

    plan = {
        "url": "https://sim.example/form",
        "steps": steps,
        "verify": "true",
        "verify_timeout_s": 0.05,
    }
    drop_refs = {f"e{drop_idx + 1}"} if (drop_idx is not None and drop_idx < n_fills) else set()
    uncheckable = {"e50"} if (add_check and fail_check) else set()

    src_browser = SimulatedBrowser(
        fields=fields,
        drop_value_refs=drop_refs,
        uncheckable_refs=uncheckable,
        verify_ready_after_evals=verify_delay,
        transient_frames_on_click=transient_frames,
    )
    tgt_browser = SimulatedBrowser(
        fields=fields,
        drop_value_refs=drop_refs,
        uncheckable_refs=uncheckable,
        verify_ready_after_evals=verify_delay,
        transient_frames_on_click=transient_frames,
    )

    src_res = _make_runner(src_browser, disable_batch=True).run_plan_file(plan)
    tgt_res = _make_runner(tgt_browser, disable_batch=False).run_plan_file(plan)

    # Observable equivalence (both forward and backward simulation):
    assert tgt_res["ok"] == src_res["ok"]
    assert tgt_res["verified"] == src_res["verified"]
    assert tgt_res["stopped_at_step"] == src_res["stopped_at_step"]
    assert tgt_res["session_open"] == src_res["session_open"]
    # Critically: click side effects MUST match — optimized batch must NEVER click when conservative halted!
    assert tgt_browser.clicks == src_browser.clicks
    assert tgt_browser.uploads == src_browser.uploads
    if src_res["ok"]:
        assert "stopped_snapshot" not in tgt_res
        assert tgt_browser.values == src_browser.values
        assert tgt_browser.checked == src_browser.checked
        # Efficiency: immediate verify with no dynamic/upload step uses strictly fewer calls.
        if not add_trailing_click and not add_upload and verify_delay == 1:
            assert tgt_browser.call_count < src_browser.call_count
    elif tgt_res["session_open"]:
        assert isinstance(tgt_res.get("stopped_snapshot"), str) and tgt_res["stopped_snapshot"]


def test_negative_proof_fusing_click_after_fill_violates_backward_simulation() -> None:
    """Counterexample proving why v0.5.0 refuses to fuse a trailing click into a fill batch.

    Because `agent-browser batch --bail` only bails on command errors (not on `get value`
    mismatch), naively fusing `click Submit` into a batch with `fill Name` would execute
    `Submit` even when `fill Name` silently dropped its input — violating backward simulation.
    """
    fields = [("e1", "textbox", "Name"), ("e2", "textbox", "Email"), ("e3", "button", "Submit")]
    steps = [
        {"action": "fill", "target": "Name", "value": "Taro"},
        {"action": "fill", "target": "Email", "value": "taro@example.com"},
        {"action": "click", "target": "Submit"},
    ]
    elements = parse_snapshot("\n".join(f'- {r} "{n}" [ref={ref}]' for ref, r, n in fields))

    # 1. Naive unsafe compiler that fuses trailing click into batch:
    unsafe_browser = SimulatedBrowser(fields=fields, drop_value_refs={"e1"})
    unsafe_cmds = [
        ["fill", "@e1", "Taro"],
        ["fill", "@e2", "taro@example.com"],
        ["get", "value", "@e1"],
        ["click", "@e3"],
    ]
    unsafe_browser.batch(unsafe_cmds, bail=True)
    assert unsafe_browser.clicks == ["e3"]  # VIOLATION: clicked Submit with empty Name!

    # 2. v0.5.0 sound compiler stops batch before click:
    selected = select_tier1_batch(steps, elements)
    assert [s["action"] for s, _ in selected] == ["fill", "fill"]  # click excluded!
    safe_browser = SimulatedBrowser(fields=fields, drop_value_refs={"e1"})
    res = _make_runner(safe_browser, disable_batch=False).run_plan_file(
        {"url": "https://sim.example/form", "steps": steps}
    )
    assert res["ok"] is False
    assert res["stopped_at_step"] == 1
    assert safe_browser.clicks == []  # Sound: Submit was NEVER clicked!


def test_tail_fusion_and_snapshot_forwarding_reduce_browser_calls() -> None:
    """Verify v0.5.0 optimizations measurably cut subprocess/observe calls while keeping semantics intact."""
    # 1. Pure form tail fusion: verify + url folded into batch -> 0 separate eval/url calls
    fields = [("e1", "textbox", "Name"), ("e2", "textbox", "Email")]
    plan = {
        "url": "https://sim.example/form",
        "steps": [
            {"action": "fill", "target": "Name", "value": "Taro"},
            {"action": "fill", "target": "Email", "value": "taro@example.com"},
        ],
        "verify": "true",
    }
    b = SimulatedBrowser(fields=fields, verify_ready_after_evals=1)
    res = _make_runner(b, disable_batch=False).run_plan_file(plan)
    assert res["ok"] is True
    assert res["verified"] is True
    assert res["url"] == "https://sim.example/form"
    # open (1) + wait_stable (2 observes) + batch with fused verify+url (1) = 4 total calls!
    assert b.call_count == 4

    # 2. Snapshot forwarding across multi-step clicks: each settled click saves 1 observe call!
    click_fields = [("e1", "button", "Step 1"), ("e2", "button", "Step 2"), ("e3", "button", "Step 3")]
    click_plan = {
        "url": "https://sim.example/wizard",
        "steps": [
            {"action": "click", "target": "Step 1"},
            {"action": "click", "target": "Step 2"},
            {"action": "click", "target": "Step 3"},
        ],
    }
    cb = SimulatedBrowser(fields=click_fields, transient_frames_on_click=0)
    cres = _make_runner(cb, disable_batch=False).run_plan_file(click_plan)
    assert cres["ok"] is True
    # Step 1: 2 observes (initial settle) + 1 check observe (changed) = 3
    # Step 2: 1 observe (matches carried snapshot!) + 1 check observe = 2 (saved 1 observe + 1 sleep!)
    # Step 3: 1 observe (matches carried snapshot!) + 1 check observe = 2 (saved 1 observe + 1 sleep!)
    assert cb.observe_calls == 7


def test_append_and_extract_batch_tail_unit() -> None:
    """Direct unit check for pure tail-fusion helpers."""
    cmds, v_idx, u_idx = append_batch_tail([["fill", "@e1", "x"]], verify_expr="true", include_url=True)
    assert v_idx == 1 and u_idx == 2
    assert cmds[1] == ["eval", "true"]
    assert cmds[2] == ["get", "url"]
    rows = [
        {"success": True, "result": {}},
        {"success": True, "result": {"origin": "https://a.test", "result": {"passed": True, "n": 7}}},
        {"success": True, "result": {"url": "https://a.test/done"}},
    ]
    passed, val, url = extract_batch_tail(rows, v_idx, u_idx)
    assert passed is True
    assert val == {"passed": True, "n": 7}
    assert url == "https://a.test/done"


def test_cegar_counterexample_snapshot_and_resume() -> None:
    """CEGAR loop invariant: Tier-3 stop exposes redacted counterexample snapshot; patching + --from-step completes."""
    fields = [
        ("e1", "textbox", "Customer name"),
        ("e2", "textbox", "API token"),
        ("e3", "radio", "Large"),
        ("e4", "button", "Resume file"),
    ]
    browser = SimulatedBrowser(fields=fields)
    runner = _make_runner(browser, disable_batch=False)

    # Initial rough plan has wrong target "Extra Large" at step 3 (simulating unseen/changed UI)
    rough_plan = {
        "url": "https://sim.example/form",
        "steps": [
            {"action": "fill", "target": "Customer name", "value": "Taro"},
            {"action": "fill", "target": "API token", "value_env": "TEST_SECRET_TOK"},
            {"action": "check", "target": "Extra Large", "role": "radio"},
            {"action": "upload", "target": "Resume file", "value": "/tmp/cv.pdf"},
        ],
        "verify": "true",
    }
    env = {"TEST_SECRET_TOK": "tok-super-secret-999"}
    res1 = runner.run_plan_file(rough_plan, session="cegar-s1", environ=env)
    assert res1["ok"] is False
    assert res1["session_open"] is True
    assert res1["stopped_at_step"] == 3
    # Counterexample snapshot is immediately returned, shows real element "Large", and redacts the secret
    snap = res1.get("stopped_snapshot", "")
    assert 'radio "Large"' in snap
    assert "tok-super-secret-999" not in snap

    # Refine step 3 from counterexample snapshot and resume in-place from step 3
    refined_plan = {
        **rough_plan,
        "steps": [
            *rough_plan["steps"][:2],
            {"action": "check", "target": "Large", "role": "radio"},
            rough_plan["steps"][3],
        ],
    }
    res2 = runner.run_plan_file(refined_plan, session="cegar-s1", from_step=3, close_on_success=True, environ=env)
    assert res2["ok"] is True
    assert res2["verified"] is True
    assert res2["session_open"] is False
    assert "stopped_snapshot" not in res2
    assert browser.uploads == [("e4", "/tmp/cv.pdf")]


def test_once_and_no_retry_form_steps_never_double_execute_via_batch_fallback() -> None:
    """Steps with once=True or retry=False must never enter batch fusion and execute at most once."""
    fields = [
        ("e1", "textbox", "Name"),
        ("e2", "textbox", "Email"),
        ("e3", "checkbox", "AutoSave"),
    ]
    for flag in ({"once": True}, {"retry": False}):
        # Case A: non-retryable fill at step 1 fails effect check -> strictly 1 fill call, no batch+fallback double run
        b1 = SimulatedBrowser(fields=fields, drop_value_refs={"e1"})
        res1 = _make_runner(b1, disable_batch=False).run_plan_file(
            {
                "url": "https://sim.example/form",
                "steps": [
                    {"action": "fill", "target": "Name", "value": "Taro", **flag},
                    {"action": "fill", "target": "Email", "value": "taro@example.com"},
                ],
            }
        )
        assert res1["ok"] is False
        assert res1["stopped_at_step"] == 1
        assert b1.fill_calls == [("e1", "Taro")]

        # Case B: normal leading fills batch safely, then trailing non-retryable check executes strictly once
        b2 = SimulatedBrowser(fields=fields, uncheckable_refs={"e3"})
        res2 = _make_runner(b2, disable_batch=False).run_plan_file(
            {
                "url": "https://sim.example/form",
                "steps": [
                    {"action": "fill", "target": "Name", "value": "Taro"},
                    {"action": "fill", "target": "Email", "value": "taro@example.com"},
                    {"action": "check", "target": "AutoSave", **flag},
                ],
            }
        )
        assert res2["ok"] is False
        assert res2["stopped_at_step"] == 3
        assert b2.fill_calls == [("e1", "Taro"), ("e2", "taro@example.com")]
        assert b2.check_calls == ["e3"]
