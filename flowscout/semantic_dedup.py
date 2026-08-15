"""Post-structural dedup, in two tiers, run over whatever flows are
still marked UNIQUE after crawler.py's structural pass (identical
normalized action sequence).

Tier 1 -- state convergence (free, exact, always runs). Two flows that
reach the *identical* application state -- same fingerprint, the one
already computed during crawling -- are duplicates, full stop, no
matter how different their paths look. This is ground truth about the
*destination*, but not the whole story about what the two paths did to
get there -- see the mutating-set guard below, added after finding it
merged away real actions.

**Found on saucedemo-wide, not hypothetical: state convergence was
silently discarding mutating actions.** `end_state_fp` is built from
the destination page's *interactive-candidate signatures*, which
doesn't change just because a flow also removed a cart item or reset
app state on the way there -- both land on a page with the same
controls as a flow that only navigated. Measured effect on a real run:
`remove` (cart item removal) and `reset-sidebar-link` (Reset App State,
clears the cart) never appeared in ANY unique flow's output -- every
occurrence got merged into a shorter duplicate that never performed
them. That's not lost coverage, it's a false negative reported as fact:
gap analysis (M2) would tell an operator "the app doesn't do this" when
the crawl did it and threw the result away. Fix: state convergence now
also requires two flows to share the same `mutating_signature_set` --
reusing the exact guard tier 2 below already had for the same reason --
before treating them as the same flow. `end_state_fp` (the destination)
still decides whether two flows are candidates for merging at all;
`mutating_signature_set` decides whether they actually did the same
thing to get there.

Tier 2 -- semantic (Gemini embeddings, needs GEMINI_API_KEY). Catches
flows that reach *different* states but plausibly represent the same
kind of user intent. This tier is inherently fuzzier and was tuned
empirically after a real bug: comparing full step-by-step transcripts
biases similarity toward whatever prefix two flows happen to share
(e.g. every flow here starts "Login -> Open Menu -> ..."), which
produced false merges between flows that don't even end in the same
state (verified against tier 1's fingerprints). Running tier 1 first
removes the cases embeddings get wrong for free, and only the flows
that *don't* trivially converge reach the embedding comparison.

Even so, tier 2 got a real case wrong on a wider crawl: a flow that
completed checkout (clicked "Finish", landed on the order-confirmation
page) scored 97% similar to one that stopped a step earlier on the
review page and never merged. Both traverse near-identical text up to
that point, so the embedding doesn't weight the difference heavily
enough. Fix: tier 2 now additionally requires two flows to have
performed the *exact same set* of mutating (state-changing) actions
before their text similarity is even considered -- a flow that clicked
"Finish" can never be merged into one that didn't, regardless of score.
"""
from __future__ import annotations

import re

from . import embeddings
from .embeddings import EmbeddingsUnavailable, cosine_similarity
from .fingerprint import human_page_label
from .identity import mutating_signature_set
from .models import Flow, FlowStatus, RunResult

DEFAULT_THRESHOLD = 0.95


def _flow_text(flow: Flow, states: dict) -> str:
    """Text representation of a flow for embedding: page + action per
    step, so the embedding captures *where and what*, not just raw click
    labels (which alone can be too terse to carry intent)."""
    parts = []
    for t in flow.transitions:
        from_state = states.get(t.from_fp)
        page = human_page_label(from_state.url_pattern) if from_state else "unknown page"
        parts.append(f"On {page}: {t.action_label}")
    return " -> ".join(parts)


def _apply_state_convergence(run: RunResult) -> int:
    """Merge UNIQUE flows that share an end_state_fp AND performed the
    same set of mutating actions to get there. Representative = the
    shortest path to that state (simplest reproduction) among flows
    with that mutating set.

    Keyed on (persona, end_state_fp, mutating_signature_set) rather than
    end_state_fp alone -- two flows landing on the same page having
    taken different state-changing actions along the way (one removed
    a cart item, one didn't) are not the same flow just because the
    destination looks identical. See module docstring for the real
    case this was found from. `persona` (Aug 2026, multi-persona
    crawling) guards the same way: an admin and a standard user
    reaching an identical-looking state must never merge into one flow,
    since which persona got there is exactly what's under test -- see
    identity.py's flow_identity for the same reasoning."""
    by_key: dict[tuple[str, str, frozenset[str]], Flow] = {}
    merged = 0
    for flow in [f for f in run.flows if f.status == FlowStatus.UNIQUE]:
        key = (flow.persona, flow.end_state_fp, mutating_signature_set(flow))
        rep = by_key.get(key)
        if rep is None:
            by_key[key] = flow
            continue
        demote, keep = (flow, rep) if len(flow.transitions) >= len(rep.transitions) else (rep, flow)
        by_key[key] = keep
        demote.status = FlowStatus.DUPLICATE
        demote.duplicate_of = keep.id
        demote.dedup_reason = (
            f"State convergence: reaches the exact same application state as flow #{keep.id} "
            f"having performed the same mutating actions (fingerprint match) via a different "
            f"path — verified from the state graph, not inferred"
        )
        merged += 1
    return merged


def _flatten_duplicate_chains(run: RunResult) -> None:
    """Tier 1 keeps a *rolling* representative per end-state (whoever has
    the shortest path so far), so a flow demoted early can later point
    at a representative that itself gets dethroned by an even shorter
    one -- e.g. #4 -> #3 -> #31, when #4 only needs to know about #31.
    Re-point every duplicate directly at its chain's final (non-
    duplicate) root, and fix up the id mentioned in its reason text."""
    by_id = {f.id: f for f in run.flows}
    for flow in run.flows:
        if flow.status != FlowStatus.DUPLICATE or flow.duplicate_of is None:
            continue
        seen = {flow.id}
        root = by_id[flow.duplicate_of]
        while root.status == FlowStatus.DUPLICATE and root.duplicate_of is not None and root.id not in seen:
            seen.add(root.id)
            root = by_id[root.duplicate_of]
        if root.id != flow.duplicate_of:
            flow.duplicate_of = root.id
            flow.dedup_reason = re.sub(r"flow #\d+", f"flow #{root.id}", flow.dedup_reason, count=1)


def apply_semantic_dedup(run: RunResult, threshold: float = DEFAULT_THRESHOLD) -> None:
    """Mutates run.flows in place: some UNIQUE flows may become DUPLICATE.
    Always sets run.semantic_dedup_status so the report can say plainly
    what ran and what didn't, and why.

    Provider (Aug 2026): read from run.config["embeddings_provider"],
    defaulting to Gemini -- see embeddings.py's module docstring for why
    this exists and what "unverified" means for openai/voyage. `threshold`
    stays a plain parameter, not provider-aware, on purpose: it was
    calibrated for Gemini specifically (see this module's own docstring),
    so a caller switching provider has to consciously pass their own
    value rather than silently inherit a number tuned for a different
    embedding space."""
    provider = run.config.get("embeddings_provider")
    convergence_merged = _apply_state_convergence(run)
    _flatten_duplicate_chains(run)
    unique_flows = [f for f in run.flows if f.status == FlowStatus.UNIQUE]

    if not embeddings.api_key_configured(provider):
        key_env = embeddings.provider_status()[provider or embeddings.DEFAULT_PROVIDER]["key_env"]
        run.semantic_dedup_status = (
            f"state convergence: {convergence_merged} merged (exact fingerprint match); "
            f"semantic (embeddings): skipped, {key_env} not set"
        )
        return
    if len(unique_flows) < 2:
        run.semantic_dedup_status = (
            f"state convergence: {convergence_merged} merged; "
            f"semantic: skipped, fewer than 2 flows left to compare"
        )
        return

    representatives: list[tuple[Flow, list[float], frozenset[str]]] = []
    semantic_merged = 0
    try:
        for flow in unique_flows:
            vec = embeddings.embed_text(_flow_text(flow, run.states), provider=provider)
            mutations = mutating_signature_set(flow)
            best_flow, best_score = None, 0.0
            for rep_flow, rep_vec, rep_mutations in representatives:
                if rep_mutations != mutations:
                    continue  # different state-changing actions taken -> not the same test
                if rep_flow.persona != flow.persona:
                    continue  # different persona -> not the same test, however similar the text
                score = cosine_similarity(vec, rep_vec)
                if score > best_score:
                    best_flow, best_score = rep_flow, score
            if best_flow is not None and best_score >= threshold:
                flow.status = FlowStatus.DUPLICATE
                flow.duplicate_of = best_flow.id
                flow.dedup_reason = (
                    f"Semantic dedup: {best_score:.0%} similar to flow #{best_flow.id} "
                    f"despite ending in a different state ({embeddings.model_name(provider)}, "
                    f"cosine ≥ {threshold:.0%}) — text-similarity based, review recommended"
                )
                semantic_merged += 1
            else:
                representatives.append((flow, vec, mutations))
    except EmbeddingsUnavailable as exc:
        _flatten_duplicate_chains(run)  # tier 2 may have demoted some tier-1 representatives before failing
        run.semantic_dedup_status = (
            f"state convergence: {convergence_merged} merged; semantic: error after partial run: {exc}"
        )
        return

    _flatten_duplicate_chains(run)  # tier 2 can demote flows tier 1 already pointed duplicates at
    run.semantic_dedup_status = (
        f"state convergence: {convergence_merged} merged (exact fingerprint match); "
        f"semantic: {len(unique_flows)} compared, {semantic_merged} merged "
        f"(embeddings, threshold {threshold:.0%})"
    )
