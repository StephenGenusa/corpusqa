"""Per-task input budgets: how much document text one call may carry.

Each task's ``max_input_tokens`` sits beside the model it was tuned for. It
lowers the window ceiling (window minus prompt scaffolding minus the output
reservation) and never raises it. A capped value warns once, not once per
file. Requests are sized for the primary model, so a fallback whose known
window cannot hold them is reported.
"""

from __future__ import annotations

import logging
from pathlib import Path

import litellm
import pytest

from corpusqa.catalog.summarizer import card_token_budget
from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.query.extractor import section_token_budget

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _config() -> AppConfig:
    return load_config(EXAMPLE)


def test_catalog_chunks_use_the_catalog_tasks_own_budget() -> None:
    config = _config()
    config.tasks[TaskName.CATALOG_SUMMARIZE].max_input_tokens = 3_000
    config.tasks[TaskName.EXTRACT].max_input_tokens = 2_500
    assert card_token_budget(config) == 3_000
    assert section_token_budget(config) == 2_500


def test_card_budget_reserves_the_catalog_models_output() -> None:
    config = _config()
    cfg = config.tasks[TaskName.CATALOG_SUMMARIZE]
    cfg.context_window = 16_384
    cfg.max_tokens = 800
    assert card_token_budget(config) == 16_384 - 1_500 - 800


def test_a_capped_budget_warns_once_not_per_file(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 10_000_000
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            section_token_budget(config)
    assert caplog.text.count("max_input_tokens") == 1


def test_fallback_with_a_smaller_known_window_is_reported(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        litellm.model_cost, "acme/tiny-for-tests", {"max_input_tokens": 4_096}
    )
    config = _config()
    cfg = config.tasks[TaskName.EXTRACT]
    cfg.context_window = 32_768
    cfg.max_input_tokens = 8_000
    cfg.fallbacks = ["openrouter/acme/tiny-for-tests"]
    with caplog.at_level(logging.WARNING):
        section_token_budget(config)
    assert "openrouter/acme/tiny-for-tests" in caplog.text


def test_fallback_with_a_large_enough_window_is_quiet(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        litellm.model_cost, "acme/roomy-for-tests", {"max_input_tokens": 131_072}
    )
    config = _config()
    config.tasks[TaskName.EXTRACT].fallbacks = ["acme/roomy-for-tests"]
    with caplog.at_level(logging.WARNING):
        section_token_budget(config)
    assert "acme/roomy-for-tests" not in caplog.text


@pytest.mark.parametrize("task", [TaskName.QUERY_ROUTE, TaskName.SYNTHESIZE])
def test_max_input_tokens_on_a_task_that_ignores_it_is_rejected(
    task: TaskName,
) -> None:
    data = _config().model_dump(mode="json")
    data["tasks"][task.value]["max_input_tokens"] = 2_000
    with pytest.raises(ValueError, match=f"{task.value}.*max_input_tokens"):
        AppConfig.model_validate(data)
