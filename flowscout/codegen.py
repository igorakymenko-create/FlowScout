"""Orchestrates M4's three outputs from a completed run -- ties
together shared_steps.py (prologue subtraction + test-worthiness),
testcase_draft.py (Markdown + CSV), and playwright_codegen.py (pytest
specs) into one `flowscout codegen` call.
"""
from __future__ import annotations

from pathlib import Path

from . import project_state as project_state_module
from .gap_analysis import DEFAULT_THRESHOLD, analyze_gaps
from .identity import flow_identity
from .models import Flow, FlowStatus, RunResult
from .playwright_codegen import PYTEST_IMPORTS, render_pytest
from .shared_steps import split_flows
from .tcms import load_tcms_csv
from .testcase_draft import build_drafts, render_csv, render_markdown


def select_candidate_flows(run: RunResult, tcms_path: str | None, threshold: float,
                            approved_only: bool) -> tuple[list[Flow], str]:
    """Returns (flows, note). Default: gap and partially-covered flows,
    if a TCMS was given -- a fully covered flow already has a test;
    regenerating one is noise. A partially-covered flow (see
    gap_analysis.py: some but not all of its actions matched something)
    still has real untested behavior in it, same as a full gap, so it's
    included too. Without a TCMS there's nothing to diff against, so
    every unique flow is a candidate. --approved-only additionally
    requires `flowscout confirm --approve` on the flow's identity."""
    unique = [f for f in run.flows if f.status == FlowStatus.UNIQUE]
    state = project_state_module.load(run.project)

    if tcms_path:
        tcms_items = load_tcms_csv(tcms_path)
        gap = analyze_gaps(run, tcms_items, tcms_source=tcms_path, threshold=threshold, project_state=state)
        gap_ids = {x.flow_id for x in gap.flow_coverage if x.status in ("gap", "partial")}
        flows = [f for f in unique if f.id in gap_ids]
        note = f"{len(flows)} of {len(unique)} unique flows are gap/partial (uncovered by {tcms_path})"
    else:
        flows = unique
        note = f"no --tcms given: considering all {len(flows)} unique flows (no gap filter applied)"

    if approved_only:
        before = len(flows)
        flows = [f for f in flows
                 if (rec := state.flows.get(flow_identity(f, run.states))) and rec.approved_for_codegen]
        note += f"; --approved-only: {len(flows)} of {before} approved (see 'flowscout confirm --approve')"

    return flows, note


def generate(run: RunResult, flows: list[Flow], out_dir: Path, id_prefix: str = "TC-DRAFT") -> dict:
    """Writes drafts.md, drafts.csv, test_flowscout_drafts.py into
    out_dir. Returns a summary dict for CLI/log output."""
    prefix, splits = split_flows(flows)
    worthy = [s for s in splits if s.test_worthy]

    out_dir.mkdir(parents=True, exist_ok=True)

    cases = build_drafts(splits, run, id_prefix=id_prefix)
    (out_dir / "drafts.md").write_text(render_markdown(cases, prefix, run), encoding="utf-8")
    (out_dir / "drafts.csv").write_text(render_csv(cases), encoding="utf-8")

    fragile_count = 0
    func_bodies = []
    for i, s in enumerate(worthy, start=1):
        src, fragile = render_pytest(f"{id_prefix}-{i}", s, prefix, run)
        fragile_count += int(fragile)
        func_bodies.append(src)

    if func_bodies:
        py_source = PYTEST_IMPORTS + "\n\n" + "\n\n".join(func_bodies)
    else:
        py_source = PYTEST_IMPORTS + "\n\n# No test-worthy flows in this batch -- nothing to generate.\n"
    (out_dir / "test_flowscout_drafts.py").write_text(py_source, encoding="utf-8")

    return {
        "candidates": len(flows),
        "shared_prefix_steps": len(prefix),
        "test_worthy": len(worthy),
        "not_worthy": len(splits) - len(worthy),
        "fragile_tests": fragile_count,
    }
