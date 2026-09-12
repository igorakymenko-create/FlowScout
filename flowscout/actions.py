"""Element discovery, locator resolution, and form-filling.

Kept separate from crawler.py so the "how do we interact with a page"
concerns are isolated from the "how do we walk the graph" concerns.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .fingerprint import normalize_signature
from .models import ElementCandidate, Risk
from .risk import classify

_DISCOVER_JS = r"""
() => {
    // Recurses into every OPEN shadow root under `root` (a document or
    // shadow root itself), collecting every element matching `selector`
    // at any depth -- plain document.querySelectorAll cannot see past a
    // shadow boundary at all (verified live: a button rendered inside
    // an open shadow root scored ZERO matches via querySelectorAll,
    // even though Playwright's OWN locator engine finds and clicks it
    // just fine -- CSS/text locators pierce open shadow DOM
    // automatically, so replay was never the problem here, only
    // discovery was). A CLOSED shadow root (`el.shadowRoot` returns
    // null) is structurally unreachable by ANY method, including
    // Playwright's own -- not something this can work around.
    function queryAllDeep(root, selector) {
        const found = Array.from(root.querySelectorAll(selector));
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) found.push(...queryAllDeep(el.shadowRoot, selector));
        }
        return found;
    }

    // document.elementFromPoint() does NOT pierce an open shadow root
    // on its own -- verified live, not assumed: it stops at the shadow
    // HOST (the element hosting the shadow tree), not the actual
    // element rendered inside it. Every shadow-DOM candidate this
    // queryAllDeep() above finds would otherwise register as
    // "occluded by its own host" and never get clicked -- a real,
    // blocking bug for this whole feature, found by testing end to
    // end rather than assuming the occlusion check would just work.
    // ShadowRoot has its OWN elementFromPoint() that resolves within
    // that specific tree -- recursing through it reaches the true
    // topmost element a real click would actually hit, however many
    // shadow roots deep.
    function deepElementFromPoint(x, y) {
        let el = document.elementFromPoint(x, y);
        while (el && el.shadowRoot) {
            const inner = el.shadowRoot.elementFromPoint(x, y);
            if (!inner || inner === el) break;
            el = inner;
        }
        return el;
    }

    // Plain 'a' (not 'a[href]'): some real, functional links are JS-driven
    // with no href at all -- e.g. saucedemo's cart icon is
    // <a data-test="shopping-cart-link" class="shopping_cart_link"> with
    // no href attribute. Requiring href silently made it (and anything
    // built the same way) invisible to discovery from the very first run.
    const sel = 'a, button, input[type=submit], input[type=button], [role="button"]';
    const nodes = queryAllDeep(document, sel);
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

    // Real forms in modern JS apps commonly have no <form> element at
    // all -- React/Vue apps build "forms" as plain containers (a <div>
    // wrapping the fields) with a submit control marked type="button"
    // specifically to suppress native form submission, since the app
    // handles it via JS instead. Requiring a literal <form> ancestor
    // made inForm false on exactly these apps, which meant
    // fill_enclosing_form()/the choice-state reader (actions.py) never
    // even tried to fill anything before clicking -- a login/search/
    // filter submit on such a site was clicked with every field still
    // empty, silently. Falls back to the closest ancestor that
    // actually CONTAINS a real input/select/textarea -- capped by
    // descendant count so this doesn't walk all the way up to <body>
    // and "find" the whole page as one giant form.
    function closestFormLike(el) {
        const real = el.closest('form');
        if (real) return real;
        let n = el.parentElement;
        while (n && n !== document.body) {
            if (n.querySelector('input, select, textarea') && n.querySelectorAll('*').length <= 200) {
                return n;
            }
            n = n.parentElement;
        }
        return null;
    }

    // Real-world motivation: a live user question about a bank FAQ page
    // where a topic link had no matching anchor and silently "led to
    // itself" -- fingerprint.py always strips fragments, so a dangling
    // in-page anchor and a working one look identical to the crawler.
    // This checks an objective, present-tense fact (does an element with
    // this id/name exist in the DOM right now), not the link's intent --
    // consistent with the project's own rule of observing, never guessing
    // at correctness. Deliberately skips a literal href="#" (no fragment
    // at all): that's a ubiquitous, legitimate idiom for a JS-driven
    // button, not a broken link, and flagging it would be pure noise.
    function anchorTargetMissing(href) {
        if (!href) return false;
        let u;
        try { u = new URL(href, location.href); } catch (e) { return false; }
        const frag = u.hash ? u.hash.slice(1) : '';
        if (!frag) return false;
        // A fragment on a DIFFERENT document (different origin/path/query)
        // targets that OTHER page's DOM, which isn't available to check
        // from here -- only claim "missing" about this document's own DOM.
        const samePage = u.origin === location.origin && u.pathname === location.pathname
            && u.search === location.search;
        if (!samePage) return false;
        return !document.getElementById(frag) && document.getElementsByName(frag).length === 0;
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

    // A full-page fixed overlay (a modal/dialog backdrop) blocks
    // EVERYTHING outside itself, regardless of on/off-screen status --
    // found live on a real production crawl: a "Sign In" click opened
    // an "Account Access" modal, and a background link far below the
    // fold got promoted as a normal candidate anyway, because the
    // per-candidate occlusion check just below only ever runs for
    // on-screen elements (elementFromPoint can't assess a point
    // outside the current viewport) -- it assumes scrolling to an
    // off-screen element will make it reachable, which is exactly
    // wrong for a FIXED overlay: it stays pinned over the viewport at
    // any scroll position, so scrolling never uncovers what's behind
    // it. Every later replay of that "candidate" failed the exact same
    // way, indistinguishable from ordinary slowness until inspected
    // directly (a screenshot showing the modal, taken mid-investigation).
    function findBlockingOverlay() {
        for (const el of document.querySelectorAll('body *')) {
            const style = getComputedStyle(el);
            if (style.position !== 'fixed' || style.display === 'none' || style.visibility === 'hidden') continue;
            const r = el.getBoundingClientRect();
            if (r.width >= vw * 0.9 && r.height >= vh * 0.9) return el;
        }
        return null;
    }
    const blockingOverlay = findBlockingOverlay();

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
            atPoint = deepElementFromPoint(cx, cy);
            occluded = !atPoint || !(n.contains(atPoint) || atPoint.contains(n));
        }
        // Applies regardless of inViewport above -- see
        // findBlockingOverlay's own comment for why the on-screen-only
        // check above can't catch this on its own.
        if (blockingOverlay && !blockingOverlay.contains(n)) {
            occluded = true;
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
            anchorTargetMissing: anchorTargetMissing(n.getAttribute('href') || ''),
            // Icon-only elements (a logo link, an icon button) often carry
            // no text of their own -- the accessible name lives on a child
            // <img alt="..."> instead (e.g. a logo <a> wrapping <img
            // alt="ACME">), or on a title attribute. Without this, the
            // label falls all the way through to the bare tag name ("a"),
            // which tells a reader nothing about what was actually clicked.
            text: firstBlockText(n.innerText || n.value || n.getAttribute('aria-label')
                   || (n.querySelector('img[alt]') || {}).alt || n.getAttribute('title') || ''),
            type: (n.getAttribute('type') || '').toLowerCase(),
            inForm: !!closestFormLike(n),
            occluded: occluded,
            occludedBy: occluded
                ? (atPoint ? (atPoint.className || atPoint.tagName || '').toString().slice(0, 60)
                            : (blockingOverlay ? 'a full-page overlay/modal (' + blockingOverlay.tagName + ')' : ''))
                : '',
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
            anchorTargetMissing: anchorTargetMissing(el.getAttribute('href') || ''),
            // See firstBlockText above -- matters most here: a wizard
            // card's own text commonly spans a heading + description,
            // which is exactly the shape that broke replay.
            text: firstBlockText(el.innerText || el.getAttribute('aria-label')
                   || (el.querySelector('img[alt]') || {}).alt || el.getAttribute('title') || ''),
            type: (el.getAttribute('type') || '').toLowerCase(),
            inForm: !!closestFormLike(el),
            // Same full-page-overlay reasoning as the main candidates
            // above -- a div-as-button element behind an open modal is
            // just as unreachable as an <a>/<button> would be.
            occluded: !!(blockingOverlay && !blockingOverlay.contains(el)),
            occludedBy: (blockingOverlay && !blockingOverlay.contains(el))
                ? ('a full-page overlay/modal (' + blockingOverlay.tagName + ')') : '',
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
    for (const s of queryAllDeep(document, 'select')) {
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
                inForm: !!closestFormLike(s),
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
    // Falls all the way to a sibling <label> anywhere in the same
    // parent as a last resort (added alongside role-checkbox/role-radio
    // support below) -- found live that a component library (Radix/
    // shadcn's Checkbox) can render a <label> next to a control with a
    // `for` attribute that doesn't even match anything (a decoy hidden
    // input has no id at all), so neither the for/id nor the
    // wrapping-label lookup above finds it, even though a human reading
    // the page would obviously associate the two.
    function inputLabelText(el) {
        if (el.id) {
            const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lbl) return firstBlockText(lbl.innerText || '');
        }
        const wrapping = el.closest('label');
        if (wrapping) return firstBlockText(wrapping.innerText || '');
        if (el.getAttribute('aria-label') || el.value) {
            return firstBlockText(el.getAttribute('aria-label') || el.value || '');
        }
        if (el.parentElement) {
            const siblingLabel = el.parentElement.querySelector('label');
            if (siblingLabel) return firstBlockText(siblingLabel.innerText || '');
        }
        return '';
    }
    function isUsableInput(el) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        // pointerEvents check (Aug 2026): found live on a real crawl --
        // a component library (Radix/shadcn's Checkbox) renders the
        // REAL interactive control as a styled <button role="checkbox">
        // and keeps a native <input type=checkbox> alongside it purely
        // for form semantics, deliberately non-interactive
        // (pointer-events:none, opacity:0, translated off-screen).
        // Every one of the other checks here already passed for that
        // decoy (nonzero size, not display:none, not .disabled) -- only
        // pointer-events catches it. Clicking it was structurally
        // guaranteed to time out: Playwright correctly waits forever
        // for an element to become "actionable" that never can be.
        return !(r.width <= 0 || r.height <= 0 || style.visibility === 'hidden'
                 || style.display === 'none' || el.disabled || style.pointerEvents === 'none');
    }

    // Radio groups: "pick one of N" is structurally the same choice a
    // <select> represents -- one candidate per option (grouped by `name`,
    // the attribute that actually defines a radio group in HTML), each
    // is_choice, same as a select option. A radio with no `name` isn't
    // really grouped with anything; treated as a lone group of one rather
    // than dropped.
    const radios = [];
    let ungroupedSeq = 0;
    for (const el of queryAllDeep(document, 'input[type=radio]')) {
        if (!isUsableInput(el)) continue;
        const groupName = el.name || `__ungrouped_${ungroupedSeq++}`;
        radios.push({
            tag: 'radio',
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: '',
            text: inputLabelText(el),
            type: 'radio-choice',
            inForm: !!closestFormLike(el),
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
    for (const el of queryAllDeep(document, 'input[type=checkbox]')) {
        if (!isUsableInput(el)) continue;
        checkboxes.push({
            tag: 'checkbox',
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: '',
            text: inputLabelText(el),
            type: 'checkbox-toggle',
            inForm: !!closestFormLike(el),
            occluded: false, occludedBy: '',
            className: (el.className || '').toString().slice(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            ariaHasPopup: '', ariaExpandedSet: false,
            ariaControls: '', controlledTag: '', controlledClass: '',
            checkboxName: el.name || el.id || '', checkboxValue: el.value || 'on',
        });
    }

    // role="checkbox"/role="radio" custom controls (Radix/shadcn/
    // Headless-UI style component libraries, extremely common in
    // current React apps): the REAL, visible, clickable element is
    // often a styled <button role="checkbox"> (or a <div role="radio">
    // inside a role="radiogroup"), not a native <input> at all -- a
    // native input, if present alongside it, exists purely for form
    // semantics and is deliberately non-interactive (see
    // isUsableInput's own pointerEvents check above, which excludes
    // exactly that decoy). `[role="checkbox"]`/`[role="radio"]` on a
    // literal <input> is skipped here -- those are already covered by
    // the native loops above, role attribute or not.
    //
    // Label resolution is JS-side best-effort only, used for THIS
    // candidate's display text and signature -- actually relocating it
    // on replay (actions.py's build_locator) asks Playwright's own
    // get_by_role(role, name=...) to compute the real accessible name
    // again, which is more reliable than reimplementing that
    // computation here. Verified live: Playwright's own accessible-name
    // resolution reached a sibling <label> with no formal
    // aria-labelledby/for wiring at all on a real Radix Checkbox
    // (alternateqa.com's "Show password" toggle) -- inputLabelText's
    // sibling-<label> fallback mirrors that specifically so the label
    // captured here has a good chance of matching what get_by_role
    // will actually find.
    const roleControls = [];
    for (const el of queryAllDeep(document, '[role="checkbox"], [role="radio"]')) {
        if (el.tagName === 'INPUT') continue;
        if (!isUsableInput(el)) continue;
        const role = el.getAttribute('role');
        const checked = el.getAttribute('aria-checked') === 'true' || el.getAttribute('data-state') === 'checked';
        const name = inputLabelText(el) || el.getAttribute('title') || '';
        let group = '__ungrouped_role_checkbox';
        if (role === 'radio') {
            const groupEl = el.closest('[role="radiogroup"]');
            group = groupEl
                ? (groupEl.getAttribute('data-test') || groupEl.id || groupEl.getAttribute('aria-label') || 'radiogroup')
                : `__ungrouped_${roleControls.length}`;
        }
        roleControls.push({
            tag: role === 'radio' ? 'role-radio' : 'role-checkbox',
            dataTest: el.getAttribute('data-test') || el.getAttribute('data-testid') || '',
            id: el.id || '',
            href: '',
            text: name,
            type: role === 'radio' ? 'role-radio-choice' : 'role-checkbox-toggle',
            inForm: !!closestFormLike(el),
            occluded: false, occludedBy: '',
            className: (el.className || '').toString().slice(0, 120),
            ariaLabel: el.getAttribute('aria-label') || '',
            ariaHasPopup: '', ariaExpandedSet: false,
            ariaControls: '', controlledTag: '', controlledClass: '',
            roleAccessibleName: name, roleChecked: checked, roleGroup: group,
        });
    }

    // Validation-error visibility (Sep 2026): state_fingerprint() is
    // built from (url pattern, candidate signatures) alone -- blind to
    // page TEXT entirely. Rejecting an invalid form submission typically
    // keeps the same URL and the same field/submit-button set, so the
    // error state fingerprints IDENTICALLY to the pre-submit state and
    // the crawler read it as a plain revisit, never exploring further --
    // an entire class of negative scenarios (bad input, duplicate
    // values, required-field misses) was structurally invisible, not
    // merely deprioritized. Detected via standards-based signals only,
    // never a guessed framework-specific CSS class name:
    const validationSignals = [];
    for (const el of queryAllDeep(document, '[aria-invalid="true"]')) {
        const name = el.getAttribute('name') || el.id || el.getAttribute('aria-label')
            || el.getAttribute('placeholder') || el.tagName;
        validationSignals.push('invalid-field:' + name);
    }
    for (const el of queryAllDeep(document, '[role="alert"]')) {
        const msg = firstBlockText(el.innerText || '');
        if (msg) validationSignals.push('alert:' + msg);
    }
    // :user-invalid (NOT plain :invalid) -- verified live: :invalid
    // matches an empty `required` field from the very first page load,
    // before any submit attempt ever happens, so it never actually
    // distinguishes "rejected" from "untouched" and would just be
    // constant noise in the fingerprint. :user-invalid only matches
    // once the field has genuinely been interacted with/submitted,
    // which is exactly the discriminator needed here. Guarded in a
    // try/catch since pseudo-class support can't be assumed forever.
    try {
        for (const el of queryAllDeep(document, ':user-invalid')) {
            const name = el.getAttribute('name') || el.id || el.tagName;
            const msg = (el.validationMessage || '').trim().slice(0, 60);
            validationSignals.push('native-invalid:' + name + (msg ? ':' + msg : ''));
        }
    } catch (e) { /* :user-invalid unsupported -- aria-invalid/role=alert above still apply */ }

    return {candidates, pool, legacyUnclassified, selects, radios, checkboxes, roleControls, validationSignals};
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
    if el_meta.get("tag") == "role-radio":
        group = (el_meta.get("roleGroup") or "").lstrip("_") or "options"
        return f'Select "{text}" in "{group}"'
    if el_meta.get("tag") == "role-checkbox":
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
            is_choice=True, choice_group=base,
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
            is_choice=True, choice_group=base,
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
            is_choice=True, choice_group=base,
        ), None

    if el.get("tag") == "role-checkbox":
        # Same shape as the native "checkbox" branch above -- one toggle
        # action, is_choice=True for the same identity reason -- but the
        # element is a role="checkbox" custom control (Radix/shadcn
        # style), not a native <input>, so there's no name/value pair to
        # lean on; roleAccessibleName (JS-side best-effort label lookup,
        # see _DISCOVER_JS) is the identifying "value" instead.
        base = el["dataTest"] or el["id"] or el["roleAccessibleName"]
        if not base:
            # Mirrors the generic "no reliable locator" guard below --
            # without data-test/id/accessible name, build_locator()'s
            # own get_by_role(name="") fallback would match ANY checkbox
            # with no name at all, the exact class of bug this whole
            # feature exists to fix, not reproduce for a new element shape.
            return None, {
                "label": describe_action(el, None),
                "reason": "no reliable locator for this checkbox (no data-test/id/accessible name) "
                          "-- skipped rather than risk clicking the wrong element",
            }
        signature = f"role-checkbox-toggle:{base}:{el['roleChecked']}"
        if signature in seen:
            return None, None
        seen.add(signature)
        label = el["roleAccessibleName"] or el["dataTest"] or el["id"] or "checkbox"
        norm_signature = normalize_signature(f"choice-{base}-{el['roleChecked']}")
        risk, reason = classify(label, None, current_domain, allowed_domains, exclude_patterns)
        return ElementCandidate(
            signature=signature, norm_signature=norm_signature, label=label,
            selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
            is_choice=True, choice_group=base,
        ), None

    if el.get("tag") == "role-radio":
        # Same shape as the native "radio" branch above -- group name
        # (roleGroup, from the nearest role="radiogroup" ancestor, or an
        # __ungrouped_N fallback) plus the specific option picked.
        # roleGroup alone never identifies which SPECIFIC option this is
        # on replay though -- that still needs data-test/id/accessible
        # name, same reasoning as role-checkbox above.
        base = el["dataTest"] or el["id"] or el["roleGroup"]
        name = el["roleAccessibleName"]
        if not (el["dataTest"] or el["id"] or name):
            return None, {
                "label": describe_action(el, None),
                "reason": "no reliable locator for this radio option (no data-test/id/accessible name) "
                          "-- skipped rather than risk clicking the wrong element",
            }
        value_key = name or el["dataTest"] or el["id"]
        signature = f"role-radio-choice:{base}:{value_key}"
        if signature in seen:
            return None, None
        seen.add(signature)
        label = name or el["dataTest"] or el["id"] or "option"
        norm_signature = normalize_signature(f"choice-{base}-{value_key}")
        risk, reason = classify(label, None, current_domain, allowed_domains, exclude_patterns)
        return ElementCandidate(
            signature=signature, norm_signature=norm_signature, label=label,
            selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
            is_choice=True, choice_group=base,
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
    if not (el.get("dataTest") or el.get("id") or el.get("href") or el.get("text")):
        # build_locator()'s only remaining option for THIS element would be
        # page.get_by_text(el["text"], exact=True) with an EMPTY string --
        # found on a real crawl (alternateqa.com, an icon-only button with
        # no aria-label/data-test/id) to resolve to a completely unrelated,
        # hidden <div>, then hang for the entire click timeout waiting for
        # it to become visible. An element with no data-test, no id, no
        # href and no visible text has no reliable way to be relocated on
        # replay at all -- reported the same way an occluded candidate
        # already is (visible in the report's Safety register, never
        # explored) rather than risking a locator that's guaranteed to
        # resolve to the wrong element.
        return None, {
            "label": describe_action(el, None),
            "reason": "no reliable locator for this element (no data-test/id/href/visible text) "
                      "-- skipped rather than risk clicking the wrong element",
        }
    norm_signature = normalize_signature(el["dataTest"] or el["id"] or el["text"] or el["tag"])
    risk, reason = classify(label, el["href"] or None, current_domain, allowed_domains, exclude_patterns)
    return ElementCandidate(
        signature=signature, norm_signature=norm_signature, label=label,
        selector=json.dumps(el), risk=risk, risk_reason=reason, discovered_via=via,
        is_choice=is_choice, anchor_target_missing=bool(el.get("anchorTargetMissing", False)),
    ), None


def discover_candidates(page, current_domain: str, allowed_domains: list[str],
                         exclude_patterns: list[str] | None = None
                         ) -> tuple[list[ElementCandidate], list[dict], list[dict], list[dict], list[str]]:
    """Returns (candidates, occluded, unclassified, disabled, validation_signals).

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

    validation_signals: normalized strings describing any validation-error
    indicator currently visible on the page (aria-invalid fields, non-empty
    role="alert" regions, native :user-invalid constraint failures) --
    see _DISCOVER_JS's own comment for why :user-invalid and not plain
    :invalid. Fed into state_fingerprint() by the caller so a rejected-
    submission state fingerprints differently from its pre-submit state,
    instead of reading as an indistinguishable revisit.

    Runs across every frame in the page (`page.frames`), not just the
    main one (Aug 2026) -- a same- or cross-origin <iframe>'s own
    content was previously completely invisible: it's a genuinely
    separate document, so the top-level document.querySelectorAll this
    used to run can't see into it at all, and neither can a plain
    page.locator() at replay time (verified live against a real
    cross-origin iframe before building this: `frame.evaluate()`
    reaches in via CDP regardless of origin, but page.locator() itself
    finds nothing). Only the MARKUP-based candidate types
    (candidates/selects/radios/checkboxes/roleControls) are gathered
    from non-main frames -- the CDP-based div-as-button pool
    (_verify_pool, below) stays main-frame-only for this pass:
    extending its own DOMDebugger session per-frame is a materially
    bigger, riskier change than this one covers. Each candidate found
    outside the main frame is tagged with `frameUrl` (see
    build_locator's own frame-resolution logic) so it can be relocated
    on replay; main-frame elements are untagged, identical to before
    this existed.
    """
    formal: list[tuple[dict, str, bool]] = []
    pool: list[dict] = []
    legacy_unclassified_raw: list[dict] = []
    validation_signals: list[str] = []
    for frame in page.frames:
        try:
            payload = frame.evaluate(_DISCOVER_JS)
        except Exception:
            continue  # a torn-down or not-yet-loaded frame -- skip it, not fatal to the rest
        is_main = frame == page.main_frame
        frame_url = "" if is_main else frame.url
        for key in ("candidates", "selects", "radios", "checkboxes", "roleControls"):
            for el in payload.get(key, []):
                if frame_url:
                    el["frameUrl"] = frame_url
                formal.append((el, "markup", False))
        # Gathered from every frame, not just main -- an error banner or
        # invalid field inside an iframe (a payment widget, say) is a
        # real validation-error state too.
        for sig in payload.get("validationSignals", []):
            validation_signals.append(f"{frame_url}:{sig}" if frame_url else sig)
        if is_main:
            pool = payload["pool"]
            legacy_unclassified_raw = payload.get("legacyUnclassified", [])

    unclassified: list[dict] = []
    promoted: list[tuple[dict, str, bool]] = []
    disabled_raw: list[dict] = []
    if pool:
        try:
            verified = _verify_pool(page, pool)
        except Exception:
            unclassified = _aggregate_unclassified(legacy_unclassified_raw)
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
    return candidates, occluded, unclassified, disabled, validation_signals


def build_locator(page, el_meta: dict):
    scope = page
    frame_url = el_meta.get("frameUrl")
    if frame_url:
        # Discovered inside an <iframe> (see discover_candidates' own
        # per-frame pass, Aug 2026) -- resolve the SAME frame by its
        # own URL rather than walking a chain of iframe DOM elements
        # down to it: page.frame(url=...) finds a frame anywhere in
        # the tree (any nesting depth) directly, verified live before
        # relying on it against a real cross-origin iframe. Falls back
        # to `page` itself if the frame isn't present on replay (not
        # loaded yet, or the app genuinely changed) -- every selector
        # below will then simply find nothing, a clear "not found"
        # rather than a silent wrong-frame match.
        scope = page.frame(url=frame_url) or page
    if el_meta.get("dataTest"):
        return scope.locator(f'[data-test="{el_meta["dataTest"]}"]').first
    if el_meta.get("id"):
        return scope.locator(f'#{el_meta["id"]}').first
    if el_meta.get("tag") == "radio":
        # Structural (type+name+value), not text: radio labels are often
        # short and generic ("Yes", "Small") -- more likely to collide
        # elsewhere on the page than a select's own dataTest/id would be,
        # so this is tried before falling all the way to text.
        return scope.locator(
            f'input[type="radio"][name="{el_meta["radioGroup"]}"][value="{el_meta["radioValue"]}"]'
        ).first
    if el_meta.get("tag") == "checkbox":
        return scope.locator(
            f'input[type="checkbox"][name="{el_meta["checkboxName"]}"][value="{el_meta["checkboxValue"]}"]'
        ).first
    if el_meta.get("tag") in ("role-checkbox", "role-radio"):
        # No data-test/id (handled above already) -- fall back to
        # Playwright's own accessible-name resolution via get_by_role(),
        # the same mechanism that actually located this element
        # correctly on a real site (a Radix/shadcn Checkbox with no
        # data-test/id at all, and no formal aria-labelledby/for wiring
        # to its own sibling <label> either -- verified live: get_by_role
        # found it anyway, and a subsequent .click() correctly toggled
        # its aria-checked/data-state, which raw CSS never could have).
        role = "checkbox" if el_meta["tag"] == "role-checkbox" else "radio"
        name = el_meta.get("roleAccessibleName") or el_meta.get("text") or ""
        return scope.get_by_role(role, name=name, exact=True).first
    if el_meta.get("href"):
        return scope.locator(f'{el_meta["tag"]}[href="{el_meta["href"]}"]').first
    return scope.get_by_text(el_meta["text"], exact=True).first


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


_CLOSEST_FORM_LIKE_JS = r"""(el) => {
    // Same fallback as _DISCOVER_JS's own closestFormLike() -- kept as
    // a separate copy here (not shared code) because this runs in a
    // fresh evaluate_handle() call at action time, not inside the one
    // big discovery script -- see this function's own call sites for
    // why a literal <form> ancestor can't be assumed to exist.
    const real = el.closest('form');
    if (real) return real;
    let n = el.parentElement;
    while (n && n !== document.body) {
        if (n.querySelector('input, select, textarea') && n.querySelectorAll('*').length <= 200) {
            return n;
        }
        n = n.parentElement;
    }
    return null;
}"""


def _closest_form_like_handle(page, el_meta: dict):
    """Resolves el_meta's own container -- a real <form> ancestor if
    one exists, else the closest ancestor that actually contains a real
    input/select/textarea (see _CLOSEST_FORM_LIKE_JS's own comment).
    Returns None if the target can't be located at all, or has no such
    container (a bare button with no nearby fields, correctly nothing
    to fill)."""
    target = build_locator(page, el_meta)
    try:
        target_handle = target.element_handle(timeout=2000)
    except Exception:
        return None
    if target_handle is None:
        return None
    try:
        container = target_handle.evaluate_handle(_CLOSEST_FORM_LIKE_JS)
        return container.as_element()
    except Exception:
        return None


def fill_enclosing_form(page, el_meta: dict, credentials: dict) -> dict | None:
    """Best-effort: fill every visible input/select/textarea in the form
    (or form-LIKE container -- see _closest_form_like_handle) that
    contains the target element, using config credentials where the
    field name matches, else a synthetic safe value. Mirrors the spec's
    'boundary/valid-value' idea in its simplest form for M0.

    Returns None if `el_meta` isn't actually a submit control (so the
    caller knows this wasn't a form submission at all -- e.g. a "Cancel"
    button with type="button" inside a REAL form shouldn't be
    mislabeled as submitting it), otherwise a {field_name: value_used}
    summary (values masked for password fields) so the report can say
    what was filled, which matters most for login steps.
    """
    if not el_meta.get("inForm") or el_meta.get("type") in (
            "select-option", "radio-choice", "checkbox-toggle",
            "role-radio-choice", "role-checkbox-toggle"):
        return None
    container = _closest_form_like_handle(page, el_meta)
    if container is None:
        return {}
    if el_meta.get("type") == "button":
        # type="button" is ambiguous on its own -- inside a REAL <form>
        # it's very likely a genuine Cancel/decorative control (the
        # form's own submit would ordinarily be type="submit", or a
        # bare <button> which defaults to submit). But when there's no
        # real <form> at all -- closestFormLike's own fallback to a
        # plain container -- type="button" is often the ONLY way a
        # modern JS app marks its actual submit action, precisely to
        # suppress native submission and handle it in JS instead.
        # Found live: a formless React-style login (a <div> wrapping
        # two inputs and a type="button" submit) was clicked with both
        # fields still empty, every time, because this guard treated
        # every type="button" as "not a submission" unconditionally.
        # Only exclude it when a genuine <form> exists to make "Cancel"
        # a meaningful alternative to begin with.
        try:
            is_real_form = container.evaluate("e => e.tagName.toLowerCase() === 'form'")
        except Exception:
            is_real_form = False
        if is_real_form:
            return None
    try:
        fields = container.query_selector_all("input, select, textarea")
    except Exception:
        return {}
    summary: dict[str, str] = {}
    for field in fields:
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
        container = _closest_form_like_handle(page, el_meta)
        if container is None:
            return {}
        result = container.evaluate("""(formEl) => {
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


def _capture_nav_status(page):
    """Registers a response listener that records the main document's
    own HTTP status (not a sub-resource's -- an image/CSS/XHR loading
    alongside the real navigation isn't what this is asking about),
    last-one-wins if a redirect chain fires more than one (the final
    destination's status is what matters, not an intermediate 30x).
    Returns (holder_dict, remove_fn) -- read holder["status"] after
    whatever action might have triggered a navigation, then always call
    remove_fn() so the listener doesn't keep firing for later, unrelated
    actions on the same page.

    Purely a side channel: never raises, never affects control flow --
    a click's own failure (element not actionable, timeout) still
    propagates exactly as it did before this existed. Added directly
    from a live question: a same-domain link landing on a 404 was
    previously indistinguishable, in every way, from a normal page."""
    holder = {"status": None}

    def _on_response(resp):
        try:
            if resp.request.resource_type == "document" and resp.frame == page.main_frame:
                holder["status"] = resp.status
        except Exception:
            pass  # a torn-down frame/request mid-navigation -- not worth this any attention

    page.on("response", _on_response)
    return holder, lambda: page.remove_listener("response", _on_response)


def _capture_dialog(page):
    """Registers a dialog listener for the duration of one action --
    catches alert()/confirm()/prompt()/beforeunload triggered by the
    click itself. Playwright's own documented default with NO listener
    at all is to silently auto-dismiss every dialog -- found live that
    this makes a "Delete" button gated behind `confirm("Are you
    sure?")` LOOK like an ordinary, inert click: the confirm always
    resolves to Cancel, nothing on the page changes, and the crawler
    records a completely unremarkable "revisit", with zero signal that
    a real, consequential dialog even existed.

    ACCEPTS every dialog instead of the previous silent-dismiss
    default -- deliberately, not a softer default: risk classification
    and `allow_mutating` already decided, before this click ever
    happened, whether this specific action was safe/acceptable to
    perform at all (see risk.py). A confirm() the app shows immediately
    afterward is procedurally part of THAT SAME action, not a separate
    decision point -- auto-rejecting it would silently prevent an
    already-opted-into mutating action from ever actually completing,
    the opposite of what allow_mutating=true is FOR. `dialog.accept()`
    on a prompt() uses whatever default text the dialog itself offers
    (Playwright's own behavior), not a synthesized value.

    Returns (holder_dict, remove_fn) -- read holder["messages"] (a
    list of "type: message" strings, ordinarily zero or one) after the
    action, then always call remove_fn()."""
    holder = {"messages": []}

    def _on_dialog(dialog):
        try:
            holder["messages"].append(f"{dialog.type}: {dialog.message}")
        except Exception:
            pass
        try:
            dialog.accept()
        except Exception:
            pass  # already handled by another listener, or the page tore down mid-dialog

    page.on("dialog", _on_dialog)
    return holder, lambda: page.remove_listener("dialog", _on_dialog)


def _capture_new_page(page):
    """Registers a listener for a new page/tab opening as a direct
    result of one action (a target="_blank" link, window.open(), ...)
    -- previously completely invisible: the crawler always keeps
    looking at the SAME page object it started the replay with, so a
    spawned tab's own URL never changes anything the crawler can see.
    The click gets recorded as an ordinary "revisit" (identical
    fingerprint, nothing about THIS page changed) -- indistinguishable
    from a genuinely inert click, even though a whole new page just
    opened.

    Does NOT follow the new tab -- a much bigger architectural change
    (this crawler explores exactly one page per replay path); this
    only reports that one appeared and where it pointed, so the report
    at least states the fact instead of staying silent. The spawned
    page closes on its own when the browser context itself does, at
    the end of this replay -- no separate cleanup needed here.

    Returns (holder_dict, remove_fn) -- read holder["url"] after the
    action, then always call remove_fn()."""
    holder = {"url": None}

    def _on_page(new_page):
        if holder["url"] is not None:
            return  # first one wins -- extra popups from the same click aren't this signal's job
        try:
            new_page.wait_for_load_state("load", timeout=3000)
        except Exception:
            pass
        try:
            holder["url"] = new_page.url
        except Exception:
            pass

    page.context.on("page", _on_page)
    return holder, lambda: page.context.remove_listener("page", _on_page)


def perform_action(page, el_meta: dict, credentials: dict, timeout_ms: int = 8000
                    ) -> tuple[dict | None, dict, int | None, str, str | None]:
    """Returns (fill_summary, choice_state, response_status,
    dialog_message, opened_new_page).
    fill_summary is None if this action wasn't a form submission -- see
    fill_enclosing_form. choice_state (see _read_choice_state) is always
    a dict, empty if there was nothing to observe; it's for the label
    only and never feeds Transition.form_fields. response_status is the
    main-document HTTP status of whatever navigation this action
    actually caused, or None if it didn't cause one (see
    _capture_nav_status). dialog_message is "" if no alert()/confirm()/
    prompt()/beforeunload fired, else each one's "type: message" joined
    by "; " (see _capture_dialog -- every dialog is accepted, not
    silently dismissed). opened_new_page is the URL of a new tab/page
    this action spawned (a target="_blank" link, window.open()), or
    None if it didn't (see _capture_new_page -- observed, not followed).

    timeout_ms (Aug 2026): was a hard-coded 8000 in four places here
    until a real crawl of a live production site (alternateqa.com, not
    a local fixture) showed it wasn't always enough -- a slower/heavier
    real deploy can genuinely take longer than any local test site to
    settle. Now threaded from crawler.py's own
    limits.get("action_timeout_ms", 8000), so a config can raise it for
    a known-slow site without patching code; 8000 stays the default,
    identical to every crawl run before this existed."""
    loc = build_locator(page, el_meta)
    # Bounded occlusion recheck (Aug 2026), same grace period for both
    # branches below -- see _wait_until_unoccluded's own docstring for
    # why this exists and why it's capped well under timeout_ms.
    occlusion_grace_ms = min(2000, timeout_ms)
    if el_meta.get("tag") == "select":
        # build_locator resolves the <select> itself (via its own
        # dataTest/id) -- select_option targets the value recorded at
        # discovery time, not whatever happens to be selected on replay.
        blocker = _wait_until_unoccluded(page, loc, max_wait_ms=occlusion_grace_ms)
        if blocker:
            raise RuntimeError(
                f"element became covered by {blocker!r} between discovery and this replay "
                f"(waited {occlusion_grace_ms}ms for it to clear) -- not attempted"
            )
        holder, remove = _capture_nav_status(page)
        dlg_holder, dlg_remove = _capture_dialog(page)
        page_holder, page_remove = _capture_new_page(page)
        try:
            _click_with_occlusion_retry(
                lambda t: loc.select_option(value=el_meta["selectValue"], timeout=t), timeout_ms)
            try:
                page.wait_for_load_state("load", timeout=timeout_ms)
            except Exception:
                pass
            _settle(page)
        finally:
            remove()
            dlg_remove()
            page_remove()
        return None, {}, holder["status"], "; ".join(dlg_holder["messages"]), page_holder["url"]
    fill_summary = fill_enclosing_form(page, el_meta, credentials)
    choice_state = _read_choice_state(page, el_meta)
    blocker = _wait_until_unoccluded(page, loc, max_wait_ms=occlusion_grace_ms)
    if blocker:
        raise RuntimeError(
            f"element became covered by {blocker!r} between discovery and this replay "
            f"(waited {occlusion_grace_ms}ms for it to clear) -- not attempted"
        )
    holder, remove = _capture_nav_status(page)
    dlg_holder, dlg_remove = _capture_dialog(page)
    page_holder, page_remove = _capture_new_page(page)
    try:
        _click_with_occlusion_retry(lambda t: loc.click(timeout=t), timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass
        _settle(page)
    finally:
        remove()
        dlg_remove()
        page_remove()
    return fill_summary, choice_state, holder["status"], "; ".join(dlg_holder["messages"]), page_holder["url"]


_OCCLUSION_CHECK_JS = """(el) => {
    // Same deepElementFromPoint reasoning as _DISCOVER_JS's own copy
    // (a separate copy here, not shared code -- this runs as its own
    // standalone evaluate() at replay time): plain
    // document.elementFromPoint() stops at an open shadow root's HOST,
    // not the actual element rendered inside it -- verified live,
    // found to make every shadow-DOM element register as permanently
    // "occluded by its own host" without this.
    function deepElementFromPoint(x, y) {
        let n = document.elementFromPoint(x, y);
        while (n && n.shadowRoot) {
            const inner = n.shadowRoot.elementFromPoint(x, y);
            if (!inner || inner === n) break;
            n = inner;
        }
        return n;
    }
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth;
    const vh = window.innerHeight || document.documentElement.clientHeight;
    const inViewport = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    if (!inViewport) return {occluded: false, blocker: ''};
    const left = Math.max(r.left, 0), right = Math.min(r.right, vw);
    const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, vh);
    const cx = (left + right) / 2, cy = (top + bottom) / 2;
    const atPoint = deepElementFromPoint(cx, cy);
    const occluded = !atPoint || !(el.contains(atPoint) || atPoint.contains(el));
    return {occluded, blocker: (occluded && atPoint) ? (atPoint.className || atPoint.tagName || '').toString().slice(0, 60) : ''};
}"""


def _wait_until_unoccluded(page, loc, max_wait_ms: int = 2000, poll_interval_ms: int = 150) -> str:
    """Bounded recheck of the exact same occlusion test _DISCOVER_JS
    already applies at discovery time (see actions.py's _DISCOVER_JS),
    run again right before a click/select_option -- catches a modal,
    toast, or onboarding overlay that appeared AFTER discovery but
    before this replay reaches it (see ROADMAP.md "Discovery->click
    occlusion desync", found live on a real production crawl: an
    element correctly not occluded at discovery, then genuinely
    covered by something else by the time a much-later replay step
    finally got to it -- previously indistinguishable from any other
    slow-to-load element, burning the FULL click timeout every time
    before failing with an opaque Playwright stack trace).

    Returns "" if the element is clear to click (immediately, or after
    a transient overlay cleared within the grace period) -- including
    when the locator can't be resolved to exactly one attached element
    at all, or is currently off-screen: neither is this function's call
    to make, so it steps aside and lets click()/select_option() raise
    their own, more specific error instead of guessing here. Returns a
    short description of whatever is still on top (its class or tag)
    if the SAME point is still covered after the whole grace period --
    the caller treats that as a fast, clearly-explained failure instead
    of attempting a click already known to be doomed.

    `max_wait_ms` is deliberately much shorter than the overall action
    timeout (2000ms here vs. 8000ms+ for the click itself) -- long
    enough to give a genuinely transient overlay (a toast, a brief
    animation) a real chance to clear, short enough that a PERSISTENT
    one (an onboarding modal that never goes away this session) fails
    fast instead of wasting the whole budget on a doomed retry loop."""
    try:
        handle = loc.element_handle(timeout=500)
    except Exception:
        return ""
    if handle is None:
        return ""
    attempts = max(1, max_wait_ms // poll_interval_ms)
    blocker = ""
    for i in range(attempts):
        try:
            # handle.evaluate(...), NOT page.evaluate(..., handle) --
            # the callback's own document.elementFromPoint() has to run
            # in the SAME document the element actually lives in.
            # page.evaluate() always runs in the main frame regardless
            # of which frame a passed-in handle belongs to -- harmless
            # for an ordinary main-frame element, but silently wrong
            # for one discovered inside an <iframe> (see
            # discover_candidates' own per-frame pass): "document" in
            # the callback would mean the WRONG document entirely.
            # ElementHandle.evaluate() runs in the handle's own frame,
            # correctly, regardless of where it came from.
            result = handle.evaluate(_OCCLUSION_CHECK_JS)
        except Exception:
            return ""  # element detached mid-check or similar -- not this function's call either
        if not result.get("occluded"):
            return ""
        blocker = result.get("blocker") or "an unknown element"
        if i < attempts - 1:
            page.wait_for_timeout(poll_interval_ms)
    return blocker


def _click_with_occlusion_retry(action_fn, timeout_ms: int) -> None:
    """Calls `action_fn(this_timeout)` (a `loc.click(timeout=...)` or
    `loc.select_option(timeout=...)` closure), retrying when -- and
    only when -- Playwright's own failure explicitly says something
    else is intercepting pointer events at the target (its exact
    wording for this scenario, checked directly, not guessed at).
    Total time spent across every attempt never exceeds `timeout_ms`,
    the same overall budget a single un-retried call would have used:
    a genuinely slow (not occluded) page still gets its full timeout
    in one shot the moment any attempt fails for a DIFFERENT reason.

    Exists because `_wait_until_unoccluded`'s pre-click snapshot,
    above, isn't enough on its own -- confirmed live, re-crawling
    alternateqa.com a second time with that fix already in place: the
    exact same failure recurred, unchanged, because the covering
    element (a button inside what reads like an onboarding/API-key
    prompt) appeared as a direct RESULT of the click's own
    scroll-into-view step, not before it even started. A snapshot
    taken beforehand cannot see that; only re-running the whole
    action -- which redoes Playwright's own scroll + actionability
    checks from scratch -- can.

    First attempt is a short probe (capped at 1500ms) specifically so
    a PERSISTENT occlusion (an onboarding modal that never goes away
    this session) is detected quickly rather than only after an
    entire full-length attempt already burned most of the budget. If
    that specific failure is occlusion-shaped, further short probes
    keep retrying (giving a transient overlay -- a toast, a brief
    animation -- real chances to clear) until the budget runs out, at
    which point the last attempt's own TimeoutError (Playwright's own
    rich diagnosis, naming what's actually on top) propagates
    unchanged -- no custom message invented here to replace it."""
    probe_ms = min(1500, timeout_ms)
    remaining = timeout_ms - probe_ms
    try:
        action_fn(probe_ms)
        return
    except PlaywrightTimeoutError as first_exc:
        if "intercepts pointer events" not in str(first_exc):
            # Not occlusion -- one final attempt with the rest of the
            # original budget, same total time a single un-retried
            # call would have spent, so a merely-slow element isn't
            # shortchanged by the shorter first probe above.
            if remaining <= 0:
                raise
            action_fn(remaining)
            return
        while remaining > 0:
            this_timeout = min(probe_ms, remaining)
            remaining -= this_timeout
            try:
                action_fn(this_timeout)
                return
            except PlaywrightTimeoutError as exc:
                if "intercepts pointer events" not in str(exc) or remaining <= 0:
                    raise


def current_domain(url: str) -> str:
    return urlsplit(url).netloc
