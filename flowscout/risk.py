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


def _split_netloc(netloc: str) -> tuple[str, str]:
    """(lowercased host, port or "") from a URL netloc or an
    allowed_domains entry -- userinfo dropped, IPv6 brackets respected."""
    hostport = (netloc or "").strip().lower().rpartition("@")[2]
    if hostport.startswith("["):
        host, _, rest = hostport[1:].partition("]")
        return host, rest.lstrip(":")
    host, sep, port = hostport.rpartition(":")
    if sep and port.isdigit():
        return host, port
    return hostport, ""


def domain_allowed(netloc: str, allowed_domains: list[str]) -> bool:
    """Whether `netloc` (a URL's host[:port]) falls inside
    `allowed_domains`. An entry WITHOUT a port ("localhost",
    "example.com") allows that host on any port; an entry WITH one
    ("localhost:8080") allows exactly that host:port. Found on a real
    run: `["localhost"]` stopped matching an app on localhost:8080 once
    excursion handling started comparing the full netloc (port included)
    to the entry exactly -- the start page itself read as an external
    excursion and the whole crawl collapsed to 1-2 states, silently.
    Case-insensitive, like hostnames are."""
    host, port = _split_netloc(netloc)
    for entry in allowed_domains:
        entry_host, entry_port = _split_netloc(entry)
        if entry_host == host and (not entry_port or entry_port == port):
            return True
    return False


def _domain_matches_excursion(domain: str, patterns: list[str]) -> bool:
    """Wildcard-aware match against an operator-approved excursion
    allowlist (e.g. "*.stripe.com" matching "checkout.stripe.com") --
    the same fnmatch mechanism exclude_patterns already uses elsewhere
    in this module, just applied to a domain instead of a URL path."""
    return any(fnmatch(domain, pattern) for pattern in patterns)


def classify(label: str, href: str | None, current_domain: str,
             allowed_domains: list[str], exclude_patterns: list[str] | None = None,
             excursion_domains: list[str] | None = None) -> tuple[Risk, str]:
    """`excursion_domains` (Sep 2026): an explicit, operator-named
    allowlist for third-party integrations worth actually walking
    through -- a payment processor, an OAuth/SSO provider -- as
    opposed to an ordinary external link, which stays DESTRUCTIVE
    unconditionally. Deliberately a SEPARATE allowlist from
    `allowed_domains`, not a relaxation of it: `allowed_domains` means
    "this is part of the app, explore it normally, with the ordinary
    depth/breadth budget"; a domain only in `excursion_domains` gets
    the much narrower excursion budget crawler.py's own DFS loop
    enforces (a small, separate depth cap, breadth capped to ~1 so the
    walk doesn't branch into the third party's own marketing pages,
    and only form-contained or progression-labeled candidates even
    tried at all -- see _order_for's own excursion-mode filtering).
    Never falls through to SAFE: leaving to a third party is never a
    harmless action even when its own label doesn't say so, so an
    excursion match floors at MUTATING (withheld exactly like
    "checkout"/"pay" already are unless allow_mutating=true) --
    matched below, not returned immediately here, since a genuinely
    destructive-looking label on the excursion domain itself (a
    logout-like keyword, an exclude_pattern) must still win."""
    text = (label or "").strip().lower()
    excursion_match = ""

    if href:
        try:
            parts = urlsplit(href)
            target_domain = parts.netloc
        except Exception:
            parts = None
            target_domain = ""
        if target_domain and not domain_allowed(target_domain, allowed_domains) and target_domain != current_domain:
            if excursion_domains and _domain_matches_excursion(target_domain, excursion_domains):
                excursion_match = target_domain
            else:
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
            reason = f"matches mutating keyword '{kw}'"
            if excursion_match:
                reason += f" (approved external excursion to {excursion_match})"
            return Risk.MUTATING, reason

    if excursion_match:
        return Risk.MUTATING, f"approved external excursion to {excursion_match}"
    return Risk.SAFE, ""
