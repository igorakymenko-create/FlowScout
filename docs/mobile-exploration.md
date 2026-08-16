# FlowScout for native mobile apps — exploratory research (Aug 2026)

Status: **not started, not decided** — this is a "topic to think about" the
user raised, answered from architecture inspection (line counts, which
modules import Playwright vs. which don't), not from any live test against
a real mobile app. Treat every claim below as a hypothesis to verify, not
a plan already validated the way the rest of this project's decisions are
(see `ROADMAP.md` for the live-verification discipline everything else
here was held to). The recommended next step, at the end, is exactly that:
verify the open questions on one real app before designing anything
further.

Scope: native Android/iOS apps (Appium-driven), explicitly not PWAs —
those are already just web pages FlowScout can crawl today.

## The headline finding: most of the codebase is platform-agnostic already

Measured directly from the repo, not estimated:

```
platform-agnostic   3305 lines  (~63%)
web-specific         1880 lines  (~36%)
on the boundary         93 lines  (risk.py)
```

Platform-agnostic: `gap_analysis.py`, `semantic_dedup.py`, `identity.py`,
`shared_steps.py`, `testcase_draft.py`, `project_state.py`,
`change_detection.py`, `report.py`, `tcms.py`, `embeddings.py`,
`models.py`, the CLI, the whole web operator UI. None of it knows what a
browser is — it operates on `Flow`/`Transition`/`StateNode` dataclasses.
The actual value proposition (compare discovered flows against a test
plan) is not web-specific by construction, not by luck.

Web-specific (`actions.py`, `crawler.py`, `fingerprint.py`,
`field_detect.py`, `playwright_codegen.py`): everything that talks to
Playwright/DOM directly.

## Four things that need porting, in order of expected difficulty

### 1. Driver — easiest

Playwright → **Appium**. One API surface for both platforms, a Python
client, and it returns an element tree analogous to the DOM. Alternatives
considered and set aside: native Espresso/XCUITest (platform-specific,
would mean two crawlers not one), Maestro (not introspective enough for
open-ended discovery, built for scripted flows).

### 2. Element discovery — plausibly *easier* than the web version

This is the pleasant surprise. The whole CDP `getEventListeners` + React
fiber saga this project went through (and the real false positive it
found on saucedemo's footer, from React's root-level event delegation)
exists because the DOM doesn't reliably say what's clickable. Accessibility
trees mostly do:

- **Android:** `clickable="true"`, `enabled`, `resource-id` (e.g.
  `com.app:id/login_button`), `content-desc`, `class`
- **iOS:** element type (`XCUIElementTypeButton`), `label`, `enabled`,
  `accessible`

`clickable="true"` is the ground truth CDP was approximating with a
heuristic. `resource-id` on Android is arguably *better* than the web's
mix of `data-test`/`id`/text — stable and content-independent
(`product_title` regardless of which product).

**The hard caveat, not a footnote:** this holds for native Android/iOS
and React Native (maps to real native views). **Flutter, Unity, and game
engines draw to a canvas** — no element tree exists unless the app
explicitly opted into exposing semantics, and unlike the web there's no
CDP-style fallback. A production Flutter app treated as a black box is
plausibly a dead end for this approach entirely. Coverage depends hard on
the target's framework — this has to be measured against the actual
target, not assumed from "mobile automation is mature now."

### 3. State fingerprint — the deepest problem

Web: `state_fingerprint(url_pattern, candidate_signatures)`. The URL is
half the identity, and `normalize_url()` does real work (collapsing
`?id=4` / `?id=5`).

Native has no URL at all. What exists instead:

- **Android:** current Activity name (`driver.current_activity`) — a
  rough path analogue. But modern single-Activity Compose apps return the
  same Activity for the entire app — the signal degrades exactly where the
  industry is moving.
- **iOS:** no equivalent at all. "Which view controller is on screen" is
  not exposed through XCUITest as a black box.

The fingerprint would have to be built almost entirely from the element
tree, with Activity as an optional secondary signal at best. This is a
real loss, not a minor one: the web version deliberately uses the URL as
a stabilizing anchor and the candidate set as the differentiator. Without
that anchor, the candidate set carries the whole burden and becomes more
sensitive to content noise. The principle FlowScout already holds ("the
fingerprint is deliberately blind to content beyond the candidate set")
becomes both more important and harder to hold onto.

### 4. Reset + replay — where the whole strategy might not survive

This is the determinism core of FlowScout: fresh browser context + replay
the path from `start_url` on every DFS step.

Native analogues:

- **Android:** `adb shell pm clear <package>` — wipes app data, a genuine
  equivalent of a fresh context. Works.
- **iOS:** simulator reset, or full reinstall on a real device (tens of
  seconds).

**The cost is the real problem.** Reset+replay is already slow on the
web — this session's own saucedemo crawls ran 90–660 seconds. On mobile:
`pm clear` + relaunch + replaying N steps is plausibly 5–15s *just to get
back to where the crawler already was*, per candidate action. DFS does one
full replay per candidate. On a run shaped like this session's saucedemo
crawls (~100 replays), that's ~17 minutes at 10s/replay for a small app;
iOS reinstall rates would run to hours.

**Two ways out, the second more interesting than the first:**

1. **Android emulator VM snapshots** — save/restore emulator state,
   substantially faster than `pm clear`, genuine isolation. Plausibly the
   answer for Android specifically.

2. **Cheap backtrack with verification.** The web crawler explicitly
   rejected browser-back as "unreliable." But the web crawler *has*
   fingerprints — meaning reliability doesn't have to be assumed, it can
   be checked: press the system Back button, compute the fingerprint,
   compare against what was expected; match → a full reset was just
   avoided for free; mismatch → fall back to a full reset. This turns
   "unreliable, so don't use it" into "cheap, and we catch it when it
   lies." Worth folding back into the *web* crawler too, independent of
   whether mobile ever gets built — the same fingerprint-verified-backtrack
   idea could cut a real, currently-unaddressed cost there.

## Three problems with no web analogue at all

- **System dialogs.** "Allow notifications?", "Allow location access?" are
  OS-level UI, not in the app's own element tree. A crawl would hit these
  on first launch. Appium can dismiss them, but this needs an explicit
  strategy, not an assumption it'll sort itself out.
- **Scroll, everywhere.** The infinite-scroll item this project
  investigated and closed as "doesn't apply, the web crawler never
  scrolls" becomes unavoidable on mobile — most native lists are
  scroll-to-load. This would need to actually be solved, not deferred.
- **Native pickers.** No analogue to `select_option()`: Android's Spinner
  opens an overlay list, iOS's `UIPickerView` needs swipe gestures.
  Radio/checkbox map reasonably cleanly (`checked` on Android, "1"/"0" on
  iOS) — pickers are their own separate piece of work.

Plus entirely new action types with no web equivalent: swipes, long-press,
pull-to-refresh, the hardware/gesture Back action, backgrounding/
foregrounding the app, deep links.

## What transfers almost for free

`risk.py` — 93 lines, almost entirely keyword matching — ports nearly
as-is. Worth noting directly: the fix from the previous session
(`exclude_patterns` matching on label text, not just `href`, because a
button-triggered client-side-routed link has no `href` to match) is
*exactly* the mobile case too — there is never an `href` on native, so
that work is already done for free.

New risk categories that would need adding: in-app purchases (real money,
and the payment sheet is OS-level UI outside the app's own tree),
contacts/photos permission grants, "Rate this app" → App Store handoff,
the share sheet, falling through to an external browser. The mobile
analogue of "external domain" is a change in `driver.current_package`.

## Recommended first step

**Not a driver abstraction.** This project just relearned that lesson
directly, one topic ago: building an abstraction for something that
can't be checked live (the OpenAI/Voyage embeddings providers) produces
scaffolding, not a feature — verified with a real key, one of the two
still doesn't work, and the fix was pausing it, not shipping it further
blind.

Instead: pick **one real Android app**, wire up Appium, and answer three
empirical questions directly against it:

1. **Does the accessibility tree actually produce a usable candidate
   set** on this specific app? (Framework-dependent — measure, don't
   assume.)
2. **Can a stable fingerprint be built without a URL?** Directly
   checkable: does the same screen produce the same fingerprint on repeat
   visits; do different-product detail screens correctly collapse the
   same way saucedemo's `?id=4`/`?id=5` already does on the web.
3. **What does reset+replay actually cost per path, and does
   Back+fingerprint-verification eliminate most of the full resets?**

If all three come back healthy, the rest is normal engineering with a
known shape. If #2 or #3 comes back bad, **the DFS reset+replay strategy
itself may not survive the port**, and this needs a different traversal
model, not a port of the existing one.

Same discipline as everything else in this project: measure the
uncertain thing on a real target first, design second.
