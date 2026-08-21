"""Longitudinal change detection: did this crawl look different from the
last one FlowScout ran against this project?

Built directly on M3.5's persistent identity + project state. Compares
this run's flows against the *previous* state.json contents (must be
called before project_state.record_run() overwrites it) and classifies
every identity as new / changed / missing / unchanged relative to last
time.

Deliberately does not treat "missing" as "broken". FlowScout doesn't
guess whether an absent flow means the feature was removed, a real
regression, or just crawl variance -- and that last one is a measured,
real risk, not a hypothetical: on this project's own Site B crawls,
three back-to-back re-crawls of an *unchanged* target produced different
state/flow counts each time (network timing affecting occlusion
detection, and breadth-budget competition from a repeated header nav --
see ROADMAP.md). A saucedemo re-crawl, by contrast, was verified
bit-for-bit identical (same 6 identities, same content hashes) across
two consecutive runs -- so how much to trust a "missing" signal
genuinely depends on the target, and the report says so rather than
presenting every target with the same confidence.

The report never says "broken" -- same principle as TCMS not_found and
as never inventing expected results: state the fact, let the operator
decide.

**"new" events linked to gap analysis (Aug 2026).** Raised directly:
a brand-new flow could be genuinely new app functionality, or a path
that was always reachable but got newly unblocked by a bug fix (in the
app, or in FlowScout's own crawler) -- this project can't tell those
apart and doesn't pretend to. But there's a real, checkable follow-up
question a "new" event alone couldn't answer before this: does this
new flow already match something in the test plan? If gap analysis
(run.flow_coverage) already marked it "covered" or "partial" this same
run, that's the honest, actionable difference between "genuinely
undocumented behavior, worth a human's attention" and "a new capability
that already has a test case waiting for it" -- the second case isn't
concerning the way an unexplained new flow is. Optional and best-effort:
`gap` is None whenever no TCMS was attached to this run at all, in
which case "new" events carry no tcms_id, same as before this existed.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import project_state as project_state_module
from .identity import content_hash, flow_identity
from .models import ChangeEvent, ChangeReport, FlowStatus, GapAnalysis, RunResult


def _domain(url: str) -> str:
    try:
        return urlsplit(url).netloc
    except Exception:
        return url


def detect_changes(run: RunResult, gap: GapAnalysis | None = None) -> ChangeReport:
    """Call BEFORE project_state.record_run(run, ...) -- needs to read
    the state as it existed prior to this run overwriting it. `gap`:
    this same run's gap analysis, if a TCMS was attached -- see the
    module docstring's "new events linked to gap analysis" note. Pass
    it in even though it means calling this *after* analyze_gaps() now,
    not before; recording project state still has to happen after both."""
    prior = project_state_module.load(run.project)
    baseline = len(prior.flows) == 0

    env_mismatch = None
    if prior.start_url and _domain(prior.start_url) != _domain(run.start_url):
        env_mismatch = (
            f'project state was built against "{prior.start_url}"; this run started at '
            f'"{run.start_url}" -- different environment, the comparison below may not be meaningful'
        )

    # flow_id -> tcms_id for whichever of this run's flows gap analysis
    # already found a real match for -- "gap" and "navigation" verdicts
    # correctly contribute nothing here, a "new" flow with no match is
    # exactly as unexplained as one with no gap analysis run at all.
    covered_by_flow_id: dict[int, str] = {
        fc.flow_id: fc.matched_tcms_id
        for fc in (gap.flow_coverage if gap is not None else [])
        if fc.status in ("covered", "partial") and fc.matched_tcms_id
    }

    unique_flows = [f for f in run.flows if f.status == FlowStatus.UNIQUE]
    current: dict[str, tuple[str, int, str]] = {}
    for f in unique_flows:
        ident = flow_identity(f, run.states)
        chash = content_hash(f)
        summary = " > ".join(t.action_label for t in f.transitions)[:200]
        current[ident] = (chash, f.id, summary)

    events: list[ChangeEvent] = []
    for ident, (chash, flow_id, summary) in current.items():
        prior_rec = prior.flows.get(ident)
        if prior_rec is None:
            events.append(ChangeEvent(
                identity=ident, kind="new", flow_id=flow_id, summary=summary,
                tcms_id=covered_by_flow_id.get(flow_id),
            ))
        elif prior_rec.last_seen_content_hash != chash:
            events.append(ChangeEvent(
                identity=ident, kind="changed", tcms_id=prior_rec.tcms_id, flow_id=flow_id,
                summary=summary, previous_summary=prior_rec.last_seen_summary,
            ))
        # else: unchanged -- not reported, nothing for the operator to act on.

    for ident, prior_rec in prior.flows.items():
        if ident not in current:
            events.append(ChangeEvent(
                identity=ident, kind="missing", tcms_id=prior_rec.tcms_id, summary=prior_rec.last_seen_summary,
            ))

    return ChangeReport(project=run.project, baseline=baseline, environment_mismatch=env_mismatch, events=events)
