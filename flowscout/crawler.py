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
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlsplit

from .actions import discover_candidates, perform_action, current_domain, describe_action
from .fingerprint import normalize_url, state_fingerprint
from .models import (
    Checkpoint, ElementCandidate, Flow, FlowStatus, Risk, RunResult, StateNode, Transition,
)
from .semantic_dedup import DEFAULT_THRESHOLD, apply_semantic_dedup


def _local_tag(tag: str) -> str:
    """Strips a namespace prefix off an XML tag ('{http://...}loc' ->
    'loc') -- sitemap.xml is supposed to declare the sitemaps.org
    namespace, but real-world sitemaps generating tools sometimes omit
    it. Matching on the local name only handles both without needing
    two separate code paths."""
    return tag.rsplit("}", 1)[-1]


def _fetch_sitemap_urls(sitemap_url: str, _depth: int = 0, _max_depth: int = 2) -> list[str]:
    """Fetches and parses a sitemap.xml: either a plain <urlset> of
    <url><loc> page entries, or a <sitemapindex> of <sitemap><loc>
    entries each pointing at a CHILD sitemap (the standard way a large
    site splits its sitemap into per-section files) -- recursed up to
    `_max_depth` levels. Stdlib-only (urllib.request + xml.etree),
    matching this project's existing convention (see embeddings.py's
    own urllib.request usage) rather than adding a new HTTP dependency
    for this one feature. Raises on a genuinely unreachable/malformed
    sitemap -- the caller (_resolve_seed_urls) is the one that decides
    that's non-fatal to the crawl as a whole and records why."""
    with urllib.request.urlopen(sitemap_url, timeout=10) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    locs = [el.text.strip() for el in root.iter() if _local_tag(el.tag) == "loc" and el.text]
    if _local_tag(root.tag) == "sitemapindex" and _depth < _max_depth:
        urls: list[str] = []
        for child_sitemap in locs:
            try:
                urls.extend(_fetch_sitemap_urls(child_sitemap, _depth + 1, _max_depth))
            except Exception:
                continue  # one broken child sitemap shouldn't sink the whole resolution
        return urls
    return locs


def _resolve_seed_urls(config: dict, allowed_domains: list[str], run: RunResult) -> list[str]:
    """Direct-URL seeding (Sep 2026): the crawler otherwise only ever
    finds what it can DFS its way to by clicking from start_url -- a
    page with no inbound link anywhere in the crawled UI (a deep-linked
    SPA route, an old promo landing page still live but delisted from
    navigation) is structurally unreachable no matter how thoroughly
    the rest of the app is explored. `config["seed_urls"]` (an explicit
    list) and/or `config["sitemap_url"]` (fetched and parsed
    automatically) name known URLs to treat as ADDITIONAL entry points,
    each explored with the crawler's full normal DFS machinery from
    there onward -- not just visited and left alone.

    Filtered the same way ordinary candidate hrefs already are (see
    risk.classify): dropped if its domain isn't in `allowed_domains`
    (an external sitemap entry is exactly as out-of-scope as an
    external link would be) or its path matches an exclude_pattern.
    Also dropped if it normalizes to the same state as start_url
    itself -- nothing new to seed there. Capped at
    `config.get("max_seed_urls", 50)`, applied AFTER filtering, since a
    real sitemap can easily list thousands of URLs and this crawler's
    per-seed exploration cost is the same as crawling an entire extra
    site; the operator raises the cap deliberately, not by accident.

    A sitemap fetch/parse failure is recorded as a checkpoint (not
    raised) -- seeding is additive to an otherwise-normal crawl, so one
    unreachable/malformed sitemap shouldn't take down the whole run,
    the same "degrade, don't abort" principle skipped_candidates and
    every other soft-failure path in this project already follows."""
    urls: list[str] = list(config.get("seed_urls", []))
    sitemap_url = config.get("sitemap_url")
    if sitemap_url:
        try:
            urls.extend(_fetch_sitemap_urls(sitemap_url))
        except Exception as exc:
            run.checkpoints.append(Checkpoint(
                kind="blocked", flow_id=None, state_fp=None,
                message="Could not fetch/parse sitemap_url -- direct-URL seeding skipped for it",
                detail=str(exc)[:500],
            ))

    exclude_patterns = config.get("exclude_patterns", [])
    start_domain = current_domain(config["start_url"])
    start_norm = normalize_url(config["start_url"])
    max_seed_urls = config.get("max_seed_urls", 50)

    filtered: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        if normalize_url(u) == start_norm:
            continue
        domain = current_domain(u)
        if domain and domain not in allowed_domains and domain != start_domain:
            continue
        path = urlsplit(u).path or u
        if any(fnmatch(path, p) for p in exclude_patterns):
            continue
        filtered.append(u)
        if len(filtered) >= max_seed_urls:
            break
    return filtered


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
                     ) -> tuple[str, str, str, list[ElementCandidate], list[dict], list[dict], list[str], list[str]]:
    url_pattern = normalize_url(page.url)
    title = page.title()
    domain = current_domain(page.url)
    exclude_patterns = run.config.get("exclude_patterns", [])
    candidates, occluded, unclassified, disabled, validation_signals, captcha_signals = discover_candidates(
        page, domain, allowed_domains, exclude_patterns)
    # validation_signals folded into the fingerprint itself (not just
    # reported afterward, like dialog_message/response_status are) --
    # see actions.py's discover_candidates docstring and Transition.
    # validation_errors for why: without this, a rejected-submission
    # state is byte-for-byte identical (by url+candidate-set) to its own
    # pre-submit state, so the crawler read it as a plain revisit and
    # never explored past it. captcha_signals is deliberately NOT folded
    # in here -- see Transition.captcha_detected's own docstring.
    fp = state_fingerprint(url_pattern, [c.signature for c in candidates] + validation_signals)
    for o in occluded:
        run.skipped_candidates.append({
            "state_fp": fp, "label": o["label"], "reason": o["reason"], "risk": "n/a",
        })
    return fp, url_pattern, title, candidates, unclassified, disabled, validation_signals, captcha_signals


def _run_path(browser, config, path: list[Transition], run: RunResult, credentials: dict):
    """Execute `path` from a fresh, isolated browser context (fresh
    cookies/localStorage -- no leakage between DFS branches). Returns
    (fp, url_pattern, title, candidates, last_fill_summary, unclassified,
    disabled, last_choice_state, last_response_status,
    last_dialog_message, last_opened_new_page, validation_errors,
    captcha_signals) for the state reached after the last step, or None
    if some step failed (an error checkpoint is recorded, pointing at
    which step). `validation_errors`/`captcha_signals` describe the
    state reached after the LAST step (see _discover_state/
    Transition.validation_errors/captcha_detected) -- unlike the other
    `last_*` fields below, they describe the state itself, not the
    action that produced it, so they aren't specific to "the final step
    of `path`" in the same way; they're simply whatever _discover_state()
    found there. `last_fill_summary`
    is whatever perform_action returned for the *final* step of `path`
    -- None if that step wasn't a form submission, else the field:value
    summary used to build a readable "Fill form and submit ... (...)"
    label (e.g. which login account was used). `last_choice_state` is
    the radio/checkbox state observed alongside it (see
    actions._read_choice_state) -- label-only, kept separate so it never
    ends up in Transition.form_fields (M4 codegen would try to `.fill()`
    a checkbox otherwise). `last_response_status` is the main-document
    HTTP status of whatever navigation the final step actually caused
    (see actions._capture_nav_status) -- None if it didn't cause one.
    `last_dialog_message`/`last_opened_new_page` are the same
    observational pass-through of actions.perform_action's own dialog/
    new-page capture (see its own docstring) for the final step.
    `credentials` is passed in rather than read from `config` directly
    so each persona's own pass (see crawl()) can supply its own -- the
    only thing that actually differs between two personas walking the
    same replay path."""
    context = browser.new_context()
    page = context.new_page()
    last_fill_summary = None
    last_choice_state: dict = {}
    last_response_status: int | None = None
    last_dialog_message = ""
    last_opened_new_page: str | None = None
    # Not a required config key -- existing configs/*.json written before
    # this existed don't have it, same reasoning as max_action_repeat
    # above. Found necessary on a real production site (alternateqa.com,
    # not a local fixture): the previous hard-coded 8000ms wasn't always
    # enough, and there was no way to raise it short of patching code.
    action_timeout_ms = config.get("limits", {}).get("action_timeout_ms", 8000)
    try:
        page.goto(config["start_url"], wait_until="load")
        page.wait_for_timeout(200)
        for i, t in enumerate(path):
            el_meta = json.loads(t.replay_meta)
            try:
                fill_summary, choice_state, response_status, dialog_message, opened_new_page = perform_action(
                    page, el_meta, credentials, timeout_ms=action_timeout_ms)
                if i == len(path) - 1:
                    last_fill_summary = fill_summary
                    last_choice_state = choice_state
                    last_response_status = response_status
                    last_dialog_message = dialog_message
                    last_opened_new_page = opened_new_page
            except Exception as exc:
                where = "final step" if i == len(path) - 1 else f"replay step {i + 1}/{len(path)}"
                run.checkpoints.append(Checkpoint(
                    kind="error", flow_id=None, state_fp=t.from_fp,
                    message=f"Action '{t.action_label}' raised an error ({where})",
                    detail=str(exc)[:1200],
                ))
                return None
        (fp, url_pattern, title, candidates, unclassified, disabled, validation_signals,
         captcha_signals) = _discover_state(page, config["allowed_domains"], run)
        validation_errors = "; ".join(validation_signals)
        captcha_detected = "; ".join(captcha_signals)
        return (fp, url_pattern, title, candidates, last_fill_summary, unclassified, disabled,
                last_choice_state, last_response_status, last_dialog_message, last_opened_new_page,
                validation_errors, captcha_detected)
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


def _run_dfs(browser, config: dict, run: RunResult, credentials: dict, persona_name: str,
             stack: list[_Frame], next_flow_id: list[int],
             max_depth: int, max_breadth: int, max_states: int, max_flows: int,
             max_action_repeat: int, allow_mutating: bool,
             states_before: int, flows_before: int, show_persona_suffix: bool,
             seq_to_flow_id: dict[tuple, int], revisit_history: set[str],
             origin_note: str = "") -> None:
    """The DFS loop itself -- extracted (Aug 2026) so crawl()'s own
    per-persona pass and resume_flow()'s targeted continuation from a
    single already-BLOCKED flow can share it instead of duplicating the
    loop. Mutates `run` in place (states/flows/checkpoints/
    skipped_candidates); `stack` is drained in place, LIFO, exactly like
    before this was split out -- this function doesn't care whether it
    was seeded with one fresh root frame or one frame resuming a
    specific blocked path, the algorithm is identical either way, only
    the starting point differs.

    `states_before`/`flows_before`: baseline the max_states/max_flows
    budgets count *up from* -- computed fresh at call time by the
    caller, so a resumed continuation gets its own full budget starting
    at 0, not the remainder of whatever the original pass had already
    spent.

    `origin_note`: stamped onto every Flow this call emits (see
    Flow.origin_note's own docstring) -- empty for crawl()'s own normal
    pass, a short note identifying which operator action produced these
    flows for resume_flow()/explore_combination()'s calls."""
    def emit_flow(path: list[Transition], end_fp: str, forced_status: FlowStatus | None = None,
                  extra_reason: str = "", resumable: bool = False) -> Flow:
        seq = tuple(t.action_norm_signature for t in path)
        status = forced_status
        dup_of = None
        reason = extra_reason
        if status is None:
            if seq in seq_to_flow_id:
                status = FlowStatus.DUPLICATE
                dup_of = seq_to_flow_id[seq]
                reason = "Same normalized action sequence as flow #%d (structural dedup: same steps, different data)" % dup_of
            else:
                status = FlowStatus.UNIQUE
                seq_to_flow_id[seq] = next_flow_id[0]
                reason = extra_reason or "New normalized action sequence"
        flow = Flow(
            id=next_flow_id[0], status=status, duplicate_of=dup_of, dedup_reason=reason,
            transitions=list(path), end_state_fp=end_fp, persona=persona_name,
            # Only meaningful (and only ever True) for a forced BLOCKED
            # status -- a flow that completed normally or deduped has
            # nothing to "resume". See Flow.resumable's own docstring
            # for which BLOCKED reasons qualify and why.
            resumable=resumable and status == FlowStatus.BLOCKED,
            origin_note=origin_note,
        )
        next_flow_id[0] += 1
        run.flows.append(flow)
        return flow

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
                        + (f" for persona '{persona_name}'" if show_persona_suffix else ""),
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
                                           f"never tried",
                              resumable=True)
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
                                           + " and ".join(withheld_by),
                              resumable=True)
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
                            is_choice=candidate.is_choice,
                            anchor_target_missing=candidate.anchor_target_missing)
        result = _run_path(browser, config, frame.path + [trial], run, credentials)
        frame.any_followed = True

        if result is None:
            # Found while building gap_analysis.py's action-pool
            # broadening (Aug 2026): trial.outcome was never set
            # here at all, silently staying at its dataclass
            # default "ok" -- meaning report.py's own `elif
            # t.outcome == "error":` rendering (a red step-error
            # note with the exception detail) was dead code, and
            # a flow's failed final step rendered identically to
            # a normal successful one. _run_path() always appends
            # exactly one Checkpoint right before returning None
            # (its only return-None path), so this is always the
            # matching detail, not a guess.
            trial.outcome = "error"
            trial.detail = run.checkpoints[-1].detail if run.checkpoints else ""
            emit_flow(frame.path + [trial], end_fp=frame.fp, forced_status=FlowStatus.BLOCKED,
                      extra_reason=f"Terminated: action '{trial.action_label}' raised an error")
            continue

        (new_fp, url_pat, title, new_candidates, fill_summary, new_unclassified, new_disabled,
         choice_state, response_status, dialog_message, opened_new_page, validation_errors,
         captcha_detected) = result
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
        trial.response_status = response_status
        trial.dialog_message = dialog_message
        trial.opened_new_page = opened_new_page
        trial.validation_errors = validation_errors
        trial.captcha_detected = captcha_detected
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
            if captcha_detected:
                # Found live, not assumed: a DIFFERENT action path can
                # reach the SAME captcha state that some earlier branch
                # already discovered (e.g. a normal click lands on the
                # exact page a seed URL was ALSO seeded at) -- captcha_
                # detected here is _discover_state()'s own fresh re-check
                # for THIS replay, not a cached value, so it's just as
                # reliable as the first-discovery case above. Without
                # this, only the flow that happened to discover the
                # state FIRST got marked blocked; every later one reading
                # the exact same challenge page read as an ordinary,
                # unremarkable "unique"/"duplicate" flow instead.
                trial.outcome = "blocked"
                emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                          extra_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) "
                                       f"-- never explored further")
            else:
                emit_flow(new_path, end_fp=new_fp)
            continue

        if captcha_detected:
            # A genuinely NEW state, but one behind a CAPTCHA/challenge
            # marker (see actions.py's discover_candidates docstring) --
            # recorded (unlike the max_states-truncation branch below,
            # this state IS worth keeping as evidence of exactly where
            # the crawl got challenged) but never explored further: there
            # is nothing legitimate to click on a challenge page, and
            # trying would risk interacting with the CAPTCHA widget
            # itself rather than the app under test. trial.outcome was
            # set to "ok"/"revisit" above -- overwritten here since
            # neither is accurate for what actually happened.
            trial.outcome = "blocked"
            run.states[new_fp] = StateNode(
                fingerprint=new_fp, url_pattern=url_pat, raw_url="", title=title,
                candidates=new_candidates, discovered_by_flow=next_flow_id[0],
                unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
                captcha_detected=captcha_detected,
            )
            emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                      extra_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) "
                                   f"-- never explored further")
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

    # Direct-URL seeding (see _resolve_seed_urls's own docstring) --
    # resolved once up front, before the browser even launches, same as
    # every other config-level decision (limits, personas) above.
    seed_urls = _resolve_seed_urls(config, allowed_domains, run)

    next_flow_id = [1]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_fp: str | None = None
        # Discovered once (by the first persona), reused by every later
        # one -- same reasoning as root_fp: a direct-nav transition
        # ignores `credentials` entirely (see actions.py's perform_action,
        # tag == "direct-nav"), so what a seed URL shows depends only on
        # the URL itself, never on which persona is walking, exactly like
        # the empty-path root state above.
        seed_fps: dict[str, tuple[str, str]] = {}  # url -> (fingerprint, captcha_detected)

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
                (root_fp, root_url_pat, root_title, root_candidates, _, root_unclassified,
                 root_disabled, _, _, _, _, _, root_captcha) = root
                run.states[root_fp] = StateNode(
                    fingerprint=root_fp, url_pattern=root_url_pat, raw_url=config["start_url"],
                    title=root_title, candidates=root_candidates,
                    unclassified_interactive=root_unclassified, disabled_interactive=root_disabled,
                    captcha_detected=root_captcha,
                )
                if root_captcha:
                    # start_url itself is behind a CAPTCHA/challenge --
                    # there's no Transition/Flow to attach this to (an
                    # empty path never goes through emit_flow, same
                    # reasoning _path_to_state's own docstring gives for
                    # the root state), so this is a checkpoint, not a
                    # blocked flow. Nothing past here is explorable for
                    # ANY persona (root is shared across all of them), so
                    # stop the crawl entirely rather than let every
                    # persona separately "explore" a challenge page's own
                    # incidental candidates.
                    run.checkpoints.append(Checkpoint(
                        kind="blocked", flow_id=None, state_fp=root_fp,
                        message="start_url is itself behind a CAPTCHA/challenge page -- "
                                "crawl stopped, nothing else is explorable",
                        detail=root_captcha,
                    ))
                    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    browser.close()
                    return run

            root_node = run.states[root_fp]
            root_frame = _Frame(fp=root_fp)
            root_frame.order = _order_for(root_node, max_breadth, run, revisit_history)
            stack: list[_Frame] = [root_frame]

            # One additional root-like frame per seed URL -- each starts
            # its own full DFS exploration from wherever that URL lands,
            # exactly like root_frame does from start_url. Built fresh
            # per persona (a fresh Transition instance each time, not
            # shared/mutated across personas) since to_fp/response_status/
            # etc. get written onto it below and a later persona must not
            # see an earlier persona's own values there.
            for seed_url in seed_urls:
                # "text" is just for a shorter, readable label (the path,
                # not the whole absolute URL) -- perform_action's own
                # direct-nav replay only ever reads "href".
                seed_el_meta = {"tag": "direct-nav", "href": seed_url,
                                 "text": urlsplit(seed_url).path or seed_url}
                seed_trial = Transition(
                    from_fp="", to_fp=None,
                    action_label=describe_action(seed_el_meta, None),
                    action_norm_signature=f"direct-nav:{normalize_url(seed_url)}",
                    risk=Risk.SAFE, risk_reason="operator-specified seed URL",
                    replay_meta=json.dumps(seed_el_meta),
                )
                if seed_url not in seed_fps:
                    seed_result = _run_path(browser, config, [seed_trial], run, credentials)
                    if seed_result is None:
                        # Already recorded as a checkpoint by _run_path
                        # itself (an error, not a warning) -- this seed
                        # URL just isn't explorable, nothing more to do.
                        continue
                    (seed_fp, seed_url_pat, seed_title, seed_candidates, _, seed_unclassified,
                     seed_disabled, _, seed_status, seed_dialog, seed_new_page, seed_validation,
                     seed_captcha) = seed_result
                    seed_trial.response_status = seed_status
                    seed_trial.dialog_message = seed_dialog
                    seed_trial.opened_new_page = seed_new_page
                    seed_trial.validation_errors = seed_validation
                    if seed_fp not in run.states:
                        run.states[seed_fp] = StateNode(
                            fingerprint=seed_fp, url_pattern=seed_url_pat, raw_url=seed_url,
                            title=seed_title, candidates=seed_candidates,
                            unclassified_interactive=seed_unclassified, disabled_interactive=seed_disabled,
                            captcha_detected=seed_captcha,
                        )
                    # Cached as (fp, captcha) together, not just fp --
                    # otherwise a LATER persona reusing this seed_url
                    # would skip straight to pushing an ordinary frame
                    # for it, silently losing the captcha finding this
                    # first persona already made.
                    seed_fps[seed_url] = (seed_fp, seed_captcha)

                seed_fp, seed_captcha = seed_fps[seed_url]
                seed_trial.to_fp = seed_fp
                seed_trial.captcha_detected = seed_captcha
                if seed_captcha:
                    # Same reasoning as the main DFS loop's own captcha
                    # branch: a real Flow here (not just a checkpoint),
                    # since a seed URL's own arrival -- unlike root's --
                    # already has a non-empty path/action_label worth
                    # reporting as its own "Blocked" card. Built directly
                    # rather than through _run_dfs's own emit_flow closure
                    # (not in scope here) -- same pattern
                    # explore_combination() already uses for its own
                    # one-off Flow construction outside that closure.
                    seed_trial.outcome = "blocked"
                    run.flows.append(Flow(
                        id=next_flow_id[0], status=FlowStatus.BLOCKED, duplicate_of=None,
                        dedup_reason=f"Blocked by a CAPTCHA/challenge page ({seed_captcha}) "
                                     f"-- never explored further",
                        transitions=[seed_trial], end_state_fp=seed_fp, persona=persona_name,
                    ))
                    next_flow_id[0] += 1
                    continue
                seed_node = run.states[seed_fp]
                seed_frame = _Frame(fp=seed_fp, path=[seed_trial])
                seed_frame.order = _order_for(seed_node, max_breadth, run, revisit_history)
                stack.append(seed_frame)

            states_before = len(run.states)
            flows_before = len(run.flows)

            _run_dfs(browser, config, run, credentials, persona_name, stack, next_flow_id,
                     max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                     states_before, flows_before, len(personas) > 1,
                     seq_to_flow_id, revisit_history)

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


def resume_flow(run: RunResult, flow: Flow, limit_overrides: dict, credentials: dict,
                 run_semantic_dedup: bool = True) -> None:
    """Continue exploring from where one specific BLOCKED flow left off,
    instead of re-crawling the whole config from start_url. Mutates
    `run` in place -- new states/flows/checkpoints/skipped_candidates
    get appended, exactly as if the original DFS stack had kept going
    past this one flow's stopping point instead of stopping there.

    Only meaningful for `flow.resumable` flows -- see its own docstring
    for exactly which BLOCKED reasons qualify (max_depth truncation, or
    a dead end from risk-policy/repeat-cap withholding: both anchored to
    THIS flow's own path). max_states/max_flows truncation isn't tied to
    any one flow, so there's nothing here to resume for those -- the
    honest fix is a full re-crawl with a higher limit.

    `run_semantic_dedup` (Aug 2026): set False by resume_all_blocked_in_run's
    own batch loop -- semantic dedup re-embeds EVERY currently-unique
    flow from scratch on each call (see semantic_dedup.py, no caching
    across calls), so resuming 12 flows in a row with this left True
    would redundantly re-embed an ever-growing flow list up to 12 times
    in a row for one button click, wasting most of the free-tier
    embeddings quota on repeat work before ever reaching real analysis.
    Single-flow resume (the individual "Resume this flow" button) keeps
    the default True -- there's exactly one call there, nothing to
    batch up.

    `limit_overrides`: a partial `limits` dict (e.g. just
    `{"max_depth": 12}`) merged over the run's own original limits, plus
    an optional `"allow_mutating"` key (not nested under `limits` in the
    config schema -- see crawl() -- accepted the same way here, applied
    to a top-level override instead). Reuses the exact replay mechanism
    backtracking already relies on: `flow.transitions` already carries
    each step's `replay_meta`, and `run.states[flow.end_state_fp]` (from
    the original crawl) is reused as-is, not re-discovered -- the same
    "trust what's already known, don't re-earn it" reasoning
    `_run_path()`'s own replay-from-root already runs on for every DFS
    step, just anchored at a later point instead of the root."""
    if not flow.resumable:
        raise ValueError(f"flow #{flow.id} is not resumable (status={flow.status.value}: {flow.dedup_reason})")
    node = run.states.get(flow.end_state_fp)
    if node is None:
        raise ValueError(f"flow #{flow.id}'s end state is missing from this run's state graph")

    from playwright.sync_api import sync_playwright

    limits = {**run.config.get("limits", {}), **{k: v for k, v in limit_overrides.items() if k != "allow_mutating"}}
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = limit_overrides.get("allow_mutating", run.config.get("allow_mutating", True))

    # Local only -- run.config stays an honest record of how the
    # original crawl was actually configured; a resume's own
    # (potentially different) limits are never written back into it.
    config = {**run.config, "limits": limits, "allow_mutating": allow_mutating}

    next_flow_id = [max((f.id for f in run.flows), default=0) + 1]
    seq_to_flow_id: dict[tuple, int] = {}
    revisit_history: set[str] = set()

    flow_ids_before = {f.id for f in run.flows}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            frame = _Frame(fp=flow.end_state_fp, path=list(flow.transitions))
            frame.order = _order_for(node, max_breadth, run, revisit_history)
            stack: list[_Frame] = [frame]

            states_before = len(run.states)
            flows_before = len(run.flows)

            _run_dfs(browser, config, run, credentials, flow.persona, stack, next_flow_id,
                     max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                     states_before, flows_before, False, seq_to_flow_id, revisit_history,
                     origin_note=f"Resumed from flow #{flow.id}")
        finally:
            browser.close()

    # Reclassify the original blocked flow itself (Aug 2026), not just
    # append new ones alongside it -- asked directly after a live
    # report: a flow that's genuinely been continued past its own
    # truncation point staying marked BLOCKED forever, still counted in
    # "N blocked"/"Resume all" totals, read as if the resume had done
    # nothing even when it demonstrably had (see ROADMAP.md). Reuses
    # the EXISTING duplicate mechanism rather than inventing a new
    # status: a truncated 3-step prefix is genuinely redundant once a
    # fuller 5-step continuation exists, the same relationship
    # `_apply_state_convergence` already expresses for unrelated
    # reasons. `resumable=False` here is what actually drops it out of
    # future "N blocked"/"Resume all" counts (both read live off
    # run.flows, not a separate cached figure) -- only when THIS resume
    # produced at least one real result; a resume that itself
    # immediately re-blocked or errored leaves the original genuinely
    # still blocked, nothing to claim otherwise.
    new_flows = [f for f in run.flows if f.id not in flow_ids_before]
    if new_flows:
        best = max(new_flows, key=lambda f: len(f.transitions))
        ids_str = ", ".join(f"#{f.id}" for f in sorted(new_flows, key=lambda f: f.id))
        flow.status = FlowStatus.DUPLICATE
        flow.duplicate_of = best.id
        flow.resumable = False
        flow.dedup_reason = (
            f"Resumed and superseded: continuing further produced {len(new_flows)} new flow(s) "
            f"({ids_str}) -- this shorter, truncated path is redundant now that the fuller "
            f"exploration exists"
        )

    sem_cfg = run.config.get("semantic_dedup", {})
    if run_semantic_dedup and sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful resume
            run.semantic_dedup_status = f"error on resume: {exc}"


def _path_to_state(run: RunResult, state_fp: str) -> list[Transition]:
    """The sequence of already-walked transitions that reaches
    `state_fp`, derived from StateNode.discovered_by_flow rather than
    guessed or re-derived: the exact flow that first discovered this
    state necessarily has a transition landing on it somewhere along
    its OWN path (_run_dfs keeps extending the same path deeper before
    ever calling emit_flow() for it -- discovery and flow-emission are
    not the same moment), so truncating that flow's transitions at the
    first one whose to_fp matches is exact, not approximate.

    Empty list for the root state -- discovered_by_flow is None only
    for the very first state (see crawl()'s own root-discovery special
    case, which sets up StateNode directly and never goes through
    _run_dfs's normal `discovered_by_flow=next_flow_id[0]` assignment)."""
    node = run.states.get(state_fp)
    if node is None:
        raise ValueError(f"state {state_fp} not found in this run")
    if node.discovered_by_flow is None:
        return []
    flow = next((f for f in run.flows if f.id == node.discovered_by_flow), None)
    if flow is None:
        raise ValueError(f"state {state_fp}'s discovering flow (#{node.discovered_by_flow}) not found")
    for i, t in enumerate(flow.transitions):
        if t.to_fp == state_fp:
            return list(flow.transitions[:i + 1])
    raise ValueError(f"state {state_fp}'s own discovering flow (#{flow.id}) doesn't actually reach it "
                      f"-- this would be a data inconsistency, not a normal error")


def explore_combination(run: RunResult, state_fp: str, candidate_indices: list[int],
                         limit_overrides: dict, credentials: dict, persona_name: str = "default") -> None:
    """Apply several is_choice candidates from ONE already-known state
    TOGETHER, in a single replay, then keep exploring normally from
    whatever that combination reaches. The direct answer to a real,
    verified limitation (see ROADMAP.md "Known limitation --
    conjunctive multi-parameter gating is invisible to DFS"): the
    crawler can only ever change one such candidate at a time on its
    own, because picking one that doesn't itself alter the visible
    candidate set reads as an ordinary "revisit" and _run_dfs's own
    main loop stops the branch right there (see its comment at
    `if new_fp in run.states:`). A page gated behind several
    parameters set TOGETHER is structurally invisible to autonomous
    exploration -- FlowScout doesn't guess the right combination (that
    would mean inventing an expected result, which this project
    deliberately never does), so this lets a human who already knows
    the right combination hand it over directly instead.

    Deliberately does NOT create a separate StateNode for "1 of N set",
    "2 of N set", etc. -- those intermediate states are exactly the
    ordinary revisits DFS already can't get past on its own (same
    fingerprint as before, no new candidates yet), so recording them
    would be noise, not signal. Only the FINAL combined state -- after
    every selected candidate has been applied, in order -- is
    discovered and recorded: the one piece of information a human
    actually wanted when they built this combination.

    `state_fp` must already be in run.states (from a prior crawl --
    this is deliberately NOT usable "cold" against a site that hasn't
    been crawled at all: `candidate_indices` resolve against that
    state's own already-discovered `node.candidates`, never
    user-authored CSS/locators FlowScout hasn't itself verified).
    `candidate_indices`: positions into `run.states[state_fp].candidates`,
    applied in the given order -- the caller's own choice of which
    control to set first/last is respected, the same way a real person
    filling in a form top-to-bottom would. Mutates `run` in place,
    exactly like resume_flow()."""
    node = run.states.get(state_fp)
    if node is None:
        raise ValueError(f"state {state_fp} not found in this run")
    if not candidate_indices:
        raise ValueError("at least one candidate must be selected")

    path_to_state = _path_to_state(run, state_fp)

    from playwright.sync_api import sync_playwright

    limits = {**run.config.get("limits", {}), **{k: v for k, v in limit_overrides.items() if k != "allow_mutating"}}
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = limit_overrides.get("allow_mutating", run.config.get("allow_mutating", True))

    # Local only -- run.config stays an honest record of how the
    # original crawl was actually configured, same reasoning as
    # resume_flow()'s own identical line.
    config = {**run.config, "limits": limits, "allow_mutating": allow_mutating}

    combo_path = list(path_to_state)
    for idx in candidate_indices:
        if idx < 0 or idx >= len(node.candidates):
            raise ValueError(f"candidate index {idx} out of range for state {state_fp}")
        candidate = node.candidates[idx]
        # Same safety invariant normal DFS enforces before ever clicking
        # anything (see this module's own top-of-file docstring) -- a
        # human picking a combination through the UI doesn't get to
        # silently bypass it.
        if candidate.risk == Risk.DESTRUCTIVE:
            raise ValueError(f"candidate '{candidate.label}' is destructive -- never walked, "
                              f"in a combination or otherwise")
        if candidate.risk == Risk.MUTATING and not allow_mutating:
            raise ValueError(f"candidate '{candidate.label}' is mutating and allow_mutating is false")
        el_meta = json.loads(candidate.selector)
        trial = Transition(from_fp=state_fp, to_fp=None, action_label=describe_action(el_meta, None),
                            action_norm_signature=candidate.norm_signature, risk=candidate.risk,
                            risk_reason=candidate.risk_reason, replay_meta=candidate.selector,
                            is_choice=candidate.is_choice,
                            anchor_target_missing=candidate.anchor_target_missing)
        combo_path.append(trial)

    next_flow_id = [max((f.id for f in run.flows), default=0) + 1]
    # Fresh, not pre-populated from run.flows -- same convention
    # resume_flow() already uses for its own seq_to_flow_id/
    # revisit_history: a combination's own new branches are deduped
    # against each other, not against the entire pre-existing run.
    seq_to_flow_id: dict[tuple, int] = {}
    revisit_history: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            result = _run_path(browser, config, combo_path, run, credentials)
            if result is None:
                raise RuntimeError("the combination failed to apply -- see this run's checkpoints "
                                    "for which step and why")
            (new_fp, url_pat, title, new_candidates, fill_summary, new_unclassified, new_disabled,
             choice_state, response_status, dialog_message, opened_new_page, validation_errors,
             captcha_detected) = result

            last = combo_path[-1]
            label_fields = {**(fill_summary or {}), **(choice_state or {})}
            last.action_label = describe_action(json.loads(last.replay_meta), label_fields or None)
            if fill_summary:
                last.form_fields = list(fill_summary.keys())
            last.to_fp = new_fp
            last.response_status = response_status
            last.dialog_message = dialog_message
            last.opened_new_page = opened_new_page
            last.validation_errors = validation_errors
            last.captcha_detected = captcha_detected

            combo_note = "Set via a user-specified parameter combination"
            if new_fp in run.states and not captcha_detected:
                last.outcome = "revisit"
                flow = Flow(
                    id=next_flow_id[0], status=FlowStatus.UNIQUE, duplicate_of=None,
                    dedup_reason="User-specified parameter combination -- reached an already-known state",
                    transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
                    origin_note=combo_note,
                )
                run.flows.append(flow)
            elif captcha_detected:
                # Checked BEFORE the plain "already-known state" branch
                # above, not only in an else -- captcha_detected here is
                # _discover_state()'s own fresh re-check for THIS combo,
                # so it's just as reliable whether new_fp turns out to be
                # brand new or a state some earlier path already reached
                # (found live: without this ordering, a combination
                # landing on an ALREADY-discovered captcha page read as
                # an unremarkable revisit instead of blocked). Records
                # the state as evidence if it's genuinely new, emits a
                # BLOCKED flow naming why, never pushes a frame to
                # explore a challenge page's own incidental candidates.
                last.outcome = "blocked"
                if new_fp not in run.states:
                    run.states[new_fp] = StateNode(
                        fingerprint=new_fp, url_pattern=url_pat, raw_url="", title=title,
                        candidates=new_candidates, discovered_by_flow=next_flow_id[0],
                        unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
                        captcha_detected=captcha_detected,
                    )
                flow = Flow(
                    id=next_flow_id[0], status=FlowStatus.BLOCKED, duplicate_of=None,
                    dedup_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) "
                                 f"-- never explored further",
                    transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
                    origin_note=combo_note,
                )
                run.flows.append(flow)
                next_flow_id[0] += 1
            else:
                last.outcome = "ok"
                new_node = StateNode(
                    fingerprint=new_fp, url_pattern=url_pat, raw_url="", title=title,
                    candidates=new_candidates, discovered_by_flow=next_flow_id[0],
                    unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
                )
                run.states[new_fp] = new_node
                flow = Flow(
                    id=next_flow_id[0], status=FlowStatus.UNIQUE, duplicate_of=None,
                    dedup_reason="User-specified parameter combination -- newly discovered state",
                    transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
                    origin_note=combo_note,
                )
                run.flows.append(flow)
                next_flow_id[0] += 1

                # The combination's own state/flow are a free seed, not
                # counted against the budget _run_dfs spends exploring
                # WHATEVER ELSE this unlocked -- same "own full budget"
                # reasoning as resume_flow()'s states_before/flows_before.
                frame = _Frame(fp=new_fp, path=combo_path)
                frame.order = _order_for(new_node, max_breadth, run, revisit_history)
                stack: list[_Frame] = [frame]
                states_before = len(run.states) - 1
                flows_before = len(run.flows) - 1

                _run_dfs(browser, config, run, credentials, persona_name, stack, next_flow_id,
                         max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                         states_before, flows_before, False, seq_to_flow_id, revisit_history,
                         origin_note="Continued after testing a parameter combination")
        finally:
            browser.close()

    sem_cfg = run.config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful combination
            run.semantic_dedup_status = f"error on combination: {exc}"
