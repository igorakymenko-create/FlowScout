"""Flow -> pytest-playwright test spec (Python, matching this project's
own stack -- no separate Node toolchain needed to even read what was
generated).

Locators use the same priority actions.build_locator() uses at crawl
time (data-test > id > href > text), rebuilt here as source code rather
than called live. Text-based locators are flagged fragile in the output
-- not hypothetically: on Site B, a real trilingual site crawled this
project, roughly half of discovered elements had neither data-test nor
id, so a locale switch would break every text-based locator in a
generated suite outright.

Credentials are never inlined. A form-submit step's field NAMES are on
Transition.form_fields (crawl-time; masked/synthetic VALUES are
deliberately not carried over -- see models.py's Transition docstring).
Generated .fill() calls read real values from os.environ under a
FLOWSCOUT_<FIELD_NAME> convention; anything unset falls back to an
unmissable "TODO_SET_..." placeholder rather than silently reusing
whatever synthetic string the crawl happened to use.

Structural assertions (the flow's own observed, repeatable end state --
guaranteed reproducible by the same isolated-context replay the crawler
itself relies on) are emitted directly. Behavioral assertions are a
single labeled TODO block: FlowScout knows what the app DID, never what
it SHOULD do, and doesn't pretend otherwise (see ROADMAP.md M4).
"""
from __future__ import annotations

import json
import re

from .identity import flow_identity
from .models import RunResult, Transition
from .shared_steps import FlowSplit

# Shared header for a combined output file -- render_pytest() below
# returns only a function body, so multiple flows' tests can share one
# import block instead of each repeating it.
PYTEST_IMPORTS = """import os
import re

from playwright.sync_api import Page, expect
"""


def _env_var(field_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", field_name).strip("_").upper()
    return f"FLOWSCOUT_{safe}"


def _locator_expr(el_meta: dict) -> tuple[str, bool]:
    """(python expression, is_fragile). Mirrors actions.build_locator's
    priority; text fallback is the one strategy flagged fragile."""
    if el_meta.get("dataTest"):
        selector = f'[data-test="{el_meta["dataTest"]}"]'
        return f"page.locator({json.dumps(selector)}).first", False
    if el_meta.get("id"):
        selector = f'#{el_meta["id"]}'
        return f"page.locator({json.dumps(selector)}).first", False
    if el_meta.get("href"):
        selector = f'{el_meta.get("tag", "a")}[href="{el_meta["href"]}"]'
        return f"page.locator({json.dumps(selector)}).first", False
    text = el_meta.get("text", "")
    return f"page.get_by_text({json.dumps(text)}, exact=True).first", True


def _url_pattern_to_regex(pattern: str) -> str:
    """normalize_url() marks per-instance segments with '*' -- turn that
    back into a real wildcard after escaping everything else."""
    return re.escape(pattern).replace(re.escape("*"), ".*")


def _step_code(t: Transition, step_num: int) -> tuple[list[str], bool]:
    """Returns un-indented lines -- render_pytest applies indentation
    once, uniformly, to every body line (steps, structural check, TODO
    block alike). Indenting here too would double it."""
    el_meta = json.loads(t.replay_meta) if t.replay_meta else {}
    lines = [f"# Step {step_num}: {t.action_label}"]

    if t.form_fields:
        for fname in t.form_fields:
            env = _env_var(fname)
            # We only ever recorded the field's *name* at crawl time, not
            # which attribute it actually was (name/id/placeholder are all
            # candidates -- see actions.fill_enclosing_form) -- so match
            # whichever one is real via a CSS selector list rather than
            # guessing wrong.
            guess = f'[name="{fname}"], #{fname}, [placeholder="{fname}"]'
            lines.append(
                f'page.fill({json.dumps(guess)}, os.environ.get({json.dumps(env)}, "TODO_SET_{env}"))'
            )
        lines.append("# NOTE: field selector guessed from name/id/placeholder -- verify it matches your form.")

    expr, is_fragile = _locator_expr(el_meta)
    if el_meta.get("tag") == "select":
        lines.append(f"{expr}.select_option(value={json.dumps(el_meta.get('selectValue', ''))})")
    else:
        lines.append(f"{expr}.click()")
    if is_fragile:
        lines.append("# NOTE: text-based locator -- fragile against i18n/content changes; "
                      "no data-test or id found for this element.")
    return lines, is_fragile


def render_pytest(draft_id: str, split: FlowSplit, prefix: list[Transition], run: RunResult) -> tuple[str, bool]:
    """Returns (python source, any_fragile_step)."""
    body: list[str] = []
    fragile_any = False
    for i, t in enumerate(prefix + split.remainder, start=1):
        lines, fragile = _step_code(t, i)
        fragile_any = fragile_any or fragile
        body += lines
        body.append("")

    end_state = run.states.get(split.flow.end_state_fp)
    if end_state:
        regex = _url_pattern_to_regex(end_state.url_pattern)
        body += [
            "# Structural check: observed during the crawl, reproducible -- NOT a behavioral assertion.",
            f'expect(page).to_have_url(re.compile(r"{regex}"))',
            "",
        ]

    body += [
        "# TODO(operator): behavioral assertions go here. FlowScout only knows",
        "# what the app DID during the crawl, not what it SHOULD do -- e.g. order",
        "# totals, error message text, anything specific to your business rules.",
    ]
    body_indented = "\n".join(f"    {line}" if line else "" for line in body)

    func_name = f"test_{re.sub(r'[^a-z0-9]+', '_', draft_id.lower()).strip('_')}"
    identity = flow_identity(split.flow, run.states)
    func_def = f'''def {func_name}(page: Page):
    """FlowScout-generated draft ({draft_id}).
    Flow identity: {identity}
    Structural checks below are observed facts, reproducible via the
    same isolated-context replay the crawler itself uses. Behavioral
    assertions are intentionally left as a TODO -- see module docstring.
    """
    page.goto({json.dumps(run.start_url)})

'''
    return func_def + body_indented + "\n", fragile_any
