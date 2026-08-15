"""risk.classify() -- exclude_patterns matching, both href-based and
label-based (the label check was added Aug 2026 after finding, on a
live saucedemo crawl, that a button-triggered action with no href at
all silently bypassed exclude_patterns entirely -- see ROADMAP.md's
"exclude_patterns is href-only" entry for the full story).

These specific assertions were run as an ad-hoc live script before that
fix was trusted; kept here as a real, permanent regression suite rather
than a one-off check.
"""
from flowscout.models import Risk
from flowscout.risk import classify


def test_label_match_with_no_href():
    """The motivating bug: a <button> with no href at all (client-side
    routing) still has to be excludable by label."""
    risk, reason = classify("Checkout", None, "www.saucedemo.com",
                             ["www.saucedemo.com"], ["*checkout*"])
    assert risk == Risk.DESTRUCTIVE
    assert "label" in reason


def test_href_match_unaffected_by_the_label_check():
    """Backward compatibility: an ordinary href-based exclude pattern
    (the only kind that existed before this fix) must behave exactly as
    it did before."""
    risk, reason = classify("Privacy Policy", "https://amfit.net/en/privacy",
                             "amfit.net", ["amfit.net"], ["*/privacy*"])
    assert risk == Risk.DESTRUCTIVE
    assert "label" not in reason


def test_url_shaped_pattern_does_not_leak_into_label_matching():
    """A pattern written for a URL path ('*/privacy*') shouldn't
    accidentally start matching unrelated label text -- ordinary label
    text doesn't contain '/', so this should stay additive."""
    risk, _ = classify("Home", "https://amfit.net/en/home", "amfit.net",
                        ["amfit.net"], ["*/privacy*"])
    assert risk == Risk.SAFE


def test_label_shaped_pattern_does_not_leak_into_href_matching():
    """The reverse: a label-shaped pattern shouldn't accidentally match
    an unrelated href path."""
    risk, _ = classify("Home", "https://amfit.net/en/home", "amfit.net",
                        ["amfit.net"], ["*checkout*"])
    assert risk == Risk.SAFE


def test_label_match_is_case_insensitive():
    risk, _ = classify("CHECKOUT NOW", None, "www.saucedemo.com",
                        ["www.saucedemo.com"], ["*checkout*"])
    assert risk == Risk.DESTRUCTIVE


def test_no_exclude_patterns_falls_through_to_ordinary_keyword_classification():
    """Without exclude_patterns configured at all, behavior must be
    identical to before this feature existed -- an unmatched button
    still gets classified by the ordinary mutating/destructive keyword
    lists."""
    risk, reason = classify("Checkout", None, "www.saucedemo.com",
                             ["www.saucedemo.com"], None)
    assert risk == Risk.MUTATING
    assert "mutating keyword" in reason


def test_external_domain_is_always_destructive():
    risk, reason = classify("External link", "https://evil.example.com/",
                             "www.saucedemo.com", ["www.saucedemo.com"], None)
    assert risk == Risk.DESTRUCTIVE
    assert "external" in reason


def test_destructive_keyword_beats_mutating_keyword():
    risk, reason = classify("Log out", None, "www.saucedemo.com",
                             ["www.saucedemo.com"], None)
    assert risk == Risk.DESTRUCTIVE
    assert "destructive keyword" in reason
