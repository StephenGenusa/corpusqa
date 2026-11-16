"""Comment-preserving config writer tests."""

from __future__ import annotations

import difflib
import json
import os
import stat
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.config.writer import config_as_dict, save_config_changes
from corpusqa.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
COMMENT = "# full recall on Elpis Israel; 3000+ drops passages (2026-10-24)"


def _write(tmp_path: Path) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "    # max_input_tokens: 4000\n",
        f"    max_input_tokens: 2500   {COMMENT}\n",
    )
    assert COMMENT in text
    p = tmp_path / "corpusqa.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_change_preserves_comments(tmp_path: Path) -> None:
    p = _write(tmp_path)
    save_config_changes(p, [(("tasks", "extract", "temperature"), 0.4)])
    assert COMMENT in p.read_text(encoding="utf-8")
    assert load_config(p).tasks[TaskName.EXTRACT].temperature == 0.4


def test_invalid_value_rejected_and_file_unchanged(tmp_path: Path) -> None:
    p = _write(tmp_path)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match=r"tasks\.extract\.temperature"):
        save_config_changes(p, [(("tasks", "extract", "temperature"), 9.0)])
    assert p.read_text(encoding="utf-8") == before


def test_only_changed_lines_differ(tmp_path: Path) -> None:
    p = _write(tmp_path)
    before = p.read_text(encoding="utf-8").splitlines()
    save_config_changes(p, [(("tasks", "extract", "max_tokens"), 1024)])
    after = p.read_text(encoding="utf-8").splitlines()
    changed = [
        d
        for d in difflib.unified_diff(before, after, lineterm="", n=0)
        if d[:1] in "+-" and d[:3] not in ("+++", "---")
    ]
    assert changed == ["-    max_tokens: 2048", "+    max_tokens: 1024"]


def test_none_deletes_key(tmp_path: Path) -> None:
    p = _write(tmp_path)
    save_config_changes(p, [(("tasks", "extract", "max_input_tokens"), None)])
    assert load_config(p).tasks[TaskName.EXTRACT].max_input_tokens is None
    assert COMMENT not in p.read_text(encoding="utf-8")


def test_creates_missing_mapping_keys(tmp_path: Path) -> None:
    p = _write(tmp_path)
    text = p.read_text(encoding="utf-8")
    p.write_text(text[: text.index("budget:")], encoding="utf-8")
    config = save_config_changes(p, [(("budget", "confirm_above_usd"), 2.5)])
    assert config.budget.confirm_above_usd == 2.5


def test_config_as_dict_is_json_safe(tmp_path: Path) -> None:
    data = config_as_dict(load_config(_write(tmp_path)))
    json.dumps(data)
    tasks = data["tasks"]
    assert isinstance(tasks, dict) and "extract" in tasks


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_mode_preserved(tmp_path: Path) -> None:
    p = _write(tmp_path)
    p.chmod(0o644)
    save_config_changes(p, [(("tasks", "extract", "temperature"), 0.4)])
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_symlinked_config_stays_symlink(tmp_path: Path) -> None:
    target = _write(tmp_path)
    link = tmp_path / "link.yaml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not creatable")
    save_config_changes(link, [(("tasks", "extract", "temperature"), 0.4)])
    assert link.is_symlink()
    assert load_config(target).tasks[TaskName.EXTRACT].temperature == 0.4
