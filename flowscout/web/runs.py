"""Run lifecycle management for the local operator UI.

No job queue, no database -- this is a single local process. A run is a
background thread (crawler.crawl() is synchronous/blocking, built on
sync Playwright, so it can't run directly on FastAPI's event loop
without freezing every other request). State lives in memory while a
run is in flight and on disk (runs/<run_id>/) once it's written, so the
run list survives a server restart by re-scanning the directory.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import project_state as project_state_module
from ..change_detection import detect_changes
from ..crawler import crawl
from ..gap_analysis import DEFAULT_THRESHOLD, analyze_gaps
from ..models import ChangeEvent, ChangeReport, GapAnalysis, RunResult
from ..report import render_html
from ..tcms import TcmsItem, load_tcms_csv

RUNS_DIR = Path("runs")


@dataclass
class RunHandle:
    run_id: str
    config: dict
    status: str = "running"  # running | done | error
    error: Optional[str] = None
    summary: Optional[dict] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    # Non-fatal: a bad TCMS file (or GEMINI_API_KEY not set) shouldn't lose
    # a completed crawl -- see _execute. Set only when a tcms_path was given
    # and gap analysis itself couldn't run; the crawl's own status is
    # unaffected.
    gap_error: Optional[str] = None


_runs: dict[str, RunHandle] = {}
_lock = threading.Lock()


def _slugify(project: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in project.strip().lower())
    return safe.strip("-") or "run"


def start_run(config: dict, tcms_path: Optional[str] = None, gap_threshold: float = DEFAULT_THRESHOLD) -> str:
    """`tcms_path` (Aug 2026): attach a TCMS export at crawl-creation time
    so a single API call produces a complete report -- flows AND gap
    analysis -- with nothing left for a human to do afterward. The step
    that makes this usable from CI, not just the local operator UI: the
    post-hoc `/api/runs/{id}/gap` upload still exists for a run that
    didn't have one attached, but CI wants one request with one response
    it can check, not "start a crawl, poll for it, then make a second
    call and poll for THAT.\""""
    project = config.get("project") or "run"
    run_id = f"{_slugify(project)}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    handle = RunHandle(run_id=run_id, config=config)
    with _lock:
        _runs[run_id] = handle
    threading.Thread(target=_execute, args=(run_id, config, tcms_path, gap_threshold), daemon=True).start()
    return run_id


def _execute(run_id: str, config: dict, tcms_path: Optional[str] = None,
             gap_threshold: float = DEFAULT_THRESHOLD) -> None:
    handle = _runs[run_id]
    out_dir = RUNS_DIR / run_id
    try:
        run = crawl(config)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "flows.json").write_text(json.dumps(run.to_json(), indent=2), encoding="utf-8")
        # Must run before record_run() below overwrites what it compares against.
        changes = detect_changes(run)
        (out_dir / "change_report.json").write_text(json.dumps(changes.to_json(), indent=2), encoding="utf-8")

        gap: Optional[GapAnalysis] = None
        if tcms_path:
            # A malformed TCMS file (or no GEMINI_API_KEY) must not lose an
            # otherwise-successful crawl -- same "degrade, don't fail the
            # whole run" convention as semantic dedup. analyze_gaps() itself
            # already degrades gracefully for a missing key; this catches
            # load_tcms_csv() raising on a genuinely bad file.
            try:
                tcms_items = load_tcms_csv(tcms_path)
                state = project_state_module.load(run.project)
                gap = analyze_gaps(run, tcms_items, tcms_source=tcms_path,
                                    threshold=gap_threshold, project_state=state)
                (out_dir / "gap_analysis.json").write_text(json.dumps(gap.to_json(), indent=2), encoding="utf-8")
            except Exception as exc:
                handle.gap_error = str(exc)[:500]

        (out_dir / "report.html").write_text(render_html(run, gap, changes), encoding="utf-8")
        project_state_module.record_run(run, run_id=run_id)
        handle.summary = run.summary()
        handle.status = "done"
    except Exception as exc:
        handle.status = "error"
        handle.error = f"{exc}\n\n{traceback.format_exc()[-2000:]}"
    finally:
        handle.finished_at = time.time()
        if tcms_path:
            Path(tcms_path).unlink(missing_ok=True)


def get_run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def get_handle(run_id: str) -> Optional[RunHandle]:
    return _runs.get(run_id)


def _read_disk_run(run_id: str, d: Path) -> Optional[dict]:
    flows_path = d / "flows.json"
    if not flows_path.exists():
        return None
    try:
        data = json.loads(flows_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "run_id": run_id,
        "project": data.get("project", run_id),
        "status": "done",
        "summary": data.get("summary"),
        "error": None,
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "has_report": (d / "report.html").exists(),
        "has_gap_analysis": (d / "gap_analysis.json").exists(),
        "has_change_report": (d / "change_report.json").exists(),
    }


def list_runs() -> list[dict]:
    by_id: dict[str, dict] = {}
    if RUNS_DIR.exists():
        for d in sorted(RUNS_DIR.iterdir()):
            if not d.is_dir():
                continue
            info = _read_disk_run(d.name, d)
            if info:
                by_id[d.name] = info
    with _lock:
        for run_id, h in _runs.items():
            if h.status == "running":
                by_id[run_id] = {
                    "run_id": run_id,
                    "project": h.config.get("project", run_id),
                    "status": "running",
                    "summary": None,
                    "error": None,
                    "started_at": h.started_at,
                    "finished_at": None,
                    "has_report": False,
                    "has_gap_analysis": False,
                    "has_change_report": False,
                }
            elif h.status == "error":
                by_id[run_id] = {
                    "run_id": run_id,
                    "project": h.config.get("project", run_id),
                    "status": "error",
                    "summary": None,
                    "error": h.error,
                    "started_at": h.started_at,
                    "finished_at": h.finished_at,
                    "has_report": False,
                    "has_gap_analysis": False,
                    "has_change_report": False,
                }
            # status == "done": the on-disk scan above already has it with full data.

    def sort_key(r: dict):
        return str(r.get("finished_at") or r.get("started_at") or "")

    return sorted(by_id.values(), key=sort_key, reverse=True)


def get_run_status(run_id: str) -> Optional[dict]:
    with _lock:
        h = _runs.get(run_id)
        if h and h.status != "done":
            return {
                "run_id": run_id, "status": h.status, "error": h.error, "summary": h.summary,
                "started_at": h.started_at, "finished_at": h.finished_at, "gap_error": h.gap_error,
            }
    info = _read_disk_run(run_id, RUNS_DIR / run_id)
    # gap_error only lives on the in-memory handle (not persisted to disk) --
    # surfaced here so a CI script polling this endpoint can tell "the crawl
    # finished but the TCMS file it was given couldn't be used" apart from
    # "no TCMS was ever attached", rather than both looking like a silent
    # has_gap_analysis: false.
    if info is not None and h is not None and h.gap_error:
        info["gap_error"] = h.gap_error
    return info


def _load_change_report(out_dir: Path) -> Optional[ChangeReport]:
    path = out_dir / "change_report.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return ChangeReport(
        project=data["project"], baseline=data["baseline"],
        environment_mismatch=data.get("environment_mismatch"),
        events=[ChangeEvent(**e) for e in data.get("events", [])],
    )


def run_gap_analysis(run_id: str, tcms_path: str, threshold: float = DEFAULT_THRESHOLD) -> GapAnalysis:
    out_dir = get_run_dir(run_id)
    flows_path = out_dir / "flows.json"
    if not flows_path.exists():
        raise FileNotFoundError(f"no completed run at {run_id}")
    run = RunResult.from_json(json.loads(flows_path.read_text(encoding="utf-8")))
    tcms_items: list[TcmsItem] = load_tcms_csv(tcms_path)
    state = project_state_module.load(run.project)
    gap = analyze_gaps(run, tcms_items, tcms_source=tcms_path, threshold=threshold, project_state=state)
    (out_dir / "gap_analysis.json").write_text(json.dumps(gap.to_json(), indent=2), encoding="utf-8")
    # Regenerating report.html here shouldn't silently drop a change-report
    # section the original crawl already produced.
    changes = _load_change_report(out_dir)
    (out_dir / "report.html").write_text(render_html(run, gap, changes), encoding="utf-8")
    return gap


def confirm_tcms_link(project: str, identity: str, tcms_id: str) -> None:
    state = project_state_module.load(project)
    state.confirm_tcms_link(identity, tcms_id)
    project_state_module.save(state)


def set_approved(project: str, identity: str, approved: bool) -> None:
    state = project_state_module.load(project)
    state.set_approved(identity, approved)
    project_state_module.save(state)
