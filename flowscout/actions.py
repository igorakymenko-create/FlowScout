"""Element discovery, locator resolution, and form-filling.

Kept separate from crawler.py so the "how do we interact with a page"
concerns are isolated from the "how do we walk the graph" concerns.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from .fingerprint import normalize_signature
from .models import ElementCandidate, Risk
from .risk import classify

_DISCOVER_JS = r"""
() => {
    // Plain 'a' (not 'a[href]'): some real, functional links are JS-driven
    // with no href at all -- e.g. saucedemo's cart icon is
    // <a data-test="shopping-cart-link" class="shopping_cart_link"> with
    // no href attribute. Requiring href silently made it (and anything
    // built the same way) invisible to discovery from the very first run.
    const sel = 'a, button, input[type=submit], input[type=button], [role="button"]';
    const nodes = Array.from(document.querySelectorAll(sel));
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const isRelatedToCandidate = (el) => nodes.some(c => el === c || el.contains(c) || c.contains(el));

    // Text capture, shared by every element-gathering pass below.
    // Deliberately takes only the FIRST block (split on raw newlines,
    // before any whitespace collapsing) -- found necessary on a real
    // replay failure, not assumed: a multi-block element's full
    // innerText (e.g. a card's heading + description, "Option A\n\n
    // Sample description text...") reads naturally to a human, but
    // Playwright's own get_by_text() matches against textContent, which
    // has NO whitespace between adjacent block children at all --
    // "Option A" and "Sample description text..." concatenate directly with
    // no separator, so neither an exact NOR a substring match against
    // the (space-joined) full text ever succeeds, at any truncation
    // length. Confirmed live: every attempt to click Site B's wizard
    // cards via their full combined text timed out; the same locator
    // built from just the first line ("Option A") resolves to exactly
    // one element and the click correctly bubbles to the card's own
    // handler. Bonus: this also produces a cleaner label than the old
    // "Option A Sample description text that keeps going for a whi"
    // (silently truncated mid-word at 60 chars) ever did.
    function firstBlockText(raw) {
        const first = (raw || '').split(/\n+/)[0] || '';
        return first.replace(/\s+/g, ' ').trim().slice(0, 60);
    }

    function isFixedPositioned(el) {
        // react-burger-menu-style off-canvas panels are position:fixed and
        // slide past the viewport edge while "closed". That's the pattern
        // the viewport check below exists to catch.
        let n = el;
        while (n && n !== document.body && n !== document.documentElement) {
            if (getComputedStyle(n).position === 'fixed') return true;
            n = n.parentElement;
        }
        return false;
    }

    const candidates = nodes.filter(n => {
        const r = n.getBoundingClientRect();
        const style = getComputedStyle(n);
        if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden'
            || style.display === 'none' || n.disabled) {
            return false;
        }
        // Only a fixed-positioned element being outside the current
        // viewport actually means "hidden by design" (off-canvas). Normal
        // document-flow content below/above the fold has a large/negative
        // top for the boring reason that the page hasn't scrolled there --
        // Playwright scrolls to it automatically before clicking. Applying
        // the same viewport check to everything silently hid an entire
        // page's worth of below-the-fold content (including a wizard's own
        // "Next" button on a real site) on any page taller than one screen.
        if (isFixedPositioned(n)) {
            const onScreen = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
            if (!onScreen) return false;
        }
        return true;
    }).map(n => {
        const r = n.getBoundingClientRect();
        // Occlusion check: an element can be on-screen, correctly sized and
        // still unclickable because something else (an open menu panel, a
        // modal backdrop) is stacked on top of it at its own coordinates --
        // e.g. saucedemo's hamburger button stays visible after the side
        // menu opens, but the menu panel now covers it. Ask the browser what
        // element actually sits at the candidate's own center point. Only
        // meaningful for elements actually within the current viewport --
        // elementFromPoint can't assess a point outside it, so below/above-
        // fold elements are assumed not occluded (Playwright's own click
        // will scroll to them and raise a real error if something's wrong).
        const inViewport = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
        let occluded = false, atPoint = null;
        if (inViewport) {
            const left = Math.max(r.left, 0), right = Math.min(r.right, vw);
            const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
            const cx = (left + right) / 2, cy = (top + bottom) / 2;
            atPoint = document.elementFromPoint(cx, cy);
            occluded = !atPoint || !(n.contains(atPoint) || atPoint.contains(n));
        }
        // Extra signals for classifying *what kind* of menu a toggle opens
        // (hamburger / sidebar / dropdown / ...), since a lot of UI
        // libraries encode that in the id/class rather than ARIA. Where a
        // library IS accessible, aria-controls lets us peek at the panel
        // it actually opens, which is a stronger signal than the button's
        // own naming.
        const ariaControls = n.getAttribute('aria-controls') || '';
        let controlledTag = '', controlledClass = '';
        if (ariaControls) {
            const controlled = document.getElementById(ariaControls);
            if (controlled) {
                controlledTag = controlled.tagName.toLowerCase();
                controlledClass = (controlled.className || '').toString();
            }
        }
        return {
            tag: n.tagName.toLowerCase(),
            dataTest: n.getAttribute('data-test') || n.getAttribute('data-testid') || '',
            id: n.id || '',
            href: n.getAttribute('href') || '',
            // Icon-only elements (a logo link, an icon button) often carry
            // no text of their own -- the accessible name lives on a child
            // <img alt="..."> instead (e.g. a logo <a> wrapping <img
            // alt="ACME">), or on a title attribute. Without this, the
            // label falls all the way through to the bare tag name ("a"),
            // which tells a reader nothing about what was actually clicked.
            text: firstBlockText(n.innerText || n.value || n.getAttribute('aria-label')
                   || (n.querySelector('img[alt]') || {}).alt || n.getAttribute('title') || ''),
            type: (n.getAttribute('type') || '').toLowerCase(),
            inForm: !!n.closest('form'),
            occluded: occluded,
            occludedBy: occluded && atPoint ? (atPoint.className || atPoint.tagName || '').toString().slice(0, 60) : '',
            className: (n.className || '').toString().slice(0, 120),
            ariaLabel: n.getAttribute('aria-label') || '',
            ariaHasPopup: n.getAttribute('aria-haspopup') || '',
            ariaExpandedSet: n.hasAttribute('aria-expanded'),
            ariaControls: ariaControls,
            controlledTag: controlledTag,
            controlledClass: controlledClass,
        };
    });

    // Div-as-button detection (Aug 2026 rewrite): a page can have real,
    // functional controls built as a <div onClick> instead of a
    // <button>/<a> (React/Tailwind apps do this constantly -- Site B's
    // entire workout wizard, all 15 selectable cards across 4 steps, is
    // built this way with zero ARIA signal). The old version of this pass
    // only *counted* such elements via a cursor:pointer/role/tabindex
    // guess and never clicked them, because a guess isn't safe to click.
    // It's been replaced with ground truth: gather every visible element
    // NOT already part of a formal candidate, hand it to Python, which
    // asks the browser via CDP (DOMDebugger.getEventListeners) whether it
    // actually has a click handler -- a fact, not a style-based guess, and
    // works regardless of framework or how the element happens to be
    // styled. Verified elements get promoted into real candidates.
    //
    // This pass only GATHERS the pool and stashes live references for the
    // CDP follow-up (see actions.py's _verify_pool) -- nothing here decides
    // what's real.
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const SKIP_TAGS = new Set(['SCRIPT','STYLE','HEAD','META','LINK','TITLE','NOSCRIPT']);
    const poolEls = [];
    for (const el of document.querySelectorAll('body *')) {
        if (el.namespaceURI === SVG_NS) continue;
        if (SKIP_TAGS.has(el.tagName)) continue;
        if (isRelatedToCandidate(el)) continue;
        const style = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (r.width <= 2 || r.height <= 2 || style.visibility === 'hidden' || style.display === 'none') continue;
        // Sanity cap against huge containers (a whole <nav>/<section> that
        // happens to have SOME click handler on it, e.g. event-delegation
        // roots) -- a real clickable card/button is a small, specific
        // element, not a page region. Known tradeoff: a legitimately large
        // custom card (>40 descendants) is missed by this cap; raised from
        // the old heuristic's 15 to be more permissive, not proven optimal.
        if (el.querySelectorAll('*').length > 40) continue;
        const onScreen = r.bottom > 0 && r.top < vh + 4000 && r.right > 0 && r.left < vw;
        if (!onScreen) continue;
        poolEls.push(el);
    }

    // React attaches ONE native listener per event type to its root
    // container (React 17+'s delegation model) and dispatches internally
    // to whichever component's onClick prop matches the real target --
    // so an individual div-as-button element in a React app commonly has
    // NO listener of its own to find via CDP at all, and walking its
    // ancestors for "some listener somewhere" was tried and rejected: it
    // reliably hits that root-level delegation listener for nearly any
    // element a few levels deep (confirmed false-positive on saucedemo's
    // own footer text, 3 levels below its React root) -- not a specific
    // signal, just "this page uses React". The fiber's own onClick prop,
    // read directly, says what THIS element does regardless of where the
    // underlying native listener physically lives.
    function hasReactOnClick(el) {
        for (const k of Object.keys(el)) {
            if (!/^__react(Props|EventHandlers)/.test(k)) continue;
            const v = el[k];
            if (v && typeof v === 'object' && typeof v.onClick === 'function') return true;
        }
        return false;
    }

    const DISABLED_RE = /disabled|not-allowed/i;
    const pool = poolEls.map(el => {
        const style = getComputedStyle(el);
        const cls = (el.className || '').toString();
        const ariaDisabled = el.getAttribute('aria-disabled') === 'true'
            || DISABLED_RE.test(cls) || style.pointerEvents === 'none';
        return {
            tag: el.tagName.toLowerCase(),
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: el.getAttribute('href') || '',
            // See firstBlockText above -- matters most here: a wizard
            // card's own text commonly spans a heading + description,
            // which is exactly the shape that broke replay.
            text: firstBlockText(el.innerText || el.getAttribute('aria-label')
                   || (el.querySelector('img[alt]') || {}).alt || el.getAttribute('title') || ''),
            type: (el.getAttribute('type') || '').toLowerCase(),
            inForm: !!el.closest('form'),
            occluded: false, occludedBy: '',
            className: cls.slice(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            ariaHasPopup: el.getAttribute('aria-haspopup') || '',
            ariaExpandedSet: el.hasAttribute('aria-expanded'),
            ariaControls: '', controlledTag: '', controlledClass: '',
            ariaDisabled: ariaDisabled,
            // Secondary signal alongside CDP's direct-listener check
            // (see actions.py's _verify_pool) -- React-specific, but
            // React is common enough that skipping this misses real
            // controls in exactly the delegation style described above.
            reactOnClick: hasReactOnClick(el),
        };
    });
    window.__flowscout_pool = poolEls;

    // Fallback bucket, used only if the CDP verification pass in Python
    // fails outright (session error) -- so that failure degrades to the
    // previously-shipped guess instead of promoting nothing at all.
    const INTERACTIVE_ROLES = new Set([
        'link', 'menuitem', 'tab', 'option', 'checkbox', 'radio', 'switch', 'button',
    ]);
    const legacyRaw = poolEls.filter(el => {
        const style = getComputedStyle(el);
        const role = el.getAttribute('role');
        const hasTabindex = el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1';
        return style.cursor === 'pointer' || (role && INTERACTIVE_ROLES.has(role)) || hasTabindex;
    });
    const legacyUnclassified = legacyRaw
        .filter(el => !legacyRaw.some(other => other !== el && other.contains(el)))
        .map(el => ({
            tag: el.tagName.toLowerCase(),
            className: (el.className || '').toString().slice(0, 80),
            text: firstBlockText(el.innerText || ''),
        }));

    // Native <select>: one action per option, not one per <select> --
    // "choose Price (low to high)" and "choose Price (high to low)" are
    // different user actions, not the same click on different days. Each
    // option becomes its own candidate-shaped entry (built via
    // actions.py's _build_candidate, tag: 'select' branch), carrying the
    // select's own dataTest/id for locating plus the specific option's
    // value/text for identity -- see identity.py's mutating_signature_set
    // for why the choice itself, not just "a select happened", matters.
    const selects = [];
    for (const s of document.querySelectorAll('select')) {
        const r = s.getBoundingClientRect();
        const style = getComputedStyle(s);
        if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden'
            || style.display === 'none' || s.disabled) continue;
        for (const opt of s.options) {
            if (opt.disabled) continue;
            selects.push({
                tag: 'select',
                dataTest: s.getAttribute('data-test') || s.getAttribute('data-testid') || '',
                id: s.id || '',
                href: '',
                text: (opt.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
                type: 'select-option',
                inForm: !!s.closest('form'),
                occluded: false, occludedBy: '',
                className: (s.className || '').toString().slice(0, 120),
                ariaLabel: s.getAttribute('aria-label') || '',
                ariaHasPopup: '', ariaExpandedSet: false,
                ariaControls: '', controlledTag: '', controlledClass: '',
                selectValue: opt.value,
            });
        }
    }

    // Radio/checkbox labels live on a sibling <label>, not the input
    // itself -- an <input type=radio> has no text content of its own.
    function inputLabelText(el) {
        if (el.id) {
            const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lbl) return firstBlockText(lbl.innerText || '');
        }
        const wrapping = el.closest('label');
        if (wrapping) return firstBlockText(wrapping.innerText || '');
        return firstBlockText(el.getAttribute('aria-label') || el.value || '');
    }
    function isUsableInput(el) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return !(r.width <= 0 || r.height <= 0 || style.visibility === 'hidden'
                 || style.display === 'none' || el.disabled);
    }

    // Radio groups: "pick one of N" is structurally the same choice a
    // <select> represents -- one candidate per option (grouped by `name`,
    // the attribute that actually defines a radio group in HTML), each
    // is_choice, same as a select option. A radio with no `name` isn't
    // really grouped with anything; treated as a lone group of one rather
    // than dropped.
    const radios = [];
    let ungroupedSeq = 0;
    for (const el of document.querySelectorAll('input[type=radio]')) {
        if (!isUsableInput(el)) continue;
        const groupName = el.name || `__ungrouped_${ungroupedSeq++}`;
        radios.push({
            tag: 'radio',
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: '',
            text: inputLabelText(el),
            type: 'radio-choice',
            inForm: !!el.closest('form'),
            occluded: false, occludedBy: '',
            className: (el.className || '').toString().slice(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            ariaHasPopup: '', ariaExpandedSet: false,
            ariaControls: '', controlledTag: '', controlledClass: '',
            radioGroup: groupName, radioValue: el.value,
        });
    }

    // Checkboxes: unlike radios, NOT mutually exclusive -- checking one
    // doesn't rule out any other, so this is one independent toggle
    // action per checkbox, not "pick one of N". Still is_choice (see
    // actions.py's _build_candidate): two flows that end up with
    // different boxes checked did genuinely different things, the same
    // reason a radio pick or a sort order has to stay distinct in
    // identity -- ticking a checkbox just isn't exclusive with its
    // siblings the way those are.
    const checkboxes = [];
    for (const el of document.querySelectorAll('input[type=checkbox]')) {
        if (!isUsableInput(el)) continue;
        checkboxes.push({
            tag: 'checkbox',
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: '',
            text: inputLabelText(el),
            type: 'checkbox-toggle',
            inForm: !!el.closest('form'),
            occluded: false, occludedBy: '',
            className: (el.className || '').toString().slice(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            ariaHasPopup: '', ariaExpandedSet: false,
            ariaControls: '', controlledTag: '', controlledClass: '',
            checkboxName: el.name || el.id || '', checkboxValue: el.value || 'on',
        });
    }

    return {candidates, pool, legacyUnclassified, selects, radios, checkboxes};
}
"""


# Ordered so a more specific pattern (hamburger) wins over a broader one
# that could also match it (nav). Matched against the trigger's own
# id/class/aria-label, and against the panel it opens when aria-controls
# resolves to one -- whichever names it more specifically.
_MENU_KIND_PATTERNS = [
    (re.compile(r"burger|hamburger", re.I), "hamburger menu"),
    (re.compile(r"sidebar|drawer|off-?canvas", re.I), "sidebar menu"),
    (re.compile(r"dropdown", re.I), "dropdown menu"),
    (re.compile(r"context-?menu", re.I), "context menu"),
    (re.compile(r"\bnav(igation)?\b", re.I), "navigation menu"),
]
# Gate before pattern-matching id/class at all: without it, a plain nav
# link that merely *lives inside* a sidebar (e.g. saucedemo's "All Items"
# link has id="inventory_sidebar_link", class="bm-item menu-item") would
# get mislabeled as a menu *trigger*. A real toggle either exposes it via
# ARIA, or its own accessible text says so ("Open Menu", "Toggle nav").
_TOGGLE_TEXT_RE = re.compile(r"\b(menu|open|close|toggle|expand|collapse|nav)\b", re.I)


def classify_menu_kind(el_meta: dict) -> str | None:
    """What kind of menu does this element toggle, if any -- 'hamburger
    menu', 'dropdown menu', etc. Returns None for elements that aren't
    menu toggles at all."""
    text = el_meta.get("text", "")
    has_aria_signal = bool(el_meta.get("ariaHasPopup")) or el_meta.get("ariaExpandedSet") \
        or bool(el_meta.get("ariaControls"))
    if not (has_aria_signal or _TOGGLE_TEXT_RE.search(text)):
        return None
    blob = " ".join(filter(None, [
        el_meta.get("id", ""), el_meta.get("className", ""), el_meta.get("ariaLabel", ""),
        el_meta.get("controlledTag", ""), el_meta.get("controlledClass", ""),
    ]))
    for pattern, kind in _MENU_KIND_PATTERNS:
        if pattern.search(blob):
            return kind
    if has_aria_signal or re.search(r"\bmenu\b", text, re.I):
        return "menu"
    return None


def describe_action(el_meta: dict, fill_summary: dict | None) -> str:
    """Human-readable description of an interaction, for the flow report.
    Raw element text alone ("Sauce Labs Backpack", "Open Menu") reads as
    ambiguous -- readers can't tell if that's a link being followed, a
    button being pressed, or a form being submitted. Name the verb, and
    for form submissions, name what was actually filled in (masking
    passwords) so e.g. login steps show which account was used."""
    text = el_meta.get("text") or el_meta.get("dataTest") or el_meta.get("id") or el_meta.get("tag", "element")
    if fill_summary is not None:
        if fill_summary:
            fields = ", ".join(f'{k}="{v}"' for k, v in fill_summary.items())
            return f'Fill form and submit "{text}" ({fields})'
        return f'Fill form and submit "{text}"'
    if el_meta.get("tag") == "select":
        group = el_meta.get("dataTest") or el_meta.get("id") or "dropdown"
        return f'Select "{text}" in "{group}"'
    if el_meta.get("tag") == "radio":
        group = el_meta.get("radioGroup", "").lstrip("_") or "options"
        return f'Select "{text}" in "{group}"'
    if el_meta.get("tag") == "checkbox":
        return f'Toggle "{text}"'
    base = f'Open "{text}"' if el_meta.get("tag") == "a" else f'Click "{text}"'
    kind = classify_menu_kind(el_meta)
    return f"{base} ({kind})" if kind else base


def _aggregate_unclassified(raw: list[dict]) -> list[dict]:
    """Dedupe the coverage-delta list: a pricing page with 9 near-identical
    "Purchase" buttons should report as one line with count=9, not nine
    identical rows."""
    counts: dict[tuple[str, str, str], int] = {}
    for el in raw:
        key = (el["tag"], el["className"][:40], el["text"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"tag": tag, "className": cls, "text": text, "count": n}
        for (tag, cls, text), n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _verify_pool(page, pool_meta: list[dict]) -> list[bool]:
    """Ground truth for `pool_meta` (parallel to window.__flowscout_pool,
    stashed by _DISCOVER_JS): does this element actually have a click
    handler of its own? Two independent signals, either is enough:

    1. CDP's DOMDebugger.getEventListeners, checked on the element
       itself -- a real addEventListener/onclick, framework-agnostic.
    2. The element's own React fiber onClick prop (see _DISCOVER_JS's
       hasReactOnClick) -- needed because React 17+ commonly attaches
       its actual native listener to the app's *root* container, not
       to individual elements, so signal 1 alone finds nothing for a
       real React div-as-button control.

    An ancestor walk (checking a few parent levels for CDP signal 1) was
    tried first and rejected: it reliably matches React's own root-level
    delegation listener for nearly any element a few DOM levels deep,
    regardless of whether that specific element does anything at all --
    confirmed as a real false positive on saucedemo's footer copyright
    text (3 levels below its React root, no onClick of its own). Signal
    2 covers the actual delegation case correctly, by reading what the
    element's own fiber declares rather than guessing from listener
    presence somewhere in its ancestry.

    Known gap, not solved by either signal: a div-as-button built with
    event delegation to a *non-React* framework's own root/document-level
    handler (or a raw addEventListener attached to a wrapper on purpose)
    won't be found. Left as a real limitation rather than reintroducing
    the ancestor walk's false-positive class to chase it.

    Measured cost (real pages, Aug 2026): ~0.7-0.8ms per element for the
    CDP call, so a page with a few hundred pool candidates costs well
    under a second -- checked, not assumed.

    Raises on CDP failure (caller falls back to the legacy heuristic
    bucket rather than silently promoting nothing)."""
    if not pool_meta:
        return []
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("Runtime.enable")

    def has_click(obj_id: str) -> bool:
        res = cdp.send("DOMDebugger.getEventListeners", {"objectId": obj_id})
        return any(l["type"] == "click" for l in res.get("listeners", []))

    verified = []
    for i, meta in enumerate(pool_meta):
        if meta.get("reactOnClick"):
            verified.append(True)
            continue
        ev = cdp.send("Runtime.evaluate",
                      {"expression": f"window.__flowscout_pool[{i}]", "returnByValue": False})
        obj_id = ev["result"].get("objectId")
        verified.append(bool(obj_id) and has_click(obj_id))
    return verified


def _dedupe_outermost(page, indices: list[int]) -> set[int]:
    """Among the given indices into window.__flowscout_pool, keep only
    the outermost element per containment cluster. A verified card
    commonly contains its own text-bearing children (a heading, a
    paragraph) that _verify_pool's ancestor walk *also* verifies -- the
    click bubbles from any of them to the same handler on the same
    element, so without this a single real control promotes as several
    duplicate candidates (confirmed on Site B: one wizard card
    produced 3, including one with an empty label from an icon-only
    wrapper div). Mirrors the old cursor-heuristic code's same dedup,
    just applied to CDP-verified elements instead of a visual guess."""
    if not indices:
        return set()
    kept = page.evaluate(
        """(indices) => {
            const els = indices.map(i => window.__flowscout_pool[i]);
            return indices.filter((idx, pos) =>
                !els.some((other, otherPos) => otherPos !== pos && other.contains(els[pos]))
            );
        }""",
        indices,
    )
    return set(kept)


def _detect_choice_groups(page, indices: list[int]) -> set[int]:
    """Among verified, deduped handler-discovered indices, which ones are
    part of a "choice group" -- 2+ siblings under the same parent, the
    shape a wizard's mutually-exclusive option cards commonly take
    (confirmed on Site B: Option A/Option B/Option C are three
    identically-styled siblings under one grid container). Grouping by
    literal parent-element identity rather than a class/text heuristic --
    JS Map supports object keys natively, so this doesn't need a
    generated string key that could collide or miss a match. Feeds
    ElementCandidate.is_choice, which identity.py's mutating_signature_set
    uses to keep two flows that picked different siblings from collapsing
    into one (see ROADMAP.md's state-fingerprint-blind-to-configuration
    entry)."""
    if not indices:
        return set()
    kept = page.evaluate(
        """(indices) => {
            const els = indices.map(i => window.__flowscout_pool[i]);
            const byParent = new Map();
            els.forEach((el, pos) => {
                const p = el.parentElement;
                if (!p) return;
                if (!byParent.has(p)) byParent.set(p, []);
                byParent.get(p).push(pos);
            });
            const choicePos = new Set();
            for (const posList of byParent.values()) {
                if (posList.length >= 2) posList.forEach(p => choicePos.add(p));
            }
            return indices.filter((idx, pos) => choicePos.has(pos));
        }""",
        indices,
    )
    return set(kept)


def _build_candidate(el: dict, via: str, current_domain: str, allowed_domains: list[str],
                      exclude_patterns: list[str] | None, seen: set,
                      is_choice: bool = False) -> tuple[ElementCandidate | None, dict | None]:
    """Returns (candidate, occlusion_dict) -- exactly one is non-None,
    or both are None for a duplicate signature already seen. `is_choice`
    is only meaningful for the non-select branch below -- a select
    option is always is_choice=True by construction (see
    identity.py's mutating_signature_set)."""
    if el.get("tag") == "select":
        # A <select>'s own dataTest/id names the CONTROL, shared by every
        # one of its options -- the generic signature scheme below would
        # collapse "sort low-to-high" and "sort high-to-low" into the same
        # signature and silently drop all but the first. The option's own
        # value is what actually distinguishes one choice from another.
        base = el["dataTest"] or el["id"] or el["text"]
        signature = f"select-choice:{base}:{el['selectValue']}"
        if signature in seen:
            return None, None
        seen.add(signature)
        label = el["text"] or el["selectValue"] or "option"
        # Deliberately NOT prefixed "select-": normalize_signature's
        # known_prefixes list generalizes away a specific *item*
        # ("add-to-cart-sauce-labs-backpack" -> "add-to-cart-*") and
        # includes "select-" -- which would collapse "sort low-to-high"
        # and "sort high-to-low" to the identical "select-*" and defeat
        # the whole point. Caught by testing the actual output, not
        # assumed safe: an earlier version of this line used a
        # "select-choice-" prefix, which still starts with "select-" and
        # collapsed both anyway. "choice-" isn't one of the known prefixes.
        norm_signature = normalize_signature(f"choice-{base}-{el['selectValue']}")
        risk, reason = classify(label, None, current_domain, allowed_domains, exclude_patterns)
        return ElementCandidate(
            signature=signature, norm_signature=norm_signature, label=label,
            selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
            is_choice=True,
        ), None

    if el.get("tag") == "radio":
        # Same reasoning as <select> above: the group name is shared by
        # every option in it, so the specific value picked has to be part
        # of the signature or every radio in a group collapses to one.
        base = el["dataTest"] or el["id"] or el["radioGroup"]
        signature = f"radio-choice:{base}:{el['radioValue']}"
        if signature in seen:
            return None, None
        seen.add(signature)
        label = el["text"] or el["radioValue"] or "option"
        # "choice-" prefix, not "radio-": normalize_signature's
        # known_prefixes list would be a coincidence away from swallowing
        # a literal "radio-" prefix the same way "select-" already did --
        # reusing the one already proven safe rather than trusting a new
        # one without the same live check.
        norm_signature = normalize_signature(f"choice-{base}-{el['radioValue']}")
        risk, reason = classify(label, None, current_domain, allowed_domains, exclude_patterns)
        return ElementCandidate(
            signature=signature, norm_signature=norm_signature, label=label,
            selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
            is_choice=True,
        ), None

    if el.get("tag") == "checkbox":
        # Unlike select/radio, there's no "value chosen among alternatives"
        # -- a checkbox has exactly one action (toggle), so the signature
        # only needs to identify *which* checkbox, not a value picked from
        # a set. Still is_choice=True: two flows ending up with different
        # boxes checked are different flows for identity purposes, even
        # though checking one doesn't exclude any other (see _DISCOVER_JS).
        base = el["dataTest"] or el["id"] or el["checkboxName"]
        signature = f"checkbox-toggle:{base}:{el['checkboxValue']}"
        if signature in seen:
            return None, None
        seen.add(signature)
        label = el["text"] or el["checkboxName"] or "checkbox"
        norm_signature = normalize_signature(f"choice-{base}-{el['checkboxValue']}")
        risk, reason = classify(label, None, current_domain, allowed_domains, exclude_patterns)
        return ElementCandidate(
            signature=signature, norm_signature=norm_signature, label=label,
            selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
            is_choice=True,
        ), None

    sig_key = el["dataTest"] or el["id"] or f"{el['tag']}:{el['text']}"
    signature = f"data-test:{sig_key}" if el["dataTest"] else (
        f"id:{sig_key}" if el["id"] else f"text:{sig_key}"
    )
    if signature in seen:
        return None, None
    seen.add(signature)
    label = el["text"] or el["dataTest"] or el["id"] or el["tag"]
    if el.get("occluded"):
        return None, {
            "label": describe_action(el, None),
            "reason": f"obstructed by another element ({el.get('occludedBy') or 'unknown'}) at click point",
        }
    norm_signature = normalize_signature(el["dataTest"] or el["id"] or el["text"] or el["tag"])
    risk, reason = classify(label, el["href"] or None, current_domain, allowed_domains, exclude_patterns)
    return ElementCandidate(
        signature=signature, norm_signature=norm_signature, label=label,
        selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
        is_choice=is_choice,
    ), None


def discover_candidates(page, current_domain: str, allowed_domains: list[str],
                         exclude_patterns: list[str] | None = None
                         ) -> tuple[list[ElementCandidate], list[dict], list[dict], list[dict]]:
    """Returns (candidates, occluded, unclassified, disabled).

    occluded: on-screen and otherwise valid candidates currently covered
    by something else (an open menu panel, a modal) per the browser's own
    elementFromPoint, so clicking them would just time out. Reported,
    never explored.

    candidates now includes both markup-matched elements (a/button/
    [role=button]/...) and div-as-button elements CDP confirmed have a
    real click listener (see _verify_pool) -- discovered_via on each
    tells them apart.

    unclassified: only populated if CDP verification itself failed
    (session error) -- a fallback to the old cursor/role/tabindex guess,
    expected to be empty on a normal Chromium run.

    disabled: elements CDP confirmed are real (have a click listener)
    but are currently disabled (aria-disabled, a disabled-looking class,
    or pointer-events:none) -- correctly never clicked, surfaced anyway
    since "this control exists but isn't available right now" is a real
    finding (e.g. a wizard option gated behind an earlier choice).
    """
    payload = page.evaluate(_DISCOVER_JS)
    formal: list[tuple[dict, str, bool]] = [(el, "markup", False) for el in payload["candidates"]]
    formal += [(el, "markup", False) for el in payload.get("selects", [])]
    # Radios/checkboxes are markup-matched the same way selects are: found by
    # tag, not by CDP handler-verification (they're native controls, always
    # real). is_choice is set inside _build_candidate itself for these tags
    # (mirrors "select"), so the flag here is a don't-care placeholder.
    formal += [(el, "markup", False) for el in payload.get("radios", [])]
    formal += [(el, "markup", False) for el in payload.get("checkboxes", [])]
    pool = payload["pool"]

    unclassified: list[dict] = []
    promoted: list[tuple[dict, str, bool]] = []
    disabled_raw: list[dict] = []
    if pool:
        try:
            verified = _verify_pool(page, pool)
        except Exception:
            unclassified = _aggregate_unclassified(payload["legacyUnclassified"])
        else:
            verified_indices = [i for i, ok in enumerate(verified) if ok]
            outermost = _dedupe_outermost(page, verified_indices)
            choice_indices = _detect_choice_groups(page, list(outermost))
            for i in outermost:
                meta = pool[i]
                if meta.get("ariaDisabled"):
                    disabled_raw.append(meta)
                else:
                    promoted.append((meta, "handler", i in choice_indices))

    candidates: list[ElementCandidate] = []
    occluded: list[dict] = []
    seen: set = set()
    for el, via, is_choice in formal + promoted:
        cand, occ = _build_candidate(el, via, current_domain, allowed_domains, exclude_patterns, seen,
                                      is_choice=is_choice)
        if cand is not None:
            candidates.append(cand)
        elif occ is not None:
            occluded.append(occ)
    # priority order for exploration: safe, then mutating, then destructive.
    # Within a tier, handler-discovered candidates sort before markup ones --
    # found necessary on a real crawl: Site B's wizard cards, positioned
    # after ~13 ordinary nav links in the candidate list, never survived
    # max_breadth_per_state's truncation even after CDP correctly found
    # them, because truncation happens before anything is clicked and the
    # old order put the newly-detected content last. Handler-discovered
    # elements are exactly the ones this detection mechanism exists to
    # reach -- worth spending scarce breadth budget on first, not last.
    order = {Risk.SAFE: 0, Risk.MUTATING: 1, Risk.DESTRUCTIVE: 2}
    via_order = {"handler": 0, "markup": 1}
    candidates.sort(key=lambda c: (order[c.risk], via_order.get(c.discovered_via, 1)))
    disabled = _aggregate_unclassified(disabled_raw)
    return candidates, occluded, unclassified, disabled


def build_locator(page, el_meta: dict):
    if el_meta.get("dataTest"):
        return page.locator(f'[data-test="{el_meta["dataTest"]}"]').first
    if el_meta.get("id"):
        return page.locator(f'#{el_meta["id"]}').first
    if el_meta.get("tag") == "radio":
        # Structural (type+name+value), not text: radio labels are often
        # short and generic ("Yes", "Small") -- more likely to collide
        # elsewhere on the page than a select's own dataTest/id would be,
        # so this is tried before falling all the way to text.
        return page.locator(
            f'input[type="radio"][name="{el_meta["radioGroup"]}"][value="{el_meta["radioValue"]}"]'
        ).first
    if el_meta.get("tag") == "checkbox":
        return page.locator(
            f'input[type="checkbox"][name="{el_meta["checkboxName"]}"][value="{el_meta["checkboxValue"]}"]'
        ).first
    if el_meta.get("href"):
        return page.locator(f'{el_meta["tag"]}[href="{el_meta["href"]}"]').first
    return page.get_by_text(el_meta["text"], exact=True).first


def _synth_value(name: str, type_: str, credentials: dict) -> str:
    name = name.lower()
    for key, val in credentials.items():
        if key.lower() in name:
            return val
    if type_ == "password":
        return "FlowScout!1"
    if type_ == "email" or "email" in name:
        return "flowscout_test@example.com"
    if type_ == "number" or "zip" in name or "postal" in name:
        return "12345"
    if "first" in name:
        return "Flow"
    if "last" in name:
        return "Scout"
    return "flowscout_test"


def fill_enclosing_form(page, el_meta: dict, credentials: dict) -> dict | None:
    """Best-effort: fill every visible input/select/textarea in the form
    that contains the target element, using config credentials where the
    field name matches, else a synthetic safe value. Mirrors the spec's
    'boundary/valid-value' idea in its simplest form for M0.

    Returns None if `el_meta` isn't actually a submit control (so the
    caller knows this wasn't a form submission at all -- e.g. a "Cancel"
    button with type="button" inside a form shouldn't be mislabeled as
    submitting it), otherwise a {field_name: value_used} summary (values
    masked for password fields) so the report can say what was filled,
    which matters most for login steps.
    """
    if not el_meta.get("inForm") or el_meta.get("type") in ("button", "select-option",
                                                               "radio-choice", "checkbox-toggle"):
        return None
    target = build_locator(page, el_meta)
    form = target.locator("xpath=ancestor::form[1]")
    try:
        count = form.locator("input, select, textarea").count()
    except Exception:
        return {}
    summary: dict[str, str] = {}
    for i in range(count):
        field = form.locator("input, select, textarea").nth(i)
        try:
            tag = field.evaluate("e => e.tagName.toLowerCase()")
            type_ = (field.get_attribute("type") or "text").lower()
            if type_ in ("submit", "button", "checkbox", "radio", "hidden"):
                continue
            name = (field.get_attribute("name") or field.get_attribute("id")
                     or field.get_attribute("placeholder") or "")
            display_name = name or tag
            if tag == "select":
                field.select_option(index=1)
                summary[display_name] = "(selected)"
            else:
                value = _synth_value(name, type_, credentials)
                field.fill(value)
                summary[display_name] = "•" * min(len(value), 10) if type_ == "password" else value
        except Exception:
            continue  # non-fatal: leave field as-is, action may still succeed or will error visibly
    return summary


def _settle(page, max_wait_ms: int = 1500):
    """Wait for in-flight CSS transitions/animations to finish (e.g. a
    slide-out menu closing) instead of a flat sleep. A fixed sleep either
    undershoots real transitions (saucedemo's menu takes 500ms -- a 250ms
    sleep catches it mid-slide and misreads its links as on-screen) or
    wastes time overshooting fast ones. Guarded by max_wait_ms so a looping
    animation (e.g. a spinner) can't hang the crawl."""
    try:
        page.evaluate(
            """(maxWait) => new Promise(resolve => {
                // A transition triggered by this click's class/style change isn't
                // necessarily registered in getAnimations() yet on the very next
                // tick -- the browser needs a style recalc first. Two rAFs give
                // it that frame before we ask what's actually running.
                requestAnimationFrame(() => requestAnimationFrame(() => {
                    Promise.race([
                        Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))),
                        new Promise(res => setTimeout(res, maxWait)),
                    ]).then(resolve);
                }));
            })""",
            max_wait_ms,
        )
    except Exception:
        pass
    page.wait_for_timeout(80)  # let layout/paint flush after animations resolve


def _read_choice_state(page, el_meta: dict) -> dict[str, str]:
    """Read-only: which radio option is currently selected (per group)
    and which checkboxes are currently checked, in the same form
    el_meta's own submit control belongs to -- recorded for the flow's
    label even for parameters this specific DFS path never explicitly
    clicked. A submitted form's real values shouldn't be invisible just
    because nothing on this particular path happened to touch them --
    they still carry SOME state, the page's own default, and that's what
    actually got submitted.

    Deliberately kept separate from fill_enclosing_form's own summary
    (see perform_action): that one becomes Transition.form_fields, which
    M4's codegen turns into `.fill()` calls -- and `.fill()` raises on a
    radio/checkbox input. This is display-only and never reaches
    form_fields."""
    if not el_meta.get("inForm"):
        return {}
    try:
        target = build_locator(page, el_meta)
        form = target.locator("xpath=ancestor::form[1]")
        result = form.evaluate("""(formEl) => {
            // Same label lookup as inputLabelText() in _DISCOVER_JS: a
            // label[for=id] first, but real markup (e.g. httpbin's own
            // pizza form) commonly wraps the input in <label> instead of
            // using for/id at all -- checked.closest('label') has to be
            // tried too, or this silently falls back to the raw value
            // attribute ("small") instead of the display text ("Small").
            function labelFor(el) {
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (lbl) return (lbl.innerText || '').trim();
                }
                const wrapping = el.closest('label');
                return wrapping ? (wrapping.innerText || '').trim() : '';
            }
            const out = {};
            const seenGroups = new Set();
            for (const el of formEl.querySelectorAll('input[type=radio]')) {
                const name = el.name || '';
                if (!name || seenGroups.has(name)) continue;
                seenGroups.add(name);
                const checked = formEl.querySelector(`input[type=radio][name="${CSS.escape(name)}"]:checked`);
                if (!checked) continue;
                out[name] = (labelFor(checked) || checked.value || '').slice(0, 40);
            }
            for (const el of formEl.querySelectorAll('input[type=checkbox]')) {
                if (!el.checked) continue;
                const name = el.name || el.id || '';
                if (!name) continue;
                out[name] = (labelFor(el) || el.value || 'checked').slice(0, 40);
            }
            return out;
        }""")
        return result or {}
    except Exception:
        return {}


def perform_action(page, el_meta: dict, credentials: dict) -> tuple[dict | None, dict]:
    """Returns (fill_summary, choice_state). fill_summary is None if this
    action wasn't a form submission -- see fill_enclosing_form.
    choice_state (see _read_choice_state) is always a dict, empty if
    there was nothing to observe; it's for the label only and never
    feeds Transition.form_fields."""
    loc = build_locator(page, el_meta)
    if el_meta.get("tag") == "select":
        # build_locator resolves the <select> itself (via its own
        # dataTest/id) -- select_option targets the value recorded at
        # discovery time, not whatever happens to be selected on replay.
        loc.select_option(value=el_meta["selectValue"], timeout=8000)
        try:
            page.wait_for_load_state("load", timeout=8000)
        except Exception:
            pass
        _settle(page)
        return None, {}
    fill_summary = fill_enclosing_form(page, el_meta, credentials)
    choice_state = _read_choice_state(page, el_meta)
    loc.click(timeout=8000)
    try:
        page.wait_for_load_state("load", timeout=8000)
    except Exception:
        pass
    _settle(page)
    return fill_summary, choice_state


def current_domain(url: str) -> str:
    return urlsplit(url).netloc
