"""Gap analysis: match discovered flows against an existing TCMS export.

This is the point the whole spec argues is FlowScout's actual
differentiator -- every competitor outputs tests; this outputs *where
your test plan and your application disagree*. Two directions, both
useful on their own:

* flow -> TCMS: flows the crawler found with nothing resembling them in
  the test plan ("gap" -- undocumented/untested behavior).
* TCMS -> flow: test cases with nothing resembling them among the
  discovered flows ("not_found" -- could be a stale test for behavior
  that no longer exists, a flow this crawl's risk policy or limits
  didn't reach, or the crawler missing something -- the report doesn't
  guess which, a human has to look).

**Matching is action-level for mutating behavior, flow-level for pure
navigation (Aug 2026 rewrite).** Originally this compared whole-flow
transcripts against TCMS text, which meant a flow doing several
mutating things (add-to-cart then remove, say) got exactly one verdict
for the whole flow -- so it could be "covered" while one of its actions
was, in reality, untested. Confirmed on a real run before rewriting
this: `reset-sidebar-link` correctly matched no test case when it was a
flow's *only* action, but hid inside a "covered" verdict when the same
action appeared alongside `add-to-cart` in a different flow matched to
the add-to-cart test -- the identical real action reported as both a
gap and covered depending which flow happened to carry it.

Fix, two pools instead of one:

1. **Mutating-action pool** -- one entry per distinct
   `action_norm_signature` with `risk == MUTATING` across all unique
   flows (deduplicated: the same action shared by many flows costs one
   embedding, not one per flow). Each flow's status is *derived* from
   its own actions' individual verdicts: "covered" only if every one
   matched something, "partial" if some did and some didn't (see
   `FlowCoverage.action_matches` for exactly which), "gap" if none did.
2. **Navigation-flow pool** -- flows with an *empty* mutating set
   (pure browsing) aren't independently testable the way a mutating
   action is, so they can't be decomposed the same way. They keep the
   original whole-flow-transcript representation (destination page +
   non-boilerplate steps), but only when there IS non-boilerplate
   content -- a flow that's 100% boilerplate (e.g. "Login > open menu >
   close menu") gets a distinct "navigation" status and is excluded
   from matching entirely, rather than padded out with generic filler
   text and risking a spurious match (the exact "trivial no-op flagged
   as a gap" behavior M4's own testing called out).

**Why two pools and not one widened pool.** Tried folding non-boilerplate
*safe* actions into the mutating pool first (broadening "capability" to
mean "mutating OR meaningfully-labeled", not just "mutating"). Measured
live, this did NOT cleanly work: safe navigational actions carry only
their *origin*-page context ("On Inventory: Open <item>"), while a TCMS
item like "View shopping cart contents" is really about the
*destination* the flow ends up at -- so an action-level text lost
exactly the signal ("Ends on Cart") that made the old flow-level match
correct in the first place, AND introduced a false positive (an
item-detail-view action top-matched "Login with valid credentials" at
0.7463, just clearing a threshold, for no real semantic reason). Kept
the two representations separate instead of forcing a fix that measurably
made things worse.

Mutating-action verdicts are computed first, since that pool's
similarity margins are the widest and cleanest (see threshold note
below); navigation flows are then matched only against whichever TCMS
items the action pool didn't already claim, so a well-earned action
match can't be quietly stolen by a fuzzier navigation-text guess.

This also means *fewer* embedding calls on a typical crawl than the old
scheme, not more: saucedemo-wide's 27 unique flows reduce to 9 distinct
mutating actions plus a handful of genuinely-distinct navigation
flows, since the same action (e.g. "checkout") is shared by many flows
and only needs one embedding regardless of how many flows contain it.

Both pools' matching is symmetric nearest-neighbor by embedding cosine
similarity, run separately in each direction -- but only for
actions/flows/TCMS items whose owning flow doesn't already have a
confirmed pairing in project state (see identity.py, project_state.py).
A human's prior confirmation is about the whole flow and is ground
truth for it; it isn't re-guessed, spends no embedding call, and marks
every one of that flow's actions covered by that confirmation.

**Threshold (0.74) recalibrated for the new action pool, and verified to
also work for the (unchanged) navigation-flow pool -- not assumed to
carry over.** Reusing the old flow-level 0.75 blindly for the new
action pool would have been exactly the mistake M1/M2 already warn
about when switching representations -- a different text has a
different similarity distribution, and 0.75 would have misclassified
real matches here (`remove` scored 0.7498, `remove-*` scored 0.7475,
both below the old threshold). Measured live against
`fixtures/tcms_saucedemo.csv` and the saucedemo-wide action pool: every
action whose real TCMS counterpart was its top match scored in
0.7475-0.8478; every genuinely-uncovered action's best (wrong) match
topped out at 0.7307 -- 0.74 sits cleanly in that gap. Separately
checked the *navigation*-flow pool (unchanged representation from the
original M2 calibration, which found genuine matches at 0.756-0.822 and
genuinely-absent ones at 0.646-0.653): 0.74 falls inside that gap too,
so one threshold serves both pools without compromising either --
convenient, and checked rather than assumed.
Small samples (9 actions, handful of nav flows) -- same caveat M2's
original calibration had at a similar size, worth re-checking as more
real crawls accumulate.

**Still not fixed by this, honestly:** order/precondition text ("remove
an item *that was previously added*") isn't captured at the action
level any better than it was at the flow level -- a flow that removes
without ever adding would still match the same way. Per-page signature
aliasing (`add-to-cart` vs `add-to-cart-*`, the same user action
performed from different pages -- see identity.py's own noted
imprecision) is counted as two separate capabilities here rather than
one, double-booking both the embedding cost and the operator's
attention. And "view this specific page" style TCMS items (the
navigation pool) are still matched by a coincidental-feeling whole-flow
embedding rather than anything that actually reasons about page
identity -- workable, not principled. Not solved -- flagged, same as
everywhere else in this project that can't claim more than it actually
knows.
"""
from __future__ import annotations

from . import embeddings
from .embeddings import EmbeddingsUnavailable, cosine_similarity
from .fingerprint import human_page_label
from .identity import flow_identity, mutating_signature_set
from .models import Flow, FlowCoverage, FlowStatus, GapAnalysis, Risk, RunResult, TcmsCoverage
from .tcms import TcmsItem

DEFAULT_THRESHOLD = 0.74
# See module docstring for how this was measured (both pools, live).

# Steps every flow shares regardless of what it's actually testing (log
# in, toggle the hamburger menu). Fine as *context* for a human reading
# the flow card, but poison for TCMS matching specifically: a human-
# written test title never mentions "log in" (it's an implicit
# precondition) -- weighting it equally with the rest of the transcript
# makes every flow's embedding gravitate toward whichever TCMS item
# happens to read most generically (empirically, "Login with valid
# credentials" won the top match for 9 of 11 flows before this filter
# existed).
_BOILERPLATE_PREFIXES = ('Fill form and submit "Login"',)
_BOILERPLATE_SUBSTRINGS = ("hamburger menu",)


def _is_boilerplate(label: str) -> bool:
    return label.startswith(_BOILERPLATE_PREFIXES) or any(s in label for s in _BOILERPLATE_SUBSTRINGS)


def _action_pool_from(flows: list[Flow], states: dict) -> tuple[dict[str, str], dict[str, list[int]]]:
    """One representative text per distinct mutating action_norm_signature
    across `flows`, plus which flow ids perform each one. First
    occurrence wins the text (signatures are already page-scoped by
    construction in the common case -- see identity.py)."""
    text: dict[str, str] = {}
    owners: dict[str, list[int]] = {}
    for f in flows:
        for t in f.transitions:
            # Same criterion as identity.py's mutating_signature_set --
            # a choice (wizard option, sort order) is a distinct
            # capability worth its own TCMS comparison, same as a
            # state-changing action, even though it's risk == SAFE.
            if not (t.risk == Risk.MUTATING or t.is_choice):
                continue
            sig = t.action_norm_signature
            owners.setdefault(sig, []).append(f.id)
            if sig not in text:
                from_state = states.get(t.from_fp)
                page = human_page_label(from_state.url_pattern) if from_state else "unknown page"
                text[sig] = f"On {page}: {t.action_label}"
    return text, owners


def _nav_flow_text(flow: Flow, states: dict) -> str | None:
    """Whole-flow representation for a pure-navigation flow (no mutating
    actions) -- destination page plus whatever non-boilerplate steps it
    took. None if the flow is 100% boilerplate (nothing distinguishing
    to embed): matching that against TCMS text would only ever produce
    a coincidental score, not a meaningful one."""
    end_state = states.get(flow.end_state_fp)
    end_page = human_page_label(end_state.url_pattern) if end_state else "unknown page"
    nav = [t.action_label for t in flow.transitions if not _is_boilerplate(t.action_label)]
    if not nav:
        return None
    return f"Ends on {end_page}. Actions taken: {'; '.join(nav)}."


def _match_pool(pool_vecs: dict, tcms_pool: list[TcmsItem], tcms_vecs: dict,
                 threshold: float) -> tuple[dict, dict]:
    """Bidirectional nearest-neighbor between one pool (action
    signatures, or flow ids for the navigation pool) and a set of TCMS
    items. Returns (verdict: key -> (status, tcms_id, tcms_title,
    score), tcms_matches: tcms_id -> (key, score) for items this pool
    covers)."""
    verdict = {}
    for key, vec in pool_vecs.items():
        best_id, best_title, best_score = None, None, 0.0
        for t in tcms_pool:
            score = cosine_similarity(vec, tcms_vecs[t.id])
            if score > best_score:
                best_id, best_title, best_score = t.id, t.title, score
        covered = best_score >= threshold
        verdict[key] = ("covered" if covered else "gap",
                         best_id if covered else None, best_title if covered else None,
                         round(best_score, 4))

    tcms_matches = {}
    for t in tcms_pool:
        best_key, best_score = None, 0.0
        for key, vec in pool_vecs.items():
            score = cosine_similarity(tcms_vecs[t.id], vec)
            if score > best_score:
                best_key, best_score = key, score
        if best_score >= threshold:
            tcms_matches[t.id] = (best_key, round(best_score, 4))
    return verdict, tcms_matches


def analyze_gaps(run: RunResult, tcms_items: list[TcmsItem], tcms_source: str,
                  threshold: float = DEFAULT_THRESHOLD, project_state=None) -> GapAnalysis:
    # Provider (Aug 2026): see semantic_dedup.py's own note on the same
    # pattern -- run.config["embeddings_provider"], defaulting to Gemini.
    # `threshold` (DEFAULT_THRESHOLD = 0.74) was calibrated for Gemini
    # specifically; switching provider without an explicit threshold
    # inherits a number that was never measured for that provider's
    # similarity distribution.
    provider = run.config.get("embeddings_provider")
    unique_flows = [f for f in run.flows if f.status == FlowStatus.UNIQUE]

    if not unique_flows:
        return GapAnalysis(tcms_source=tcms_source, threshold=threshold,
                            status="skipped: no unique flows to compare")
    if not tcms_items:
        return GapAnalysis(tcms_source=tcms_source, threshold=threshold,
                            status="skipped: TCMS file had no usable rows")

    tcms_by_id = {t.id: t for t in tcms_items}
    identities = {f.id: flow_identity(f, run.states) for f in unique_flows}
    mutations = {f.id: sorted(mutating_signature_set(f)) for f in unique_flows}

    flow_coverage: dict[int, FlowCoverage] = {}
    tcms_coverage: dict[str, TcmsCoverage] = {}

    # Tier 0: operator-confirmed pairings from a previous run. Free (no
    # embedding call), exact (no threshold), and takes both sides of the
    # pairing out of the fuzzy pools below so a strong-but-wrong match
    # elsewhere on this run can't steal either side of it. A confirmation
    # is about the whole flow, so every one of its actions is marked
    # covered by it too -- not re-guessed at the action level.
    if project_state is not None:
        for f in unique_flows:
            rec = project_state.flows.get(identities[f.id])
            if rec and rec.tcms_id and rec.tcms_id in tcms_by_id:
                title = tcms_by_id[rec.tcms_id].title
                acts = mutations[f.id]
                flow_coverage[f.id] = FlowCoverage(
                    flow_id=f.id, status="covered", matched_tcms_id=rec.tcms_id, matched_tcms_title=title,
                    score=1.0, identity=identities[f.id], confirmed=True, mutating_actions=acts,
                    action_matches=[{"action": a, "matched_tcms_id": rec.tcms_id,
                                      "matched_tcms_title": title, "score": 1.0, "covered": True} for a in acts],
                )
                tcms_coverage[rec.tcms_id] = TcmsCoverage(
                    tcms_id=rec.tcms_id, tcms_title=title, status="covered", matched_flow_id=f.id,
                    score=1.0, confirmed=True,
                )
    confirmed_n = len(flow_coverage)

    remaining_flows = [f for f in unique_flows if f.id not in flow_coverage]
    remaining_tcms = [t for t in tcms_items if t.id not in tcms_coverage]

    action_flows = [f for f in remaining_flows if mutations[f.id]]
    nav_flows = [f for f in remaining_flows if not mutations[f.id]]

    nav_text: dict[int, str] = {}
    for f in nav_flows:
        txt = _nav_flow_text(f, run.states)
        if txt is None:
            flow_coverage[f.id] = FlowCoverage(
                flow_id=f.id, status="navigation", matched_tcms_id=None, matched_tcms_title=None,
                score=0.0, identity=identities[f.id], mutating_actions=[],
            )
        else:
            nav_text[f.id] = txt

    action_text, action_owners = _action_pool_from(action_flows, run.states)

    action_verdict: dict[str, tuple] = {}
    nav_verdict: dict[int, tuple] = {}
    fuzzy_notes = []

    if not (action_text or nav_text):
        for t in remaining_tcms:
            tcms_coverage[t.id] = TcmsCoverage(t.id, t.title, "not_found", None, 0.0)
    elif not embeddings.api_key_configured(provider):
        key_env = embeddings.provider_status()[provider or embeddings.DEFAULT_PROVIDER]["key_env"]
        fuzzy_notes.append(f"{len(action_text)} action(s)/{len(nav_text)} nav flow(s) vs "
                            f"{len(remaining_tcms)} test(s) not compared: {key_env} not set")
        for sig in action_text:
            action_verdict[sig] = ("gap", None, None, 0.0)
        for fid in nav_text:
            nav_verdict[fid] = ("gap", None, None, 0.0)
        for t in remaining_tcms:
            tcms_coverage[t.id] = TcmsCoverage(t.id, t.title, "not_found", None, 0.0)
    else:
        try:
            action_vecs = {sig: embeddings.embed_text(text, provider=provider) for sig, text in action_text.items()}
            nav_vecs = {fid: embeddings.embed_text(text, provider=provider) for fid, text in nav_text.items()}
            tcms_vecs = {t.id: embeddings.embed_text(t.text(), provider=provider) for t in remaining_tcms}
        except EmbeddingsUnavailable as exc:
            return GapAnalysis(tcms_source=tcms_source, threshold=threshold, status=f"error: {exc}")

        # Pass 1: mutating actions, the tightest-calibrated pool, goes
        # first and gets first claim on TCMS items.
        action_verdict, action_tcms_matches = _match_pool(action_vecs, remaining_tcms, tcms_vecs, threshold)
        for tid, (sig, score) in action_tcms_matches.items():
            tcms_coverage[tid] = TcmsCoverage(
                tcms_id=tid, tcms_title=tcms_by_id[tid].title, status="covered",
                matched_flow_id=min(action_owners[sig]), score=score,
            )

        # Pass 2: navigation flows, only against whatever's still unclaimed.
        remaining_tcms_after_actions = [t for t in remaining_tcms if t.id not in tcms_coverage]
        if nav_vecs and remaining_tcms_after_actions:
            nav_tcms_vecs = {t.id: tcms_vecs[t.id] for t in remaining_tcms_after_actions}
            nav_verdict, nav_tcms_matches = _match_pool(nav_vecs, remaining_tcms_after_actions, nav_tcms_vecs, threshold)
            for tid, (fid, score) in nav_tcms_matches.items():
                tcms_coverage[tid] = TcmsCoverage(
                    tcms_id=tid, tcms_title=tcms_by_id[tid].title, status="covered",
                    matched_flow_id=fid, score=score,
                )
        else:
            nav_verdict = {fid: ("gap", None, None, 0.0) for fid in nav_vecs}

        for t in remaining_tcms:
            if t.id not in tcms_coverage:
                tcms_coverage[t.id] = TcmsCoverage(t.id, t.title, "not_found", None, 0.0)

        fuzzy_notes.append(f"{len(action_text)} distinct mutating action(s) (from {len(action_flows)} flow(s)) "
                            f"and {len(nav_text)} navigation flow(s) vs {len(remaining_tcms)} test(s) "
                            f"compared (threshold {threshold:.0%})")

    trivial_nav_n = len(nav_flows) - len(nav_text)
    if trivial_nav_n:
        fuzzy_notes.append(f"{trivial_nav_n} navigation-only flow(s) excluded (no distinguishing content)")
    fuzzy_note = "; ".join(fuzzy_notes)

    # Derive each action-flow's status from its actions' individual verdicts.
    for f in action_flows:
        matches = []
        for sig in mutations[f.id]:
            status, tid, title, score = action_verdict.get(sig, ("gap", None, None, 0.0))
            matches.append({"action": sig, "matched_tcms_id": tid, "matched_tcms_title": title,
                             "score": score, "covered": status == "covered"})
        n_covered = sum(1 for m in matches if m["covered"])
        if n_covered == len(matches):
            flow_status = "covered"
        elif n_covered == 0:
            flow_status = "gap"
        else:
            flow_status = "partial"
        best = max(matches, key=lambda m: m["score"])
        flow_coverage[f.id] = FlowCoverage(
            flow_id=f.id, status=flow_status,
            matched_tcms_id=best["matched_tcms_id"], matched_tcms_title=best["matched_tcms_title"],
            score=best["score"], identity=identities[f.id], mutating_actions=mutations[f.id],
            action_matches=matches,
        )

    # Navigation flows with real content: single flow-level verdict, no
    # per-action decomposition (there's nothing to decompose).
    for fid, text in nav_text.items():
        status, tid, title, score = nav_verdict.get(fid, ("gap", None, None, 0.0))
        flow_coverage[fid] = FlowCoverage(
            flow_id=fid, status=status, matched_tcms_id=tid, matched_tcms_title=title,
            score=score, identity=identities[fid], mutating_actions=[],
        )

    # Restore original order (dict insertion put confirmed pairs first).
    flow_coverage_list = [flow_coverage[f.id] for f in unique_flows]
    tcms_coverage_list = [tcms_coverage[t.id] for t in tcms_items]

    gaps = sum(1 for x in flow_coverage_list if x.status == "gap")
    partial = sum(1 for x in flow_coverage_list if x.status == "partial")
    not_found = sum(1 for x in tcms_coverage_list if x.status == "not_found")
    status = f"ran: {len(unique_flows)} flows vs {len(tcms_items)} TCMS items"
    if confirmed_n:
        status += f", {confirmed_n} already confirmed"
    if fuzzy_note:
        status += f"; {fuzzy_note}"
    status += f" — {gaps} flow(s) undocumented, {partial} partially covered, {not_found} test(s) not found in the app"

    return GapAnalysis(tcms_source=tcms_source, threshold=threshold, status=status,
                        flow_coverage=flow_coverage_list, tcms_coverage=tcms_coverage_list)
