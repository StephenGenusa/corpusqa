"""Structured-call helper tests with a mocked task client (M1 acceptance)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from corpusqa.config.schema import TaskName
from corpusqa.errors import StructuredOutputError
from corpusqa.llm.structured import complete_structured
from corpusqa.llm.tasks import Completion

VALID = (
    __import__("pathlib").Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
)


class _Out(BaseModel):
    answer: str
    score: int


class FakeClient:
    """Stands in for LLMTaskClient at the single mock seam."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self, task: TaskName, messages: list[dict[str, str]], **_: Any
    ) -> Completion:
        self.calls.append(messages)
        return Completion(
            text=self._replies.pop(0),
            model="fake",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
        )


def test_valid_json_first_try() -> None:
    client = FakeClient(['{"answer": "ok", "score": 3}'])
    out = asyncio.run(
        complete_structured(client, TaskName.QUERY_ROUTE, [], _Out)  # type: ignore[arg-type]
    )
    assert out == _Out(answer="ok", score=3)
    assert len(client.calls) == 1


def test_fenced_json_is_tolerated() -> None:
    client = FakeClient(['```json\n{"answer": "ok", "score": 3}\n```'])
    out = asyncio.run(
        complete_structured(client, TaskName.QUERY_ROUTE, [], _Out)  # type: ignore[arg-type]
    )
    assert out.score == 3


def test_repair_retry_succeeds() -> None:
    client = FakeClient(
        ['{"answer": "ok"}', '{"answer": "ok", "score": 5}']  # first lacks score
    )
    out = asyncio.run(
        complete_structured(client, TaskName.QUERY_ROUTE, [], _Out)  # type: ignore[arg-type]
    )
    assert out.score == 5
    assert len(client.calls) == 2  # exactly one repair round trip


def test_repair_failure_raises_with_raw_output() -> None:
    client = FakeClient(["not json", "still not json"])
    with pytest.raises(StructuredOutputError) as exc_info:
        asyncio.run(
            complete_structured(client, TaskName.QUERY_ROUTE, [], _Out)  # type: ignore[arg-type]
        )
    assert exc_info.value.raw_output == "still not json"
    assert len(client.calls) == 2  # no repair loops


def test_count_tokens_falls_back_on_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    from corpusqa.config import load_config
    from corpusqa.llm.tasks import LLMTaskClient

    config = load_config(VALID)

    def boom(**_: object) -> int:
        raise ValueError("no tokenizer for provider-prefixed model")

    monkeypatch.setattr(litellm, "token_counter", boom)
    client = LLMTaskClient(config)
    assert client.count_tokens(TaskName.QUERY_ROUTE, "x" * 400) == 100


def test_token_counter_noise_is_contained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No LiteLLM version may leak prints/logs from token counting."""
    import logging
    import sys

    import litellm

    from corpusqa.config import load_config
    from corpusqa.llm.tasks import LLMTaskClient

    def noisy(**_: object) -> int:
        print("LLM Provider NOT provided.")  # print channel
        print("Provider List: ...", file=sys.stderr)  # stderr channel
        logging.getLogger("LiteLLM").error("LLM Provider NOT provided.")
        raise ValueError("boom")

    monkeypatch.setattr(litellm, "token_counter", noisy)
    client = LLMTaskClient(load_config(VALID))
    assert client.count_tokens(TaskName.QUERY_ROUTE, "x" * 400) == 100
    captured = capsys.readouterr()
    assert "Provider" not in captured.out
    assert "Provider" not in captured.err


def test_openrouter_models_registered_at_init() -> None:
    import litellm

    from corpusqa.config import load_config
    from corpusqa.llm.tasks import LLMTaskClient

    LLMTaskClient(load_config(VALID))
    assert "google/gemini-2.5-flash" in litellm.openrouter_models
    # stripped name now resolves; no exception, no noise
    _, provider, _, _ = litellm.get_llm_provider(model="google/gemini-2.5-flash")
    assert provider == "openrouter"
