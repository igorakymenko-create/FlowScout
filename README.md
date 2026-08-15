# FlowScout

An autonomous browser agent that crawls a web app, discovers real user
flows, and tells you which of your existing test cases are covered —
and which aren't.

FlowScout does **not** invent expected results and does **not** assert
anything about data correctness. It only reports what it can actually
verify by exploring the app: which flows exist, which of your TCMS test
cases match them, and which don't. That's a narrower promise than "AI
writes your tests for you" — and a more honest one.

## What it does

- **Crawls** a site with Playwright (DFS, isolated browser context per
  path), classifying every clickable action as `safe` / `mutating` /
  `destructive` before ever touching it. Destructive actions (logout,
  leaving the allowed domain, an excluded page) are never followed.
  Mutating ones (checkout, submit, delete...) only run if you opt in.
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
  crawl can tell you what's new, what disappeared, and what moved.
- **Multi-persona**: crawl the same site as multiple logged-in users
  (sequentially — see `ROADMAP.md` for why not in parallel) into one
  report, so admin-only flows and standard-user flows don't collapse
  into each other.
- A **local operator UI** (FastAPI + vanilla JS, no build step) to
  configure and run crawls, attach a TCMS export, and browse reports —
  or drive all of this from the CLI / a CI job instead.

## Install

```bash
pip install flowscout
playwright install chromium
```

The `playwright install` step downloads a Chromium build (~150 MB) —
it's a one-time setup, not a FlowScout-specific quirk, but it's easy to
miss and the first run will fail without it.

For local development instead of a package install:

```bash
git clone <this repo>
cd flowscout
pip install -e ".[dev]"
playwright install chromium
```

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
(saucedemo.com, httpbin.org, quotes.toscrape.com) and one real site
(amfit.net) during development. No test suite existed for most of this
project's history — live verification against real sites was the
primary correctness discipline instead (see `ROADMAP.md`); `tests/`
now covers the parts of that discipline that fit a deterministic,
offline test.

## License

Apache-2.0 — see `LICENSE`.
