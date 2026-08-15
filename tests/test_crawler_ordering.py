"""crawler._order_for() -- breadth-limit truncation and the revisit-
history-aware ordering that decides which candidates survive a cut.

See ROADMAP.md's "Revisit-history-aware candidate ordering" entry for
the full story: candidates whose norm_signature was already confirmed,
earlier in the same persona's pass, to lead to an already-known state
are stable-sorted to the back before truncation -- so a forced cut
preferentially drops actions already shown to be unproductive.

is_choice candidates (select/radio/checkbox options) are deliberately
exempt from this deprioritization, no matter what revisit_history says.
This is not a hypothetical edge case: picking a <select> option changes
display order, not the candidate *set*, so state_fingerprint()
deliberately doesn't change -- meaning EVERY choice in a group reads as
a revisit the instant any one option is tried anywhere in the run. The
first version of this feature didn't have the exemption and silently
buried saucedemo's own sort dropdown (a real, valued, TCMS-matched
capability) under max_breadth_per_state as a result -- caught by a live
crawl, reproduced deterministically here.
"""
from flowscout.crawler import _order_for
from flowscout.models import ElementCandidate, Risk, RunResult, StateNode


def _cand(sig: str, is_choice: bool) -> ElementCandidate:
    return ElementCandidate(signature=sig, norm_signature=sig, label=sig, selector="{}",
                             risk=Risk.SAFE, risk_reason="", discovered_via="markup", is_choice=is_choice)


def _run() -> RunResult:
    return RunResult(project="t", start_url="https://example.com/", config={})


def test_breadth_truncation_keeps_the_first_max_breadth_candidates_when_nothing_is_flagged():
    node = StateNode(fingerprint="x", url_pattern="/", raw_url="", title="",
                      candidates=[_cand(f"c{i}", False) for i in range(5)])
    idxs = _order_for(node, max_breadth=3, run=_run(), revisit_history=set())
    kept = [node.candidates[i].signature for i in idxs]
    assert kept == ["c0", "c1", "c2"]


def test_revisit_flagged_candidates_are_deprioritized_before_truncation():
    node = StateNode(fingerprint="x", url_pattern="/", raw_url="", title="",
                      candidates=[_cand("flagged", False), _cand("fresh1", False),
                                  _cand("fresh2", False)])
    idxs = _order_for(node, max_breadth=2, run=_run(), revisit_history={"flagged"})
    kept = {node.candidates[i].signature for i in idxs}
    assert kept == {"fresh1", "fresh2"}
    assert "flagged" not in kept


def test_choice_candidates_are_exempt_from_revisit_deprioritization():
    """The regression this feature actually caused before the fix:
    12 candidates, 6 ordinary (2 flagged as revisit-producers) + 4
    is_choice (ALL flagged, simulating 'one sort option already tried
    elsewhere in this run') + 2 unflagged ordinary ones, breadth=10.
    Every choice candidate must survive; only the flagged ordinary ones
    should be cut."""
    node = StateNode(fingerprint="x", url_pattern="/inv", raw_url="", title="", candidates=[
        _cand("add1", False), _cand("add2", False), _cand("flagged1", False),
        _cand("add3", False), _cand("add4", False), _cand("flagged2", False),
        _cand("menu", False), _cand("cart", False),
        _cand("choice-sort-az", True), _cand("choice-sort-za", True),
        _cand("choice-sort-lohi", True), _cand("choice-sort-hilo", True),
    ])
    revisit_history = {"flagged1", "flagged2", "choice-sort-az", "choice-sort-za",
                        "choice-sort-lohi", "choice-sort-hilo"}
    run = _run()
    idxs = _order_for(node, max_breadth=10, run=run, revisit_history=revisit_history)
    kept = {node.candidates[i].signature for i in idxs}
    choice_kept = {s for s in kept if s.startswith("choice-sort")}
    assert choice_kept == {"choice-sort-az", "choice-sort-za", "choice-sort-lohi", "choice-sort-hilo"}
    assert "flagged1" not in kept
    assert "flagged2" not in kept


def test_truncated_candidates_are_recorded_in_skipped_candidates():
    node = StateNode(fingerprint="x", url_pattern="/", raw_url="", title="",
                      candidates=[_cand(f"c{i}", False) for i in range(4)])
    run = _run()
    _order_for(node, max_breadth=2, run=run, revisit_history=set())
    assert len(run.skipped_candidates) == 2
    assert all(s["reason"] == "breadth limit exceeded" for s in run.skipped_candidates)
