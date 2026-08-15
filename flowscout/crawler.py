"""DFS crawler with reset+replay backtracking.

Design notes (why it's built this way, not the naive way):

* Backtracking is done by *replaying the recorded action path from a
  fresh browser context* -- not browser-back, and not just re-navigating
  an existing page. Browser history is unreliable once forms/SPAs are
  involved; and reusing one context across branches lets storage state
  (cart contents, tokens, anything in localStorage/cookies) leak between
  unrelated flows, which silently corrupts fingerprints and produces
  false "the app is non-deterministic" symptoms. A brand-new context per
  path execution is slower but genuinely deterministic and auditable --
  every flow is something we can replay in isolation and get the same
  answer.
* The DFS is explicit (a stack of frames), not recursive, so a frame
  can be re-entered after a full reset without fighting Python's call
  stack / Playwright's page lifecycle.
* Every candidate is risk-classified *before* it is ever clicked
  (see risk.py). Destructive candidates are never followed; mutating
  ones are followed only if the config opts in.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .actions import discover_candidates, perform_action, current_domain, describe_action
from .fingerprint import normalize_url, state_fingerprint
from .models import (
    Checkpoint, ElementCandidate, Flow, FlowStatus, Risk, RunResult, StateNode, Transition,
)
from .semantic_dedup import DEFAULT_THRESHOLD, apply_semantic_dedup


@dataclass
class _Frame:
    fp: str
    path: list[Transition] = field(default_factory=list)
    order: list[int] = field(default_factory=list)   # indices into node.candidates, priority-capped
    pos: int = 0
    any_followed: bool = False
    any_risk_skipped: bool = False
    any_repeat_skipped: bool = False


def _discover_state(page, allowed_domains, run: RunResult
                     ) -> tuple[str, str, str, list[ElementCandidate], list[dict], list[dict]]:
    url_pattern = normalize_url(page.url)
    title = page.title()
    domain = current_domain(page.url)
    exclude_patterns = run.config.get("exclude_patterns", [])
    candidates, occluded, unclassified, disabled = discover_candidates(
        page, domain, allowed_domains, exclude_patterns)
    fp = state_fingerprint(url_pattern, [c.signature for c in candidates])
    for o in occluded:
        run.skipped_candidates.append({
            "state_fp": fp, "label": o["label"], "reason": o["reason"], "risk": "n/a",
        })
    return fp, url_pattern, title, candidates, unclassified, disabled


def _run_path(browser, config, path: list[Transition], run: RunResult, credentials: dict):
    """Execute `path` from a fresh, isolated browser context (fresh
    cookies/localStorage -- no leakage between DFS branches). Returns
    (fp, url_pattern, title, candidates, last_fill_summary, unclassified,
    disabled, last_choice_state) for the state reached after the last
    step, or None if some step failed (an error checkpoint is recorded,
    pointing at which step). `last_fill_summary` is whatever
    perform_action returned for the *final* step of `path` -- None if
    that step wasn't a form submission, else the field:value summary
    used to build a readable "Fill form and submit ... (...)" label
    (e.g. which login account was used). `last_choice_state` is the
    radio/checkbox state observed alongside it (see
    actions._read_choice_state) -- label-only, kept separate so it never
    ends up in Transition.form_fields (M4 codegen would try to `.fill()`
    a checkbox otherwise).
    `credentials` is passed in rather than read from `config` directly
    so each persona's own pass (see crawl()) can supply its own -- the
    only thing that actually differs between two personas walking the
    same replay path."""
    context = browser.new_context()
    page = context.new_page()
    last_fill_summary = None
    last_choice_state: dict = {}
    try:
        page.goto(config["start_url"], wait_until="load")
        page.wait_for_timeout(200)
        for i, t in enumerate(path):
            el_meta = json.loads(t.replay_meta)
            try:
                fill_summary, choice_state = perform_action(page, el_meta, credentials)
                if i == len(path) - 1:
                    last_fill_summary = fill_summary
                    last_choice_state = choice_state
            except Exception as exc:
                where = "final step" if i == len(path) - 1 else f"replay step {i + 1}/{len(path)}"
                run.checkpoints.append(Checkpoint(
                    kind="error", flow_id=None, state_fp=t.from_fp,
                    message=f"Action '{t.action_label}' raised an error ({where})",
                    detail=str(exc)[:1200],
                ))
                return None
        fp, url_pattern, title, candidates, unclassified, disabled = _discover_state(
            page, config["allowed_domains"], run)
        return fp, url_pattern, title, candidates, last_fill_summary, unclassified, disabled, last_choice_state
    finally:
        context.close()


def _order_for(node: StateNode, max_breadth: int, run: RunResult, revisit_history: set[str]) -> list[int]:
    """`revisit_history`: norm_signatures already confirmed, earlier in
    THIS persona's own pass, to lead to a state already in run.states --
    see crawl()'s own comment at the revisit branch for how it's built.
    A stable sort moves known-revisit signatures to the back before
    breadth truncation runs, so a forced cut preferentially drops actions
    already confirmed to lead nowhere new, not ones that might. Learned
    live, not guessed from labels: measured on a real saucedemo run
    (before this existed) that the actual revisit-producing signatures
    are dominated by ordinary mutating actions converging on a shared end
    state -- add-to-cart, checkout, remove, cancel -- not UI-chrome
    toggles a label heuristic (open/close, expand/collapse) would catch;
    that heuristic was considered and dropped for exactly this reason,
    on top of already being a language-dependent guess (the same class
    of bug field_detect.py's login-trigger matching hit earlier this
    project, fixed by matching structure instead of English text).

    `is_choice` candidates are exempt from this deprioritization no
    matter what revisit_history says -- caught live, not assumed safe:
    picking a `<select>` option changes display order, not the candidate
    *set*, so state_fingerprint() (deliberately) doesn't change and
    EVERY select/radio/checkbox choice reads as a "revisit" the very
    first time any one of them is tried anywhere in the run, regardless
    of which option. Without this exemption, that flags the whole choice
    group as a revisit-producer almost immediately and buries it under
    max_breadth_per_state everywhere else -- confirmed on saucedemo's own
    sort dropdown (TC-10 in the M2 gap-analysis calibration, a real,
    valued capability): found via breadth=20 but silently missing at the
    realistic default breadth=10, the exact regression the is_choice
    mechanism exists to prevent elsewhere (gap_analysis.py, shared_steps.
    py, testcase_draft.py all already special-case it for the same
    reason -- this file just hadn't caught up yet)."""
    idxs = list(range(len(node.candidates)))
    idxs.sort(key=lambda i: not node.candidates[i].is_choice and node.candidates[i].norm_signature in revisit_history)
    if len(idxs) > max_breadth:
        overflow = idxs[max_breadth:]
        for i in overflow:
            c = node.candidates[i]
            run.skipped_candidates.append({
                "state_fp": node.fingerprint, "label": describe_action(json.loads(c.selector), None),
                "reason": "breadth limit exceeded", "risk": c.risk.value,
            })
        idxs = idxs[:max_breadth]
    return idxs


def crawl(config: dict) -> RunResult:
    from playwright.sync_api import sync_playwright

    limits = config["limits"]
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    # Not a required key like the four above -- existing configs/*.json
    # written before this existed don't have it, and a plain limits[...]
    # index would KeyError every one of them the moment the CLI loaded
    # the file (unlike the web UI's config.setdefault(), the CLI passes
    # the JSON straight through). See ROADMAP.md "Parked -- smart limits":
    # the direct fix for the actual combinatorial-growth case (an N-item
    # cart is up to 2^N reachable states) -- a third add-to-cart click
    # within one DFS path teaches the crawler nothing a second one didn't
    # already show, so 2 is the default: enough to see "one item" and
    # "two items" behavior, not enough to keep multiplying.
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = config.get("allow_mutating", True)
    allowed_domains = config.get("allowed_domains", [])

    # Multiple personas (named credential sets) walk the same config
    # sequentially into ONE RunResult -- one report, one change-report,
    # one CI exit code. `credentials` (singular) is still accepted as
    # shorthand for a single persona named "default", so every config
    # written before this existed keeps working unchanged.
    #
    # Sequential, not parallel -- deliberately (see ROADMAP.md "Multi-
    # persona crawling"): personas can corrupt each other's results
    # through shared server-side state (one persona's "Reset App State"
    # mid-crawl would silently invalidate whatever another persona was
    # mid-flow doing at that moment), the same class of cross-run
    # non-determinism M5 already had to document for Site B -- just
    # within a single run instead of across two.
    #
    # max_states/max_flows apply *per persona*, not to the run as a
    # whole -- each persona gets the same budget config would give it as
    # a standalone crawl, rather than later personas silently starving
    # because earlier ones used up a shared cap. States a later persona
    # reaches that an earlier one already discovered (the state graph is
    # shared and reused across personas -- see below) don't count against
    # its budget at all, only genuinely new ones do.
    personas = config.get("personas")
    if not personas:
        personas = [{"name": "default", "credentials": config.get("credentials", {})}]

    run = RunResult(project=config["project"], start_url=config["start_url"], config=config)
    run.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    next_flow_id = [1]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_fp: str | None = None

        for persona in personas:
            persona_name = persona.get("name", "default")
            credentials = persona.get("credentials", {})

            # Fresh per persona, not shared: a structural-dedup match
            # against another persona's identical-looking action sequence
            # is NOT the same flow (see identity.py's flow_identity) --
            # what a persona is *allowed* to do getting there is exactly
            # the thing multi-persona crawling exists to tell apart.
            seq_to_flow_id: dict[tuple, int] = {}

            # Same "fresh per persona" reasoning as seq_to_flow_id above:
            # a norm_signature converging on an already-known state for
            # one persona doesn't mean it will for another -- personas can
            # legitimately reach different states from the same action
            # (that's the whole point of multi-persona crawling). See
            # _order_for()'s own docstring for what this is used for.
            revisit_history: set[str] = set()

            def emit_flow(path: list[Transition], end_fp: str, forced_status: FlowStatus | None = None,
                          extra_reason: str = "", _seq=seq_to_flow_id, _persona=persona_name) -> Flow:
                seq = tuple(t.action_norm_signature for t in path)
                status = forced_status
                dup_of = None
                reason = extra_reason
                if status is None:
                    if seq in _seq:
                        status = FlowStatus.DUPLICATE
                        dup_of = _seq[seq]
                        reason = "Same normalized action sequence as flow #%d (structural dedup: same steps, different data)" % dup_of
                    else:
                        status = FlowStatus.UNIQUE
                        _seq[seq] = next_flow_id[0]
                        reason = extra_reason or "New normalized action sequence"
                flow = Flow(
                    id=next_flow_id[0], status=status, duplicate_of=dup_of, dedup_reason=reason,
                    transitions=list(path), end_state_fp=end_fp, persona=_persona,
                )
                next_flow_id[0] += 1
                run.flows.append(flow)
                return flow

            if root_fp is None:
                # Only the very first persona actually visits start_url --
                # an empty path never calls perform_action at all, so this
                # state provably never depends on credentials and is safe
                # (and cheaper) to reuse for every later persona rather
                # than re-discovering it once per persona.
                root = _run_path(browser, config, [], run, credentials)
                if root is None:
                    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    browser.close()
                    return run
                root_fp, root_url_pat, root_title, root_candidates, _, root_unclassified, root_disabled, _ = root
                run.states[root_fp] = StateNode(
                    fingerprint=root_fp, url_pattern=root_url_pat, raw_url=config["start_url"],
                    title=root_title, candidates=root_candidates,
                    unclassified_interactive=root_unclassified, disabled_interactive=root_disabled,
                )

            root_node = run.states[root_fp]
            root_frame = _Frame(fp=root_fp)
            root_frame.order = _order_for(root_node, max_breadth, run, revisit_history)
            stack: list[_Frame] = [root_frame]

            states_before = len(run.states)
            flows_before = len(run.flows)

            while stack:
                if len(run.flows) - flows_before >= max_flows:
                    # Silent truncation, until Aug 2026: this used to just
                    # `break`, abandoning every remaining stack frame with no
                    # record anywhere -- no blocked flow, no skipped
                    # candidate, no checkpoint. A run that stopped here looked
                    # byte-for-byte like a run that finished naturally.
                    # Found while measuring depth budgets: raising max_depth
                    # past 14 changed nothing on saucedemo, because max_flows
                    # (not depth) had silently become the binding constraint
                    # and nothing said so. Same fix as max_depth truncation
                    # above -- a checkpoint (not a flow, since the abandoned
                    # frames aren't paths anyone walked) naming exactly how
                    # much was left unexplored.
                    unexplored_states = len(stack)
                    unexplored_actions = sum(len(f.order) - f.pos for f in stack)
                    run.checkpoints.append(Checkpoint(
                        kind="blocked", flow_id=None, state_fp=None,
                        message=f"Crawl stopped early: max_flows limit ({max_flows}) reached"
                                + (f" for persona '{persona_name}'" if len(personas) > 1 else ""),
                        detail=f"{unexplored_states} state(s) were still queued for exploration, with "
                               f"{unexplored_actions} candidate action(s) never tried. Raise max_flows to "
                               f"continue past this point -- the flows reported here are a prefix of what "
                               f"this config would eventually find, not the complete picture.",
                    ))
                    break
                frame = stack[-1]
                node = run.states[frame.fp]

                if frame.pos >= len(frame.order) or len(frame.path) >= max_depth:
                    # Two genuinely different situations used to collapse into
                    # one -- "ran out of things to try" (frame.pos exhausted,
                    # a real dead end) and "there were more candidates, but
                    # max_depth was hit before trying them" (budget, not a
                    # dead end) produced the exact same flow status and the
                    # exact same generic "New normalized action sequence"
                    # reason, with nothing anywhere recording which one
                    # happened. A user had no way to tell "this flow is
                    # complete" from "this flow was cut short and might have
                    # continued" -- found by a user asking exactly that
                    # question about a real run. depth_truncated distinguishes
                    # them the same way max_states truncation already does
                    # below: forced BLOCKED status, an explicit "Truncated"
                    # reason, and the untried candidates recorded in
                    # skipped_candidates so the report's Safety register shows
                    # precisely what was never even attempted, not just that
                    # something was.
                    depth_truncated = len(frame.path) >= max_depth and frame.pos < len(frame.order)
                    if frame.path:
                        if depth_truncated:
                            remaining = frame.order[frame.pos:]
                            for i in remaining:
                                c = node.candidates[i]
                                run.skipped_candidates.append({
                                    "state_fp": frame.fp,
                                    "label": describe_action(json.loads(c.selector), None),
                                    "reason": "max_depth limit reached", "risk": c.risk.value,
                                })
                            emit_flow(frame.path, frame.fp, forced_status=FlowStatus.BLOCKED,
                                      extra_reason=f"Truncated: max_depth limit reached with "
                                                   f"{len(remaining)} further action(s) available from here, "
                                                   f"never tried")
                        elif not frame.any_followed and (frame.any_risk_skipped or frame.any_repeat_skipped):
                            # Same "name what actually happened" discipline as
                            # depth/max_flows truncation above -- a dead end
                            # reached only because policy withheld every
                            # remaining action reads identically to a genuine
                            # dead end unless the reason is spelled out, and
                            # the two withholding reasons (risk gating vs. the
                            # repeat-action cap) are independent enough that a
                            # frame can hit either, or both, at once.
                            withheld_by = []
                            if frame.any_risk_skipped:
                                withheld_by.append("risk policy (destructive, or mutating with allow_mutating=false)")
                            if frame.any_repeat_skipped:
                                withheld_by.append(f"the action-repeat cap (max_action_repeat={max_action_repeat})")
                            emit_flow(frame.path, frame.fp, forced_status=FlowStatus.BLOCKED,
                                      extra_reason="Dead end: remaining actions were withheld by "
                                                   + " and ".join(withheld_by))
                        else:
                            emit_flow(frame.path, frame.fp)
                    stack.pop()
                    continue

                cand_idx = frame.order[frame.pos]
                frame.pos += 1
                candidate = node.candidates[cand_idx]

                if candidate.risk == Risk.DESTRUCTIVE:
                    frame.any_risk_skipped = True
                    run.skipped_candidates.append({
                        "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                        "reason": candidate.risk_reason, "risk": "destructive",
                    })
                    continue
                if candidate.risk == Risk.MUTATING and not allow_mutating:
                    frame.any_risk_skipped = True
                    run.skipped_candidates.append({
                        "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                        "reason": "mutating action withheld (allow_mutating=false)", "risk": "mutating",
                    })
                    continue

                # Repeat-action cap: how many times has THIS normalized action
                # already been performed earlier in this same path (not
                # per-state -- across the whole walk from root)? Targets the
                # actual combinatorial-growth case directly: repeatedly
                # clicking "add-to-cart-*" on different products all
                # normalize to the same signature (known_prefixes in
                # fingerprint.py), and each one opens a genuinely new state
                # (the cart's own candidate list includes the item), so
                # nothing else already caps this growth at its source --
                # max_depth only bounds it indirectly, by being high enough
                # to *tolerate* the blow-up before reaching anything past
                # it. is_choice actions (select/radio/checkbox) are
                # unaffected in practice: their norm_signature is kept
                # maximally distinct per option specifically so it's never
                # generalized (see fingerprint.py's "choice-" early return),
                # so the same one only ever repeats if a path genuinely
                # revisits the identical option, which this cap correctly
                # still allows twice before withholding.
                repeat_count = sum(1 for t in frame.path if t.action_norm_signature == candidate.norm_signature)
                if repeat_count >= max_action_repeat:
                    frame.any_repeat_skipped = True
                    run.skipped_candidates.append({
                        "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                        "reason": f"action-repeat cap reached ({repeat_count}x '{candidate.norm_signature}' "
                                  f"already performed earlier in this path)",
                        "risk": candidate.risk.value,
                    })
                    continue

                el_meta = json.loads(candidate.selector)
                trial = Transition(from_fp=frame.fp, to_fp=None, action_label=describe_action(el_meta, None),
                                    action_norm_signature=candidate.norm_signature, risk=candidate.risk,
                                    risk_reason=candidate.risk_reason, replay_meta=candidate.selector,
                                    is_choice=candidate.is_choice)
                result = _run_path(browser, config, frame.path + [trial], run, credentials)
                frame.any_followed = True

                if result is None:
                    emit_flow(frame.path + [trial], end_fp=frame.fp, forced_status=FlowStatus.BLOCKED,
                              extra_reason=f"Terminated: action '{trial.action_label}' raised an error")
                    continue

                new_fp, url_pat, title, new_candidates, fill_summary, new_unclassified, new_disabled, choice_state = result
                # choice_state (radio/checkbox selections observed at submit time) is
                # merged into the label for human/gap-analysis visibility only -- it
                # must never reach trial.form_fields, since M4's codegen turns that
                # into .fill() calls and .fill() raises on a radio/checkbox input.
                label_fields = {**(fill_summary or {}), **(choice_state or {})}
                trial.action_label = describe_action(el_meta, label_fields or None)
                if fill_summary:
                    trial.form_fields = list(fill_summary.keys())
                trial.to_fp = new_fp
                trial.outcome = "revisit" if new_fp in run.states else "ok"
                new_path = frame.path + [trial]

                if new_fp in run.states:
                    # Learned live, for _order_for()'s benefit on every node
                    # discovered from here on in this persona's pass: this
                    # exact action, taken from this exact state, produced no
                    # new information. Recorded by norm_signature (not tied
                    # to this one state) because the same signature reaching
                    # an already-known state once is real evidence it's
                    # likely to again -- confirmed on real data before this
                    # existed (add-to-cart/checkout/remove/cancel all showed
                    # up as revisit-producers, not just UI-chrome toggles).
                    revisit_history.add(candidate.norm_signature)
                    emit_flow(new_path, end_fp=new_fp)
                    continue

                if len(run.states) - states_before >= max_states:
                    run.skipped_candidates.append({
                        "state_fp": frame.fp, "label": trial.action_label,
                        "reason": "max_states limit reached", "risk": candidate.risk.value,
                    })
                    emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                              extra_reason="Truncated: max_states limit reached before this state could be explored")
                    continue

                new_node = StateNode(fingerprint=new_fp, url_pattern=url_pat, raw_url="",
                                      title=title, candidates=new_candidates,
                                      discovered_by_flow=next_flow_id[0],
                                      unclassified_interactive=new_unclassified,
                                      disabled_interactive=new_disabled)
                run.states[new_fp] = new_node
                child = _Frame(fp=new_fp, path=new_path)
                child.order = _order_for(new_node, max_breadth, run, revisit_history)
                stack.append(child)

        browser.close()

    sem_cfg = config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down a completed crawl
            run.semantic_dedup_status = f"error: {exc}"
    else:
        run.semantic_dedup_status = "skipped: disabled in config"

    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return run
