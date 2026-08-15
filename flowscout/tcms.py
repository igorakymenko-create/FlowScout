"""Minimal TCMS import: a CSV export of an existing test plan.

Deliberately format-agnostic rather than tied to one vendor (TestRail /
Xray / Zephyr all export CSV with slightly different column names) --
column matching is case-insensitive and accepts a few common synonyms.
Only `id` and `title` are required; `steps` is optional but improves
match quality since it gives the embedding more to work with than a
short title alone.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

_TITLE_COLUMNS = ["title", "name", "summary", "test case", "test_case"]
_STEPS_COLUMNS = ["steps", "description", "preconditions", "test_steps"]
_ID_COLUMNS = ["id", "case_id", "test_id", "key"]


@dataclass
class TcmsItem:
    id: str
    title: str
    steps: str = ""

    def text(self) -> str:
        return f"{self.title}. {self.steps}".strip()


def _find_column(fieldnames: list[str], candidates: list[str]) -> str | None:
    lower = {f.lower().strip(): f for f in fieldnames}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def load_tcms_csv(path: str) -> list[TcmsItem]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        id_col = _find_column(fieldnames, _ID_COLUMNS)
        title_col = _find_column(fieldnames, _TITLE_COLUMNS)
        steps_col = _find_column(fieldnames, _STEPS_COLUMNS)
        if not title_col:
            raise ValueError(
                f"Could not find a title/name/summary column in {path}. "
                f"Columns found: {fieldnames}"
            )
        items = []
        for i, row in enumerate(reader, start=1):
            item_id = (row.get(id_col) if id_col else None) or f"row-{i}"
            title = (row.get(title_col) or "").strip()
            if not title:
                continue
            steps = (row.get(steps_col) or "").strip() if steps_col else ""
            items.append(TcmsItem(id=item_id.strip(), title=title, steps=steps))
        return items
