from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codegen, project_state
from .change_detection import detect_changes
from .crawler import crawl
from .dotenv import load_dotenv
from .gap_analysis import DEFAULT_THRESHOLD, analyze_gaps
from .models import ChangeEvent, ChangeReport, GapAnalysis, RunResult
from .report import render_html
from .tcms import load_tcms_csv


def _run_gap_analysis(run: RunResult, tcms_path: str, threshold: float) -> GapAnalysis:
    tcms_items = load_tcms_csv(tcms_path)
    print(f"[flowscout] gap analysis: {len(tcms_items)} TCMS items from {tcms_path} ...", file=sys.stderr)
    state = project_state.load(run.project)
    gap = analyze_gaps(run, tcms_items, tcms_source=tcms_path, threshold=threshold, project_state=state)
    print(f"[flowscout] gap analysis: {gap.status}", file=sys.stderr)
    return gap


def _write_outputs(run: RunResult, out_dir: Path, gap: GapAnalysis | None,
                    changes: ChangeReport | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "flows.json").write_text(json.dumps(run.to_json(), indent=2), encoding="utf-8")
    if gap is not None:
        (out_dir / "gap_analysis.json").write_text(json.dumps(gap.to_json(), indent=2), encoding="utf-8")
    if changes is not None:
        (out_dir / "change_report.json").write_text(json.dumps(changes.to_json(), indent=2), encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(run, gap, changes), encoding="utf-8")
    print(f"[flowscout] wrote {out_dir / 'flows.json'}"
          + (f", {out_dir / 'gap_analysis.json'}" if gap is not None else "")
          + (f", {out_dir / 'change_report.json'}" if changes is not None else "")
          + f", {out_dir / 'report.html'}", file=sys.stderr)


def cmd_crawl(args) -> None:
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    print(f"[flowscout] crawling {config['start_url']} ...", file=sys.stderr)
    run = crawl(config)
    s = run.summary()
    print(f"[flowscout] done: {s['states_discovered']} states, {s['flows_total']} flows "
          f"({s['flows_unique']} unique / {s['flows_duplicate']} duplicate / {s['flows_blocked']} blocked), "
          f"{s['checkpoints']} checkpoints, {s['skipped_candidates']} actions withheld", file=sys.stderr)

    gap = _run_gap_analysis(run, args.tcms, args.gap_threshold) if args.tcms else None

    # Must run before record_run() below overwrites what it would compare
    # against. Runs AFTER gap analysis (Aug 2026) so a brand-new flow that
    # happens to match a TCMS item this same run gets linked in the change
    # report too -- see detect_changes()'s own gap parameter.
    changes = detect_changes(run, gap)
    if changes.baseline:
        print("[flowscout] change detection: baseline run, nothing to compare against yet", file=sys.stderr)
    else:
        cs = changes.summary()
        print(f"[flowscout] change detection: {cs['new']} new ({cs['new_matched']} already match a "
              f"test case), {cs['changed']} changed ({cs['changed_confirmed']} confirmed), "
              f"{cs['missing']} missing ({cs['missing_confirmed']} confirmed)"
              + (f" -- {changes.environment_mismatch}" if changes.environment_mismatch else ""), file=sys.stderr)

    out_dir = Path(args.out)
    state = project_state.record_run(run, run_id=out_dir.name)
    print(f"[flowscout] project state: {len(state.flows)} known flow identities "
          f"({project_state.state_path(run.project)})", file=sys.stderr)

    _write_outputs(run, out_dir, gap, changes)


def cmd_gap(args) -> None:
    flows_path = Path(args.run) / "flows.json"
    run = RunResult.from_json(json.loads(flows_path.read_text(encoding="utf-8")))
    gap = _run_gap_analysis(run, args.tcms, args.threshold)
    # No new crawl happened, so there's no fresh change_report to compute --
    # but if the original crawl already wrote one, regenerating report.html
    # here shouldn't silently drop that section.
    changes = None
    existing = Path(args.run) / "change_report.json"
    if existing.exists():
        data = json.loads(existing.read_text(encoding="utf-8"))
        changes = ChangeReport(
            project=data["project"], baseline=data["baseline"],
            environment_mismatch=data.get("environment_mismatch"),
            events=[ChangeEvent(**e) for e in data.get("events", [])],
        )
    _write_outputs(run, Path(args.out or args.run), gap, changes)


def cmd_confirm(args) -> None:
    state = project_state.load(args.project)
    if args.tcms_id:
        rec = state.confirm_tcms_link(args.identity, args.tcms_id)
        print(f"[flowscout] confirmed: flow identity {args.identity} -> TCMS {args.tcms_id}", file=sys.stderr)
    if args.approve or args.unapprove:
        rec = state.set_approved(args.identity, approved=bool(args.approve))
        print(f"[flowscout] approved_for_codegen = {rec.approved_for_codegen} for {args.identity}", file=sys.stderr)
    project_state.save(state)


def cmd_codegen(args) -> None:
    flows_path = Path(args.run) / "flows.json"
    run = RunResult.from_json(json.loads(flows_path.read_text(encoding="utf-8")))
    flows, note = codegen.select_candidate_flows(
        run, args.tcms, args.threshold, approved_only=args.approved_only)
    print(f"[flowscout] codegen: {note}", file=sys.stderr)
    summary = codegen.generate(run, flows, Path(args.out), id_prefix=args.id_prefix)
    print(f"[flowscout] codegen: {summary['test_worthy']} test-worthy "
          f"({summary['not_worthy']} filtered as shared-step-only/no state change), "
          f"{summary['shared_prefix_steps']}-step shared prologue, "
          f"{summary['fragile_tests']} generated test(s) have a fragile (text-based) locator", file=sys.stderr)
    print(f"[flowscout] wrote {args.out}/drafts.md, {args.out}/drafts.csv, "
          f"{args.out}/test_flowscout_drafts.py", file=sys.stderr)


def cmd_serve(args) -> None:
    import uvicorn
    print(f"[flowscout] operator UI at http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run("flowscout.web.app:app", host=args.host, port=args.port, reload=False)


def main(argv=None):
    load_dotenv(".env.local", ".env")  # e.g. GEMINI_API_KEY -- see .env.example

    parser = argparse.ArgumentParser(prog="flowscout")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="run the exploration crawler")
    p_crawl.add_argument("--config", required=True, help="path to a run config JSON")
    p_crawl.add_argument("--out", required=True, help="output directory for flows.json / report.html")
    p_crawl.add_argument("--tcms", help="optional TCMS CSV export to run gap analysis against")
    p_crawl.add_argument("--gap-threshold", type=float, default=DEFAULT_THRESHOLD)
    p_crawl.set_defaults(func=cmd_crawl)

    p_gap = sub.add_parser("gap", help="run gap analysis against an existing crawl (no re-crawl)")
    p_gap.add_argument("--run", required=True, help="directory of a previous crawl (contains flows.json)")
    p_gap.add_argument("--tcms", required=True, help="TCMS CSV export")
    p_gap.add_argument("--out", help="output directory (default: overwrite --run in place)")
    p_gap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p_gap.set_defaults(func=cmd_gap)

    p_confirm = sub.add_parser(
        "confirm", help="record an operator decision against a flow identity (survives re-crawls)")
    p_confirm.add_argument("--project", required=True, help="project name (as used in the run config)")
    p_confirm.add_argument("--identity", required=True, help="flow identity, from a gap-analysis or report output")
    p_confirm.add_argument("--tcms-id", help="confirm this flow corresponds to this TCMS case id")
    p_confirm.add_argument("--approve", action="store_true", help="approve this flow for future test-case codegen")
    p_confirm.add_argument("--unapprove", action="store_true", help="revoke a previous --approve")
    p_confirm.set_defaults(func=cmd_confirm)

    p_codegen = sub.add_parser(
        "codegen", help="generate test-case drafts + pytest-playwright specs from a completed crawl")
    p_codegen.add_argument("--run", required=True, help="directory of a previous crawl (contains flows.json)")
    p_codegen.add_argument("--out", required=True, help="output directory for drafts.md / drafts.csv / test file")
    p_codegen.add_argument("--tcms", help="TCMS CSV export -- default filter is gap flows only when given")
    p_codegen.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="gap-analysis threshold")
    p_codegen.add_argument("--approved-only", action="store_true",
                            help="only flows explicitly approved via 'flowscout confirm --approve'")
    p_codegen.add_argument("--id-prefix", default="TC-DRAFT", help="prefix for generated draft ids")
    p_codegen.set_defaults(func=cmd_codegen)

    p_serve = sub.add_parser("serve", help="start the local operator UI")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
