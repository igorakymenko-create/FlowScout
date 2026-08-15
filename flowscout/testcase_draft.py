"""Flow -> human-readable test-case draft: Markdown for a person to read
and fill in, plus a TCMS-importable CSV (same id/title/steps shape
tcms.py already reads, so a draft can round-trip back into gap analysis
next time).

Design decision (see ROADMAP.md M4): never invents expected results.
Behavioral checks ("the order total is $X") are genuinely unknowable
from a crawl and are left as an explicit blank for the operator.
Structural/reachability facts (which page a flow observably, repeatably
lands on) ARE recorded -- that's an observation, not a fabrication -- and
are kept in their own clearly-labeled block so the two are never
presented as equally certain.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .fingerprint import human_page_label
from .identity import flow_identity
from .models import RunResult
from .shared_steps import FlowSplit


def _short_label(label: str) -> str:
    """Trim a verbose action_label (which may carry a parenthetical
    field dump, e.g. 'Fill form and submit "Continue" (firstName=...)')
    down to something fit for a title."""
    return label.split(" (")[0].strip()


@dataclass
class DraftCase:
    draft_id: str
    title: str
    identity: str
    end_page: str
    steps: list[str]           # remainder steps only -- precondition is separate
    structural_check: str


def build_drafts(splits: list[FlowSplit], run: RunResult, id_prefix: str = "TC-DRAFT") -> list[DraftCase]:
    cases = []
    for i, s in enumerate(splits, start=1):
        if not s.test_worthy:
            continue
        end_state = run.states.get(s.flow.end_state_fp)
        end_page = human_page_label(end_state.url_pattern) if end_state else "unknown page"
        # Same criterion as identity.py's mutating_signature_set -- a
        # choice step (wizard option, sort order) belongs in the title
        # just as much as a state-changing one.
        title_bits = [_short_label(t.action_label) for t in s.remainder if t.risk.value == "mutating" or t.is_choice]
        title = " -> ".join(title_bits) or _short_label(s.remainder[-1].action_label)
        cases.append(DraftCase(
            draft_id=f"{id_prefix}-{i}",
            title=title,
            identity=flow_identity(s.flow, run.states),
            end_page=end_page,
            steps=[t.action_label for t in s.remainder],
            structural_check=f'Reaches "{end_page}" ({end_state.url_pattern if end_state else "?"}).',
        ))
    return cases


def render_markdown(cases: list[DraftCase], prefix_steps: list, run: RunResult) -> str:
    lines = [f"# Test case drafts — {run.project}", "", f"Source: FlowScout run, `{run.start_url}`.", ""]

    if prefix_steps:
        lines += ["## Shared precondition", "", "Every draft below assumes this already happened:", ""]
        for i, t in enumerate(prefix_steps, start=1):
            lines.append(f"{i}. {t.action_label}")
        lines.append("")

    if not cases:
        lines.append("_No flows in this batch reach a state-changing action worth its own test case._")
        return "\n".join(lines)

    for c in cases:
        lines += [
            f"## {c.draft_id}: {c.title}",
            "",
            f"**Flow identity:** `{c.identity}` — use with `flowscout confirm --identity {c.identity} --tcms-id ...`",
            "",
            "| # | Step | Expected result |",
            "|---|------|------------------|",
        ]
        for i, step in enumerate(c.steps, start=1):
            lines.append(f"| {i} | {step} | _(fill in)_ |")
        lines += [
            "",
            f"**Structural check (observed, not a behavioral assertion):** {c.structural_check}",
            "",
        ]
    return "\n".join(lines)


def render_csv(cases: list[DraftCase]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "title", "steps"])
    for c in cases:
        steps_text = "\n".join(f"{i}. {s}\n   Expected result: " for i, s in enumerate(c.steps, start=1))
        writer.writerow([c.draft_id, c.title, steps_text])
    return buf.getvalue()
