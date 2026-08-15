"""State fingerprinting.

A UI state is identified by (normalized URL pattern, sorted set of
interactive-element signatures) -- NOT raw URL and NOT raw DOM. This is
the fix over the spec's naive "hash(URL + elements)": raw URLs/DOM
carry per-instance noise (numeric ids, timestamps) that would make
every visit look like a brand-new state and blow up the graph.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

_NUMERIC = re.compile(r"^\d+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def normalize_url(raw_url: str) -> str:
    """Collapse per-instance identifiers in the URL so equivalent pages
    (e.g. inventory-item.html?id=4 and ?id=5) share a pattern, while
    still keeping path segments that carry real product/page identity."""
    parts = urlsplit(raw_url)
    path = "/".join(
        "*" if _NUMERIC.match(seg) or _UUID.match(seg) else seg
        for seg in parts.path.split("/")
    )
    q = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if _NUMERIC.match(v) or _UUID.match(v):
            v = "*"
        q.append((k, v))
    query = urlencode(sorted(q))
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def _norm_token(sig: str) -> str:
    """Generalize a data-test/id style signature by stripping the
    trailing data-specific slug, e.g. 'add-to-cart-sauce-labs-backpack'
    -> 'add-to-cart-*'. Used for structural (not semantic) dedup."""
    # "choice-" (select/radio/checkbox, see actions.py's _build_candidate)
    # is deliberately excluded from every generalization below, not just
    # the known_prefixes loop -- these signatures exist specifically to
    # stay maximally distinct per option/value. Caught live: a checkbox
    # group with purely numeric values ("topping"="1"/"2"/...) fell
    # through to the generic trailing-digit fallback below and collapsed
    # every option to the same 'choice-topping' signature -- the exact
    # same class of bug the "select-" prefix caused earlier, just via a
    # different code path (that one's already excluded from
    # known_prefixes for the same reason but the fallback still caught
    # it before this early return existed).
    if sig.startswith("choice-"):
        return sig
    known_prefixes = [
        "add-to-cart-", "remove-", "item-", "product-", "delete-", "select-",
    ]
    for p in known_prefixes:
        if sig.startswith(p) and len(sig) > len(p):
            return p + "*"
    # generic fallback: strip trailing digits/uuids
    stripped = re.sub(r"[-_ ]?\d+$", "", sig)
    return stripped if stripped != sig else sig


def normalize_signature(sig: str) -> str:
    return _norm_token(sig)


def state_fingerprint(url_pattern: str, candidate_signatures: list[str]) -> str:
    payload = url_pattern + "|" + "|".join(sorted(candidate_signatures))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def human_page_label(url_pattern: str) -> str:
    """A short human name for a state's page, derived from its URL --
    shared by the HTML report (so a step can say *where* it happened)
    and semantic dedup (so a flow's embedding text captures page context,
    not just raw click labels)."""
    path = urlsplit(url_pattern).path.strip("/")
    if not path:
        return "Start page"
    segment = re.sub(r"\.\w+$", "", path.split("/")[-1])
    segment = segment.replace("-", " ").replace("_", " ").replace("*", "…").strip()
    return (segment[:1].upper() + segment[1:]) if segment else "Start page"
