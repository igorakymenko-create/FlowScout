"""Shared-step extraction: the TestRail sense, where a common prologue
(login, opening a menu) is authored once and referenced from every test
case rather than repeated in each one.

Directly answers the "gap != needs a test" problem found during M2: gap
analysis on saucedemo-wide flagged exactly 2 uncovered flows, and both
were trivial no-ops after their shared login+menu prologue. Rather than
a significance *heuristic*, this subtracts the prologue every unique
flow in a run actually shares (computed from data already in hand, no
threshold to tune) and judges test-worthiness on what's left. It falls
out of identity.py's anchor for free: an empty mutating-action set on
the remainder is the same "not a test case" signal used there, just
applied after the subtraction instead of to the whole flow.

Deliberately the simplest version that could work: ONE global longest
common prefix across ALL unique flows, not clustered sub-prefixes.
Pure-navigation flows with no mutating actions anywhere (e.g. "view a
product detail page") are also filtered out here, same as flows whose
mutating actions are entirely inside the shared prefix -- both read as
"not test case worthy" under this rule, which is a real simplification,
not an oversight: telling "genuinely shared precondition" apart from "a
legitimately low-stakes pure-nav flow" would need cross-flow segment
frequency analysis this version doesn't do.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Flow, Risk, Transition


def common_prefix_length(flows: list[Flow]) -> int:
    """Length of the longest action_norm_signature prefix shared, in
    order, by every flow in the list."""
    if not flows:
        return 0
    sequences = [[t.action_norm_signature for t in f.transitions] for f in flows]
    shortest = min(len(seq) for seq in sequences)
    n = 0
    for i in range(shortest):
        sig = sequences[0][i]
        if all(seq[i] == sig for seq in sequences):
            n += 1
        else:
            break
    return n


@dataclass
class FlowSplit:
    flow: Flow
    prefix: list[Transition]      # the shared steps (same for every flow in the set)
    remainder: list[Transition]   # what's actually unique to this flow
    test_worthy: bool             # does the remainder do anything state-changing


def split_flows(flows: list[Flow]) -> tuple[list[Transition], list[FlowSplit]]:
    """Returns (shared_prefix, [FlowSplit, ...]) for a set of unique
    flows -- typically the gap flows a caller is deciding whether to
    draft test cases for."""
    n = common_prefix_length(flows)
    prefix = flows[0].transitions[:n] if flows and n else []
    splits = []
    for f in flows:
        remainder = f.transitions[n:]
        # Same criterion as identity.py's mutating_signature_set: a
        # choice (wizard option, sort order) makes a flow's remainder
        # worth a test case just as much as a state-changing action
        # does, even though it's risk == SAFE.
        worthy = any(t.risk == Risk.MUTATING or t.is_choice for t in remainder)
        splits.append(FlowSplit(flow=f, prefix=prefix, remainder=remainder, test_worthy=worthy))
    return prefix, splits
