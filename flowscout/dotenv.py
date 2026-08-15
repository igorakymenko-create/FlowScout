"""Minimal .env loader -- no python-dotenv dependency, just enough to
pull GEMINI_API_KEY (and similar) out of a local, gitignored file
instead of requiring it set in the shell every session.

Precedence: real environment variables win over both files; .env.local
wins over .env. Neither file is required to exist.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(*paths: str, base_dir: Path | None = None) -> None:
    base = base_dir or Path.cwd()
    for name in paths:
        path = base / name
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)  # existing env vars / earlier files always win
