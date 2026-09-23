"""DFS crawler with reset+replay backtracking.

Design notes (why it's built this way, not the naive way):

* Backtracking is done by *replaying the recorded action path from a
  fresh browser context* -- not browser-back, and not just re-navigating
  an existing page. Browser history is unreliable once forms/SPAs are
  involved; and reusing one context across branches lets storage state
  (cart contents, tokens, anything in localStorage/cookies) leak between
  unrelated flows, which silently corrupts fingerprints and produces
  false "the app is non-deterministic" symptoms. A brand-new context per
  path execution is slower but genuinely deterministic and auditable --
  every flow is something we can replay in isolation and get the same
  answer.
* The DFS is explicit (a stack of frames), not recursive, so a frame
  can be re-entered after a full reset without fighting Python's call
  stack / Playwright's page lifecycle.
* Every candidate is risk-classified *before* it is ever clicked
  (see risk.py). Destructive candidates are never followed; mutating
  ones are followed only if the config opts in.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlsplit

from .actions import discover_candidates, perform_action, current_domain, describe_action, _wait_for_render
from .combinatorics import generate_pairwise_combinations
from .fingerprint import normalize_url, state_fingerprint
from .models import (
    Checkpoint, ElementCandidate, Flow, FlowStatus, Risk, RunResult, StateNode, Transition,
)
from .risk import _MUTATING_KEYWORDS
from .semantic_dedup import DEFAULT_THRESHOLD, apply_semantic_dedup


def _excursion_eligible(candidate: ElementCandidate) -> bool:
    """Whether a candidate is worth trying while OFF allowed_domains
    (see StateNode.external_domain) -- deliberately narrow, since
    excursion mode exists specifically to avoid wandering into a third
    party's own marketing/nav pages instead of completing the actual
    integration (a payment, an OAuth/SSO login) and returning. Eligible
    if either:
    - it's inside a real <form> (`inForm` in the same el_meta
      fill_enclosing_form already reads) -- checkout/login forms are
      almost universally form-based, and
    - its label matches the SAME _MUTATING_KEYWORDS list risk.py
      already uses to recognize a progression-like action (pay,
      confirm, continue, submit...) -- reused as-is rather than
      inventing a second, parallel keyword list.
    Anything else (a header logo, a footer link, "About us") is
    withheld -- not DESTRUCTIVE (the domain itself was already approved
    by risk.classify()), just out of scope for what this excursion is
    for, recorded in skipped_candidates like any other withheld action."""
    try:
        el_meta = json.loads(candidate.selector)
    except Exception:
        el_meta = {}
    # A plain <a> is NEVER eligible, via either check below -- found
    # live, not assumed, on two separate false positives in the same
    # fixture: (1) closestFormLike's own loose fallback (actions.py,
    # "closest ancestor CONTAINING a real input/select/textarea", capped
    # at 200 descendants) marked a sibling link on the same simple page
    # as an unrelated <form> as inForm too, since both were direct
    # children of <body> and <body> itself "contains" the form; (2) a
    # link reading "Fake-Pay Home" matched the _MUTATING_KEYWORDS
    # substring "pay" via the third party's own BRAND NAME, not an
    # actual pay/submit action -- a real risk for any payment/identity
    # provider whose name itself contains a keyword (PayPal, GPay,
    # Razorpay...). A nav/footer/homepage link is virtually always an
    # <a>; a real progression control (submit a payment, click
    # "Continue"/"Authorize") is virtually always a button or input
    # (native or ARIA), regardless of styling framework -- so this
    # excludes every <a> outright rather than trying to patch either
    # signal into being precise enough for THIS specific decision.
    if el_meta.get("tag") == "a":
        return False
    if el_meta.get("inForm"):
        return True
    label = (candidate.label or "").lower()
    return any(kw in label for kw in _MUTATING_KEYWORDS)


def _local_tag(tag: str) -> str:
    """Strips a namespace prefix off an XML tag ('{http://...}loc' ->
    'loc') -- sitemap.xml is supposed to declare the sitemaps.org
    namespace, but real-world sitemaps generating tools sometimes omit
    it. Matching on the local name only handles both without needing
    two separate code paths."""
    return tag.rsplit("}", 1)[-1]


def _fetch_sitemap_urls(sitemap_url: str, _depth: int = 0, _max_depth: int = 2) -> list[str]:
    """Fetches and parses a sitemap.xml: either a plain <urlset> of
    <url><loc> page entries, or a <sitemapindex> of <sitemap><loc>
    entries each pointing at a CHILD sitemap (the standard way a large
    site splits its sitemap into per-section files) -- recursed up to
    `_max_depth` levels. Stdlib-only (urllib.request + xml.etree),
    matching this project's existing convention (see embeddings.py's
    own urllib.request usage) rather than adding a new HTTP dependency
    for this one feature. Raises on a genuinely unreachable/malformed
    sitemap -- the caller (_resolve_seed_urls) is the one that decides
    that's non-fatal to the crawl as a whole and records why."""
    with urllib.request.urlopen(sitemap_url, timeout=10) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    locs = [el.text.strip() for el in root.iter() if _local_tag(el.tag) == "loc" and el.text]
    if _local_tag(root.tag) == "sitemapindex" and _depth < _max_depth:
        urls: list[str] = []
        for child_sitemap in locs:
            try:
                urls.extend(_fetch_sitemap_urls(child_sitemap, _depth + 1, _max_depth))
            except Exception:
                continue  # one broken child sitemap shouldn't sink the whole resolution
        return urls
    return locs


def _resolve_seed_urls(config: dict, allowed_domains: list[str], run: RunResult) -> list[str]:
    """Direct-URL seeding (Sep 2026): the crawler otherwise only ever
    finds what it can DFS its way to by clicking from start_url -- a
    page with no inbound link anywhere in the crawled UI (a deep-linked
    SPA route, an old promo landing page still live but delisted from
    navigation) is structurally unreachable no matter how thoroughly
    the rest of the app is explored. `config["seed_urls"]` (an explicit
    list) and/or `config["sitemap_url"]` (fetched and parsed
    automatically) name known URLs to treat as ADDITIONAL entry points,
    each explored with the crawler's full normal DFS machinery from
    there onward -- not just visited and left alone.

    Filtered the same way ordinary candidate hrefs already are (see
    risk.classify): dropped if its domain isn't in `allowed_domains`
    (an external sitemap entry is exactly as out-of-scope as an
    external link would be) or its path matches an exclude_pattern.
    Also dropped if it normalizes to the same state as start_url
    itself -- nothing new to seed there. Capped at
    `config.get("max_seed_urls", 50)`, applied AFTER filtering, since a
    real sitemap can easily list thousands of URLs and this crawler's
    per-seed exploration cost is the same as crawling an entire extra
    site; the operator raises the cap deliberately, not by accident.

    A sitemap fetch/parse failure is recorded as a checkpoint (not
    raised) -- seeding is additive to an otherwise-normal crawl, so one
    unreachable/malformed sitemap shouldn't take down the whole run,
    the same "degrade, don't abort" principle skipped_candidates and
    every other soft-failure path in this project already follows."""
    urls: list[str] = list(config.get("seed_urls", []))
    sitemap_url = config.get("sitemap_url")
    if sitemap_url:
        try:
            urls.extend(_fetch_sitemap_urls(sitemap_url))
        except Exception as exc:
            run.checkpoints.append(Checkpoint(
                kind="blocked", flow_id=None, state_fp=None,
                message="Could not fetch/parse sitemap_url -- direct-URL seeding skipped for it",
                detail=str(exc)[:500],
            ))

    exclude_patterns = config.get("exclude_patterns", [])
    start_domain = current_domain(config["start_url"])
    start_norm = normalize_url(config["start_url"])
    max_seed_urls = config.get("max_seed_urls", 50)

    filtered: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        if normalize_url(u) == start_norm:
            continue
        domain = current_domain(u)
        if domain and domain not in allowed_domains and domain != start_domain:
            continue
        path = urlsplit(u).path or u
        if any(fnmatch(path, p) for p in exclude_patterns):
            continue
        filtered.append(u)
        if len(filtered) >= max_seed_urls:
            break
    return filtered


@dataclass
class _Frame:
    fp: str
    path: list[Transition] = field(default_factory=list)
    order: list[int] = field(default_factory=list)   # indices into node.candidates, priority-capped
    pos: int = 0
    any_followed: bool = False
    any_risk_skipped: bool = False
    any_repeat_skipped: bool = False
    any_excursion_capped: bool = False
    # How many CONSECUTIVE steps this path has just taken outside
    # allowed_domains (see StateNode.external_domain) -- 0 for an
    # ordinary in-app frame, reset to 0 the moment a step lands back in
    # allowed_domains. Capped against config["excursion_max_depth"] in
    # _run_dfs's own main loop, independently of the ordinary max_depth
    # budget: a real integration excursion (payment, OAuth/SSO) is
    # typically 2-5 screens, not worth the same budget as the app itself.
    excursion_depth: int = 0


def _discover_state(page, allowed_domains, run: RunResult
                     ) -> tuple[str, str, str, str, list[ElementCandidate], list[dict], list[dict], list[str], list[str], str]:
    raw_url = page.url
    url_pattern = normalize_url(page.url)
    title = page.title()
    domain = current_domain(page.url)
    exclude_patterns = run.config.get("exclude_patterns", [])
    excursion_domains = run.config.get("excursion_domains", [])
    # "" for an ordinary in-app state; the domain itself when this state
    # was reached OUTSIDE allowed_domains -- only possible at all when
    # that domain is also in excursion_domains (an unapproved external
    # domain is DESTRUCTIVE in risk.classify() and never gets clicked in
    # the first place, so discovery never runs against it). See
    # StateNode.external_domain's own docstring for what this is used for.
    external_domain = domain if domain not in allowed_domains else ""
    candidates, occluded, unclassified, disabled, validation_signals, captcha_signals = discover_candidates(
        page, domain, allowed_domains, exclude_patterns, excursion_domains)
    if not candidates and not occluded and not unclassified:
        # Found literally nothing at all -- could be a genuinely empty
        # page, or a modern SPA whose own async data-fetch-then-render
        # cycle (see actions._wait_for_render's own docstring) simply
        # hasn't finished yet: verified live on a real production app
        # (OrangeHRM, not a fixture) that showed 0 candidates 200ms
        # after its dashboard's own `load` event, and 34 real ones
        # ~3 seconds later. Retried ONCE, after a bounded network-idle
        # wait, rather than waiting unconditionally on every single
        # discovery regardless of need -- tried that first and reverted
        # it after measuring the real cost live: saucedemo's own
        # ordinary background traffic alone takes ~2.7s to naturally
        # quiet down, and unconditionally paying that on every action
        # made an ALREADY-rendered, already-correct page get re-sampled
        # at a different point in ITS OWN progressive loading (a
        # dynamic-catalog/spinner/lazy-load page, exactly the kind
        # saucedemo already tests) -- multiplying one real state into
        # several spurious ones with different candidate counts each
        # time, confirmed live (inventory.html alone showed up 3 times
        # with 27/29/32 candidates in one run). Confining the extra
        # wait to "found NOTHING at all" means a page with ANY
        # candidates already visible -- including one still mid-way
        # through loading more -- is accepted exactly as before this
        # existed, and only a genuinely blank-so-far page pays this
        # cost, once.
        _wait_for_render(page)
        candidates, occluded, unclassified, disabled, validation_signals, captcha_signals = discover_candidates(
            page, domain, allowed_domains, exclude_patterns, excursion_domains)
    # validation_signals folded into the fingerprint itself (not just
    # reported afterward, like dialog_message/response_status are) --
    # see actions.py's discover_candidates docstring and Transition.
    # validation_errors for why: without this, a rejected-submission
    # state is byte-for-byte identical (by url+candidate-set) to its own
    # pre-submit state, so the crawler read it as a plain revisit and
    # never explored past it. captcha_signals is deliberately NOT folded
    # in here -- see Transition.captcha_detected's own docstring.
    fp = state_fingerprint(url_pattern, [c.signature for c in candidates] + validation_signals)
    for o in occluded:
        run.skipped_candidates.append({
            "state_fp": fp, "label": o["label"], "reason": o["reason"], "risk": "n/a",
        })
    return fp, url_pattern, raw_url, title, candidates, unclassified, disabled, validation_signals, captcha_signals, external_domain


_CLOCK_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$", re.I)
_CLOCK_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}


def _parse_clock_duration(value):
    """Accepts a plain number (milliseconds, passed straight to
    Playwright's own clock.fast_forward()), a friendly "<N><unit>"
    string (s/m/h/d, e.g. "30d", "90s") converted to milliseconds here
    since Playwright's own fast_forward() has no such shorthand, or
    Playwright's own native "HH:MM:SS"/"MM:SS"/"SS" string form,
    returned unmodified for Playwright itself to parse."""
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    m = _CLOCK_DURATION_RE.match(text)
    if m:
        amount, unit = m.groups()
        return int(float(amount) * _CLOCK_UNIT_MS[unit.lower()])
    return text


def _run_path(browser, config, path: list[Transition], run: RunResult, credentials: dict):
    """Execute `path` from a fresh, isolated browser context (fresh
    cookies/localStorage -- no leakage between DFS branches). Returns
    (fp, url_pattern, title, candidates, last_fill_summary, unclassified,
    disabled, last_choice_state, last_response_status,
    last_dialog_message, last_opened_new_page, validation_errors,
    captcha_signals, external_domain) for the state reached after the
    last step, or None if some step failed (an error checkpoint is
    recorded, pointing at which step).
    `validation_errors`/`captcha_signals`/`external_domain` describe the
    state reached after the LAST step (see _discover_state/
    Transition.validation_errors/captcha_detected/
    StateNode.external_domain) -- unlike the other `last_*` fields
    below, they describe the state itself, not the
    action that produced it, so they aren't specific to "the final step
    of `path`" in the same way; they're simply whatever _discover_state()
    found there. `last_fill_summary`
    is whatever perform_action returned for the *final* step of `path`
    -- None if that step wasn't a form submission, else the field:value
    summary used to build a readable "Fill form and submit ... (...)"
    label (e.g. which login account was used). `last_choice_state` is
    the radio/checkbox state observed alongside it (see
    actions._read_choice_state) -- label-only, kept separate so it never
    ends up in Transition.form_fields (M4 codegen would try to `.fill()`
    a checkbox otherwise). `last_response_status` is the main-document
    HTTP status of whatever navigation the final step actually caused
    (see actions._capture_nav_status) -- None if it didn't cause one.
    `last_dialog_message`/`last_opened_new_page` are the same
    observational pass-through of actions.perform_action's own dialog/
    new-page capture (see its own docstring) for the final step.
    `credentials` is passed in rather than read from `config` directly
    so each persona's own pass (see crawl()) can supply its own -- the
    only thing that actually differs between two personas walking the
    same replay path.

    `config.get("storage_state")` (Sep 2026), when set, seeds every
    fresh context with an already-authenticated session (cookies/
    localStorage) -- either a path to a Playwright storage-state JSON
    file (`context.storage_state(path=...)` after a manual login) or
    the state dict inline; Playwright's own `new_context()` accepts
    either form natively, so it's passed straight through unexamined.
    Found directly from a live question: seed_urls (see crawl()'s own
    seeding loop) visits each URL from a brand-new, unauthenticated
    context -- on an auth-walled app, every single seed just redirects
    to the login page, collapsing what should have been N distinct
    destinations into one indistinguishable "reached the login page"
    flow. credentials alone can't fix this: filling and submitting a
    login form is itself an ACTION this crawler only ever performs by
    exploring to it, not something a bare direct-nav step does on its
    own -- storage_state sidesteps the whole problem by starting every
    context already logged in, root discovery included, so ordinary
    DFS naturally reaches an authenticated app's real content and every
    seed_urls entry lands on its actual destination instead of a login
    redirect. Deliberately NOT "find whichever discovered candidate's
    label contains the word 'login' and replay it before each seed" --
    that's a guess about which candidate is the right one AND an
    English-text-dependent heuristic, the same class of fragility this
    project already rejected once for field_detect.py's own login-
    trigger matching. Loading a bad storage_state (a stale/expired
    session, a typo'd path) fails exactly like any other per-step
    error -- a Checkpoint, this call returns None -- rather than
    crashing the whole crawl or silently falling back to an
    unauthenticated context with no signal that happened at all."""
    storage_state = config.get("storage_state")
    try:
        context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
    except Exception as exc:
        run.checkpoints.append(Checkpoint(
            kind="error", flow_id=None, state_fp=path[0].from_fp if path else None,
            message="Could not create a browser context with the configured storage_state",
            detail=str(exc)[:1200],
        ))
        return None
    page = context.new_page()
    # config["mock_clock"] (Sep 2026): two INDEPENDENT Playwright Clock
    # API mechanisms, not one unified "just skip forward" behavior --
    # verified live, not assumed, that they don't substitute for each
    # other. "start_at" (-> clock.install(time=...), before the page
    # ever loads) answers a Date.now()/new Date() comparison against an
    # absolute deadline ("available starting <date>"), checked
    # synchronously at load -- confirmed live that fast_forward() alone,
    # called AFTER that same load, does nothing for it, since the check
    # already ran against the ORIGINAL time. "fast_forward" (applied
    # once, right after the page's own initial load below) answers a
    # setTimeout/setInterval-scheduled cooldown ("resend code in 00:30")
    # -- confirmed live that setting "start_at" alone, with no
    # fast_forward, does nothing for it either, since a timer scheduled
    # at load always waits its own full duration from that moment,
    # regardless of what date it thinks it is. Real sites can use
    # either pattern (or both); this doesn't guess which, it exposes
    # both documented primitives and lets the operator pick. Neither
    # touches server-side time-gating (a real timestamp checked in the
    # app's own backend) -- genuinely unreachable from outside the
    # browser, not something any client-side mechanism can address.
    mock_clock = config.get("mock_clock")
    if mock_clock:
        try:
            start_at = mock_clock.get("start_at")
            page.clock.install(time=start_at) if start_at else page.clock.install()
        except Exception as exc:
            run.checkpoints.append(Checkpoint(
                kind="error", flow_id=None, state_fp=path[0].from_fp if path else None,
                message="Could not install the configured mock_clock",
                detail=str(exc)[:1200],
            ))
            return None
    last_fill_summary = None
    last_choice_state: dict = {}
    last_response_status: int | None = None
    last_dialog_message = ""
    last_opened_new_page: str | None = None
    # Not a required config key -- existing configs/*.json written before
    # this existed don't have it, same reasoning as max_action_repeat
    # above. Found necessary on a real production site (alternateqa.com,
    # not a local fixture): the previous hard-coded 8000ms wasn't always
    # enough, and there was no way to raise it short of patching code.
    action_timeout_ms = config.get("limits", {}).get("action_timeout_ms", 8000)
    try:
        try:
            page.goto(config["start_url"], wait_until="load")
        except Exception as exc:
            # Found live, not assumed: this specific page.goto() -- unlike
            # every step inside the loop below -- ran completely outside
            # any try/except until now, so a transient failure here (a
            # slow/flaky network, a public demo server briefly
            # overloaded) crashed the whole crawl() call with an
            # uncaught exception instead of degrading to a checkpoint,
            # every other navigation failure in this codebase's own
            # documented behavior. Every _run_path() call re-visits
            # start_url first (see this function's own docstring), so
            # this one line runs on every single replay, not just root
            # discovery -- the exposure was universal, not an edge case.
            run.checkpoints.append(Checkpoint(
                kind="error", flow_id=None, state_fp=path[0].from_fp if path else None,
                message="Could not load start_url for this replay",
                detail=str(exc)[:1200],
            ))
            return None
        page.wait_for_timeout(200)
        if mock_clock and mock_clock.get("fast_forward"):
            try:
                page.clock.fast_forward(_parse_clock_duration(mock_clock["fast_forward"]))
            except Exception as exc:
                run.checkpoints.append(Checkpoint(
                    kind="error", flow_id=None, state_fp=path[0].from_fp if path else None,
                    message="Could not fast-forward the configured mock_clock",
                    detail=str(exc)[:1200],
                ))
                return None
        for i, t in enumerate(path):
            el_meta = json.loads(t.replay_meta)
            try:
                fill_summary, choice_state, response_status, dialog_message, opened_new_page = perform_action(
                    page, el_meta, credentials, timeout_ms=action_timeout_ms)
                if i == len(path) - 1:
                    last_fill_summary = fill_summary
                    last_choice_state = choice_state
                    last_response_status = response_status
                    last_dialog_message = dialog_message
                    last_opened_new_page = opened_new_page
            except Exception as exc:
                where = "final step" if i == len(path) - 1 else f"replay step {i + 1}/{len(path)}"
                run.checkpoints.append(Checkpoint(
                    kind="error", flow_id=None, state_fp=t.from_fp,
                    message=f"Action '{t.action_label}' raised an error ({where})",
                    detail=str(exc)[:1200],
                ))
                return None
        try:
            (fp, url_pattern, raw_url, title, candidates, unclassified, disabled, validation_signals,
             captcha_signals, external_domain) = _discover_state(page, config["allowed_domains"], run)
        except Exception as exc:
            # Same "was outside any try/except until now" gap as the
            # initial page.goto() above, for the SAME reason: nothing
            # here is tied to one specific Transition (this runs once
            # per replay, after every step already succeeded), so a
            # discovery-time failure (e.g. a CDP session dying mid-
            # crawl) previously crashed the whole crawl uncaught instead
            # of degrading to a checkpoint like everything else.
            run.checkpoints.append(Checkpoint(
                kind="error", flow_id=None, state_fp=path[-1].to_fp if path else None,
                message="Could not discover the state reached after this replay",
                detail=str(exc)[:1200],
            ))
            return None
        validation_errors = "; ".join(validation_signals)
        captcha_detected = "; ".join(captcha_signals)
        return (fp, url_pattern, raw_url, title, candidates, last_fill_summary, unclassified, disabled,
                last_choice_state, last_response_status, last_dialog_message, last_opened_new_page,
                validation_errors, captcha_detected, external_domain)
    finally:
        context.close()


def _ubiquitous_nav_signatures(node: StateNode, run: RunResult) -> set[str]:
    """Signatures that look like global chrome (a header/footer nav
    repeated identically on every page) rather than this page's own
    content -- see ROADMAP.md's "Candidate priority still starves
    page-unique content behind repeated header nav" entry (Aug 2026):
    a site's header (logo + nav links + language switchers) appears on
    every state and, being earlier in the DOM, fills the whole
    max_breadth_per_state budget before a page's own primary element
    ever gets a turn.

    Recomputed fresh from `run.states` on every call (cheap relative to
    a page load -- states/candidates per run are small) rather than
    maintained as an incremental counter, so it stays correct for every
    caller (crawl's own DFS, resume_flow, explore_combination, a
    handoff step) without each one remembering to update a separate
    running tally.

    Needs a real sample before it says anything: fewer than 3 OTHER
    already-discovered states (this node itself excluded -- otherwise
    every one of its own signatures trivially "appears on a state",
    itself) returns empty, so the first few pages of any crawl are
    never penalized for looking similar by coincidence. A signature
    counts as ubiquitous once it's present on at least
    max(3, 80% of those other states) -- a high bar on purpose: this
    is a structural signal ("this exact same nav item shows up nearly
    everywhere"), not a guess about *which* labels look like navigation
    (a language-dependent heuristic this project has avoided
    elsewhere, e.g. field_detect.py's login-trigger matching)."""
    others = [st for st in run.states.values() if st.fingerprint != node.fingerprint]
    if len(others) < 3:
        return set()
    counts: Counter = Counter()
    for st in others:
        for sig in {c.norm_signature for c in st.candidates}:
            counts[sig] += 1
    threshold = max(3, round(0.8 * len(others)))
    return {sig for sig, n in counts.items() if n >= threshold}


def _order_for(node: StateNode, max_breadth: int, run: RunResult, revisit_history: set[str],
                excursion_max_breadth: int | None = None) -> list[int]:
    """`excursion_max_breadth` (Sep 2026): when `node.external_domain`
    is set (this state was reached on an operator-approved third-party
    integration domain -- see StateNode.external_domain), candidates
    are first narrowed to `_excursion_eligible()` ones (form-contained,
    or matching the same progression-keyword list risk.py's own
    MUTATING classification uses) before anything else runs, and
    `max_breadth` itself is overridden by this, much smaller, cap --
    defaulting effectively to 1 so the walk follows a SINGLE path
    through the third party's flow instead of branching into their own
    marketing/nav pages. An ordinary in-app state (`external_domain ==
    ""`) is completely unaffected -- this parameter is only ever
    consulted for the off-domain case.

    `revisit_history`: norm_signatures already confirmed, earlier in
    THIS persona's own pass, to lead to a state already in run.states --
    see crawl()'s own comment at the revisit branch for how it's built.
    A stable sort moves known-revisit signatures to the back before
    breadth truncation runs, so a forced cut preferentially drops actions
    already confirmed to lead nowhere new, not ones that might. Learned
    live, not guessed from labels: measured on a real saucedemo run
    (before this existed) that the actual revisit-producing signatures
    are dominated by ordinary mutating actions converging on a shared end
    state -- add-to-cart, checkout, remove, cancel -- not UI-chrome
    toggles a label heuristic (open/close, expand/collapse) would catch;
    that heuristic was considered and dropped for exactly this reason,
    on top of already being a language-dependent guess (the same class
    of bug field_detect.py's login-trigger matching hit earlier this
    project, fixed by matching structure instead of English text).

    `is_choice` candidates are exempt from this deprioritization no
    matter what revisit_history says -- caught live, not assumed safe:
    picking a `<select>` option changes display order, not the candidate
    *set*, so state_fingerprint() (deliberately) doesn't change and
    EVERY select/radio/checkbox choice reads as a "revisit" the very
    first time any one of them is tried anywhere in the run, regardless
    of which option. Without this exemption, that flags the whole choice
    group as a revisit-producer almost immediately and buries it under
    max_breadth_per_state everywhere else -- confirmed on saucedemo's own
    sort dropdown (TC-10 in the M2 gap-analysis calibration, a real,
    valued capability): found via breadth=20 but silently missing at the
    realistic default breadth=10, the exact regression the is_choice
    mechanism exists to prevent elsewhere (gap_analysis.py, shared_steps.
    py, testcase_draft.py all already special-case it for the same
    reason -- this file just hadn't caught up yet).

    Global-nav deprioritization (Sep 2026, see
    _ubiquitous_nav_signatures's own docstring): a SECONDARY sort key,
    weaker than a confirmed revisit_history dead end -- a signature
    that's both a confirmed dead end AND ubiquitous still sorts to the
    very back, but page-unique candidates are tried before merely-
    ubiquitous ones even when neither has been confirmed a dead end
    yet. Also exempt for is_choice candidates, same reasoning as
    revisit_history above: a choice control's own options are never
    global chrome."""
    idxs = list(range(len(node.candidates)))
    effective_breadth = max_breadth
    if node.external_domain and excursion_max_breadth is not None:
        eligible = [i for i in idxs if _excursion_eligible(node.candidates[i])]
        for i in idxs:
            if i not in eligible:
                c = node.candidates[i]
                run.skipped_candidates.append({
                    "state_fp": node.fingerprint, "label": describe_action(json.loads(c.selector), None),
                    "reason": f"outside excursion scope (not a form field/submit or a progression-"
                              f"like action on {node.external_domain})",
                    "risk": c.risk.value,
                })
        idxs = eligible
        effective_breadth = excursion_max_breadth
    nav_signatures = _ubiquitous_nav_signatures(node, run)
    idxs.sort(key=lambda i: (
        not node.candidates[i].is_choice and node.candidates[i].norm_signature in revisit_history,
        not node.candidates[i].is_choice and node.candidates[i].norm_signature in nav_signatures,
    ))
    if len(idxs) > effective_breadth:
        overflow = idxs[effective_breadth:]
        for i in overflow:
            c = node.candidates[i]
            run.skipped_candidates.append({
                "state_fp": node.fingerprint, "label": describe_action(json.loads(c.selector), None),
                "reason": "breadth limit exceeded", "risk": c.risk.value,
            })
        idxs = idxs[:effective_breadth]
    return idxs


def _run_dfs(browser, config: dict, run: RunResult, credentials: dict, persona_name: str,
             stack: list[_Frame], next_flow_id: list[int],
             max_depth: int, max_breadth: int, max_states: int, max_flows: int,
             max_action_repeat: int, allow_mutating: bool,
             states_before: int, flows_before: int, show_persona_suffix: bool,
             seq_to_flow_id: dict[tuple, int], revisit_history: set[str],
             origin_note: str = "", stop_when=None) -> "str | None":
    """The DFS loop itself -- extracted (Aug 2026) so crawl()'s own
    per-persona pass and resume_flow()'s targeted continuation from a
    single already-BLOCKED flow can share it instead of duplicating the
    loop. Mutates `run` in place (states/flows/checkpoints/
    skipped_candidates); `stack` is drained in place, LIFO, exactly like
    before this was split out -- this function doesn't care whether it
    was seeded with one fresh root frame or one frame resuming a
    specific blocked path, the algorithm is identical either way, only
    the starting point differs.

    `states_before`/`flows_before`: baseline the max_states/max_flows
    budgets count *up from* -- computed fresh at call time by the
    caller, so a resumed continuation gets its own full budget starting
    at 0, not the remainder of whatever the original pass had already
    spent.

    `origin_note`: stamped onto every Flow this call emits (see
    Flow.origin_note's own docstring) -- empty for crawl()'s own normal
    pass, a short note identifying which operator action produced these
    flows for resume_flow()/explore_combination()'s calls.

    `stop_when` (Sep 2026, multi-actor handoff scenarios --
    run_handoff_scenario()'s own "find" step): an optional
    `StateNode -> bool` predicate, checked once for every genuinely NEW
    state right after it's discovered and added to `run.states`. The
    first state it accepts stops the whole DFS immediately (a normal
    flow is still emitted reaching it, and a checkpoint records that
    the search stopped early, naming how much was left unexplored --
    same "state what happened, don't just look identical to a normal
    finish" discipline `max_flows` truncation already established) and
    its fingerprint is returned. Returns `None` if the stack drains (or
    a budget is hit) without `stop_when` ever accepting anything --
    this is itself a real, reportable finding for a handoff's "find"
    step (the target persona genuinely can't reach a state matching
    the target within the given search budget), not a tool failure.
    `None` (the default) preserves this function's exact original
    behavior for every existing caller (crawl()/resume_flow()/
    explore_combination(), none of which pass it or read a return
    value) -- this parameter is purely additive."""
    def emit_flow(path: list[Transition], end_fp: str, forced_status: FlowStatus | None = None,
                  extra_reason: str = "", resumable: bool = False) -> Flow:
        seq = tuple(t.action_norm_signature for t in path)
        status = forced_status
        dup_of = None
        reason = extra_reason
        if status is None:
            if seq in seq_to_flow_id:
                status = FlowStatus.DUPLICATE
                dup_of = seq_to_flow_id[seq]
                reason = "Same normalized action sequence as flow #%d (structural dedup: same steps, different data)" % dup_of
            else:
                status = FlowStatus.UNIQUE
                seq_to_flow_id[seq] = next_flow_id[0]
                reason = extra_reason or "New normalized action sequence"
        flow = Flow(
            id=next_flow_id[0], status=status, duplicate_of=dup_of, dedup_reason=reason,
            transitions=list(path), end_state_fp=end_fp, persona=persona_name,
            # Only meaningful (and only ever True) for a forced BLOCKED
            # status -- a flow that completed normally or deduped has
            # nothing to "resume". See Flow.resumable's own docstring
            # for which BLOCKED reasons qualify and why.
            resumable=resumable and status == FlowStatus.BLOCKED,
            origin_note=origin_note,
        )
        next_flow_id[0] += 1
        run.flows.append(flow)
        return flow

    # Deliberately much smaller than the ordinary max_depth/max_breadth
    # budgets above -- see risk.classify's own docstring and
    # _order_for's excursion-mode filtering. A real integration
    # (payment, OAuth/SSO) is typically 2-5 screens; walking it with the
    # SAME budget as the app itself risks wandering into the third
    # party's own marketing/nav pages instead of completing it.
    excursion_max_depth = config.get("excursion_max_depth", 4)
    excursion_max_breadth = config.get("excursion_max_breadth", 1)

    while stack:
        if len(run.flows) - flows_before >= max_flows:
            # Silent truncation, until Aug 2026: this used to just
            # `break`, abandoning every remaining stack frame with no
            # record anywhere -- no blocked flow, no skipped
            # candidate, no checkpoint. A run that stopped here looked
            # byte-for-byte like a run that finished naturally.
            # Found while measuring depth budgets: raising max_depth
            # past 14 changed nothing on saucedemo, because max_flows
            # (not depth) had silently become the binding constraint
            # and nothing said so. Same fix as max_depth truncation
            # above -- a checkpoint (not a flow, since the abandoned
            # frames aren't paths anyone walked) naming exactly how
            # much was left unexplored.
            unexplored_states = len(stack)
            unexplored_actions = sum(len(f.order) - f.pos for f in stack)
            run.checkpoints.append(Checkpoint(
                kind="blocked", flow_id=None, state_fp=None,
                message=f"Crawl stopped early: max_flows limit ({max_flows}) reached"
                        + (f" for persona '{persona_name}'" if show_persona_suffix else ""),
                detail=f"{unexplored_states} state(s) were still queued for exploration, with "
                       f"{unexplored_actions} candidate action(s) never tried. Raise max_flows to "
                       f"continue past this point -- the flows reported here are a prefix of what "
                       f"this config would eventually find, not the complete picture.",
            ))
            break
        frame = stack[-1]
        node = run.states[frame.fp]

        if frame.pos >= len(frame.order) or len(frame.path) >= max_depth or frame.excursion_depth >= excursion_max_depth:
            # Two genuinely different situations used to collapse into
            # one -- "ran out of things to try" (frame.pos exhausted,
            # a real dead end) and "there were more candidates, but
            # max_depth was hit before trying them" (budget, not a
            # dead end) produced the exact same flow status and the
            # exact same generic "New normalized action sequence"
            # reason, with nothing anywhere recording which one
            # happened. A user had no way to tell "this flow is
            # complete" from "this flow was cut short and might have
            # continued" -- found by a user asking exactly that
            # question about a real run. depth_truncated distinguishes
            # them the same way max_states truncation already does
            # below: forced BLOCKED status, an explicit "Truncated"
            # reason, and the untried candidates recorded in
            # skipped_candidates so the report's Safety register shows
            # precisely what was never even attempted, not just that
            # something was.
            depth_truncated = len(frame.path) >= max_depth and frame.pos < len(frame.order)
            # Same distinction, for the SEPARATE excursion-depth budget
            # (see StateNode.external_domain/_order_for's own docstring)
            # -- checked only once depth_truncated already didn't fire,
            # since a frame can't be truncated for two different reasons
            # in the same visit.
            excursion_capped = (not depth_truncated and frame.excursion_depth >= excursion_max_depth
                                 and frame.pos < len(frame.order))
            if frame.path:
                if depth_truncated:
                    remaining = frame.order[frame.pos:]
                    for i in remaining:
                        c = node.candidates[i]
                        run.skipped_candidates.append({
                            "state_fp": frame.fp,
                            "label": describe_action(json.loads(c.selector), None),
                            "reason": "max_depth limit reached", "risk": c.risk.value,
                        })
                    emit_flow(frame.path, frame.fp, forced_status=FlowStatus.BLOCKED,
                              extra_reason=f"Truncated: max_depth limit reached with "
                                           f"{len(remaining)} further action(s) available from here, "
                                           f"never tried",
                              resumable=True)
                elif excursion_capped:
                    remaining = frame.order[frame.pos:]
                    for i in remaining:
                        c = node.candidates[i]
                        run.skipped_candidates.append({
                            "state_fp": frame.fp,
                            "label": describe_action(json.loads(c.selector), None),
                            "reason": f"excursion depth limit reached (excursion_max_depth={excursion_max_depth})",
                            "risk": c.risk.value,
                        })
                    emit_flow(frame.path, frame.fp, forced_status=FlowStatus.BLOCKED,
                              extra_reason=f"Truncated: excursion depth limit reached "
                                           f"({frame.excursion_depth} consecutive step(s) outside "
                                           f"allowed_domains) with {len(remaining)} further action(s) "
                                           f"available from here, never tried",
                              resumable=True)
                elif not frame.any_followed and (frame.any_risk_skipped or frame.any_repeat_skipped
                                                  or frame.any_excursion_capped):
                    # Same "name what actually happened" discipline as
                    # depth/max_flows truncation above -- a dead end
                    # reached only because policy withheld every
                    # remaining action reads identically to a genuine
                    # dead end unless the reason is spelled out, and
                    # the withholding reasons are independent enough
                    # that a frame can hit more than one at once.
                    withheld_by = []
                    if frame.any_risk_skipped:
                        withheld_by.append("risk policy (destructive, or mutating with allow_mutating=false)")
                    if frame.any_repeat_skipped:
                        withheld_by.append(f"the action-repeat cap (max_action_repeat={max_action_repeat})")
                    if frame.any_excursion_capped:
                        withheld_by.append("excursion scope (see Safety register for which domain/candidates)")
                    emit_flow(frame.path, frame.fp, forced_status=FlowStatus.BLOCKED,
                              extra_reason="Dead end: remaining actions were withheld by "
                                           + " and ".join(withheld_by),
                              resumable=True)
                else:
                    emit_flow(frame.path, frame.fp)
            stack.pop()
            continue

        cand_idx = frame.order[frame.pos]
        frame.pos += 1
        candidate = node.candidates[cand_idx]

        if candidate.risk == Risk.DESTRUCTIVE:
            frame.any_risk_skipped = True
            run.skipped_candidates.append({
                "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                "reason": candidate.risk_reason, "risk": "destructive",
            })
            continue
        if candidate.risk == Risk.MUTATING and not allow_mutating:
            frame.any_risk_skipped = True
            run.skipped_candidates.append({
                "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                "reason": "mutating action withheld (allow_mutating=false)", "risk": "mutating",
            })
            continue

        # Repeat-action cap: how many times has THIS normalized action
        # already been performed earlier in this same path (not
        # per-state -- across the whole walk from root)? Targets the
        # actual combinatorial-growth case directly: repeatedly
        # clicking "add-to-cart-*" on different products all
        # normalize to the same signature (known_prefixes in
        # fingerprint.py), and each one opens a genuinely new state
        # (the cart's own candidate list includes the item), so
        # nothing else already caps this growth at its source --
        # max_depth only bounds it indirectly, by being high enough
        # to *tolerate* the blow-up before reaching anything past
        # it. is_choice actions (select/radio/checkbox) are
        # unaffected in practice: their norm_signature is kept
        # maximally distinct per option specifically so it's never
        # generalized (see fingerprint.py's "choice-" early return),
        # so the same one only ever repeats if a path genuinely
        # revisits the identical option, which this cap correctly
        # still allows twice before withholding.
        repeat_count = sum(1 for t in frame.path if t.action_norm_signature == candidate.norm_signature)
        if repeat_count >= max_action_repeat:
            frame.any_repeat_skipped = True
            run.skipped_candidates.append({
                "state_fp": frame.fp, "label": describe_action(json.loads(candidate.selector), None),
                "reason": f"action-repeat cap reached ({repeat_count}x '{candidate.norm_signature}' "
                          f"already performed earlier in this path)",
                "risk": candidate.risk.value,
            })
            continue

        el_meta = json.loads(candidate.selector)
        trial = Transition(from_fp=frame.fp, to_fp=None, action_label=describe_action(el_meta, None),
                            action_norm_signature=candidate.norm_signature, risk=candidate.risk,
                            risk_reason=candidate.risk_reason, replay_meta=candidate.selector,
                            is_choice=candidate.is_choice,
                            anchor_target_missing=candidate.anchor_target_missing)
        result = _run_path(browser, config, frame.path + [trial], run, credentials)
        frame.any_followed = True

        if result is None:
            # Found while building gap_analysis.py's action-pool
            # broadening (Aug 2026): trial.outcome was never set
            # here at all, silently staying at its dataclass
            # default "ok" -- meaning report.py's own `elif
            # t.outcome == "error":` rendering (a red step-error
            # note with the exception detail) was dead code, and
            # a flow's failed final step rendered identically to
            # a normal successful one. _run_path() always appends
            # exactly one Checkpoint right before returning None
            # (its only return-None path), so this is always the
            # matching detail, not a guess.
            trial.outcome = "error"
            trial.detail = run.checkpoints[-1].detail if run.checkpoints else ""
            emit_flow(frame.path + [trial], end_fp=frame.fp, forced_status=FlowStatus.BLOCKED,
                      extra_reason=f"Terminated: action '{trial.action_label}' raised an error")
            continue

        (new_fp, url_pat, raw_url, title, new_candidates, fill_summary, new_unclassified, new_disabled,
         choice_state, response_status, dialog_message, opened_new_page, validation_errors,
         captcha_detected, external_domain) = result
        # choice_state (radio/checkbox selections observed at submit time) is
        # merged into the label for human/gap-analysis visibility only -- it
        # must never reach trial.form_fields, since M4's codegen turns that
        # into .fill() calls and .fill() raises on a radio/checkbox input.
        label_fields = {**(fill_summary or {}), **(choice_state or {})}
        trial.action_label = describe_action(el_meta, label_fields or None)
        if fill_summary:
            trial.form_fields = list(fill_summary.keys())
        trial.to_fp = new_fp
        trial.outcome = "revisit" if new_fp in run.states else "ok"
        trial.response_status = response_status
        trial.dialog_message = dialog_message
        trial.opened_new_page = opened_new_page
        trial.validation_errors = validation_errors
        trial.captcha_detected = captcha_detected
        new_path = frame.path + [trial]

        if new_fp in run.states:
            # Learned live, for _order_for()'s benefit on every node
            # discovered from here on in this persona's pass: this
            # exact action, taken from this exact state, produced no
            # new information. Recorded by norm_signature (not tied
            # to this one state) because the same signature reaching
            # an already-known state once is real evidence it's
            # likely to again -- confirmed on real data before this
            # existed (add-to-cart/checkout/remove/cancel all showed
            # up as revisit-producers, not just UI-chrome toggles).
            revisit_history.add(candidate.norm_signature)
            if captcha_detected:
                # Found live, not assumed: a DIFFERENT action path can
                # reach the SAME captcha state that some earlier branch
                # already discovered (e.g. a normal click lands on the
                # exact page a seed URL was ALSO seeded at) -- captcha_
                # detected here is _discover_state()'s own fresh re-check
                # for THIS replay, not a cached value, so it's just as
                # reliable as the first-discovery case above. Without
                # this, only the flow that happened to discover the
                # state FIRST got marked blocked; every later one reading
                # the exact same challenge page read as an ordinary,
                # unremarkable "unique"/"duplicate" flow instead.
                trial.outcome = "blocked"
                emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                          extra_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) "
                                       f"-- never explored further")
            else:
                emit_flow(new_path, end_fp=new_fp)
            continue

        if captcha_detected:
            # A genuinely NEW state, but one behind a CAPTCHA/challenge
            # marker (see actions.py's discover_candidates docstring) --
            # recorded (unlike the max_states-truncation branch below,
            # this state IS worth keeping as evidence of exactly where
            # the crawl got challenged) but never explored further: there
            # is nothing legitimate to click on a challenge page, and
            # trying would risk interacting with the CAPTCHA widget
            # itself rather than the app under test. trial.outcome was
            # set to "ok"/"revisit" above -- overwritten here since
            # neither is accurate for what actually happened.
            trial.outcome = "blocked"
            run.states[new_fp] = StateNode(
                fingerprint=new_fp, url_pattern=url_pat, raw_url=raw_url, title=title,
                candidates=new_candidates, discovered_by_flow=next_flow_id[0],
                unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
                captcha_detected=captcha_detected, external_domain=external_domain,
            )
            emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                      extra_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) "
                                   f"-- never explored further")
            continue

        if len(run.states) - states_before >= max_states:
            run.skipped_candidates.append({
                "state_fp": frame.fp, "label": trial.action_label,
                "reason": "max_states limit reached", "risk": candidate.risk.value,
            })
            emit_flow(new_path, end_fp=new_fp, forced_status=FlowStatus.BLOCKED,
                      extra_reason="Truncated: max_states limit reached before this state could be explored")
            continue

        new_node = StateNode(fingerprint=new_fp, url_pattern=url_pat, raw_url=raw_url,
                              title=title, candidates=new_candidates,
                              discovered_by_flow=next_flow_id[0],
                              unclassified_interactive=new_unclassified,
                              disabled_interactive=new_disabled,
                              external_domain=external_domain)
        run.states[new_fp] = new_node
        if stop_when is not None and stop_when(new_node):
            emit_flow(new_path, end_fp=new_fp)
            return new_fp
        child = _Frame(fp=new_fp, path=new_path)
        # Consecutive, not cumulative -- resets to 0 the moment a step
        # lands back in allowed_domains, so returning from a completed
        # integration resumes ordinary exploration with the ordinary
        # budget, exactly as if the excursion never happened.
        child.excursion_depth = frame.excursion_depth + 1 if external_domain else 0
        child.order = _order_for(new_node, max_breadth, run, revisit_history, excursion_max_breadth)
        # _order_for already recorded WHY each ineligible candidate was
        # withheld in skipped_candidates; this only flags it on the
        # frame itself so the dead-end message below (if every
        # candidate got filtered out) names excursion scope instead of
        # falling through to a generic "New normalized action sequence".
        if external_domain and new_candidates and not child.order:
            child.any_excursion_capped = True
        stack.append(child)

    if stop_when is not None:
        # The stack drained (or a budget truncated it) without ever
        # finding a state stop_when accepted -- itself a real,
        # reportable finding for a handoff's "find" step (see this
        # function's own docstring), not silently identical to an
        # ordinary finished crawl.
        run.checkpoints.append(Checkpoint(
            kind="blocked", flow_id=None, state_fp=None,
            message="Search stopped without finding a matching state"
                    + (f" for persona '{persona_name}'" if show_persona_suffix else ""),
            detail=f"Explored {len(run.states) - states_before} new state(s) and "
                   f"{len(run.flows) - flows_before} flow(s) within budget; none matched.",
        ))
    return None


def _apply_payment_sandbox(config: dict) -> dict:
    """Merges `config["payment_sandbox"]` into the two EXISTING
    mechanisms that already do the actual work -- `excursion_domains`
    (approves following a real payment gateway off allowed_domains --
    see risk.classify()'s own docstring) and `credentials`
    (actions.py's `_synth_value()` field-name matching, already used
    for login fields) -- rather than inventing a third, parallel
    concept. Deliberately provider-agnostic: FlowScout has no built-in
    knowledge of Stripe, PayPal, or any other gateway's own test-mode
    conventions (a Stripe test card, a Braintree one, and a plain
    "Visa test card" number are all just field values an operator
    supplies, matched the same field-name-substring way a login
    username/password already is) -- this is a UI/config grouping for
    a specific task, not new matching logic.

    A no-op (returns `config` itself, unchanged) when `payment_sandbox`
    is absent or not enabled -- every existing config keeps working
    exactly as before this existed. Returns a NEW dict when enabled
    (config and its own "personas" list are never mutated in place) --
    callers elsewhere (the web UI's saved-config JSON, an in-memory
    config dict a caller still holds a reference to) are never
    surprised by a mutation they didn't ask for.

    Sandbox fields fill in ONLY behind whatever a persona's/the
    top-level credentials dict already defines -- an operator's own
    explicit login credential always wins a same-named collision
    (unlikely in practice, but an existing config should never be
    silently overridden by a newer, unrelated feature)."""
    sandbox = config.get("payment_sandbox")
    if not sandbox or not sandbox.get("enabled"):
        return config
    domains = sandbox.get("domains") or []
    fields = sandbox.get("fields") or {}
    if not domains:
        raise ValueError("payment_sandbox is enabled but has no domains -- nothing would ever be approved "
                          "to follow off allowed_domains")
    if not fields:
        raise ValueError("payment_sandbox is enabled but has no fields -- nothing would ever be filled "
                          "into the gateway's own form")

    merged_excursion = list(config.get("excursion_domains", []))
    for d in domains:
        if d not in merged_excursion:
            merged_excursion.append(d)

    new_config = {**config, "excursion_domains": merged_excursion}
    if config.get("personas"):
        new_config["personas"] = [
            {**p, "credentials": {**fields, **p.get("credentials", {})}}
            for p in config["personas"]
        ]
    else:
        new_config["credentials"] = {**fields, **config.get("credentials", {})}
    return new_config


def crawl(config: dict) -> RunResult:
    config = _apply_payment_sandbox(config)
    from playwright.sync_api import sync_playwright

    limits = config["limits"]
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    # Not a required key like the four above -- existing configs/*.json
    # written before this existed don't have it, and a plain limits[...]
    # index would KeyError every one of them the moment the CLI loaded
    # the file (unlike the web UI's config.setdefault(), the CLI passes
    # the JSON straight through). See ROADMAP.md "Parked -- smart limits":
    # the direct fix for the actual combinatorial-growth case (an N-item
    # cart is up to 2^N reachable states) -- a third add-to-cart click
    # within one DFS path teaches the crawler nothing a second one didn't
    # already show, so 2 is the default: enough to see "one item" and
    # "two items" behavior, not enough to keep multiplying.
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = config.get("allow_mutating", True)
    allowed_domains = config.get("allowed_domains", [])
    # Same default as _run_dfs's own read of this -- root/seed states are
    # never actually off-domain by construction (start_url and every
    # resolved seed_urls entry are already checked against
    # allowed_domains elsewhere), so this is a no-op safety net here,
    # not something expected to fire; kept for consistency rather than
    # assuming that invariant holds forever.
    excursion_max_breadth = config.get("excursion_max_breadth", 1)

    # Multiple personas (named credential sets) walk the same config
    # sequentially into ONE RunResult -- one report, one change-report,
    # one CI exit code. `credentials` (singular) is still accepted as
    # shorthand for a single persona named "default", so every config
    # written before this existed keeps working unchanged.
    #
    # Sequential, not parallel -- deliberately (see ROADMAP.md "Multi-
    # persona crawling"): personas can corrupt each other's results
    # through shared server-side state (one persona's "Reset App State"
    # mid-crawl would silently invalidate whatever another persona was
    # mid-flow doing at that moment), the same class of cross-run
    # non-determinism M5 already had to document for Site B -- just
    # within a single run instead of across two.
    #
    # max_states/max_flows apply *per persona*, not to the run as a
    # whole -- each persona gets the same budget config would give it as
    # a standalone crawl, rather than later personas silently starving
    # because earlier ones used up a shared cap. States a later persona
    # reaches that an earlier one already discovered (the state graph is
    # shared and reused across personas -- see below) don't count against
    # its budget at all, only genuinely new ones do.
    personas = config.get("personas")
    if not personas:
        personas = [{"name": "default", "credentials": config.get("credentials", {})}]

    run = RunResult(project=config["project"], start_url=config["start_url"], config=config)
    run.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Direct-URL seeding (see _resolve_seed_urls's own docstring) --
    # resolved once up front, before the browser even launches, same as
    # every other config-level decision (limits, personas) above.
    seed_urls = _resolve_seed_urls(config, allowed_domains, run)

    next_flow_id = [1]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        root_fp: str | None = None
        # Discovered once (by the first persona), reused by every later
        # one -- same reasoning as root_fp: a direct-nav transition
        # ignores `credentials` entirely (see actions.py's perform_action,
        # tag == "direct-nav"), so what a seed URL shows depends only on
        # the URL itself, never on which persona is walking, exactly like
        # the empty-path root state above.
        seed_fps: dict[str, tuple[str, str]] = {}  # url -> (fingerprint, captcha_detected)

        for persona in personas:
            persona_name = persona.get("name", "default")
            credentials = persona.get("credentials", {})

            # Fresh per persona, not shared: a structural-dedup match
            # against another persona's identical-looking action sequence
            # is NOT the same flow (see identity.py's flow_identity) --
            # what a persona is *allowed* to do getting there is exactly
            # the thing multi-persona crawling exists to tell apart.
            seq_to_flow_id: dict[tuple, int] = {}

            # Same "fresh per persona" reasoning as seq_to_flow_id above:
            # a norm_signature converging on an already-known state for
            # one persona doesn't mean it will for another -- personas can
            # legitimately reach different states from the same action
            # (that's the whole point of multi-persona crawling). See
            # _order_for()'s own docstring for what this is used for.
            revisit_history: set[str] = set()

            if root_fp is None:
                # Only the very first persona actually visits start_url --
                # an empty path never calls perform_action at all, so this
                # state provably never depends on credentials and is safe
                # (and cheaper) to reuse for every later persona rather
                # than re-discovering it once per persona.
                root = _run_path(browser, config, [], run, credentials)
                if root is None:
                    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    browser.close()
                    return run
                (root_fp, root_url_pat, _, root_title, root_candidates, _, root_unclassified,
                 root_disabled, _, _, _, _, _, root_captcha, root_external_domain) = root
                run.states[root_fp] = StateNode(
                    fingerprint=root_fp, url_pattern=root_url_pat, raw_url=config["start_url"],
                    title=root_title, candidates=root_candidates,
                    unclassified_interactive=root_unclassified, disabled_interactive=root_disabled,
                    captcha_detected=root_captcha, external_domain=root_external_domain,
                )
                if root_captcha:
                    # start_url itself is behind a CAPTCHA/challenge --
                    # there's no Transition/Flow to attach this to (an
                    # empty path never goes through emit_flow, same
                    # reasoning _path_to_state's own docstring gives for
                    # the root state), so this is a checkpoint, not a
                    # blocked flow. Nothing past here is explorable for
                    # ANY persona (root is shared across all of them), so
                    # stop the crawl entirely rather than let every
                    # persona separately "explore" a challenge page's own
                    # incidental candidates.
                    run.checkpoints.append(Checkpoint(
                        kind="blocked", flow_id=None, state_fp=root_fp,
                        message="start_url is itself behind a CAPTCHA/challenge page -- "
                                "crawl stopped, nothing else is explorable",
                        detail=root_captcha,
                    ))
                    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    browser.close()
                    return run

            root_node = run.states[root_fp]
            root_frame = _Frame(fp=root_fp)
            root_frame.order = _order_for(root_node, max_breadth, run, revisit_history, excursion_max_breadth)
            stack: list[_Frame] = [root_frame]

            # One additional root-like frame per seed URL -- each starts
            # its own full DFS exploration from wherever that URL lands,
            # exactly like root_frame does from start_url. Built fresh
            # per persona (a fresh Transition instance each time, not
            # shared/mutated across personas) since to_fp/response_status/
            # etc. get written onto it below and a later persona must not
            # see an earlier persona's own values there.
            for seed_url in seed_urls:
                # "text" is just for a shorter, readable label (the path,
                # not the whole absolute URL) -- perform_action's own
                # direct-nav replay only ever reads "href".
                seed_el_meta = {"tag": "direct-nav", "href": seed_url,
                                 "text": urlsplit(seed_url).path or seed_url}
                seed_trial = Transition(
                    from_fp="", to_fp=None,
                    action_label=describe_action(seed_el_meta, None),
                    action_norm_signature=f"direct-nav:{normalize_url(seed_url)}",
                    risk=Risk.SAFE, risk_reason="operator-specified seed URL",
                    replay_meta=json.dumps(seed_el_meta),
                )
                if seed_url not in seed_fps:
                    seed_result = _run_path(browser, config, [seed_trial], run, credentials)
                    if seed_result is None:
                        # Already recorded as a checkpoint by _run_path
                        # itself (an error, not a warning) -- this seed
                        # URL just isn't explorable, nothing more to do.
                        continue
                    (seed_fp, seed_url_pat, _, seed_title, seed_candidates, _, seed_unclassified,
                     seed_disabled, _, seed_status, seed_dialog, seed_new_page, seed_validation,
                     seed_captcha, seed_external_domain) = seed_result
                    seed_trial.response_status = seed_status
                    seed_trial.dialog_message = seed_dialog
                    seed_trial.opened_new_page = seed_new_page
                    seed_trial.validation_errors = seed_validation
                    if seed_fp not in run.states:
                        run.states[seed_fp] = StateNode(
                            fingerprint=seed_fp, url_pattern=seed_url_pat, raw_url=seed_url,
                            title=seed_title, candidates=seed_candidates,
                            unclassified_interactive=seed_unclassified, disabled_interactive=seed_disabled,
                            captcha_detected=seed_captcha, external_domain=seed_external_domain,
                        )
                    # Cached as (fp, captcha) together, not just fp --
                    # otherwise a LATER persona reusing this seed_url
                    # would skip straight to pushing an ordinary frame
                    # for it, silently losing the captcha finding this
                    # first persona already made.
                    seed_fps[seed_url] = (seed_fp, seed_captcha)

                seed_fp, seed_captcha = seed_fps[seed_url]
                seed_trial.to_fp = seed_fp
                seed_trial.captcha_detected = seed_captcha
                if seed_captcha:
                    # Same reasoning as the main DFS loop's own captcha
                    # branch: a real Flow here (not just a checkpoint),
                    # since a seed URL's own arrival -- unlike root's --
                    # already has a non-empty path/action_label worth
                    # reporting as its own "Blocked" card. Built directly
                    # rather than through _run_dfs's own emit_flow closure
                    # (not in scope here) -- same pattern
                    # explore_combination() already uses for its own
                    # one-off Flow construction outside that closure.
                    seed_trial.outcome = "blocked"
                    run.flows.append(Flow(
                        id=next_flow_id[0], status=FlowStatus.BLOCKED, duplicate_of=None,
                        dedup_reason=f"Blocked by a CAPTCHA/challenge page ({seed_captcha}) "
                                     f"-- never explored further",
                        transitions=[seed_trial], end_state_fp=seed_fp, persona=persona_name,
                    ))
                    next_flow_id[0] += 1
                    continue
                seed_node = run.states[seed_fp]
                seed_frame = _Frame(fp=seed_fp, path=[seed_trial])
                seed_frame.order = _order_for(seed_node, max_breadth, run, revisit_history, excursion_max_breadth)
                stack.append(seed_frame)

            states_before = len(run.states)
            flows_before = len(run.flows)

            _run_dfs(browser, config, run, credentials, persona_name, stack, next_flow_id,
                     max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                     states_before, flows_before, len(personas) > 1,
                     seq_to_flow_id, revisit_history)

        browser.close()

    sem_cfg = config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down a completed crawl
            run.semantic_dedup_status = f"error: {exc}"
    else:
        run.semantic_dedup_status = "skipped: disabled in config"

    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return run


def resume_flow(run: RunResult, flow: Flow, limit_overrides: dict, credentials: dict,
                 run_semantic_dedup: bool = True) -> None:
    """Continue exploring from where one specific BLOCKED flow left off,
    instead of re-crawling the whole config from start_url. Mutates
    `run` in place -- new states/flows/checkpoints/skipped_candidates
    get appended, exactly as if the original DFS stack had kept going
    past this one flow's stopping point instead of stopping there.

    Only meaningful for `flow.resumable` flows -- see its own docstring
    for exactly which BLOCKED reasons qualify (max_depth truncation, or
    a dead end from risk-policy/repeat-cap withholding: both anchored to
    THIS flow's own path). max_states/max_flows truncation isn't tied to
    any one flow, so there's nothing here to resume for those -- the
    honest fix is a full re-crawl with a higher limit.

    `run_semantic_dedup` (Aug 2026): set False by resume_all_blocked_in_run's
    own batch loop -- semantic dedup re-embeds EVERY currently-unique
    flow from scratch on each call (see semantic_dedup.py, no caching
    across calls), so resuming 12 flows in a row with this left True
    would redundantly re-embed an ever-growing flow list up to 12 times
    in a row for one button click, wasting most of the free-tier
    embeddings quota on repeat work before ever reaching real analysis.
    Single-flow resume (the individual "Resume this flow" button) keeps
    the default True -- there's exactly one call there, nothing to
    batch up.

    `limit_overrides`: a partial `limits` dict (e.g. just
    `{"max_depth": 12}`) merged over the run's own original limits, plus
    an optional `"allow_mutating"` key (not nested under `limits` in the
    config schema -- see crawl() -- accepted the same way here, applied
    to a top-level override instead). Reuses the exact replay mechanism
    backtracking already relies on: `flow.transitions` already carries
    each step's `replay_meta`, and `run.states[flow.end_state_fp]` (from
    the original crawl) is reused as-is, not re-discovered -- the same
    "trust what's already known, don't re-earn it" reasoning
    `_run_path()`'s own replay-from-root already runs on for every DFS
    step, just anchored at a later point instead of the root."""
    if not flow.resumable:
        raise ValueError(f"flow #{flow.id} is not resumable (status={flow.status.value}: {flow.dedup_reason})")
    node = run.states.get(flow.end_state_fp)
    if node is None:
        raise ValueError(f"flow #{flow.id}'s end state is missing from this run's state graph")

    from playwright.sync_api import sync_playwright

    limits = {**run.config.get("limits", {}), **{k: v for k, v in limit_overrides.items() if k != "allow_mutating"}}
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = limit_overrides.get("allow_mutating", run.config.get("allow_mutating", True))
    excursion_max_breadth = run.config.get("excursion_max_breadth", 1)

    # Local only -- run.config stays an honest record of how the
    # original crawl was actually configured; a resume's own
    # (potentially different) limits are never written back into it.
    config = {**run.config, "limits": limits, "allow_mutating": allow_mutating}

    next_flow_id = [max((f.id for f in run.flows), default=0) + 1]
    seq_to_flow_id: dict[tuple, int] = {}
    revisit_history: set[str] = set()

    flow_ids_before = {f.id for f in run.flows}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            frame = _Frame(fp=flow.end_state_fp, path=list(flow.transitions))
            # Reconstructed, not assumed 0: the flow being resumed may
            # already have ended mid-excursion (truncated by the
            # ordinary max_depth, not excursion_max_depth, before this
            # feature existed to tell the two apart) -- counted from the
            # trailing run of off-domain states its own transitions
            # actually reached, using each StateNode's own recorded
            # external_domain rather than re-deriving it from scratch.
            for t in reversed(flow.transitions):
                st = run.states.get(t.to_fp)
                if st and st.external_domain:
                    frame.excursion_depth += 1
                else:
                    break
            frame.order = _order_for(node, max_breadth, run, revisit_history, excursion_max_breadth)
            stack: list[_Frame] = [frame]

            states_before = len(run.states)
            flows_before = len(run.flows)

            _run_dfs(browser, config, run, credentials, flow.persona, stack, next_flow_id,
                     max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                     states_before, flows_before, False, seq_to_flow_id, revisit_history,
                     origin_note=f"Resumed from flow #{flow.id}")
        finally:
            browser.close()

    # Reclassify the original blocked flow itself (Aug 2026), not just
    # append new ones alongside it -- asked directly after a live
    # report: a flow that's genuinely been continued past its own
    # truncation point staying marked BLOCKED forever, still counted in
    # "N blocked"/"Resume all" totals, read as if the resume had done
    # nothing even when it demonstrably had (see ROADMAP.md). Reuses
    # the EXISTING duplicate mechanism rather than inventing a new
    # status: a truncated 3-step prefix is genuinely redundant once a
    # fuller 5-step continuation exists, the same relationship
    # `_apply_state_convergence` already expresses for unrelated
    # reasons. `resumable=False` here is what actually drops it out of
    # future "N blocked"/"Resume all" counts (both read live off
    # run.flows, not a separate cached figure) -- only when THIS resume
    # produced at least one real result; a resume that itself
    # immediately re-blocked or errored leaves the original genuinely
    # still blocked, nothing to claim otherwise.
    new_flows = [f for f in run.flows if f.id not in flow_ids_before]
    if new_flows:
        best = max(new_flows, key=lambda f: len(f.transitions))
        ids_str = ", ".join(f"#{f.id}" for f in sorted(new_flows, key=lambda f: f.id))
        flow.status = FlowStatus.DUPLICATE
        flow.duplicate_of = best.id
        flow.resumable = False
        flow.dedup_reason = (
            f"Resumed and superseded: continuing further produced {len(new_flows)} new flow(s) "
            f"({ids_str}) -- this shorter, truncated path is redundant now that the fuller "
            f"exploration exists"
        )

    sem_cfg = run.config.get("semantic_dedup", {})
    if run_semantic_dedup and sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful resume
            run.semantic_dedup_status = f"error on resume: {exc}"


def _path_to_state(run: RunResult, state_fp: str) -> list[Transition]:
    """The sequence of already-walked transitions that reaches
    `state_fp`, derived from StateNode.discovered_by_flow rather than
    guessed or re-derived: the exact flow that first discovered this
    state necessarily has a transition landing on it somewhere along
    its OWN path (_run_dfs keeps extending the same path deeper before
    ever calling emit_flow() for it -- discovery and flow-emission are
    not the same moment), so truncating that flow's transitions at the
    first one whose to_fp matches is exact, not approximate.

    Empty list for the root state -- discovered_by_flow is None only
    for the very first state (see crawl()'s own root-discovery special
    case, which sets up StateNode directly and never goes through
    _run_dfs's normal `discovered_by_flow=next_flow_id[0]` assignment)."""
    node = run.states.get(state_fp)
    if node is None:
        raise ValueError(f"state {state_fp} not found in this run")
    if node.discovered_by_flow is None:
        return []
    flow = next((f for f in run.flows if f.id == node.discovered_by_flow), None)
    if flow is None:
        raise ValueError(f"state {state_fp}'s discovering flow (#{node.discovered_by_flow}) not found")
    for i, t in enumerate(flow.transitions):
        if t.to_fp == state_fp:
            return list(flow.transitions[:i + 1])
    raise ValueError(f"state {state_fp}'s own discovering flow (#{flow.id}) doesn't actually reach it "
                      f"-- this would be a data inconsistency, not a normal error")


def _apply_one_combination(browser, run: RunResult, config: dict, credentials: dict, persona_name: str,
                            path_to_state: list[Transition], state_fp: str, node: "StateNode",
                            candidate_indices: list[int], next_flow_id: list[int],
                            seq_to_flow_id: dict[tuple, int], revisit_history: set[str],
                            max_depth: int, max_breadth: int, max_states: int, max_flows: int,
                            max_action_repeat: int, allow_mutating: bool, excursion_max_breadth: int,
                            combo_note: str, origin_note: str) -> str:
    """The actual body of applying ONE candidate combination and
    continuing DFS from wherever it lands -- extracted from
    explore_combination() (Sep 2026) so explore_combinations_pairwise()
    can call this in a loop, inside ONE shared browser/Playwright
    session and with semantic dedup run ONCE at the very end, instead
    of paying the launch-a-browser-and-call-an-embeddings-API cost once
    per generated combination (a pairwise plan can easily be 10-20+
    combinations -- see combinatorics.py's own numbers -- and this
    project has already hit real embeddings rate limits from far fewer
    calls than that, see ROADMAP.md's Gemini batching entry).

    Returns a short outcome tag ('new' / 'revisit' / 'blocked') for the
    caller's own per-combination reporting -- explore_combination()
    itself doesn't need this (it reports via the run-level delta
    instead) but explore_combinations_pairwise() wants to say which
    combinations actually found something new, not just a final total.
    Every other behavior (safety checks, which state/flow gets
    recorded, when DFS continues) is byte-for-byte identical to what
    explore_combination() did inline before this was extracted."""
    combo_path = list(path_to_state)
    for idx in candidate_indices:
        if idx < 0 or idx >= len(node.candidates):
            raise ValueError(f"candidate index {idx} out of range for state {state_fp}")
        candidate = node.candidates[idx]
        if candidate.risk == Risk.DESTRUCTIVE:
            raise ValueError(f"candidate '{candidate.label}' is destructive -- never walked, "
                              f"in a combination or otherwise")
        if candidate.risk == Risk.MUTATING and not allow_mutating:
            raise ValueError(f"candidate '{candidate.label}' is mutating and allow_mutating is false")
        el_meta = json.loads(candidate.selector)
        trial = Transition(from_fp=state_fp, to_fp=None, action_label=describe_action(el_meta, None),
                            action_norm_signature=candidate.norm_signature, risk=candidate.risk,
                            risk_reason=candidate.risk_reason, replay_meta=candidate.selector,
                            is_choice=candidate.is_choice,
                            anchor_target_missing=candidate.anchor_target_missing)
        combo_path.append(trial)

    result = _run_path(browser, config, combo_path, run, credentials)
    if result is None:
        raise RuntimeError("the combination failed to apply -- see this run's checkpoints for which step and why")
    (new_fp, url_pat, raw_url, title, new_candidates, fill_summary, new_unclassified, new_disabled,
     choice_state, response_status, dialog_message, opened_new_page, validation_errors,
     captcha_detected, external_domain) = result

    last = combo_path[-1]
    label_fields = {**(fill_summary or {}), **(choice_state or {})}
    last.action_label = describe_action(json.loads(last.replay_meta), label_fields or None)
    if fill_summary:
        last.form_fields = list(fill_summary.keys())
    last.to_fp = new_fp
    last.response_status = response_status
    last.dialog_message = dialog_message
    last.opened_new_page = opened_new_page
    last.validation_errors = validation_errors
    last.captcha_detected = captcha_detected

    if new_fp in run.states and not captcha_detected:
        last.outcome = "revisit"
        flow = Flow(
            id=next_flow_id[0], status=FlowStatus.UNIQUE, duplicate_of=None,
            dedup_reason=f"{combo_note} -- reached an already-known state",
            transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
            origin_note=origin_note,
        )
        run.flows.append(flow)
        return "revisit"

    if captcha_detected:
        last.outcome = "blocked"
        if new_fp not in run.states:
            run.states[new_fp] = StateNode(
                fingerprint=new_fp, url_pattern=url_pat, raw_url=raw_url, title=title,
                candidates=new_candidates, discovered_by_flow=next_flow_id[0],
                unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
                captcha_detected=captcha_detected, external_domain=external_domain,
            )
        flow = Flow(
            id=next_flow_id[0], status=FlowStatus.BLOCKED, duplicate_of=None,
            dedup_reason=f"Blocked by a CAPTCHA/challenge page ({captcha_detected}) -- never explored further",
            transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
            origin_note=origin_note,
        )
        run.flows.append(flow)
        next_flow_id[0] += 1
        return "blocked"

    last.outcome = "ok"
    new_node = StateNode(
        fingerprint=new_fp, url_pattern=url_pat, raw_url=raw_url, title=title,
        candidates=new_candidates, discovered_by_flow=next_flow_id[0],
        unclassified_interactive=new_unclassified, disabled_interactive=new_disabled,
        external_domain=external_domain,
    )
    run.states[new_fp] = new_node
    flow = Flow(
        id=next_flow_id[0], status=FlowStatus.UNIQUE, duplicate_of=None,
        dedup_reason=f"{combo_note} -- newly discovered state",
        transitions=combo_path, end_state_fp=new_fp, persona=persona_name,
        origin_note=origin_note,
    )
    run.flows.append(flow)
    next_flow_id[0] += 1

    frame = _Frame(fp=new_fp, path=combo_path)
    for t in reversed(combo_path):
        st = run.states.get(t.to_fp)
        if st and st.external_domain:
            frame.excursion_depth += 1
        else:
            break
    frame.order = _order_for(new_node, max_breadth, run, revisit_history, excursion_max_breadth)
    stack: list[_Frame] = [frame]
    states_before = len(run.states) - 1
    flows_before = len(run.flows) - 1

    _run_dfs(browser, config, run, credentials, persona_name, stack, next_flow_id,
             max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
             states_before, flows_before, False, seq_to_flow_id, revisit_history,
             origin_note=origin_note)
    return "new"


def explore_combination(run: RunResult, state_fp: str, candidate_indices: list[int],
                         limit_overrides: dict, credentials: dict, persona_name: str = "default") -> None:
    """Apply several is_choice candidates from ONE already-known state
    TOGETHER, in a single replay, then keep exploring normally from
    whatever that combination reaches. The direct answer to a real,
    verified limitation (see ROADMAP.md "Known limitation --
    conjunctive multi-parameter gating is invisible to DFS"): the
    crawler can only ever change one such candidate at a time on its
    own, because picking one that doesn't itself alter the visible
    candidate set reads as an ordinary "revisit" and _run_dfs's own
    main loop stops the branch right there (see its comment at
    `if new_fp in run.states:`). A page gated behind several
    parameters set TOGETHER is structurally invisible to autonomous
    exploration -- FlowScout doesn't guess the right combination (that
    would mean inventing an expected result, which this project
    deliberately never does), so this lets a human who already knows
    the right combination hand it over directly instead.

    Deliberately does NOT create a separate StateNode for "1 of N set",
    "2 of N set", etc. -- those intermediate states are exactly the
    ordinary revisits DFS already can't get past on its own (same
    fingerprint as before, no new candidates yet), so recording them
    would be noise, not signal. Only the FINAL combined state -- after
    every selected candidate has been applied, in order -- is
    discovered and recorded: the one piece of information a human
    actually wanted when they built this combination.

    `state_fp` must already be in run.states (from a prior crawl --
    this is deliberately NOT usable "cold" against a site that hasn't
    been crawled at all: `candidate_indices` resolve against that
    state's own already-discovered `node.candidates`, never
    user-authored CSS/locators FlowScout hasn't itself verified).
    `candidate_indices`: positions into `run.states[state_fp].candidates`,
    applied in the given order -- the caller's own choice of which
    control to set first/last is respected, the same way a real person
    filling in a form top-to-bottom would. Mutates `run` in place,
    exactly like resume_flow()."""
    node = run.states.get(state_fp)
    if node is None:
        raise ValueError(f"state {state_fp} not found in this run")
    if not candidate_indices:
        raise ValueError("at least one candidate must be selected")

    path_to_state = _path_to_state(run, state_fp)

    from playwright.sync_api import sync_playwright

    limits = {**run.config.get("limits", {}), **{k: v for k, v in limit_overrides.items() if k != "allow_mutating"}}
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = limit_overrides.get("allow_mutating", run.config.get("allow_mutating", True))
    excursion_max_breadth = run.config.get("excursion_max_breadth", 1)

    # Local only -- run.config stays an honest record of how the
    # original crawl was actually configured, same reasoning as
    # resume_flow()'s own identical line.
    config = {**run.config, "limits": limits, "allow_mutating": allow_mutating}

    next_flow_id = [max((f.id for f in run.flows), default=0) + 1]
    # Fresh, not pre-populated from run.flows -- same convention
    # resume_flow() already uses for its own seq_to_flow_id/
    # revisit_history: a combination's own new branches are deduped
    # against each other, not against the entire pre-existing run.
    seq_to_flow_id: dict[tuple, int] = {}
    revisit_history: set[str] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            _apply_one_combination(
                browser, run, config, credentials, persona_name, path_to_state, state_fp, node,
                candidate_indices, next_flow_id, seq_to_flow_id, revisit_history,
                max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                excursion_max_breadth, combo_note="Set via a user-specified parameter combination",
                origin_note="Continued after testing a parameter combination")
        finally:
            browser.close()

    sem_cfg = run.config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful combination
            run.semantic_dedup_status = f"error on combination: {exc}"


def explore_combinations_pairwise(run: RunResult, state_fp: str, limit_overrides: dict,
                                   credentials: dict, persona_name: str = "default") -> dict:
    """Automated answer to ROADMAP.md's "Known limitation -- conjunctive
    multi-parameter gating is invisible to DFS" entry, one level up from
    explore_combination(): instead of a human hand-picking ONE
    combination at a time, this finds EVERY distinct is_choice group at
    `state_fp` and generates a PAIRWISE covering set of combinations
    (see combinatorics.py's own docstring) -- covering every pair of
    values across every pair of groups at least once, catching the
    large majority of real conjunctive-gating bugs at roughly quadratic
    cost instead of the exponential cost a full cross-product would
    need. Not a new exploration mechanism -- every combination is
    applied via the exact same `_apply_one_combination()` body
    explore_combination() itself uses, just looped, inside one shared
    browser session with semantic dedup deferred to the very end
    (see `_apply_one_combination`'s own docstring for why that matters
    at this scale).

    `state_fp` must already be in run.states, same requirement as
    explore_combination() -- this never invents locators, only
    recombines candidates the crawl already discovered. Raises
    ValueError if the state has fewer than 2 distinct is_choice groups
    (nothing to combine) or if the parameter space is too large to
    cover pairwise at all (see combinatorics.py's own
    MAX_FULL_FACTORIAL guard).

    Returns a summary dict -- {"groups": [...], "group_sizes": [...],
    "full_factorial_size": int, "combinations_tried": int, "results":
    [{"candidate_indices": [...], "outcome": "new"|"revisit"|"blocked"}]}
    -- so a caller (the web API, a report) can show not just "done" but
    HOW MUCH was covered and what each specific combination actually
    found, not just a final aggregate."""
    node = run.states.get(state_fp)
    if node is None:
        raise ValueError(f"state {state_fp} not found in this run")

    groups: dict[str, list[int]] = {}
    for i, c in enumerate(node.candidates):
        if c.is_choice and c.choice_group:
            groups.setdefault(c.choice_group, []).append(i)
    if len(groups) < 2:
        raise ValueError(f"state {state_fp} has fewer than 2 distinct choice groups -- nothing to combine")

    # Stable order (sorted group names) so the SAME state always
    # produces the SAME combination plan across repeated calls --
    # matches generate_pairwise_combinations()'s own determinism, not
    # undermined by an unstable dict/set iteration order here.
    group_names = sorted(groups)
    group_indices = [groups[name] for name in group_names]
    group_sizes = [len(idxs) for idxs in group_indices]
    combos = generate_pairwise_combinations(group_sizes)
    real_combos = [
        [group_indices[g][offset] for g, offset in enumerate(combo)]
        for combo in combos
    ]
    full_factorial = 1
    for s in group_sizes:
        full_factorial *= s

    path_to_state = _path_to_state(run, state_fp)

    from playwright.sync_api import sync_playwright

    limits = {**run.config.get("limits", {}), **{k: v for k, v in limit_overrides.items() if k != "allow_mutating"}}
    max_depth = limits["max_depth"]
    max_breadth = limits["max_breadth_per_state"]
    max_states = limits["max_states"]
    max_flows = limits["max_flows"]
    max_action_repeat = limits.get("max_action_repeat", 2)
    allow_mutating = limit_overrides.get("allow_mutating", run.config.get("allow_mutating", True))
    excursion_max_breadth = run.config.get("excursion_max_breadth", 1)
    config = {**run.config, "limits": limits, "allow_mutating": allow_mutating}

    next_flow_id = [max((f.id for f in run.flows), default=0) + 1]
    seq_to_flow_id: dict[tuple, int] = {}
    revisit_history: set[str] = set()

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for i, candidate_indices in enumerate(real_combos):
                try:
                    outcome = _apply_one_combination(
                        browser, run, config, credentials, persona_name, path_to_state, state_fp, node,
                        candidate_indices, next_flow_id, seq_to_flow_id, revisit_history,
                        max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                        excursion_max_breadth,
                        combo_note=f"Set via an automatic pairwise combination ({i + 1}/{len(real_combos)})",
                        origin_note="Continued after an automatic pairwise combination")
                except (ValueError, RuntimeError) as exc:
                    # A single generated combination failing (e.g. a
                    # candidate that's since become destructive/mutating-
                    # withheld, or a genuine replay error) shouldn't
                    # abandon the rest of the plan -- recorded per-combo,
                    # same "a partial failure doesn't read as a silent
                    # whole-batch no-op" discipline resume_all already
                    # established.
                    results.append({"candidate_indices": candidate_indices, "outcome": f"error: {exc}"})
                    continue
                results.append({"candidate_indices": candidate_indices, "outcome": outcome})
        finally:
            browser.close()

    sem_cfg = run.config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful pass
            run.semantic_dedup_status = f"error on pairwise combinations: {exc}"

    return {
        "groups": group_names, "group_sizes": group_sizes,
        "full_factorial_size": full_factorial, "combinations_tried": len(real_combos),
        "results": results,
    }


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _substitute(text: str, values: dict) -> str:
    """Replaces every `{name}` in `text` with `values[name]`
    (str-coerced). Raises `KeyError(name)` for a placeholder never
    captured by an earlier step -- run_handoff_scenario() turns this
    into a precise checkpoint naming exactly which one, rather than
    silently leaving the literal `{name}` in a URL/search target."""
    def repl(m):
        name = m.group(1)
        if name not in values:
            raise KeyError(name)
        return str(values[name])
    return _PLACEHOLDER_RE.sub(repl, text)


def _handoff_direct_step(browser, config: dict, credentials: dict, seed_url: str,
                          action_label: str | None, run: RunResult):
    """One handoff step's "seed_url" mode: a direct-nav to `seed_url`
    (the exact same mechanism crawl()'s own seed_urls uses), and, if
    `action_label` is given, finding a candidate matching it there
    (case-insensitive exact label match -- not a position, which would
    silently point at the wrong control if the page's layout ever
    shifts between runs) and replaying both steps together. Returns
    (result_tuple_from_run_path_or_None, path_so_far, error_or_None)."""
    seed_el_meta = {"tag": "direct-nav", "href": seed_url, "text": urlsplit(seed_url).path or seed_url}
    seed_trial = Transition(
        from_fp="", to_fp=None, action_label=describe_action(seed_el_meta, None),
        action_norm_signature=f"direct-nav:{normalize_url(seed_url)}",
        risk=Risk.SAFE, risk_reason="handoff step seed URL",
        replay_meta=json.dumps(seed_el_meta),
    )
    seed_result = _run_path(browser, config, [seed_trial], run, credentials)
    if seed_result is None:
        return None, [seed_trial], f"could not reach seed_url '{seed_url}'"
    (seed_fp, seed_url_pat, _, seed_title, seed_candidates, _, seed_unclassified, seed_disabled,
     _, _, _, _, _, seed_captcha, seed_external) = seed_result
    seed_trial.to_fp = seed_fp
    if seed_fp not in run.states:
        run.states[seed_fp] = StateNode(
            fingerprint=seed_fp, url_pattern=seed_url_pat, raw_url=seed_url, title=seed_title,
            candidates=seed_candidates, unclassified_interactive=seed_unclassified,
            disabled_interactive=seed_disabled, captcha_detected=seed_captcha, external_domain=seed_external,
        )
    if not action_label:
        return seed_result, [seed_trial], None

    match = next((c for c in seed_candidates if c.label.strip().lower() == action_label.strip().lower()), None)
    if match is None:
        return None, [seed_trial], f"no candidate labeled '{action_label}' found at '{seed_url}'"
    el_meta = json.loads(match.selector)
    action_trial = Transition(
        from_fp=seed_fp, to_fp=None, action_label=describe_action(el_meta, None),
        action_norm_signature=match.norm_signature, risk=match.risk, risk_reason=match.risk_reason,
        replay_meta=match.selector, is_choice=match.is_choice,
    )
    full_path = [seed_trial, action_trial]
    result = _run_path(browser, config, full_path, run, credentials)
    if result is None:
        return None, full_path, f"action '{action_label}' failed"
    return result, full_path, None


def _handoff_find_step(browser, config: dict, credentials: dict, persona_name: str, search_from: str,
                        contains: str, action_label: str | None, run: RunResult, next_flow_id: list[int]):
    """One handoff step's "find" mode -- the answer to "how does the
    crawler know where in the admin's own menus a specific request
    lives": a bounded, genuinely autonomous DFS search (see _run_dfs's
    own `stop_when`) from `search_from`, as `persona_name`, for the
    first state whose url_pattern, title, or any candidate's own label
    contains `contains` (case-insensitive) -- never told the exact
    destination, the same way ordinary DFS never is. Everything
    explored along the way (successful or not) is recorded in `run`
    like any other exploration; exhausting the budget without finding
    anything is itself a real, reportable finding (this persona
    genuinely can't reach a state matching the target), not a tool
    failure -- see _run_dfs's own stop_when docstring.

    Returns (result_tuple_from_run_path_or_None, path_so_far,
    error_or_None), same shape as _handoff_direct_step."""
    seed_el_meta = {"tag": "direct-nav", "href": search_from, "text": urlsplit(search_from).path or search_from}
    seed_trial = Transition(
        from_fp="", to_fp=None, action_label=describe_action(seed_el_meta, None),
        action_norm_signature=f"direct-nav:{normalize_url(search_from)}",
        risk=Risk.SAFE, risk_reason="handoff find-step search root",
        replay_meta=json.dumps(seed_el_meta),
    )
    root_result = _run_path(browser, config, [seed_trial], run, credentials)
    if root_result is None:
        return None, [seed_trial], f"could not reach search_from '{search_from}'"
    (root_fp, root_url_pat, _, root_title, root_candidates, _, root_unclassified, root_disabled,
     _, _, _, _, _, root_captcha, root_external) = root_result
    seed_trial.to_fp = root_fp
    if root_fp not in run.states:
        run.states[root_fp] = StateNode(
            fingerprint=root_fp, url_pattern=root_url_pat, raw_url=search_from, title=root_title,
            candidates=root_candidates, unclassified_interactive=root_unclassified,
            disabled_interactive=root_disabled, captcha_detected=root_captcha, external_domain=root_external,
        )
    root_node = run.states[root_fp]
    contains_lower = contains.lower()

    def self_matches(node: StateNode) -> bool:
        return contains_lower in (node.url_pattern + " " + node.title).lower()

    def matching_candidate(node: StateNode):
        return next((c for c in node.candidates if contains_lower in c.label.lower()), None)

    def matches(node: StateNode) -> bool:
        return self_matches(node) or matching_candidate(node) is not None

    if matches(root_node):
        found_fp, path_to_found = root_fp, [seed_trial]
    else:
        limits = config.get("limits", {})
        frame = _Frame(fp=root_fp, path=[seed_trial])
        frame.order = _order_for(root_node, limits.get("max_breadth_per_state", 8), run, set())
        stack = [frame]
        states_before = len(run.states) - 1
        flows_before = len(run.flows)
        found_fp = _run_dfs(
            browser, config, run, credentials, persona_name, stack, next_flow_id,
            limits.get("max_depth", 6), limits.get("max_breadth_per_state", 8),
            limits.get("max_states", 30), limits.get("max_flows", 50),
            limits.get("max_action_repeat", 2), config.get("allow_mutating", True),
            states_before, flows_before, False, {}, set(),
            origin_note=f"Handoff: searching for a state matching '{contains}'",
            stop_when=matches,
        )
        if found_fp is None:
            return None, [seed_trial], f"no state found matching '{contains}' within budget"
        # discovered_by_flow is only ever unset for a TRUE crawl root
        # (empty path) -- see _path_to_state's own docstring. This
        # frame's own root is a seed, not that root, but it never goes
        # through emit_flow() either, so the same "empty path" special
        # case would silently misfire for it too if found_fp == root_fp.
        path_to_found = [seed_trial] if found_fp == root_fp else _path_to_state(run, found_fp)

    # "matches" allows a hit on a CANDIDATE's own label (e.g. a listing
    # page's "Review request #482" link) as well as the state's own
    # url/title -- found live: without following that link, "found" was
    # left pointing at the LISTING page, where the requested action_label
    # (e.g. "Approve") never actually lives; it's one hop further, on
    # whatever that specific link leads to. A self-match needs no extra
    # hop; a candidate-only match does.
    found_node = run.states[found_fp]
    if not self_matches(found_node):
        link = matching_candidate(found_node)
        if link is None:
            return None, path_to_found, f"matched '{contains}' but the link it came from is no longer present"
        el_meta = json.loads(link.selector)
        follow_trial = Transition(
            from_fp=found_fp, to_fp=None, action_label=describe_action(el_meta, None),
            action_norm_signature=link.norm_signature, risk=link.risk, risk_reason=link.risk_reason,
            replay_meta=link.selector, is_choice=link.is_choice,
        )
        follow_path = path_to_found + [follow_trial]
        follow_result = _run_path(browser, config, follow_path, run, credentials)
        if follow_result is None:
            return None, follow_path, f"found a link matching '{contains}' but following it failed"
        (new_fp, new_url_pat, new_raw_url, new_title, new_candidates, _, new_unclassified, new_disabled,
         _, _, _, _, _, new_captcha, new_external) = follow_result
        follow_trial.to_fp = new_fp
        if new_fp not in run.states:
            run.states[new_fp] = StateNode(
                fingerprint=new_fp, url_pattern=new_url_pat, raw_url=new_raw_url, title=new_title,
                candidates=new_candidates, unclassified_interactive=new_unclassified,
                disabled_interactive=new_disabled, captcha_detected=new_captcha, external_domain=new_external,
            )
        found_fp, path_to_found = new_fp, follow_path

    if not action_label:
        # Re-replay the found path once more to read a clean, current
        # result tuple for the caller's own capture/explore handling --
        # same reset+replay principle this whole crawler already
        # relies on for everything else, not a special case.
        result = _run_path(browser, config, path_to_found, run, credentials)
        return result, path_to_found, (None if result else "could not replay to the found state")

    node = run.states[found_fp]
    match = next((c for c in node.candidates if c.label.strip().lower() == action_label.strip().lower()), None)
    if match is None:
        return None, path_to_found, f"found a matching state but no candidate labeled '{action_label}' there"
    el_meta = json.loads(match.selector)
    action_trial = Transition(
        from_fp=found_fp, to_fp=None, action_label=describe_action(el_meta, None),
        action_norm_signature=match.norm_signature, risk=match.risk, risk_reason=match.risk_reason,
        replay_meta=match.selector, is_choice=match.is_choice,
    )
    full_path = path_to_found + [action_trial]
    result = _run_path(browser, config, full_path, run, credentials)
    if result is None:
        return None, full_path, f"action '{action_label}' failed"
    return result, full_path, None


def run_handoff_scenario(config: dict) -> RunResult:
    """Executes a scripted, multi-actor scenario -- `config["handoff"]
    ["steps"]`, an ORDERED list of steps, each performed as a named
    persona (`config["personas"]`). Unlike crawl()'s own autonomous
    DFS, a handoff can never be discovered on its own: correlating
    "the request THIS run's employee persona just created" with "the
    one request an admin persona needs to approve" requires a human to
    say so explicitly -- the same reasoning explore_combination() is
    already built on.

    Each step is EITHER:
    - `"seed_url"` (a direct navigation, `{name}`-templated from an
      earlier step's own `capture`) -- for when the operator already
      knows exactly where this step needs to land, or
    - `"find": {"contains": ..., "search_from": ...}` -- a bounded,
      genuinely autonomous DFS search (see `_run_dfs`'s own
      `stop_when`) for a state whose URL/title/candidate labels
      contain the (also `{name}`-templated) target string, run AS the
      step's own persona from `search_from` (default:
      `config["start_url"]`). This is the answer to "how does the
      crawler know where in the admin's own menus to find this one
      specific request" -- it doesn't need to be told the URL, it
      searches for it the same way DFS already searches for anything
      else, just with an early-stop condition. A search that exhausts
      its budget without finding anything is recorded as its own
      checkpoint -- a real, reportable finding (this persona genuinely
      can't reach anything matching the target), not a tool failure.
      A match can land on the target's OWN url/title, or on a
      CANDIDATE's label sitting on some other state (e.g. a listing
      page's own "Review request #482" link) -- found live: the
      latter needs one more hop, automatically followed, since the
      requested `action` (e.g. "Approve") typically lives on whatever
      that link leads to, not on the listing page itself.

    Either way, an optional `"action"` names a candidate (by LABEL, not
    position -- an index would silently point at the wrong control if
    a page's layout shifts between runs) to click once that step's
    landing state is reached.

    `"storage_state"` on a step is either a literal value (a path/dict,
    exactly like the top-level config's own `storage_state`) or a
    `{name}` reference to an EARLIER step's own `"capture_session"` --
    the mechanism that lets a LATER step resume the exact live session
    an earlier step's persona was using, not just log back in as "the
    same persona" from a fresh, unauthenticated context. Necessary for
    e.g. "the user who just registered and submitted a request needs
    to come back, once approved, as THAT SAME (now-approved) account"
    -- a fresh login wouldn't be wrong exactly, but the whole point of
    a handoff is continuity of the SAME session across steps.

    `"capture": {"from": "url", "pattern": <regex>, "as": <name>}`
    extracts a value from the step's OWN resulting url_pattern via a
    regex capture group, for a LATER step's `seed_url`/`find.contains`
    to reference as `{name}`. Deliberately URL-only, not page text --
    a structural signal, not a fragile text-scrape.

    `"explore": true` hands off into a FULL, ordinary autonomous DFS
    crawl from this step's own landing state (the run's normal limits,
    same persona, same live session) -- the answer to "the approved
    user logs in and just keeps crawling normally", reusing `_run_dfs`
    exactly as `crawl()`'s own per-persona pass and
    `explore_combination()` already do, merging every state/flow it
    finds into this SAME `RunResult` (one report, one gap analysis for
    the whole scenario, not a separate result per step).

    KNOWN, DISCLOSED LIMITATION: nothing in this mechanism can read an
    email inbox. A real-world signup flow gated behind "click the link
    we emailed you" cannot be automated by this or any part of
    FlowScout -- there is no mail access, and inventing one is out of
    scope. A handoff scenario with an email-verification step in the
    middle will stall there (the following step's `seed_url`/`find`
    will simply fail to find what it's looking for, recorded honestly
    as a checkpoint) rather than silently skipping past it or
    fabricating a click that never really happened."""
    config = _apply_payment_sandbox(config)
    from playwright.sync_api import sync_playwright

    steps = config["handoff"]["steps"]
    if not steps:
        raise ValueError("handoff.steps must have at least one step")

    personas_cfg = config.get("personas") or [{"name": "default", "credentials": config.get("credentials", {})}]
    creds_by_persona = {p.get("name", "default"): p.get("credentials", {}) for p in personas_cfg}

    run = RunResult(project=config["project"], start_url=config["start_url"], config=config)
    run.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    captured_values: dict[str, str] = {}
    captured_sessions: dict[str, dict] = {}
    next_flow_id = [1]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for i, step in enumerate(steps):
                where = f"handoff step {i + 1}/{len(steps)}"
                persona_name = step.get("persona", "default")
                if persona_name not in creds_by_persona:
                    run.checkpoints.append(Checkpoint(
                        kind="error", flow_id=None, state_fp=None,
                        message=f"{where}: unknown persona '{persona_name}'",
                        detail=f"Known personas: {sorted(creds_by_persona)}",
                    ))
                    break
                credentials = creds_by_persona[persona_name]

                storage_state = None
                raw_storage_state = step.get("storage_state")
                if raw_storage_state:
                    m = _PLACEHOLDER_RE.fullmatch(raw_storage_state) if isinstance(raw_storage_state, str) else None
                    if m:
                        session_name = m.group(1)
                        if session_name not in captured_sessions:
                            run.checkpoints.append(Checkpoint(
                                kind="error", flow_id=None, state_fp=None,
                                message=f"{where}: no captured session named '{session_name}'",
                                detail=f"Captured so far: {sorted(captured_sessions)}",
                            ))
                            break
                        storage_state = captured_sessions[session_name]
                    else:
                        storage_state = raw_storage_state
                step_config = {**config, "storage_state": storage_state}

                result = None
                path: list[Transition] = []
                err = None
                try:
                    if "seed_url" in step:
                        seed_url = _substitute(step["seed_url"], captured_values)
                        result, path, err = _handoff_direct_step(
                            browser, step_config, credentials, seed_url, step.get("action"), run)
                    elif "find" in step:
                        find_cfg = step["find"]
                        contains = _substitute(find_cfg["contains"], captured_values)
                        search_from = _substitute(find_cfg.get("search_from", config["start_url"]), captured_values)
                        result, path, err = _handoff_find_step(
                            browser, step_config, credentials, persona_name, search_from, contains,
                            step.get("action"), run, next_flow_id)
                    else:
                        err = f"{where}: needs either 'seed_url' or 'find'"
                except KeyError as exc:
                    err = f"{where}: references {{{exc.args[0]}}}, never captured by an earlier step"

                if err:
                    run.checkpoints.append(Checkpoint(
                        kind="error", flow_id=None,
                        state_fp=next((t.to_fp for t in reversed(path) if t.to_fp), None),
                        message=f"{where} failed", detail=err,
                    ))
                    break

                (fp, url_pat, raw_url, title, candidates, _, unclassified, disabled,
                 _, _, _, _, _, captcha_detected, external_domain) = result
                if fp not in run.states:
                    run.states[fp] = StateNode(
                        fingerprint=fp, url_pattern=url_pat, raw_url=raw_url, title=title, candidates=candidates,
                        discovered_by_flow=next_flow_id[0], unclassified_interactive=unclassified,
                        disabled_interactive=disabled, captcha_detected=captcha_detected,
                        external_domain=external_domain,
                    )
                # Recorded as its own flow so the scenario's report
                # shows every step, not just whatever "explore" finds
                # afterward.
                run.flows.append(Flow(
                    id=next_flow_id[0], status=FlowStatus.UNIQUE, duplicate_of=None,
                    dedup_reason=f"{where}: {' -> '.join(t.action_label for t in path)}",
                    transitions=path, end_state_fp=fp, persona=persona_name,
                    origin_note="Multi-actor handoff scenario",
                ))
                next_flow_id[0] += 1

                capture_cfg = step.get("capture")
                if capture_cfg:
                    # Matched against raw_url (the real, un-normalized
                    # page.url), not url_pat -- normalize_url() collapses
                    # numeric/UUID path segments to "*" for stable state
                    # fingerprinting (see fingerprint.py), which would make
                    # a capture pattern like r"/requests/(\d+)/confirmation"
                    # structurally unable to ever match (found live: the
                    # digits are already gone by the time url_pat exists).
                    m = re.search(capture_cfg["pattern"], raw_url)
                    if not m:
                        run.checkpoints.append(Checkpoint(
                            kind="error", flow_id=None, state_fp=fp,
                            message=f"{where}: capture pattern didn't match the resulting URL",
                            detail=f"pattern={capture_cfg['pattern']!r} url={raw_url!r}",
                        ))
                        break
                    captured_values[capture_cfg["as"]] = m.group(1) if m.groups() else m.group(0)

                session_name = step.get("capture_session")
                if session_name:
                    # Needs a LIVE context -- the one _run_path used
                    # above already closed (fresh-context-per-path is
                    # this whole crawler's own architecture, see this
                    # file's top-of-file docstring), so this replays
                    # the step's OWN path once more in a fresh context
                    # to reach the same logged-in state and saves ITS
                    # storage_state, rather than threading a live
                    # context handle out of _run_path -- which would
                    # break the "every replay is independent, fresh,
                    # closed" invariant every other feature here relies
                    # on.
                    context = (browser.new_context(storage_state=storage_state) if storage_state
                               else browser.new_context())
                    page = context.new_page()
                    try:
                        page.goto(config["start_url"], wait_until="load")
                        for t in path:
                            perform_action(page, json.loads(t.replay_meta), credentials,
                                            timeout_ms=config.get("limits", {}).get("action_timeout_ms", 8000))
                        captured_sessions[session_name] = context.storage_state()
                    except Exception as exc:
                        run.checkpoints.append(Checkpoint(
                            kind="error", flow_id=None, state_fp=fp,
                            message=f"{where}: could not capture session '{session_name}'",
                            detail=str(exc)[:1200],
                        ))
                        context.close()
                        break
                    context.close()

                if step.get("explore"):
                    limits = config.get("limits", {})
                    max_depth = limits.get("max_depth", 6)
                    max_breadth = limits.get("max_breadth_per_state", 8)
                    max_states = limits.get("max_states", 30)
                    max_flows = limits.get("max_flows", 50)
                    max_action_repeat = limits.get("max_action_repeat", 2)
                    allow_mutating = config.get("allow_mutating", True)
                    node = run.states[fp]
                    frame = _Frame(fp=fp, path=path)
                    frame.order = _order_for(node, max_breadth, run, set())
                    stack = [frame]
                    states_before = len(run.states) - 1
                    flows_before = len(run.flows)
                    _run_dfs(browser, step_config, run, credentials, persona_name, stack, next_flow_id,
                             max_depth, max_breadth, max_states, max_flows, max_action_repeat, allow_mutating,
                             states_before, flows_before, False, {}, set(),
                             origin_note=f"{where}: continued exploring after the handoff")
        finally:
            browser.close()

    sem_cfg = config.get("semantic_dedup", {})
    if sem_cfg.get("enabled", True):
        try:
            apply_semantic_dedup(run, threshold=sem_cfg.get("threshold", DEFAULT_THRESHOLD))
        except Exception as exc:  # never let a dedup-pass bug take down an otherwise-successful scenario
            run.semantic_dedup_status = f"error: {exc}"
    else:
        run.semantic_dedup_status = "skipped: disabled in config"

    run.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return run
