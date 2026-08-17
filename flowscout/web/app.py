"""Local operator UI: FastAPI backend + a static vanilla-JS frontend.

Run with: python -m flowscout.web  (or: uvicorn flowscout.web.app:app)
Then open http://127.0.0.1:8787

No auth, no hosted secret storage -- this reads/writes the same
.env.local and configs/*.json already used by the CLI. Deliberate
scope: a local tool for one operator on their own machine, not a
hosted multi-tenant product (see ROADMAP.md).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import runs as runs_module
from .. import project_state as project_state_module
from ..dotenv import load_dotenv
from .. import embeddings
from ..field_detect import detect_fields
from ..gap_analysis import DEFAULT_THRESHOLD

load_dotenv(".env.local", ".env")

app = FastAPI(title="FlowScout Operator UI")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).parent / "static"
_CONFIGS_DIR = Path("configs")


@app.get("/api/status")
def status():
    # gap_default_threshold is served rather than duplicated in the UI:
    # it's been recalibrated once already (0.75 -> 0.74, when gap matching
    # moved from flow-level to action-level), and a hardcoded copy in the
    # form silently disagreed with the CLI until it was caught by hand.
    #
    # embeddings_providers (Aug 2026): one row per known provider, not
    # just Gemini -- see embeddings.py's module docstring for why
    # (Gemini isn't the only key an operator might have; Anthropic users
    # in particular have no Gemini key by default and would reach for
    # Voyage AI, Anthropic's own recommended embeddings partner, instead).
    # gemini_key_configured kept alongside for anything still reading the
    # old field name.
    providers = embeddings.provider_status()
    return {"gemini_key_configured": providers["gemini"]["configured"],
            "embeddings_providers": providers,
            "gap_default_threshold": DEFAULT_THRESHOLD}


@app.get("/api/configs")
def list_configs():
    """Saved run configs (configs/*.json) -- lets the UI offer 'load a
    previous config' instead of everyone typing limits/domains from
    scratch every time. No cap on how many -- it's just files in a
    directory; a long list is a UI-polish problem to revisit if it ever
    actually happens, not something worth pre-solving."""
    out = []
    if _CONFIGS_DIR.exists():
        for p in sorted(_CONFIGS_DIR.glob("*.json")):
            try:
                out.append({"name": p.stem, "config": json.loads(p.read_text(encoding="utf-8"))})
            except Exception:
                continue
    return out


def _config_path(name: str) -> Path:
    # Strip anything that isn't alnum/-/_ rather than rejecting the
    # request -- neutralizes path traversal ("../../etc") by construction
    # instead of trying to enumerate bad inputs.
    safe = "".join(c for c in name.strip() if c.isalnum() or c in "-_")
    if not safe:
        raise HTTPException(400, "invalid config name")
    return _CONFIGS_DIR / f"{safe}.json"


@app.put("/api/configs/{name}")
async def save_config(name: str, config: dict):
    _CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = _config_path(name)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return {"name": path.stem}


@app.delete("/api/configs/{name}")
def delete_config(name: str):
    path = _config_path(name)
    if not path.exists():
        raise HTTPException(404, "config not found")
    path.unlink()
    return {"deleted": path.stem}


@app.post("/api/detect-fields")
async def post_detect_fields(body: dict):
    """Read-only visit to `start_url` (see field_detect.py) so an
    operator filling in Credentials can see the site's own field
    name/id/placeholder instead of guessing what a config key needs to
    match. Runs sync Playwright in a worker thread -- short enough
    (one page load, at most one click) to await directly rather than
    needing the background-run + poll pattern runs.py uses for full
    crawls."""
    start_url = (body or {}).get("start_url", "").strip()
    if not start_url:
        raise HTTPException(400, "start_url is required")
    result = await asyncio.to_thread(detect_fields, start_url)
    return result


@app.post("/api/runs")
async def create_run(request: Request):
    """Two request shapes, both supported: a plain JSON body (the
    original contract -- kept working unchanged so nothing that already
    calls this breaks), or multipart/form-data with a `config` field
    (the same JSON, as a string) plus an optional `tcms` file and
    `gap_threshold` -- attaching a TCMS export at crawl-creation time
    (Aug 2026) so one request produces one report with flows AND gap
    analysis already in it. This is the step that makes a crawl usable
    from CI without a human in the loop afterward: the alternative is
    "start a crawl, poll for it, then make a second call and poll for
    that too" (still available -- see POST /api/runs/{id}/gap -- for a
    run that didn't have a TCMS attached up front)."""
    content_type = request.headers.get("content-type", "")
    tcms_path = None
    gap_threshold = DEFAULT_THRESHOLD

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_config = form.get("config")
        if raw_config is None:
            raise HTTPException(400, "config field is required")
        try:
            config = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"config must be valid JSON: {exc}") from None
        raw_threshold = form.get("gap_threshold")
        if raw_threshold:
            try:
                gap_threshold = float(raw_threshold)
            except ValueError:
                raise HTTPException(400, "gap_threshold must be a number") from None
        tcms_file = form.get("tcms")
        if tcms_file is not None and getattr(tcms_file, "filename", ""):
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                shutil.copyfileobj(tcms_file.file, tmp)
                tcms_path = tmp.name
    else:
        config = await request.json()

    if not config.get("start_url"):
        raise HTTPException(400, "start_url is required")
    config.setdefault("project", "run")
    config.setdefault("credentials", {})
    config.setdefault("limits", {"max_depth": 4, "max_breadth_per_state": 8, "max_states": 30, "max_flows": 50,
                                  "max_action_repeat": 2})
    config.setdefault("allow_mutating", False)
    config.setdefault("allowed_domains", [])
    config.setdefault("embeddings_provider", embeddings.DEFAULT_PROVIDER)
    run_id = runs_module.start_run(config, tcms_path=tcms_path, gap_threshold=gap_threshold)
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs():
    return runs_module.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    info = runs_module.get_run_status(run_id)
    if info is None:
        raise HTTPException(404, "run not found")
    return info


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    try:
        runs_module.delete_run(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"deleted": run_id}


@app.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
def get_report(run_id: str):
    path = runs_module.get_run_dir(run_id) / "report.html"
    if not path.exists():
        raise HTTPException(404, "report not ready yet")
    # no-store (Aug 2026): found live -- resume_flow's own
    # window.location.reload() (see report.py's _resume_script_html)
    # re-navigated to this exact URL after a real, confirmed-on-disk
    # update (flows.json genuinely had the new flows), but the browser
    # served a cached copy of the OLD report anyway, since this
    # endpoint's response carried no cache directive at all and this
    # content genuinely changes underneath the same URL over a run's
    # lifetime (gap uploads, and now resume). Every consumer of this
    # endpoint needs a fresh copy every time, not just this one caller.
    return HTMLResponse(content=path.read_text(encoding="utf-8"),
                         headers={"Cache-Control": "no-store"})


@app.get("/api/runs/{run_id}/flows")
def get_flows(run_id: str):
    path = runs_module.get_run_dir(run_id) / "flows.json"
    if not path.exists():
        raise HTTPException(404, "flows.json not found")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/runs/{run_id}/gap")
def get_gap(run_id: str):
    path = runs_module.get_run_dir(run_id) / "gap_analysis.json"
    if not path.exists():
        raise HTTPException(404, "no gap analysis for this run yet")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/runs/{run_id}/changes")
def get_changes(run_id: str):
    """Longitudinal diff against this project's previous run -- see
    change_detection.py. Written automatically at crawl time (not on
    demand like gap analysis), so 404 here means either this run is the
    project's first (a baseline, not a missing feature) or it simply
    hasn't finished yet."""
    path = runs_module.get_run_dir(run_id) / "change_report.json"
    if not path.exists():
        raise HTTPException(404, "no change report for this run")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/runs/{run_id}/gap")
async def post_gap(run_id: str, tcms: UploadFile, threshold: float = Form(DEFAULT_THRESHOLD)):
    if not (runs_module.get_run_dir(run_id) / "flows.json").exists():
        raise HTTPException(404, "run not found or not completed yet")
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        shutil.copyfileobj(tcms.file, tmp)
        tmp_path = tmp.name
    try:
        gap = runs_module.run_gap_analysis(run_id, tmp_path, threshold=threshold)
    except Exception as exc:
        raise HTTPException(400, f"gap analysis failed: {exc}") from None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return gap.to_json()


@app.post("/api/runs/{run_id}/resume")
async def resume_flow(run_id: str, body: dict):
    """Continue exploring from one specific BLOCKED (resumable) flow
    with adjusted limits, instead of re-crawling the whole config --
    see crawler.resume_flow()'s own docstring for exactly which BLOCKED
    reasons this applies to. `body`: {"flow_id": int, "limits": {...}}
    -- `limits` is a partial override (e.g. just {"max_depth": 12}),
    merged over the run's own original limits; an "allow_mutating" key
    inside it is accepted too, applied as a top-level override the same
    way (not nested under limits in the config schema)."""
    flow_id = body.get("flow_id")
    if flow_id is None:
        raise HTTPException(400, "flow_id is required")
    limit_overrides = body.get("limits") or {}
    try:
        run = await asyncio.to_thread(runs_module.resume_flow_in_run, run_id, int(flow_id), limit_overrides)
    except FileNotFoundError:
        raise HTTPException(404, "run not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"summary": run.summary()}


@app.get("/api/projects/{project}/state")
def get_project_state(project: str):
    """The durable cross-run record (identity.py + project_state.py) --
    what flows have been seen, and what an operator has confirmed about
    them. Separate from any single run's flows.json."""
    from dataclasses import asdict
    state = project_state_module.load(project)
    return {"project": state.project, "start_url": state.start_url,
            "flows": {k: asdict(v) for k, v in state.flows.items()}}


@app.post("/api/projects/{project}/confirm")
async def confirm_link(project: str, body: dict):
    identity = body.get("identity")
    tcms_id = body.get("tcms_id")
    if not identity or not tcms_id:
        raise HTTPException(400, "identity and tcms_id are required")
    runs_module.confirm_tcms_link(project, identity, tcms_id)
    return {"ok": True}


@app.post("/api/projects/{project}/approve")
async def approve_flow(project: str, body: dict):
    identity = body.get("identity")
    if not identity:
        raise HTTPException(400, "identity is required")
    runs_module.set_approved(project, identity, bool(body.get("approved", True)))
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
