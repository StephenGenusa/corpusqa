"""Minimal ``.env`` loader (no third-party dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def _parse_value(raw: str) -> str:
    """Strips whitespace and one pair of matching surrounding quotes."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def load_dotenv(path: Path) -> list[str]:
    """Loads ``KEY=VALUE`` lines into ``os.environ`` without overriding.

    Blank lines, ``#`` comments and lines without ``=`` are skipped; an
    optional ``export `` prefix and surrounding quotes are accepted.

    Args:
        path: The ``.env`` file; a missing file is not an error.

    Returns:
        Names of the variables that were set (already-set ones are kept).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    set_names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or not key or key in os.environ:
            continue
        os.environ[key] = _parse_value(raw)
        set_names.append(key)
    return set_names
