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
"""
from __future__ import annotations

from urllib.parse import urlsplit

from . import project_state as project_state_module
from .identity import content_hash, flow_identity
from .models import ChangeEvent, ChangeReport, FlowStatus, RunResult


def _domain(url: str) -> str:
    try:
        return urlsplit(url).netloc
    except Exception:
        return url


def detect_changes(run: RunResult) -> ChangeReport:
    """Call BEFORE project_state.record_run(run, ...) -- needs to read
    the state as it existed prior to this run overwriting it."""
    prior = project_state_module.load(run.project)
    baseline = len(prior.flows) == 0

    env_mismatch = None
    if prior.start_url and _domain(prior.start_url) != _domain(run.start_url):
        env_mismatch = (
            f'project state was built against "{prior.start_url}"; this run started at '
            f'"{run.start_url}" -- different environment, the comparison below may not be meaningful'
        )

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
            events.append(ChangeEvent(identity=ident, kind="new", flow_id=flow_id, summary=summary))
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
