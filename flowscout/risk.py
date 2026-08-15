"""Action risk classification.

Every candidate action is classified before it is ever clicked. This is
the safety layer the original spec omitted: an autonomous agent must
not be free to fire irreversible actions (logout mid-crawl kills the
session/replay chain; external links leave the target app entirely;
delete/cancel actions destroy state) without being told to.
"""
from __future__ import annotations

from fnmatch import fnmatch
from urllib.parse import urlsplit

from .models import Risk

_DESTRUCTIVE_KEYWORDS = [
    "log out", "logout", "sign out", "delete account", "close account",
    "cancel subscription", "deactivate", "unsubscribe",
]

_MUTATING_KEYWORDS = [
    "checkout", "finish", "place order", "submit", "pay", "confirm",
    "add to cart", "remove", "delete", "save", "update", "continue",
    # Real-money / subscription actions -- withheld whenever allow_mutating
    # is false, which is exactly what you want on a live production site
    # with a real payment processor wired up (found missing while about to
    # crawl a site with real Stripe integration: "Purchase" fell through
    # to SAFE with the original list, meaning allow_mutating=false alone
    # wouldn't have stopped it).
    "purchase", "buy", "subscribe", "donate", "upgrade",
    # Found missing on saucedemo's own "Reset App State" -- clears the
    # cart, a real state change, but fell through to SAFE with the
    # original list. This let semantic_dedup's state-convergence tier
    # (which now requires equal mutating_signature_set, see
    # semantic_dedup.py) treat it as interchangeable with plain
    # navigation and merge it away every time.
    "reset",
]


def classify(label: str, href: str | None, current_domain: str,
             allowed_domains: list[str], exclude_patterns: list[str] | None = None) -> tuple[Risk, str]:
    text = (label or "").strip().lower()

    if href:
        try:
            parts = urlsplit(href)
            target_domain = parts.netloc
        except Exception:
            parts = None
            target_domain = ""
        if target_domain and target_domain not in allowed_domains and target_domain != current_domain:
            return Risk.DESTRUCTIVE, f"external navigation to {target_domain}"
        # Operator-specified no-go pages (legal/privacy/social, anything
        # not worth the crawl budget or not safe to touch) -- same
        # treatment as an external domain: never followed, regardless of
        # allow_mutating, since this is an explicit exclusion, not a risk
        # tier the operator might opt into.
        if parts is not None and exclude_patterns:
            path = parts.path or href
            for pattern in exclude_patterns:
                if fnmatch(path, pattern):
                    return Risk.DESTRUCTIVE, f"matches exclude pattern '{pattern}'"

    # Label match -- outside the `if href:` block on purpose, so it
    # still runs when there's no href at all. Found live on saucedemo:
    # "Checkout" is a <button>, not an <a>, navigating via client-side
    # routing -- exclude_patterns: ["*checkout*"] silently let it
    # through, because the URL-only check above never even ran. Any
    # button-triggered client-side routing (React Router, Vue Router,
    # Next.js <Link> rendered as a button -- most modern SPA frontends)
    # has the same gap: there's no href to pattern-match until after the
    # click, and clicking first to check defeats the whole point of an
    # exclusion (you cannot safely "click once to check" a control meant
    # to be excluded). Same glob syntax, same exclude_patterns list --
    # a pattern already written for a URL path ("*/privacy*") won't
    # accidentally start matching label text too, since ordinary label
    # text doesn't contain "/", so this is additive for patterns already
    # in use, not a behavior change for them.
    if exclude_patterns and text:
        for pattern in exclude_patterns:
            if fnmatch(text, pattern.lower()):
                return Risk.DESTRUCTIVE, f"matches exclude pattern '{pattern}' (label)"

    for kw in _DESTRUCTIVE_KEYWORDS:
        if kw in text:
            return Risk.DESTRUCTIVE, f"matches destructive keyword '{kw}'"

    for kw in _MUTATING_KEYWORDS:
        if kw in text:
            return Risk.MUTATING, f"matches mutating keyword '{kw}'"

    return Risk.SAFE, ""
