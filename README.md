# FlowScout

An autonomous browser agent that crawls a web app, discovers real user
flows, and tells you which of your existing test cases are covered —
and which aren't.

FlowScout does **not** invent expected results and does **not** assert
anything about data correctness. It only reports what it can actually
verify by exploring the app: which flows exist, which of your TCMS test
cases match them, and which don't. That's a narrower promise than "AI
writes your tests for you" — and a more honest one.

One more honest boundary, worth stating up front rather than letting a
report imply otherwise: a `not_found`/`gap` result means a TCMS item
didn't match anything *this crawl found* — not that the described
behavior doesn't exist in the app. Permissions, feature flags,
per-customer customization, and undocumented business process can all
gate a real workflow behind something FlowScout was never told to look
for; see `ROADMAP.md`'s "undiscovered vs. uncovered" entry for the
full reasoning. FlowScout amplifies how much of an app a human can
verify quickly — it doesn't replace knowing what to point it at.

## What it does

- **Crawls** a site with Playwright (DFS, isolated browser context per
  path), classifying every clickable action as `safe` / `mutating` /
  `destructive` before ever touching it. Destructive actions (logout,
  leaving the allowed domain, an excluded page) are never followed.
  Mutating ones (checkout, submit, delete...) only run if you opt in.
- **Recognizes a CAPTCHA/challenge page** for what it is (reCAPTCHA,
  hCaptcha, Cloudflare Turnstile, Cloudflare's own challenge
  interstitial) and reports it as a Blocked flow naming which marker
  was found — never attempts to solve or bypass it. A real test
  environment should disable CAPTCHA entirely or use the vendor's own
  test sitekeys; that's a target-site configuration choice, not
  something this crawler does for you.
- **Dedupes** flows three ways: structural (same normalized action
  sequence), state-convergence (different paths landing on the same
  application state), and — optionally, needs an embeddings API key —
  semantic (different steps, same intent).
- **Compares against your TCMS** (a CSV export from TestRail, Zephyr,
  Xray, qTest, or similar) and reports each flow as covered, partially
  covered, or a gap — plus which of your test cases the crawl never
  touched at all.
- **Generates test-case drafts** (Markdown + a TCMS-importable CSV) and
  runnable `pytest-playwright` specs from what it found.
- **Tracks changes across runs** for the same project, so a second
  crawl can tell you what's new, what disappeared, and what moved —
  scoped by `"variant"` (a customer/tenant, feature-flag set, or
  environment), so two configurations sharing one entry point never get
  diffed against each other.
- **States its own preconditions** in every report: which personas,
  which configuration, which seeded URLs, which limits. A `not_found`
  is always relative to those, and the report says so on its face.
- **Multi-persona**: crawl the same site as multiple logged-in users
  (sequentially — see `ROADMAP.md` for why not in parallel) into one
  report, so admin-only flows and standard-user flows don't collapse
  into each other.
- **Resumes what a budget cut short**, without a full re-crawl: a flow
  truncated by `max_depth`, or dead-ended by risk policy, can be
  continued on its own with adjusted limits ("Resume this flow"), or
  every currently-resumable flow can be continued in one click
  ("Resume all blocked flows") — sequentially, since they share one
  state graph. A flow that's genuinely carried further this way
  changes its own status instead of sitting there stale forever.
- **Lets an operator hand over a specific parameter combination** the
  crawler can't find on its own: several checkboxes/radios/selects set
  *together* can gate content no single-action DFS pass will ever
  reach (see `ROADMAP.md`'s "conjunctive multi-parameter gating"
  entry) — pick the values, and exploration continues automatically
  from whatever that combination reveals.
- **Seeds additional entry points** the DFS can't click its way to on
  its own: a `sitemap.xml` URL (fetched and parsed automatically,
  including a sitemap index of child sitemaps) and/or an explicit list
  of known URLs. Each one is explored with the crawler's full normal
  logic from there onward, not just visited once — finding pages with
  no inbound link anywhere in the crawled UI (a deep-linked SPA route,
  an old landing page delisted from navigation).
- **Starts already authenticated** via `"storage_state"` (a Playwright
  storage-state file from a manual login, or the state inline) — every
  context, including seed URLs, begins logged in, so auth-walled pages
  never redirect to a login screen instead of the real content.
- **Walks a bounded excursion into an approved third-party integration**
  (a payment gateway, an OAuth/SSO provider) via `"excursion_domains"` —
  ordinary external links stay destructive and are never followed, but
  a named domain gets a narrow, separate budget (breadth capped to 1,
  depth capped independently of the app's own limits) that follows the
  real integration through to completion instead of wandering into the
  third party's own marketing pages, then resumes ordinary exploration
  the moment it returns.
- A **local operator UI** (FastAPI + vanilla JS, no build step) to
  configure and run crawls, attach a TCMS export, and browse reports —
  or drive all of this from the CLI / a CI job instead.

## Install

**Not yet published to PyPI** — install from source:

```bash
git clone https://github.com/igorakymenko-create/FlowScout.git
cd FlowScout
pip install -e ".[dev]"
playwright install chromium
```

The `playwright install` step downloads a Chromium build (~150 MB) —
it's a one-time setup, not a FlowScout-specific quirk, but it's easy to
miss and the first run will fail without it.

## Try it in 30 seconds

No config to write, no site to pick — this repo already ships a
working example against [saucedemo.com](https://www.saucedemo.com/), a
public practice site built for QA automation, plus a real 10-case TCMS
export for it (`fixtures/tcms_saucedemo.csv`: login, add-to-cart,
checkout, sort, logout, an invalid-login negative case, and more):

```bash
flowscout crawl --config configs/saucedemo.json --out runs/demo \
  --tcms fixtures/tcms_saucedemo.csv
```

Open `runs/demo/report.html` when it finishes — every flow the crawler
actually found, each one matched against those 10 test cases and
marked covered, partially covered, or a gap.

## Quickstart

Write a run config (see `configs/saucedemo.json` for a working
example against the public saucedemo.com practice site):

```json
{
  "project": "my-app",
  "start_url": "https://example.com/",
  "credentials": { "user-name": "standard_user", "password": "secret_sauce" },
  "limits": { "max_depth": 6, "max_breadth_per_state": 8, "max_states": 60, "max_flows": 60 },
  "allow_mutating": true,
  "allowed_domains": ["example.com"]
}
```

Then either:

```bash
flowscout crawl --config configs/my-app.json --out runs/my-app \
  --tcms my_export.csv
```

...or start the local UI and do the same thing through a form:

```bash
flowscout serve
# http://127.0.0.1:8787
```

The UI is a **local, single-operator tool by design** — no auth, no
multi-tenant isolation, reads/writes files on the machine it runs on.
It is not meant to be exposed on a public network; see `ROADMAP.md`.

## Embeddings (optional)

Semantic dedup and TCMS gap-matching need a text-embeddings API call.
Without a key, FlowScout still works — it just falls back to
structural-only dedup and marks every TCMS item as unmatched instead of
comparing by meaning. Copy `.env.example` to `.env.local` and set one
provider's key:

```bash
cp .env.example .env.local
# then edit .env.local and set GEMINI_API_KEY=...
```

Gemini is the only provider currently active — see `.env.example` and
`flowscout/embeddings.py`'s module docstring for the full multi-provider
story (OpenAI/Voyage AI support exists in the code but is paused,
commented out, pending a working billing setup to verify it against a
real API call).

Embedding calls are batched (Gemini's `:batchEmbedContents`, up to 100
texts per request) and retried once on a 429, honoring the wait time
the API's own error names — found necessary on real runs: comparing
many flows and TCMS items one-request-per-text hit the free tier's
rate limit far faster than the actual comparison work would suggest
(see `ROADMAP.md`).

## Project layout

```
flowscout/         crawler, risk classification, dedup, gap analysis,
                    codegen, report rendering, web UI
configs/            example run configs (start here: configs/saucedemo.json)
tests/              pytest suite
ROADMAP.md          the actual engineering log: what's built, real bugs
                    found and fixed via live verification, what's
                    deliberately not built and why
```

`ROADMAP.md` is not a marketing roadmap — it's a running record of
design decisions, live experiments, and real bugs caught by testing
against actual sites (saucedemo.com, httpbin.org, quotes.toscrape.com,
and others) rather than reasoned about in the abstract. If you want to
know *why* something works the way it does, that file has the answer
before the source code does.

## Status

Alpha. Built and verified against public demo/practice sites
(saucedemo.com, httpbin.org, quotes.toscrape.com) and one real,
unaffiliated third-party site during development (referred to as
"Site B" throughout `ROADMAP.md` — not named here since it wasn't a
demo site built for this kind of testing). No test suite existed for
most of this project's history — live verification against real sites
was the primary correctness discipline instead (see `ROADMAP.md`);
`tests/` now covers the parts of that discipline that fit a
deterministic, offline test.

## License

Apache-2.0 — see `LICENSE`.
