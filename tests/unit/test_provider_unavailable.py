"""A provider error no retry can fix stops the run instead of every call failing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from litellm import exceptions as litellm_errors

from corpusqa.config import load_config
from corpusqa.config.schema import ProviderConfig, TaskName
from corpusqa.errors import LLMError, ProviderUnavailableError
from corpusqa.ingest.pipeline import run_index
from corpusqa.llm import tasks as llm_tasks
from corpusqa.llm.tasks import Completion, LLMTaskClient, gather_or_stop
from corpusqa.query import pipeline as qp
from corpusqa.query.extractor import extract_file
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
_NO_MODEL = (
    "Lm_studioException - No models loaded. Please load a model in the "
    "developer page or use the 'lms load' command."
)


def _complete_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> Exception:
    """Runs one LM Studio extract call whose LiteLLM call raises ``exc``."""
    cfg = load_config(EXAMPLE)
    cfg.providers["box"] = ProviderConfig(
        kind="lm_studio", api_base="http://localhost:1234/v1"
    )
    cfg.tasks[TaskName.EXTRACT] = cfg.tasks[TaskName.EXTRACT].model_copy(
        update={"model": "lm_studio/qwen", "provider": "box"}
    )

    async def fake_acompletion(**_: Any) -> Any:  # noqa: ANN401
        raise exc

    monkeypatch.setattr(llm_tasks.litellm, "acompletion", fake_acompletion)
    with pytest.raises(LLMError) as info:
        asyncio.run(
            LLMTaskClient(cfg).complete(
                TaskName.EXTRACT, [{"role": "user", "content": "x"}]
            )
        )
    return info.value


def test_no_model_loaded_is_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = _complete_raising(
        monkeypatch,
        litellm_errors.BadRequestError(_NO_MODEL, "qwen", "lm_studio"),
    )
    assert isinstance(err, ProviderUnavailableError)
    assert "No models loaded" in str(err)


def test_auth_failure_is_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = _complete_raising(
        monkeypatch,
        litellm_errors.AuthenticationError("bad key", "openai", "qwen"),
    )
    assert isinstance(err, ProviderUnavailableError)


def test_context_overflow_is_an_ordinary_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = _complete_raising(
        monkeypatch,
        litellm_errors.ContextWindowExceededError("too long", "qwen", "lm_studio"),
    )
    assert not isinstance(err, ProviderUnavailableError)


class _UnavailableClient(FakeClient):
    """First call fails fatally; any other call blocks until cancelled."""

    def __init__(self) -> None:
        super().__init__({})
        self.started = 0

    async def complete(
        self, task: TaskName, messages: list[dict[str, str]], **_: Any
    ) -> Completion:
        self.started += 1
        if self.started == 1:
            await asyncio.sleep(0)
            raise ProviderUnavailableError("no models loaded")
        await asyncio.sleep(60)
        raise AssertionError("a sibling call was not cancelled")


def test_extract_file_stops_at_first_fatal_section() -> None:
    fake = _UnavailableClient()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                "one\n\ntwo\n\nthree",
                fake,  # type: ignore[arg-type]
                load_config(EXAMPLE),
                sections=["one", "two", "three"],
            )
        )
    assert fake.started == 1


def test_gather_or_stop_cancels_the_rest() -> None:
    cancelled: list[int] = []

    async def slow(i: int) -> int:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(i)
            raise
        return i

    async def fatal() -> int:
        await asyncio.sleep(0)
        raise ProviderUnavailableError("down")

    async def main() -> None:
        await gather_or_stop([slow(1), fatal(), slow(2)])

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(asyncio.wait_for(main(), timeout=5))
    assert sorted(cancelled) == [1, 2]


def test_run_query_stops_on_provider_unavailable(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.md").write_text(f"# F{i}\n\nText {i}.", encoding="utf-8")
    config = load_config(EXAMPLE)
    run_index(tmp_path, config)
    fake = _UnavailableClient()

    async def run() -> None:
        await qp.run_query(
            tmp_path, "q", config, mode="sweep", client=fake, confirm=lambda _e: True
        )

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(asyncio.wait_for(run(), timeout=10))
