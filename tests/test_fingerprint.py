"""fingerprint.normalize_signature() / normalize_url() -- the
generalization rules that decide which per-instance differences collapse
into the same structural signature and which don't.

"choice-" prefixed signatures (select/radio/checkbox options, see
actions.py's _build_candidate) are deliberately exempt from every
generalization rule here, on purpose: they exist specifically to stay
maximally distinct per option/value. Two real bugs were caught by
testing this directly rather than assuming it:

1. A first version used a "select-choice-" prefix, which still starts
   with "select-" and collapsed every <select> option to the same
   signature via known_prefixes.
2. Even after switching to "choice-" (not in known_prefixes), the
   *generic* trailing-digit-stripping fallback still collapsed any
   choice whose value happens to be purely numeric (e.g. two
   checkboxes with value="1" / value="2") -- a different code path
   hitting the same class of bug.
"""
from flowscout.fingerprint import normalize_signature, normalize_url


def test_choice_prefixed_signatures_stay_distinct_across_ordinary_values():
    a = normalize_signature("choice-product-sort-container-lohi")
    b = normalize_signature("choice-product-sort-container-hilo")
    assert a != b


def test_choice_prefixed_signatures_stay_distinct_for_numeric_values():
    """The second bug: purely numeric option values must not collapse
    via the generic trailing-digit-stripping fallback."""
    a = normalize_signature("choice-topping-1")
    b = normalize_signature("choice-topping-2")
    assert a != b
    assert a == "choice-topping-1"
    assert b == "choice-topping-2"


def test_select_prefix_is_not_reused_for_choice_signatures():
    """Historical regression check: a "select-choice-" prefix (an
    earlier, wrong version of this) starts with "select-", which
    known_prefixes generalizes to "select-*" -- collapsing every
    option. Confirms the current "choice-" prefix doesn't have the
    same problem."""
    collapsed = normalize_signature("select-choice-sort-lohi")
    assert collapsed == "select-*"  # the bug this test guards against re-occurring elsewhere
    not_collapsed = normalize_signature("choice-sort-lohi")
    assert not_collapsed == "choice-sort-lohi"


def test_known_prefix_still_generalizes_ordinary_per_item_signatures():
    """The generalization known_prefixes exists for: two different
    products' add-to-cart buttons should read as the same *kind* of
    action for structural dedup / repeat-cap purposes."""
    a = normalize_signature("add-to-cart-sauce-labs-backpack")
    b = normalize_signature("add-to-cart-sauce-labs-bike-light")
    assert a == b == "add-to-cart-*"


def test_generic_trailing_digit_fallback_still_applies_outside_choice_prefix():
    """The fallback that caused the numeric-choice bug is still correct
    and needed for ordinary (non-choice) auto-numbered elements."""
    assert normalize_signature("tab-1") == "tab"
    assert normalize_signature("tab-2") == "tab"


def test_normalize_url_collapses_numeric_path_segments():
    a = normalize_url("https://example.com/inventory-item.html?id=4")
    b = normalize_url("https://example.com/inventory-item.html?id=5")
    assert a == b


def test_normalize_url_collapses_numeric_pagination_segments():
    """quotes.toscrape.com-shaped pagination: /tag/love/page/2/ and
    /tag/love/page/3/ should share a pattern (see ROADMAP.md's
    "Superseded -- Infinite scroll / pagination limits" entry --
    confirmed this already works, the actual problem measured there was
    that pagination controls rarely get followed at all, not that
    visited pages fail to collapse)."""
    a = normalize_url("http://quotes.toscrape.com/tag/love/page/2/")
    b = normalize_url("http://quotes.toscrape.com/tag/love/page/3/")
    assert a == b


def test_normalize_url_keeps_distinct_non_numeric_paths_distinct():
    a = normalize_url("https://example.com/tag/love/")
    b = normalize_url("https://example.com/tag/humor/")
    assert a != b
