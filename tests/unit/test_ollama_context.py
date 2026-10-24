"""Ollama runs with the context window the config budgets for.

corpusqa sizes route shards, extract sections and synthesis input from each
task's ``context_window``. Ollama allocates its own window (``num_ctx``,
otherwise the server default) and silently truncates prompts that exceed it,
so the two must not disagree: Ollama calls send ``num_ctx`` explicitly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.llm import tasks as llm_tasks
from corpusqa.llm.tasks import LLMTaskClient
from tests.unit.test_pricing import _config, _fake_response


def _sent_kwargs(
    monkeypatch: pytest.MonkeyPatch, config: AppConfig, task: TaskName
) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:  # noqa: ANN401
        sent.update(kwargs)
        return _fake_response(kwargs["model"])

    monkeypatch.setattr(llm_tasks.litellm, "acompletion", fake_acompletion)
    client = LLMTaskClient(config)
    asyncio.run(client.complete(task, [{"role": "user", "content": "x"}]))
    return sent


@pytest.mark.parametrize("prefix", ["ollama/", "ollama_chat/"])
def test_ollama_call_sends_context_window_as_num_ctx(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    config = _config()
    cfg = config.tasks[TaskName.EXTRACT]
    cfg.model = prefix + "gemma4:e4b"
    cfg.context_window = 16_384
    sent = _sent_kwargs(monkeypatch, config, TaskName.EXTRACT)
    assert sent["num_ctx"] == 16_384


def test_non_ollama_call_does_not_send_num_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    cfg = config.tasks[TaskName.SYNTHESIZE]
    cfg.model = "openrouter/anthropic/claude-sonnet-4.5"
    cfg.provider = "openrouter"
    config.providers["openrouter"].api_key_env = None
    sent = _sent_kwargs(monkeypatch, config, TaskName.SYNTHESIZE)
    assert "num_ctx" not in sent
