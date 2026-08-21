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
