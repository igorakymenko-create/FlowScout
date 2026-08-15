""""Detect fields": read-only, one-off visit to a site's start page to
find the REAL name/id/placeholder of its form inputs, so an operator
filling in Credentials doesn't have to guess a site's field-naming
convention (or open browser dev tools themselves) to know what a
config key needs to literally contain to match -- see
actions.py's _synth_value, a case-insensitive substring match against
these exact attributes.

Deliberately narrow: never fills, never submits, never explores beyond
the start page plus (at most) one click on an obvious login-style
trigger if the start page itself has no password field yet -- most
sites' login form isn't on the landing page, it's one click away
("Log In" in the header), and without this the single most common case
(saucedemo aside) would come back empty. Not a crawl: no risk
classification, no DFS, no state graph -- just "what inputs exist here
right now".
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_FIELD_SCAN_JS = r"""
() => {
    const SKIP_TYPES = new Set(['submit', 'button', 'hidden', 'checkbox', 'radio', 'image', 'reset', 'file']);
    const out = [];
    for (const el of document.querySelectorAll('input, textarea, select')) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (r.width <= 0 || r.height <= 0 || style.visibility === 'hidden'
            || style.display === 'none' || el.disabled) continue;
        const rawType = el.getAttribute('type');
        // filterType (for SKIP_TYPES below) synthesizes a stand-in for
        // <textarea>/<select>, which have no real `type` attribute at
        // all -- but that stand-in must never reach the reported field
        // below. Found live on google.com's own search box (a real
        // <textarea name="q">): echoing the tag name back as a fake
        // `type` produced a redundant, confusing "textarea · textarea"
        // in the UI's own label, which just concatenates tag + type.
        const filterType = (rawType
            || (el.tagName === 'TEXTAREA' ? 'textarea' : el.tagName === 'SELECT' ? 'select' : 'text')).toLowerCase();
        if (SKIP_TYPES.has(filterType)) continue;
        out.push({
            tag: el.tagName.toLowerCase(),
            // Only a genuine `type` attribute value is reported. <input>
            // with no explicit type genuinely defaults to text (browser
            // behavior, not a guess) -- <textarea>/<select> get '' since
            // they have no such attribute to report at all.
            type: rawType ? rawType.toLowerCase() : (el.tagName === 'INPUT' ? 'text' : ''),
            name: el.getAttribute('name') || '',
            id: el.id || '',
            placeholder: el.getAttribute('placeholder') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            inForm: !!el.closest('form'),
        });
    }
    return out;
}
"""

# Two signals, tried in order -- found necessary on a real trilingual
# site (Site B), not assumed: its default locale (root URL redirects
# to /ua) shows a login link whose visible text is "Вхід", which no
# English keyword list will ever match. Its href is "/ua/login" --
# developer-facing URLs stay in English even when the whole UI doesn't,
# so an href pattern is the language-agnostic signal and goes first;
# the English text list is a fallback for JS-driven triggers with no
# real href at all (a button that opens a login modal, say), and only
# helps on English-language sites -- a real, acknowledged gap for
# anything else when href doesn't carry it either.
_LOGIN_HREF_RE = re.compile(r"log-?in|sign-?in|log-?on", re.I)
_LOGIN_TRIGGER_RE = re.compile(r"log ?in|sign ?in|log ?on", re.I)

_FIND_TRIGGER_JS = r"""
({hrefPattern, textPattern}) => {
    const isVisible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

    const hrefRe = new RegExp(hrefPattern, 'i');
    for (const el of document.querySelectorAll('a[href]')) {
        if (!isVisible(el)) continue;
        const href = el.getAttribute('href') || '';
        if (!hrefRe.test(href)) continue;
        const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
        return {text: text.slice(0, 60), href: href};
    }

    const textRe = new RegExp(textPattern, 'i');
    for (const el of document.querySelectorAll('a, button, [role="button"]')) {
        if (!isVisible(el)) continue;
        const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
        if (!text || !textRe.test(text)) continue;
        return {text: text.slice(0, 60), href: el.getAttribute('href') || ''};
    }
    return null;
}
"""


def _dedupe(fields: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for f in fields:
        key = (f["tag"], f["type"], f["name"], f["id"], f["placeholder"])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def detect_fields(start_url: str, timeout_ms: int = 20000) -> dict:
    """Returns {"fields": [...], "clicked_trigger": str | None,
    "error": str | None}. Never raises for ordinary site failures (bad
    URL, timeout, no login found) -- reports the reason instead, same
    "degrade, don't crash" convention as embeddings/semantic dedup."""
    from playwright.sync_api import sync_playwright

    start_domain = urlsplit(start_url).netloc
    result: dict = {"fields": [], "clicked_trigger": None, "error": None}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context().new_page()
            try:
                page.goto(start_url, wait_until="load", timeout=timeout_ms)
                page.wait_for_timeout(400)
                fields = page.evaluate(_FIELD_SCAN_JS)

                if not any(f["type"] == "password" for f in fields):
                    trigger = page.evaluate(_FIND_TRIGGER_JS, {
                        "hrefPattern": _LOGIN_HREF_RE.pattern, "textPattern": _LOGIN_TRIGGER_RE.pattern,
                    })
                    if trigger:
                        # Skip anything that looks like it navigates off-site
                        # (an OAuth "Sign in with X" link, say) -- clicking
                        # would leave the domain this operator is testing.
                        href = trigger["href"]
                        off_domain = href and urlsplit(href).netloc and urlsplit(href).netloc != start_domain
                        if not off_domain:
                            try:
                                # Prefer the href-based locator when known --
                                # text-based click matching a *localized*
                                # label is one more thing that could fail to
                                # resolve uniquely; the exact href always
                                # identifies the same element unambiguously.
                                if href:
                                    page.locator(f'a[href="{href}"]').first.click(timeout=5000)
                                else:
                                    page.get_by_text(trigger["text"], exact=False).first.click(timeout=5000)
                                page.wait_for_timeout(800)
                                fields = fields + page.evaluate(_FIELD_SCAN_JS)
                                result["clicked_trigger"] = trigger["text"] or href
                            except Exception:
                                pass  # the initial-page fields are still returned

                result["fields"] = _dedupe(fields)
            finally:
                browser.close()
    except Exception as exc:
        result["error"] = str(exc)[:300]
    return result
