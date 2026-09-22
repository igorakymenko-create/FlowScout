# FlowScout — roadmap

## M0 — DOM crawler skeleton (done)

Working: `flowscout/` (Python + Playwright). DFS crawler with reset+replay
backtracking in an isolated browser context per path, on-screen +
occlusion-aware element discovery, risk classification (safe / mutating /
destructive) with destructive actions never followed, structural
dedup (normalized action-sequence match), human-readable step labels
(verb + page context + menu-kind classification + form-fill summary),
self-contained HTML report.

Verified on a live crawl of saucedemo.com: 8 states / 28 flows (18 unique
/ 10 duplicate / 0 blocked / 0 checkpoints), reproducible across repeated
runs.

### Bugs found & fixed while widening the crawl (Aug 2026)

- **`a[href]` selector blinded discovery to JS-driven links with no `href`**
  (found by noticing the crawler never once reached `cart.html` even with
  breadth=15/depth=8/states=100). saucedemo's cart icon is
  `<a data-test="shopping-cart-link">` with no `href` at all. Fixed in
  `flowscout/actions.py` by dropping the `[href]` requirement — likely a
  real-world-common pattern, not saucedemo-specific.
- **Semantic dedup could merge a completed checkout into an incomplete one**
  (97% text similarity, but one flow clicked "Finish" and reached
  `checkout-complete.html`, the other stopped a step earlier). Fixed by
  requiring two flows to have performed the *identical set* of mutating
  actions before their embedding similarity is even considered — see
  `_mutating_signature_set` in `flowscout/semantic_dedup.py`.

### Found on Site B (Aug 2026)

- **`onScreen` viewport filter was excluding all below-the-fold content**,
  not just genuinely off-canvas elements — any page taller than one
  viewport (720px default) silently lost everything past the first
  screen, including a workout-wizard's own "Next" button. Root cause:
  the check didn't distinguish `position: fixed` off-canvas panels (where
  being outside the viewport really does mean hidden) from normal
  document-flow content (where it just means "hasn't been scrolled to
  yet" — Playwright scrolls automatically before clicking). Fixed by only
  applying the strict viewport check to elements with a `position: fixed`
  ancestor. Verified: Site B's wizard page went from 9 to 16
  candidates; saucedemo's burger-menu occlusion behavior unaffected
  (regression-checked).
- **Candidate priority still starves page-unique content behind repeated
  header nav.** Even after the fix above, "Next" was discovered but never
  clicked in a real run — the site's header (logo + 5 nav links + 3
  language switchers = 9 items) appears identically on every page and,
  being earlier in the DOM, fills the entire `max_breadth_per_state`
  budget before a page's own primary interactive element gets a turn.
  Not fixed: candidate ordering currently has no notion of "this element
  is the same nav link that showed up on the last five pages, de-
  prioritize it." Worth doing before relying on FlowScout for sites with
  a consistent global header/footer, i.e. nearly all of them.
- ~~Div/H3-based clickable elements remain invisible to discovery~~
  **Correction, not actually the cause for "Purchase":** the earlier
  manual investigation zoomed into the innermost text-bearing `<div>`
  label inside the tier cards ("Придбати") and concluded the whole
  control was a non-semantic div. Wrong element — the *actual* clickable
  ancestor wrapping the whole card (name + price + label) is a real
  `button`/`[role=button]`, and it only looked invisible because it, too,
  was below the fold. The viewport fix above surfaced it correctly,
  classified `mutating` (the `purchase` keyword catches it), still
  withheld by `allow_mutating: false`. The wizard's focus-selection cards
  (Option A / Option B / Option C) *are* genuinely non-semantic divs
  though — confirmed via the coverage-delta pass below, still invisible,
  still correctly unclicked (nothing unclassified is ever clicked).
- **Coverage delta (implemented, Aug 2026).** `_DISCOVER_JS` now also
  scans for elements that *look* interactive (`cursor: pointer`, an
  interactive ARIA role, or `tabindex`) but aren't a formal candidate,
  excluding anything already related to one (ancestor/descendant),
  SVG-namespace noise, and oversized containers, then collapses nested
  matches within the same cluster down to the outermost element. Never
  clicked — purely diagnostic. Surfaces as a `unclassified_interactive`
  list per `StateNode`, a "possible blind spots" run-level metric, a
  per-state count column in the report's state graph, and a dedicated
  "Coverage gaps" section listing page/element/class/count. This is what
  correctly found the wizard's Option A/Option B/Option C cards as
  genuine, still-unaddressed blind spots.

### Embeddings provider abstraction (done, Aug 2026 — Gemini live; OpenAI/Voyage paused, see below)

Found while reviewing the operator UI: the "Gemini: configured" badge
and "(needs GEMINI_API_KEY)" label looked like dynamic status text but
weren't, in the sense that mattered — `flowscout/embeddings.py` had no
provider abstraction at all, `_API_KEY_ENV = "GEMINI_API_KEY"` was the
only key the system would ever look for. Widened into real multi-
provider support after a direct follow-up question: not every operator
has a Gemini key -- a company standardized on Anthropic has no Gemini
key by default, and Anthropic itself has **no embeddings model of its
own** (Voyage AI is Anthropic's own recommended embeddings partner, not
a hypothetical third option this project invented).

**Built without live access to either new provider -- an explicit,
deliberate break from this project's usual discipline, not an
oversight.** Every other feature in this file was checked against a
real live call before being trusted. Gemini's own implementation is
still that: unchanged in behavior, still the one that's been exercised
live, repeatedly, throughout this project (most recently: re-verified
live immediately after this refactor, below). OpenAI and Voyage were
written directly from each provider's documented, stable API contract
(`POST /v1/embeddings`, bearer auth, `{"data": [{"embedding": [...]}]}`
response shape for both) with no key available to actually call them.
**Treat them as a best-effort scaffold, not a finished, checked
integration** -- `embeddings.py`'s module docstring has a one-line smoke
test to run once a key is available.

**Design: three providers behind one dispatch, not three code paths in
every caller.** `embeddings.py` now exposes `api_key_configured
(provider)`, `embed_text(text, provider=...)`, `model_name(provider)`,
and `provider_status()` (one row per provider: configured, key_env,
model_name, `verified_live`) -- everything else (per-provider endpoint,
auth header shape, response parsing) is private to the module.
`semantic_dedup.py` and `gap_analysis.py` each read
`run.config["embeddings_provider"]` themselves (defaulting to Gemini)
and pass it straight through -- no signature changes needed at their own
call sites in `crawler.py`/`cli.py`/`web/runs.py`, since both already
receive the full `run`/`config` object. `MODEL_NAME`, previously a
module-level constant `semantic_dedup.py` imported directly, is now a
function call (`embeddings.model_name(provider)`) since the model name
is provider-dependent.

**The threshold-calibration warning from the original framing still
applies, unchanged, and is now stated everywhere the choice is made**:
dedup's 0.95 and gap-analysis's 0.74 were both measured empirically
against Gemini's own similarity distribution (see the M1/M2 entries
above -- real false-merge bugs were found and fixed during that
calibration, not a number picked once). Switching provider without
redoing that same live-measurement discipline risks silently
reintroducing the exact bugs M1/M2 already fixed once. `threshold`
deliberately stayed a plain, non-provider-aware parameter on both
functions rather than gaining a per-provider default table -- there's
no real number to default to for openai/voyage without measuring them
first, and a plausible-looking auto-selected default would be worse
than forcing the caller to consciously supply their own.

**Surfaced everywhere the Gemini-only version was, not just the
backend:** `/api/status` now returns `embeddings_providers` (all three,
each with `configured`/`verified_live`), not just
`gemini_key_configured` (kept alongside for anything still reading the
old field). The web UI's sidebar shows one badge per provider with a ⚠
on the unverified ones (hover explains why); the New Run form gained an
"Embeddings provider" picker whose options are read from the backend's
own registry, not hardcoded in the page, with the same unverified/no-
key warnings folded into each option's label; the help panel explains
what "unverified" means before an operator picks one. `.env.example`
documents all three key names side by side with the same warning.

**Verified live, the one path that could be:** re-ran saucedemo with
`embeddings_provider: "gemini"` set explicitly (not just relying on the
default) through the new dispatch layer end to end --
`semantic_dedup_status`: `"semantic: 13 compared, 1 merged (embeddings,
threshold 95%)"`, a real merge via a real API call, byte-for-byte the
same shape as before this refactor; `gap_analysis.analyze_gaps()`
against two inline TCMS items produced a normal, real verdict
(`"12 flows vs 2 TCMS items ... 1 partially covered, 1 test(s) not
found"`) with no errors either. Confirms the refactor didn't change
Gemini's behavior, only added a dispatch layer in front of it.

**Not verified, and said so everywhere an operator would look:**
OpenAI and Voyage's actual HTTP calls, response parsing, and both
providers' default thresholds. Next step for whoever has a key: the
smoke test in `embeddings.py`'s docstring, then the same live-
calibration pass M1/M2 document doing for Gemini (real crawl, real
known-duplicate and known-distinct flow pairs, measure the actual
cosine-similarity distribution) before trusting either provider's
results for anything that matters.

**OpenAI got a real key, took one real step further, then paused --
same session, same day.** A live key was set (`OPENAI_API_KEY` in
`.env.local`). First check, `GET /v1/models` (free): succeeded --
key valid, `text-embedding-3-small` visible in the list, ruling out a
model-name or auth-header mistake. Second check, the actual smoke test
(`embed_text('hello world', provider='openai')`): failed with
`429 insufficient_quota` -- OpenAI's own error code for "this account
has no available balance," distinct from `rate_limit_exceeded` (a
throughput problem) and confirmed not to be one by checking the
account's own free-tier rate-limit table (40,000 TPM / 100 RPM easily
covers one 16-token test call). An account-level billing gap, not a
code defect -- but it means the one thing that actually needed live
verification (does the success-path response parsing match a real 200
payload) still hasn't been exercised.

**Paused at the user's explicit request rather than left registered-
but-broken.** Both OpenAI's and Voyage's implementation blocks and
their two lines in `embeddings._PROVIDERS` are now commented out, not
deleted -- `_PROVIDERS` currently registers only `gemini`.
`provider_status()` (and everything downstream of it: `/api/status`,
the web UI's badges, the New Run provider dropdown) now shows only
Gemini, automatically, with no separate UI change needed -- confirms
the abstraction built above is genuinely data-driven, not a hardcoded
three-item list repeated in the frontend. A stale saved config or old
UI state still referencing `"embeddings_provider": "openai"` degrades
the same way an unset key already did: `EmbeddingsUnavailable("Unknown
embeddings provider 'openai' (known: gemini)")`, caught by both
`semantic_dedup.py` and `gap_analysis.py`'s existing exception
handling -- confirmed live, not assumed. To resume: uncomment the
OpenAI/Voyage sections in `embeddings.py` (marked clearly at the top of
each block) once OpenAI's billing is resolved and a Voyage key exists
to test with.

### Known follow-ups (polish, not blocking)

- ~~Image-only link labels are raw identifiers~~ **Fixed (Aug 2026)**,
  triggered by hitting it for real on Site B: a logo `<a>` wrapping
  `<img alt="ACME">` with no text of its own fell all the way through to
  the bare tag name, showing up in a flow as the meaningless `Open "a"`.
  `_DISCOVER_JS` (`flowscout/actions.py`) now falls back to a contained
  `img[alt]`, then `title`, before giving up. Verified against the actual
  Site B logo (now labeled `ACME`); should also improve saucedemo's
  image-link labels the same way, unverified.

## M1 — Flow-as-artifact + semantic dedup

- **Semantic dedup: implemented and live.** `flowscout/embeddings.py` calls
  the Gemini Embeddings API via stdlib `urllib` — no new dependency.
  `flowscout/semantic_dedup.py` runs a post-pass over flows still marked
  UNIQUE after structural dedup, embeds each flow's page+action text, and
  merges any pair at cosine similarity ≥ 90% (configurable per-run via
  `"semantic_dedup": {"threshold": ...}`). Degrades gracefully with no
  key set — `run.semantic_dedup_status` records why it didn't run,
  structural dedup alone still applies, and the crawl never fails
  because of this.
  **Model: `gemini-embedding-2`** (not `-001`, not `-2-preview`) — there's
  no Flash/Pro split for embeddings, that's a generative-model concept.
  Picked by testing live against this project's own key rather than
  trusting docs (which gave inconsistent answers across pages): all
  three available models work with an identical request shape;
  `-001` has a documented free-tier quota problem on batch embedding
  (github.com/RooCodeInc/Roo-Code/issues/5713); `-2` is GA (not preview),
  doubles the input token limit (8192 vs 2048), and passed an 8-call
  back-to-back burst test with zero failures.
  **Key setup:** `GEMINI_API_KEY` in `.env.local` (gitignored; see
  `.env.example`), loaded automatically by `flowscout/dotenv.py`.
  (Switched from OpenAI/Anthropic to Gemini per the user's Aug 2026
  choice — their Claude API access was unavailable for testing at the
  time.)
  **Two tiers, not one — found and fixed a real false-merge bug.**
  First live run at threshold 90% merged 18 unique flows down to 5;
  spot-checking against `end_state_fp` (deterministic, already computed
  during crawling) showed some of those merges connected flows that
  don't even end in the same application state — the embedding was
  picking up shared-prefix text ("Login -> Open Menu -> ...", identical
  across most saucedemo flows) rather than genuine intent overlap. Fix:
  `flowscout/semantic_dedup.py` now runs state convergence first (free,
  exact — flows sharing an end-state fingerprint are duplicates, no
  embedding call needed) and only sends the *remaining* flows to
  embeddings, at a raised threshold (95%) with the report reason
  flagged "review recommended" for that tier specifically, since it's
  inherently fuzzier than an exact fingerprint match.
- **Tier 1 (state convergence) had the same false-merge class tier 2
  already got fixed for, just not yet applied there — found while
  investigating vision fallback (Aug 2026), fixed immediately.**
  `end_state_fp` is built only from the destination page's candidate
  set, so a flow that removed a cart item or reset app state on the way
  there fingerprints identically to one that only navigated there —
  `_apply_state_convergence` was merging them as duplicates and
  silently discarding the mutating one. Measured, not hypothetical: on
  the saucedemo-wide run, `remove` (cart item removal) and
  `reset-sidebar-link` (Reset App State) never appeared in **any**
  unique flow's output at all — every occurrence got merged into a
  shorter duplicate that never performed them. Consequence up the
  pipeline: gap analysis (M2) reported these as "the app doesn't do
  this" (`not_found`) when the crawl did do them and threw the result
  away — a false negative stated as fact. Fix: tier 1 now keys on
  `(end_state_fp, mutating_signature_set)` instead of `end_state_fp`
  alone, reusing the exact guard tier 2 already had. Also found in the
  same pass: `risk.py`'s `_MUTATING_KEYWORDS` was missing `"reset"`, so
  Reset App State classified as SAFE, hiding it from the guard too
  (fixed alongside). Re-crawled saucedemo-wide end to end to confirm,
  not just unit-checked: unique flows went from 11 → 27, `remove` and
  `reset-sidebar-link` both now surface (previously 0 of 1 and 0 of 9
  occurrences kept; now 1 of 1 and 6 of 6), and the newly-surfaced
  `reset-sidebar-link` flows correctly show up as gaps against
  `fixtures/tcms_saucedemo.csv` (no test plan entry covers it). One
  flow (`back-to-products`, after visiting a product detail page) still
  doesn't survive — checked, and confirmed legitimate: it converges
  with a menu-open/close flow that also performs zero mutating actions,
  so both are correctly the same "pure navigation" flow under the
  current identity model, not a re-introduction of the bug just fixed.
- Human-readable flow documents (export beyond the HTML report: Markdown).

## M2 — Gap analysis vs TCMS (done)

- **`flowscout/tcms.py`** — vendor-agnostic CSV import (case-insensitive
  column matching, accepts common TestRail/Xray/Zephyr synonyms). Only
  `title` is required.
- **`flowscout/gap_analysis.py`** — bidirectional nearest-neighbor
  matching by Gemini embedding cosine similarity: flow → best TCMS match
  (no match above threshold = **gap**, the app does this and nothing
  tests it) and TCMS → best flow match (no match = **not_found**, could
  be a stale test, an app change, or something this crawl didn't reach).
  New CLI subcommand `flowscout gap --run <dir> --tcms <csv>` re-runs
  this against an already-completed crawl's `flows.json` with no
  re-crawl needed; `flowscout crawl --tcms <csv>` runs it inline.
  Degrades gracefully like the M1 passes — no key, empty TCMS, or zero
  unique flows all produce a clear `status` string instead of failing.
  **Threshold (0.75) calibrated empirically**, same discipline as M1: a
  full similarity matrix was computed against `fixtures/tcms_saucedemo.csv`
  (a hand-authored plausible pre-FlowScout test plan) run against the
  wide saucedemo crawl. First pass reused M1's flow-text representation
  and was unusable — "Login with valid credentials" won the top match
  for 9 of 11 flows, because every flow's transcript starts with the
  same login+menu boilerplate that human-written TCMS titles never
  mention. Fixed by writing a *separate* representation for gap
  matching (`_flow_text` in `gap_analysis.py`, not shared with
  `semantic_dedup.py`'s): destination page + the flow's mutating actions
  only, boilerplate stripped. Re-run: genuine matches clustered
  0.756–0.822, genuinely-absent test cases (never-run invalid-login,
  never-run logout) clustered 0.646–0.653 — clean separation, 0.75 sits
  in the gap.
  **A crawler limitation surfaced itself through this**, unprompted:
  "Sort inventory items by price" scored right at the boundary (0.749,
  correctly flagged not-found) because `<select>` dropdowns aren't in
  `discover_candidates`'s selector at all — FlowScout couldn't even see
  the sort control, let alone test it. Noted here as a real finding, not
  filed as a "known follow-up" nicety, because it's exactly the kind of
  gap the tool exists to surface. **Fixed (Aug 2026)** — see "State
  fingerprint blind to configuration-like selections" below, which added
  native `<select>` support as part of the same identity fix; re-run
  confirmed this exact test case now `covered` at 0.904, the single
  highest-confidence match in the whole run.
- **Fixed (Aug 2026): matching was flow-level, now action-level for
  mutating behavior.** A flow performing several mutating actions
  (add-to-cart *and* remove, say) used to get exactly one TCMS verdict
  for the whole flow. Found while investigating why a newly-surfaced
  `reset-sidebar-link` flow (see M1's state-convergence fix above)
  matched a test case correctly when it was a flow's *only* action, but
  hid inside a "covered" verdict when the same action appeared alongside
  `add-to-cart` in a different flow matched to the add-to-cart test —
  the identical real action reported as both a gap and covered depending
  which flow happened to carry it. Measured on saucedemo-wide: 11 of 21
  "covered" flows performed more than one mutating action.
  Shipped in two steps, each independently verified against a live
  re-crawl before moving to the next:
  1. **Cheap mitigation first** (no matcher change, no recalibration
     risk): `FlowCoverage.mutating_actions` started carrying every
     mutating action a flow performed regardless of status, and the
     report surfaced covered flows doing more than one thing instead of
     letting "covered" silently speak for all of them.
  2. **Real fix**: `gap_analysis.py` now compares one embedding per
     *distinct mutating action* (`action_norm_signature`) against the
     TCMS, not one per flow — 9 distinct actions vs. 27 flows on
     saucedemo-wide, so *fewer* embedding calls despite finer detail.
     Each flow's status is derived from its own actions' verdicts:
     `covered` only if every action matched something, the new
     `partial` status if some did and some didn't (with exactly which
     spelled out per-flow), `gap` if none did.
     **First attempt at this regressed three real matches** (TC-01
     Login, TC-03 view product detail, TC-04 view cart flipped from
     correctly "covered" to "not_found") — caught by re-running the
     live calibration against the same fixture rather than assuming the
     new matcher was strictly better. Root cause: those three TCMS items
     describe pure-*navigation* behavior with no mutating action to
     match against at all, and the old flow-level text's real signal for
     them was the flow's *destination page* ("Ends on Cart"), which an
     action-only pool doesn't carry. Fix: navigation-only flows (empty
     mutating set) keep their original whole-flow destination-page
     representation in a second pool, matched only against whatever the
     action pool doesn't already claim — except flows with *zero*
     non-boilerplate content (e.g. "Login > open menu > close menu"),
     which get a distinct `navigation` status and are excluded from
     matching entirely rather than padded out with generic filler text
     (the M4-documented trivial-no-op-as-gap failure mode). Re-verified
     end to end: the three regressed items recovered, the genuinely-
     uncovered set (TC-08 invalid login, TC-09 logout, TC-10 sort) came
     back to exactly the same 3 items the very first M2 calibration
     found, and `reset-sidebar-link`/`continue-shopping` no longer hide
     inside any "covered" verdict.
  **Threshold (0.74)** recalibrated live for the new action pool
  (genuine matches 0.7475–0.8478, genuinely-uncovered actions ≤0.7307);
  separately checked against the (unchanged) navigation-pool
  representation and found to sit inside its original 0.646–0.822 gap
  too, so one threshold serves both pools without compromise — checked,
  not assumed, per the standing rule after M1/M2's provider-switch
  warning.
  M4 codegen's default candidate filter now includes `partial` flows
  alongside `gap` ones, since a partially-covered flow still has real
  untested behavior in it.
  **Still not fixed, honestly:** order/precondition text ("remove an
  item *that was previously added*") isn't captured at the action level
  any better than before; per-page signature aliasing (`add-to-cart` vs
  `add-to-cart-*` — see M3.5's identity.py notes) still double-books as
  two capabilities instead of one; and "view this page" TCMS items are
  still matched by a whole-flow embedding rather than anything that
  actually reasons about page identity.

## Config surface additions (Aug 2026)

- **`exclude_patterns`** (list of glob patterns, e.g. `["*/privacy*",
  "*/terms*"]`) — operator no-go pages. Matched against a link's target
  path in `risk.classify()`, same treatment as an external domain:
  DESTRUCTIVE, never followed, regardless of `allow_mutating`. Cheap,
  useful independent of any UI question — added directly to the JSON
  config schema. Doesn't apply to in-page anchors (no separate path to
  match against), only real page navigations.

## Superseded — Vision fallback (investigated live, Aug 2026; not building it)

Original idea: hybrid mode, DOM discovery stays primary, fall back to a
screenshot + vision-model pass on states where the coverage delta shows
suspiciously many blind spots — motivated by the Site B wizard's
Option A/Option B/Option C cards (div-based controls, no `<button>`/
`role="button"`) being invisible to `discover_candidates`.

**Investigated against the live site before building anything, and the
motivating case turned out not to need vision at all.** Four live
probes against `https://site-b.example/en/wizard` (Chromium via CDP,
matching `crawler.py`'s own launch settings):

1. **Detection**: already solved. `unclassified_interactive` (the
   coverage-delta heuristic already shipped) catches all three cards
   today — confirmed straight from `runs/site-b/flows.json`, no live
   call needed for this part.
2. **Classification** ("is this div actually clickable, or just
   `cursor: pointer` on something decorative?"): `CDP
   DOMDebugger.getEventListeners` and the React fiber's own
   `__reactProps$*.onClick` **both** confirm a real `click` handler on
   each card, with 100% agreement between the two independent signals.
   No vision needed to answer this either — this is exactly Tier 1
   below.
3. **Does clicking one do anything?** First attempt said no (byte-
   identical HTML before/after) — which would have been a real reason
   to reach for vision. Caught before trusting it: the card clicked
   (Option A) turned out to be the wizard's pre-selected default
   (`border-primary bg-primary/5 shadow-lg` already present on load),
   so the "no-op" was a real no-op, not a detection failure. Re-run
   against a genuinely non-default card (Option B) showed 6 DOM
   mutations and a real content swap — confirming selection *does*
   work, and that a naive before/after diff on the wrong element would
   have produced a false "vision is needed here" conclusion.
4. **Does it matter for the crawl's own state model?** No — and this is
   the actual finding. `state_fingerprint()` is `url_pattern +
   candidate signatures`; picking Option B over the default Option A
   changes the page's *content* but not its *set of interactive
   controls*, so the fingerprint is bit-identical before and after
   (`42c892766bcc5260` both times), and the state one step later (after
   "Next") is *also* identical between the two paths
   (`e47a2d3d9e1ed9fa` both times, zero controls unique to either path).
   Structural dedup would discard the Option B path as a duplicate of
   the Option A path regardless of how well the card is detected or
   located.

**So the chain breaks somewhere vision can't fix.** Detection: solved
already. Classification: solved by Tier 1 below, free, deterministic,
no model call. Locator stability, resolution independence, cost — all
moot, because the actual blocker is upstream of all of them: the
fingerprint that decides "is this a new state worth exploring" doesn't
account for config-like selections that change page content without
changing the control set. Building vision fallback would have shipped
a capability that doesn't move this case at all. Not filing this as
"still needed, just deprioritized" — the investigation changed the
conclusion, not just the schedule.

**One implementation trap found along the way, worth keeping even
though vision itself isn't being built:** the third card (Option C)
was disabled (`opacity-50 cursor-not-allowed`), which a real `<button>`
would expose via `.disabled` — a bare `<div>` doesn't, `el.disabled` is
`undefined`. Any promotion of coverage-delta elements into real
candidates (Tier 1 below) must check `aria-disabled` and the element's
own disabled-looking class/style convention, or it will click controls
the page itself is refusing to offer and record a phantom flow.

## CDP-based control detection (done, Aug 2026)

**What actually answers "is this div-as-button real"**, found during
the vision investigation above and implemented here: `CDP
DOMDebugger.getEventListeners` gives a factual yes/no with no threshold
to tune and no model call, replacing the old `cursor: pointer` / ARIA-
role / `tabindex` guess as the detection mechanism, not just as a
verification pass layered on top of it. Chromium-only, but the crawler
already runs Chromium only.

**Why this jumped ahead of the state-fingerprint problem below in
priority.** The old heuristic's real defect isn't any single missed
element, it's the shape of the failure: a markup/style whitelist is
weakest on exactly the newest code, since new code is what's least
likely to happen to match a known pattern. Detection that verifies
actual behavior (a real listener) rather than guessing from appearance
doesn't have that property — a normal new feature, built in whatever
style is current, produces no new "needs attention" item at all, because
its controls simply get found. Confirmed empirically before committing
to this priority order: on the Site B wizard, `unclassified_interactive`
had already been flagging the cards as *possibly* clickable since M0 —
the missing piece was never detection, it was turning "possibly" into a
verified "yes, and here's what happens when you click it."

**Implementation** (`flowscout/actions.py`):
- `_DISCOVER_JS` gathers a "pool" of every visible, non-trivial element
  not already part of a formal (markup-matched) candidate, capped at 40
  descendants per element (avoids CDP-querying huge structural
  containers; a legitimately larger custom card is a known, accepted
  miss). No cursor/role/tabindex prefilter — the old heuristic is kept
  only as a fallback bucket (`legacyUnclassified`), used solely if CDP
  verification fails outright, so a CDP failure degrades to previously-
  shipped behavior rather than silently promoting nothing.
- `_verify_pool()` checks each pool element via CDP
  (`DOMDebugger.getEventListeners`, direct on the element only) OR via
  its own React fiber `onClick` prop (`hasReactOnClick` in
  `_DISCOVER_JS`) — either is sufficient. Verified, non-disabled
  elements get promoted into real `ElementCandidate`s
  (`discovered_via="handler"`) and are clicked like any other candidate;
  verified-but-disabled ones (aria-disabled, a disabled-looking class,
  or `pointer-events: none`) are reported separately
  (`StateNode.disabled_interactive`), never clicked.
- **Measured cost, not assumed**: ~0.7–0.8ms per CDP call on real pages
  (85–187 DOM nodes on the two live targets used throughout this
  project) — a full-page sweep costs well under 0.1s. No JS-side
  prefilter was needed to keep this affordable.

**A rejected design, kept in the code comments as a warning: ancestor-
walk event delegation.** First version also walked up to 3 parent
levels looking for a listener, to catch patterns where a wrapper
handles clicks for the whole group. Caught by re-testing against
saucedemo (not just Site B) before shipping: this produced a real false
positive — saucedemo's footer copyright text got promoted as a
"control" because React 17+ attaches its *entire* synthetic event
system to the app's root container, so walking up 3 levels from nearly
any element in a React app eventually hits that root-level delegation
listener, which says nothing about whether *this specific element* does
anything. Root cause understood, not patched around: removed the
ancestor walk entirely; the React-fiber-prop check (added for the same
reason) correctly covers the delegation case for React specifically by
reading what the element's own fiber declares, without depending on
where the underlying native listener physically lives. Known residual
gap: a div-as-button built via delegation to a *non-React* framework's
own root/document handler still won't be found — flagged, not solved,
rather than reintroducing the ancestor walk's false-positive class to
chase it.

**A second bug found the same way (dedup, not detection):** a verified
card's own children (a heading, a paragraph) also independently
verify — a click on any of them bubbles to the same handler — so
without deduping, one real control promoted as 2–3 duplicate candidates
(confirmed on Site B: one wizard card produced 3, including one with an
empty label from an icon-only wrapper div with no text of its own).
Fixed with `_dedupe_outermost()`: among verified elements, keep only
the outermost per containment cluster — the CDP-verified equivalent of
the dedup the old visual heuristic already did, just applied to ground
truth instead of a guess.

**Verified end-to-end against both live targets**, not just unit-level:
- Site B's `/en/wizard`: all 3 wizard cards found — 2 correctly
  promoted and clicked (Option A, Option B), 1 correctly identified
  as real-but-disabled (Option C, gated behind an earlier
  selection) and never clicked. A full production crawl
  (`flowscout crawl --config configs/site-b.json`) completed in ~2.5
  minutes with these controls now real candidates, no performance
  regression from the CDP calls.
- saucedemo: zero false positives across inventory/cart/checkout pages
  (including the footer bug above, confirmed fixed) and zero missed
  formal candidates — the markup-based path is untouched.

**Two unrelated bugs found and fixed while extending this exact code
path**, per this project's standing practice of fixing real bugs found
during testing immediately rather than filing them for later:
- `RunResult.from_json()` never reconstructed `StateNode.candidates` —
  `candidates=[]` was permanent, not a placeholder. Every candidate a
  crawl found silently vanished from the report's States table
  (outdegree/risk columns) on any regeneration from a saved
  `flows.json` (`flowscout gap`, `flowscout confirm`, the web UI's gap
  re-run) instead of a fresh crawl — and because `flowscout gap` also
  *writes back* `flows.json`, this was self-reinforcing: each re-run
  baked the loss in permanently. Confirmed on real data before fixing:
  `runs/saucedemo_wide/flows.json` had lost candidates on all 26 states
  after several `gap` re-runs during this project's own M2 rework
  earlier in the same session. Fixed, and the affected run was restored
  via a fresh crawl (the only real source of truth once lost).
- `_DISCOVER_JS`'s new comments used literal `\n\n` inside a Python
  triple-quoted (non-raw) string meant to illustrate JS output — Python
  quietly turned that into two real newline characters, which broke a
  `//` JS comment (only valid to end-of-line) and produced a JS
  `SyntaxError` caught immediately via `node --check` on the generated
  script rather than a live browser call. Fixed by rephrasing the
  comment and converting `_DISCOVER_JS` to a raw string (`r"""..."""`),
  removing the whole class of bug rather than just this instance.

**Report additions**: "Handler-discovered controls" (the positive
counterpart to the old "coverage gaps" list — what was actually found
and clicked, not just noticed) and "Disabled controls found" sections,
plus `handler_discovered_total`/`disabled_interactive_total` metrics.
"Coverage gaps" itself is now framed as a CDP-failure fallback,
expected empty on a normal run, rather than a normal-case metric.

## State fingerprint blind to configuration-like selections (done, Aug 2026)

**The real finding from the vision investigation.** `state_fingerprint()`
hashes the URL pattern plus the *set* of interactive-candidate
signatures. Picking one wizard option over another (Site B's
Option A/Option B/Option C cards; a `<select>` sort order) changes what
the page *does* downstream without changing *which controls exist on
it* — so two genuinely different configuration paths collapsed onto the
same fingerprint, deduped as if they were one flow, with the crawler
only ever surfacing whichever branch DFS happened to hit first.

**Fixed the same way M1's state-convergence bug was fixed, not by
touching the fingerprint.** `state_fingerprint()` is untouched — still
just URL pattern + candidate-signature set, still exactly as sensitive
(or insensitive) to incidental variation as it was before, so none of
M5's measured Site B non-determinism got worse. Instead,
`identity.py`'s `mutating_signature_set()` — already the dedup key for
state-convergence and the anchor for cross-run `flow_identity()` — was
widened to include a new `is_choice` dimension alongside `risk ==
MUTATING`. Two flows reaching the identical fingerprint now stay
distinct if they picked different alternatives, exactly like two flows
reaching the identical fingerprint already stayed distinct if one of
them removed a cart item and the other didn't.

**`is_choice` is deliberately independent from `risk`.** Picking a sort
order or a workout focus has no state-changing consequence worth gating
behind `allow_mutating` (unlike checkout or add-to-cart) — conflating
"carries a distinguishing choice" with "unsafe to click without opt-in"
would have made `allow_mutating=false` silently stop exploring wizard
options, an unrelated and unwanted side effect. `Transition.is_choice`
is a new, separate field for exactly this reason.

Every place that used to filter on `risk == Risk.MUTATING` as a proxy
for "this is what makes a flow worth caring about" was widened to `risk
== Risk.MUTATING or is_choice` for consistency: `gap_analysis.py`'s
action pool (a wizard choice is now its own TCMS-comparable capability),
`shared_steps.py`'s test-worthiness check, `testcase_draft.py`'s title
generation. `crawler.py`'s `allow_mutating` gate and `risk.py` itself
were deliberately left untouched, per the paragraph above.

**Two new sources of the choice, one native, one detected:**
- **`<select>`** — a new action shape (`actions.py`'s `_DISCOVER_JS`
  gathers one candidate per `<option>`, not one per `<select>`, since
  "sort low-to-high" and "sort high-to-low" are different user actions,
  not the same click on different days). Executed via Playwright's
  `select_option(value=...)`, not `.click()` — a new branch in
  `perform_action()`, `fill_enclosing_form()` (guarded out, a select
  choice isn't a form submission), and M4's `playwright_codegen.py`
  (generates `select_option()` in drafted specs, not `.click()`).
- **Div-as-button choice groups** — among CDP-verified handler-
  discovered elements, `_detect_choice_groups()` clusters by literal
  parent-element identity (JS `Map` keyed on the DOM node itself, no
  generated string key to collide) and marks 2+ siblings under the same
  parent as `is_choice` -- the shape Site B's wizard cards take (three
  identically-styled siblings under one grid container).

**Also fixed: handler-discovered candidates never survived
`max_breadth_per_state` even after CDP found them.** Detection working
turned out not to be sufficient — found by checking the real crawl
output, not assumed fixed once the cards showed up in `discover_candidates()`.
Truncation happens *before* anything is clicked, ranked by risk tier
only; Site B's cards sorted after ~13 ordinary nav links within the SAFE
tier and never made an 8-candidate budget on every single state that
had them, confirmed across all 5 states/locales checked. Fixed by
sorting handler-discovered candidates before markup ones within the
same risk tier — the elements this detection mechanism exists to reach
are worth spending scarce breadth budget on first.

**Two more real bugs found via the actual live crawl, not the isolated
unit tests that had already passed:**
- **`normalize_signature("select-choice-...")` still collapsed
  everything.** First version of the `<select>` signature used a
  `"select-choice-"` string prefix for structural dedup, forgetting
  that `_norm_token`'s generalization list already treats any
  `"select-"`-prefixed signature as one interchangeable slug (by design,
  for a *different* purpose — collapsing `"add-to-cart-sauce-labs-
  backpack"` to `"add-to-cart-*"`). Caught immediately by testing the
  actual `normalize_signature()` output rather than trusting the
  variable name: `"choice-"` (no "select" prefix at all) fixed it.
- **Text-based locators timed out on every real click of a handler-
  discovered card**, surfaced only by the live crawl producing 4 real
  error checkpoints, not by any isolated discovery-only test run before
  it. Root cause: a card's full `innerText` spans multiple rendering
  blocks (heading + description, "Option A\n\nSample description text..."),
  but Playwright's `get_by_text()` matches against `textContent`, which
  has *no* separator between adjacent block children at all — so
  neither an exact nor a substring match against the (space-joined)
  full text ever succeeds, at any truncation length, confirmed by
  testing both directly against the live page before guessing at a fix.
  Fixed by capturing only the first text block (`firstBlockText()` in
  `_DISCOVER_JS`, split on raw newlines before any whitespace
  collapsing) for every element-gathering pass, not just the new one —
  shorter, uniquely resolvable, and a cleaner report label as a side
  effect (no more mid-word truncation like `"...functional str"`).

**Verified end-to-end on real data, each fix checked before moving to
the next:**
- `state_fingerprint()` measured bit-identical (`8ea284bbd11b3f2b`)
  after sorting saucedemo's inventory low-to-high vs. high-to-low —
  confirms the original problem was real, not assumed.
- `mutating_signature_set()` on the same two flows:
  `{choice-product-sort-container-lohi}` vs.
  `{choice-product-sort-container-hilo}` — genuinely different dedup
  keys despite the identical fingerprint.
- Full `flowscout crawl --tcms` on saucedemo-wide: **TC-10 ("Sort
  inventory items by price"), `not_found` since the very first M2
  calibration, is now `covered` at 0.904 — the single highest-confidence
  match in the entire run.** Three of the four sort choices survived as
  distinct unique flows (the fourth lost to ordinary breadth-budget
  competition, not a bug).
- Full `flowscout crawl` on Site B: two wizard-card flows
  (`Menu > Wizard > Option B` and `> Option A`) land on the
  *identical* `end_state_fp` and both correctly stay `status: unique`;
  a longer path to the *same* choice (via `About` first)
  correctly still collapses as a duplicate of the matching one. Cross-
  run `flow_identity()` confirmed distinct for the two
  (`c2f0478eafa30caa` vs. `d36a1a8a6d8f20b0`). Zero checkpoints, zero
  blocked flows (down from 4 of each before the locator fix).
- M4 codegen against the refreshed saucedemo-wide run: generates real
  `select_option(value="lohi")` calls against a stable `data-test`
  locator (not flagged fragile), `ast.parse()`-valid across all 23
  generated test functions.

**Known residual gaps, not solved by this:**
- Choice-group clustering is parent-identity-based only — a choice
  group whose members *aren't* DOM siblings (e.g. split across two
  containers for layout reasons) won't be detected.
- `<select>` support doesn't yet extend to `<input type="radio">` groups
  or ARIA `role="radiogroup"`/`role="tablist"` patterns — same
  underlying shape, not yet wired to the same `is_choice` mechanism.
  **Resolved for radio/checkbox — see "Radio buttons and checkboxes as
  choice candidates" below; ARIA `role="radiogroup"`/`role="tablist"`
  remain unhandled.**
- The already-documented M3.5 imprecision (same signature from
  different pages, or numeric option values getting stripped by
  `_norm_token`'s trailing-digit rule) applies to choice signatures the
  same way it applies to mutating ones — not new, not re-solved here.
  **The numeric-stripping half of this was hit for real and fixed for
  every `choice-`-prefixed signature — see below.**

## Radio buttons and checkboxes as choice candidates (done, Aug 2026)

**Prompted by a direct question:** a real page can carry "множество
параметров" (many parameters) — dropdowns, radios, checkboxes — that
often affect navigation/flow and, even when they don't, are exactly the
kind of thing a TCMS test case asserts about. Radio and checkbox inputs
were completely invisible to the crawler before this: `_DISCOVER_JS`
never gathered them, so a page like httpbin's own pizza-order form
(3 radios + 4 checkboxes + 4 text fields) surfaced exactly one candidate
— the Submit button — and the submitted flow's label silently omitted
which size/toppings had actually gone out with the request, even though
the browser's own defaults determined that on every submit.

**Scope, deliberately bounded to three of the four items originally
proposed** (the fourth — comparing TCMS test cases that assert about
*parameter combinations/data correctness* against found flows — stays a
separate, later topic; FlowScout's core promise is reachability/
structural facts, never asserted data correctness, so that item's honest
ceiling is a `partial` gap-analysis match at best and needs its own
design pass, not a rider on this one):

1. **Radio buttons treated like `<select>`.** Mutually exclusive by
   `name` (HTML's own grouping), so N options → N `is_choice` candidates,
   the same shape a `<select>`'s options already got. New `radioGroup`/
   `radioValue` fields in `_DISCOVER_JS`'s gathering pass, a `"radio"`
   branch in `_build_candidate()` (signature keyed on group+value, same
   reasoning as `<select>`'s `dataTest`+`selectValue` keying), a
   structural `input[type="radio"][name=...][value=...]` locator in
   `build_locator()` (tried before the generic text fallback — radio
   labels are often short/generic like "Yes"/"Small", more collision-
   prone elsewhere on the page than a `<select>`'s own `dataTest`/`id`).
2. **Checkboxes treated as independent single-toggle actions, explicitly
   NOT 2^N combinatorial exploration of every checked/unchecked state** —
   the user's own framing, confirmed and implemented as such. Each
   checkbox is one candidate (toggle it), not one candidate per subset.
   Still `is_choice=True`: two flows ending up with different boxes
   checked are genuinely different flows for identity purposes, the same
   reason a radio pick or sort order already had to stay distinct, even
   though checking one box doesn't exclude any other the way a radio
   pick does.
3. **Observed radio/checkbox state recorded on the submitting flow's
   label, even for parameters this specific DFS path never explicitly
   touched.** New `_read_choice_state()`: a read-only DOM read of every
   radio group's checked option and every checked checkbox within the
   submitting element's own `<form>`, at the moment of submit — a
   submitted form carries real values (the page's own defaults, if
   nothing was clicked) and those shouldn't be invisible in the report
   just because this particular replay path happened not to touch them.
   Threaded through as a genuinely separate return value at every layer
   (`perform_action()` now returns `(fill_summary, choice_state)`,
   `_run_path()` returns both as its 7th/8th elements) and merged into
   `describe_action()`'s label dict *without* ever reaching
   `Transition.form_fields` — M4's codegen turns `form_fields` into
   `.fill()` calls, and `.fill()` raises on a radio/checkbox input.

**Two real bugs found by testing the actual live output, not assumed
correct from the design:**
- **Numeric checkbox/radio values collapsed to the same
  `norm_signature`.** `httpbin`'s own form happens to use text values
  (`"small"`, `"bacon"`), so a first pass looked clean; a follow-up check
  built specifically to probe the risk flagged during design (many real
  forms use `value="1"`/`"2"`/... for radio/checkbox groups) found
  `normalize_signature("choice-topping-1")` and
  `normalize_signature("choice-topping-2")` both collapsing to
  `"choice-topping"` — `_norm_token`'s generic trailing-digit-stripping
  fallback doesn't check the `known_prefixes` exclusion list at all, so
  a signature that correctly dodges the `"select-"`-collapsing bug (see
  the section above) fell into the *next* line of the same function and
  got collapsed anyway, just by a different mechanism. Fixed with an
  early return: any `"choice-"`-prefixed signature now skips
  generalization entirely, on the same reasoning the prefix was chosen
  for in the first place — these signatures exist specifically to stay
  maximally distinct per option, never to be generalized like an
  `"item-42"`-style per-instance id.
- **`_read_choice_state()`'s label lookup only checked
  `label[for=id]`**, missing the "input wrapped inside `<label>`,
  no `id`/`for` at all" pattern that `_DISCOVER_JS`'s own
  `inputLabelText()` already handled — caught by asserting the actual
  observed label text (`"Small"`) against what came back (the raw
  `value` attribute, `"small"`) on httpbin's real markup, which uses
  exactly that wrapping pattern. Fixed by adding the same
  `closest('label')` fallback `inputLabelText()` already uses.

**Verified end-to-end on real markup** (httpbin.org itself returned 503
during verification — reproduced its actual, real `forms/post` template
byte-for-byte on a local fixture server rather than skip verification or
reason abstractly about it):
- All 8 real controls discovered (3 radios + 4 checkboxes + Submit) from
  a page that previously surfaced 1; all `is_choice=True` for the
  radios/checkboxes, all with distinct `norm_signature`s
  (`choice-size-small`/`-medium`/`-large`,
  `choice-topping-bacon`/`-cheese`/`-onion`/`-mushroom`).
- Clicking "Small" then "Bacon" then Submit: `choice_state` correctly
  read back `{"size": "Small", "topping": "Bacon"}`; merged label reads
  `Fill form and submit "Submit order" (custname=..., ..., size="Small",
  topping="Bacon")`; `form_fields` on that same transition stayed
  `["custname", "custtel", "custemail", "comments"]` — confirmed the
  radio/checkbox state never leaked into what M4 would turn into
  `.fill()` calls.
- Regression check on saucedemo (no radio/checkbox on that site at all):
  full login → menu → inventory crawl completed with zero checkpoints,
  identical shape to before this change — the new code paths are
  additive, not a rewrite of the existing click/select/fill logic.

**Known residual gaps, not solved by this:**
- ARIA `role="radiogroup"`/`role="tablist"` patterns that don't use real
  `<input type="radio">` markup still aren't detected — same gap
  `<select>` already had, unchanged by this work.
- Choice-group clustering for CDP-detected div-as-*checkbox* patterns
  (a custom-styled checkbox built from a clickable `<div>`, not a real
  `<input type="checkbox">`) isn't covered — `_detect_choice_groups()`
  clusters *mutually exclusive* sibling groups (radio/select-shaped);
  nothing currently promotes a div-as-checkbox to `is_choice` the way a
  real `<input type="checkbox">` now does.
- Item 4 from the original framing — matching TCMS test cases that
  assert about parameter *combinations* or actual submitted values
  against found flows — is explicitly deferred, not started here.

## M3 — Operator UI (done)

- **`flowscout serve`** (or `python -m flowscout.web`) starts a local
  FastAPI server (`flowscout/web/app.py`) on `127.0.0.1:8787` serving a
  single static vanilla-JS frontend (`flowscout/web/static/index.html`)
  — no Node/React toolchain, deliberately: the UI surface (a config
  form, a run list, an embedded report) doesn't need component
  frameworks, and it keeps the project's dependency footprint to
  Playwright + FastAPI. The backend is a plain REST API, so swapping in
  a real SPA later wouldn't touch it.
- **Run lifecycle** (`flowscout/web/runs.py`): `crawl()` is synchronous
  (sync Playwright), so each run executes in a background thread, not
  on FastAPI's event loop — otherwise one crawl would freeze every other
  request. No job queue/database; state lives in memory while running
  and on disk (`runs/<run_id>/`) once written, so the run list survives
  a server restart by re-scanning the directory.
- **Endpoints**: `POST /api/runs` (start), `GET /api/runs` (list,
  merges in-flight + on-disk), `GET /api/runs/{id}` (poll status),
  `GET /api/runs/{id}/report` (serves the same `report.html` the CLI
  produces), `POST /api/runs/{id}/gap` (multipart TCMS CSV upload →
  re-renders the report with the gap-analysis section), `GET
  /api/configs` (lists `configs/*.json` so the form can prefill from a
  saved config instead of everyone typing limits/domains from scratch).
- **Verified end-to-end**, not just imported: started the server,
  launched a real crawl via `POST /api/runs`, polled to completion,
  fetched the report, uploaded a TCMS CSV and got back an updated
  gap-analysis section — then loaded the page in an actual headless
  browser and confirmed zero console errors, not just that the JSON
  endpoints respond.

### Config management UX (found by user review, Aug 2026)

- **Bug: selecting "— blank —" after a saved config left the old values
  in place.** `<select>`'s change handler only acted when the chosen
  option carried a config object; blank has none, so the handler did
  nothing and the form just looked stuck. Fixed with an explicit
  `resetForm()` on the no-match branch — confirmed via a real browser
  session (select `Site B` → fields fill → select blank → fields actually
  clear).
- **No way to save or delete a config from the UI** — could only load
  ones that existed as files already. Added `PUT`/`DELETE
  /api/configs/{name}` and a "Save current settings as…" button + 🗑 next
  to the dropdown. Config names are sanitized by stripping to
  alnum/-/_ rather than validated/rejected — neutralizes path traversal
  by construction instead of trying to enumerate bad input.
- **No cap on saved configs** — `/api/configs` just globs the directory;
  a long list is a "add search/filter to the dropdown" problem to solve
  if it ever actually comes up, not pre-solved here.
- **Help modal added** — a `?` next to "New run" opens an overlay
  explaining the safe/mutating/destructive risk model and what every
  field actually does, including specific, true claims (e.g. "the 0.95
  threshold was set by testing real flow pairs, not picked blind") over
  generic descriptions.

**Scoping decision (Aug 2026): local tool, not hosted multi-tenant.**
The UI is a local web app (FastAPI + React) that runs on localhost and
reads/writes the same `.env.local` and JSON configs already in use —
no auth, no hosted secret storage, no per-user key vault. This is a
real fork that had to be settled before design, not after: a hosted
multi-tenant version needs user accounts, encrypted secret storage, and
a "who pays for whose Gemini calls" billing story, none of which apply
here. Revisit only if FlowScout needs to be handed to people who won't
run a local process.

Held off building UI until now deliberately — the config surface kept
changing shape through M0–M2 (`allow_mutating`, `allowed_domains`,
`semantic_dedup.threshold`, `--tcms`, `exclude_patterns` all landed
*after* M3 was first sketched); building forms around a config schema
that was still moving would have meant redoing them repeatedly.

- FastAPI + React. Graph visualization (Cytoscape). Checkpoint queue.
  Run configuration and control (the exclude-patterns / limits / TCMS
  fields already exist in config — this is presentation, not new
  capability).

## M3.5 — Persistent flow identity + project state (done)

**Implemented (Aug 2026).** `flowscout/identity.py` (anchor + content
hash, see below), `flowscout/project_state.py` (the store:
`projects/<slug>/state.json`, tracked in git like `configs/` — unlike
`runs/`, these are durable operator decisions, not disposable
snapshots). Wired in three places: `flowscout crawl` and the web UI's
run executor both call `project_state.record_run()` automatically after
every crawl (pure bookkeeping, no operator action needed); a new
`flowscout confirm --project P --identity ID --tcms-id TC-05
[--approve]` CLI command plus matching `POST /api/projects/{p}/confirm`
and `/approve` endpoints let an operator make a pairing durable.

**First real payoff, not just plumbing:** `gap_analysis.py` now checks
project state before spending an embedding call. A confirmed pairing is
treated as ground truth (score 1.0, `confirmed: true`) and pulled out of
the fuzzy-matching pool entirely, on both sides — it can't be
re-guessed, can't be stolen by a stronger fuzzy match to something else
on a later run, and costs nothing. The report's gap section shows each
undocumented flow's identity plus the exact `flowscout confirm` command
to run once you know where it belongs, and marks each covered TCMS row
"confirmed" vs "inferred" so the two are never presented as equally
certain.

**Verified end-to-end on a real crawl**, not just unit-level: fresh
saucedemo crawl → `projects/.../state.json` populated (9 unique flows
collapsed to 6 identities, exactly the "same accomplishment, different
DOM" collapsing the anchor is designed to do) → confirmed one flow to
TC-02 via CLI → re-ran gap analysis → confirmed pair showed
`score: 1.0, confirmed: true` on both the flow and TCMS side, status
line read "9 flows vs 10 TCMS items, 1 already confirmed; 8 flows vs 9
tests compared" (i.e. the confirmed pair was genuinely excluded from
the embedding pool, not just relabeled after the fact) → same checked
through the web API (`POST .../confirm`, `.../approve`, `GET
.../state`) with no regression to existing endpoints.

**Not built yet, deliberately out of M3.5 scope:** UI buttons in the
report itself to confirm a link by clicking (report is static HTML, no
JS currently; this needs either adding JS to the report or a
confirm-from-the-operator-UI flow) — CLI/API access is sufficient for
now, and M4/M5 will clarify what the operator's actual moment-of-use
looks like before that surface gets built.

### The identity anchor problem (recap — see identity.py for the live version)

Settled in design discussion (Aug 2026). Two problems raised separately
turn out to be one feature: durable operator approvals (needed by M4)
and durable flow↔TCMS links (needed by M5) both require the same thing —
a **stable flow identity that survives a re-crawl**, plus a project-level
store to hang decisions off. Everything FlowScout persists today is
per-run (`runs/<id>/flows.json`); no operator decision outlives a run.

### The identity anchor problem

Identity has to be *coarser* than flow content — otherwise a changed
flow reads as a brand-new flow rather than a changed one, and change
detection is impossible by construction.

`end_state_fp` is the obvious candidate and **it does not work**.
Measured on the existing saucedemo-wide run: `cart.html` alone produced
**8 distinct fingerprints within a single crawl**, `inventory.html`
another 8. The fingerprint is `hash(url_pattern + element signatures)`,
so "cart with 1 item" and "cart with 2 items" are different states — by
design, and correctly so for within-run dedup. As a cross-run anchor it
would fire "flow changed!" every time anyone adds a footer link.

**Chosen anchor: `(url_pattern, frozenset of mutating action
signatures)`** — what the flow *accomplishes*, not what the DOM looked
like. Reuses `_mutating_signature_set` from `semantic_dedup.py` (written
for M1's false-merge guard; second use, same concept). Validated against
the real saucedemo-wide flows, where it decomposed the checkout chain
into exactly the three milestones a human would name:

```
/checkout-step-one.html   + {checkout}
/checkout-step-two.html   + {checkout, continue}
/checkout-complete.html   + {checkout, continue, finish}
```

It also independently reproduces the "gap ≠ needs a test" filter for
free: the two junk gap flows (`Login > Open Menu > All Items` and
`… > Close Menu`) collapse to the same identity `(/inventory.html, {})`
— an empty mutating set *is* the "this is a shared step, not a test
case" signal, no separate heuristic needed.

### Known imprecision in the anchor (found by running it, not by design)

- **Too coarse in one place:** flows adding one item vs two items to the
  cart collapse to the same identity, because `add-to-cart-*`
  normalization erases which/how many products.
- **Too sensitive in another:** the same user-facing action gets
  different signatures depending on where it was performed —
  saucedemo's product-detail button is `data-test="add-to-cart"` while
  listing buttons are `add-to-cart-sauce-labs-backpack` →
  `add-to-cart-*`. A refactor that unified those attributes would be
  reported as a flow change when nothing behavioral changed.

Both acceptable for v1, documented so they aren't rediscovered later.

### Store contents

`<project>/state.json` (or SQLite if it outgrows a file): flow identity →
`{ tcms_id, confirmed_at, approved_for_codegen, last_seen_run,
last_seen_content_hash }`. Content hash is the full normalized action
sequence — the thing that's allowed to change under a stable identity.

## M4 — Test-case codegen (done)

**Design decision (Aug 2026): FlowScout never invents expected results.**
It observed what the app *does*; it knows nothing about what the app
*should* do. Deriving an "expected result" from observed behavior is
circular — any bug present during the crawl gets frozen in as the
assertion, producing a test that passes forever on the bug and can
never reveal it. The operator writes behavioral expectations, for
uncovered flows and partially-covered ones alike.

But there are two distinct things called "expected result", and only
one of them is off-limits:

- **Behavioral** ("the order total is $X", "an invalid password shows
  an error") — genuinely unknowable from a crawl. Left as an explicit
  empty block for the human.
- **Structural / reachability** ("after these 6 steps the app is on
  `checkout-complete.html` with these controls present") — actually
  observed, deterministically, and reproducible across runs (that's what
  the isolated-context replay and state fingerprints already guarantee).
  Asserting this is recording a fact, not fabricating an expectation.
  Emit these, clearly labeled as smoke/reachability checks: they make a
  generated spec useful as a regression test on day one (catches "the
  checkout button is gone", "this path now 404s") without pretending to
  verify business logic.

### Scope

1. **Flow → test-case draft** (Markdown, plus TCMS-importable CSV):
   human-readable steps (the report's existing `action_label` text
   already reads correctly), observed end state, provenance (run id +
   stable flow identity), and an empty `Expected result:` per step.
2. **Flow → Playwright spec**: steps compiled from the existing
   `replay_meta` locators, structural assertions, and a marked TODO
   block for the operator's real assertions. Credentials must come from
   env vars at runtime — never inlined into a committed spec file.
3. **Selection / approval** — which flows get exported.
4. **Default filter**: gap flows only (covered flows already have a
   test; regenerating one is noise), operator-overridable.

### Problems to solve before writing code

- **Gap analysis is flow-level, not step-level.** There is currently no
  mechanism for "steps 1–3 are covered by TC-05, step 4 isn't" — a
  single cosine compares a whole flow to a whole TCMS item. Building
  step-level matching naively is a known trap: M2 already demonstrated
  that matching machine transcripts against human prose fails badly
  ("Login with valid credentials" won top match for 9 of 11 flows before
  the representation was fixed). Honest v1: flow-level status, plus the
  matched TCMS item's text shown alongside the steps so a human can
  eyeball the difference — not an automated step diff.
- **Flow IDs are not stable across runs.** `next_flow_id` restarts at 1
  every crawl, so flow #12 in one run is unrelated to #12 in the next.
  Any persisted operator approval must key off something that survives a
  re-crawl — normalized action sequence + `end_state_fp` are both
  stable by construction; the sequential ID is not.
- **A "gap" is not automatically worth testing — and the junk is
  actually shared steps.** Concrete evidence from the saucedemo-wide
  run: gap analysis flagged exactly 2 uncovered flows, and both were
  trivial no-ops (`Login > Open Menu > All Items` and `Login > Open Menu
  > Close Menu`). Blind codegen over gap flows would emit junk. The
  right framing (user's, better than the "significance heuristic" first
  proposed here): these aren't noise to discard, they're **shared-step
  candidates** — the TestRail sense, where a common prologue is authored
  once and referenced from every case rather than repeated. Implementable
  from data we already have, no heuristic needed: frequency-count
  `action_norm_signature` prefixes across all unique flows (`Login`
  appears in 100% of saucedemo flows, `Open Menu` in nearly all → those
  are preconditions by definition), emit the common prefix once as a
  shared step, and subtract it — what remains is the flow's actual
  unique contribution. If the remainder is empty or pure navigation,
  it's not a test case. Note this falls out of the M3.5 identity anchor
  for free: an empty mutating-action set is the same signal.
- **Locator fragility varies by site, and codegen should say so.**
  saucedemo has `data-test` everywhere → sturdy generated specs.
  Site B frequently has neither `data-test` nor `id`, so
  `build_locator` falls back to text matching — on a *trilingual* site,
  where a locale switch breaks the locator outright. Steps resting on a
  text fallback should be flagged as fragile in the generated output
  rather than shipped silently.

### Implemented (Aug 2026)

All three scope items shipped as designed, plus the shared-step framing
from "Problems to solve" above (implemented as written, not the
significance-heuristic alternative first floated).

- **`flowscout/shared_steps.py`** — `common_prefix_length()` +
  `split_flows()`: frequency-counts the longest common
  `action_norm_signature` prefix across all candidate flows, subtracts
  it, and marks a flow `test_worthy` only if its remainder still has a
  non-empty mutating-action set (the M3.5 identity anchor's own signal,
  reused for free). Validated on the saucedemo-wide run: 11 unique flows
  share a 2-step prologue (Login, Open Menu) → 7 test-worthy, 4 filtered
  as shared-step-only junk — matching the "Login > Open Menu > Close
  Menu" no-ops called out above.
- **`flowscout/testcase_draft.py`** — Markdown (steps + empty `Expected
  result:` per step, provenance = run id + flow identity) and a
  TCMS-importable CSV using tcms.py's own id/title/steps schema, so a
  draft can round-trip back through gap analysis once filled in.
- **`flowscout/playwright_codegen.py`** — pytest-playwright specs.
  Locators rebuilt as source (mirroring `actions.build_locator`'s own
  priority: `data-test` > `id` > `href` > text), text fallback flagged
  fragile in a code comment, not just in an operator-facing report.
  Structural assertions (end-state URL pattern) emitted directly;
  behavioral assertions left as a single labeled TODO block per the
  design decision above. Credentials read from `os.environ` under a
  `FLOWSCOUT_<FIELD_NAME>` convention (see the `Transition.form_fields`
  model change below) with an unmissable `TODO_SET_...` fallback —
  never inlined into a committed spec.
- **`flowscout/codegen.py` + `flowscout codegen` CLI** —
  `select_candidate_flows()` defaults to gap flows when `--tcms` is
  given (nothing to diff against otherwise → all unique flows),
  `--approved-only` further filters through `project_state`'s
  `approved_for_codegen` flag from M3.5. `generate()` writes
  `drafts.md` + `drafts.csv` + one combined `test_flowscout_drafts.py`
  (a shared `PYTEST_IMPORTS` header, one test function per flow).

**Model change required to make credentials recoverable at codegen
time.** `action_label` masks password values for display and never
stored the synthetic values it typed either — by the time codegen runs,
there's nothing to read. Rather than trying to parse a masked display
string back apart, added `Transition.form_fields: list[str]` (field
*names* only, populated at crawl time right next to `action_label`) so
generated `.fill()` calls know which env var to ask for without ever
having seen a real credential.

**Bug found and fixed via `ast.parse()` validation, not assumed
correct.** First version of `_step_code()` returned lines already
indented 4 spaces, and `render_pytest()` indented the whole body again
on top → `SyntaxError: unexpected indent` on every generated file.
Fixed by making step lines unindented and applying indentation exactly
once, uniformly, in `render_pytest()`. Caught by actually parsing every
generated source with `ast.parse()` rather than eyeballing it.

**Verified end-to-end against the live site, not just parsed.** Ran
`flowscout codegen` through the real CLI entrypoint both with and
without `--tcms` against a fresh saucedemo-wide crawl (`--tcms`
correctly produced 0 test-worthy flows, since both of that run's 2 gap
flows were shared-step-only junk — the filter working as intended, not
a bug). Copied the resulting combined `test_flowscout_drafts.py` (7
functions, one shared import header) and ran it for real:
`FLOWSCOUT_USER_NAME=... FLOWSCOUT_PASSWORD=... python -m pytest
test_flowscout_drafts.py -v` against `https://www.saucedemo.com/` →
**7 passed**, credentials pulled from env, generated locators and
structural assertions all correct together, not just individually
unit-tested.

**Known imprecision, reconfirmed during this testing.** The M3.5
identity anchor is deliberately coarser than full flow content (see
M3.5 below) — two of the drafted flows here (`TC-DRAFT-9` and
`TC-DRAFT-10`, "add 1 item to cart" vs "add 2 items to cart") collapsed
onto the identical identity `bebdd173aaa6964f` despite being genuinely
different flows a human would want as separate test cases. Not a new
bug — the same known trade-off M3.5 already documents, surfacing again
under a new consumer (codegen) of that identity.

## M5 — Longitudinal change detection (done)

**Implemented (Aug 2026).** `flowscout/change_detection.py`: `detect_changes(run)`,
called before `project_state.record_run()` overwrites what it needs to
compare against. Classifies every identity as new / changed / missing
relative to the project's prior state; unchanged ones aren't reported
(nothing for the operator to act on). Wired into both `flowscout crawl`
and the web UI's run executor -- runs automatically on every crawl now
that project state is tracked, no flag needed. Report gets a "Change
detection" section placed right after the summary metrics, before
Flows, since "what changed" is usually the first thing worth knowing on
a re-crawl; a `flowscout gap` re-run preserves an existing
`change_report.json` instead of silently dropping the section when it
only regenerates the gap analysis.

**"Missing" language was calibrated against real measured
non-determinism, not assumed.** Validated crawl determinism directly
before writing the wording: two consecutive saucedemo crawls produced
bit-for-bit identical identities and content hashes; Site B, on the
same project across this whole build, produced different state/flow
counts on three separate re-crawls of an unchanged target (network
timing, breadth-budget competition from repeated header nav). So
"missing" is worded as a fact ("not found this run"), never "broken" --
same principle as TCMS `not_found` and as never inventing expected
results. Confirmed-linked flows get more urgent framing than
unconfirmed ones in the report, since those are the ones an operator
actually has a stake in.

**Verified end-to-end with a real, not staged, negative result along
the way.** First attempt at proving a "missing" signal used
`exclude_patterns` to block the checkout flow between two crawls -- and
it silently didn't work. Root cause, confirmed by inspecting the live
DOM: saucedemo's "Checkout" is a `<button>` with no `href` at all
(client-side routing), and `exclude_patterns` matching is entirely
href-based (`risk.classify()` only evaluates the pattern inside `if
href:`). This is a real, separate limitation, documented below, not
silently patched over -- there's no URL to pattern-match against a
button that hasn't been clicked yet. Re-ran the test using a genuine
budget constraint (`max_breadth_per_state` lowered enough that checkout
fell out of reach) instead, and got a real 3-missing / 1-missing-
confirmed result, correctly labeled in both `change_report.json` and
the rendered report.

**Known limitation found in the process, not yet fixed** -- see its own
entry below ("Parked -- exclude_patterns is href-only").

**Not built:** the ROADMAP's original "link cardinality" question
(TCMS case -> many flows) is moot for change detection itself, since
diffing happens per flow identity regardless of link cardinality; it
still applies to M4 codegen's flow selection, unresolved there.

The idea that turns FlowScout from a one-shot audit into something worth
wiring into CI (user's, Aug 2026). Built directly on M3.5's persistent
store: once an operator confirms "this flow corresponds to TC-05", that
judgment — the expensive part — is reused on every subsequent run
instead of re-derived. Embeddings drop from *decider* to *suggester*:
after confirmation the link is exact ground truth, and no fuzzy
comparison is needed for that pair again.

Signals to raise on a re-crawl, against a linked flow:

- **Anchor gone** — the flow's `(url_pattern, mutating set)` is no
  longer reachable at all. Either the feature was removed (the linked
  test case is stale) or it broke (a bug). FlowScout deliberately does
  not guess which; that's the operator's call, same principle as never
  inventing expected results.
- **Path changed** — anchor still reachable, but the action sequence to
  get there differs from `last_seen_content_hash`. The linked test
  case's steps may now be wrong.
- **Unchanged** — nothing reported.

Open questions before building:

- **Link cardinality.** A TCMS case may legitimately cover several
  flows; a flow maps to at most one case. Start one-to-many, resist
  building a general many-to-many link table for v1.
- **Environment mismatch.** A project state file built against staging
  and then applied to a prod crawl would produce meaningless diffs.
  Needs at minimum a recorded `start_url`/environment fingerprint in the
  store, and a loud mismatch warning.
- **Baseline semantics.** Run 1 establishes a baseline rather than
  reporting changes; the report needs to say which mode it's in, so a
  first run doesn't read as "nothing changed, all good."

## Multi-persona crawling (done, Aug 2026)

**The problem, from a user question, not a self-generated one:** wiring
FlowScout into CI only works if a crawl can actually see everything a
real test suite would need to check — and a real app has flows
restricted to specific roles (an admin dashboard, an owner-only delete
button) that a single set of credentials can never reach. Manual runs
sidestepped this by just re-running with different credentials by hand;
CI can't sidestep it, since a pipeline needs one command with one exit
code, not an operator swapping logins between runs.

**Design decision: sequential, not parallel — deliberately, and for a
reason specific to this tool, not a generic "sequential is simpler"
default.** Personas can corrupt each other's results through shared
*server-side* state: one persona's "Reset App State" (or any action
with a real backend side effect) mid-crawl would silently invalidate
whatever another persona was mid-flow doing against the same test
server at that moment. This is the exact class of non-determinism M5
already had to document and design around for Site B (three
back-to-back re-crawls of an unchanged target produced different
results) — multi-persona parallelism would introduce a *new*, harder-
to-diagnose source of it, this time from the tool's own concurrency
rather than the target's own timing. Not built as an option at all,
not even opt-in, until a real target actually demonstrates it needs
the speed badly enough to accept that risk.

**`persona` added as a first-class dimension everywhere flow identity
and dedup are decided — not layered on top as a separate concept.**
Same principle the M1 state-convergence fix and the is_choice/
configuration-selection fix both already established: extend the
*key*, don't touch the thing being keyed.
- `Flow.persona: str = "default"` — new field, defaults to `"default"`
  for every run that doesn't use personas, so old code and old saved
  `flows.json` files keep working unchanged (`RunResult.from_json`
  needs no special-casing: a missing key just hits the dataclass
  default).
- `identity.py`'s `flow_identity()` folds persona into the hashed
  string. Two personas reaching an identical-looking destination having
  performed identical actions are, by design, NOT the same flow — which
  persona was *allowed* to get there is exactly the thing under test.
- `semantic_dedup.py`'s tier-1 state-convergence key gained `persona` as
  a third component (alongside `end_state_fp` and `mutating_signature_set`,
  same shape as the M1 fix); tier-2's false-merge guard gained a persona-
  equality check alongside its existing mutating-set check.
- `crawler.py`: structural dedup's `seq_to_flow_id` map is now fresh per
  persona (not shared across the whole run) -- persona B's first
  occurrence of a sequence persona A already walked must never be
  reported as "duplicate of persona A's flow".

**Config shape, backward compatible by construction.** `"personas":
[{"name": ..., "credentials": {...}}, ...]` runs each set sequentially
into one `RunResult` — one report, one change-report, one CI exit code.
A config with no `"personas"` key falls back to the original single
`"credentials"` dict as one persona named `"default"` — every config
written before this feature existed, and the CLI/API payloads that
build them, needed zero changes.

**Per-persona budgets, not a shared pool.** `max_states`/`max_flows`
apply fresh to each persona's own pass (counted from a snapshot taken
when that persona's pass starts), not to the run's grand total — so a
later persona in the list can't be silently starved of budget by
whatever an earlier persona happened to explore first. The *state
graph* itself stays shared and reused across personas, deliberately:
the very first state (the pre-login landing page, reached by an empty
action path that calls no credentials at all) is providably identical
regardless of persona and is only ever discovered once; any later state
two personas happen to reach with a truly identical candidate set is
legitimately the same state in the graph, and content differences
(an admin-only "Delete" button making the candidate set differ) already
produce a different fingerprint automatically, with no persona tag
needed on `StateNode` itself.

**Web UI**: the existing "Credentials" section is unchanged (the
implicit "default" persona); a new, optional "Additional personas"
section lets an operator add more named credential sets, collected into
a `personas` array only if at least one exists (so the common single-
login case still POSTs the exact same payload shape it always did — no
`personas` key sent for nothing). Config save/load round-trips a
`personas` array's first entry back into the base Credentials section
so a saved multi-persona config reloads correctly.

**Verified end-to-end against live saucedemo, not just unit-level.**
Ran real `standard_user` + `locked_out_user` accounts (the second is
saucedemo's own built-in "this account can't log in" persona) through
the actual DFS, both via the CLI and via a genuine `POST /api/runs`
HTTP call against the running server (not just calling `crawl()`
directly) — 0 checkpoints either way. Results matched the motivating
scenario exactly: `standard` produced 16 unique flows across the whole
app; `locked_out` produced 3, one explicitly showing it hit the
account's error banner and got no further — a real, visible "flows
restricted to a specific area of the app for one persona" case, not a
hypothetical. Structural dedup confirmed correctly scoped per persona:
both personas independently produced a flow whose entire action
sequence is just `['login-button']` (all either can do, trivially, is
click Login), and both survived as separate `unique` flows rather than
one being marked a duplicate of the other. A synthetic isolation test
on `flow_identity()` alone (identical everything else, persona A vs. B)
confirmed distinct identities, and identical persona vs. itself
confirmed identical, stable identities.

**One real, unavoidable, one-time consequence — found by running a
plain single-persona regression crawl after the change, not assumed
away:** `flow_identity()`'s hash input changed shape (a `persona +
"|"` prefix was added), which changes *every* flow's identity hash —
including flows whose persona is `"default"` and whose real content
never changed at all, since the string being hashed literally differs
now. Confirmed on saucedemo-wide's own project state: a same-day re-
crawl of an otherwise-identical target reported "26 new, 36 missing"
purely from this formula change, not from any real application change.
This is a one-time reset that happens to *every* existing project's
`project_state.json` the first time it's crawled after upgrading to
this feature — expected, unavoidable given the design (the alternative
would have meant NOT keying identity on persona, defeating the whole
point), and worth knowing about in advance rather than looking like a
real regression the first time an operator sees it.

**Known scope boundaries, not solved here:**
- `gap_analysis.py`'s capability pool (the action-level TCMS matching
  from the earlier gap-analysis rewrite) stays persona-agnostic --
  two personas performing an action with the same
  `action_norm_signature` still share one pooled entry/embedding. A
  real "admin can complete checkout" vs. "guest can complete checkout"
  distinction isn't separately tracked for TCMS-coverage purposes,
  only for flow identity/dedup. Splitting the capability pool by
  persona is a real, larger follow-up (more embedding calls, a schema
  change to `FlowCoverage`), deliberately out of scope here.
- M4 codegen's generated `.fill()` calls read credentials from a single
  `FLOWSCOUT_<FIELD_NAME>` env var convention, with no persona in the
  name -- a generated test for a `locked_out` flow and one for a
  `standard` flow reference the exact same env var, so running both
  against the same environment variables would silently use whichever
  persona's credentials happen to be set. A per-persona env var prefix
  (`FLOWSCOUT_STANDARD_USERNAME` vs. `FLOWSCOUT_ADMIN_USERNAME`) is the
  obvious fix, not built.

## Depth-truncation was invisible (done, Aug 2026)

**The question that found this: a user asked how they'd know a flow got
cut short by budget rather than being genuinely complete, and how
they'd know what budget they even need.** Looking for the answer in the
code surfaced a real gap rather than an existing feature: it turned out
there wasn't one. `max_states` truncation already gets a distinct
`BLOCKED` status and a `"Truncated: max_states limit reached..."`
reason (built for M0). `max_depth` truncation did not — it shared one
code branch with genuine natural completion (a state where every
candidate had already been tried), producing the exact same status and
the exact same generic `"New normalized action sequence"` reason either
way. There was no way to tell, from the report or the data, "this flow
is finished" from "this flow was cut short and might have kept going."

**Confirmed on a real run before fixing, not assumed.** The very flow
this session had already been using as a worked example --
`add-to-cart` never chaining into `checkout` at `max_depth=8` -- turned
out to be exactly this: the state reached (cart with an item in it) had
`Checkout`/`Remove`/`Continue Shopping`/`Reset App State` all sitting
right there as real, valid candidates, discovered and risk-classified,
simply never clicked because the path that reached that state first had
already used its entire depth budget getting there. The resulting flow
was previously indistinguishable, in the data, from a flow that
legitimately had nowhere left to go.

**Fix**: split the compound condition
(`frame.pos >= len(frame.order) or len(frame.path) >= max_depth`) into
its two real cases. `frame.pos` exhausted -> genuine completion,
unchanged. `max_depth` hit *while candidates remained untried* -> now
gets the same treatment `max_states` already had: forced `BLOCKED`
status, an explicit `"Truncated: max_depth limit reached with N further
action(s) available from here, never tried"` reason, and every one of
those untried candidates recorded into `skipped_candidates` (state,
label, risk) -- the same audit trail the report's Safety register
already shows for breadth-limit and max_states truncation, extended to
cover the one budget that hadn't been reporting itself at all.

**Verified end-to-end, not just the one already-known case.** Re-ran
the same saucedemo config live: 5 flows now correctly flagged
`Truncated: max_depth`, 45 specific skipped-candidate entries recorded
-- including a *second*, previously-unnoticed case one step away from
`Finish` (the last step of checkout), not just the add-to-cart one this
session had already been discussing. Confirmed the report renders this
plainly (the reason text shows directly on the flow card, which --
since `BLOCKED` flows are shown in the same lead list as `UNIQUE` ones,
not tucked into the collapsed duplicates section -- means truncated
flows are visible by default, not something an operator has to go
looking for) and that the full downstream pipeline (gap analysis, M4
codegen) runs cleanly with these flows correctly excluded from the
"unique, test-worthy" pool, the same treatment `max_states`-truncated
flows already got.

**This is also the honest answer to "what budget do I need":** there
isn't a formula, and this doesn't try to invent one. What it does
instead is turn "guess the right budget up front" into "run once at a
reasonable budget, read the report, and it says exactly where more
budget would help and by how much" -- the report now states the fact
(N flows truncated, here's exactly what was left untried and where)
rather than requiring an operator to notice an absence.

**A sibling bug in `max_flows`, found immediately after -- while
actually measuring whether raising `max_depth` helps, not by code
review.** A user asked why not just raise the depth budget a lot, since
the untried-candidate report can't say how much *further* an unexplored
branch might go. Measured live on saucedemo across `max_depth` 8/14/
20/30 (same `max_flows`/`max_states` throughout): 8 -> 14 fixed the
depth-truncation cases for real (62 -> 103 unique flows, longest flow
walked grew 8 -> 11 steps); 14 -> 20 -> 30 changed *nothing* -- all
three produced byte-identical unique-flow counts, all three hit exactly
`max_flows` (150) flows before depth ever became the limiting factor
again. Confirms depth-truncation's own fix worked (raising the budget
that's actually binding helps; raising past it costs nothing, since
`_run_path` never gets called for candidates that were never reached).
But it also meant `max_flows` had quietly become the *real* limiting
budget the whole time, and a direct test (`max_flows=12`) confirmed
this budget's truncation was **entirely silent** -- unlike `max_depth`,
which now reports itself, and unlike `max_states`, which already did:
the DFS's `while stack: if ... >= max_flows: break` just abandoned
every remaining stack frame with zero record anywhere. No blocked flow
(there's no single flow to blame it on -- the abandoned frames are
queued states, not paths anyone walked), no skipped-candidate entry, no
checkpoint. A run stopped by `max_flows` looked byte-for-byte identical
to one that finished because there was nothing left to explore.

**Fix**: a `Checkpoint(kind="blocked")` at the moment `max_flows` cuts a
persona's pass short, naming exactly how much was abandoned --
`"9 state(s) were still queued for exploration, with 56 candidate
action(s) never tried"` -- plus an explicit note that the flows already
reported are a prefix, not the complete picture. A checkpoint rather
than a per-flow reason (the `max_depth`/`max_states` pattern) because
there's no single flow this belongs to; it's every frame still on the
stack at the moment the whole persona's pass got cut off. Verified live
against the same `max_flows=12` case that exposed it.

**Practical read for budget-tuning, now that both are honest:** raise
`max_depth` freely -- confirmed near-zero cost once it stops being the
binding constraint, and the report says plainly when it still is.
`max_flows` is the real dial that costs wall-clock time and needs
deliberate tuning; it's also, unlike depth, now impossible to exhaust
silently.

## TCMS in the web UI: discoverability + attach-at-creation (done, Aug 2026)

**The question that found this: a user looked for where to upload test
cases in the web UI and couldn't find it.** It existed -- but only
after selecting an already-*completed* run, as a bare file input +
button sitting directly above the report iframe with no heading, no
label, nothing signposting it. The "New run" config form (project,
URL, credentials, personas, limits) had no TCMS field at all, and
`POST /api/runs` didn't even accept a file (`config: dict`, plain JSON
body). The CLI's `flowscout crawl --tcms` (attach at crawl time) had
simply never been carried over to the web UI, which only ever had the
`flowscout gap` (separate, after-the-fact) shape.

**Two fixes, done in the order asked for a reason.**

1. **Discoverability first.** `renderGapUploadUI` gained a heading and
   an explanatory hint (what a TCMS export is, what uploading one
   does), and now reads the run's own `has_gap_analysis` flag to say
   "Compare against your test plan" vs. "Gap analysis vs TCMS" (already
   has one -- re-run to replace it) rather than the same static button
   label regardless of state.

2. **Attach a TCMS at crawl-creation time -- explicitly framed by the
   user as the step toward CI, not just a convenience.** A CI pipeline
   needs one request with one response it can check, not "start a
   crawl, poll for it, then make a second call and poll for that too."
   `POST /api/runs` now accepts either shape: the original plain JSON
   body (kept working completely unchanged -- verified with a live
   regression call, not just left alone and assumed fine, since nothing
   here should silently break the multi-persona work verified earlier
   the same way), or `multipart/form-data` with a `config` field (the
   same JSON, as a string), an optional `tcms` file, and an optional
   `gap_threshold`. `runs.py`'s `_execute()` now runs `analyze_gaps()`
   inline right after the crawl, before `report.html` is written, so a
   single response gives back a `run_id` whose report has flows *and*
   gap analysis together as soon as it's done -- nothing left for a
   human (or a second CI step) to do afterward.

**A malformed TCMS file must not lose an otherwise-successful crawl.**
Same "degrade, don't fail the whole thing" convention this project
applies everywhere else (semantic dedup, embeddings). `load_tcms_csv()`
raising on a genuinely bad file is caught specifically; the crawl still
completes, still writes its report, just without a gap section --
and `gap_error` (new field, surfaced through `GET /api/runs/{id}`) says
why, so a CI script polling status can tell "no TCMS was ever attached"
apart from "one was attached and couldn't be used" instead of both
looking identical.

**Verified end-to-end against the live server, all three paths:**
- Plain JSON, no TCMS: completed normally, `has_gap_analysis: false`,
  confirming the original contract is genuinely untouched.
- Multipart with a real TCMS file (`fixtures/tcms_saucedemo.csv`): one
  `POST /api/runs` call, and the finished run's report already had a
  populated Gap analysis section -- no second request made.
- Multipart with a deliberately garbage CSV: crawl still completed
  (`status: done`, real flows in the report), `has_gap_analysis: false`,
  `gap_error` explaining exactly why (`"Could not find a title/name/
  summary column..."`) -- the crash-avoidance path exercised for real,
  not just reasoned about.

**Known scope boundary, deliberate:** a saved config (`configs/*.json`,
the Load/Save feature) never carries a TCMS reference -- there's no
sensible way to persist a file inside a JSON config, and the CLI's own
`--tcms` is a per-invocation flag too, never part of the saved config
shape. Loading or resetting the form clears whatever TCMS file was
selected rather than trying to remember it.

## Parked — smart limits, not just numeric budgets

**The deeper question a numeric-budget report can't fully answer, from
the same conversation that found the two truncation bugs above.** If
raising `max_depth` is nearly free once the state graph stops growing,
why does the crawler need a depth limit at all -- why not let it run
until the graph is exhausted?

**Because the graph doesn't always stop growing, and depth is
currently the only thing standing between that and an unbounded
crawl.** `state_fingerprint()` already collapses the one case that
*looks* like it should be unbounded but isn't -- `normalize_url()`
turns `inventory-item.html?id=4` and `?id=5` into the same pattern, and
`normalize_signature()` does the same for the click that reaches them,
so measured on saucedemo-wide: 6 distinct products produce **zero**
extra states, only the 4 that come from real UI differences (menu open/
closed x cart empty/has-item). Confirmed by reading the actual
candidate lists, not assumed from the URL pattern matching.

But the same run also shows the case that doesn't collapse: `cart.html`
alone produced multiple distinct fingerprints, and inspecting *why*
shows the cart's own candidate list includes the item itself
(`"Sauce Labs Backpack"`, a quantity badge) -- state that's real,
correctly distinguished, and combinatorial. N addable items is up to
2^N reachable cart states, none of them a false collapse to fix, all of
them genuinely different application states. This is already
documented as identity.py's known imprecision from the M3.5 work, from
the identity side; this is the same fact from the crawl-budget side --
depth (and the other numeric limits) is currently the *only* thing
capping how far the crawler chases that combinatorial growth, which is
exactly why it can't simply be turned off.

**What "smart" would actually mean here, roughly in order of how cheap
each is and how much of the real problem it solves:**
- **A repeat-of-the-same-action-type cap.** The most direct fix for the
  actual combinatorial case: a third `add-to-cart` click teaches the
  crawler nothing a second one didn't already show, so capping repeats
  of the same `action_norm_signature` within one DFS path directly
  starves the 2^N cart-state growth at its source, rather than
  papering over it with a depth ceiling that has to be high enough to
  *tolerate* the blow-up before it can reach anything past it. **Done,
  see below.**
- **Infinite scroll.** Not handled at all today -- each scroll reveals
  more DOM, which is more candidate signatures, which is a new
  fingerprint every time, indefinitely. Same failure shape as the cart,
  with no natural ceiling at all (a cart tops out at "every item";
  a feed doesn't). **Investigated live, not building it as a "limit" --
  see "Superseded" below: the assumed risk doesn't apply, since the
  crawler doesn't scroll at all today.**
- **Pagination.** Page 2 of a product listing is structurally identical
  to page 1 -- same candidate *shape*, different specific items --
  which `normalize_signature()` already collapses for individual
  product links but does nothing for the pager control itself walking
  page 2, 3, 4, ... indefinitely. Partially, incidentally mitigated by
  the repeat-action cap below if the pager control itself has a stable
  signature (clicking "Next" repeatedly now gets capped the same way
  "Open Menu" does) -- not a real fix, since it still doesn't reach
  page 3 onward on purpose, just stops going further by accident.
  **Investigated live, not building it -- see "Superseded" below: the
  measured problem turned out to be the opposite of this framing.**
- **Reversible-pair collapsing.** Already visible in this project's own
  data: opening and closing the hamburger menu doubles states without
  adding information, the exact inefficiency that ate the depth budget
  in the add-to-cart-then-checkout case documented above. A generic
  "does this action's target state contain the same candidate set as
  where we came from, modulo this one toggle" check would catch this
  and similar UI-chrome pairs (expand/collapse, show/hide filters)
  without needing to special-case hamburger menus specifically. **Built,
  but not as originally framed here -- see below: measuring the real
  cost live before designing anything found that the label-based
  "toggle pair" framing above would have missed most of the actual
  waste, and the real fix ended up data-driven, not label-matched.**

Both infinite scroll and pagination have since been investigated live
(Aug 2026) -- see the "Superseded" section below. Neither is being built
as originally framed here; both conclusions came from measuring a real
site, not from reasoning about the *shape* of the problem in the
abstract.

## Repeat-of-the-same-action-type cap (done, Aug 2026)

**The first, cheapest item from the "smart limits" list above,
implemented on its own rather than the whole list at once** -- the other
three items are each a genuinely different detection problem (a scroll
pattern, a pager control, a reversible-pair heuristic), while this one
needed no new detection at all: just counting what the crawler already
tracks.

**New limit, `limits.max_action_repeat` (default 2, not a required key --
`limits.get(...)`, not `limits[...]`, so every config written before
this existed keeps loading unchanged).** In the main DFS loop, right
alongside the existing risk-policy gates (destructive / mutating-without-
opt-in), a candidate is now also withheld if its own
`action_norm_signature` already occurs `max_action_repeat` times or more
earlier in the *same path* (root to the current frame -- not per-state,
across the whole walk). Default 2: enough to see "one item in cart" and
"two items in cart" behavior, not enough to keep multiplying toward 2^N.
Withheld candidates are recorded in `skipped_candidates` with an explicit
reason, same as every other withholding reason in this project -- and if
withholding leaves a frame with nothing else to follow, it now correctly
emits a `BLOCKED` flow with "Dead end: remaining actions were withheld by
the action-repeat cap (max_action_repeat=N)", extended to combine with
the existing risk-policy dead-end message when both apply to the same
frame (`_Frame` gained a second flag, `any_repeat_skipped`, alongside the
existing `any_risk_skipped`) -- the same "name what actually happened,
don't let two different reasons collapse into one generic message"
discipline as the depth/max_flows truncation fixes above.

**Deliberately generic, not cart-specific.** The cap keys on
`action_norm_signature` alone, whatever it is -- no special-casing
"add-to-cart". `is_choice` actions (select/radio/checkbox) are
essentially unaffected in practice: their norm_signature is kept
maximally distinct per option specifically so it's never generalized
(see "Radio buttons and checkboxes as choice candidates" above), so the
same one only repeats if a path genuinely revisits the identical option
-- which this cap still correctly allows up to the limit before
withholding, rather than silently exempting choices from the cap
altogether.

**Verified live on saucedemo, two separate runs:**
- `max_action_repeat=2` (the default): the cap engaged for real, but on
  a control this project's own data had *already* flagged as a source of
  wasted budget -- the hamburger "Open Menu" button, clicked from several
  different states along different paths, correctly withheld on its 3rd
  repeat within a path. Zero checkpoints, zero errors; 14 states, 70
  flows walked, matching a normal saucedemo shape.
- `max_action_repeat=1` (deliberately tight, to force an actual dead
  end rather than just observe withholding): produced a real `BLOCKED`
  flow --
  `['Fill form and submit "Login"', 'Click "Open Menu"', 'Open "Sauce
  Labs Backpack"', 'Click "Add to cart"', 'Open "1"', 'Click "Continue
  Shopping"']` -- with reason `"Dead end: remaining actions were
  withheld by the action-repeat cap (max_action_repeat=1)"`. The cap
  also correctly triggered across other action shapes in the same run,
  not just the button case: `item-*` (the `known_prefixes`-generalized
  signature for "Open <product>" links) withheld a second "Open Sauce
  Labs Bike Light" within the same path, and `shopping-cart-link`
  withheld a second visit to the cart. Zero checkpoints in this run
  either.

**Propagated through the whole surface, not just `crawler.py`:** the web
UI's "New run" form (a 5th Limits field, `f-action-repeat`), its
save/load round-trip (`fillForm`/`resetForm`), and the HTML report's
meta-row (`max repeats/action`) -- same treatment every other limit
already gets, so this doesn't become the kind of silently-invisible
knob the depth/max_flows truncation work above exists to prevent.

## Revisit-history-aware candidate ordering (done, Aug 2026)

**Started from the "Parked" list's "Reversible-pair collapsing" item,
but measuring the real cost first changed the design before any code
was written** -- consistent with this project's discipline of
investigating before building, not with the original framing being
wrong exactly, just imprecise about where the actual waste is.

**What measuring first found.** The original framing assumed opening
and closing the hamburger menu "doubles states." It doesn't, and
checking the mechanics directly shows why: when a click's target
fingerprint already matches a known state (`outcome == "revisit"`), the
DFS already never pushes a new frame for it (`continue`s the current
frame instead) -- confirmed on a live run: **0 of 296 transitions**
continued past a revisit. So no wasted future depth, no doubled states,
contradicting the original claim. Measuring instead found a different,
real cost: **73% of all flows in that same run ended in a revisit** --
a full fresh-context replay (re-running every step from the start of
the browser session) spent purely to reconfirm a state already known --
and **28.5% of candidates cut by `max_breadth_per_state`** shared a
`norm_signature` with something *elsewhere in the same run* already
confirmed to lead nowhere new, meaning breadth truncation was cutting
candidates close to arbitrarily, sometimes discarding a genuinely novel
one to make room for one already known to be unproductive. The
`norm_signature`s that actually triggered revisits were also
informative: `add-to-cart`, `checkout`, `remove`, `cancel`, `continue`
-- ordinary mutating actions converging on a shared end state, not
mostly UI-chrome toggles. A label-matched "open/close/expand/collapse"
heuristic (the original framing) would have caught almost none of this,
on top of being a language-dependent guess -- the same class of mistake
`field_detect.py`'s login-trigger matching hit earlier this project,
fixed there by matching structure instead of English text.

**Fix: learn revisit-proneness live, per-persona, and use it only to
break ties in candidate ordering.** A new per-persona `revisit_history:
set[str]` (reset each persona's pass, same reasoning as
`seq_to_flow_id` -- what one persona converges on doesn't mean another
will) records every `action_norm_signature` that has, at least once
already in this persona's pass, produced a revisit. `_order_for()`
(already responsible for `max_breadth_per_state` truncation) now stable-
sorts a state's candidates so any already-flagged signature moves to the
back before truncation runs -- a forced cut preferentially drops actions
already confirmed to lead nowhere new, everything else keeps
`discover_candidates()`'s own risk-tier ordering unchanged. Nothing is
withheld outright the way risk gating or the repeat-action cap
withholds candidates -- this only changes which ones survive a breadth
cut that was going to happen anyway, so no new `limits` knob, no new
report surface: there's no number for an operator to tune.

**A real bug this exact live-verification step caught before it
shipped, not after.** Re-running with a realistic (non-artificially-
tight) breadth confirmed states_discovered held steady (14, matching
the pre-fix baseline) -- but a targeted check for saucedemo's own sort
dropdown (`TC-10` in the M2 gap-analysis calibration, a real, valued
capability, `is_choice=True`) came back completely missing. Root cause:
picking a `<select>` option changes display order, not the candidate
*set* -- `state_fingerprint()` deliberately doesn't change (see "State
fingerprint blind to configuration-like selections" above), so *every*
choice in a group reads as a revisit the instant any ONE option is
tried anywhere in the run. Without an exemption, that flagged the whole
sort-choice group as "known unproductive" and buried it under
`max_breadth_per_state` at every state discovered afterward -- silently
reintroducing, in this new ordering code, exactly the mistake the
`is_choice` mechanism already exists to prevent elsewhere
(`gap_analysis.py`, `shared_steps.py`, `testcase_draft.py` all already
special-case it; this file just hadn't caught up). Fixed with an
exemption: `is_choice` candidates are never deprioritized by
`revisit_history`, full stop, regardless of what it says about their
signature. Proven with an isolated, deterministic check (not live-crawl
noise): a constructed state with 4 `is_choice` candidates all flagged
as revisit-producers, alongside 2 flagged and 6 unflagged ordinary
ones, at `max_breadth=10` -- confirmed all 4 choice candidates survive
the cut and exactly the 2 flagged ordinary ones are the two dropped.

**Verified live, before and after, same saucedemo config
(`max_breadth_per_state=5`, deliberately tight, to force truncation and
make the effect measurable):**
- Breadth-cut candidates sharing a signature with a known revisit-
  producer: **28.5% -> 52.6%** of all breadth cuts -- confirms the
  reordering is doing its job, preferentially sacrificing already-
  confirmed-unproductive candidates when a cut has to happen.
- Flows ending in a pure revisit (wasted full replay): **73.2% ->
  60.6%**.
- Wall clock for the same config: **167.7s -> 88.0s** -- nearly halved,
  though this specific number is a side effect of the traversal shape
  changing (different candidates surviving truncation means a
  genuinely different subgraph gets walked), not a claimed guarantee
  for every site.
- Re-run at a realistic (non-artificially-tight) breadth=10: states
  discovered held at 14 (matching the pre-fix baseline measured for the
  repeat-action-cap work above), all previously-verified capabilities
  (login, add-to-cart, checkout, remove) still present. The one
  apparent miss (sort dropdown) traced to a pre-existing, unrelated
  condition -- saucedemo's inventory page legitimately has ~12 real
  candidates (6 add-to-cart + menu + cart-link + 4 sort options),
  already more than breadth=10 allows regardless of ordering (confirmed
  by re-running at breadth=20, where the primary inventory state's sort
  options survive intact, and by checking `saucedemo_wide.json`'s own
  config, already `max_breadth_per_state: 15`, exactly why the earlier
  M2 calibration never hit this) -- already honestly reported via the
  existing `skipped_candidates`/"breadth limit exceeded" mechanism, not
  something this change caused or needs to fix.

**Known residual imprecision, inherent to learning live rather than
upfront:** a state discovered early in a persona's pass can't benefit
from revisit-proneness learned later -- `_order_for()` runs exactly once
per state, at first discovery, so ordering quality depends on DFS
traversal order. Not fixable without either a two-pass crawl (real cost
in wall-clock and complexity) or re-ordering already-pushed frames
retroactively (real cost in correctness -- a frame's `order`/`pos` is
mutated in place during exploration). Left as directional, not
exhaustive, matching this project's existing tolerance for CDP
verification and other heuristics that improve the common case without
promising completeness.

## Superseded — Infinite scroll / pagination limits (investigated live, Aug 2026; not building either)

**The last two items from "Parked -- smart limits" above.** Both were
framed as risks of *unbounded growth* -- a scroll or a pager control
that the crawler follows indefinitely, needing an explicit cap. Both
turned out, on live investigation, to have the opposite problem, or no
problem at all. Investigated against `quotes.toscrape.com` -- a public
site built specifically for scraper testing, with a real, finite
paginated listing at `/` (10 pages, `/page/N/`) and a real,
JS-driven infinite-scroll variant at `/scroll` (100 items, no page
numbers, pure scroll-triggered AJAX) -- the same "test against a real
public site rather than a private/authenticated app" choice `httpbin.org`
served for the radio/checkbox work above.

**Infinite scroll: the assumed risk doesn't apply, because the crawler
doesn't scroll.** Checked `_DISCOVER_JS` directly -- there is no scroll
call anywhere in it, and Playwright's own auto-scroll-into-view (used
only to bring a *specific* element into view before clicking it) is the
only scrolling that happens today. Confirmed live on `/scroll`: the page
starts with 10 quotes and grows to 100 after repeated manual
`mouse.wheel()` calls in the probe script, but `discover_candidates()`
never triggers that growth on its own, so the crawler only ever sees the
first 10. This isn't a runaway-growth risk today (the failure mode the
original framing assumed) -- it's a **coverage gap**: 90% of a real
infinite-scroll page's content is invisible to the crawler, silently.
Adding real scroll support is a genuinely different, larger piece of
work than a "limit" -- a new interaction primitive (when to scroll, how
to detect newly-revealed content vs. a fingerprint that's still
technically "new" because of accumulated DOM, when to decide enough has
been seen) with its own design questions, not a small addition to
`_order_for`. Not building it now -- no evidence yet that any site this
project actually targets uses infinite scroll for content that matters
to a test suite, and the honest fix for *that* gap, if it ever shows up,
is a scope decision on its own, not a rider on this investigation.

**Pagination: the measured problem is the opposite of the framing.**
Two live runs, both showing the "Next" control essentially never gets
followed at all under realistic budgets -- not because anything
withholds it deliberately, but because it almost always loses ordinary
DOM-order competition to everything else on the page:
- From `quotes.toscrape.com/`'s real tag cloud (36 candidates at the
  root: dozens of tag links, not just one pager) at
  `max_breadth_per_state=15`: 52 states discovered, 657s wall clock,
  **zero flows ever contain a "Next" step** -- it loses to the tag cloud
  on breadth every time (`"Open \"Next →\""` shows up repeatedly in
  `skipped_candidates` with `"breadth limit exceeded"`).
- Starting directly on a tag page with real, short, naturally-ending
  pagination (`/tag/love/`: page 1 -> page 2 -> no page 3) at a
  deliberately generous `max_breadth_per_state=40` (breadth no longer
  the constraint): **still zero successful "Next" follows** -- this
  time `max_depth` is what cuts it, because "Next" typically sits last
  in a page's DOM order, and a naturally-ordered DFS tries everything
  else on the page (and on pages reached from it) first.
- The `max_action_repeat` cap (built earlier this session specifically
  to bound this kind of chain) never once got the chance to engage in
  either run -- there's no data showing it's insufficient, because
  "Next" was never reached enough times in a row to test it.

**So the real, evidenced problem is coverage, not runaway growth --
content that only exists on page 2+ is disproportionately likely to be
missed, not disproportionately likely to be over-explored.** And that
coverage gap is already honestly reported by the exact same
transparency mechanism every other budget limit in this project uses:
`skipped_candidates` with `"breadth limit exceeded"` or `"max_depth
limit reached"`, visible in the report the same way any other
truncation is (see "Depth-truncation was invisible" above) -- an
operator who needs page-2+ content covered already has the tools
(raise `max_breadth_per_state`, or use `exclude_patterns` to shed
competing tag/nav noise) and the report already tells them what was cut
and why. No new mechanism is needed to *explain* the gap; whether one is
needed to *close* it (e.g. biasing pager-shaped controls higher in
`_order_for`'s ordering) has no evidence behind it yet either -- doing
that generically, without a fragile label/language guess, would need
its own real measurement of how often it actually matters on a site
this project targets, the same discipline that shaped every other fix
in this file.

**Both closed as investigated, not deprioritized -- the investigation
changed the conclusion, not just the schedule,** same framing as the
"Superseded -- Vision fallback" entry above.

## exclude_patterns is href-only (done, Aug 2026)

Found while validating M5, fixed the same session it was picked back up.
`exclude_patterns` (`risk.classify()`) only evaluated a glob pattern
when the candidate element had a real `href` attribute. Confirmed on
live saucedemo: "Checkout" is a `<button>` with no `href` at all,
navigating via client-side routing -- `exclude_patterns: ["*checkout*"]`
silently let it through, no error, no warning.

**Not a saucedemo quirk -- structural, and broadly applicable.** Any
site using button-triggered client-side routing (React Router, Vue
Router, Next.js `<Link>` rendered as a button, etc. -- a majority of
modern SPA frontends) has the same gap: nothing to pattern-match
because the destination URL doesn't exist until after the click.
`configs/site-b.json`'s own `*/privacy*`/`*/terms*` patterns were exposed
to this too, for any privacy/terms link on Site B (or elsewhere)
that turns out to be button-based rather than a plain `<a href>`.

**Why not a quick patch:** the only way to learn a button's destination
is to click it -- which defeats the purpose for exactly the case
`exclude_patterns` exists to protect (you cannot safely "click once to
check the URL" a control meant to be excluded, e.g. a hypothetical
`*cancel-subscription*` pattern).

**Fix: match on the element's own label too, not just `href`.** A new
check in `risk.classify()`, deliberately placed *outside* the
`if href:` block so it still runs when there's no `href` at all --
the same `exclude_patterns` list, the same glob syntax (`fnmatch`), just
matched against the candidate's lowercased label text as well as the
URL path. Reuses the existing pattern list rather than adding a second
config surface for "label patterns" -- a pattern already written for a
URL path (`"*/privacy*"`) won't accidentally start matching label text
too, since ordinary label text doesn't contain `/`, so this is additive
for patterns already in use, not a behavior change for them (verified
below, not assumed).

**Verified live, both directions:**
- The exact motivating case, saucedemo with `exclude_patterns:
  ["*checkout*"]`: **zero flows now contain a Checkout step** (previously
  every flow walked straight through it). `skipped_candidates` records
  the honest reason -- `"matches exclude pattern '*checkout*' (label)"`
  -- distinguishing it from an href-based match in the same list.
- Backward compatibility, Site B with its existing `exclude_patterns:
  ["*/terms*", "*/privacy*"]` (both href-based, pre-existing config,
  unmodified): still zero terms/privacy steps in any flow, byte-for-byte
  the same exclusion behavior as before this change -- confirms the new
  label check doesn't interfere with or duplicate the existing href
  check.
- Four isolated, deterministic checks (label-only match with no href,
  href-only match unaffected, a URL-shaped pattern *not* accidentally
  matching unrelated label text, a label-shaped pattern *not*
  accidentally matching an unrelated href path, case-insensitivity, and
  "no `exclude_patterns` configured at all falls through to ordinary
  keyword classification unchanged") -- all passed before the live runs,
  narrowing down exactly what live verification needed to confirm.

## Detect fields: fake "type" on textarea/select (found by user report, Aug 2026)

Found by a user running "Detect fields from site" against google.com
and reporting the literal output: `textarea · textarea "q"`. Confirmed
live before touching anything -- `detect_fields('https://www.google.com/')`
returned `{"tag": "textarea", "type": "textarea", "name": "q", ...}`,
both fields genuinely identical.

**Root cause: `_FIELD_SCAN_JS` was fabricating a `type` for elements
that don't have one.** `<textarea>`/`<select>` have no `type` attribute
in real HTML at all -- the scanner synthesized a stand-in (`'textarea'`
/ `'select'`, literally echoing the tag name) so a second piece of code,
`SKIP_TYPES.has(type)`, would have something to check for `<input>`
elements. That stand-in then leaked straight into the reported field
data, and the web UI's own label logic (`f.tag + (f.type ? ' · ' +
f.type : '')`) had no way to tell a real `type` from a fabricated one --
so a real, correctly-detected field (Google's search box genuinely is
`<textarea name="q" id="APjFqb">`, not a detection mistake) produced a
redundant, confusing label. Not a hypothetical: exactly the "never
invent information you don't actually have" principle this project
holds elsewhere (flow identity, gap analysis, risk classification) --
this was the same mistake in a much smaller, easy-to-miss corner.

**Fix:** split the stand-in (kept, local to the filter check) from what
gets reported. A field's `type` in the output is now the real attribute
value when one exists, `'text'` for a bare `<input>` with none (a
genuine browser default, not a guess), and `''` for `<textarea>`/
`<select>` -- nothing to fabricate for those. The UI's existing ternary
already handles an empty `type` correctly (omits the `· type` suffix
entirely) with no separate UI change needed.

**Verified live, three cases:**
- google.com re-run: `type: ""` now, no other field changed --
  `tag`/`name`/`id`/`ariaLabel` all identical to before the fix.
- Regression, saucedemo's login form (real `<input type="text">` /
  `<input type="password">`): both still report their genuine, distinct
  `type` unchanged -- confirms the fix didn't touch the case the
  feature was originally built around (telling a username field apart
  from a password field).
- A local fixture page with a real `<select>` (not tested live before,
  same code path as `<textarea>` by construction but not assumed
  identical without checking): `type: ""`, confirming the fix covers
  both untyped element kinds, not just the one from the bug report.

## Run management: delete only (done, Aug 2026)

Asked directly, checked before answering rather than assumed: does the
operator UI have any run-management -- deleting old runs, pagination,
sorting by something other than newest-first, grouping/filtering by
project? None of it existed. `GET /api/runs` returns every run on disk
in one unbounded list, sorted a single fixed way
(`finished_at ?? started_at`, newest first, no alternative); there's a
`DELETE /api/configs/{name}` for saved configs but no equivalent for
runs at all -- `runs/` only ever grows.

**Scoped to deletion only, deliberately, not the whole list.**
Irreversible accumulation is the actual pain point once this gets used
for real; browsing a long list is a milder problem, and pagination/
sort/group-by-project have no usage data yet to size them against
(same reasoning as the earlier infinite-scroll/pagination
investigation above -- build the piece that's confirmed to matter,
not the whole imagined feature set at once).

**`runs.delete_run(run_id)`**: removes `runs/<run_id>/` from disk and
drops the in-memory `RunHandle` if one's still held. Refuses a
still-running crawl (`_execute()` only creates the output directory
*after* `crawl()` returns, so there'd be nothing on disk to delete yet,
and clearing the handle out from under a live background thread would
orphan it rather than stop it -- this isn't a cancel feature).
`project_state/` -- the durable, cross-run record keyed by project
name, not run_id -- is deliberately untouched, the same way it already
survives a run simply aging out of the listing on its own.

`DELETE /api/runs/{run_id}` — 200 on success, 404 if the run doesn't
exist anywhere (disk or memory), 409 if it's still running. Web UI: a
🗑 button per run-list row (`stopPropagation` so it doesn't also select
the run), a `confirm()` prompt, and — if the deleted run was the one
currently open in the detail pane — the pane resets to the empty state
rather than leaving a stale iframe pointed at a `report.html` that no
longer exists.

**Verified live, not just unit-level:** a real Playwright-style
`flows.json` written to `runs/test-delete-me-12345/`, deleted via
`delete_run()` directly -- directory confirmed gone. Separately, the
actual HTTP path against a running server: created a run directory,
confirmed it in `GET /api/runs`'s output, `curl -X DELETE
/api/runs/live-delete-test` → `200 {"deleted": ...}`, directory gone
from disk, run gone from the next `GET /api/runs`. `DELETE` on a
nonexistent run id → `404` confirmed the same way. The still-running
refusal and the not-found case were also checked directly against
`delete_run()` (a fake `RunHandle` with `status="running"` correctly
raises and is left in place, not removed).

## Detect fields: two-step login forms merged in an unrelated page (found by user report, Aug 2026)

Found by a user report against gmail.com: `detect_fields()` returned
three fields, one of them (`recoveryIdentifierId`) not from the landing
page at all -- `clicked_trigger` was `"Forgot email?"`, a link the tool
should never have followed.

**Confirmed live, both layers, before touching anything.** Gmail's real
landing page already has a genuine, correctly-detected field: an
`identifier` input (step 1 of Google's own two-step sign-in -- email/
phone first, password on a second page after "Next"). But
`detect_fields()`'s trigger-search condition was `if not any(f["type"]
== "password" for f in fields):` -- true here, since no password field
exists *yet* by design, not because the form wasn't found. That
incorrectly sent it hunting for something to click, and a second, truly
independent bug did the rest: `_LOGIN_HREF_RE` (`log-?in|sign-?in|
log-?on`) matched "Forgot email?" purely because its real href
(`/signin/usernamerecovery?...`) contains "signin" as a bare substring
-- the link is account recovery, not login continuation, but the regex
doesn't distinguish a login-flow URL namespace from a specific
login-continuation link inside it. The click landed on the recovery
page, and its own field got merged (`fields = fields +
page.evaluate(...)`, additive by design for the *original* motivating
case) into the result alongside the real landing-page fields.

**Fixed the trigger condition, not the regex.** Changed `if not
any(password)` to `if not fields:` -- only chase a trigger when the
landing page shows genuinely nothing useful yet, not merely "no
password specifically." A real, already-visible field (even just step
1 of a multi-step form) is useful information on its own and shouldn't
be silently supplemented by wherever an imprecise trigger match happens
to lead. The href-substring looseness in `_LOGIN_HREF_RE` is still
real and unfixed -- it just no longer gets a chance to misfire once a
real field has already been found, which covers this case completely
without needing to solve the harder, fuzzier regex-precision problem
today.

**Verified live, three cases, not just the one that motivated the fix:**
- gmail.com re-run: `clicked_trigger: null`, exactly the two real
  landing-page fields (`identifier`, `hl`) -- no recovery-page field,
  no bogus click.
- The original motivating scenario (a landing page with zero visible
  fields, real form one click away) re-checked against a local fixture
  built to reproduce it: `clicked_trigger: "Log In"`, both
  username/password fields found on the page it correctly navigated
  to -- confirms the fix didn't regress the case this feature exists
  for in the first place.
- Regression, saucedemo's login form (both fields already on the
  landing page, no click ever needed): unchanged, `clicked_trigger:
  null` before and after.

## Detect fields: fixed sleep too short for a client-side-rendered page (found by user report, Aug 2026)

Found by a user report against `account.proton.me/mail`: "Detect
fields from site" found nothing at all -- no error, no fields, no
trigger click attempted.

**Confirmed live, not assumed, and the real cause was different from
what the symptom suggested.** First checked for a shadow-DOM boundary
(a plausible reason `document.querySelectorAll` could miss real
elements) -- ruled out directly: the page's own `username`/`password`
inputs sit in the plain document, not a shadow root. The actual cause:
timing. Measured directly, repeated to be sure it wasn't a one-off:
`document.querySelectorAll("input, textarea, select").length` reads 0
at 400ms after Playwright's `wait_until="load"` fires, and a stable 3
from ~500ms onward, consistently across three separate runs (a genuine
one-time step as the page's JS finishes mounting the form, not a
flicker -- checked with 150ms-interval polling before concluding that).
`detect_fields()`'s fixed `page.wait_for_timeout(400)` was reading the
DOM mid-render on this specific client-side-rendered login page and
correctly, honestly reporting "nothing here yet" -- not a detection
bug, a timing one.

**Fix: poll instead of sleep-and-hope, both places a fixed wait
existed.** New `_wait_for_any_field()`: checks for any
input/textarea/select every 150ms, capped at 3000ms total, returning
as soon as one appears. Replaces both the initial-page wait (400ms
fixed -> adaptive) and the post-trigger-click wait (800ms fixed ->
same adaptive helper, same reasoning: whatever page a login trigger
navigates to can be just as client-side-rendered as the landing page
was). A page with genuinely nothing to find spends the full 3-second
budget before giving up -- an acceptable cost for a one-off, human-
triggered lookup (not part of the crawl's own budget-sensitive loop).

**Verified live:**
- `account.proton.me/mail`, three consecutive runs: both `username`
  and `password` fields found every time, `clicked_trigger: null`
  (correct -- the form was already there, no click needed once the
  wait was actually long enough).
- Confirmed through the real running server's own HTTP endpoint too,
  not just a direct function call.
- Regression: gmail.com and saucedemo (both fast-rendering, fields
  available well before 400ms) -- identical results to before this
  change.
- The original empty-landing-page-then-click scenario, re-checked on a
  fresh local fixture: still finds and clicks the trigger, still finds
  the real form on the page it navigates to -- confirms the
  post-click wait replacement didn't regress the case it also covers.
- A genuinely empty page (no fields, no trigger anywhere): degrades
  the same way as before -- empty result, no error -- just takes up to
  ~3.6s instead of ~0.4s, measured directly rather than assumed
  acceptable.

## Parked — native mobile apps (exploratory research, Aug 2026, not started)

Raised as "a topic to think about," not a decision to build. Answered
from architecture inspection (which modules import Playwright, which
don't), not from any live test against a real mobile app -- unlike
everything else in this file, this hasn't been checked against a real
target yet. Full writeup: [`docs/mobile-exploration.md`](docs/mobile-exploration.md).

Headline: ~63% of the codebase (`gap_analysis.py`, `semantic_dedup.py`,
`identity.py`, `report.py`, the whole web UI, ...) operates on
`Flow`/`Transition`/`StateNode` and doesn't know what a browser is --
the actual value proposition ports without changes. What doesn't:
the driver (Playwright -> Appium, plausibly the easy part), element
discovery (accessibility trees plausibly *easier* than the DOM, but
framework-dependent -- Flutter/canvas-rendered apps may be a dead end),
the state fingerprint (no URL exists on native at all -- the doc's
proposed fix is a fingerprint-verified Back-button backtrack, worth
folding into the *web* crawler too independent of mobile), and
reset+replay's cost (mobile app resets are plausibly 5-15s+ each,
vs. this project's own measured 90-660s *full crawls* on the web --
DFS's per-candidate full-replay cost may not survive the port at all).
Recommended first step, not yet taken: three empirical questions
against one real Android app, before designing anything further.

## Reverse gap analysis: diagnosing not_found TCMS items (done, Aug 2026)

Raised directly: gap analysis already finds flows with nothing
resembling them in the test plan ("gap"), but a TCMS item with nothing
resembling it among discovered flows ("not_found") was a dead end --
just a bare status, no reason. Two ideas from the user for what to do
about it: (1) try to understand and reproduce the TCMS-described flow,
to find gaps on FlowScout's own side; (2) if reproduction fails,
diagnose *why* and say so -- an app bug, or a stale test case.

**Idea (1), reframed before building anything.** The literal version --
an LLM reads the TCMS text and drives a live browser trying to enact
it -- was set aside deliberately, not attempted. That's a different
tool category (agentic browser automation) with a different reliability
profile (grounding free text to a real element is exactly the kind of
guesswork this project has avoided everywhere else -- M1/M2's own
history is full of embedding-similarity surprises when representations
aren't chosen carefully), and it would mean asserting FlowScout tried
something it can't fully verify happened the way it thinks it did. A
graph-search alternative (does a path already exist in the *full*
discovered graph, not just the promoted unique-flow pool, whose
transition-label sequence matches the TCMS steps) was considered as a
better-grounded middle path -- reuses data already collected, invents
nothing -- but scoped out of this pass at the user's explicit choice;
left for later, see the Parked idea below.

**Idea (2), built now -- the cheaper, more directly useful half.** For
every `not_found` TCMS item, `_diagnose_not_found()`
(`gap_analysis.py`) checks two things the crawl already recorded,
nothing new:

1. **`skipped_candidates`** -- every action the crawl found but chose
   not to follow, each with an exact reason (risk policy, a specific
   limit). A semantic match here means "withheld", with the real reason
   attached -- directly actionable (raise a limit, toggle
   `allow_mutating`, adjust `exclude_patterns`).
2. **`checkpoints`** (`kind == "error"`) -- actions the crawl DID
   click, that raised a real exception. A match here means "errored" --
   the strongest signal this project can offer for "this might be a
   real app bug," short of a human confirming it.

Neither matching leaves `diagnosis` as `None`, not a third catch-all
status -- deliberately, since it could mean several different things
(a stale test case, a path the crawl never got close to, or a
precondition -- an already-logged-in admin, an item already in the
cart -- the clean-slate-per-path model doesn't produce), and this
project doesn't guess which. The report says exactly that rather than
picking one.

**Cost-conscious by construction.** `skipped_candidates` are deduped by
`(label, reason)` before embedding -- the same withheld control often
repeats across many states (e.g. "Open Menu" skipped at five different
pages for the same reason), so this bounds the extra embedding calls by
*distinct* withholding reasons, not raw occurrence count, which on a
truncation-heavy run can be hundreds apart. Gated on the same
`embeddings.api_key_configured(provider)` check as the rest of gap
analysis -- degrades to undiagnosed `not_found` (the pre-existing
behavior) rather than failing anything.

**Threshold, stated honestly, not assumed.** Reuses `analyze_gaps()`'s
own `threshold` parameter (0.74 by default) as a starting point --
this specific comparison shape (TCMS text vs. a short skipped-candidate
label or checkpoint message) has *not* been separately calibrated the
way the action/nav pools were (see this file's own M1/M2 threshold
story). Documented as informational, not as confidently scored as an
action-pool match, both in the code and in the report's own copy.

**Verified live, all three outcomes, not assumed from reading the
code:**
- **"withheld"**: saucedemo with `allow_mutating: false` (so add-to-
  cart/checkout land in `skipped_candidates` with an explicit
  `"mutating action withheld"` reason) plus a TCMS item describing
  "Add a product to the shopping cart" -- correctly diagnosed
  `withheld`, 78% match. (Genuinely instructive: it matched a
  *different* real skipped-candidate entry for the same control --
  one truncated by `max_depth` rather than the `allow_mutating`
  withholding I'd set out to construct -- still accurate, still
  useful, confirms the mechanism isn't just echoing back the one
  case it was built against.)
- **"errored"**: a local fixture (a button with `pointer-events:none`,
  a real, deterministic Playwright actionability timeout, not a timing
  race that could go either way) plus a matching TCMS item -- correctly
  diagnosed `errored`, 80% match, detail text matches the real
  checkpoint message.
- **No diagnosis**: a TCMS item describing something that genuinely
  doesn't exist on saucedemo ("Export order history as a PDF invoice")
  -- correctly left undiagnosed, `None`/`None`.
- Full HTML report rendered end-to-end with a real gap analysis
  carrying both a "withheld" and a "no evidence found" item -- diagnosis
  chips present and correctly styled (risk-mutating / risk-destructive /
  risk-neutral, reusing the existing chip system rather than adding a
  new one), no template errors.

**Parked, not built now:** the graph-search half of idea (1) --
searching the *full* discovered state graph (not just the promoted
unique-flow pool) for a path whose transition-label sequence matches a
`not_found` TCMS item's own steps, which would give "reachable, but
never counted as its own flow" (budget/dedup truncation, not a real gap
of any kind) its own honest diagnosis distinct from the three built
here. No new crawling needed for it either -- same "read what's already
there" discipline -- just a bigger search than a single embedding
comparison. Scoped out at the user's explicit choice to start with the
cheaper half; worth revisiting once there's a real `not_found` case
that this doesn't already explain. **Revisited the next day -- see
"Reverse gap analysis, part 2" below: measuring idea (1) properly
before building the graph-search version found a cheaper, more
important problem first (a real false negative, not a missing
diagnosis) and fixed that instead of the originally-parked idea.**

## Reverse gap analysis, part 2: a real false negative, found by measuring idea (1) before building it (done, Aug 2026)

Came back to the parked graph-search idea with the discipline this
project keeps using: measure before designing. Two measurements first,
neither assumed:

**Measurement 1 -- the TCMS step format itself doesn't support step-
by-step matching.** Checked this project's own `fixtures/
tcms_saucedemo.csv` (a real, representative export, not a synthetic
one): `steps` is unstructured prose, no numbering, and mixes actions
with expected results in the same sentence -- `"click Add to cart on
any product. The cart badge should increment."` A naive step-by-step
split would try to match "the cart badge should increment" against
something clickable, which can't ever succeed (FlowScout deliberately
never asserts about outcomes) and isn't a real gap either. The
graph-search version of idea (1) would have needed this exact
step-by-step structure to mean anything -- checked before building it,
not discovered after.

**Measurement 2 -- a real, measurable blind spot, and it's a false
negative, not a missing diagnosis.** Counted directly on the saved
`saucedemo-wide` run (112 flows: 27 unique, 80 duplicate, 5 blocked):
9 distinct mutating/choice signatures in the unique-flow pool
`gap_analysis.py` already compares against, but **10** across every
flow the crawl actually walked. The missing one: `finish` -- the last
click of checkout, on this run reachable only through flows that ended
up deduped or truncated before ever being counted "unique". A TCMS
item describing checkout completion would score a false `not_found`
today, despite the crawl genuinely having completed it. This isn't
something graph-search diagnosis (the originally parked idea) would
even explain correctly -- it's not "reachable but not counted", it's
"literally already happened, and the tool just isn't looking at the
data that proves it."

**Built the fix for the measured problem, in order of found cost, both
grounded in data already collected -- no new crawling, no LLM-driven
live browsing:**

**A. Action pool now draws from every flow the crawl walked, not just
unique ones.** `_action_pool_from()`'s `flows` parameter is now
`run.flows` (any status) at the `analyze_gaps()` call site, not
`action_flows` (unique-only). *Reportable* flow status (`FlowCoverage`
-- what an operator actually sees per flow) stays scoped to unique
flows exactly as before; only the *pool of known capabilities* an
action gets compared against widened. A real second bug caught while
building this, not assumed away: broadening naively would have also
pooled in an action that was *attempted and failed* (a flow terminated
by `"Terminated: action ... raised an error"` still carries that failed
action as its last transition) as if the app supports it -- exactly
backwards, since a failed action is the opposite signal. Fixed by
excluding any transition with `to_fp is None` (crawler.py: only ever
true for the one transition that raised an exception, never set on a
genuine success). `matched_flow_id` selection (`_pick_matched_flow_id`)
now prefers a unique flow when one exists, falling back to a
duplicate/blocked flow's id only when the action genuinely lives
nowhere else -- keeps the report's "look at flow #N" pointer canonical
when possible.

**A sibling bug found and fixed along the way, not scope-creep --
directly in the same code path.** `Transition.outcome` was documented
(`# ok | revisit | skipped | error`) but crawler.py never actually set
it to `"error"` anywhere -- the error-termination branch built the
`Checkpoint` correctly but left `trial.outcome` at its dataclass
default `"ok"`. This made `report.py`'s own `elif t.outcome ==
"error":` rendering (a red step-error note with the exception detail)
dead code: a flow's failed final step rendered identically to a normal
successful one. Also needed directly for the `to_fp is None` exclusion
above to be trustworthy (same signal, more explicit). Fixed: the error
branch now sets `trial.outcome = "error"` and `trial.detail` from the
Checkpoint it always appends immediately before returning (its only
return-`None` path, so this is always the matching detail, not a
guess). Verified live on a deterministic `pointer-events:none` fixture
(not a timing race): `outcome == "error"`, real Playwright timeout text
in `detail`, and the report's `step-error` note actually renders.

**B. New third diagnosis, `"discovered_not_walked"`, for `not_found`
TCMS items.** Extends `_diagnose_not_found()` (see "Reverse gap
analysis" above) with a third pool: every candidate discovered in some
`StateNode.candidates` whose `norm_signature` never became a transition
in any flow, any status. The clearest real case this catches:
`max_flows` cutting a persona's pass short records one aggregate
`Checkpoint` for everything still queued, not a per-candidate
`skipped_candidates` reason -- so a specific control lost to it has no
individual trace anywhere except its own discovery record. Tells the
operator "the app has this, the crawl saw it, but ran out of budget
before trying it" -- a gap on FlowScout's own side, not the app's,
distinct from "withheld" (an explicit, reasoned withholding) and
"errored" (attempted and failed). All three pools now score against
each `not_found` item together, highest wins -- no artificial priority
order beyond that, since each represents a genuinely different,
independently-plausible explanation.

**Verified live, both parts, plus the report render:**
- **Part A**: `_action_pool_from(run.flows, ...)` on the saved
  `saucedemo-wide` run now includes `finish` (absent before);
  `finish`'s only owner is a non-unique flow, confirming
  `_pick_matched_flow_id`'s fallback path actually engages, not just
  its preferred path. A TCMS item describing checkout completion:
  `not_found` before this fix, `covered` (0.87 score) after, against
  the *same* underlying run data -- confirms this is a real correction,
  not a different measurement.
- **Part B**: saucedemo crawled live with a deliberately tight
  `max_flows=3` (5 states discovered, only 3 flows walked, checkpoint
  confirms "28 candidate action(s) never tried"). A TCMS item for
  "Add a product to the shopping cart" -- a control confirmed present
  in `StateNode.candidates`, confirmed absent from every flow's
  transitions AND from `skipped_candidates` -- correctly diagnosed
  `discovered_not_walked`, 81% match, detail naming the exact control
  ("On Inventory: Add to cart").
- **A real false-positive risk found and avoided during this exact
  verification, not shipped by accident:** two TCMS items about the
  cart page scored `covered` through the *pre-existing* navigation-flow
  pool's own known imprecision (a pure-navigation flow's whole-flow
  text happened to say "Ends on Cart page", coincidentally close enough
  to both "open the cart" and "proceed to checkout" text) *before ever
  reaching the new diagnosis code* -- already documented as a live
  limitation in this module's own docstring ("still not fixed by this,
  honestly"), not something introduced today. Recognized the
  contamination, switched to a TCMS item describing a genuinely
  mutating action (which can only match through the action pool) to
  get a clean, uncontaminated verification of Part B specifically.
- Full HTML report re-rendered with both new (`discovered_not_walked`,
  a green "seen, not tried" chip reusing the existing risk-chip system)
  and previously-built (`withheld`, `errored`, no-evidence) diagnosis
  chips present together, no template errors.

**Not built, and why:** the graph-search version of idea (1) stays
parked. Measurement 1 above is the honest reason -- the TCMS step
format this project's own real data actually has doesn't support
step-by-step sequence matching yet, so building the search machinery
first would have produced something with nothing meaningful to search
against. Worth revisiting if TCMS sources with real structured steps
show up, not before.

## Resume a specific blocked flow (done, Aug 2026)

Asked directly: does the report mark flows that got cut short so an
operator can see them, and could they edit a budget (e.g. max_depth)
and re-run just that one flow to completion instead of the whole
crawl? Marking was already comprehensive (see "Depth-truncation was
invisible" above) -- every truncated/withheld flow already shows up in
the report's lead list, not tucked away, with the exact reason on the
card. Targeted continuation didn't exist at all.

**Scoped to per-flow-anchored causes only, at the user's explicit
choice.** `max_depth` truncation and a dead end from risk-policy/
repeat-cap withholding are both genuinely anchored to one flow's own
path (`frame.path` at the moment it happened) -- "keep going from
exactly here with a different limit" is a coherent thing to ask for.
`max_states`/`max_flows` truncation are whole-persona-pass budgets,
not tied to any one flow -- resuming "just this flow" wouldn't address
what actually blocked it; the honest fix there stays a full re-crawl
with a higher limit. New `Flow.resumable: bool` (set only at the two
qualifying `emit_flow()` call sites in crawler.py) marks the
distinction explicitly, not inferred from parsing `dedup_reason` text.

**Architecture: extracted the DFS loop, not duplicated it.** The
highest-risk part of this by far -- the per-persona `while stack:`
loop is the most load-bearing code in the project. Pulled out into
`_run_dfs()` (browser, run, credentials, persona_name, a seed `stack`,
`next_flow_id`, the limit values, and a `states_before`/`flows_before`
baseline pair), called identically by `crawl()`'s own per-persona pass
and the new `resume_flow()`. `resume_flow()` itself just seeds the
stack differently: one `_Frame` at `flow.end_state_fp` with
`path=list(flow.transitions)` (reusing the already-known StateNode and
each transition's own `replay_meta` -- no re-discovery needed, the
exact same "trust what backtracking already earned" reasoning
`_run_path()`'s replay-from-root already runs on, just anchored later)
instead of a fresh root frame, with `states_before`/`flows_before`
computed fresh at resume time so the new limits get their own full
budget, not whatever remained of the original pass's.

**A real design question worked out by tracing the actual loop
mechanics, not assumed:** does resuming risk re-trying candidates the
original pass already tried from this exact frame? No -- traced
directly: `len(frame.path)` is fixed for a frame's whole lifetime (only
child frames get a longer path), so a depth-truncated frame *always*
hits its limit at `frame.pos == 0` -- nothing was ever tried from it. A
dead-end-withheld frame ran its `order` to completion but every
candidate was withheld, never actually followed. Both cases mean a
fresh `_Frame` with a fresh `_order_for()`-computed order and `pos=0`
is exactly correct for resuming -- no double-counting, confirmed by
reasoning about the loop's own invariants before trusting it.

**`Flow.resumable` needed its own from_json fix, caught before it
shipped, not after** -- the same class of bug this project has hit
more than once now (StateNode.candidates, RunHandle.gap_error):
`RunResult.from_json()` reconstructs `Flow` with named fields, not
`Flow(**d)`, so a new dataclass field is silently dropped on every
reload unless the reconstruction is updated too. Added
`resumable=f.get("resumable", False)` explicitly rather than
discovering the gap by a resumed-then-reloaded flow quietly losing its
own resumability.

**A genuinely new piece of surface for this report: its first
interactive JS.** Every other section of `report.py` is static HTML --
`render_html()` now takes an optional `run_id`, used only to decide
whether to render a "Resume this flow" box (max-depth input, an
allow-mutating checkbox defaulting to the run's own original setting,
a button) on `resumable` cards, and only when one is given: a
standalone CLI report (`flowscout crawl --out ...`, no server behind
it) has nothing to `POST` a resume to, so the box -- and the one
`<script>` block in the whole file -- is omitted entirely rather than
shipped non-functional. Server side: `POST /api/runs/{run_id}/resume`
(`web/app.py`) -> `runs_module.resume_flow_in_run()` (`web/runs.py`),
synchronous (via `asyncio.to_thread`, the same pattern
`/api/detect-fields` already uses for a comparably one-shot Playwright
call) rather than start_run()'s background-thread-plus-poll pattern --
a resume continues from one already-reached state, expected to be much
shorter than a full crawl; a real measurement to revisit if that
assumption turns out wrong, not a guess to build ahead of.

**A real bug caught by clicking the actual button in a real browser,
not just checking the HTML looked right.** First attempt: `flows.json`
correctly showed the new flows on disk, but a real Playwright-driven
click on "Resume this flow" followed by the page's own
`window.location.reload()` kept showing the stale, pre-resume flow
list. Diagnosed as browser caching (`GET /api/runs/{run_id}/report`
carried no cache directive for genuinely dynamic content that changes
under the same URL -- gap uploads, now resume) and fixed with
`Cache-Control: no-store` on that endpoint -- but re-testing showed the
*same* symptom, meaning the real cause was still unexplained. Traced
further by reading the actual `.resume-status` text instead of
trusting a page-title/URL proxy for "did it reload": it still read
"Resuming…" -- the very first test's 3-second wait was simply shorter
than a real server-side Playwright resume (browser launch + replay +
further exploration) takes. Re-tested with real patience (polling up to
90s): genuinely new flows (ids 21-35) appeared after a real click, on a
real page, through a real reload. The `Cache-Control` fix stayed in --
correct regardless of which bug actually explained the first failure,
and it fixes the same staleness risk for every other consumer of that
endpoint, not just this one call site.

**Verified live, the whole chain, more than once:**
- Direct function-level: `crawl()` on saucedemo with `max_depth=6`
  produced 4 `resumable` flows; `resume_flow()` on one of them with
  `{"max_depth": 12}` produced 7 new flows, several genuinely reaching
  past the original 6-step cutoff (up to 8 steps, including `Finish`
  and `Back Home` -- a real checkout completion the original crawl
  never got to), state convergence and semantic dedup both correctly
  ran on the new flows (one via each), and the original blocked flow
  #6 stayed present, unmodified.
- Through the real HTTP API against a running server: same result
  (20 -> 27 flows, 13 -> 14 states), `flows.json`/`report.html` updated
  on disk, confirmed via a fresh `GET`. Error cases checked the same
  way: resuming a non-resumable (`UNIQUE`) flow -> `400` with the exact
  reason; a nonexistent `flow_id` -> `400`; a nonexistent `run_id` ->
  `404`.
- Through a real browser click end-to-end (after the caching
  detour above): 20 -> 35 flows visible in the reloaded report,
  confirming the full path -- button, fetch, server-side resume,
  disk write, reload, fresh render -- works as one real user-facing
  action, not just as separately-tested layers.
- Full regression: the `crawl()` refactor itself (extracting
  `_run_dfs()`) produced byte-identical summary shape on a fresh
  saucedemo run to what this project's numbers looked like before the
  extraction -- confirms the split didn't change the algorithm, only
  where its code lives.

## Change detection: link "new" flows to this run's own gap analysis (done, Aug 2026)

Asked directly whether run-to-run comparison exists at all (M5 already
did -- new/changed/missing, comprehensive, automatic on every crawl),
plus two specific questions about it: does a "new" flow distinguish
genuinely new functionality from something merely unblocked by a bug
fix, and does the report connect a new flow to a matching TCMS item
when one exists ("if it matches a test case, that's fine"). Checked
the code rather than assumed: the first is answered the same way
"missing" already is (deliberately not guessed at -- see below); the
second was a real gap -- `ChangeEvent` for `kind="new"` never carried
a `tcms_id` at all, only "changed"/"missing" did (inherited from a
*prior run's* human-confirmed link). A brand-new flow that happened to
match a TCMS item this same run had no way to say so.

**Built the fix chosen first** (of two real gaps found; the second --
naming a FlowScout-side regression as an explicit possible cause of
"missing", not just an app-side one -- stays open, not addressed here):
`detect_changes()` now takes an optional `gap: GapAnalysis | None`.
When given, "new" events look up their own `flow_id` in
`gap.flow_coverage`, and if gap analysis already scored that flow
`covered` or `partial` against a real TCMS item, its `tcms_id` gets
set -- same field "changed"/"missing" already use, but sourced
differently, and the docstring says so plainly: a prior confirmation
is a *certain* pairing a human made; a "new" flow's match is *this
run's own fuzzy embedding guess*, not confirmed by anyone. Worded that
way in the report too ("Gap-analysis match, not a confirmed link"), not
folded into the same "_confirmed" language `changed_confirmed`/
`missing_confirmed` already use for the real thing.

**Required reordering, not just adding a parameter.** Both call sites
(`web/runs.py`'s `_execute`, `cli.py`'s `cmd_crawl`) ran
`detect_changes()` *before* gap analysis -- harmless before, since
neither needed the other, but exactly backwards for this. Reordered so
gap analysis runs first in both places; `detect_changes()` still runs
before `project_state.record_run()` overwrites what it compares
against, same as always, just later in the sequence than before.

**New `ChangeReport.summary()["new_matched"]`** (mirroring
`changed_confirmed`/`missing_confirmed`'s existing shape) and the
CLI's own change-detection print line updated to show it alongside the
other counts.

**Verified live, two consecutive real crawls of the same project, not
assumed from reading the code alone:** crawled saucedemo once with
`allow_mutating: false` (add-to-cart never becomes a real flow, can't
enter project state) to seed a baseline, then again with
`allow_mutating: true` (add-to-cart becomes real for the first time)
plus a TCMS item describing exactly that. Result:
`{"new": 1, "new_matched": 1, "missing": 1, ...}` -- the newly-unblocked
add-to-cart flow correctly linked to `TC-ADDCART` through this run's
own gap analysis, not left looking as unexplained as a "new" flow with
no TCMS attached at all would. Report re-rendered with a synthetic
matched/unmatched pair of "new" events to confirm the note text
renders correctly for both cases (present when matched, empty
otherwise) without a template error.

**Still open, not addressed in this pass:** naming a regression in
FlowScout's own crawling logic (as opposed to the app under test) as
an explicit possible cause of a "missing" event -- currently
`ChangeEvent`'s own docstring lists "the feature was removed, a real
regression [in the app], or just crawl variance" but not "the crawler
itself changed behavior between runs," even though that's exactly as
real a cause as the other three, and arguably the most pointed one to
call out given how much of `crawler.py` this very session touched.
Deferred at the user's own explicit choice of which gap to start
with, not forgotten.


## HTTP 404/5xx detection for same-domain links (done, Aug 2026)

Asked directly, with a real-world anecdote (Michael Bolton's Canadian
bank FAQ page, where a topic link had no matching anchor and "led to
itself"): what does the crawler do when a link points to itself, and
separately, how does it react to an internal same-domain link that
leads to a 404 page? Investigated both live before building anything.

**Self-link finding: structurally invisible, and correctly so, not a
bug.** `normalize_url()` in `fingerprint.py` always strips URL
fragments (`urlunsplit(..., fragment="")`, hardcoded, no opt-out) --
so a fragment-only navigation is fingerprint-identical to the page
before it, whether the anchor it points to exists or not. A working
in-page anchor link and Bolton's broken one produce the exact same
observation: "clicked, state didn't change." FlowScout has no way to
know the link's *intent*, only its *observed effect*, and inventing a
guess at intent (e.g. flagging every anchor-only link as suspicious)
would violate the project's own standing rule of never asserting
about correctness, only reachability. Given the choice between
building a heuristic for this and building nothing, explicitly chose
nothing here -- see "not addressed" below.

**404 finding: a real, worse gap.** A same-domain link landing on a
genuine server-rendered 404 page was completely invisible: the HTTP
status of a navigation was never captured anywhere in the crawl, so a
404 page just looked like an ordinary new page/state, indistinguishable
in the report from a working one. This one *is* an objective,
observable fact (not a guess about intent), so worth fixing.

**Chosen scope, via explicit user decision: 404/5xx detection only,
not the broken-anchor-link heuristic** (offered as two independent
options; the anchor heuristic stays explicitly deferred, not
forgotten).

**Built:**
- `actions.py`: new `_capture_nav_status(page)` registers a Playwright
  `page.on("response", ...)` listener that records the *main
  document's* own status code (`resp.request.resource_type ==
  "document" and resp.frame == page.main_frame` -- ignores XHR/asset
  responses; last-one-wins across a redirect chain, matching the
  final rendered page). `perform_action()` wraps its existing
  click/select_option call in try/finally around this listener
  (cleanup only -- the click itself is not newly wrapped in a
  swallowing try/except, so a real click failure still propagates
  exactly as before). Returns the captured status as a third tuple
  element: `tuple[dict | None, dict, int | None]`.
- `models.py`: new `Transition.response_status: Optional[int] = None`.
  Documented explicitly as a pure visibility signal: never stops the
  crawl or changes what gets explored, same principle as
  `change_detection`'s "missing" and `gap_analysis`'s "not_found" --
  state the fact, let the operator decide.
- `crawler.py`: `_run_path()`'s return tuple grew from 8 to 9 elements
  (`..., last_choice_state, last_response_status`); both call sites
  (root discovery in `crawl()`, and the main DFS-loop unpack inside
  `_run_dfs()`) updated to match, with `trial.response_status` now set
  alongside `trial.to_fp`/`trial.outcome` at the same point in the
  loop.
- `report.py`: `_flow_steps_html()` now appends a
  `→ page returned HTTP {status}` warning note (same `step-error`
  styling as the existing error-outcome note) whenever
  `t.response_status is not None and t.response_status >= 400`.
  Additive, not a replacement -- a step could in principle carry both
  a revisit/error outcome note and a bad status note.

**Verified live**, not just read: a local fixture server (two links
from a home page, one to a real `200`, one to a real `404`) crawled
end to end. Confirmed `Transition.response_status == 200` for the
working link and `== 404` for the broken one, with `outcome == "ok"`
on both -- exactly the intended behavior of *observing*, not
reclassifying or blocking the crawl. Rendered the report and confirmed
the "→ page returned HTTP 404" note appears in the HTML for the 404
step and nowhere else. Regression: ordinary crawls (saucedemo,
elsewhere in the suite) are unaffected -- `response_status` stays
`None`/`200` and nothing about crawl behavior changed; all 20 existing
tests still pass unmodified.

**Not addressed in this pass, by explicit user choice:** detecting a
broken in-page anchor link (Bolton's original example). Revisited and
built in the very next session turn -- see "Dangling in-page anchor
detection" below; it turned out an objective, non-guessing check was
possible after all.


## Dangling in-page anchor detection (done, Aug 2026)

Direct follow-up question to the 404/5xx feature above: what about the
*other* half of Bolton's original anecdote, the self-referencing FAQ
link itself, not just 404s? Asked plainly: should FlowScout also tell
the operator when an anchor is "empty" (leads nowhere)?

Re-examined the earlier "would require guessing at intent" conclusion
and found it overstated. There IS an objective, present-tense,
non-guessing check available: does the DOM contain an element whose
`id` (or legacy `name`) matches the link's fragment, right now? That's
a structural fact about the page, exactly the same category of thing
as an HTTP status code -- it doesn't assert the link is "wrong" or
guess what the author meant, only that its stated target doesn't
currently exist. What genuinely would have required guessing (and
stayed out of scope) is a *different*, much noisier case: a literal
`href="#"` with no fragment at all, which is an extremely common,
completely legitimate idiom for a JS-driven button/toggle (jQuery/
Bootstrap-era markup especially) -- flagging every one of those would
be almost pure noise, not signal. Put both options to the user
explicitly; **chose "dangling fragment only" (recommended), literal
`href="#"` excluded.**

**Built:**
- `actions.py`'s `_DISCOVER_JS`: new `anchorTargetMissing(href)` JS
  helper, using the browser's own `URL()` to resolve the href (handles
  relative/absolute forms correctly rather than string-splitting by
  hand) and compare it against `location` -- same origin/pathname/
  search required, since a `#frag` on a *different* document targets
  that page's DOM, not the one being checked. Empty fragment (`href="#"`
  or no fragment at all) returns `false` immediately -- the explicit
  scope boundary. Otherwise: `!document.getElementById(frag) &&
  document.getElementsByName(frag).length === 0`. Wired into both
  places an element's `href` is already read (the main a/button/
  [role=button] pass and the div-as-button pool pass -- the latter is
  effectively always `false` since divs don't carry `href`, but kept
  for uniformity/future-proofing rather than special-cased away).
- `models.py`: new `ElementCandidate.anchor_target_missing: bool =
  False`, computed once at discovery time (unlike `response_status`,
  this is knowable *before* the click even happens -- it's a fact
  about the DOM as it stands, not about what clicking did). Copied
  onto `Transition.anchor_target_missing` in `crawler.py` at the same
  point `is_choice` already gets copied from the candidate that
  produced it.
- `report.py`: `_flow_steps_html()` appends a
  `→ this link's anchor target doesn't exist on the page` warning note
  whenever `t.anchor_target_missing` is set -- additive to whatever
  other outcome note the same step already has (a dangling anchor
  transition is *also* always a same-fingerprint "revisit", since
  clicking it causes no real navigation either way, so both notes
  render together in practice).

**Verified live**, three links on one fixture page: a working anchor
(`#topic-a`, a matching `id="topic-a"` element really exists), a
dangling one built to match Bolton's exact scenario (`#topic-ghost`,
no matching element anywhere, label deliberately phrased as "How do I
reset my thing?" to mirror his FAQ wording), and a literal `href="#"`
JS-hook link. Result: `anchor_target_missing` was `True` only for the
dangling link, `False` for both the working anchor and the empty-hash
link -- confirming the scope boundary holds, not just the positive
case. All three transitions still recorded `outcome == "revisit"`
(none caused real navigation, exactly as expected) with the crawl
itself completely unaffected either way. Report HTML confirmed to
render the note text correctly, alongside (not replacing) the
existing "back to an already-explored state" note on the same step.
All 20 existing tests still pass unmodified.


## Known limitation — conjunctive multi-parameter gating is invisible to DFS (investigated live, Aug 2026; not fixed)

Asked directly: how effective is the crawler when a page has many
independent controls (dropdowns/checkboxes/radios) whose *combination*
gates access to other parts of the page -- e.g. 10 parameters where a
hidden section only appears once several of them are set together?
Investigated live, both by reading the code and by building a real
fixture, rather than reasoning about it in the abstract.

**Root cause, from the code:** `state_fingerprint()`
([fingerprint.py](flowscout/fingerprint.py#L69-L71)) hashes the URL
pattern plus the *set of candidate signatures currently on the page* --
not the current *value* of any control. Toggling a checkbox or picking
a `<select>`/radio option that doesn't itself reveal or hide any
element produces the exact same fingerprint as before the click. In
`_run_dfs()`'s main loop
([crawler.py:387-399](flowscout/crawler.py#L387-L399)), a candidate
that lands on an already-known fingerprint is recorded as a "revisit"
flow and the branch stops there (`continue`) -- the next candidate
tried is a *sibling* of the one just clicked, from the *same* original
state, not a continuation past it. Two (or more) parameter choices
only ever end up in the same explored path if **every intermediate
step individually changes the visible candidate set** -- there is no
mechanism to accumulate several simultaneous choices into one
continued branch otherwise.

**Verified empirically, not just from reading the code.** Built a
local fixture: 2 checkboxes + 1 `<select>`, with a hidden link
revealed only when all three are set to specific values *together*
(`cbA` checked AND `cbB` checked AND `selC == "yes"`) -- no single
control, and no partial combination, changes anything by itself.
Crawled it with generous limits (`max_depth: 6, max_breadth_per_state:
20, max_states: 100, max_flows: 100`):

```
States discovered: 1
Flows total: 4
flow 1 [unique]: ['Select "No" in "sel-c"']
flow 2 [unique]: ['Select "Yes" in "sel-c"']
flow 3 [unique]: ['Toggle "Enable A"']
flow 4 [unique]: ['Toggle "Enable B"']
Secret feature EVER discovered as a candidate: False
```

Exactly one state (the root) was ever discovered; every flow tries
exactly one action and stops. The hidden link was never found.
Confirmed separately, via a direct Playwright script setting all three
controls before checking, that the fixture's own logic is correct and
the link genuinely does appear once all three conditions are met --
this is a real gap in the crawler, not a fixture bug.

**When it does work, and its own limits even then.** If each parameter
individually causes *some* observable change (even a small one --
a new candidate appearing, one disappearing), DFS can chain multiple
picks into a deeper, combined path, because each step becomes a
genuinely new state. But even then, full-factorial coverage of many
parameters is not computationally realistic: `max_breadth_per_state`
caps how many candidates get tried from any one state (so some
variants of some parameters are dropped before ever being tried), and
`max_states`/`max_flows` cap the whole run's total exploration budget
-- 10 parameters at even 3-4 options each is already thousands to
millions of combinations, far beyond any practical budget. Exploration
in that regime is a DFS-order-biased *sample* of the combinatorial
space, not exhaustive coverage, even before the conjunctive-gating
case above is considered.

**Not fixed here -- this is a design limitation of DFS-over-fingerprint
itself, not a bug with a small patch.** The real-world technique for
this class of problem is pairwise/all-pairs (combinatorial) testing --
covering every *pair* of parameter values rather than every
combination, which turns exponential growth into roughly quadratic
growth and catches the large majority of real interaction bugs in
practice. Building that would mean a genuinely new exploration
mode (generating specific value-sets and setting them all before
checking page state, not click-by-click DFS) rather than a change to
the existing crawl loop. Not started -- parked here as a real,
verified gap until there's a concrete site/scenario to justify the
investment.


## Real production crawl (alternateqa.com) surfaced three real bugs (done, Aug 2026)

The user reported a real crawl of their own site
(https://alternateqa.com/, a live production app, not a local fixture)
where many flows failed with `Locator.click: Timeout 8000ms exceeded`
against `a[href="/academy"]` and `get_by_text("Get Team License",
exact=True)`, and separately that clicking "Resume this flow" after
raising `max_depth` showed "Resuming…" indefinitely with no visible
result. Investigated by reproducing directly against the live site --
not by reasoning about the error text alone.

**Reproduced live**, a small real crawl of alternateqa.com
(`max_depth: 3, max_breadth_per_state: 8, max_states: 15, max_flows:
15`, `allow_mutating: false`): 3 checkpoints, all genuine, in 90
seconds against a 15-flow crawl -- confirming the site itself is
slow/error-prone enough for this to be a real, recurring pattern, not
a one-off.

**Bug 1 (real crawler bug) -- `build_locator()`'s final fallback
resolves to an unrelated hidden element when text is empty.**
[actions.py](flowscout/actions.py)'s locator strategy falls through
dataTest → id → radio/checkbox → href → `get_by_text(el_meta["text"],
exact=True)`. For an icon-only button with none of the above (no
aria-label, no data-test, no id -- confirmed by the generic `"Click
'button'"` label, meaning label fell all the way to the bare tag
name), this becomes `get_by_text("", exact=True)` -- an empty-string
match, which resolved on the live site to `<div hidden="">`, then
hung for the full click timeout waiting for it to become visible.
**Fixed** in `_build_candidate()`: when dataTest, id, href AND text
are all empty, the element is reported the same way an occluded
candidate already is (`{"label": ..., "reason": "no reliable locator
..."}`-shaped, visible in the report's Safety register, never
explored) instead of building a candidate around a locator guaranteed
to resolve to the wrong element. Re-crawling the identical config
afterward: checkpoints dropped from 3 to 2, the `"Click 'button'"`
error gone entirely, and the freed-up flow budget (max_flows: 15) let
the crawler reach two more real pages instead of burning a slot on a
doomed click.

**Bug 2 (real, but NOT fixed by choice) -- discovery-time occlusion
can go stale by click time.** The `/settings` link's own timeout
detail shows Playwright's own diagnosis plainly: the element *is*
visible/enabled/stable and gets scrolled into view, but a *different*
`<button>` intercepts the pointer event at click time -- most likely a
modal/overlay (the link's own label, "Go to Settings to enter your
key", reads like an API-key-prompt CTA) that appeared *after*
`_DISCOVER_JS`'s own occlusion check ran. FlowScout only checks
occlusion once, at discovery; nothing re-checks right before the
actual click. Considered fixing this too (an explicit design option
offered to the user) but **left alone by explicit choice** -- a
"re-check occlusion right before clicking" heuristic needs its own
investigation on real sites with real overlays to avoid trading one
false negative for false positives elsewhere, and the timeout increase
below (Bug 3) already gives Playwright's own actionability retry loop
more chances to succeed if the overlay is transient.

**Bug 3 (real gap, not really a "bug") -- the 8000ms click/select
timeout was hard-coded, twice, with no way to raise it.**
`perform_action()` had `timeout=8000` baked into both `loc.click()`
and `loc.select_option()` (plus their own `wait_for_load_state`
calls) -- fine for local fixtures and most sites, evidently not always
enough for this one real production deploy. **Fixed:** new
`limits.action_timeout_ms` (default `8000`, identical to every crawl
before this existed), threaded from `_run_path()`'s own
`config.get("limits", {}).get("action_timeout_ms", 8000)` through
`perform_action(..., timeout_ms=...)` to every one of the four
call sites. Exposed in the web UI too (`index.html`'s config form,
"Click/select timeout (seconds)" -- seconds in the UI, milliseconds in
the config, converted both ways) so an operator can actually use this
without hand-editing JSON. **Verified live, directly against
`perform_action()`** (bypassing discovery so the test isn't
confounded by the already-existing discovery-time occlusion filter):
an element made unclickable (`pointer-events: none`) until 2000ms
after load. `timeout_ms=800` failed in 0.8s (`TimeoutError`);
`timeout_ms=5000` succeeded once the element became clickable, in
2.6s total -- conclusively proving the parameter reaches Playwright's
real timeout, not just a config field nobody reads.

**Investigated separately (not a bug) -- `resume_flow()` isn't stuck,
it's just genuinely slow on a slow site.** Loaded the real
alternateqa.com run above and called `resume_flow()` directly
(bypassing the browser/web UI entirely) on its own `"Get Team
License"` flow (matching the user's second reported error) with
`max_depth: 6`: **it completed successfully in 168.4 seconds**,
producing 11 new flows. Not a hang -- a resumed flow replays its
entire path from a fresh browser for every candidate it tries, then
keeps exploring from there, and on a site with repeated 8-second
timeouts that adds up to minutes fast. The real problem is that
[report.py](flowscout/report.py)'s `_resume_script_html()` showed a
completely static `"Resuming…"` for that whole time, indistinguishable
from actually being stuck. **Fixed** with a client-side-only elapsed-
time ticker (`setInterval`, cleared in a `finally`) --
`"Resuming… (47s elapsed — this can take several minutes on slower
sites)"` -- no backend change, since there was nothing actually broken
on the backend to fix. Separately: the current local server's own log
showed zero `POST /resume` requests ever received during this
investigation, meaning the user's original stuck click most likely
happened against a different server session (or the request never
left the browser) -- flagged back to the user to check the browser's
Network tab if it recurs, rather than guessed at further without
evidence.

**All three fixes verified together**: all 20 existing tests still
pass; no Playwright browser processes left running after any of the
live verification crawls (checked via `Get-CimInstance Win32_Process`
for headless/playwright chrome processes -- zero).


## Discovery->click occlusion desync, investigated further (Aug 2026)

Follow-up to the `/settings` finding above: reproduced the exact
mechanism with a purpose-built local fixture rather than only reading
the code. `_DISCOVER_JS`'s occlusion check
([actions.py:121-129](flowscout/actions.py#L121-L129)) is a ONE-SHOT
`elementFromPoint` snapshot taken once, at discovery time; `_run_path()`
never re-runs it before an individual replay step's own click -- it
just loads the saved `el_meta` and calls `perform_action()` directly.

**Fixture**: a link, unoccluded at page-load; a fixed-position overlay
that appears 900ms later (mimicking a first-visit onboarding
modal/toast -- exactly the kind of thing that would appear fresh on
EVERY replay, since FlowScout's own fresh-context-per-path design
guarantees "first visit" every single time). Result, reproduced
on-demand and exactly matching alternateqa.com's own diagnosis:

```
- element is visible, enabled and stable
- <div id="overlay">…</div> intercepts pointer events
```

Discovery correctly said "not occluded" at T0; by T0+1100ms (a
realistic stand-in for replay overhead or other candidates tried
first from the same state) something else had appeared and now blocks
the click. Root cause confirmed, not just theorized.

**Built in a later session turn** (this pass was investigation only at
first, at the user's own explicit request; the fix below followed once
the SAME class of failure recurred live -- `/academy` again, plus a
second real one on the same site, `"Get Team License"`, that hadn't
been root-caused before):

`actions.py`'s new `_wait_until_unoccluded(page, loc, max_wait_ms=2000,
poll_interval_ms=150)`: re-runs the exact same `elementFromPoint`
occlusion test `_DISCOVER_JS` already does at discovery time, right
before `perform_action()`'s `click()`/`select_option()` call. Not a
guess about WHERE things went wrong -- the same check, just asked
again, later. Steps aside (returns "not occluded") for anything it
isn't this function's call to decide: a locator that doesn't resolve
to exactly one attached element, or one currently off-screen -- both
cases let `click()`/`select_option()` raise their own more specific
error instead. If genuinely still occluded, polls every 150ms up to a
grace period capped at `min(2000, timeout_ms)` -- long enough for a
transient toast/animation to clear, short enough that a persistent
overlay (an onboarding modal that never goes away this session) fails
FAST instead of burning the whole click timeout. On timeout, raises a
plain `RuntimeError` naming what's on top (`"element became covered by
'DIV' between discovery and this replay (waited 2000ms for it to
clear) -- not attempted"`) instead of an opaque Playwright stack
trace -- flows through `_run_path()`'s existing error-checkpoint path
completely unchanged, no crawler.py changes needed at all.

**Verified live, three scenarios, not just the happy path:**
- **Reproduced the original bug's exact fixture again**, now WITH the
  fix: failed in **1.9s** (down from the full 3.0s timeout before),
  with the new clear message -- same failure, caught much faster and
  explained instead of a raw stack trace.
- **A transient-overlay fixture** (appears at 900ms, clears again at
  1400ms): the click **succeeded in 0.5s**, correctly waiting out the
  overlay within the grace period rather than giving up the instant it
  saw ANY occlusion -- confirms the bounded retry does its job, not
  just the fast-fail half.
- **Regression check on an ordinary, never-occluded crawl** (the same
  conjunctive-gating fixture from above): identical shape to before
  the fix -- 4 flows, 0 checkpoints, 2.3s total -- confirming the extra
  recheck adds no meaningful overhead to the common, unoccluded case.

All 20 existing tests still pass; no leftover Playwright processes
after any of the live verification runs.


## Occlusion desync, continued: the pre-check and retry weren't enough -- the real bug was at discovery time (done, Aug 2026)

Restarted the server with the bounded-recheck fix above and re-crawled
alternateqa.com again: **both original errors were still there**,
byte-for-byte identical to before -- a real Playwright
`Locator.click: Timeout 8000ms exceeded`, not the new custom message.
The bounded pre-check wasn't even firing its own message, which meant
it wasn't the mechanism actually at play. Investigated further instead
of assuming the fix was just incomplete in scope.

**First refinement: retry the whole click, not just a pre-check.**
Replaying the exact recorded `"Get Team License"` path 5 times in a
row succeeded every time in isolation -- not deterministic, ruling out
a simple reproducible bug in that one action alone. Built
`_click_with_occlusion_retry()`: retries the WHOLE `click()`/
`select_option()` call (not just a snapshot beforehand) whenever
Playwright's own failure explicitly says something intercepts pointer
events, since a `_wait_until_unoccluded()` snapshot taken before the
click starts can't see an overlay that appears as a result of the
click's OWN scroll-into-view step. Total budget still capped at
`timeout_ms`, same as a single un-retried call.

**Still didn't fix `/settings`.** Replaying that exact flow with the
retry wrapper in place still failed, now visibly spending the full
8-second budget across several short probes (confirmed via the
final attempt's own `Timeout 500ms exceeded` -- exactly the tail end
of an evenly-split retry budget). This meant the occlusion here
wasn't transient at all -- something was *permanently* blocking the
target for the whole session, which no amount of retrying would ever
clear.

**Root cause, found by looking, not guessing further:** replayed the
flow's own first two steps manually (`"How It Works"` -> `"Sign In"
(menu)`) and took a screenshot before attempting the third step.
`"Sign In"` opens a real **"Account Access" modal** -- a
`position: fixed` overlay covering the entire page. The target link
(`"Go to Settings to enter your key"`) sits far below the fold
(`bounding box y=1978` against an 800px viewport) -- and
`_DISCOVER_JS`'s own occlusion check has a documented blind spot for
exactly this: an off-screen element is "assumed not occluded" because
scrolling normally reveals it (see the comment already there,
predating this session). That assumption is **specifically wrong for
a fixed-position overlay**: it stays pinned over the viewport at
*every* scroll position, so scrolling to the target never uncovers it.
Discovery promoted a candidate that could never actually be clicked
while that modal was open -- not a replay-timing problem at all, a
**discovery-time false positive**.

**The actual fix:** `actions.py`'s `_DISCOVER_JS` gained
`findBlockingOverlay()` -- scans for any `position: fixed` element
covering at least 90% of the viewport in both dimensions (a modal/
dialog backdrop, structurally). If one exists, EVERY candidate that
isn't a descendant of it is now occluded, regardless of on/off-screen
status -- applied to both the main candidate pass and the div-as-
button pool pass. This is a strictly different mechanism from the
per-candidate `elementFromPoint` check above it (which only ever
applies to on-screen elements) -- it's the missing piece for exactly
the case that check structurally cannot cover.

**Verified on a purpose-built fixture** matching the real bug's shape
(a background link 2000px down the page; a "Sign In" button that
opens a full-page fixed modal): before opening the modal, both `"Sign
In"` and `"Go to Settings"` are ordinary candidates, nothing occluded.
After opening it -- `"Go to Settings"` is now correctly reported as
occluded (`"obstructed by another element (a full-page overlay/modal
(DIV))"`), not silently promoted as a doomed candidate; only the
modal's own `"Close"` button remains clickable, exactly as a real user
would experience the page.

**Verified against the live site, discovery-level, not a saved replay:**
re-crawled alternateqa.com fresh. **Both original errors are gone.**
A different, previously-unseen error surfaced instead (`Toggle "on"`,
an unnamed checkbox -- `input[type="checkbox"][name=""][value="on"]`,
likely ambiguous the same way the very first empty-locator bug was) --
noted as a new, separate finding, not chased further in this pass; the
two errors the user actually reported are confirmed fixed. All 20
existing tests still pass; no leftover Playwright processes.


## User-guided combinations for conjunctive multi-parameter gating (done, Aug 2026)

Direct answer to the limitation documented above ("Known limitation --
conjunctive multi-parameter gating is invisible to DFS"): since
FlowScout can't guess the right combination (that would mean inventing
an expected result, which this project's own core principle never
does), let a human who already knows it hand it over directly.
Designed and built the same session the limitation was found, chosen
explicitly over parking it: web UI (not raw JSON config), reusing the
already-discovered candidates from a prior crawl rather than asking
anyone to author CSS selectors.

**Design, reusing `resume_flow()`'s own proven shape rather than
inventing new machinery:**
- `models.py`: new `ElementCandidate.choice_group` -- the identity of
  the underlying `<select>`/radio-group/checkbox a candidate is an
  option of (the same "base" `normalize_signature` already folded into
  `norm_signature`, just surfaced as its own field). Populated in
  `actions.py`'s `_build_candidate()` for all three native choice
  shapes. **Known scope limit**: NOT populated for handler-discovered
  div-as-button choice groups (Site B's wizard-card style) --
  `_detect_choice_groups()` would need to return a group id per index,
  not just a plain set, to support that; left for later since the
  concrete case that motivated this (checkbox/radio/select) is fully
  covered without it.
- `crawler.py`'s new `_path_to_state(run, state_fp)`: the real,
  already-walked transitions reaching an arbitrary state, derived
  exactly (not guessed) from `StateNode.discovered_by_flow` --
  truncating that flow's own transitions at the first one whose
  `to_fp` matches. Works because DFS always extends the SAME path
  deeper before ever emitting a flow for it, so the discovering flow's
  transitions are guaranteed to contain the target state as a prefix.
- `crawler.py`'s new `explore_combination(run, state_fp,
  candidate_indices, limit_overrides, credentials)`: builds ONE
  synthetic path (the real reach-path, plus one `Transition` per
  selected candidate, in the caller's chosen order), replays it in a
  SINGLE `_run_path()` call (deliberately not one call per candidate --
  no intermediate StateNode gets created for "1 of N set", since those
  are exactly the ordinary revisits DFS can't get past anyway and
  would be noise, not signal), then seeds `_run_dfs()` from whatever
  state that reaches to continue exploring normally, mirroring
  `resume_flow()`'s own "own full budget from here" pattern. Same
  safety invariant as normal DFS: a `Risk.DESTRUCTIVE` candidate is
  refused outright, a `Risk.MUTATING` one only with `allow_mutating`
  -- a human doesn't get to silently bypass this through the UI either.
- `web/runs.py`'s new `explore_combination_in_run()` (loads from disk,
  mutates, writes flows.json/report.html back, marks any existing gap
  analysis stale) and `web/app.py`'s new `POST
  /api/runs/{run_id}/explore-combination` -- same shape as the
  existing resume endpoint, one call, `{state_fp, candidate_indices,
  limits}`.
- `report.py`: new "Test a parameter combination" section -- one card
  per state with 2+ distinct `choice_group`s (a state with fewer has
  nothing to combine: options within ONE group are mutually exclusive
  by definition). Radio buttons for a group with 2+ members, a
  checkbox for a group of exactly one (a lone checkbox, not an
  alternative among several). Same elapsed-time-ticker JS pattern as
  the resume box, in the same `<script>` tag (renamed conceptually,
  not literally, to hold both).

**Verified live, on the exact fixture that originally proved the
limitation** (2 checkboxes + 1 select gating a hidden link, from the
ROADMAP entry above): a normal crawl still finds only the 1 root
state, as documented. Then, directly against the real
`run.states`/`ElementCandidate`s that crawl produced (not
hand-authored test data): picked the 3 real candidate indices
(`choice_group` values confirmed correctly populated: `'sel-c'`,
`'cb-a'`, `'cb-b'`), called `explore_combination()` --

```
After normal crawl: states=1 flows=4
After explore_combination: states=3 flows=15
Secret feature discovered: True
flow 5 [UNIQUE]: ['Select "Yes" in "sel-c"', 'Toggle "Enable A"',
                  'Toggle "Enable B"'] -- User-specified parameter
                  combination -- newly discovered state
```

-- states went 1 -> 3 and the previously-unreachable secret link was
found, with `_run_dfs` correctly continuing to explore past the
unlocked state on its own. Verified the full web-layer path too, not
just the crawler primitive: wrote a real `flows.json` to a scratch run
directory, called `explore_combination_in_run()` exactly as the API
endpoint would, confirmed the on-disk `report.html` got regenerated
with the new flow visible, and cleaned up the scratch directory
afterward. Separately confirmed the rendered report actually contains
the combo-box UI with the correct three `choice_group`s and a "Test
this combination" button. All 20 existing tests still pass; no
leftover Playwright processes after verification.

**Not yet covered, explicitly out of scope for this pass**: multi-page
combinations (set a value on page 1, navigate, set another on page 2)
-- the ROADMAP scenario that motivated this was colocated controls on
one state, and that's what got built; a genuinely cross-page version
would need its own design (which page to navigate to between steps,
how to resolve THAT page's own candidates) rather than reusing this
one directly.


## The "Toggle 'on'" checkbox bug: role="checkbox"/"radio" custom controls (done, Aug 2026)

The `alternateqa.com` re-crawl above surfaced a fourth real error,
noted but not chased at the time: `Toggle "on"`, an ambiguous locator
(`input[type="checkbox"][name=""][value="on"]`) that could match any
unnamed checkbox on the page. Investigated on request, live, rather
than guessing at the mechanism.

**Root cause**: this is the Register form's "Show password" toggle,
built with a component library (Radix UI/shadcn -- judging by the
class names). The REAL, visible, clickable control is a styled
`<button role="checkbox" aria-checked="false" data-state="unchecked">`
-- a native `<input type="checkbox">` sits alongside it purely for
form semantics, and is deliberately non-interactive:
`pointer-events: none; opacity: 0; transform: translateX(-100%)`.
`_DISCOVER_JS`'s `isUsableInput()` checked width/height/visibility/
display/disabled -- never `pointer-events` -- so it promoted the inert
decoy as a normal checkbox candidate, guaranteed to time out on every
click, while never even considering the real button (which the
existing markup selector *does* match, being a plain `<button>`, but
with nothing to reliably identify it -- see below).

**This is a widespread pattern, not a one-off**, so the fix goes
further than excluding the decoy: `_DISCOVER_JS` gained a new
`role="checkbox"`/`role="radio"` discovery pass (skipping literal
`<input>` elements, which the native loops already cover), reading
`aria-checked`/`data-state` for current state and grouping radios by
their nearest `role="radiogroup"` ancestor -- the same shape as the
existing native radio/checkbox/select handling, `is_choice`,
`choice_group` and all.

**Label resolution -- a real complication, resolved by leaning on the
browser instead of reimplementing it.** `inputLabelText()` (used for
native radio/checkbox labels already) gained a last-resort fallback:
a sibling `<label>` anywhere in the same parent, not just `label[for=id]`
or a wrapping `<label>` -- needed because label association for these
components isn't always a formal ARIA link. For the actual REPLAY
locator, `build_locator()` doesn't try to reproduce the browser's own
accessible-name computation in Python -- it asks Playwright's
`get_by_role(role, name=...)` to do exactly that, again, at click time.
Verified directly before relying on it: `get_by_role("checkbox",
name="Show password")` found the element on the live site with zero
matches otherwise achievable via plain CSS, and a subsequent `.click()`
correctly flipped `data-state` from `"unchecked"` to `"checked"`.
(In practice this element also had a real `id` -- `build_locator()`'s
existing id-based branch fires first and is simpler still; `get_by_role`
is the fallback for when no data-test/id exists at all, which does
happen on other sites/elements.)

**Same "no reliable locator" guard as the earlier button/link fix,
applied here too**: if a role-checkbox/role-radio has no data-test, no
id, AND no resolvable accessible name, it's reported the same way an
occluded candidate already is -- never built into a candidate that
would resolve `get_by_role(name="")` against literally any checkbox on
the page, which would just be the exact same class of bug in a new
shape.

**Verified live, end to end:**
- Direct discovery + replay on the real Register form: `"Show
  password"` correctly discovered as `tag: "role-checkbox"`,
  `is_choice=True`, `choice_group="show-password-register"`, labeled
  `"Show password"` (not a generic/empty label). `perform_action()`
  clicked it for real -- `data-state` went from `"unchecked"` to
  `"checked"`.
- **Full re-crawl of alternateqa.com: zero errors.** All three
  originally-reported failures (`/academy`, `"Get Team License"`, and
  this checkbox) are gone in the same run -- 11 states, 15 flows, only
  the expected `max_flows` budget checkpoint, nothing else.
- Regression check on the conjunctive-gating fixture (plain native
  checkboxes/select, no component library involved): identical shape
  to before -- 4 flows, 0 checkpoints, ~2.2s -- confirming the new
  role-based pass doesn't affect ordinary native-input discovery at all.

All 20 existing tests still pass; no leftover Playwright processes
after any of the live verification runs.


## "Resume all blocked flows" (done, Aug 2026)

Asked directly: how many blocked flows can be resumed at once, and can
one button resume all of them instead of clicking "Resume this flow"
per flow? The honest answer to the first half mattered before building
the second: `resume_flow()` reads, mutates and writes back the SAME
`run.states`/`run.flows` graph and the SAME on-disk `flows.json` --
running two resumes concurrently against the same run would race and
silently clobber whichever one wrote last. There is no "N at once" for
a single run; correctness requires exactly one at a time. That's
exactly what a "resume all" button can automate, though -- doing the
same sequential clicking a human would, just without the clicking.

**Built**: `web/runs.py`'s new `resume_all_blocked_in_run(run_id,
depth_increment=5, allow_mutating=None)` -- snapshots which flows are
`resumable` *before* starting (deliberately doesn't chase newly-
discovered blocked flows a resume in this same batch might itself
produce, e.g. hitting a new truncation one level deeper -- bounded,
predictable work per click; a second click picks up whatever's new
afterward), then calls `crawler.resume_flow()` once per flow,
strictly sequentially, writing `flows.json` back after EACH one (so a
crash partway through doesn't lose earlier progress). `depth_increment`
is added to **each flow's own current depth**
(`len(flow.transitions) + depth_increment`), not one flat `max_depth`
shared by every flow -- chosen explicitly over a single shared number,
since resumable flows commonly sit at very different depths and one
number could easily do nothing for whichever ones it doesn't exceed.
`allow_mutating`, when given, is the one setting applied uniformly
across the whole batch -- covers the other real resumable reason (a
risk-policy dead end, not depth truncation) that raising depth alone
never fixes. New `POST /api/runs/{run_id}/resume-all` mirrors the
existing per-flow endpoint's shape, returning a per-flow
`{flow_id, status, detail?}` breakdown alongside the run summary so a
single genuinely-failed site interaction doesn't read as the whole
batch silently doing nothing.

`report.py`: new `.resume-all-box`, shown above the Flows list only
when at least one flow is currently resumable (silently absent
otherwise, not a permanent fixture) -- flow count, a depth-increment
field (default 5), an "Allow mutating" checkbox (defaulting to the
run's own configured value, same convention the per-flow resume box
already uses), and the button itself. Same elapsed-time-ticker UX
pattern as the single-flow resume box, adapted to say what's actually
happening ("processes them one at a time, so it can take a while") and
to summarize the outcome ("Done — N resumed, M failed") before
reloading.

**Verified live** on a purpose-built fixture (three independent
4-page-deep link chains, `max_depth: 2` so each one truncates as its
own BLOCKED, resumable flow): 3 resumable flows found. Ran
`resume_all_blocked_in_run` directly against a real `flows.json` on
disk (not just the crawler primitive) -- all 3 resumed successfully in
one call (`{'flow_id': 1, 'status': 'ok'}` etc.), states 7 → 10, flows
6 → 12, `report.html` regenerated with the new box present. Separately
confirmed *why* new resumable flows appeared afterward wasn't a bug in
this feature at all: one branch's own action-repeat cap (an unrelated,
already-existing feature) kicked in one level deeper, purely an
artifact of this test fixture's own naming scheme (`data-test="a-link-2"`,
`"a-link-3"`, ... collapsing to a shared normalized signature) --
confirmed directly before concluding it wasn't this feature's fault,
not assumed. All 20 existing tests still pass; scratch run directory
and fixture server cleaned up; no leftover Playwright processes.


## Gemini embeddings: batching + retry-on-429 (done, Aug 2026)

Asked directly after hitting Gemini's free-tier quota suspiciously
fast: `Quota exceeded ... embed_content_free_tier_requests, limit: 100`.
The number itself (100 requests/minute on the free tier) checked out
against public docs/community reports -- the real finding was WHY an
ordinary run got there so quickly, found by reading
`semantic_dedup.py`/`gap_analysis.py`, not by assuming the limit was
unusually low: **every embedding comparison in this project issued
one unbatched HTTP call per text.** `semantic_dedup.py` called
`embed_text()` once per unique flow; `gap_analysis.py` called it once
per skipped-candidate, per error, per discovered-but-unwalked
candidate, and once per TCMS item -- for BOTH its action-pool and
navigation-pool comparisons. A single crawl with a TCMS export
attached can easily need 60-150+ individual embeddings, comfortably
exceeding 100/min with zero batching and zero backoff -- not a small
crawl hitting an unusually strict limit, a normal crawl hitting an
entirely avoidable one.

**Verified the actual API contract live before building against it**
(this project's own standing practice) -- a 3-text request against
Gemini's `:batchEmbedContents` endpoint (never used here before;
every call went through the single-item `:embedContent`) returned
`{"embeddings": [{"values": [...]}, ...], "usageMetadata": {...}}`,
vectors in the same order as the requests, exactly the documented
shape.

**Built both fixes chosen together, not either alone:**
- `embeddings.py`: new `_gemini_embed_batch()` (the real
  `:batchEmbedContents` call) and provider-agnostic
  `embed_texts_batch()` -- chunks a text list into groups of at most
  100 before calling a provider's batch implementation once per chunk,
  falling back to one-call-per-text for a provider with no batch
  implementation registered (none today lack one). Order of returned
  vectors matches the input exactly, across chunk boundaries.
- `embeddings.py`: new shared `_post_json_with_retry()`, used by
  every single AND batch call alike -- retries ONLY a 429, only up to
  2 times, honoring the wait time Gemini's own error body already
  names (`"Please retry in 42.86s"`, parsed directly, capped at 60s so
  a client-reported wait can't hang a crawl indefinitely) rather than
  guessing at a fixed backoff or failing outright on the first hit.
  Any other HTTP error still propagates immediately, unchanged from
  before this existed.
- `semantic_dedup.py`: the per-flow `embed_text()` loop now computes
  every unique flow's embedding up front via one `embed_texts_batch()`
  call, then runs the *exact same* incremental representative-
  comparison loop as before -- only where the vectors come from
  changed, not the dedup logic itself.
- `gap_analysis.py`: new `_embed_dict()` helper (batches a `{key:
  text}` mapping through `embed_texts_batch()`, preserving keys) used
  at all seven of the module's individual `embed_text()` call sites --
  skipped candidates, errors, discovered-but-unwalked candidates, TCMS
  items (both diagnosis and main matching passes), action text, nav
  text.

**Verified live, several independent checks, not just "it compiles":**
- Retry logic verified deterministically WITHOUT touching the real
  API (no quota wasted re-triggering a rate limit on purpose): faked
  `urllib.request.urlopen` to raise a real-shaped 429
  (`"Please retry in 1.2s"`) once, then succeed -- confirmed exactly 2
  attempts, 1.20s elapsed, matching the parsed wait time precisely.
- Batch/single consistency verified against the REAL API (small,
  deliberately cheap: 6 calls total): a 5-text batch call plus one
  separate single-text call for a duplicate of `texts[0]`. Identical
  text within the same batch scored `cosine = 1.0000`; the batch
  vector for `texts[0]` against the SEPARATELY single-called vector
  for the same text also scored `1.0000` -- batch and single-item
  embeddings are numerically interchangeable, not just structurally
  similar. Genuinely different texts scored `0.6550`, confirming real
  semantic differentiation still holds.
- Full real crawl with semantic dedup enabled (a 3-branch link-chain
  fixture, 6 unique flows): `"semantic: 6 compared, 3 merged"` -- ran
  to completion with ONE batch call instead of 6 individual ones, no
  errors.
- Full real gap analysis with a small TCMS file (3 items, 2 matching
  real flows, 1 deliberately unrelated): both real matches correctly
  scored `covered` (0.82, 0.79), the unrelated item correctly
  `not_found` (0.00) -- genuinely correct semantic matching on the
  batched path, not just "didn't crash."

All 20 existing tests still pass; fixture server and scratch files
cleaned up; no leftover Playwright processes.


## "Resume all" looked like it did nothing -- it didn't (done, Aug 2026)

Reported directly: resumed 12 blocked flows via "Resume all blocked
flows", it ran for ~800 seconds, the elapsed-time UI eventually
disappeared, but none of the blocked flows changed in the report.
Investigated the user's OWN real run on disk before assuming
anything -- this project's standing practice, not a formality here:
the server log showed `POST .../resume-all` returned a clean `200 OK`,
and `flows.json` was genuinely rewritten (98 new flows added across
the 12 resumes, each one verified directly to be a real extension of
its own blocked flow's exact path, not something unrelated). 11 of the
98 were newly UNIQUE -- e.g. a "Show password" toggle sequence and
deeper navigation into previously-unreached pages. **The backend
worked correctly and found real coverage.**

**Three things combined to make it look like nothing happened, none of
them a backend bug:**
1. The original 12 blocked flows never change -- by design,
   `resume_flow()` only appends new flows, so anyone looking at those
   exact 12 cards afterward would correctly see them unchanged.
2. 87 of the 98 new flows landed as DUPLICATE (revisits to already-
   explored pages) -- and duplicates render inside a collapsed
   `<details>` ("Show N duplicate flows"), closed by default.
3. The "Resume all" box itself always shows the CURRENT resumable
   count (still 12, since the original blocked flows stay
   `resumable=True` forever) -- reads as "stuck at 12" even on a
   completely successful run.

**Built both fixes chosen together:**
- `models.py`: new `Flow.origin_note: str = ""` -- empty for a flow
  from a normal crawl pass, a short human-readable note
  (`"Resumed from flow #5"`, `"Set via a user-specified parameter
  combination"`, `"Continued after testing a parameter combination"`)
  for one an operator action produced. `RunResult.from_json()`'s
  manual `Flow(...)` reconstruction updated too -- checked proactively
  before shipping, the same class of bug this project has hit before
  (a new field silently dropped on reload).
- `crawler.py`: `_run_dfs()` gained an `origin_note` parameter, stamped
  onto every `Flow` its `emit_flow()` closure creates -- `crawl()`'s
  own call leaves it empty (default), `resume_flow()`'s call sets it
  to `f"Resumed from flow #{flow.id}"`, `explore_combination()` sets
  it directly on its own manually-built Flow plus on its own
  `_run_dfs()` continuation call.
- `report.py`: flow cards now show a small chip (`↳ {origin_note}`)
  right in the header when `origin_note` is set -- visible whether the
  card is unique or tucked inside the collapsed duplicates section.
- `web/runs.py`: new `_flow_delta(before, after)` -- `run.summary()`
  snapshots taken before and after the actual operation, diffed into
  `{new_flows, new_unique, new_duplicate, new_blocked}`. All three
  operator-action endpoints (`resume_flow_in_run`,
  `resume_all_blocked_in_run`, `explore_combination_in_run`) now return
  this alongside the run, and `web/app.py`'s three endpoints include it
  in their JSON response.
- `report.py`'s JS: new shared `flowscoutDeltaText(delta)` formats the
  delta into one line (`"6 new flows found (3 new unique)"`); all
  three actions now show it in the final status line BEFORE reloading,
  with a genuine 2.5s pause (`flowscoutSleep`) so the line is actually
  readable instead of flashing for a few milliseconds before the
  reload wipes it -- the same class of oversight `window.location.
  reload()` firing immediately after setting the text would have
  repeated otherwise.

**Verified live, against a real backend call, not just that it
compiles:** a 3-branch fixture (identical shape to earlier tests) --
before: 6 flows, 3 resumable. After `resume_all_blocked_in_run`:
`delta = {'new_flows': 6, 'new_unique': 3, 'new_duplicate': 0,
'new_blocked': 3}`, cross-checked by hand against the run's own
before/after `summary()` totals (`6/6/0/6` after minus the recorded
`before` -- matched exactly). All 6 new flows carried
`origin_note == "Resumed from flow #<id>"`; `report.html` on disk
contained both the badge text and its CSS class. Repeated for the
single-flow `resume_flow_in_run()` too: `delta = {'new_flows': 2,
'new_unique': 1, 'new_duplicate': 0, 'new_blocked': 1}`, badge present.
All 20 existing tests still pass; fixture server and scratch run
directories cleaned up; the user's own real run on disk was read for
investigation but never written to; no leftover Playwright processes.


## Resume-all was re-embedding the whole flow list once per resumed flow (done, Aug 2026)

Follow-up report: hit Gemini's free-tier quota again, and separately
still saw "12 blocked flows" after a resume-all with no visible
change. Investigated the user's OWN real run on disk again rather than
assuming the previous fix hadn't worked. It had: the server log showed
a clean `200 OK`, `flows.json` carried the same shape as the earlier
verified case (98 new flows, 11 newly unique, all 98 correctly tagged
with `origin_note`), and `report.html` on disk genuinely contained the
badge markup and the delta-summary JS. **The resume itself worked --
the "still 12 blocked" perception was the same by-design behavior
already explained (the original 12 never change; the "Resume all" box
always shows the current resumable count) meeting a fresh reminder of
the quota problem, not a new bug in the resume logic.**

**The actual new finding, in `semantic_dedup_status` itself:** `"error
after partial run: ... limit: 1000 ... Please retry in 59.79s"` -- a
**different** quota metric than the last hit (100/minute): this is the
free tier's **1000-requests-per-DAY** ceiling, confirming genuinely
avoidable waste, not just heavy usage. Root cause, found by reading
`crawler.py` again: `resume_flow()` calls `apply_semantic_dedup()`
**unconditionally** at its own end -- and semantic dedup re-embeds
EVERY currently-unique flow in the run from scratch every time it
runs (no caching across calls). `resume_all_blocked_in_run()`'s loop
calls `resume_flow()` once per blocked flow -- 12 times for the user's
own batch -- so ONE click on "Resume all" silently re-embedded an
ever-growing unique-flow list up to 12 TIMES over, burning daily quota
on repeat work before the batch was even half done.

**Fixed:** `crawler.py`'s `resume_flow()` gained
`run_semantic_dedup: bool = True` -- `resume_all_blocked_in_run()`
now passes `False` for every per-flow call inside its loop, then runs
`apply_semantic_dedup()` itself exactly ONCE after the whole batch
completes (same non-fatal try/except as before: a 429 here still
can't take down an otherwise-successful batch). The single-flow
"Resume this flow" button is untouched -- it only ever makes one call,
so there was nothing to batch there in the first place.

**Verified live, actual call counts, not just "it should work":**
patched `apply_semantic_dedup` with a counting wrapper across
`crawler.py` and `web/runs.py`'s own imported references (both modules
bind the name at import time, so both needed patching to actually
intercept every call site). On the same 3-branch fixture used to
verify the earlier delta/badge fix: **resuming all 3 blocked flows now
calls `apply_semantic_dedup` exactly once** (confirmed to have been 3
before this fix, one per resumed flow). Single-flow
`resume_flow_in_run()` checked separately: still exactly 1 call,
unchanged. All 20 existing tests still pass; fixture server and
scratch run directories cleaned up; no leftover Playwright processes.

For the user's own 12-flow case, this turns 12 redundant re-embeddings
of an ever-growing flow list into 1 -- a meaningful cut to how fast one
"Resume all" click can burn through the free tier's daily allowance,
though not a substitute for raising the tier or reducing how often
dedup needs to run at all if usage keeps growing.


## Blocked flows that were successfully resumed now change status (done, Aug 2026)

Direct follow-up: "what's the point of a stale blocked flow sitting
there forever" -- the user's own framing, not a euphemism. Two earlier
fixes (`origin_note` badges, the delta summary) addressed *finding*
the new flows a resume produced; this addresses the complaint at its
actual root: an original blocked flow that's genuinely been continued
past its own truncation point staying marked BLOCKED, resumable, and
counted in "N blocked"/"Resume all" totals forever, indistinguishable
from one nobody has ever touched.

**Chosen design, from the user's own explicit answer, not guessed at:**
the "N blocked" count and "Resume all" box should reflect the truth at
the END of whatever just ran, not a number frozen at the original
crawl. A flow that was blocked but has since been carried further
successfully should change its own status and take its place among
ordinary flows, not linger in a separate "still needs attention"
bucket.

**Built by reusing the EXISTING duplicate mechanism, not inventing a
new status.** A truncated 3-step prefix is genuinely redundant once a
fuller 5-step continuation exists -- the exact same relationship
`_apply_state_convergence` already expresses between flows for an
unrelated reason (shorter path superseded by a longer one to the same
place). `crawler.py`'s `resume_flow()` now snapshots which flow ids
exist before calling `_run_dfs()`; if the resume produced at least one
new flow (a real result, not an immediate re-error or re-block),
the ORIGINAL flow is reclassified: `status = DUPLICATE`,
`duplicate_of` = whichever new flow went deepest, `resumable = False`,
and `dedup_reason` names exactly which new flow(s) superseded it.
`resumable = False` is what actually drops it out of future "N
blocked"/"Resume all" counts, since both read live off `run.flows`,
not a cached figure -- no separate bookkeeping needed. A resume that
itself immediately re-blocks or errors leaves the original genuinely
still blocked, correctly -- nothing to claim resolved there.

Deliberately does NOT gate this on `run_semantic_dedup` -- it's a
structural fact about the flow graph, unrelated to embeddings, so it
runs on every resume_flow() call including every one inside a
resume-all batch, independent of whichever single flow at the end
actually runs the (now de-duplicated, single) semantic dedup pass.

**Verified live, actual before/after flow lists, not just the delta
number:** on the same 3-branch fixture used throughout this feature's
testing -- all 3 originally blocked flows (`#1, #3, #5`) correctly
came back `status=duplicate, resumable=False`, each with a
`dedup_reason` naming its own specific successors (e.g. `"Resumed and
superseded: continuing further produced 2 new flow(s) (#7, #8)"`).
Separately confirmed the "Resume all" box in the regenerated
`report.html` shows the CURRENT truth, not a frozen number: in this
run, exploring further past the resolved originals hit a genuinely
NEW, different dead end (the same action-repeat-cap fixture artifact
already documented earlier in this file, not a bug) -- and the box
correctly showed `"3 blocked flow(s) can be resumed"` for those three
NEW ones, matching `sum(f.resumable for f in run.flows)` exactly,
never the stale original three. All 20 existing tests still pass; no
leftover Playwright processes.


## Forms without a `<form>` element, and `type="button"` submit controls (done, Aug 2026)

Asked directly for a coverage audit: what classes of elements/scenarios
does the crawler structurally miss? The highest-value finding, chosen
to fix first: modern React/Vue apps very commonly build a "form" as a
plain container (no `<form>` tag at all) with a submit control
deliberately marked `type="button"` -- to suppress native submission,
handled via JS instead. **Both halves of this pattern independently
defeated form-filling before this fix**, found by reading the code
rather than assumed:

1. `inForm` (`_DISCOVER_JS`, all six candidate-gathering spots) was
   `!!el.closest('form')` -- with no real `<form>` ancestor,
   `fill_enclosing_form()`/`_read_choice_state()` both short-circuited
   immediately (`if not el_meta.get("inForm"): return`), never even
   attempting to fill anything.
2. Separately, `fill_enclosing_form()` unconditionally excluded
   `el_meta.get("type") == "button"` as "not a submission" (reasoning:
   a `type="button"` inside a real form is very likely a Cancel/
   decorative control). Correct when a real `<form>` exists -- wrong
   when it doesn't, since `type="button"` is often the ONLY way such
   an app marks its actual submit action.

**Fixed both, verified each was independently necessary (fixing only
one still failed the live test):**

- `actions.py`'s `_DISCOVER_JS` gained `closestFormLike(el)`: tries a
  real `<form>` ancestor first, falls back to the closest ancestor
  that actually contains a real `input`/`select`/`textarea` -- capped
  at 200 descendants so this can't walk all the way to `<body>` and
  "find" the whole page as one giant form. Used at all six
  `inForm:` assignment sites (replacing the bare `.closest('form')`).
- New Python-side `_CLOSEST_FORM_LIKE_JS` + `_closest_form_like_handle()`
  (same fallback logic, called via `element_handle.evaluate_handle()`
  since this runs in a separate action-time call, not inside the one
  big discovery script) -- `fill_enclosing_form()` and
  `_read_choice_state()` both now resolve the target's real container
  this way instead of Playwright's `xpath=ancestor::form[1]`, which
  only ever matched a genuine `<form>` tag.
- `fill_enclosing_form()`'s `type == "button"` exclusion is now
  conditional: still excluded when the resolved container is a real
  `<form>` (`container.evaluate("e => e.tagName.toLowerCase() ===
  'form'")` -- the Cancel-button case, unchanged), no longer excluded
  when it's a fallback container (no real `<form>` to make "Cancel" a
  meaningful alternative to begin with).

**Verified live, three fixtures, not just "it compiles":**
- **The motivating case**: a `<div>` wrapping two inputs and a
  `type="button"` submit, wired via `onclick` to navigate to
  `/success` only if BOTH fields were non-empty at click time.
  Before either fix: clicked with both fields empty, landed on
  `/error`, every time. After: `fill_summary = {'username': ...,
  'password': '••••••••••'}`, landed on `/success`.
- **Regression check**: a REAL `<form>` with a genuine `type="submit"`
  Log In button AND a `type="button"` Cancel button. The Cancel click
  still returns `fill_summary = None` and correctly navigates to
  `/cancelled` without touching the fields -- unchanged from before
  this fix.
- **Regression check on the real `type="submit"` button in the same
  fixture**: still fills and submits normally, landing on
  `/submitted?username=...&password=...`.
- **Full live crawl of saucedemo.com** (a real `<form>`-based login,
  unaffected by any of this): reached the inventory page, zero
  checkpoints besides the expected `max_flows` budget note -- no
  regression on the well-established real-form case.

All 20 existing tests still pass; no leftover Playwright processes.
Remaining items from the same audit (iframes, Shadow DOM, native
dialogs, `target="_blank"`/popups, negative-input scenarios being
fingerprint-blind) are real but deliberately not addressed in this
pass -- this was the single highest-value item chosen first.


## Native dialogs (confirm/alert/prompt) and target="_blank"/popups (done, Aug 2026)

Second item from the same coverage audit. Investigated both live
before building anything -- and both matched the audit's own
hypothesis exactly, with the real mechanism confirmed rather than
guessed:

**Dialogs**: Playwright's own documented default with NO `dialog`
listener registered is to silently auto-dismiss every
`alert()`/`confirm()`/`prompt()`/`beforeunload` -- and FlowScout
registered none anywhere. Built a fixture: a "Delete" button gated
behind `confirm("Are you sure you want to delete this?")`. Confirmed
live: the confirm always resolved to Cancel, the page's own `#result`
div said `"CANCELLED"`, and the crawler recorded a completely
unremarkable revisit -- zero signal that a real, consequential dialog
had even appeared. (A first attempt at this same test registered a
passive `page.on("dialog", ...)` listener that never resolved the
dialog itself -- Playwright's behavior changes the moment ANY listener
exists: it stops auto-dismissing and waits for the listener to decide,
so that version hung until timeout. Caught before drawing the wrong
conclusion from a test-script artifact, not from real crawler code.)

**Popups**: a `target="_blank"` link. Confirmed live: clicking it opens
a genuinely new page in the browser context (`len(context.pages)` goes
1 → 2), but the crawler's own `_discover_state()` only ever looks at
the ORIGINAL page object, whose URL never changes -- recorded as an
ordinary revisit, with the entire new tab (and everything reachable
from it) structurally invisible, not merely deprioritized.

**Built, both as pure visibility signals -- same "state the fact,
don't invent behavior" principle as `response_status`/
`anchor_target_missing`:**

- `actions.py`'s new `_capture_dialog(page)`: registers a listener that
  **accepts** every dialog (a deliberate choice, not a softer default)
  while recording its `"type: message"`. Reasoning: risk classification
  and `allow_mutating` already decided, before the click ever happened,
  whether this specific action was acceptable to perform at all -- a
  `confirm()` the app shows immediately afterward is procedurally part
  of THAT SAME action, not a separate decision point. Auto-rejecting
  it (Playwright's own old default) would silently prevent an
  already-opted-into mutating action from ever actually completing --
  the opposite of what `allow_mutating=true` is for.
- `actions.py`'s new `_capture_new_page(page)`: registers a listener on
  the page's own browser context for a new page/tab opening as a
  direct result of the action, records its URL. Does NOT follow it --
  a much bigger architectural change (this crawler explores exactly
  one page per replay path) -- only reports that one appeared. Closes
  on its own when the context does; no separate cleanup needed.
- `perform_action()`'s return signature grew from a 3-tuple to a
  5-tuple (`..., dialog_message, opened_new_page`); threaded through
  `_run_path()`'s own return tuple (9 → 11 elements) and all three of
  its call sites in `crawler.py` (the main DFS loop, `crawl()`'s root
  discovery, `explore_combination()`), the same mechanical pattern
  already used for `response_status`/`anchor_target_missing`.
- `models.py`: new `Transition.dialog_message: str` and
  `Transition.opened_new_page: Optional[str]`.
- `report.py`: two new additive step notes (`"→ dialog: confirm: Are
  you sure..."`, `"→ opened a new tab: <url> (not explored -- crawler
  stays on this page)"`), alongside whatever outcome note a step
  already had, same as `response_status`'s own note.

**Verified live, through the FULL real `crawl()` pipeline, not just
`perform_action()` in isolation:** the same two-fixture page crawled
end to end --

```
flow 1: 'Open "Open in new tab"'
    dialog_message=''
    opened_new_page='http://127.0.0.1:8944/other-page'
flow 2: 'Click "Delete item"'
    dialog_message='confirm: Are you sure you want to delete this?'
    opened_new_page=None
```

-- both fields correctly populated, both new report notes present in
the rendered HTML. Separately confirmed the accept-by-default design
actually works as intended, not just that it doesn't crash: flow 2's
own `end_state_fp` resolved to `/deleted`, not back to `/` -- the
delete genuinely went through, where Playwright's old silent-dismiss
default would have silently cancelled it every time. Regression
check: a full live crawl of saucedemo.com (real login form, no
dialogs/popups involved at all) -- identical shape to before, zero
new checkpoints, confirming the two new listeners registered on every
single action add no observable side effect to ordinary crawls.

All 20 existing tests still pass; no leftover Playwright processes.

## Iframes and Shadow DOM (done, Sep 2026)

Continuing the same code-based coverage audit that produced the
forms-without-`<form>` and dialogs/popups fixes above: grepped the
crawler for any handling of `<iframe>` or shadow roots and found zero
matches for either. Both are common in real apps -- embedded payment
widgets, chat plugins, and third-party auth iframes on one side;
design-system web components (`<my-button>` etc.) built on Shadow DOM
on the other -- and both meant the crawler was structurally blind to
any interactive element living inside one, not merely deprioritizing
it. The user explicitly chose to build both in one pass rather than
split them across turns.

**Investigated live before writing any fix, since the two turned out
to need genuinely different mechanisms:**

- Shadow DOM: confirmed `document.querySelectorAll(...)` does not see
  into an open shadow root at all (`payload['candidates']` came back
  empty for a shadow-hosted button), but `page.locator()` *does*
  pierce shadow roots on its own -- so the missing half was purely
  discovery, not replay.
- Iframes: the opposite split. `page.locator()` does **not** auto-pierce
  an iframe (verified: locator count 0 against a real cross-origin
  iframe), but `frame.evaluate()` reaches cross-origin iframe content
  fine via CDP, and `page.frame_locator(...).locator(...)` can find and
  click cross-origin iframe content too. Rejected the more obvious
  design (walking the DOM chain of iframe tags via
  `frame_locator` nesting, keyed on the iframe's own `data-test`/`id`/
  `src`/nth-index) in favor of a simpler one, verified first in a
  throwaway script: record `frameUrl = frame.url` on any candidate
  found outside the main frame at discovery time; at replay time,
  resolve `scope = page.frame(url=el_meta["frameUrl"]) or page` and
  build the exact same kind of locator against `scope` instead of
  `page`. Confirmed live that `page.frame(url=...)` resolves correctly
  and a locator built on the returned `Frame` finds and clicks
  cross-origin iframe content.

**A real bug found along the way, not by inspection but by a fix that
silently didn't work:** shadow-DOM discovery still failed after
`queryAllDeep` correctly found the button in raw JS testing. Root
cause: `document.elementFromPoint()` (used by the existing occlusion
check to confirm a candidate is actually clickable, not hidden under
something else) does **not** pierce an open shadow root -- it returns
the shadow *host*, not the internal element
(`atPointIsHost: True, atPointIsBtn: False`, checked directly),
contradicting the assumption that Chromium retargets hit-testing
through shadow boundaries. Every shadow-DOM candidate was registering
as occluded by its own host and getting silently dropped. Fixed with
a `deepElementFromPoint(x, y)` helper that recurses through
`ShadowRoot.elementFromPoint()` at each level; added in both places an
occlusion check runs (`_DISCOVER_JS`'s own check, and the separate
`_OCCLUSION_CHECK_JS` used by the replay-time recheck in
`_wait_until_unoccluded()`, which cannot share scope with
`_DISCOVER_JS` since it's a different `evaluate()` call).

While tracing that code path, also caught and fixed a second,
not-yet-triggered bug in the same function: `_wait_until_unoccluded()`
was calling `page.evaluate(_OCCLUSION_CHECK_JS, handle)`, which always
runs the callback in the **main frame** regardless of which frame the
passed element handle actually belongs to -- silently wrong for any
iframe-nested element once iframe support existed. Fixed to
`handle.evaluate(_OCCLUSION_CHECK_JS)`, which runs in the handle's own
frame.

**Built:**

- `_DISCOVER_JS` (`actions.py`): new `queryAllDeep(root, selector)`,
  recursing into every open shadow root; wired into 5 of the 7
  `document.querySelectorAll` call sites (the main a/button/input/
  role=button query, select, radio, checkbox, role=checkbox|radio).
  Deliberately *not* wired into the other 2 (`findBlockingOverlay()`'s
  full-page-overlay detector, and the CDP-based div-as-button pool) --
  an explicit scope limit, not an oversight.
- `discover_candidates()` (`actions.py`): rewritten to loop over
  `page.frames` (main frame first, Playwright's own guaranteed
  ordering), running `_DISCOVER_JS` per frame via `frame.evaluate()`
  and merging the markup-based candidate types across frames, tagging
  any candidate found outside the main frame with
  `el["frameUrl"] = frame.url`. The CDP-based `pool`/
  `legacyUnclassified` div-as-button detection stays main-frame-only --
  extending a per-element CDP DOMDebugger session across frames/shadow
  roots was judged materially bigger and riskier than the rest of this
  pass, so deliberately deferred rather than folded in silently.
- `build_locator()` (`actions.py`): now resolves
  `scope = page.frame(url=el_meta["frameUrl"]) or page` first when
  `frameUrl` is present, then builds the same kind of locator
  (`data-test`/`id`/role/etc.) against `scope` instead of `page`
  directly.
- No `models.py` changes needed -- `frameUrl` rides inside the
  existing generic `el_meta`/selector JSON blob, the same mechanism
  already used for `radioGroup`/`checkboxName`.

**Verified live, through the full real `crawl()` pipeline:**

Shadow DOM -- a fixture page with an ordinary link plus an open
shadow-DOM button that navigates on click:

```
States=3 Flows=2
flow: 'Open "Ordinary link"' -> /ordinary
flow: 'Click "Shadow DOM button"' -> /shadow-clicked
```

Both discovered and clicked correctly.

Iframes -- two genuinely cross-origin HTTP servers (outer page on one
port with an ordinary link plus an `<iframe>` pointing at the other
port; the iframe's own button navigates the iframe itself on click):

```
States=3 Flows=3 Checkpoints=0
flow 1: 'Open "Ordinary link"' -> /ordinary
flow 2: 'Click "Click me (inside iframe)"', 'Open "Ordinary link"' -> /ordinary
flow 3: 'Click "Click me (inside iframe)"' -> /
Root candidates found: ['Ordinary link', 'Click me (inside iframe)']
Iframe button discovered: True
frameUrl recorded in el_meta: http://127.0.0.1:8949/
```

The iframe button is discovered cross-origin via per-frame
`_DISCOVER_JS`, correctly tagged with its own `frameUrl`, and clicked
successfully with zero checkpoints -- confirming `build_locator()`'s
frame resolution plus the existing occlusion-retry/click machinery
works unchanged against a resolved `Frame` object.

Regression check: a full live crawl of saucedemo.com (login form,
dropdown-driven navigation, no iframes or shadow DOM anywhere) --
`States=9 Flows=20`, login succeeded, dropdown-menu flows explored
identically to before, one checkpoint which is the expected
`max_flows` limit rather than a new error. The per-frame discovery
loop adds no observable regression to ordinary single-frame crawls.

All 20 existing tests still pass; both fixture servers and their
ports confirmed shut down; no leftover Playwright/headless-Chromium
processes.

## State fingerprint was blind to validation errors (done, Sep 2026)

The last item flagged in the same audit that produced the three fixes
above, and arguably the most consequential: `state_fingerprint()` is
built purely from `(url_pattern, sorted candidate signatures)` -- it
never looks at page TEXT. Rejecting an invalid form submission
typically keeps the same URL and the same field/submit-button set (the
form is still there, still fillable), so the resulting error state
fingerprints byte-for-byte IDENTICALLY to the pre-submit state. The
crawler read this as an ordinary "revisit" and `_run_dfs`'s main loop
never pushes a new frame to explore past a revisit (see its own
comment at `if new_fp in run.states:`) -- so an entire class of
negative scenarios (bad input, duplicate values, required-field
misses, wrong credentials) was structurally invisible to the crawler,
not merely deprioritized like everything else this audit found.

**Investigated before writing anything, since the obvious first idea
turned out to be wrong:** planned to detect invalid fields via CSS
`:invalid`, then checked it live first. `:invalid` matches an empty
`required` field from the very first page load, before any submit
attempt ever happens -- confirmed live: count stayed at the same
nonzero value both before AND after clicking submit, pure noise, never
actually discriminating "rejected" from "merely untouched". The
`:user-invalid` pseudo-class (Chromium 119+, bundled Playwright
Chromium supports it) is the real discriminator -- verified live on
the same fixture: 0 matches on fresh load, 1 match only after a
genuine submit attempt. Also verified the complementary path: a form
with no native HTML5 constraints at all, whose OWN JavaScript sets
`aria-invalid="true"` and a `role="alert"` banner after a failed
client-side check -- both went from absent to present only after the
actual submit click, never before.

**Built:**

- `_DISCOVER_JS` (`actions.py`): new `validationSignals` gathering,
  added to the same per-frame payload already produced for candidate
  discovery (so it's picked up inside iframes/shadow roots too, for
  free, via the existing `queryAllDeep`/per-frame loop above) --
  `invalid-field:<name>` for every `[aria-invalid="true"]` element,
  `alert:<text>` for every non-empty `[role="alert"]` region (via the
  existing `firstBlockText` helper), `native-invalid:<name>:<message>`
  for every `:user-invalid` element (guarded in a try/catch, since
  pseudo-class support can't be assumed forever) -- deliberately NOT
  plain `:invalid`, for the reason above.
- `discover_candidates()` (`actions.py`): now returns a 5th element,
  `validation_signals` (merged across every frame, each non-main one
  prefixed with its `frameUrl`).
- `_discover_state()` (`crawler.py`): feeds `validation_signals`
  straight into `state_fingerprint()` alongside the ordinary candidate
  signatures -- the actual fix. A rejected submission now genuinely
  becomes its own state.
- `Transition.validation_errors: str` (`models.py`): the joined
  signal string observed after the action, threaded through
  `_run_path()`'s return tuple (11 -> 12 elements) and all three call
  sites (main DFS loop, `crawl()`'s root discovery, `explore_combination()`),
  the same mechanical pattern already used for
  `response_status`/`dialog_message`/`opened_new_page`. Unlike those
  three, this one isn't purely observational -- it also changes the
  fingerprint, so it gets its own paragraph in `_run_path()`'s
  docstring saying so.
- `report.py`: a new step note, styled as an error (not a plain note
  like `dialog_message`/`opened_new_page`) since surfacing exactly
  this -- what the app does with bad input -- is the whole point of a
  QA tool, not an incidental fact.

**Verified live, through the full real `crawl()` pipeline, twice:**

A fixture with an ordinary link plus a form whose own JS validation
requires a "promo code" field to be digits-only -- the crawler's
generic auto-fill value (`"flowscout_test"`, not digits) fails that
check every real time, so this happens on every single run, not by
chance:

```
States=3 Flows=4 Checkpoints=0
flow 1 (unique): 'Open "Ordinary link"' -> /ordinary
flow 4 (unique): 'Fill form and submit "Apply promo" (promo_code="flowscout_test")' -> /
    validation_errors: 'invalid-field:promo_code; alert:Promo code must be digits only'
flow 3 (duplicate): [submit, submit] -> /
    validation_errors: ... (both steps)
flow 2 (duplicate): [submit, "Open Ordinary link"] -> /ordinary
```

Three distinct fingerprints (root / error-state / `/ordinary`), not
two -- before this fix, "Apply promo" would have fingerprinted
identically to the root and ended the branch immediately as a
revisit. Instead the crawler correctly kept exploring FROM the error
state: submitting again, and successfully following the ordinary link
away from it, exactly like it would from any other real state.

Second, against a real production site, not a fixture: submitted
saucedemo.com's own login form with a wrong password.
`discover_candidates()`'s `validation_signals` came back
`['alert:Epic sadface: Username and password do not match any user in']`
-- saucedemo's real error banner (`<h3 data-test="error" role="alert">`)
matched the `role="alert"` signal directly, no fixture involved. The
canonical "wrong credentials" negative test case, previously invisible
to the crawler for exactly the reason above.

Regression check: a full saucedemo.com crawl with a *correct* login --
`States=9 Flows=20`, identical shape to the pre-fix baseline, same
single `max_flows` checkpoint. No aria-invalid/role=alert/native-invalid
elements appear anywhere in that flow, so `validation_signals` is
empty at every state and the fingerprint is completely unaffected --
confirming the fix is additive, not a behavior change for sites
without validation errors.

**Disclosed limitation, not fixed in this pass:** a site with a
persistent, unrelated `role="alert"` region always present regardless
of any action (a rotating ad banner, say) would fold its own changing
text into every state's fingerprint and could cause spurious
duplicate states. Not observed on any real site tested against so
far; considered an acceptable, documented trade-off rather than reason
to abandon the fix, the same way this project already accepts
`_norm_token`'s coarser tradeoffs elsewhere.

All 20 existing tests still pass; both fixture servers and their
ports confirmed shut down; no leftover Playwright/headless-Chromium
processes.

## Direct URL seeding -- sitemap.xml and an explicit list (done, Sep 2026)

Next item in the same audit's ordering: the crawler can only ever find
what it can DFS its way to by clicking from `start_url`. A page with
no inbound link anywhere in the crawled UI -- a deep-linked SPA route,
an old promo landing page still live but delisted from navigation --
is structurally unreachable no matter how thoroughly the rest of the
app is explored, with no way to tell the crawler "this page exists
too" short of it happening to be linked from somewhere.

**Design, decided before writing code:** rather than inventing a
parallel "visit this URL and stop" code path, represent a seed URL as
a synthetic pseudo-candidate (`{"tag": "direct-nav", "href": url}`)
that reuses the *exact same* Transition/`_run_path()`/`_run_dfs()`
machinery every ordinary click already goes through. `perform_action()`
recognizes the tag and does `page.goto(el_meta["href"])` instead of
locating and clicking a DOM element; everything downstream (state
discovery, fingerprinting, risk display, replay, resume) needed zero
special-casing because a seed URL's arrival is just an ordinary
1-step Transition like any other, with an empty `from_fp` being the
only tell that it didn't come from clicking anywhere already known.
This means a seed URL is genuinely explored, not just visited: the
crawler clicks around from wherever it lands, exactly like it does
from `start_url` itself.

**Built:**

- `actions.py`: `perform_action()` gains a `tag == "direct-nav"`
  branch, before `build_locator()` ever runs (there's no DOM element
  to locate). Reuses the same nav-status/dialog/new-page capture as an
  ordinary click -- a direct `page.goto()` can land on a 404, trigger
  a `beforeunload`, etc., same as any navigation. `describe_action()`
  gains a matching branch (`Open URL directly: "<path>"`).
- `crawler.py`: `_fetch_sitemap_urls()` (stdlib `urllib.request` +
  `xml.etree.ElementTree`, matching this project's existing
  no-new-HTTP-dependency convention from embeddings.py) fetches and
  parses either a plain `<urlset>` or a `<sitemapindex>` of child
  sitemaps, recursed up to 2 levels. `_resolve_seed_urls()` merges
  `config["seed_urls"]` (explicit) with whatever the sitemap yielded,
  then filters exactly like an ordinary candidate href already is (see
  risk.classify): dropped if its domain isn't in `allowed_domains`,
  dropped if its path matches an `exclude_patterns` glob, dropped if
  it's just `start_url` itself (nothing new to seed there). Capped at
  `config.get("max_seed_urls", 50)` -- a real sitemap can list
  thousands of URLs, and each seed's own exploration costs the same as
  crawling an entire extra site, so raising the cap is a deliberate
  operator choice, not an accident. A fetch/parse failure is recorded
  as a checkpoint, not raised -- seeding is additive to an otherwise-
  normal crawl, so one broken sitemap shouldn't sink the whole run.
- `crawl()`: one additional root-like `_Frame` per resolved seed URL,
  pushed onto the same DFS stack as `root_frame` -- each starts its
  own full exploration from wherever that URL lands. Discovered once
  (by the first persona) and reused by every later one, same
  "provably persona-independent" reasoning `root_fp` itself already
  relies on: a direct-nav transition ignores `credentials` entirely,
  so what a seed URL shows depends only on the URL, never on which
  persona is walking.
- `report.py`: a seed transition's `from_fp` is `""` -- no state to
  look up, by design, since it's an additional entry point rather than
  something reached by clicking from anywhere already discovered. A
  bare "?" would read as a bug; special-cased to show
  "(direct navigation)" instead.
- Web UI (`index.html`): a new "Direct URL seeding" section (Sitemap
  URL + Additional seed URLs fields), wired into the same load/reset/
  collect functions every other config field already goes through, plus
  a matching help-overlay entry -- kept in sync with the JSON schema
  the same way `allowed_domains`/`exclude_patterns` already are.

**Verified live, through the full real `crawl()` pipeline AND the
actual HTTP API (not just the Python function directly):** a fixture
site whose home page links to `/linked` only -- `/hidden` and its own
child `/hidden-child` have no inbound link anywhere, reachable only
via a `sitemap_index.xml` that recurses into `sitemap1.xml`, which
also lists `/excluded` (to prove exclude_patterns filtering) and an
external-domain URL (to prove domain filtering):

```
States=4 Flows=3 Checkpoints=0
flow 1: 'Open URL directly: "/hidden"', 'Open "Hidden child"' -> /hidden-child
flow 2: 'Open URL directly: "/hidden"' -> /hidden
flow 3: 'Open "Linked page"' -> /linked
```

`/hidden`/`/hidden-child` correctly discovered and explored, `/excluded`
correctly never visited, the external URL correctly dropped, `/`
correctly deduped against `start_url` itself (no redundant "Open URL
directly" flow for the crawl's own home page). Separately verified:
`max_seed_urls` caps `_resolve_seed_urls()`'s own output exactly as
configured; an unreachable `sitemap_url` degrades to a single
checkpoint with the crawl still completing normally, not an exception;
and the identical config posted through the real running server's
`/api/runs` endpoint (not called as a Python function) produced the
same `States=4 Flows=3`, with the rendered HTML report showing both
the "Open URL directly" label and the "(direct navigation)" origin
note.

Regression check: a full saucedemo.com crawl (a config with neither
`seed_urls` nor `sitemap_url` at all) is byte-for-byte unchanged in
shape from the pre-feature baseline -- `_resolve_seed_urls()` returns
an empty list and the per-persona seeding loop is a no-op, exactly as
it was before this existed.

All 20 existing tests still pass; the fixture server and its port
confirmed shut down; the test run created through the live HTTP API
removed from `runs/`; no leftover Playwright/headless-Chromium
processes.

## Recognize CAPTCHA/challenge pages and report them as Blocked (done, Sep 2026)

Prompted by a real Cloudflare email about changes to its AI-crawler
controls, which led to the actual question worth answering: FlowScout
itself isn't affected by that specific policy (it doesn't send any
declared-AI-bot signature -- verified in the code: no `user_agent` is
set anywhere, so Playwright's own stock Chrome UA goes out unchanged),
but a genuinely adjacent, pre-existing gap surfaced from the
discussion -- the crawler had zero handling for a CAPTCHA or bot-
mitigation interstitial. It would discover whatever candidates a
challenge page happens to expose and explore it like any other state,
with the encounter itself never surfacing anywhere in the report.

**Explicit, deliberate non-goal, decided before writing anything:**
FlowScout will not attempt to solve or bypass a CAPTCHA. A third-party
CAPTCHA-solving service (human-solver farms, ML-based solvers) doesn't
distinguish "this is the site's own owner testing it" from "this is
someone scraping a site they don't control" -- building that capability
into an autonomous crawler is building a general anti-bot-evasion tool,
not a QA tool, regardless of this project's own intended use. The right
answer for a real test environment is disabling CAPTCHA there entirely,
or using the vendor's own official test/dummy sitekeys (Cloudflare
Turnstile and reCAPTCHA both publish ones made exactly for automated
testing) -- outside this codebase's own scope to build, since it's a
target-site configuration choice, not a crawler feature. What IS this
project's job: recognize a challenge when the crawler hits one, and
say so clearly instead of pretending it's ordinary content.

**Detection, standards-based only, same discipline as validationSignals
before it -- never a guess about any one site's own markup:**

- `iframe[src]` matching `recaptcha`/`hcaptcha.com`/
  `challenges.cloudflare.com` -- each vendor's own required embed
  mechanism.
- `.g-recaptcha`/`[data-sitekey]`/`.cf-turnstile` -- each vendor's own
  documented widget-container class/attribute, mandated by their own
  integration instructions, not this project's guess about any site's
  CSS conventions.
- `script[src]` containing `/cdn-cgi/challenge-platform/` -- catches
  Cloudflare's own full-page "Just a moment..." interstitial, which
  has no ordinary content for a widget marker to sit inside at all.

**Disclosed verification limit, stated plainly rather than glossed
over:** the first two categories were verified live end to end through
the real `crawl()` pipeline against a fixture built to carry each
vendor's documented marker. The Cloudflare interstitial script-path
marker was matched against Cloudflare's own publicly documented
structure, NOT verified against a real, live Cloudflare challenge --
this project has no Cloudflare-protected site to test against. If this
turns out to misfire in practice, the fix is narrowing or correcting
one pattern in `_DISCOVER_JS`, not a design change.

**Built:**

- `_DISCOVER_JS` (`actions.py`): the markers above, gathered per frame
  (so a CAPTCHA embedded in an iframe -- e.g. a login form's reCAPTCHA
  widget -- is caught too, not just a whole-page interstitial),
  deduped via a `Set`, returned as `captchaSignals`.
- `discover_candidates()` returns a 6th element, `captcha_signals`,
  merged across frames (each non-main one prefixed with its
  `frameUrl`, same convention as `validation_signals`). Deliberately
  NOT fed into `state_fingerprint()` -- unlike a validation error, the
  crawler doesn't want to keep exploring a CAPTCHA state at all, so
  fingerprint stability there doesn't matter; it forces the flow to
  BLOCKED instead.
- `Transition.captcha_detected: str` and `StateNode.captcha_detected:
  str` (`models.py`) -- stored on the STATE too, not just the one
  Transition that happened to discover it first (see the bug below for
  why that distinction turned out to matter). `Transition.outcome`
  gains a new value, `"blocked"`.
- `crawler.py`, all four places a new state gets discovered:
  - `_run_dfs`'s main loop: a genuinely new state behind a CAPTCHA
    marker is recorded (evidence of exactly where the crawl got
    challenged) but no `_Frame` is pushed for it -- nothing legitimate
    to click on a challenge page, and trying risks interacting with
    the CAPTCHA widget itself rather than the app under test. The
    flow is forced to `FlowStatus.BLOCKED` with the marker(s) named in
    its own reason text.
  - `crawl()`'s root discovery: if `start_url` itself is behind a
    CAPTCHA, there's no Transition to attach a Blocked flow to (an
    empty path never reaches `emit_flow`) -- recorded as a Checkpoint
    instead, and the crawl stops entirely (root is shared across every
    persona, so nothing downstream is explorable for any of them).
  - `crawl()`'s seed-URL discovery (see the direct-URL-seeding entry
    above): a seed landing on a CAPTCHA DOES have a real path/label
    worth its own Blocked flow -- built directly (the same "construct
    a Flow without `_run_dfs`'s own `emit_flow` closure" pattern
    `explore_combination()` already uses), and cached as
    `(fingerprint, captcha_signal)` together so a LATER persona reusing
    this seed doesn't silently lose the finding.
  - `explore_combination()`: same BLOCKED treatment, checked BEFORE
    the plain "already-known state" branch rather than only in an
    `else` -- see the bug below for why the ordering itself matters.
- `report.py`: a `"blocked"` outcome gets its own per-step note; a new
  `captcha_detected` note (styled as an error, additive to whatever
  else the step already says) mirrors `validation_errors`' own
  treatment, visible both inline per-step and in the flow card's own
  "Blocked" pill + reason text.

**A real bug found by testing the fix, not by inspection:** the first
working version only forced `FlowStatus.BLOCKED` on the flow that
discovered a CAPTCHA state for the FIRST time. A live test proved this
wrong: seeding `/verify` (the CAPTCHA page) directly via `seed_urls`,
in the SAME crawl where an ordinary click path also happens to reach
that identical page, showed the click path's own flow as an
unremarkable "unique" -- not blocked -- purely because some OTHER
branch had already discovered that fingerprint first. Root cause:
`captcha_detected` was only checked on first discovery, never on the
ordinary "already-known state" revisit path every later branch
reaching the same state takes. Fixed by checking the CURRENT replay's
own freshly-computed `captcha_detected` (not a cached value -- every
replay re-runs `_discover_state()` from a fresh browser context, so
it's just as reliable) in the revisit branch too, in both `_run_dfs`'s
main loop and `explore_combination()` -- every flow that ends on a
CAPTCHA state now reads as blocked, regardless of which path reached
it, or in what order.

**Verified live, through the full real `crawl()` pipeline, all three
discovery paths in one fixture:**

```
1) main DFS loop -- click "Go to form" -> "Submit" -> lands on /verify:
   flow 1 (blocked): 'Open "Go to form"', 'Click "Submit"'
     reason: Blocked by a CAPTCHA/challenge page (reCAPTCHA (iframe);
     CAPTCHA widget marker (data-sitekey); Cloudflare Turnstile widget
     marker; Cloudflare challenge interstitial) -- never explored further
   flow 3 (unique): 'Open "Ordinary link"'   <- unrelated flow unaffected

2) seed_urls seeding /verify directly, in the SAME crawl as (1):
   flow 1 (blocked): 'Open URL directly: "/verify"'
   flow 2 (blocked): 'Open "Go to form"', 'Click "Submit"'   <- the
     revisit-branch fix: reaches the SAME state as flow 1, correctly
     blocked too, not read as an unremarkable "unique"

3) start_url itself is the CAPTCHA page:
   States=1 Flows=0 Checkpoints=1
   checkpoint: start_url is itself behind a CAPTCHA/challenge page --
   crawl stopped, nothing else is explorable
```

All four detectable markers correctly named together in one reason
string; the unrelated "Ordinary link" flow unaffected; the CAPTCHA
state recorded (visible in the graph as evidence) but never explored
past. Regression check: a full saucedemo.com crawl (no CAPTCHA
anywhere) is byte-for-byte unchanged in shape from the pre-feature
baseline.

Caught proactively while wiring this up, before it could cause a real
bug: `RunResult.from_json()` reconstructs `StateNode` manually (not via
`StateNode(**d)`) -- exactly the recurring bug class this project
already watches for (`Flow.origin_note` needed the same explicit
`.get()` treatment earlier). Without it, `captcha_detected` would have
silently vanished from every StateNode reloaded from a saved
`flows.json` (`flowscout gap`, `flowscout confirm`, the web UI's gap
re-run) -- fixed with an explicit `s.get("captcha_detected", "")`,
verified with a direct round-trip test before moving on.

All 20 existing tests still pass; the fixture server and its port
confirmed shut down; no leftover Playwright/headless-Chromium
processes.

## Validation-error text was silently truncated at 60 characters (done, Sep 2026)

Found directly from a live question: a user manually added a persona
through the web UI, its login legitimately failed (see below for why),
and the report showed `alert:Epic sadface: Username and password do
not match any user in` -- clearly cut off mid-sentence, not the real
saucedemo message. Checked `flows.json` directly, not just the
rendered report, to rule out report.py truncating it for display: the
STORED string was already exactly 66 characters (`"alert:"` + 60
truncated characters), proving the cut happened at discovery time in
`_DISCOVER_JS`, not at render time.

Root cause: the `[role="alert"]` and `:user-invalid` branches of the
validation-signal detection (added for "State fingerprint was blind to
validation errors", above) reused `firstBlockText()` -- a helper
deliberately designed to be locator-safe-SHORT (60 chars, first line
only) for candidate LABELS, since it feeds `page.get_by_text()`
locators and needs to stay short and unambiguous. Reusing it for
diagnostic ERROR TEXT was the wrong tool for the job: a QA engineer
reading a validation error wants the whole message, not a
label-shaped fragment of it.

Fixed with a dedicated `fullErrorText(raw)` helper -- same whitespace
collapsing as `firstBlockText`, but capped at 500 characters (not 60)
and not restricted to the first line/block. 500 is a deliberate,
generous safety net against a pathological `role="alert"` region that
isn't really a short message at all, not a real constraint on any
actual human-written validation message. Verified live against the
real saucedemo.com login error that prompted this: raw `innerText` is
`"Epic sadface: Username and password do not match any user in this
service"` (76 chars) -- previously truncated to `"...any user in"`,
now captured in full end to end through the real `crawl()` pipeline,
landing in `Transition.validation_errors` untouched.

All 20 existing tests still pass; a full saucedemo.com crawl with
correct credentials is unchanged in shape from the pre-fix baseline.

### Separately, the persona that triggered this: why its login failed

Investigating the actual run that surfaced the truncation bug also
answered the user's other question -- why a manually-added persona's
login failed at all, despite believing a real password was entered.
The saved run config showed `"persona-1": {"credentials": {}}` --
completely empty, not just wrong. With no credential keys to match,
`_synth_value()` (`actions.py`) filled the form with its generic
fallback values (`user-name="flowscout_test"`, a synthesized
password) instead of anything resembling a real saucedemo account --
correctly rejected, exactly as any other invalid-login attempt would
be. Not a crawler bug: FlowScout behaved correctly given what it was
actually told.

Most likely mechanism, traced through the web UI's own
`collectConfig()` (`index.html`): a persona's credential row only gets
included if its FIELD-NAME input is non-empty --
`if (kIn.value.trim()) creds[kIn.value.trim()] = vIn.value;`. Typing a
password into the VALUE box while leaving the field-NAME box blank (a
plausible slip -- `addPersonaBlock()` starts both boxes empty, with no
placeholder text hinting a value alone isn't enough) gets the whole
row silently dropped, with no warning anywhere that it happened.
**Not yet fixed** -- flagged here as a real, disclosed UX gap
(surfacing a warning when a persona ends up with empty credentials
despite the operator having added one) rather than something guessed
at and patched blind; a concrete next step if wanted.

## Credential matching ignored data-test/data-testid entirely (done, Sep 2026)

A direct follow-up to the persona investigation above, once the
*actual* cause of that failed login was found (empty credentials, not
this) -- a separate, real question came out of it: the user's config
used the key `"username"`, saucedemo's real field is
`name="user-name"`/`id="user-name"` (the hyphen defeats a substring
match against `"username"`), and the field's own
`data-test="username"` -- exactly what a QA engineer opens devtools
and looks at first, ahead of `name`/`id` -- was never even consulted.
`fill_enclosing_form()` (`actions.py`) only ever built its matching
string from `name || id || placeholder` (first non-empty wins, the
rest discarded entirely, not even combined) -- so a config key that
matches a field's `data-test` but nothing else silently gets the
generic synthetic fallback value instead, with no error, no warning,
just a login that quietly never succeeds.

**Fixed** by building a separate `match_key` string for
`_synth_value()`'s own substring matching -- ALL of `name`, `id`,
`placeholder`, `data-test`, `data-testid` joined together, not just
whichever one wins a first-non-empty preference order. The report's
own `display_name` (what shows up in a flow's "Fill form and submit
... (user-name=...)" label) keeps the original `name || id ||
placeholder || data-test` preference order unchanged -- this only
widens what MATCHES, not what gets displayed.

Also extended to stay consistent with what actually gets matched:
- `field_detect.py`'s `_FIELD_SCAN_JS` ("Detect fields from site") now
  reports each field's `data-test`/`data-testid` alongside
  name/id/placeholder, so the suggestion tool doesn't quietly omit the
  one attribute a QA engineer is most likely to already know.
- The web UI's own "+ use" suggestion (`index.html`) falls back to it
  last, after name/id/placeholder, matching `display_name`'s own order.
- The Credentials help-overlay text was also just plain wrong before
  this: it claimed a key of `user-name` "also matches an input
  literally named `username`" -- backwards from how substring matching
  actually works (neither is a substring of the other; the hyphen
  defeats it in both directions, which is the exact real bug that
  prompted this whole investigation). Rewritten with an example that's
  actually true (a key of `user` matches `user-name`, `username`, AND
  `data-test="username"` alike) and a pointer at "Detect fields from
  site" instead of guessing.

**Verified live** by reproducing the user's own exact failure first --
`credentials: {"username": "standard_user", "password": "secret_sauce"}`
against real saucedemo.com produced the identical symptom confirmed in
their own saved `flows.json` (login filled with the generic
`flowscout_test` fallback, stuck at 2 states / 3 flows around the
login page) -- then confirming the fix: the exact same config now logs
in successfully and reaches the ordinary ~9-state, ~15-flow shape.
`detect_fields()` against the same site now reports
`"dataTest": "username"` alongside `name`/`id`/`placeholder`.
Regression check: the original working config (`user-name` key)
crawled against saucedemo.com is unchanged in shape.

All 20 existing tests still pass; no leftover Playwright/headless-
Chromium processes; no stray run artifacts (the verification scripts
called `crawl()` directly, never wrote to `runs/`).

## Auth-walled apps: seed URLs redirected to login, and a crash on network failure (done, Sep 2026)

A second, much more technical piece of LinkedIn feedback: a user
piloted FlowScout against OrangeHRM (a real, complex Vue-based HR
SPA -- the public demo instance, not a fixture), reporting three
concrete issues with logs and a patch diff attached. Investigated by
reproducing all of it directly against the same live site
(`opensource-demo.orangehrmlive.com`, public `Admin`/`admin123` demo
credentials) before touching any code, per this project's own standing
discipline.

**Issue 1 -- seed_urls all redirected to /auth/login.** Reproduced
exactly: 5 seed URLs pointed at auth-walled Admin/PIM pages, all 5
collapsed into one indistinguishable "reached the login page" flow.
Root cause: `_run_path()` visits every seed URL from a brand-new,
unauthenticated browser context (this project's own reset+replay
design, deliberately isolated per path) -- `credentials` alone can't
fix this, since filling and submitting a login form is itself an
ACTION this crawler only ever performs by exploring to it, not
something a bare direct-nav step does on its own.

The user's own patch worked around it by finding whichever root-
discovered candidate's label or norm_signature contained the substring
"login" and replaying it before each seed -- and reported the
predictable symptom: "Login now shows in flows but stays revisit vs
manual ok -- likely locator." **Not adopted** -- this is a guess about
which candidate is the right one, AND an English-text-dependent
heuristic, the exact class of fragility this project already rejected
once for field_detect.py's own login-trigger matching (see that
module's own docstring). Built `config["storage_state"]` support
instead (a path to a Playwright storage-state JSON file, or the state
dict inline -- Playwright's own `new_context()` accepts either form
natively): every fresh context, root discovery included, starts
already logged in, so ordinary DFS naturally reaches an authenticated
app's real content and every seed_urls entry lands on its actual
destination -- no heuristic about which candidate is "the" login
control, no English-text dependency, no separate login replay step at
all. Verified live: with a real saved OrangeHRM session, both seed
URLs (`/admin/viewAdminModule`, `/pim/viewPimModule`) correctly landed
on their real destinations (`/admin/viewSystemUsers`,
`/pim/viewEmployeeList`) as two distinct, correctly-labeled flows,
where before they collapsed into one login-page duplicate.

**Issue 2 -- "SPA nav blocked" navigating into Admin/PIM.** Traced to
two compounding causes, both found live, neither guessed:

1. A genuine SPA-rendering timing gap: OrangeHRM's dashboard is a
   Vue app whose real nav only appears after its own async data-fetch
   resolves, well after Playwright's `load` event fires. Measured
   live: 0 real candidates 200ms after `load` (this crawler's own
   flat wait at the time), 34 real ones roughly 3 seconds later.
   Neither `page.wait_for_load_state("load")` (fires on the initial
   HTML/JS/CSS, before the app's own JS has fetched or rendered
   anything) nor the existing `_settle()` (CSS animations only, not an
   XHR-driven re-render) covered this at all.

   First fix attempt -- calling a new `_wait_for_render()` (a bounded
   `page.wait_for_load_state("networkidle", ...)`) unconditionally
   after every navigation/click -- was WRONG, caught by measuring it,
   not by inspection: it fixed OrangeHRM but nearly tripled a full
   saucedemo.com crawl's runtime (saucedemo's own ordinary background
   traffic alone takes ~2.7s to naturally go network-idle) and,
   independently, sampled saucedemo's own dynamic-catalog/spinner/
   lazy-load pages (deliberately progressive-loading test fixtures) at
   different points in their own loading lifecycle across different
   DFS branches, multiplying one real state into several spurious ones
   with different candidate counts each time (confirmed live:
   `inventory.html` alone showed up 3 times in one run, with 27/29/32
   candidates). **Reverted** in favor of calling `_wait_for_render()`
   only when `_discover_state()` (crawler.py) finds LITERALLY nothing
   at all -- no candidates, no occluded elements, no unclassified ones
   either. An ordinary, already-rendered page (including one still
   mid-way through loading MORE content) is accepted exactly as
   before and pays nothing extra; only a genuinely blank-so-far page
   retries, once, after a bounded wait. Separately confirmed the
   saucedemo timing/count instability itself is real but PRE-EXISTING
   and unrelated to this fix -- reproduced byte-for-byte identically
   (same 12 states, same candidate counts, same runtime) against the
   unmodified `git stash`-ed baseline with none of this session's
   changes applied at all.

2. A real, independent robustness bug, found only because the public
   demo server it was being tested against turned out to be
   intermittently slow: `_run_path()`'s own initial
   `page.goto(config["start_url"], wait_until="load")` -- unlike
   every step inside its per-path loop, and unlike this same
   function's final `_discover_state()` call -- ran completely
   outside any try/except. A transient navigation failure here (which
   this function hits on EVERY single replay, not just root
   discovery) crashed the entire `crawl()` call with an uncaught
   exception instead of degrading to a checkpoint like every other
   navigation failure in this codebase's own documented behavior.
   Reproduced live against the flaky demo server, fixed by wrapping
   both the initial goto and the final `_discover_state()` call in the
   same checkpoint-and-return-None pattern the per-step loop already
   used. Verified the fix's actual value live, not just that it
   compiles: one retry run hit exactly this navigation timeout on its
   very first replay, logged a clean checkpoint, and the crawl
   continued regardless -- reaching 34+53+78 candidates across
   multiple authenticated states in the SAME run, instead of the
   whole process dying right there.

**Built:**
- `_run_path()` (`crawler.py`): `config.get("storage_state")` passed to
  `browser.new_context()` when set; a failure loading it degrades to a
  checkpoint (this call returns `None`) instead of crashing or silently
  falling back to an unauthenticated context with no signal that
  happened. The initial `page.goto()` and the final `_discover_state()`
  call are now both wrapped the same way.
- `actions.py`: new `_wait_for_render()` helper (bounded
  `networkidle` wait, 5s default).
- `crawler.py`'s `_discover_state()`: calls it, once, only when a
  discovery pass finds nothing at all.

**Verified live, through the full real `crawl()` pipeline, against
OrangeHRM's real public demo instance:** seed URLs into Admin/PIM
correctly authenticated and landed on their real destinations;
candidate counts at every level (dashboard 34, admin/viewSystemUsers
24-63, pim/viewEmployeeList 51-79) matched a fully-rendered page, not
an empty one; DFS explored multiple levels deep into both modules
(reaching `pim/updatePassword`, `pim/configurePim`, `help/support`);
a genuine mid-crawl navigation timeout degraded to a checkpoint and the
crawl continued productively instead of dying. Regression check: a
full saucedemo.com crawl is unaffected by the conditional
`_wait_for_render` (never triggers there, since saucedemo's pages
always show at least some candidates immediately) and its own
dynamic-catalog-driven state-count variance was confirmed pre-existing
and unrelated via a direct `git stash` comparison against the
unmodified baseline.

All 20 existing tests still pass; no leftover Playwright/headless-
Chromium processes.

## Known limitation — "undiscovered" and "uncovered" are not the same thing (raised Sep 2026; not solvable by this tool alone)

A skeptical piece of LinkedIn feedback raised the sharpest question
this project has gotten yet, and it deserves a straight answer rather
than a defensive one: how do we know the flows FlowScout finds are the
complete picture? In a real enterprise system, behavior is routinely
gated by permissions, feature flags, per-customer customization,
integrations, backend data conditions, and undocumented business
process -- none of it visible from navigation alone. If a workflow
transition is gated behind one of these, a gap-analysis report can
look complete while silently missing exactly the highest-risk,
least-documented part of the system -- often the part most worth
testing.

**The honest answer: FlowScout cannot tell "this doesn't exist" apart
from "we didn't know to look for it," and does not claim to.** Every
"gap"/"not_found" in a report is scoped narrowly -- it means "this TCMS
item didn't match any flow THIS crawl actually found," never "this
behavior doesn't exist in the app." The found-flow set is a floor on
real system behavior, not a ceiling, and nothing in this project
computes or reports a measure of how close that floor is to the
ceiling -- because nothing CAN, from outside the app, without already
knowing what's being missed. This is not unique to FlowScout: any
automated-discovery-based testing approach (fuzzing, model-based
testing, even a human writing test cases from a spec) has the same
fundamental limit -- you cannot test what you don't know exists. What
would be dishonest is a report that reads as more complete than this.

**What already exists, and exactly what it does and doesn't solve --
each one requires a human to already suspect the gated behavior, none
of them solve the unknown-unknown case:**
- Multi-persona crawling (`personas` in config) surfaces
  permission-gated differences -- IF an operator configures a persona
  for each role worth checking. A role nobody thought to add is
  invisible, same as before.
- `storage_state`/`credentials` gets past an auth wall -- IF the
  operator has a working login for it. A customer-specific tenant this
  crawler was never pointed at is invisible.
- `explore_combination()` (see ROADMAP.md's own "conjunctive
  multi-parameter gating" entry above) lets a human hand over a
  SPECIFIC parameter combination DFS can't find alone -- only once a
  human already suspects it matters enough to try.
- `seed_urls`/`sitemap_url` reaches a page with no inbound link -- only
  for a URL someone already knows about.
- None of these -- individually or together -- reach a feature flag
  nobody mentioned, an integration callback that only fires under real
  production conditions, or a workflow that only exists because of a
  business decision made years ago and never written down anywhere a
  crawler (or a new hire) could find it.

**What still has real value despite this, stated plainly rather than
oversold:** the reachable surface FlowScout DOES cross is real,
verified behavior, not a guess -- cheaper and more thorough to check
this way than by hand, for exactly the "large, undifferentiated,
tedious to manually re-test" part of an app most QA time actually goes
to. And a TCMS item that comes back `not_found` is itself a genuine,
actionable signal even under this limitation: either the documented
flow doesn't exist the way the test case describes anymore, or it
exists behind a condition this run didn't have configured -- both are
worth a human's attention, and the report says which of the disclosed
reasons applies (`skipped_candidates` names anything withheld by risk
policy, a limit, or a persona gap) rather than pretending "not found"
always means the same thing. The genuinely hard part the feedback
names correctly -- finding the highest-risk, least-documented
workflows nobody wrote down -- stays a job for a human who knows the
business, not something this or any other purely technical crawling
approach replaces. FlowScout's honest job is amplifying that human's
reach into the areas they DO already know about, not replacing their
knowledge of what to look for.

## Run conditions in the report, and per-configuration change baselines (done, Sep 2026)

Direct follow-up to the entry above. Re-reading that skeptic's list
with a colder eye, most of it turns out NOT to be an epistemic limit at
all -- it's an access-and-setup question with a precise answer:

- **Permissions**: provision the persona with the role you want
  covered. Don't, and you correctly don't see those flows.
- **Feature flags**: a flag that's off *definitionally* means "this
  does not exist for this user". Reporting it as absent is the correct
  answer, not a blind spot.
- **Per-customer customization**: arguably where crawling BEATS
  spec-derived test design -- the crawler reads the UI actually
  deployed for that customer, not the one the documentation describes.
- **Data conditions**: stand up the fixture data, same as any other
  form of testing has always required.

The precise claim FlowScout can defend is therefore narrower and much
firmer than "we might be missing something": **"here is what this
identity could reach, in this system state."** Two things were missing
before that claim held up in practice, and both came straight out of
that re-reading:

**1. The report never stated its own preconditions.** A reader saw
"gap" and "not found" with no statement of which identities, which
configuration, which seeds, or which limits produced them -- so a
scoped result read as an absolute one. Added a **Run conditions**
section (`_run_envelope_html`, report.py) directly under the header:
configuration label, personas (names only, never credential values),
whether a pre-authenticated `storage_state` was supplied, how many
seed URLs / whether a sitemap was used, allowed domains, exclude-
pattern count -- followed by an explicit line that everything below is
scoped to exactly those conditions and that "not found" means "this
crawl didn't reach it", never "it doesn't exist".

**2. Change detection compared different configurations against each
other.** `project_state.state_path()` was keyed by project NAME alone,
so two customers (or one customer with a feature flag on and then off)
crawled under one project name shared a single baseline.

Verified the bug before fixing it, not assumed: two "tenants" on one
fixture server, each with one flow the other lacks, crawled in
sequence under the same project name --

```
WITHOUT variant -- customer B crawled after customer A, same project name:
  baseline=False  missing=1  new=1
    MISSING (false alarm): Open "Alpha feature"
```

Customer A's flow reported as *missing* when nothing changed in either
system. Fixed by adding an operator-set `config["variant"]` (a
customer/tenant, feature-flag set, or environment) folded into the
state key: `projects/<project>/variants/<variant>/state.json`.
Deliberately operator-chosen rather than derived -- FlowScout cannot
tell from outside which customer's customization it is looking at, and
hashing the config to guess would silently split or merge baselines
whenever any unrelated setting changed. Threaded through
`load`/`save`/`record_run`/`detect_changes`, the CLI (`confirm
--variant`), and the web API (`?variant=` / body field), with a form
field and help entry in the operator UI. Empty variant keeps the
original path byte-for-byte, so every config written before this keeps
its existing state file and history.

Verified live end to end on a local fixture: two variants under one
project name keep independent baselines (beta's first crawl reads as
its own baseline with zero false "missing"; alpha's re-crawl still
sees its own history and reports nothing missing), separate state
files are written per variant with no unscoped file created, and a
variant-less run still lands on the original path and behaves exactly
as before. The rendered report carries the Run conditions section, the
configuration label, and the scoping caveat.

**Still genuinely unsolved, and narrower than the original list** --
none of these are fixable by handing the crawler more permissions:
- **Multi-actor handoffs.** Employee submits → *manager* approves →
  employee sees the result. Personas crawl sequentially and
  independently; there is no coordinated handoff between them, and one
  super-user cannot reproduce a flow that requires two actors in
  sequence.
- **One-shot / irreversible transitions.** The whole architecture is
  reset-and-replay (see this file's top). "Activate account", "consume
  this token", "final approval" cannot be re-walked against the same
  backend entity.
- **Time-dependent flows.** Anything gated behind a scheduler or a
  30-day wait.
- **Integrations that leave the allowed domain.** `risk.py` classifies
  any off-domain navigation as DESTRUCTIVE and never follows it, so a
  real "pay at external provider → return to the shop" round trip is
  never walked. A bounded excursion (N steps off-domain, then back)
  is a plausible design, not yet built.

All 20 existing tests still pass; test project state cleaned up; no
leftover Playwright/headless-Chromium processes.

## Bounded excursions into approved third-party integrations (done, Sep 2026)

The first of the four remaining items from the entry above, picked as
the most tractable. A payment gateway (Stripe Checkout, PayPal), an
OAuth/SSO provider, a subscription/billing portal -- any real
integration that leaves the app's own domain and (usually) comes back
-- was previously indistinguishable from an ordinary external link:
`risk.classify()` marks any off-`allowed_domains` navigation
DESTRUCTIVE, unconditionally, so the crawler never even tries it.

**The operator's own design concern, addressed directly, not
papered over with "just allow N steps":** a flat step budget alone
isn't enough -- a hosted payment/billing page typically has its OWN
marketing nav (a homepage link, "Pricing", "About us"), and an
unconstrained DFS given a few steps of rope would spend them wandering
into the third party's own site instead of completing the actual
integration. Two mechanisms, not one:
- A completely separate, explicit allowlist --
  `config["excursion_domains"]` (wildcard-aware, e.g. `"*.stripe.com"`)
  -- distinct from `allowed_domains`: the latter means "this is the
  app, explore it normally"; the former means "this specific
  third party is worth walking through, with a much narrower budget."
  An ordinary external link (a random domain, a social-share button)
  is unaffected -- still unconditionally DESTRUCTIVE exactly as before.
- Once actually on an excursion domain, candidates are narrowed to
  `_excursion_eligible()` ones before anything else runs: a real
  progression control (a form's own button/input, or one matching the
  SAME `_MUTATING_KEYWORDS` list risk.py's own classification already
  uses) -- never a plain `<a>` link, which is precisely the "wander to
  another page" affordance this exists to avoid. Breadth is separately
  capped at `excursion_max_breadth` (default 1: follow ONE path
  through, don't branch), and depth at `excursion_max_depth` (default
  4, independent of the app's own `max_depth` -- a real integration is
  typically 2-5 screens, not worth the same budget as the app itself).
  Both caps reset to zero the moment a step lands back in
  `allowed_domains`, so returning from a completed integration resumes
  ordinary exploration with the ordinary budget.

**Two real false positives found live while building the fixture to
prove eligibility filtering actually works, neither assumed:**
1. `closestFormLike()`'s own loose fallback (used for `inForm`) walks
   up to the closest ancestor containing ANY real input/select/
   textarea, capped at 200 descendants -- on a simple fixture page, a
   sibling link and an unrelated `<form>` were both direct children of
   `<body>`, and `<body>` "contains" the form, so the link registered
   as `inForm` too even though it had nothing to do with it. Fixed by
   requiring the candidate's OWN tag to be `button`/`input` before the
   `inForm` fast path applies at all -- never a bare `<a>`.
2. A link reading "Fake-Pay Home" matched `_MUTATING_KEYWORDS`'s
   substring `"pay"` via the third party's own BRAND NAME, not an
   actual pay/submit action -- a real, generalizable risk for any
   payment/identity provider whose own name contains a keyword
   (PayPal, GPay, Razorpay...). Fixed the same way as (1): a plain
   `<a>` is now never eligible via either check, keyword match
   included, closing both false positives with one rule rather than
   trying to patch each signal into being precise enough on its own.

**Classification (`risk.py`):** an excursion-domain match doesn't
return early as DESTRUCTIVE, but falls through to the SAME keyword
checks an ordinary same-domain candidate goes through (so a genuinely
destructive-looking label on the excursion domain itself -- a
logout-like keyword, an `exclude_patterns` match -- still wins), and
floors at MUTATING if nothing else matched: leaving to a third party
is never treated as harmless just because its own label doesn't say
so, withheld exactly like `checkout`/`pay` already are unless
`allow_mutating=true`.

**`StateNode.external_domain`** (empty for an ordinary in-app state,
the domain itself when reached outside `allowed_domains`) flows
through to two more places: the report (a badge on the flow card, plus
a per-step "left the app for X (approved excursion)" note -- a plain
note, not an error, since this is expected, correct behavior) and
`gap_analysis.py`, where a flow ending externally is excluded from
TCMS matching entirely (a new `FlowCoverage` status, `"external"`,
counted in the summary) rather than left to potentially match a test
case describing the app's OWN checkout page against the third party's
own hosted UI text.

**Verified live, through the full real `crawl()` pipeline, three
separate fixtures:**

```
1) Payment round trip (app 8971 -> fake gateway 8972 -> back):
   flow: 'Open "Cart"', 'Open "Checkout"',
         'Fill form and submit "Pay now" (card_number=...)'
     -> http://127.0.0.1:8971/order-confirmed (external_domain reset to "")
   Gateway's OWN homepage/pricing/about NEVER reached at all --
   only 4 states total, none of them the gateway's marketing pages.
   Gateway homepage link recorded: "outside excursion scope (not a
   form field/submit or a progression-like action on 127.0.0.1:8972)"

2) excursion_max_depth=4 against a 7-step off-domain chain:
   reaches step1->step2->step3->step4 (excursion_depth 1,2,3,4), then:
   "Truncated: excursion depth limit reached (4 consecutive step(s)
   outside allowed_domains) with 1 further action(s) available from
   here, never tried" -- step5/6/7/done never reached.

3) gap_analysis exclusion: a flow ending on the excursion domain came
   back with FlowCoverage.status == "external", excluded from ordinary
   TCMS matching, correctly counted in summary()["flows_external"].
```

Regression check: a full saucedemo.com crawl with no `excursion_domains`
configured at all is unchanged in shape (12 states, 20 flows, same
single `max_flows` checkpoint) -- the entire feature is a no-op unless
explicitly opted into.

All 20 existing tests still pass; all fixture servers and ports
confirmed shut down; no leftover Playwright/headless-Chromium
processes.

## Time-gated content: client-side clock mocking (done, Sep 2026)

Second of the four remaining items from two entries above. A control
gated behind "resend code in 00:30", "available starting <date>", or
"come back in 30 days" was previously indistinguishable from a
permanently unavailable one -- `disabled_interactive` already
surfaced it as "a real control, currently disabled" (see its own
docstring), but nothing could get PAST that to see what it unlocks.

**Deliberately not a "time-gate detector."** Considered and rejected:
unlike CAPTCHA (reCAPTCHA/hCaptcha/Turnstile all have vendor-
documented, structural markers -- an iframe src pattern, a widget
class name -- that are facts about the DOM, not guesses), there is no
equivalent standard for "this control is time-gated" -- every app
implements a countdown/deadline check differently, in its own
JavaScript, with no shared markup convention to key off. A text-based
detector ("contains a countdown", "says 'available in'") would be
exactly the English-language-dependent guess this project already
rejected once for field_detect.py's own login-trigger matching. Built
a real capability instead of a guessed signal: `config["mock_clock"]`
exposes Playwright's own Clock API directly, so an operator who
already suspects (or knows) a control is time-gated can try to get
past it, without FlowScout ever having to correctly identify the gate
on its own first.

**Two independent mechanisms, verified live to NOT substitute for each
other -- a real finding, not assumed from the API docs alone:**
- `mock_clock.start_at` (`page.clock.install(time=...)`, before the
  page ever loads) answers a `Date.now()`/`new Date()` comparison
  against an absolute deadline, checked synchronously at load (e.g.
  "available starting 2027-01-01"). Verified live with a fixture doing
  exactly that check: installing the clock at real "now" and calling
  `fast_forward()` a full YEAR afterward did nothing at all -- the
  comparison had already run, against the original time, before
  `fast_forward` was ever called. Only pre-setting `start_at` to a
  point past the deadline worked.
- `mock_clock.fast_forward` (`page.clock.fast_forward(...)`, applied
  once right after the page's own initial load) answers a
  `setTimeout`/`setInterval`-scheduled cooldown (e.g. a real 30-second
  "resend code" button, disabled via JS until a timer fires).
  Verified live with a separate fixture: pre-setting `start_at` to a
  point a year in the future did nothing for this one either -- a
  timer scheduled at load always waits its own full duration from
  that moment, regardless of what date it thinks it currently is.
  Only `fast_forward()`, called after that load, worked.

Neither is a superset of the other; a real site can use either pattern
(or, in principle, both, on different controls). `mock_clock` exposes
both documented primitives as independent config keys and lets the
operator pick, rather than guessing which one a given site's
implementation actually needs.

**Explicitly out of scope, disclosed rather than glossed over: this
only ever touches client-side JavaScript timing.** A real, more common
production pattern -- a timestamp stored server-side, checked by the
backend on the next request -- is genuinely unreachable from outside
the browser. No client-side mechanism, this or any other, can fake
what a server believes the date is. `mock_clock` helps exactly the
class of gate implemented in the page's own JS and nothing past that
boundary; ROADMAP.md's own "undiscovered vs. uncovered" entry already
covers why a tool operating from outside the app can't do better here.

**Built:** `_run_path()` (`crawler.py`) installs the clock right after
context/page creation (same place `storage_state` already does, and
for the same reason: every fresh replay context needs it, not just
root discovery), and applies `fast_forward` once, right after the
initial `page.goto()` succeeds -- before any of the replayed path
steps run, so ordinary exploration proceeds from an already-time-
travelled state without repeating the jump on every subsequent click.
A small `_parse_clock_duration()` helper accepts a friendly `"<N>
<unit>"` string (`s`/`m`/`h`/`d`, e.g. `"30d"`) alongside raw
milliseconds and Playwright's own native `"HH:MM:SS"` string form --
Playwright's own `fast_forward()` has no day/hour shorthand of its
own. A failure to install or fast-forward the clock (a malformed
`start_at`, an invalid duration) degrades to a checkpoint and returns
`None` for that replay, the same pattern `storage_state` already
established, rather than silently proceeding as if nothing were
configured.

**Verified live, through the full real `crawl()` pipeline, against
both fixture types:** a `setTimeout`-gated button (disabled for 30
real seconds) discovered as 0 real candidates without `mock_clock`,
and correctly discovered as a real, clickable candidate
(`fast_forward: "35s"`) with it; a `Date.now()`-comparison-gated
button (available starting a fixed future date) likewise went from
undiscoverable to discoverable with `start_at` set past that date.
Regression check: a full saucedemo.com crawl with no `mock_clock`
configured is unchanged in shape (12 states, 20 flows, one
`max_flows` checkpoint) -- the feature is a no-op unless opted into.

All 20 existing tests still pass; both fixture servers and their
ports confirmed shut down; no leftover Playwright/headless-Chromium
processes.

## Multi-actor handoff scenarios (done, Sep 2026)

The gap this closes: everything above `crawl()` operates as ONE
persona at a time, discovering what that persona alone can reach.
Real workflows routinely need a SECOND actor to intervene mid-flow --
an admin approving a request, a moderator reviewing a submission --
before the FIRST persona can continue. No amount of autonomous DFS
can invent that correlation; only a human operator can say "this
request THIS run just created is the one THAT persona needs to act
on next."

**Design discussion, not a unilateral call.** Two questions were
worked through with the operator before any code was written:

1. *How does the crawler know where in the admin's own menus to find
   the specific thing it needs to act on?* Two options were weighed:
   (A) the operator names the exact destination URL directly -- cheap,
   but doesn't "understand" anything and breaks the moment the app's
   own URL scheme changes; (B) a bounded, genuinely autonomous DFS
   *search* (`"find": {"contains": ..., "search_from": ...}`) for the
   first state whose URL/title/candidate label contains a target
   string, using `_run_dfs`'s new `stop_when` early-stop parameter --
   exercising the admin's OWN real navigation/search UI as a side
   effect, and an unsuccessful search is itself a reportable finding
   ("this persona genuinely can't reach anything matching the
   target"), not a tool failure. (B) was chosen.
2. *How does the SAME original user's SAME session come back after
   the admin acts, rather than a fresh login as "the same persona"?*
   Resolved by `"capture_session"` -- save the acting persona's own
   live `context.storage_state()` under a name after a step -- and a
   later step's `"storage_state": "{name}"` resuming it, reusing the
   existing `storage_state` session-persistence mechanism (built
   earlier for auth-walled apps) rather than inventing a new concept.

**What got built (`run_handoff_scenario()` in `crawler.py`, parallel
to `crawl()`/`explore_combination()`/`resume_flow()`):** a thin
orchestration layer over the SAME engine, not a new one. Exactly two
new primitives were added to the shared engine itself:
- `_run_dfs(..., stop_when: StateNode -> bool | None = None)`: checked
  once per genuinely-new state; on a match, emits the flow and returns
  that state's fingerprint immediately. Every existing caller ignores
  the new parameter, so their behavior is byte-for-byte unchanged.
- `raw_url` threaded through `_discover_state()` -> `_run_path()` ->
  every call site, alongside the existing (normalized) `url_pattern`.

`config["handoff"]["steps"]` is an ORDERED list, each performed as a
named persona. A step is either `"seed_url"` (direct navigation,
`{name}`-templated from an earlier step's `"capture"`) or `"find"`
(the bounded search above); either way an optional `"action"` clicks a
candidate by LABEL (never position -- an index would silently point
at the wrong control if layout shifts between runs) once the step's
landing state is reached. `"capture": {"from": "url", "pattern": ...,
"as": name}` extracts a value for later steps to reference as
`{name}`. `"explore": true` hands a step's landing state off into a
FULL, ordinary `_run_dfs()` pass -- the answer to "the now-approved
user logs in and just keeps crawling normally" -- merging every state/
flow it finds into the SAME `RunResult` (one report, one gap analysis
for the whole scenario).

**Two real bugs found via live, end-to-end verification (a 4-step
fixture: user signs up -> submits an access request -> admin finds it
in a real navigable list and approves it -> same user returns and
explores a newly-unlocked private area), neither assumed:**

1. **`capture`'s regex could never match.** `pattern=r"/requests/
   (\d+)/confirmation"` was matched against `url_pattern` --
   `fingerprint.py`'s `normalize_url()` deliberately collapses numeric/
   UUID path segments to `*` for stable state-fingerprinting elsewhere
   in the codebase, so the actual request id was already gone by the
   time this code saw it (`url='.../requests/*/confirmation'`, never
   matching a `\d+` pattern). Root-caused by reading `normalize_url()`
   itself, not guessed. Fixed by threading `raw_url` (the real,
   un-normalized `page.url`) all the way through and matching
   `capture` against THAT instead of `url_pattern`.
2. **A "find" match on a listing page's own link stopped one hop too
   early.** `matches()` treats a hit on a CANDIDATE's label (e.g. the
   admin list's own "Review request #1 (alice: need-access)" anchor)
   the same as a hit on the state's own url/title -- correct for
   *finding* the right page, but the requested `action` ("Approve")
   doesn't live on the LISTING page, it lives on whatever that link
   leads to. Found live: step 3 correctly located `/admin/requests`
   but then failed with "found a matching state but no candidate
   labeled 'Approve' there". Fixed by checking whether the found
   state matches on its OWN url/title; if not (it matched only via a
   candidate), that one candidate is auto-followed one hop before
   `action` is looked for, using the same `_run_path`-replay pattern
   `_handoff_direct_step` already uses.

**Verified live, full 4-step scenario, both bugs fixed:**
```
States=9 Flows=7 Checkpoints=0
  flow 3 (admin, unique): Open "/admin/requests" -> Open "Review
    request #1 (alice: need-access)" -> Click "Approve"
  flow 4 (user, unique): Open "/requests/1/status" -> Open "Go to
    dashboard"
  flow 5/6 (user, unique): ... -> Open "Profile" / "Settings"
PASS: signup, submit, admin found+approved via search, same user
session resumed and explored the newly-unlocked private area
```
Also verified through the actual CLI path end-to-end (`flowscout
handoff --config ... --out ...`, new subcommand added to `cli.py`,
sharing gap-analysis/change-detection/project-state/report-rendering
with `crawl` via an extracted `_finish_run()` helper) -- report.html
renders the handoff's flows and origin notes correctly.

**KNOWN, DISCLOSED LIMITATION (raised explicitly, not left implicit):
no email/inbox access.** A real-world flow gated behind "click the
link we emailed you" cannot be automated by this or any part of
FlowScout -- there is no mail access, and inventing one is out of
scope. A handoff scenario with an email-verification step in the
middle will stall there: the FOLLOWING step's `seed_url`/`find` simply
fails to find what it's looking for, recorded honestly as a
checkpoint, rather than silently skipping past it or fabricating a
click that never really happened. Documented in this mechanism's own
docstring in `crawler.py` as well as here and in `README.md`.

Regression check: a full saucedemo.com crawl via ordinary `crawl()`
(no `"handoff"` in the config) still completes cleanly after the
`raw_url` plumbing change -- 19 states, 59 flows (9 unique / 44
duplicate / 6 blocked), 0 checkpoints -- the entire handoff mechanism
is a separate entry point (`run_handoff_scenario`), never touched by
an ordinary crawl.

All 20 existing tests still pass; the handoff fixture server and its
port confirmed shut down; no leftover Playwright/headless-Chromium
processes.

**Web-UI editor for handoff configs (done, Sep 2026).** Originally
deferred (see below) since every other config knob added this session
(`storage_state`, `variant`, `excursion_domains`, `mock_clock`) is a
flat key the existing form-based UI already handles, while
`"handoff"` is a nested, ordered list of steps that doesn't fit that
pattern -- built once the deferral's own reason (token budget) no
longer applied. A dedicated per-step form (`+ add step`, matching the
rest of the UI's own style, not a raw-JSON textarea shortcut): each
step block has a persona name, a Direct-URL/Find mode toggle with the
matching fields shown/hidden accordingly, and optional storage-state-
resume / action / capture / capture-session / explore fields, exactly
mirroring `run_handoff_scenario()`'s own JSON shape field-for-field.
`collectConfig()` only emits `config.handoff` when at least one step
block exists, so an ordinary crawl's payload is byte-for-byte
unchanged from before this existed. The backend (`web/runs.py`'s
`_execute`) dispatches to `run_handoff_scenario(config)` instead of
`crawl(config)` purely based on `config.get("handoff")` being
truthy -- everything downstream (gap analysis, change detection,
project state, report rendering) is identical either way, since both
functions return the same `RunResult` shape.

Verified live, through the real running server (not just reading the
code): a Playwright-driven browser session against `flowscout serve`
(1) filled Project/Start URL/Allowed domains, confirmed the submit
button reads "Start crawl"; (2) added a step, confirmed the button
relabels to "Run handoff scenario" and `collectConfig()`'s own JSON
output has the exact persona/seed_url/capture_session/find/action
shape entered in the form; (3) removed steps one at a time, confirmed
the button reverts correctly at each count; (4) round-tripped a full
2-step config through `fillForm()` (as loading a saved config would)
and confirmed every field, including the mode-dependent Find fields
and the Explore checkbox, was correctly rebuilt; (5) confirmed
`resetForm()` clears the steps and reverts the button label. Separately,
POSTed the same 4-step fixture config from the earlier verification
directly to the running server's `/api/runs` endpoint (exactly what
the form's own submit handler sends) and confirmed it produces the
identical result as the CLI/API paths: 9 states, 7 flows, 0
checkpoints, and the resulting `report.html` (served through
`/api/runs/{id}/report`, not just the file on disk) renders the
handoff's own flow labels and origin notes correctly.

---

Originally deferred here (kept for the record): every other config
knob added this session is a flat key the existing form-based UI
already handles; `"handoff"` is a nested, ordered list of steps that
doesn't fit that pattern. Operator's call at the time: plan the
design but defer the actual implementation to a later session over
a token-budget constraint -- resolved once that constraint lifted,
above.

## Candidate priority for repeated global nav (done, Sep 2026)

Picked as the first, highest-leverage item off the prioritized backlog
(nav-dedup > ARIA choice support > pairwise combinatorial testing >
embeddings verification > mobile port) -- this affects nearly every
real site with a persistent header/footer, not a narrow edge case,
closing the "Candidate priority still starves page-unique content
behind repeated header nav" item logged back in the Aug 2026 Site B
section (see this file's own M0 entry) as "worth doing before relying
on FlowScout for... nearly all of them".

**The gap, precisely:** `_order_for()`'s existing `revisit_history`
mechanism already deprioritizes a signature ONCE it's been confirmed,
from ANYWHERE, to lead to an already-known state -- but it can only
ever help with candidates that actually CONVERGE. A structurally
identical piece of chrome that produces a technically "new" state on
every single occurrence (the ROADMAP's own example: a language
switcher, which changes the CURRENT page's own state rather than
navigating to one fixed target) is invisible to that mechanism no
matter how many times it's explored -- `revisit_history` has nothing
to key off, since every occurrence genuinely differs.

**Fix (`_ubiquitous_nav_signatures()` + `_order_for()` in
`crawler.py`):** a new, SEPARATE signal, orthogonal to
`revisit_history` -- a candidate's signature counts as "ubiquitous"
(structural chrome, not page content) once it's present on
`max(3, 80%)` of the OTHER already-discovered states, recomputed fresh
from `run.states` on every `_order_for()` call rather than maintained
as an incremental counter, so it works identically for every caller
(crawl's own DFS, `resume_flow`, `explore_combination`, a handoff
step) without each one remembering to update a separate running tally.
`_order_for()`'s sort key became a 2-tuple `(revisit, ubiquitous)` --
a confirmed dead end still sorts to the very back regardless of
ubiquity, but among candidates NOT yet confirmed as dead ends,
page-unique ones are now tried before merely-ubiquitous ones. Deliberately
a STRUCTURAL signal ("this exact signature shows up almost everywhere"),
not a label-based guess ("this looks like navigation") -- the same
discipline `field_detect.py`'s login-trigger matching and
`revisit_history` itself were already held to. Needs at least 3 OTHER
states before saying anything, so the first few pages of any crawl are
never penalized for looking similar by coincidence.

**Verified live, and only accepted once the test itself was correct --
two fixture-modeling bugs caught and fixed along the way, not glossed
over:**
1. First attempt used numeric wizard steps (`/wizard/1`, `/wizard/2`...)
   -- `normalize_url()` deliberately collapses purely-numeric path
   segments to `*` for stable fingerprinting (the same mechanism the
   handoff feature's own `raw_url` bug ran into, above), so every step
   collapsed into the exact same state regardless of which one was
   actually reached. Fixed by using non-numeric step names.
2. Second attempt gave every "Next" link an identical `data-test`,
   which hit `max_action_repeat` (an existing, unrelated cap on how many
   times one normalized action can repeat in a single path) before
   candidate ordering was ever consulted -- realistic (a real wizard's
   own button never changes label step to step) but orthogonal to what
   was being tested, so the test config raises it rather than the
   fixture being wrong.

**The actual before/after, same fixture, same config, only the new
signal toggled (monkeypatched to return an empty set for "before"):**
a header of 5 static nav links (Home/About/Services/Contact/Blog --
these DO converge, and were already handled by `revisit_history`) plus
3 "language switcher" links whose destination is always
`<current-page>-<lang>` (same signature everywhere, but a genuinely
new, never-converging state every time -- deliberately isolating the
NEW mechanism from the existing one) sit in front of a 4-step wizard's
own "Next" link, `max_breadth_per_state=3`:
```
WITHOUT the fix: States=8  Flows=24  -- never even reaches the wizard
  entry point ("Start wizard" itself starved on /blog, the header's
  own last static destination)
WITH the fix:    States=18 Flows=54  -- reaches /wizard/done, full
  4-step chain traversed
```
Regression check: a full saucedemo.com crawl is unchanged in shape --
19 states, 59 flows (9 unique / 44 duplicate / 6 blocked), 0
checkpoints, identical to the pre-fix baseline -- saucedemo's own
header/footer isn't large or repetitive enough to have ever triggered
starvation, so the fix is a genuine no-op there, exactly as expected
for a purely additive ordering change.

All 20 existing tests still pass; the nav-dedup fixture and its port
confirmed shut down; no leftover Playwright/headless-Chromium processes.

## ARIA `role="tab"`/`role="tablist"` as choice candidates (done, Sep 2026)

Second item off the prioritized backlog. `role="checkbox"`/`role="radio"`
custom controls (Radix/shadcn-style component libraries) were already
recognized as `is_choice` candidates, grouped by the nearest
`role="radiogroup"` ancestor -- but `role="tab"`/`role="tablist"` was
explicitly logged as still unhandled (this file's own "Radio buttons
and checkboxes as choice candidates" entry). Structurally the same
gap: a tab switches which panel is visible, exactly like a radio
option deselects its siblings, but was previously an ordinary,
ungrouped candidate -- a click on one tab read as an unremarkable "new
state," and picking a DIFFERENT tab later in the same run wasn't
recognized as a distinct alternative the way a radio/select pick
already is.

**Built by mirroring the existing role-radio/role-checkbox shape** (a
new `[role="tab"]` query in `_DISCOVER_JS`, a `role-tab` branch in
`_build_candidate`, `describe_action`, and `build_locator`), reusing
`is_choice=True` + `choice_group` generically -- everything downstream
(`identity.py`'s anchor-widening, `gap_analysis.py`, `shared_steps.py`,
`testcase_draft.py`, the report's own choice-group display) already
keys off those two fields alone, not a tag string, so nothing else
needed to change for a tablist to get the same treatment select/radio/
checkbox already have.

**Two real bugs caught by building a live fixture, not by inspecting
the code and assuming it would work the same as role-radio:**
1. **Wrong label.** Copied `role-radio`'s own `inputLabelText(el)` for
   the tab's accessible name at first -- wrong function for the job.
   `inputLabelText` is built for a form control whose OWN text is
   typically empty and whose real label lives on a separate, PAIRED
   `<label>` element (a checkbox/radio's usual shape) -- a tab is the
   opposite, its accessible name is normally its own visible text
   ("Plan A"). Found live: the candidate's label came back as the raw
   `data-test` attribute instead of the tab's real text. Fixed by
   reading `el.innerText` directly, the same way the generic candidate
   loop already names a button/link.
2. **Wrong grouping.** Copied role-radio's own `base` computation
   (`dataTest || id || roleGroup`) for `choice_group` too -- also
   wrong: a real tab typically carries its OWN per-tab `data-test`
   ("tab-a", "tab-b", ...), so `base` came out DIFFERENT for every tab,
   splitting 3 mutually-exclusive alternatives into 3 separate
   one-tab "groups" instead of recognizing them as one choice.
   (`role-radio`'s own version likely shares this same latent
   assumption -- a radio option WITHOUT its own per-option data-test,
   relying on the group's own identifier instead -- but that's
   existing, shipped, previously-verified-live code and untouched
   here; not implicated by this specific fix.) Fixed by always using
   `roleGroup` (the tablist's own identifier) as the group, keeping
   `dataTest`/`id`/accessible-name as a SEPARATE per-tab distinguishing
   value.

**Verified live** against a 3-tab fixture (`role="tablist"` +
`role="tab"` + `role="tabpanel"`, each panel showing a genuinely
different, uniquely-labeled purchase link, so switching tabs produces
a real, distinct state): all 3 tabs correctly recognized as one
`is_choice` group sharing `choice_group="pricing-tabs"`, each with its
own correct label ("Plan A"/"Plan B"/"Plan C"), and the full crawl
reached all three "Buy Plan X" destinations -- none starved or deduped
away.

**Disclosed, not fixed here (pre-existing, shared with role-checkbox/
role-radio, not introduced by this change):** an interactive
`role="tab"` element with a REAL click listener (a component library's
own event binding, or a literal `<button role="tab">` matching the
crawler's generic `a, button, ...` candidate selector directly) also
gets picked up by the ordinary candidate-discovery path, producing a
second, redundant candidate for the same physical element under the
generic `data-test:`/`id:`/`text:` signature scheme alongside the new
`role-tab-choice:` one -- confirmed live on this fixture's own
`<div role="tab" onclick=...>`, caught by CDP's handler-discovery pool.
Not incorrect (both paths click the same element, reaching the same
state either way), just redundant -- one extra breadth slot spent
exploring what's effectively the identical action twice under two
different labels. Fixing it would mean excluding every roleControls-
matched element from the generic candidates/pool pass entirely, a
bigger, riskier change touching the already-working role-checkbox/
role-radio code too -- a separate backlog item if it's ever worth
doing, not scope-crept into this one.

Regression check: a full saucedemo.com crawl is unchanged in shape --
19 states, 59 flows (9 unique / 44 duplicate / 6 blocked), 0
checkpoints, identical to the pre-fix baseline (saucedemo has no ARIA
tabs, so this is a genuine no-op there).

All 20 existing tests still pass; the tabs fixture and its port
confirmed shut down; no leftover Playwright/headless-Chromium
processes.
