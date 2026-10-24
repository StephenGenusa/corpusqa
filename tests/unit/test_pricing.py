"""Cost accounting never books an unknown paid model at $0.

A model missing from LiteLLM's cost map, with no config override, is
*unpriced*: the estimate says so and the budget gate asks before spending.
Only a provider on a loopback address is assumed free. A fallback model that
cannot be priced is flagged per call rather than recorded as free.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from corpusqa.catalog.summarizer import CardReport
from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, ProviderConfig, TaskName
from corpusqa.errors import BudgetExceededError
from corpusqa.llm import tasks as llm_tasks
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.query import pipeline as qp
from corpusqa.query.budget import CostEstimate, estimate_pass1, estimate_query
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
UNKNOWN_REMOTE = "openrouter/acme/no-such-model-for-tests"
UNKNOWN_LOCAL = "ollama/no-such-model-for-tests:1b"


def _config() -> AppConfig:
    return load_config(EXAMPLE)


def _unknown_remote(config: AppConfig, task: TaskName) -> None:
    cfg = config.tasks[task]
    cfg.model = UNKNOWN_REMOTE
    cfg.provider = "openrouter"
    cfg.cost_per_mtok_in = None
    cfg.cost_per_mtok_out = None


# --- estimates ------------------------------------------------------------


def test_unknown_remote_model_is_unpriced_in_estimate() -> None:
    config = _config()
    _unknown_remote(config, TaskName.SYNTHESIZE)
    est = estimate_query([1_000], config)
    assert est.unpriced_tasks == ("synthesize",)


def test_unknown_model_on_loopback_provider_is_free() -> None:
    config = _config()
    config.providers["lan"] = ProviderConfig(api_base="http://127.0.0.1:1234")
    cfg = config.tasks[TaskName.SYNTHESIZE]
    cfg.model = UNKNOWN_LOCAL
    cfg.provider = "lan"
    cfg.cost_per_mtok_in = None
    cfg.cost_per_mtok_out = None
    est = estimate_query([1_000], config)
    assert est.unpriced_tasks == ()
    assert est.per_task_usd["synthesize"] == 0.0


def test_unknown_model_on_remote_lan_host_is_not_assumed_free() -> None:
    config = _config()
    config.providers["lan"] = ProviderConfig(api_base="http://192.168.1.20:11434")
    cfg = config.tasks[TaskName.SYNTHESIZE]
    cfg.model = UNKNOWN_LOCAL
    cfg.provider = "lan"
    cfg.cost_per_mtok_in = None
    cfg.cost_per_mtok_out = None
    assert estimate_query([1_000], config).unpriced_tasks == ("synthesize",)


def test_pass1_estimate_reports_unpriced_catalog_model() -> None:
    config = _config()
    _unknown_remote(config, TaskName.CATALOG_SUMMARIZE)
    config.tasks[TaskName.QUERY_ROUTE].cost_per_mtok_in = 0.3
    config.tasks[TaskName.QUERY_ROUTE].cost_per_mtok_out = 2.5
    est = estimate_pass1([1_000], 1, config)
    assert est.unpriced_tasks == ("catalog_summarize",)


def test_tiny_cost_is_not_rounded_to_zero() -> None:
    config = _config()
    config.tasks[TaskName.EXTRACT].cost_per_mtok_in = 1.0
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_in = 0.0
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_out = 0.0
    est = estimate_query([4], config)  # $0.000004: below 4-decimal rounding
    assert est.total_usd > 0.0


# --- the gate -------------------------------------------------------------


def test_gate_asks_when_a_task_is_unpriced_even_under_threshold() -> None:
    config = _config()
    config.budget.confirm_above_usd = 100.0
    asked: list[CostEstimate] = []

    def confirm(est: CostEstimate) -> bool:
        asked.append(est)
        return False

    est = CostEstimate(total_usd=0.0, unpriced_tasks=("synthesize",))
    with pytest.raises(BudgetExceededError, match="synthesize"):
        qp._check_budget_gate(est, config, confirm, "extraction+synthesis")
    assert asked == [est]


def test_gate_passes_unpriced_estimate_when_confirmed() -> None:
    config = _config()
    est = CostEstimate(total_usd=0.0, unpriced_tasks=("synthesize",))
    qp._check_budget_gate(est, config, lambda _e: True, "extraction+synthesis")


def test_gate_still_free_for_fully_priced_zero_cost_run() -> None:
    config = _config()
    est = CostEstimate(total_usd=0.0)
    qp._check_budget_gate(est, config, None, "extraction+synthesis")


# --- actual call accounting ---------------------------------------------


def _fake_response(model: str) -> Any:  # noqa: ANN401 -- stands in for LiteLLM's response
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(prompt_tokens=1_000, completion_tokens=500),
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        ],
    )


def _run_extract(monkeypatch: pytest.MonkeyPatch, served: str) -> LLMTaskClient:
    async def fake_acompletion(**_: Any) -> Any:  # noqa: ANN401
        return _fake_response(served)

    monkeypatch.setattr(llm_tasks.litellm, "acompletion", fake_acompletion)
    client = LLMTaskClient(_config())
    asyncio.run(client.complete(TaskName.EXTRACT, [{"role": "user", "content": "x"}]))
    return client


def test_unpriced_fallback_call_is_flagged_not_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # extract is local with $0 overrides; the fallback that served the call
    # is a paid model LiteLLM cannot price.
    client = _run_extract(monkeypatch, served="acme/no-such-fallback-for-tests")
    assert client.unpriced_by_task() == {TaskName.EXTRACT: 1}


def test_primary_call_with_override_is_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _run_extract(monkeypatch, served="ollama/qwen3:14b")
    assert client.unpriced_by_task() == {}
    assert client.usage_by_task()[TaskName.EXTRACT][2] == 0.0


# --- surfacing ------------------------------------------------------------


def test_query_report_counts_unpriced_calls(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    fake = FakeClient(
        {TaskName.EXTRACT: [_extraction_json()]}, unpriced={TaskName.EXTRACT}
    )
    report = asyncio.run(
        qp.run_query(
            corpus, "q", _config(), mode="sweep", client=fake, confirm=lambda _e: True
        )
    )
    assert report.unpriced_calls == {TaskName.EXTRACT: 1}


def test_cli_warns_when_calls_were_unpriced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import corpusqa.cli.main as cli

    async def fake_run_query(*_: object, **__: object) -> qp.QueryReport:
        return qp.QueryReport(
            answer=None,
            candidates=[],
            relevant_files=[],
            estimate=CostEstimate(),
            card_report=CardReport(),
            unpriced_calls={TaskName.EXTRACT: 2},
        )

    monkeypatch.setattr(cli, "run_query", fake_run_query)
    monkeypatch.setattr(cli, "_warn_drift", lambda *_: None)
    cli.main(["query", "q", ".", "--config", str(EXAMPLE), "--yes"])
    err = capsys.readouterr().err
    assert "2 extract call(s)" in err and "no known price" in err


def test_confirm_prompt_names_unpriced_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import corpusqa.cli.main as cli

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert not cli._confirm(CostEstimate(unpriced_tasks=("synthesize",)))
    out = capsys.readouterr().out
    assert "unpriced" in out and "synthesize" in out


def _extraction_json() -> str:
    return json.dumps(
        {"file_hash": "a" * 64, "relevant": False, "confidence": 0.9, "findings": []}
    )


def test_estimate_command_names_unpriced_tasks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import corpusqa.cli.main as cli

    async def fake_estimate(
        *_: object, **__: object
    ) -> tuple[list[object], CostEstimate, CardReport]:
        return [], CostEstimate(unpriced_tasks=("catalog_summarize",)), CardReport()

    monkeypatch.setattr(cli, "run_estimate", fake_estimate)
    cli.main(["estimate", "q", ".", "--config", str(EXAMPLE), "--yes"])
    out = capsys.readouterr().out
    assert "unpriced" in out and "catalog_summarize" in out


# --- half-set overrides -------------------------------------------------


def test_half_override_takes_other_side_from_cost_map() -> None:
    from corpusqa.llm.pricing import task_rate

    config = _config()
    cfg = config.tasks[TaskName.QUERY_ROUTE]  # openrouter/google/gemini-2.5-flash
    cfg.cost_per_mtok_in = 3.0
    cfg.cost_per_mtok_out = None
    rate = task_rate(cfg, config.providers[cfg.provider])
    assert rate is not None
    assert rate[0] == 3.0
    assert rate[1] == pytest.approx(2.5)  # from LiteLLM, not $0


def test_half_override_on_unknown_model_is_unpriced() -> None:
    config = _config()
    _unknown_remote(config, TaskName.SYNTHESIZE)
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_in = 3.0
    assert estimate_query([1_000], config).unpriced_tasks == ("synthesize",)


def test_half_override_on_loopback_model_is_free_on_other_side() -> None:
    from corpusqa.llm.pricing import task_rate

    config = _config()
    cfg = config.tasks[TaskName.EXTRACT]  # ollama on localhost
    cfg.model = UNKNOWN_LOCAL
    cfg.cost_per_mtok_in = 0.5
    cfg.cost_per_mtok_out = None
    assert task_rate(cfg, config.providers[cfg.provider]) == (0.5, 0.0)


def test_completion_records_when_a_fallback_served_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _run_extract(monkeypatch, served="acme/no-such-fallback-for-tests")
    assert [c.fallback for c in client._ledger[TaskName.EXTRACT]] == [True]


def test_completion_from_primary_is_not_marked_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _run_extract(monkeypatch, served="ollama/qwen3:14b")
    assert [c.fallback for c in client._ledger[TaskName.EXTRACT]] == [False]
