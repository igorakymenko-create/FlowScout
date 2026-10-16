"""GapAnalysis.overlap_groups() -- test cases that matched the SAME
action/navigation flow. A "review these together" signal, not a duplicate
verdict, and never including human-confirmed pairings or not_found tests."""
from flowscout.models import GapAnalysis, TcmsCoverage


def _cov(tid, group, score=0.8, status="covered", confirmed=False, label="On Cart: Click \"Checkout\""):
    return TcmsCoverage(tid, f"title {tid}", status, 1 if status == "covered" else None, score,
                        confirmed=confirmed, match_group=group, match_label=label if group else None)


def _gap(items):
    return GapAnalysis(tcms_source="x.csv", threshold=0.74, status="ok", tcms_coverage=items)


def test_several_tests_on_one_action_form_a_group_best_score_first():
    g = _gap([_cov("A", "action:checkout", 0.78), _cov("B", "action:checkout", 0.91),
              _cov("C", "action:checkout", 0.80), _cov("D", "action:login", 0.9)])
    groups = g.overlap_groups()
    assert len(groups) == 1
    label, members = groups[0]
    assert [m.tcms_id for m in members] == ["B", "C", "A"]
    assert g.summary()["tcms_overlap_groups"] == 1
    assert g.summary()["tcms_in_overlap_groups"] == 3


def test_single_test_per_behavior_is_not_a_group():
    g = _gap([_cov("A", "action:checkout"), _cov("B", "action:login"), _cov("C", "nav:3")])
    assert g.overlap_groups() == []
    assert g.summary()["tcms_overlap_groups"] == 0


def test_confirmed_and_not_found_tests_are_left_out():
    g = _gap([_cov("A", "action:checkout"), _cov("B", "action:checkout", confirmed=True),
              _cov("C", None, status="not_found")])
    assert g.overlap_groups() == []


def test_navigation_matches_are_never_grouped():
    """Real run: "Login with valid credentials" and "View a product's
    detail page" both matched one navigation flow -- a misleading group,
    since a navigation flow is a whole path several different tests
    legitimately touch."""
    g = _gap([_cov("A", "nav:7", label="Ends on Cart"), _cov("B", "nav:7", label="Ends on Cart"),
              _cov("C", "action:x"), _cov("D", "action:x"), _cov("E", "action:y"), _cov("F", "action:y")])
    groups = g.overlap_groups()
    assert sorted(len(m) for _, m in groups) == [2, 2]
    assert all(x.match_group.startswith("action:") for _, m in groups for x in m)


def test_largest_group_first():
    g = _gap([_cov("A", "action:a"), _cov("B", "action:a"),
              _cov("C", "action:x"), _cov("D", "action:x"), _cov("E", "action:x")])
    assert [len(m) for _, m in g.overlap_groups()] == [3, 2]


def test_round_trip_keeps_the_group_and_old_files_without_it_still_load():
    g = _gap([_cov("A", "action:checkout"), _cov("B", "action:checkout")])
    again = GapAnalysis.from_json(g.to_json())
    assert len(again.overlap_groups()) == 1
    old = g.to_json()
    for row in old["tcms_coverage"]:
        row.pop("match_group"); row.pop("match_label")
    legacy = GapAnalysis.from_json(old)
    assert legacy.overlap_groups() == [] and legacy.summary()["tcms_covered"] == 2
