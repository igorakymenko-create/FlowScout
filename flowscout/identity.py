"""Stable flow identity, valid *across separate crawl runs*.

Every other identifier attached to a Flow is per-run and useless for
this: `flow.id` restarts at 1 on every crawl (flow #12 today has nothing
to do with flow #12 tomorrow), and `end_state_fp` is an exact DOM-content
fingerprint that's sensitive to state that varies run to run for
uninteresting reasons -- measured on a real saucedemo-wide run,
`cart.html` alone produced 8 distinct fingerprints purely from item-count
differences. A cross-run identity has to be coarser than that: what the
flow *accomplishes* (destination page + which state-changing actions it
performed), not what the DOM looked like at the instant it was crawled.

Chosen anchor: (end url_pattern, frozenset of mutating action
signatures). Validated against real data before committing to it --
decomposed saucedemo's checkout chain into exactly the three milestones
a human would name it by:

    /checkout-step-one.html   + {checkout}
    /checkout-step-two.html   + {checkout, continue}
    /checkout-complete.html   + {checkout, continue, finish}

and, for free, gives the "is this flow actually worth a test case"
signal used elsewhere: an empty mutating set means the flow is pure
navigation -- a shared-step/precondition candidate, not a test case.

Known imprecision (found by running it, not designed away -- documented
so it isn't rediscovered as a surprise later):

- Too coarse in one place: adding 1 vs 2 items to a cart both normalize
  to the same "add-to-cart-*" signature and collapse to one identity.
- Too sensitive in another: the same user-facing action can carry a
  different signature depending on which page it's performed from
  (saucedemo's product-detail "Add to cart" is data-test="add-to-cart";
  the listing page's is "add-to-cart-sauce-labs-backpack", normalizing
  differently). A refactor that unified those attributes would read as
  a changed flow when nothing behavioral changed.

**Anchor widened to include is_choice actions (Aug 2026), not just
risk == MUTATING.** Found via a real, structural gap: picking one
wizard option over another (Site B's Option A/Option B cards; a
`<select>` sort order) changes what the flow *accomplishes* just as
much as adding something to a cart does, but carries no server-side
state-change risk worth gating behind `allow_mutating` -- so it was
correctly classified SAFE by risk.py, and just as correctly invisible
to an identity anchor keyed on risk alone. Two flows picking different
wizard options landed on the same `mutating_signature_set` (empty, in
both cases) and collapsed into one identity, even though the crawler
had, by that point, actually clicked through to two different results.
`Transition.is_choice` (set at discovery time -- see actions.py) is
deliberately a separate flag from `risk`, not a new risk tier: it
answers "does this pick one of several alternatives", not "is this
safe to click without an operator opt-in" -- conflating the two would
have made `allow_mutating=false` silently stop exploring wizard
options, an unrelated and unwanted side effect.
"""
from __future__ import annotations

import hashlib

from .models import Flow, Risk, StateNode


def mutating_signature_set(flow: Flow) -> frozenset[str]:
    """The set of actions a flow performed that make it a genuinely
    different flow from one that didn't perform them: state-changing
    ones (add to cart, checkout, finish, risk == MUTATING) and choice
    ones (picked one of several alternatives, is_choice -- a wizard
    option, a sort order). Two flows that took a *different* set of
    these are, by definition, different tests -- one of them did
    something, or chose something, the other didn't -- no matter how
    similar their text reads. (Originally written for semantic_dedup's
    tier-2 false-merge guard; identity.py is now the one home for it.)"""
    return frozenset(t.action_norm_signature for t in flow.transitions
                      if t.risk == Risk.MUTATING or t.is_choice)


def content_hash(flow: Flow) -> str:
    """Hash of the flow's full normalized action sequence -- the thing
    that IS allowed to change under a stable identity. Two records with
    the same identity but a different content_hash means the *path*
    changed even though what the flow accomplishes didn't."""
    seq = "|".join(t.action_norm_signature for t in flow.transitions)
    return hashlib.sha256(seq.encode("utf-8")).hexdigest()[:16]


def flow_identity(flow: Flow, states: dict[str, StateNode]) -> str:
    """Stable cross-run identity. See module docstring for why this
    anchor and not end_state_fp.

    Includes `flow.persona` (Aug 2026, multi-persona crawling): two
    personas reaching the same destination having performed the same
    actions are NOT the same flow -- what a persona was *allowed* to do
    getting there is exactly the thing multi-persona crawling exists to
    tell apart (an admin-only page an admin can reach and a standard
    user can't must never collapse into "one flow", regardless of how
    identical the rest of the anchor looks). Single-persona runs (no
    "personas" in config) all carry persona="default", so this is a
    no-op for every run that predates the feature."""
    end_state = states.get(flow.end_state_fp)
    url_pattern = end_state.url_pattern if end_state else "unknown"
    mutations = mutating_signature_set(flow)
    raw = flow.persona + "|" + url_pattern + "|" + "|".join(sorted(mutations))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_shared_step_candidate(flow: Flow) -> bool:
    """No mutating actions at all -> pure navigation. Not a test case by
    itself; more likely a precondition (login, opening a menu) that
    recurs across many flows. Free byproduct of the identity anchor:
    this is exactly the flows whose mutating_signature_set is empty."""
    return len(mutating_signature_set(flow)) == 0
