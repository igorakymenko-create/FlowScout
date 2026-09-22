"""Pairwise (all-pairs) covering-array generation -- the automated
answer to ROADMAP.md's "Known limitation -- conjunctive multi-parameter
gating is invisible to DFS" entry. `explore_combination()` (crawler.py)
already lets an operator hand over ONE specific combination by hand;
this generates a SET of combinations that covers every *pair* of
values across every pair of parameter groups at least once, instead of
the full cross-product -- the standard real-world technique for this
class of problem, catching the large majority of real interaction bugs
at roughly quadratic cost instead of exponential.

Deliberately a plain, dependency-free module: no crawler/Playwright
knowledge here at all, just index arithmetic over group sizes, so it's
independently testable and reusable regardless of what a "group" or
"value" actually represents to the caller.
"""
from __future__ import annotations

import itertools

# A real page could in principle have many groups with many values each
# -- the greedy algorithm below still terminates, but the "needed
# pairs" set it builds up front is O(sum of size_i * size_j for every
# pair of groups), which for a pathological input (say 20 groups of 20
# values) would mean tens of thousands of pairs and a very long crawl
# even if the algorithm itself finishes quickly. Capped generously
# above anything a real UI's own choice groups would realistically
# have, so a mistake (or a deliberately adversarial config) fails fast
# with a clear reason instead of the crawler silently grinding through
# an enormous combination set.
MAX_FULL_FACTORIAL = 100_000


def generate_pairwise_combinations(sizes: list[int]) -> list[tuple[int, ...]]:
    """Given the sizes of N independent choice groups (e.g. [2, 2, 3]
    for two checkboxes and a 3-option select), returns a list of
    index-tuples -- one per group, each in `range(sizes[i])` -- such
    that every pair of (group_i value, group_j value) across every
    pair of groups appears together in at least one returned tuple.

    Fewer than 2 groups has no pairs to cover by definition; returns
    every single-group value as its own 1-tuple (N=1) or [] (N=0)
    rather than raising, so a caller doesn't need to special-case the
    trivial input just to call this safely.

    Greedy, not globally optimal (finding the SMALLEST possible
    covering array is NP-hard) -- builds one combination at a time,
    picking each group's value to cover as many still-uncovered pairs
    as possible given the values already fixed earlier in that same
    combination, then removes every pair the finished combination
    covers (not just the ones the greedy search targeted -- one
    combination covers C(n, 2) pairs at once) and repeats until none
    remain. Deterministic (stable iteration order) on purpose, so the
    same input always produces the same plan -- a real QA workflow
    wants to see a stable set of combinations run, not a different
    one on every retry."""
    n = len(sizes)
    if n == 0:
        return []
    if n == 1:
        return [(v,) for v in range(sizes[0])]

    full_factorial = 1
    for s in sizes:
        full_factorial *= max(s, 1)
    needed: set[tuple[int, int, int, int]] = set()
    for i, j in itertools.combinations(range(n), 2):
        pair_count = sizes[i] * sizes[j]
        if len(needed) + pair_count > MAX_FULL_FACTORIAL:
            raise ValueError(
                f"too many parameter combinations to cover pairwise ({len(sizes)} groups, "
                f"sizes {sizes}) -- this would need over {MAX_FULL_FACTORIAL} value-pairs; "
                f"narrow the groups being combined")
        for vi in range(sizes[i]):
            for vj in range(sizes[j]):
                needed.add((i, j, vi, vj))

    combos: list[tuple[int, ...]] = []
    while needed:
        combo: list[int | None] = [None] * n
        for i in range(n):
            # Scored against EVERY other group, not just ones already
            # fixed earlier in this combination -- an already-fixed
            # group k contributes a definite match/no-match against
            # value v; a not-yet-fixed group k contributes the count of
            # its OWN values that could still pair with v, a proxy for
            # "how much unresolved coverage does picking v unlock".
            # Scoring group 0 (the first processed, nothing fixed
            # before it) purely against already-fixed peers would find
            # none and default to value 0 forever, verified live: an
            # earlier version did exactly that and never covered any
            # pair requiring group 0's value to be anything else,
            # looping until `needed` was exhausted by other groups
            # alone -- which for a pair that can ONLY be resolved by
            # varying group 0 never happens, hanging indefinitely.
            best_value, best_score = 0, -1
            for v in range(sizes[i]):
                score = 0
                for k in range(n):
                    if k == i:
                        continue
                    a, b = (i, k) if i < k else (k, i)
                    if combo[k] is not None:
                        va, vb = (v, combo[k]) if i < k else (combo[k], v)
                        if (a, b, va, vb) in needed:
                            score += 1
                    else:
                        for w in range(sizes[k]):
                            va, vb = (v, w) if i < k else (w, v)
                            if (a, b, va, vb) in needed:
                                score += 1
                if score > best_score:
                    best_value, best_score = v, score
            combo[i] = best_value
        combo_t = tuple(combo)  # type: ignore[arg-type]
        combos.append(combo_t)
        for i, j in itertools.combinations(range(n), 2):
            needed.discard((i, j, combo_t[i], combo_t[j]))
    return combos
