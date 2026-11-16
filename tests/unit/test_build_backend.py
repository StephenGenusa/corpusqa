"""Decision logic of scripts/build_backend.py (no build is run)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_backend.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_backend", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("uv_available", "unpinned", "expected"),
    [
        (True, False, "pinned"),
        (False, False, "fail"),
        (False, True, "unpinned"),
        (True, True, "unpinned"),
    ],
)
def test_pin_mode(uv_available: bool, unpinned: bool, expected: str) -> None:
    assert _load().pin_mode(uv_available, unpinned) == expected


def test_missing_uv_fails_without_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    build_backend = _load()
    monkeypatch.setattr(build_backend.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit) as exc:
        build_backend._lock_constraints(unpinned=False)
    message = str(exc.value)
    assert "uv is required to pin to uv.lock" in message
    assert "--unpinned" in message


def test_missing_uv_fails_before_venv_or_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_backend = _load()
    calls: list[str] = []
    monkeypatch.setattr(build_backend.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        build_backend.venv, "EnvBuilder", lambda *a, **k: calls.append("venv")
    )
    monkeypatch.setattr(
        build_backend.subprocess, "run", lambda *a, **k: calls.append("run")
    )
    with pytest.raises(SystemExit) as exc:
        build_backend.main([])
    assert "uv is required" in str(exc.value)
    assert calls == []
