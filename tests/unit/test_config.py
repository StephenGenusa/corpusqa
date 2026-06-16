"""Config schema and loader tests (M1 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.errors import ConfigError

VALID = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def test_example_config_loads() -> None:
    """The shipped example must always validate."""
    config = load_config(VALID)
    assert config.providers["ollama_local"].max_concurrency == 2
    assert config.tasks  # all six present, enforced by validator


def test_missing_task_is_rejected(tmp_path: Path) -> None:
    """Omitting a task category fails at load, not mid-run."""
    text = VALID.read_text(encoding="utf-8").replace("  extract:", "  _extract:")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="extract"):
        load_config(bad)


def test_unknown_provider_reference_is_rejected(tmp_path: Path) -> None:
    """A task pointing at an undefined provider is a hard error."""
    text = VALID.read_text(encoding="utf-8").replace(
        "    provider: ollama_local", "    provider: nope", 1
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="nope"):
        load_config(bad)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """Typos in keys fail fast instead of being silently ignored."""
    text = VALID.read_text(encoding="utf-8") + "\nbudgett:\n  x: 1\n"
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    """A nonexistent path raises ConfigError, not OSError."""
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.yaml")
