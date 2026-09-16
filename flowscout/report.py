"""Render a RunResult into a single self-contained HTML report."""
from __future__ import annotations

import html
import json
from collections import Counter

from .fingerprint import human_page_label
from .models import ChangeReport, FlowStatus, GapAnalysis, Risk, RunResult

_STATUS_META = {
    FlowStatus.UNIQUE: ("Unique", "pill-unique"),
    FlowStatus.DUPLICATE: ("Duplicate", "pill-duplicate"),
    FlowStatus.BLOCKED: ("Blocked", "pill-blocked"),
}
_RISK_META = {
    Risk.SAFE: ("safe", "risk-safe"),
    Risk.MUTATING: ("mutating", "risk-mutating"),
    Risk.DESTRUCTIVE: ("destructive", "risk-destructive"),
}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _flow_steps_html(flow, states: dict) -> str:
    parts = []
    for i, t in enumerate(flow.transitions):
        risk_label, risk_class = _RISK_META[t.risk]
        from_state = states.get(t.from_fp)
        if from_state:
            page = _esc(human_page_label(from_state.url_pattern))
        elif not t.from_fp:
            # from_fp == "" only for a direct-nav seed transition
            # (crawler.py's seed_urls/sitemap_url handling) -- there IS
            # no "page this started from", by design: it's an
            # additional entry point, not something reached by clicking
            # from anywhere already discovered. A bare "?" here would
            # look like a bug; name what it actually is instead.
            page = "(direct navigation)"
        else:
            page = "?"
        outcome_note = ""
        if t.outcome == "revisit":
            to_state = states.get(t.to_fp)
            dest = f" ({_esc(human_page_label(to_state.url_pattern))})" if to_state else ""
            outcome_note = f' <span class="step-note">→ back to an already-explored state{dest}</span>'
        elif t.outcome == "error":
            outcome_note = f' <span class="step-note step-error">→ error: {_esc(t.detail[:140])}</span>'
        elif t.outcome == "blocked":
            # Set only by a CAPTCHA/challenge detection (crawler.py's
            # captcha_detected handling) -- the flow's own card already
            # shows the "Blocked" pill and the full reason via
            # dedup_reason, this is the per-step echo of it, same
            # relationship captcha_detected below has to that reason.
            outcome_note = ' <span class="step-note step-error">→ blocked (see reason above)</span>'
        # response_status is a pure visibility signal (see actions.py's
        # _capture_nav_status) -- it never changed what got crawled, so it's
        # additive to whatever outcome_note already says, not a replacement.
        # A same-domain link landing on a 404/5xx was previously completely
        # invisible: same fingerprint shape as any other new page.
        if t.response_status is not None and t.response_status >= 400:
            outcome_note += (
                f' <span class="step-note step-error">→ page returned HTTP {t.response_status}</span>'
            )
        # anchor_target_missing is known BEFORE the click (a fact about the
        # DOM at discovery time -- see ElementCandidate's own docstring),
        # unlike response_status above which only exists after. Same
        # "state the fact" styling either way.
        if t.anchor_target_missing:
            outcome_note += (
                ' <span class="step-note step-error">'
                '→ this link\'s anchor target doesn\'t exist on the page</span>'
            )
        # dialog_message/opened_new_page (Aug 2026): both pure
        # visibility signals, same "state the fact" principle as
        # response_status/anchor_target_missing above -- neither
        # changes what got crawled. A native confirm()/alert()/prompt()
        # was previously invisible (Playwright's own default silently
        # auto-dismisses it with no listener registered, so a "Delete"
        # button gated behind confirm() looked like an ordinary, inert
        # click); a target="_blank"/window.open() spawned tab was
        # equally invisible, since the crawler only ever looks at the
        # ORIGINAL page, whose own fingerprint never changed.
        if t.dialog_message:
            outcome_note += f' <span class="step-note">→ dialog: {_esc(t.dialog_message)}</span>'
        if t.opened_new_page:
            outcome_note += (
                f' <span class="step-note">→ opened a new tab: {_esc(t.opened_new_page)} '
                f'(not explored -- crawler stays on this page)</span>'
            )
        # validation_errors (Sep 2026) differs from the visibility-only
        # signals above: it's also folded into state_fingerprint() itself
        # (see crawler.py's _discover_state()), so a rejected submission
        # genuinely becomes its own explorable state instead of an
        # indistinguishable revisit. Styled as an error note (not a plain
        # one) since surfacing exactly this -- what the app does with bad
        # input -- is the whole point of a QA tool, not an incidental fact.
        if t.validation_errors:
            outcome_note += (
                f' <span class="step-note step-error">→ validation error: '
                f'{_esc(t.validation_errors)}</span>'
            )
        # captcha_detected (Sep 2026): like validation_errors, this
        # changes what happens next (crawler.py forces the flow to
        # BLOCKED and never explores past it) rather than being purely
        # observational -- surfaced inline here in addition to the
        # flow-level "Blocked" reason so it's visible scanning the step
        # list alone, without needing to also read the card's own text.
        if t.captcha_detected:
            outcome_note += (
                f' <span class="step-note step-error">→ CAPTCHA/challenge detected: '
                f'{_esc(t.captcha_detected)}</span>'
            )
        parts.append(
            f'<li class="step"><span class="step-idx">{i + 1}</span>'
            f'<span class="step-page">{page}</span>'
            f'<span class="step-label">{_esc(t.action_label)}</span>'
            f'<span class="risk-chip {risk_class}">{risk_label}</span>'
            f"{outcome_note}</li>"
        )
    return "<ol class=\"steps\">" + "".join(parts) + "</ol>"


def _resume_box_html(flow, run_id: str, default_allow_mutating: bool) -> str:
    # Only rendered when both a run_id (this report is being served by
    # the web UI, not a standalone CLI --out file with no server behind
    # it to call) and flow.resumable (see its own docstring for exactly
    # which BLOCKED reasons qualify) are true. The two fields cover the
    # two per-flow-anchored reasons uniformly rather than branching the
    # UI on which one this particular flow hit -- raising depth doesn't
    # hurt a repeat-cap/risk-policy dead end, it just may not be the
    # fix either, and the operator can see which applies from the
    # reason text printed right above this box.
    suggested_depth = len(flow.transitions) + 5
    mutating_checked = "checked" if default_allow_mutating else ""
    return f"""
          <div class="resume-box">
            <label>Max depth <input type="number" class="resume-depth" value="{suggested_depth}" min="{len(flow.transitions) + 1}" style="width:64px"></label>
            <label><input type="checkbox" class="resume-mutating" {mutating_checked}> Allow mutating</label>
            <button type="button" onclick="flowscoutResume('{_esc(run_id)}', {flow.id}, this)">Resume this flow</button>
            <span class="resume-status"></span>
          </div>"""


def _flow_card_html(flow, states: dict, show_persona: bool = False,
                     run_id: str | None = None, default_allow_mutating: bool = True) -> str:
    status_label, status_class = _STATUS_META[flow.status]
    end_state = states.get(flow.end_state_fp)
    end_url = _esc(end_state.url_pattern) if end_state else "?"
    # Only shown when a run actually crawled more than one persona --
    # a single-persona run's every card would otherwise say "default",
    # noise with nothing to distinguish it from.
    persona_html = f'<span class="risk-chip risk-neutral">{_esc(flow.persona)}</span>' if show_persona else ""
    resume_html = (_resume_box_html(flow, run_id, default_allow_mutating)
                   if run_id and flow.resumable else "")
    # origin_note (Aug 2026): only ever set on a flow an operator action
    # produced afterward (Resume this flow/Resume all/Test this
    # combination), never a normal crawl pass -- see Flow.origin_note's
    # own docstring for the live gap this closes: an operator running
    # "Resume all blocked flows" on 12 flows had no way to tell, from the
    # report alone, which of the resulting cards were the new result
    # versus which were there before.
    origin_html = (f'<span class="risk-chip risk-safe" title="{_esc(flow.origin_note)}">'
                   f'↳ {_esc(flow.origin_note)}</span>' if flow.origin_note else "")
    return f"""
        <article class="flow-card">
          <header class="flow-head">
            <span class="flow-id">#{flow.id}</span>
            <span class="pill {status_class}">{status_label}</span>
            {persona_html}
            {origin_html}
            <span class="flow-end mono">{end_url}</span>
            <span class="flow-len">{len(flow.transitions)} step{'s' if len(flow.transitions) != 1 else ''}</span>
          </header>
          {_flow_steps_html(flow, states)}
          <p class="flow-reason">{_esc(flow.dedup_reason)}</p>
          {resume_html}
        </article>"""


def _resume_all_box_html(run: RunResult, run_id: str | None) -> str:
    # Same run_id gate as the per-flow resume box -- a standalone CLI
    # report has no server to POST this to. Silently empty (not an
    # "empty" message) when there's nothing resumable, or no server,
    # rather than a permanent fixture cluttering every report.
    if not run_id:
        return ""
    resumable_count = sum(1 for f in run.flows if f.resumable)
    if resumable_count == 0:
        return ""
    default_allow_mutating = bool(run.config.get("allow_mutating", True))
    mutating_checked = "checked" if default_allow_mutating else ""
    plural = "s" if resumable_count != 1 else ""
    return f"""
      <div class="resume-all-box">
        <span>{resumable_count} blocked flow{plural} can be resumed.</span>
        <label>Depth increment <input type="number" class="resume-all-depth" value="5" min="1" style="width:56px"></label>
        <label><input type="checkbox" class="resume-all-mutating" {mutating_checked}> Allow mutating</label>
        <button type="button" onclick="flowscoutResumeAll('{_esc(run_id)}', this)">Resume all blocked flows</button>
        <span class="resume-all-status"></span>
      </div>"""


def _flows_html(run: RunResult, run_id: str | None = None) -> str:
    # Unique (and blocked -- these need a look too, they're not redundant,
    # they're incomplete) lead the list, fully visible. Duplicates are real
    # findings worth keeping -- each still carries its own reasoning -- but
    # they're not what a reader scans first, so they're tucked into a
    # closed-by-default <details>: no JS needed, and a collapsed summary
    # is more honest than a huge wall of cards no one reads past #3.
    lead = [f for f in run.flows if f.status in (FlowStatus.UNIQUE, FlowStatus.BLOCKED)]
    duplicates = [f for f in run.flows if f.status == FlowStatus.DUPLICATE]
    show_persona = len({f.persona for f in run.flows}) > 1
    default_allow_mutating = bool(run.config.get("allow_mutating", True))

    lead_html = "\n".join(_flow_card_html(f, run.states, show_persona, run_id, default_allow_mutating)
                           for f in lead)
    if not duplicates:
        return lead_html

    n = len(duplicates)
    dup_cards = "\n".join(_flow_card_html(f, run.states, show_persona, run_id, default_allow_mutating)
                           for f in duplicates)
    return f"""{lead_html}
        <details class="dup-details">
          <summary class="dup-summary">Show {n} duplicate flow{'s' if n != 1 else ''} — each still shows its own dedup reasoning</summary>
          <div class="dup-body">{dup_cards}</div>
        </details>"""


def _change_report_html(changes: ChangeReport | None) -> str:
    """Longitudinal diff against this project's *previous* run (see
    change_detection.py) -- always computed at crawl time now that M3.5
    tracks project state, so unlike the gap section this doesn't depend
    on a TCMS being supplied. Deliberately never says "broken": a
    missing flow could be a removed feature, a real regression, or just
    crawl variance -- measured, not hypothetical (Site B produced
    different flow counts on three back-to-back re-crawls of an
    unchanged target; saucedemo was bit-for-bit identical across two).
    The operator decides; this states the fact plainly."""
    if changes is None:
        return ""
    if changes.baseline:
        return """
  <h2>Change detection</h2>
  <p class="subhead">No prior run of this project to compare against — this crawl is the new baseline. Re-crawl later and this section will show what moved.</p>"""

    s = changes.summary()
    env_html = (f'<p class="flow-reason" style="color:var(--sem-destructive)">{_esc(changes.environment_mismatch)}</p>'
                if changes.environment_mismatch else "")

    def _event_row(e, note: str = "") -> str:
        badge = f' <span class="risk-chip risk-mutating">{_esc(e.tcms_id)}</span>' if e.tcms_id else ""
        prev = (f'<div class="flow-reason">Was: {_esc(e.previous_summary)}</div>' if e.previous_summary else "")
        return f"""
        <article class="flow-card">
          <header class="flow-head">
            <span class="flow-id mono">{_esc(e.identity)}</span>{badge}
          </header>
          <p class="step-label">{_esc(e.summary)}</p>
          {prev}
          {f'<p class="flow-reason">{note}</p>' if note else ''}
        </article>"""

    missing = [e for e in changes.events if e.kind == "missing"]
    changed = [e for e in changes.events if e.kind == "changed"]
    new = [e for e in changes.events if e.kind == "new"]

    missing_html = "".join(_event_row(
        e, "Not found this run — could be a removed/broken feature, or crawl variance on this target. "
           "Confirmed test cases (tagged above) are worth checking first." if e.tcms_id else
           "Not found this run — not linked to a test case, lower priority to investigate."
    ) for e in missing) or '<p class="empty">Nothing previously seen went missing.</p>'

    changed_html = "".join(_event_row(
        e, "Reaches the same milestone via a different path than last time — a linked test case's "
           "steps may now be wrong." if e.tcms_id else "Path changed since last time."
    ) for e in changed) or '<p class="empty">Every previously-seen flow still reaches its milestone the same way.</p>'

    new_html = ""
    if new:
        # A "new" flow's tcms_id (Aug 2026) comes from THIS run's own gap
        # analysis, not a prior confirmation -- the honest, checkable
        # difference between "genuinely undocumented behavior, worth a
        # look" and "a new capability that already matches a test case".
        # A fuzzy match, not a certain one, so worded that way rather than
        # "confirmed" (see models.py's ChangeEvent.tcms_id docstring).
        rows = "".join(_event_row(
            e, "Matches an existing test case (tagged above) — likely a newly-covered capability, "
               "not undocumented behavior. Gap-analysis match, not a confirmed link — worth a quick "
               "look, not necessarily a concern." if e.tcms_id else ""
        ) for e in new)
        new_html = f"""
  <details class="dup-details">
    <summary class="dup-summary">Show {len(new)} new flow{'s' if len(new) != 1 else ''} since last run</summary>
    <div class="dup-body">{rows}</div>
  </details>"""

    return f"""
  <h2>Change detection</h2>
  <p class="subhead">Compared against this project's previous run.</p>
  {env_html}
  <div class="metrics">
    <div class="metric"><div class="num">{s['missing']}</div><div class="lbl">missing since last run</div></div>
    <div class="metric"><div class="num">{s['missing_confirmed']}</div><div class="lbl">missing, confirmed test cases</div></div>
    <div class="metric"><div class="num">{s['changed']}</div><div class="lbl">path changed</div></div>
    <div class="metric"><div class="num">{s['changed_confirmed']}</div><div class="lbl">changed, confirmed test cases</div></div>
  </div>

  <h3>Missing since last run</h3>
  <p class="subhead">Seen before, not found this time. Not necessarily broken — see above.</p>
  {missing_html}

  <h3>Path changed</h3>
  <p class="subhead">Still reachable, but the route there is different from last time.</p>
  {changed_html}
  {new_html}"""


def _gap_section_html(gap: GapAnalysis | None, run: RunResult) -> str:
    """Only rendered when a TCMS was actually supplied -- the section
    that makes FlowScout more than a fancy crawler: not just what the
    app does, but where that disagrees with what the team already
    tests."""
    if gap is None:
        return ""
    if not gap.flow_coverage and not gap.tcms_coverage:
        return f"""
  <h2>Gap analysis</h2>
  <p class="subhead">Compared against <span class="mono">{_esc(gap.tcms_source)}</span> — {_esc(gap.status)}</p>"""

    s = gap.summary()
    flows_by_id = {f.id: f for f in run.flows}
    gap_flows = [x for x in gap.flow_coverage if x.status == "gap"]
    partial_flows = [x for x in gap.flow_coverage if x.status == "partial"]
    nav_flows = [x for x in gap.flow_coverage if x.status == "navigation"]
    not_found = [x for x in gap.tcms_coverage if x.status == "not_found"]
    covered_tcms = [x for x in gap.tcms_coverage if x.status == "covered"]
    show_persona = len({f.persona for f in run.flows}) > 1

    gap_flows_parts = []
    for x in gap_flows:
        gap_flows_parts.append(_flow_card_html(flows_by_id[x.flow_id], run.states, show_persona))
        gap_flows_parts.append(
            f'<p class="flow-reason">Flow identity: <span class="mono">{_esc(x.identity)}</span> — '
            f'once you know which TCMS case (if any) this should be, '
            f'<span class="mono">flowscout confirm --identity {_esc(x.identity)} --tcms-id &lt;ID&gt;</span> '
            f'makes that link durable across future runs.</p>')
    gap_flows_html = "".join(gap_flows_parts) or (
        '<p class="empty">Every discovered flow matched something in the test plan.</p>')

    partial_html = ""
    if partial_flows:
        rows = "".join(f"""
        <tr><td>flow #{x.flow_id}</td>
        <td>{'<br>'.join(f'{_esc(m["action"])} → ' + (f'<span class="mono">{_esc(m["matched_tcms_id"])}</span> ({m["score"]:.0%})' if m["covered"] else '<span class="risk-chip risk-mutating">no match</span>') for m in x.action_matches)}</td></tr>"""
                        for x in partial_flows)
        n = len(partial_flows)
        partial_html = f"""
  <h3>Flows partially covered</h3>
  <p class="subhead">Some of what this flow does matched a test case; some didn't — listed per action below.</p>
  <div class="table-scroll"><table class="data-table"><thead><tr><th>Flow</th>
    <th>Actions and their matches</th></tr></thead><tbody>{rows}</tbody></table></div>"""

    nav_note = ""
    if nav_flows:
        n = len(nav_flows)
        nav_note = (f'<p class="subhead">{n} flow{"s" if n != 1 else ""} excluded as navigation-only '
                    f'(no state-changing action to test — same shared-step signal M4 codegen uses).</p>')

    if not_found:
        # Reverse direction (Aug 2026): a bare "not_found" doesn't say
        # WHY -- could be a stale test case, a path this crawl never got
        # close to, or a precondition (already-logged-in admin, an item
        # already in the cart) the clean-slate-per-path model doesn't
        # produce. `diagnosis` (see gap_analysis.py's _diagnose_not_found)
        # is a grounded reason drawn from data the crawl already
        # collected -- withheld by risk/limit policy, attempted and
        # errored, or discovered but never tried (a budget gap on
        # FlowScout's own side, not the app's) -- when one was found;
        # honestly labeled "no evidence" rather than guessed at when
        # none matched.
        def _diagnosis_chip(x):
            if x.diagnosis == "withheld":
                return f'<span class="risk-chip risk-mutating" title="{_esc(x.diagnosis_detail)}">withheld</span>'
            if x.diagnosis == "errored":
                return f'<span class="risk-chip risk-destructive" title="{_esc(x.diagnosis_detail)}">attempted, errored</span>'
            if x.diagnosis == "discovered_not_walked":
                return f'<span class="risk-chip risk-safe" title="{_esc(x.diagnosis_detail)}">seen, not tried</span>'
            return '<span class="risk-chip risk-neutral">no evidence found</span>'

        rows = "".join(f"""
        <tr><td class="mono">{_esc(x.tcms_id)}</td><td>{_esc(x.tcms_title)}</td>
        <td class="num">{x.score:.0%}</td><td>{_diagnosis_chip(x)}</td></tr>""" for x in not_found)
        not_found_html = (f'<table class="data-table"><thead><tr><th>ID</th><th>Title</th>'
                           f'<th class="num">Best match score</th><th>Diagnosis</th></tr></thead>'
                           f'<tbody>{rows}</tbody></table>'
                           f'<p class="subhead">"Withheld"/"attempted, errored"/"seen, not tried" are drawn '
                           f'from what this crawl actually saw (hover for detail) -- "seen, not tried" means '
                           f'the crawl discovered the control but ran out of budget before trying it (raise '
                           f'max_flows/max_states to close it); "no evidence found" means none of the three '
                           f'matched, which could mean a stale test case or a precondition this crawl '
                           f'doesn\'t set up on its own.</p>')
    else:
        not_found_html = '<p class="empty">Every test case matched a discovered flow.</p>'

    covered_html = ""
    if covered_tcms:
        rows = "".join(f"""
        <tr><td class="mono">{_esc(x.tcms_id)}</td><td>{_esc(x.tcms_title)}</td>
        <td>flow #{x.matched_flow_id}</td><td class="num">{x.score:.0%}</td>
        <td>{'<span class="risk-chip risk-safe">confirmed</span>' if x.confirmed else '<span class="risk-chip risk-neutral">inferred</span>'}</td></tr>"""
                        for x in covered_tcms)
        n = len(covered_tcms)
        covered_html = f"""
  <details class="dup-details">
    <summary class="dup-summary">Show {n} covered test case{'s' if n != 1 else ''}</summary>
    <div class="dup-body"><table class="data-table"><thead><tr><th>ID</th><th>Title</th>
      <th>Matched flow</th><th class="num">Score</th><th>Source</th></tr></thead><tbody>{rows}</tbody></table></div>
  </details>"""

    # Matching is action-level now (see gap_analysis.py docstring), so a
    # "covered" flow doing several things has each of them independently
    # verified, not just guessed as a whole -- worth showing the evidence,
    # not a warning like this section used to be before that fix.
    multi_action = [x for x in gap.flow_coverage if x.status == "covered" and len(x.mutating_actions) > 1]
    multi_action_html = ""
    if multi_action:
        rows = "".join(f"""
        <tr><td>flow #{x.flow_id}</td>
        <td>{'<br>'.join(f'{_esc(m["action"])} → <span class="mono">{_esc(m["matched_tcms_id"])}</span> ({m["score"]:.0%})' for m in x.action_matches)}</td></tr>"""
                        for x in multi_action)
        n = len(multi_action)
        multi_action_html = f"""
  <details class="dup-details">
    <summary class="dup-summary">Show {n} covered flow{'s' if n != 1 else ''} verified across multiple actions</summary>
    <div class="dup-body"><table class="data-table"><thead><tr><th>Flow</th>
      <th>Each action's own match</th></tr></thead><tbody>{rows}</tbody></table></div>
  </details>"""

    return f"""
  <h2>Gap analysis</h2>
  <p class="subhead">Compared against <span class="mono">{_esc(gap.tcms_source)}</span> — {_esc(gap.status)}</p>
  <div class="metrics">
    <div class="metric"><div class="num">{s['flows_gap']}</div><div class="lbl">flows with no matching test</div></div>
    <div class="metric"><div class="num">{s['flows_partial']}</div><div class="lbl">flows partially covered</div></div>
    <div class="metric"><div class="num">{s['tcms_not_found']}</div><div class="lbl">tests not found in the app</div></div>
    <div class="metric"><div class="num">{s['flows_covered']}</div><div class="lbl">flows covered</div></div>
    <div class="metric"><div class="num">{s['tcms_covered']}</div><div class="lbl">tests covered</div></div>
    <div class="metric"><div class="num">{s['flows_confirmed']}</div><div class="lbl">confirmed by operator</div></div>
  </div>

  <h3>Flows with no matching test case</h3>
  <p class="subhead">The application does this. Nothing in the test plan says it should — that's the gap.</p>
  {gap_flows_html}
  {partial_html}

  <h3>Test cases not found in the explored flows</h3>
  <p class="subhead">Could be a stale test for behavior that no longer exists, an app change, or something this crawl's risk policy or limits didn't reach — the report doesn't guess which, worth a human look either way.</p>
  <div class="table-scroll">{not_found_html}</div>
  {covered_html}
  {multi_action_html}
  {nav_note}"""


def _states_html(run: RunResult) -> str:
    rows = []
    for fp, node in run.states.items():
        outdeg = len(node.candidates)
        handler_n = sum(1 for c in node.candidates if c.discovered_via == "handler")
        risk_counts = Counter(c.risk.value for c in node.candidates)
        risk_str = ", ".join(f"{v} {k}" for k, v in risk_counts.items()) or "—"
        outdeg_cell = f"{outdeg} <span class=\"step-note\">(+{handler_n} via handler)</span>" if handler_n else str(outdeg)
        gap_n = sum(u["count"] for u in node.unclassified_interactive)
        gap_cell = f'<span class="risk-chip risk-mutating">{gap_n}</span>' if gap_n else "—"
        rows.append(f"""
        <tr>
          <td class="mono">{_esc(fp)}</td>
          <td class="mono">{_esc(node.url_pattern)}</td>
          <td>{_esc(node.title)}</td>
          <td class="num">{outdeg_cell}</td>
          <td>{_esc(risk_str)}</td>
          <td class="num">{gap_cell}</td>
        </tr>""")
    return "\n".join(rows)


def _combination_box_html(fp: str, node, run_id: str, default_allow_mutating: bool) -> str:
    """Groups this state's is_choice candidates by ElementCandidate.
    choice_group (a <select>/radio group's own identity, or a
    checkbox's own -- see that field's docstring) so an operator can
    pick ONE value per group (radio-style for a group with 2+ members
    -- mutually exclusive alternatives) or toggle it (checkbox-style
    for a group of exactly 1) and submit the combination together.
    Only rendered when a state has 2+ distinct groups -- combining
    options WITHIN one group makes no sense (only one can ever be
    picked at a time), so anything with fewer than two groups has
    nothing to combine."""
    groups: dict[str, list[tuple[int, "ElementCandidate"]]] = {}
    for i, c in enumerate(node.candidates):
        if c.is_choice and c.choice_group:
            groups.setdefault(c.choice_group, []).append((i, c))
    if len(groups) < 2:
        return ""
    parts = []
    for group_name, members in groups.items():
        if len(members) >= 2:
            options = "".join(
                f'<label><input type="radio" name="combo-{_esc(fp)}-{_esc(group_name)}" '
                f'class="combo-choice" value="{i}"> {_esc(c.label)}</label>'
                for i, c in members
            )
        else:
            i, c = members[0]
            options = f'<label><input type="checkbox" class="combo-choice" value="{i}"> {_esc(c.label)}</label>'
        parts.append(f'<div class="combo-group"><span class="combo-group-name">{_esc(group_name)}</span>{options}</div>')
    mutating_checked = "checked" if default_allow_mutating else ""
    return f"""
      <div class="combo-box" data-state-fp="{_esc(fp)}">
        {"".join(parts)}
        <div class="combo-controls">
          <label>Max depth <input type="number" class="combo-depth" value="8" min="1" style="width:64px"></label>
          <label><input type="checkbox" class="combo-mutating" {mutating_checked}> Allow mutating</label>
          <button type="button" onclick="flowscoutExploreCombination('{_esc(run_id)}', this)">Test this combination</button>
          <span class="combo-status"></span>
        </div>
      </div>"""


def _combination_section_html(run: RunResult, run_id: str | None) -> str:
    # Same run_id gate as the resume box -- a standalone CLI report
    # (flowscout crawl --out ..., no server behind it) has nothing to
    # POST this to, so the UI is omitted rather than rendered dead.
    if not run_id:
        return '<p class="empty">Only available from the web UI (needs a server to apply the combination against).</p>'
    default_allow_mutating = bool(run.config.get("allow_mutating", True))
    cards = []
    for fp, node in run.states.items():
        box = _combination_box_html(fp, node, run_id, default_allow_mutating)
        if not box:
            continue
        page = _esc(human_page_label(node.url_pattern))
        cards.append(f'<article class="flow-card"><h3 class="combo-card-title">{page}</h3>{box}</article>')
    if not cards:
        return ('<p class="empty">No state has two or more independent choice controls '
                '(checkbox/radio/select) to combine.</p>')
    return "\n".join(cards)


def _handler_discovered_html(run: RunResult) -> str:
    """Candidates that weren't a/button/[role=button]-style markup at
    all -- div-as-button elements promoted after CDP's
    DOMDebugger.getEventListeners confirmed a real click handler on them
    (actions.py's _verify_pool, Aug 2026). The positive counterpart to
    Coverage gaps below: this is what the new detection mechanism
    actually found and clicked, not just noticed."""
    rows = []
    for fp, node in run.states.items():
        page = _esc(human_page_label(node.url_pattern))
        for c in node.candidates:
            if c.discovered_via != "handler":
                continue
            rows.append(f"""
        <tr>
          <td>{page}</td>
          <td>{_esc(c.label)}</td>
          <td><span class="risk-chip risk-{_esc(c.risk.value)}">{_esc(c.risk.value)}</span></td>
        </tr>""")
    if not rows:
        return '<p class="empty">No div-as-button controls found on this run.</p>'
    return f"""<table class="data-table">
      <thead><tr><th>Page</th><th>Element</th><th>Risk</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _disabled_html(run: RunResult) -> str:
    """Elements CDP confirmed have a real click handler but that were
    disabled at the time (aria-disabled, a disabled-looking class, or
    pointer-events:none) -- correctly never clicked, but a real finding:
    a control that exists and is currently gated behind something else
    (e.g. an earlier choice in a wizard)."""
    rows = []
    for fp, node in run.states.items():
        if not node.disabled_interactive:
            continue
        page = _esc(human_page_label(node.url_pattern))
        for u in node.disabled_interactive:
            label = u["text"] or u["className"] or u["tag"]
            rows.append(f"""
        <tr>
          <td>{page}</td>
          <td class="mono">&lt;{_esc(u['tag'])}&gt; {_esc(label)}</td>
          <td class="num">{u['count']}</td>
        </tr>""")
    if not rows:
        return '<p class="empty">No disabled-but-real controls found on this run.</p>'
    return f"""<table class="data-table">
      <thead><tr><th>Page</th><th>Element</th><th class="num">Count</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _coverage_gaps_html(run: RunResult) -> str:
    """Populated only if CDP verification itself failed on some state
    (session error) -- a fallback list built from the old cursor:pointer/
    role/tabindex guess, so a CDP failure degrades to previously-shipped
    behavior instead of promoting nothing silently. Expected to be empty
    on a normal Chromium run; non-empty here is itself worth investigating."""
    rows = []
    for fp, node in run.states.items():
        if not node.unclassified_interactive:
            continue
        page = _esc(human_page_label(node.url_pattern))
        for u in node.unclassified_interactive:
            label = u["text"] or u["className"] or u["tag"]
            rows.append(f"""
        <tr>
          <td>{page}</td>
          <td class="mono">&lt;{_esc(u['tag'])}&gt; {_esc(label)}</td>
          <td class="mono">{_esc(u['className'])}</td>
          <td class="num">{u['count']}</td>
        </tr>""")
    if not rows:
        return '<p class="empty">CDP verification ran cleanly on every state -- nothing fell back to the unverified guess.</p>'
    return f"""<table class="data-table">
      <thead><tr><th>Page</th><th>Element</th><th>Class</th><th class="num">Count</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _skipped_html(run: RunResult) -> str:
    if not run.skipped_candidates:
        return '<p class="empty">No candidates were withheld — nothing destructive or over the breadth cap was found in this run.</p>'
    grouped = Counter((s["label"], s["reason"], s["risk"]) for s in run.skipped_candidates)
    rows = []
    for (label, reason, risk), count in sorted(grouped.items(), key=lambda kv: -kv[1]):
        try:
            risk_label, risk_class = _RISK_META[Risk(risk)]
        except ValueError:
            risk_label, risk_class = "obstructed", "risk-neutral"
        rows.append(f"""
        <tr>
          <td>{_esc(label)}</td>
          <td><span class="risk-chip {risk_class}">{risk_label}</span></td>
          <td>{_esc(reason)}</td>
          <td class="num">{count}</td>
        </tr>""")
    return f"""<table class="data-table">
      <thead><tr><th>Action</th><th>Risk</th><th>Why withheld</th><th>Times seen</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>"""


def _checkpoints_html(run: RunResult) -> str:
    if not run.checkpoints:
        return '<p class="empty">No checkpoints raised — the run completed without errors or ambiguous states.</p>'
    rows = []
    for cp in run.checkpoints:
        rows.append(f"""
        <li class="checkpoint checkpoint-{_esc(cp.kind)}">
          <span class="cp-kind">{_esc(cp.kind)}</span>
          <div><p class="cp-msg">{_esc(cp.message)}</p>
          {f'<p class="cp-detail mono">{_esc(cp.detail)}</p>' if cp.detail else ''}</div>
        </li>""")
    return f'<ul class="checkpoints">{"".join(rows)}</ul>'


def _resume_script_html() -> str:
    # The report's only interactive JS (Aug 2026) -- everything else in
    # this file is genuinely static HTML. Only emitted when render_html
    # was given a run_id (see its own docstring), so a standalone CLI
    # report never carries dead code with nothing to call. Holds both
    # flowscoutResume (per-flow) and flowscoutExploreCombination
    # (per-state, multiple candidates at once) -- kept in one script
    # tag rather than two, since both are gated by the exact same
    # run_id condition and there's no reason to split them.
    return """
<script>
// Shared by all three actions below (Aug 2026) -- found live that
// "Done — reloading…" alone isn't enough: an operator resumed 12
// blocked flows, it genuinely worked (98 new flows, 11 newly unique),
// but the immediate reload gave no indication of that, and nothing
// in the reloaded report singled out which cards were new (see
// Flow.origin_note's own docstring for the other half of this fix).
// Formats the {new_flows, new_unique, new_duplicate, new_blocked}
// delta the backend now returns into one readable line.
function flowscoutDeltaText(delta) {
  if (!delta || delta.new_flows === 0) return 'no new flows found';
  const parts = [];
  if (delta.new_unique) parts.push(`${delta.new_unique} new unique`);
  if (delta.new_duplicate) parts.push(`${delta.new_duplicate} duplicate`);
  if (delta.new_blocked) parts.push(`${delta.new_blocked} still blocked`);
  return `${delta.new_flows} new flow${delta.new_flows !== 1 ? 's' : ''} found`
    + (parts.length ? ` (${parts.join(', ')})` : '');
}

// Pause before reloading so the summary line above is actually
// readable, not overwritten mid-render by the reload it precedes.
function flowscoutSleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function flowscoutResume(runId, flowId, btn) {
  const box = btn.closest('.resume-box');
  const depth = Number(box.querySelector('.resume-depth').value);
  const allowMutating = box.querySelector('.resume-mutating').checked;
  const statusEl = box.querySelector('.resume-status');
  btn.disabled = true;
  // Elapsed-time ticker (Aug 2026): a resumed flow replays its entire
  // path from a fresh browser for every candidate it tries, then keeps
  // exploring from there -- on a real, slower production site (not a
  // fast local fixture) this can genuinely take minutes, not seconds
  // (measured live: one real resume took 168s). A static "Resuming…"
  // with no elapsed time is indistinguishable from "stuck" well before
  // that -- this is a client-side-only fix (nothing is actually wrong
  // on the backend to fix) that just says so plainly instead of going
  // quiet.
  const startedAt = Date.now();
  const tick = () => {
    const secs = Math.round((Date.now() - startedAt) / 1000);
    statusEl.textContent = `Resuming… (${secs}s elapsed — this can take several minutes on slower sites)`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const res = await fetch(`/api/runs/${runId}/resume`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({flow_id: flowId, limits: {max_depth: depth, allow_mutating: allowMutating}}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      statusEl.textContent = 'Failed: ' + (data.detail || res.statusText);
      btn.disabled = false;
      return;
    }
    statusEl.textContent = `Done — ${flowscoutDeltaText(data.delta)} — reloading…`;
    await flowscoutSleep(2500);
    window.location.reload();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e;
    btn.disabled = false;
  } finally {
    clearInterval(timer);
  }
}

async function flowscoutExploreCombination(runId, btn) {
  const box = btn.closest('.combo-box');
  const stateFp = box.dataset.stateFp;
  const depth = Number(box.querySelector('.combo-depth').value);
  const allowMutating = box.querySelector('.combo-mutating').checked;
  const statusEl = box.querySelector('.combo-status');
  const indices = [...box.querySelectorAll('.combo-choice:checked')].map(el => Number(el.value));
  if (indices.length === 0) {
    statusEl.textContent = 'Pick at least one value first';
    return;
  }
  btn.disabled = true;
  // Same elapsed-time ticker as flowscoutResume above, same reason:
  // this replays a real path plus every selected choice, then keeps
  // exploring from there -- easily minutes on a real site, and a
  // static "Testing…" is indistinguishable from stuck.
  const startedAt = Date.now();
  const tick = () => {
    const secs = Math.round((Date.now() - startedAt) / 1000);
    statusEl.textContent = `Testing… (${secs}s elapsed — this can take several minutes on slower sites)`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const res = await fetch(`/api/runs/${runId}/explore-combination`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({state_fp: stateFp, candidate_indices: indices,
                             limits: {max_depth: depth, allow_mutating: allowMutating}}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      statusEl.textContent = 'Failed: ' + (data.detail || res.statusText);
      btn.disabled = false;
      return;
    }
    statusEl.textContent = `Done — ${flowscoutDeltaText(data.delta)} — reloading…`;
    await flowscoutSleep(2500);
    window.location.reload();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e;
    btn.disabled = false;
  } finally {
    clearInterval(timer);
  }
}

async function flowscoutResumeAll(runId, btn) {
  const box = btn.closest('.resume-all-box');
  const depthIncrement = Number(box.querySelector('.resume-all-depth').value);
  const allowMutating = box.querySelector('.resume-all-mutating').checked;
  const statusEl = box.querySelector('.resume-all-status');
  btn.disabled = true;
  // Same elapsed-time ticker as the single-flow versions above, more
  // important here still: this resumes EVERY blocked flow one after
  // another (has to be sequential -- see resume_all_blocked_in_run's
  // own docstring), so the total wait is the sum of however many
  // flows there are, not just one.
  const startedAt = Date.now();
  const tick = () => {
    const secs = Math.round((Date.now() - startedAt) / 1000);
    statusEl.textContent = `Resuming all blocked flows… (${secs}s elapsed — this processes them one at a time, so it can take a while)`;
  };
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const res = await fetch(`/api/runs/${runId}/resume-all`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({depth_increment: depthIncrement, allow_mutating: allowMutating}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      statusEl.textContent = 'Failed: ' + (data.detail || res.statusText);
      btn.disabled = false;
      return;
    }
    const results = data.results || [];
    const okCount = results.filter(r => r.status === 'ok').length;
    const errCount = results.length - okCount;
    statusEl.textContent = `Done — ${okCount} resumed${errCount ? `, ${errCount} failed` : ''}, `
      + `${flowscoutDeltaText(data.delta)} — reloading…`;
    await flowscoutSleep(2500);
    window.location.reload();
  } catch (e) {
    statusEl.textContent = 'Failed: ' + e;
    btn.disabled = false;
  } finally {
    clearInterval(timer);
  }
}
</script>"""


def render_html(run: RunResult, gap: GapAnalysis | None = None, changes: ChangeReport | None = None,
                 run_id: str | None = None) -> str:
    # run_id (Aug 2026): only known when this report is being served by
    # the web UI (web/runs.py always passes it) -- a standalone report
    # written by the CLI (`flowscout crawl --out ...`, no server behind
    # it to call) has nothing to POST a resume request to, so the
    # "Resume this flow" UI on BLOCKED cards is simply omitted rather
    # than rendered non-functional.
    s = run.summary()
    flows_html = _flows_html(run, run_id)
    resume_all_html = _resume_all_box_html(run, run_id)
    gap_html = _gap_section_html(gap, run)
    change_html = _change_report_html(changes)
    states_html = _states_html(run)
    combination_html = _combination_section_html(run, run_id)
    handler_discovered_html = _handler_discovered_html(run)
    disabled_html = _disabled_html(run)
    coverage_gaps_html = _coverage_gaps_html(run)
    skipped_html = _skipped_html(run)
    checkpoints_html = _checkpoints_html(run)
    cfg = run.config
    limits = cfg.get("limits", {})

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>FlowScout — {_esc(run.project)} run report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{
  --bg: #F6F7F9;
  --surface: #FFFFFF;
  --surface-alt: #EEF0F3;
  --border: #DDE1E6;
  --text-primary: #161A21;
  --text-secondary: #545C68;
  --text-tertiary: #8A919C;
  --accent: #14647F;
  --accent-soft-bg: #E3EEF2;
  --sem-safe: #0F6E56; --sem-safe-bg: #E1F5EE;
  --sem-mutating: #8A5A0B; --sem-mutating-bg: #FAEEDA;
  --sem-destructive: #9A3B22; --sem-destructive-bg: #FAECE7;
  --sem-info: #234E8C; --sem-info-bg: #E6EEF8;
  --font-sans: -apple-system, "Segoe UI", system-ui, sans-serif;
  --font-mono: "Cascadia Code", "Cascadia Mono", Consolas, "SF Mono", ui-monospace, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #14171C; --surface: #1B1F26; --surface-alt: #20252D; --border: #2C323C;
    --text-primary: #ECEEF1; --text-secondary: #A6AEBA; --text-tertiary: #71798A;
    --accent: #6FC1DC; --accent-soft-bg: #17313A;
    --sem-safe: #7FDCB9; --sem-safe-bg: #0E3B30;
    --sem-mutating: #F0BE6B; --sem-mutating-bg: #40300D;
    --sem-destructive: #F2A488; --sem-destructive-bg: #46201A;
    --sem-info: #9CC1F0; --sem-info-bg: #1B2E4A;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #14171C; --surface: #1B1F26; --surface-alt: #20252D; --border: #2C323C;
  --text-primary: #ECEEF1; --text-secondary: #A6AEBA; --text-tertiary: #71798A;
  --accent: #6FC1DC; --accent-soft-bg: #17313A;
  --sem-safe: #7FDCB9; --sem-safe-bg: #0E3B30;
  --sem-mutating: #F0BE6B; --sem-mutating-bg: #40300D;
  --sem-destructive: #F2A488; --sem-destructive-bg: #46201A;
  --sem-info: #9CC1F0; --sem-info-bg: #1B2E4A;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: var(--bg); color: var(--text-primary); font-family: var(--font-sans);
  font-size: 15px; line-height: 1.6; font-variant-numeric: tabular-nums;
}}
.wrap {{ max-width: 960px; margin: 0 auto; padding: 2.5rem 1.5rem 5rem; }}
.mono {{ font-family: var(--font-mono); font-size: 0.85em; }}
h1 {{ font-size: 26px; font-weight: 600; letter-spacing: -0.01em; text-wrap: balance; margin: 0 0 4px; }}
h2 {{ font-size: 18px; font-weight: 600; margin: 2.5rem 0 .9rem; padding-bottom: .5rem; border-bottom: 1px solid var(--border); }}
h2:first-of-type {{ margin-top: 2rem; }}
h3 {{ font-size: 14.5px; font-weight: 600; margin: 1.5rem 0 .5rem; }}
.eyebrow {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--accent); margin-bottom: 10px; }}
.subhead {{ color: var(--text-secondary); font-size: 14px; }}
.subhead .mono {{ color: var(--text-secondary); }}
.meta-row {{ display: flex; gap: 18px; flex-wrap: wrap; margin-top: 14px; }}
.meta-item {{ font-size: 12.5px; color: var(--text-tertiary); }}
.meta-item b {{ color: var(--text-secondary); font-weight: 600; }}

.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 1.75rem; }}
.metric {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.metric .num {{ font-size: 24px; font-weight: 600; letter-spacing: -0.01em; }}
.metric .lbl {{ font-size: 12px; color: var(--text-secondary); margin-top: 2px; }}

.pill {{ display: inline-flex; align-items: center; font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 99px; letter-spacing: .01em; }}
.pill-unique {{ background: var(--sem-safe-bg); color: var(--sem-safe); }}
.pill-duplicate {{ background: var(--sem-mutating-bg); color: var(--sem-mutating); }}
.pill-blocked {{ background: var(--sem-destructive-bg); color: var(--sem-destructive); }}

.risk-chip {{ display: inline-flex; font-size: 10.5px; font-weight: 600; padding: 2px 7px; border-radius: 6px; text-transform: uppercase; letter-spacing: .03em; }}
.risk-safe {{ background: var(--sem-safe-bg); color: var(--sem-safe); }}
.risk-mutating {{ background: var(--sem-mutating-bg); color: var(--sem-mutating); }}
.risk-destructive {{ background: var(--sem-destructive-bg); color: var(--sem-destructive); }}
.risk-neutral {{ background: var(--surface-alt); color: var(--text-tertiary); }}

.flow-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; }}
.flow-head {{ display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 10px; }}
.flow-id {{ font-family: var(--font-mono); font-size: 12.5px; color: var(--text-tertiary); }}
.flow-end {{ color: var(--text-secondary); font-size: 12px; margin-left: auto; }}
.flow-len {{ font-size: 11.5px; color: var(--text-tertiary); }}
.steps {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }}
.step {{ display: flex; align-items: center; gap: 8px; font-size: 13.5px; flex-wrap: wrap; }}
.step-idx {{ font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); width: 16px; flex-shrink: 0; }}
.step-page {{ font-size: 10.5px; font-weight: 600; color: var(--accent); background: var(--accent-soft-bg); padding: 2px 7px; border-radius: 5px; white-space: nowrap; }}
.step-label {{ color: var(--text-primary); }}
.step-note {{ font-size: 12px; color: var(--text-tertiary); }}
.step-error {{ color: var(--sem-destructive); }}
.flow-reason {{ margin: 10px 0 0; font-size: 12.5px; color: var(--text-tertiary); font-style: italic; }}
.resume-box {{ margin: 10px 0 0; padding: 8px 10px; background: var(--surface-alt); border-radius: 6px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; }}
.resume-box label {{ display: flex; align-items: center; gap: 4px; color: var(--text-secondary); }}
.resume-box input[type="number"] {{ font: inherit; padding: 2px 4px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: var(--text-primary); }}
.resume-box button {{ font: inherit; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text-primary); cursor: pointer; }}
.resume-box button:hover {{ border-color: var(--accent); }}
.resume-box button:disabled {{ opacity: .6; cursor: default; }}
.resume-status {{ color: var(--text-tertiary); }}

.resume-all-box {{ margin: 10px 0 16px; padding: 10px 12px; background: var(--accent-soft-bg); border: 1px solid var(--border); border-radius: 8px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 13px; }}
.resume-all-box label {{ display: flex; align-items: center; gap: 4px; color: var(--text-secondary); }}
.resume-all-box input[type="number"] {{ font: inherit; padding: 2px 4px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: var(--text-primary); }}
.resume-all-box button {{ font: inherit; padding: 5px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text-primary); cursor: pointer; font-weight: 600; }}
.resume-all-box button:hover {{ border-color: var(--accent); }}
.resume-all-box button:disabled {{ opacity: .6; cursor: default; }}
.resume-all-status {{ color: var(--text-tertiary); }}

.combo-card-title {{ font-size: 14px; font-weight: 600; margin: 0 0 8px; }}
.combo-box {{ display: flex; flex-direction: column; gap: 8px; font-size: 12.5px; }}
.combo-group {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 6px 8px; background: var(--surface-alt); border-radius: 6px; }}
.combo-group-name {{ color: var(--text-tertiary); font-family: var(--font-mono); min-width: 90px; }}
.combo-group label {{ display: flex; align-items: center; gap: 4px; color: var(--text-secondary); }}
.combo-controls {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
.combo-controls label {{ display: flex; align-items: center; gap: 4px; color: var(--text-secondary); }}
.combo-controls input[type="number"] {{ font: inherit; padding: 2px 4px; border: 1px solid var(--border); border-radius: 4px; background: var(--surface); color: var(--text-primary); }}
.combo-controls button {{ font: inherit; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text-primary); cursor: pointer; }}
.combo-controls button:hover {{ border-color: var(--accent); }}
.combo-controls button:disabled {{ opacity: .6; cursor: default; }}
.combo-status {{ color: var(--text-tertiary); }}

.dup-details {{ margin-top: 12px; }}
.dup-summary {{
  cursor: pointer; list-style: none; user-select: none;
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  padding: 11px 14px; background: var(--surface-alt);
  border: 1px solid var(--border); border-radius: 10px;
}}
.dup-summary::-webkit-details-marker {{ display: none; }}
.dup-summary::before {{ content: '▸'; font-size: 11px; color: var(--text-tertiary); transition: transform .15s; }}
.dup-details[open] > .dup-summary::before {{ transform: rotate(90deg); }}
.dup-details[open] > .dup-summary {{ border-radius: 10px 10px 0 0; border-bottom-color: transparent; }}
.dup-summary:hover {{ color: var(--text-primary); }}
.dup-summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.dup-body {{
  border: 1px solid var(--border); border-top: none; border-radius: 0 0 10px 10px;
  padding: 12px 14px 2px; background: var(--bg);
}}
.dup-body .flow-card:last-child {{ margin-bottom: 10px; }}
@media (prefers-reduced-motion: reduce) {{
  .dup-summary::before {{ transition: none; }}
}}

.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.data-table th {{ text-align: left; font-weight: 600; font-size: 11.5px; color: var(--text-secondary); background: var(--surface-alt); padding: 8px 10px; border-bottom: 1px solid var(--border); }}
.data-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
.data-table tr:last-child td {{ border-bottom: none; }}
.data-table .num {{ text-align: right; }}
.table-scroll {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }}
.table-scroll .data-table {{ border: none; }}
.table-scroll th:first-child, .table-scroll td:first-child {{ padding-left: 14px; }}

.checkpoints {{ list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }}
.checkpoint {{ display: flex; gap: 10px; align-items: flex-start; background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--sem-info); border-radius: 8px; padding: 10px 14px; }}
.checkpoint-error {{ border-left-color: var(--sem-destructive); }}
.checkpoint-ambiguous {{ border-left-color: var(--sem-mutating); }}
.cp-kind {{ font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: var(--text-tertiary); padding-top: 2px; white-space: nowrap; }}
.cp-msg {{ margin: 0; font-size: 13.5px; }}
.cp-detail {{ margin: 4px 0 0; font-size: 11.5px; color: var(--text-tertiary); white-space: pre-wrap; }}

.empty {{ color: var(--text-tertiary); font-size: 13.5px; font-style: italic; }}
footer {{ margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--border); font-size: 12px; color: var(--text-tertiary); }}
footer .mono {{ color: var(--text-tertiary); }}
a {{ color: var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">FlowScout · run report</div>
  <h1>{_esc(run.project)}</h1>
  <p class="subhead">Crawled <span class="mono">{_esc(run.start_url)}</span> — {_esc(run.started_at)} to {_esc(run.finished_at)}</p>
  <div class="meta-row">
    <span class="meta-item"><b>max depth</b> {_esc(limits.get('max_depth'))}</span>
    <span class="meta-item"><b>max breadth/state</b> {_esc(limits.get('max_breadth_per_state'))}</span>
    <span class="meta-item"><b>max states</b> {_esc(limits.get('max_states'))}</span>
    <span class="meta-item"><b>max repeats/action</b> {_esc(limits.get('max_action_repeat', 2))}</span>
    <span class="meta-item"><b>mutating actions</b> {'allowed' if cfg.get('allow_mutating') else 'withheld'}</span>
  </div>

  <div class="metrics">
    <div class="metric"><div class="num">{s['states_discovered']}</div><div class="lbl">states discovered</div></div>
    <div class="metric"><div class="num">{s['flows_total']}</div><div class="lbl">flows walked</div></div>
    <div class="metric"><div class="num">{s['flows_unique']}</div><div class="lbl">unique flows</div></div>
    <div class="metric"><div class="num">{s['flows_duplicate']}</div><div class="lbl">duplicates found</div></div>
    <div class="metric"><div class="num">{s['flows_blocked']}</div><div class="lbl">blocked flows</div></div>
    <div class="metric"><div class="num">{s['checkpoints']}</div><div class="lbl">checkpoints raised</div></div>
    <div class="metric"><div class="num">{s['convergence_merges']}</div><div class="lbl">state-convergence merges</div></div>
    <div class="metric"><div class="num">{s['semantic_merges']}</div><div class="lbl">semantic merges</div></div>
    <div class="metric"><div class="num">{s['handler_discovered_total']}</div><div class="lbl">controls found via handler detection</div></div>
    <div class="metric"><div class="num">{s['disabled_interactive_total']}</div><div class="lbl">real controls found disabled</div></div>
    <div class="metric"><div class="num">{s['unclassified_interactive_total']}</div><div class="lbl">unverified (CDP fallback)</div></div>
  </div>
  {change_html}

  <h2>Flows</h2>
  <p class="subhead">Every root-to-leaf path the crawler walked, in discovery order. <b style="color:var(--sem-safe)">Unique</b> flows introduce a new sequence of action types; <b style="color:var(--sem-mutating)">duplicates</b> repeat a known sequence — via an identical normalized action sequence, an identical resulting application state reached by a different path (exact, verified — see the note below each card), or high text similarity between different-ending flows (embeddings, lower confidence, worth a second look); <b style="color:var(--sem-destructive)">blocked</b> flows dead-ended because the risk policy withheld the only remaining actions, or an error occurred.</p>
  <p class="subhead">Dedup beyond exact-sequence match: {_esc(run.semantic_dedup_status)}</p>
  {resume_all_html}
  {flows_html}
  {gap_html}

  <h2>Safety register</h2>
  <p class="subhead">Actions the crawler discovered but never clicked, and why. This is the audit trail proving the risk policy held for the whole run.</p>
  <div class="table-scroll">{skipped_html}</div>

  <h2>Checkpoints</h2>
  <p class="subhead">Events that would page a human operator in the full product: unexpected errors, and replay landing somewhere other than expected.</p>
  {checkpoints_html}

  <h2>State graph</h2>
  <p class="subhead">Every distinct UI state found, fingerprinted by normalized URL + the set of interactive elements present (not raw DOM/URL, which would treat every product page as a new state). "Actions" includes controls found via handler detection, noted in parens — see below. The last column is the CDP-fallback count — see Coverage gaps below, normally zero.</p>
  <div class="table-scroll">
    <table class="data-table">
      <thead><tr><th>Fingerprint</th><th>URL pattern</th><th>Title</th><th class="num">Actions</th><th>Risk mix</th><th class="num">Coverage gaps</th></tr></thead>
      <tbody>{states_html}</tbody>
    </table>
  </div>

  <h2>Test a parameter combination</h2>
  <p class="subhead">The crawler only ever changes one checkbox/radio/select at a time on its own — a section gated behind SEVERAL of these set together is invisible to it (see ROADMAP.md's own documented limitation). Pick a value per control below and submit them together; if that combination reveals something new, exploration continues automatically from there.</p>
  {combination_html}

  <h2>Handler-discovered controls</h2>
  <p class="subhead">Div-as-button elements (no semantic markup our discovery selector matches) that CDP confirmed have a real click listener attached, and were clicked like any other candidate. Framework-agnostic — this works the same whether the click handler is a raw addEventListener or a React onClick prop.</p>
  <div class="table-scroll">{handler_discovered_html}</div>

  <h2>Disabled controls found</h2>
  <p class="subhead">Real controls (CDP confirmed a click listener) that were disabled at the time — correctly never clicked, but worth knowing they exist: a wizard option gated behind an earlier choice, for example.</p>
  <div class="table-scroll">{disabled_html}</div>

  <h2>Coverage gaps</h2>
  <p class="subhead">Only populated if CDP verification itself failed on some state — a fallback to the old cursor:pointer/role/tabindex guess, so a CDP failure degrades to previously-shipped behavior rather than silently promoting nothing. Expected to be empty on a normal run; anything listed here is itself worth investigating (why did CDP fail on this state?), not just a blind spot to shrug at.</p>
  <div class="table-scroll">{coverage_gaps_html}</div>

  <footer>
    Generated by FlowScout M0 · config: <span class="mono">{_esc(json.dumps({k: v for k, v in cfg.items() if k != 'credentials'}))}</span>
  </footer>
</div>
{_resume_script_html() if run_id else ""}
</body>
</html>"""
