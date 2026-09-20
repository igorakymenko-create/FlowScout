"""Project-level persistent state: operator decisions that must survive
a re-crawl.

Everything else FlowScout writes is per-run (`runs/<run_id>/flows.json`)
-- a snapshot, disposable, safe to delete. This is the durable layer on
top of it, keyed by `identity.flow_identity` rather than a run's
sequential flow id (which restarts at 1 every crawl and carries no
meaning across runs). Lives in `projects/<project-slug>/state.json`,
deliberately separate from `runs/` so "run history" (ephemeral) and
"project state" (durable) aren't tangled in one directory.

M3.5 scope: record what was seen and let an operator confirm a flow's
TCMS link or approve it for codegen. It does NOT yet diff/alert on
change -- that's M5. What it does store (`last_seen_content_hash`) is
exactly what M5 needs to compute that diff later without a rewrite: the
old value is sitting right there in the record *before* record_seen()
overwrites it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .identity import content_hash, flow_identity
from .models import FlowStatus, RunResult

PROJECTS_DIR = Path("projects")


def _slugify(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip().lower())
    return safe.strip("-") or "project"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class FlowRecord:
    identity: str
    tcms_id: Optional[str] = None
    confirmed_at: Optional[str] = None
    approved_for_codegen: bool = False
    last_seen_run: Optional[str] = None
    last_seen_content_hash: Optional[str] = None
    last_seen_at: Optional[str] = None
    # Human-readable breadcrumb (e.g. the flow's step text) so state.json
    # is inspectable on its own, without cross-referencing a run's
    # flows.json just to remember what this identity even is.
    last_seen_summary: str = ""


@dataclass
class ProjectState:
    project: str
    # Crawl entry point, recorded so a state file built against one
    # environment (e.g. staging) can be flagged, not silently applied,
    # against a crawl of a different one (e.g. prod). Enforced in M5's
    # change_detection.py (environment_mismatch), not here -- this field
    # just carries the value that check compares against.
    start_url: str = ""
    # Which CONFIGURATION of the project this state belongs to (Sep
    # 2026) -- a per-customer tenant, a feature-flag set, an
    # environment. Empty for every config written before this existed,
    # which keeps their state file exactly where it already is. See
    # state_path()/variant_of() for why this has to be part of the key
    # rather than folded into the project name by hand.
    variant: str = ""
    flows: dict[str, FlowRecord] = field(default_factory=dict)

    def record_seen(self, identity: str, run_id: str, content_hash: str, summary: str) -> FlowRecord:
        rec = self.flows.get(identity)
        if rec is None:
            rec = FlowRecord(identity=identity)
            self.flows[identity] = rec
        rec.last_seen_run = run_id
        rec.last_seen_content_hash = content_hash
        rec.last_seen_summary = summary
        rec.last_seen_at = _now()
        return rec

    def confirm_tcms_link(self, identity: str, tcms_id: str) -> FlowRecord:
        rec = self.flows.get(identity)
        if rec is None:
            rec = FlowRecord(identity=identity)
            self.flows[identity] = rec
        rec.tcms_id = tcms_id
        rec.confirmed_at = _now()
        return rec

    def set_approved(self, identity: str, approved: bool) -> FlowRecord:
        rec = self.flows.get(identity)
        if rec is None:
            rec = FlowRecord(identity=identity)
            self.flows[identity] = rec
        rec.approved_for_codegen = approved
        return rec

    def confirmed_identity_for_tcms(self, tcms_id: str) -> Optional[str]:
        """Reverse lookup: which flow identity, if any, has already been
        confirmed as the match for this TCMS item. Used by gap_analysis
        to skip re-guessing a pairing a human already settled."""
        for identity, rec in self.flows.items():
            if rec.tcms_id == tcms_id:
                return identity
        return None


def variant_of(config: dict) -> str:
    """The configuration label a run was crawled under, from
    `config["variant"]` -- a per-customer tenant, a feature-flag set, an
    environment name. Free-form and operator-chosen on purpose: FlowScout
    has no way to detect from outside which customer's customization or
    which flag set it is looking at, and guessing one (hashing the
    config, say) would silently split or merge baselines whenever an
    unrelated setting changed. Empty when unset, which is every config
    written before this existed."""
    return str(config.get("variant") or "").strip()


def state_path(project: str, variant: str = "") -> Path:
    """`projects/<project>/state.json`, or
    `projects/<project>/variants/<variant>/state.json` when a variant is
    set.

    Why the variant has to be part of the path (Sep 2026, raised
    directly by a skeptical user): project state is what change
    detection compares a new crawl against, and it was keyed by project
    NAME alone. Crawl customer A, then crawl customer B under the same
    project name, and B is diffed against A's baseline -- every flow
    unique to A reads as "missing since last run" and every flow unique
    to B as "new", when in reality nothing changed in either system.
    The same applies to one tenant crawled with a feature flag on and
    then off. Folding the variant into the project name by hand would
    work too, but it would also fragment the RUN history and the
    reports, which genuinely do belong to one project."""
    base = PROJECTS_DIR / _slugify(project)
    return base / "variants" / _slugify(variant) / "state.json" if variant else base / "state.json"


def load(project: str, variant: str = "") -> ProjectState:
    path = state_path(project, variant)
    if not path.exists():
        return ProjectState(project=project, variant=variant)
    data = json.loads(path.read_text(encoding="utf-8"))
    flows = {k: FlowRecord(**v) for k, v in data.get("flows", {}).items()}
    return ProjectState(project=data.get("project", project), start_url=data.get("start_url", ""),
                         variant=data.get("variant", variant), flows=flows)


def record_run(run: RunResult, run_id: str) -> ProjectState:
    """Update (and persist) project state with every unique flow from a
    completed run. Called automatically after every crawl -- pure
    bookkeeping, no operator action required for this part. Duplicate
    and blocked flows aren't recorded: they don't represent something an
    operator would link to a test case or approve for codegen."""
    state = load(run.project, variant_of(run.config))
    if not state.start_url:
        state.start_url = run.start_url
    for flow in run.flows:
        if flow.status != FlowStatus.UNIQUE:
            continue
        identity = flow_identity(flow, run.states)
        chash = content_hash(flow)
        summary = " > ".join(t.action_label for t in flow.transitions)[:200]
        state.record_seen(identity, run_id, chash, summary)
    save(state)
    return state


def save(state: ProjectState) -> None:
    path = state_path(state.project, state.variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "project": state.project,
        "start_url": state.start_url,
        "variant": state.variant,
        "flows": {k: asdict(v) for k, v in state.flows.items()},
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
