"""Logging setup and LLM call capture (publish-prep)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.logging_setup import configure

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def test_configure_writes_run_file(tmp_path: Path) -> None:
    log_path = configure(verbosity=1, log_dir=tmp_path / "logs")
    assert log_path is not None and log_path.parent.exists()
    logging.getLogger("corpusqa.test").info("hello key=value")
    logging.getLogger("corpusqa").handlers[1].flush()
    assert "hello key=value" in log_path.read_text(encoding="utf-8")


def test_configure_stderr_only_for_readonly() -> None:
    assert configure(verbosity=0, log_dir=None) is None


def test_vv_captures_full_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    async def fake_acompletion(**_: Any) -> Any:
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
            choices=[SimpleNamespace(message=SimpleNamespace(content="REPLY"))],
            model="fake-model",
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    log_path = configure(verbosity=2, log_dir=tmp_path / "logs")
    client = LLMTaskClient(load_config(EXAMPLE))
    asyncio.run(
        client.complete(
            TaskName.QUERY_ROUTE, [{"role": "user", "content": "SECRET-PROMPT"}]
        )
    )
    for handler in logging.getLogger("corpusqa").handlers:
        handler.flush()
    assert log_path is not None
    text = log_path.read_text(encoding="utf-8")
    assert "task=query_route" in text  # INFO summary
    assert "SECRET-PROMPT" in text  # DEBUG prompt capture
    assert "REPLY" in text  # DEBUG response capture


def test_v1_does_not_capture_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    async def fake_acompletion(**_: Any) -> Any:
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[SimpleNamespace(message=SimpleNamespace(content="R"))],
            model="m",
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    log_path = configure(verbosity=1, log_dir=tmp_path / "logs")
    client = LLMTaskClient(load_config(EXAMPLE))
    asyncio.run(
        client.complete(TaskName.QUERY_ROUTE, [{"role": "user", "content": "P"}])
    )
    for handler in logging.getLogger("corpusqa").handlers:
        handler.flush()
    assert log_path is not None
    text = log_path.read_text(encoding="utf-8")
    assert "task=query_route" in text
    assert "messages=" not in text  # no prompt capture at -v
