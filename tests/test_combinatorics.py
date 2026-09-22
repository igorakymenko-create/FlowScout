"""generate_pairwise_combinations() -- the automated answer to
ROADMAP.md's "Known limitation -- conjunctive multi-parameter gating is
invisible to DFS" entry. The one property that actually matters for
correctness is completeness (every pair of values across every pair of
groups appears in at least one returned combination) -- optimality
(the SMALLEST such set) is NP-hard and not what's being tested here."""
import itertools

import pytest

from flowscout.combinatorics import MAX_FULL_FACTORIAL, generate_pairwise_combinations


def _missing_pairs(sizes, combos):
    needed = set()
    for i, j in itertools.combinations(range(len(sizes)), 2):
        for vi in range(sizes[i]):
            for vj in range(sizes[j]):
                needed.add((i, j, vi, vj))
    for combo in combos:
        for i, j in itertools.combinations(range(len(sizes)), 2):
            needed.discard((i, j, combo[i], combo[j]))
    return needed


@pytest.mark.parametrize("sizes", [
    [2, 2, 3],          # the original conjunctive-gating fixture's own shape
    [2, 3, 4],
    [2, 2, 2, 2],
    [3, 3, 3, 3, 3],
    [1, 2, 2],           # a group of size 1 (a lone checkbox) mixed with real choices
    [5, 5],
])
def test_covers_every_pair_of_values(sizes):
    combos = generate_pairwise_combinations(sizes)
    assert _missing_pairs(sizes, combos) == set()


def test_every_combination_has_a_valid_index_per_group():
    sizes = [2, 3, 4]
    for combo in generate_pairwise_combinations(sizes):
        assert len(combo) == len(sizes)
        for value, size in zip(combo, sizes):
            assert 0 <= value < size


def test_far_fewer_combinations_than_full_factorial_for_several_groups():
    """The actual point of pairwise testing: quadratic, not exponential,
    growth. 5 groups of 3 values each is 3^5 = 243 full-factorial --
    pairwise should need a small fraction of that."""
    sizes = [3, 3, 3, 3, 3]
    combos = generate_pairwise_combinations(sizes)
    full_factorial = 3 ** 5
    assert len(combos) < full_factorial / 4


def test_deterministic_across_repeated_calls():
    sizes = [2, 3, 4, 2]
    first = generate_pairwise_combinations(sizes)
    second = generate_pairwise_combinations(sizes)
    assert first == second


def test_two_groups_pairwise_equals_full_factorial():
    """With only 2 groups there's exactly one pair of groups to cover,
    so covering every pair of VALUES between them requires visiting
    every combination anyway -- pairwise degenerates to full-factorial
    at N=2, as it should."""
    sizes = [2, 3]
    combos = generate_pairwise_combinations(sizes)
    assert len(combos) == 6
    assert _missing_pairs(sizes, combos) == set()


def test_fewer_than_two_groups_returns_trivial_result_not_an_error():
    assert generate_pairwise_combinations([]) == []
    assert generate_pairwise_combinations([3]) == [(0,), (1,), (2,)]


def test_huge_input_raises_instead_of_grinding_forever():
    with pytest.raises(ValueError):
        generate_pairwise_combinations([500] * 6)


def test_max_full_factorial_guard_is_a_real_module_constant():
    # Guards against the constant silently drifting out of sync with
    # the docstring/ROADMAP language describing "over 100,000 pairs".
    assert MAX_FULL_FACTORIAL == 100_000
