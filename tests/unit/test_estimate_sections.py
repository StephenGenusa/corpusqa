"""The pre-flight estimate counts every extract call, not one per file.

A file larger than one section is read in several calls, each carrying the
prompt scaffolding and each able to return up to ``max_tokens``. Projecting
one call per file let a book with a paid extract model pass the gate at a
fraction of its cost. Files with cached verdicts make no calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.ingest.pipeline import index_paths
from corpusqa.query import pipeline as qp
from corpusqa.query.budget import (
    EXTRACT_PROMPT_OVERHEAD_TOKENS,
    CostEstimate,
    estimate_query,
)
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
# 40 headed pages of ~100 fake tokens each.
BOOK = "".join(f"## Page {i}\n" + "word " * 78 + "\n" for i in range(40))


def _priced_config() -> AppConfig:
    config = load_config(EXAMPLE)
    config.tasks[TaskName.EXTRACT].cost_per_mtok_in = 1.0
    config.tasks[TaskName.EXTRACT].cost_per_mtok_out = 1.0
    return config


def test_estimate_projects_every_section_call() -> None:
    config = _priced_config()
    max_tokens = config.tasks[TaskName.EXTRACT].max_tokens
    est = estimate_query([[1_000, 1_000, 1_000]], config)
    extract_in = 3_000 + 3 * EXTRACT_PROMPT_OVERHEAD_TOKENS
    extract_out = 3 * max_tokens
    assert est.per_task_usd["extract"] == pytest.approx(
        (extract_in + extract_out) / 1_000_000
    )
    assert est.candidate_files == 1


def _book_corpus(tmp_path: Path) -> Path:
    corpus = _indexed_corpus(tmp_path)
    cache_dir = index_paths(corpus)[2]
    (cache_dir / f"{'a' * 64}.md").write_text(BOOK, encoding="utf-8")
    return corpus


def _reply() -> str:
    return json.dumps(
        {"file_hash": "a" * 64, "relevant": False, "confidence": 0.9, "findings": []}
    )


def _sweep(corpus: Path, config: AppConfig, replies: int) -> CostEstimate:
    seen: list[CostEstimate] = []

    def confirm(est: CostEstimate) -> bool:
        seen.append(est)
        return True

    fake = FakeClient({TaskName.EXTRACT: [_reply()] * replies})
    asyncio.run(
        qp.run_query(
            corpus,
            "q",
            config,
            mode="sweep",
            client=fake,
            confirm=confirm,  # type: ignore[arg-type]
        )
    )
    return seen[-1]


def test_run_query_estimate_counts_the_planned_sections(tmp_path: Path) -> None:
    corpus = _book_corpus(tmp_path)
    config = _priced_config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 1_000
    sections = qp.plan_sections(BOOK, FakeClient({}), config)  # type: ignore[arg-type]
    assert len(sections) > 1
    est = _sweep(corpus, config, replies=len(sections))
    max_tokens = config.tasks[TaskName.EXTRACT].max_tokens
    assert est.per_task_usd["extract"] >= (
        len(sections) * (EXTRACT_PROMPT_OVERHEAD_TOKENS + max_tokens) / 1_000_000
    )


def test_cached_files_add_nothing_to_the_estimate(tmp_path: Path) -> None:
    corpus = _book_corpus(tmp_path)
    config = _priced_config()
    _sweep(corpus, config, replies=1)
    est = _sweep(corpus, config, replies=0)
    assert est.per_task_usd["extract"] == 0.0
    assert est.candidate_files == 0


def test_run_estimate_counts_the_planned_sections(tmp_path: Path) -> None:
    corpus = _book_corpus(tmp_path)
    config = _priced_config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 1_000
    n = len(qp.plan_sections(BOOK, FakeClient({}), config))  # type: ignore[arg-type]
    decision = {"file_hash": "a" * 64, "include": True, "reason": "r"}
    fake = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: ["{}"],
            TaskName.QUERY_ROUTE: [json.dumps({"decisions": [decision]})],
        }
    )
    _, est, _ = asyncio.run(
        qp.run_estimate(corpus, "q", config, client=fake, confirm=lambda _e: True)  # type: ignore[arg-type]
    )
    max_tokens = config.tasks[TaskName.EXTRACT].max_tokens
    assert est.per_task_usd["extract"] >= (
        n * (EXTRACT_PROMPT_OVERHEAD_TOKENS + max_tokens) / 1_000_000
    )
