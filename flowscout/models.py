"""Core data model for a FlowScout crawl run.

Deliberately plain dataclasses (no ORM) -- the run's full result is
serialized to a single flows.json at the end, which is the source of
truth consumed by report.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Risk(str, Enum):
    SAFE = "safe"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


class FlowStatus(str, Enum):
    UNIQUE = "unique"
    DUPLICATE = "duplicate"
    BLOCKED = "blocked"


@dataclass
class ElementCandidate:
    """A clickable candidate discovered on a state (link/button/submit)."""

    signature: str          # stable id used for dedup/visited-tracking, e.g. data-test attr or role+name
    norm_signature: str     # signature with data-specific tokens generalized (structural dedup key)
    label: str              # human readable text used in reports
    selector: str            # playwright selector to (re-)locate this element
    risk: Risk
    risk_reason: str = ""
    # "markup" (matched the a/button/[role=button] selector) or "handler"
    # (a div-as-button style element promoted after CDP confirmed a real
    # click listener on it -- see actions.py's coverage-delta rewrite,
    # Aug 2026). Purely informational, but worth knowing which candidates
    # came from the newer, less-tested detection path.
    discovered_via: str = "markup"
    # True for an action that picks one of several mutually-exclusive
    # alternatives (a <select> option, a wizard "choice card") --
    # deliberately independent of `risk`: picking a sort order or a
    # workout type has no state-changing consequence worth gating behind
    # allow_mutating, but which alternative was picked is exactly the
    # kind of thing that should keep two flows distinct instead of
    # collapsing into one (see identity.py's mutating_signature_set,
    # which this feeds alongside risk == MUTATING).
    is_choice: bool = False


@dataclass
class StateNode:
    fingerprint: str
    url_pattern: str
    raw_url: str
    title: str
    candidates: list[ElementCandidate] = field(default_factory=list)
    discovered_by_flow: Optional[int] = None
    # Elements CDP couldn't verify one way or the other (its own session
    # failed, or -- extremely rare on Chromium -- getEventListeners was
    # unavailable): a fallback list built from the old cursor/role/
    # tabindex guess, kept only so a CDP failure degrades to previously-
    # shipped behavior instead of silently promoting nothing. Expected to
    # be empty on a normal Chromium run. Each dict has tag/className/text/count.
    unclassified_interactive: list[dict] = field(default_factory=list)
    # Elements CDP confirmed have a real click listener but that are
    # currently disabled (aria-disabled, a disabled-looking class, or
    # pointer-events:none) -- a real control, correctly never clicked,
    # but worth surfacing (e.g. Site B's "Option C" wizard card was
    # disabled pending an unrelated selection). Each dict has
    # tag/className/text/count.
    disabled_interactive: list[dict] = field(default_factory=list)


@dataclass
class Transition:
    from_fp: str
    to_fp: Optional[str]     # None if the action was skipped (risk/limit) or errored
    action_label: str
    action_norm_signature: str
    risk: Risk
    risk_reason: str = ""
    outcome: str = "ok"       # ok | revisit | skipped | error
    detail: str = ""
    replay_meta: str = ""      # JSON descriptor of the element, used to relocate it on replay
    # Field NAMES only (e.g. "user-name", "password") when this step filled
    # and submitted a form -- never values. action_label's own fill summary
    # masks password values for display and doesn't even store real ones for
    # synthetic fields, so it can't be parsed back into a working .fill()
    # call; this is what M4 codegen uses instead, pairing each name with an
    # env-var convention rather than ever inlining a value into generated
    # code (see ROADMAP.md M4).
    form_fields: list[str] = field(default_factory=list)
    # Copied from the ElementCandidate this transition was built from --
    # see ElementCandidate.is_choice.
    is_choice: bool = False


@dataclass
class Flow:
    id: int
    status: FlowStatus
    duplicate_of: Optional[int]
    dedup_reason: str
    transitions: list[Transition] = field(default_factory=list)
    end_state_fp: str = ""
    # Which configured persona (logged-in user) walked this flow. A
    # single-credentials config produces one persona named "default", so
    # this is "default" for every pre-multi-persona run and config.
    # Part of every dedup and identity key (see identity.py) -- two
    # personas reaching the same page having done the same things are
    # NOT the same flow, since what they were *allowed* to do getting
    # there is the thing being tested.
    persona: str = "default"
    # True only for BLOCKED flows cut short by a budget anchored to THIS
    # flow's own path (max_depth, or a dead end from risk-policy/repeat-
    # cap withholding) -- see crawler.py's emit_flow(). Deliberately
    # False for max_states/max_flows truncation: those are whole-run
    # budgets, not tied to any one flow, so "resume just this flow with
    # a bigger number" wouldn't address what actually blocked it -- the
    # honest fix there is a full re-crawl with a higher limit, not a
    # targeted continuation. Also False for a genuine error ("Terminated:
    # action ... raised an error") -- the action itself failed, more
    # budget doesn't fix that.
    resumable: bool = False

    def action_sequence(self) -> list[str]:
        return [t.action_norm_signature for t in self.transitions]


@dataclass
class Checkpoint:
    kind: str   # blocked | error | ambiguous
    flow_id: Optional[int]
    state_fp: Optional[str]
    message: str
    detail: str = ""


@dataclass
class RunResult:
    project: str
    start_url: str
    config: dict
    states: dict[str, StateNode] = field(default_factory=dict)
    flows: list[Flow] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    skipped_candidates: list[dict] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    semantic_dedup_status: str = "not run"

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "start_url": self.start_url,
            "config": self.config,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "semantic_dedup_status": self.semantic_dedup_status,
            "states": {fp: asdict(s) for fp, s in self.states.items()},
            "flows": [asdict(f) for f in self.flows],
            "checkpoints": [asdict(c) for c in self.checkpoints],
            "skipped_candidates": self.skipped_candidates,
            "summary": self.summary(),
        }

    @classmethod
    def from_json(cls, d: dict) -> "RunResult":
        """Reconstruct a RunResult from a previously-saved flows.json --
        lets the gap-analysis pass (and report re-renders) run against a
        completed crawl without re-crawling."""
        run = cls(project=d["project"], start_url=d["start_url"], config=d["config"])
        run.started_at = d.get("started_at", "")
        run.finished_at = d.get("finished_at", "")
        run.semantic_dedup_status = d.get("semantic_dedup_status", "not run")
        run.skipped_candidates = d.get("skipped_candidates", [])
        for fp, s in d.get("states", {}).items():
            # `candidates=[]` here used to be permanent, not a placeholder --
            # every candidate a crawl actually found silently vanished from
            # the States table's outdegree/risk columns on any report
            # regenerated from a saved flows.json (`flowscout gap`,
            # `flowscout confirm`, the web UI's gap re-run) instead of a
            # fresh crawl. Found while extending this exact serialization
            # path, verified against a real regenerated report (outdegree
            # showed 0 for a state with known real candidates) before fixing.
            candidates = [ElementCandidate(**{**c, "risk": Risk(c["risk"])}) for c in s.get("candidates", [])]
            run.states[fp] = StateNode(
                fingerprint=s["fingerprint"], url_pattern=s["url_pattern"], raw_url=s.get("raw_url", ""),
                title=s.get("title", ""), candidates=candidates, discovered_by_flow=s.get("discovered_by_flow"),
                unclassified_interactive=s.get("unclassified_interactive", []),
                disabled_interactive=s.get("disabled_interactive", []),
            )
        for f in d.get("flows", []):
            transitions = [
                Transition(**{**t, "risk": Risk(t["risk"])}) for t in f["transitions"]
            ]
            run.flows.append(Flow(
                id=f["id"], status=FlowStatus(f["status"]), duplicate_of=f.get("duplicate_of"),
                dedup_reason=f.get("dedup_reason", ""), transitions=transitions,
                end_state_fp=f.get("end_state_fp", ""), persona=f.get("persona", "default"),
                resumable=f.get("resumable", False),
            ))
        for c in d.get("checkpoints", []):
            run.checkpoints.append(Checkpoint(**c))
        return run

    def summary(self) -> dict:
        unique = sum(1 for f in self.flows if f.status == FlowStatus.UNIQUE)
        dup = sum(1 for f in self.flows if f.status == FlowStatus.DUPLICATE)
        blocked = sum(1 for f in self.flows if f.status == FlowStatus.BLOCKED)
        semantic_merges = sum(1 for f in self.flows if f.dedup_reason.startswith("Semantic dedup"))
        convergence_merges = sum(1 for f in self.flows if f.dedup_reason.startswith("State convergence"))
        states_with_gaps = sum(1 for s in self.states.values() if s.unclassified_interactive)
        unclassified_total = sum(u["count"] for s in self.states.values() for u in s.unclassified_interactive)
        handler_discovered_total = sum(
            1 for s in self.states.values() for c in s.candidates if c.discovered_via == "handler")
        disabled_total = sum(u["count"] for s in self.states.values() for u in s.disabled_interactive)
        return {
            "states_discovered": len(self.states),
            "flows_total": len(self.flows),
            "flows_unique": unique,
            "flows_duplicate": dup,
            "flows_blocked": blocked,
            "checkpoints": len(self.checkpoints),
            "skipped_candidates": len(self.skipped_candidates),
            "semantic_merges": semantic_merges,
            "convergence_merges": convergence_merges,
            "states_with_coverage_gaps": states_with_gaps,
            "unclassified_interactive_total": unclassified_total,
            # Both new in the Aug 2026 CDP-based control-detection rewrite --
            # see actions.py's _verify_pool.
            "handler_discovered_total": handler_discovered_total,
            "disabled_interactive_total": disabled_total,
        }


@dataclass
class FlowCoverage:
    """One discovered (unique) flow's coverage verdict against the TCMS.

    Matching is action-level, not flow-level (see gap_analysis.py):
    each of the flow's mutating actions gets its own independent verdict
    against the TCMS, and this flow's overall status is derived from
    them -- "covered" only when every action matched something,
    "partial" when some did and some didn't (see action_matches for
    exactly which), "gap" when none did, "navigation" for flows with no
    mutating actions at all (pure browsing -- not compared, same
    shared-step/precondition signal M4's codegen already uses)."""
    flow_id: int
    status: str  # "covered" | "partial" | "gap" | "navigation"
    # Quick-glance single match: the highest-scoring entry in
    # action_matches below. For the common single-action flow this IS
    # the whole story; for a multi-action flow, check action_matches for
    # the per-action breakdown rather than trusting this alone.
    matched_tcms_id: Optional[str]
    matched_tcms_title: Optional[str]
    score: float
    # identity.flow_identity(flow, states) -- the cross-run-stable handle
    # an operator passes to `flowscout confirm` to make this pairing
    # permanent. Empty when not computed (e.g. no project_state supplied).
    identity: str = ""
    # True when this verdict came from a project_state-recorded operator
    # confirmation, not a fresh embedding guess -- score is then 1.0 by
    # convention, not a real cosine similarity. A confirmation is about
    # the whole flow, so it marks every one of its actions covered too.
    confirmed: bool = False
    # Every mutating action this flow actually performed (identity.
    # mutating_signature_set, sorted). Kept alongside action_matches as a
    # plain name list for callers (e.g. M4 codegen) that just need "what
    # does this flow do", not the coverage detail.
    mutating_actions: list[str] = field(default_factory=list)
    # Per-action breakdown: [{"action", "matched_tcms_id",
    # "matched_tcms_title", "score", "covered"}, ...] -- one entry per
    # mutating_actions entry, empty for "navigation" flows.
    action_matches: list[dict] = field(default_factory=list)


@dataclass
class TcmsCoverage:
    """One TCMS test case's verdict against the discovered flows -- did
    the crawl find *anything* resembling what this test describes.
    Matched against individual actions now, not whole flow transcripts
    (see gap_analysis.py); matched_flow_id is one flow that performs the
    matching action, picked for the operator to have somewhere concrete
    to look, not necessarily the only one that does."""
    tcms_id: str
    tcms_title: str
    status: str  # "covered" | "not_found"
    matched_flow_id: Optional[int]
    score: float
    confirmed: bool = False
    # Only meaningful when status == "not_found" -- see
    # gap_analysis.py's _diagnose_not_found(). Turns a bare "not found"
    # into a specific, grounded reason using data the crawl already
    # collected (skipped_candidates, error checkpoints) -- never new
    # browsing, never a guess about intent. None means neither matched:
    # could be a stale test case, a reachable path this crawl never got
    # close to, or a precondition (already-logged-in admin, an item
    # already in the cart) this crawl's clean-slate-per-path model
    # doesn't produce -- the report says so honestly rather than
    # picking one.
    diagnosis: Optional[str] = None         # "withheld" | "errored" | "discovered_not_walked" | None
    diagnosis_detail: Optional[str] = None  # human-readable, ready for the report


@dataclass
class GapAnalysis:
    tcms_source: str
    threshold: float
    status: str
    flow_coverage: list[FlowCoverage] = field(default_factory=list)
    tcms_coverage: list[TcmsCoverage] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "flows_covered": sum(1 for x in self.flow_coverage if x.status == "covered"),
            "flows_partial": sum(1 for x in self.flow_coverage if x.status == "partial"),
            "flows_gap": sum(1 for x in self.flow_coverage if x.status == "gap"),
            "flows_navigation": sum(1 for x in self.flow_coverage if x.status == "navigation"),
            "flows_confirmed": sum(1 for x in self.flow_coverage if x.confirmed),
            "tcms_covered": sum(1 for x in self.tcms_coverage if x.status == "covered"),
            "tcms_not_found": sum(1 for x in self.tcms_coverage if x.status == "not_found"),
            "tcms_not_found_withheld": sum(1 for x in self.tcms_coverage if x.diagnosis == "withheld"),
            "tcms_not_found_errored": sum(1 for x in self.tcms_coverage if x.diagnosis == "errored"),
            "tcms_not_found_discovered_not_walked": sum(
                1 for x in self.tcms_coverage if x.diagnosis == "discovered_not_walked"),
        }

    def to_json(self) -> dict:
        return {
            "tcms_source": self.tcms_source,
            "threshold": self.threshold,
            "status": self.status,
            "flow_coverage": [asdict(x) for x in self.flow_coverage],
            "tcms_coverage": [asdict(x) for x in self.tcms_coverage],
            "summary": self.summary(),
        }

    @classmethod
    def from_json(cls, d: dict) -> "GapAnalysis":
        """Reconstruct from a saved gap_analysis.json -- both member
        dataclasses (FlowCoverage, TcmsCoverage) are flat JSON-safe
        fields already, so a plain **kwargs unpack is enough (unlike
        RunResult.from_json's states/flows, which need real
        reconstruction for their own nested types). Used when
        re-rendering a report after resume_flow() extends a run's flows
        without a fresh TCMS upload -- keeps whatever gap analysis
        already existed visible rather than silently dropping it, even
        though it's now stale relative to the newly-added flows (see
        web/runs.py's own note on this at the resume call site)."""
        return cls(
            tcms_source=d["tcms_source"], threshold=d["threshold"], status=d["status"],
            flow_coverage=[FlowCoverage(**x) for x in d.get("flow_coverage", [])],
            tcms_coverage=[TcmsCoverage(**x) for x in d.get("tcms_coverage", [])],
        )


@dataclass
class ChangeEvent:
    """One identity's status relative to the *previous* run of this
    project -- see change_detection.py. "missing" is deliberately not
    "broken": FlowScout doesn't know whether an absent flow means the
    feature was removed, a real regression, or just crawl variance
    (breadth/depth budgets and live-site timing are both real, measured
    sources of run-to-run non-determinism on some targets -- see
    ROADMAP.md). The operator decides; the report states the fact."""
    identity: str
    kind: str  # "new" | "changed" | "missing"
    # For "changed"/"missing": populated when this identity has a
    # confirmed TCMS link from a PRIOR run (project_state, a human's own
    # `flowscout confirm`) -- a certain pairing. For "new" (Aug 2026):
    # populated instead from THIS run's own gap analysis, if one was
    # attached -- a fuzzy embedding match, not a confirmed one, but still
    # the honest difference between "genuinely undocumented new
    # behavior" and "a new capability that already has a matching test
    # case waiting for it". None either way if nothing matched, or no
    # TCMS/gap analysis was available to check against.
    tcms_id: Optional[str] = None
    flow_id: Optional[int] = None       # this run's flow id -- absent for "missing"
    summary: str = ""                   # current (new/changed) or last-known (missing) step text
    previous_summary: str = ""          # only meaningful for "changed"


@dataclass
class ChangeReport:
    project: str
    baseline: bool                          # True: no prior state existed, nothing to compare against
    environment_mismatch: Optional[str] = None
    events: list[ChangeEvent] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "new": sum(1 for e in self.events if e.kind == "new"),
            "changed": sum(1 for e in self.events if e.kind == "changed"),
            "missing": sum(1 for e in self.events if e.kind == "missing"),
            "changed_confirmed": sum(1 for e in self.events if e.kind == "changed" and e.tcms_id),
            "missing_confirmed": sum(1 for e in self.events if e.kind == "missing" and e.tcms_id),
            # A "new" event's tcms_id (Aug 2026) comes from THIS run's own
            # gap analysis, not a prior human confirmation the way
            # changed/missing's tcms_id does -- a fuzzy match, not a
            # certain one, so counted separately rather than folded into
            # the same "_confirmed" naming.
            "new_matched": sum(1 for e in self.events if e.kind == "new" and e.tcms_id),
        }

    def to_json(self) -> dict:
        return {
            "project": self.project,
            "baseline": self.baseline,
            "environment_mismatch": self.environment_mismatch,
            "events": [asdict(e) for e in self.events],
            "summary": self.summary(),
        }
