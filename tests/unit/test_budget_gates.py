"""Budget gates cover all four spending stages (M7).

The pass-1 gate (cataloging + routing) must fire BEFORE any LLM call --
historically card backfill was the largest unguarded spend. These tests
build the index store directly (no Docling) and use FakeClient with no
scripted catalog replies: if the gate leaks, the test fails loudly with
"no scripted reply left" instead of silently spending.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import BudgetExceededError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.query import pipeline as qp
from corpusqa.query.budget import CostEstimate
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _indexed_corpus(tmp_path: Path) -> Path:
    """Builds a one-file index by hand: store row + cached markdown."""
    root = tmp_path / "corpus"
    root.mkdir()
    _, db_path, cache_dir = index_paths(root)
    cache_dir.mkdir(parents=True)
    file_hash = "a" * 64
    (cache_dir / f"{file_hash}.md").write_text("# A\nalpha beta", encoding="utf-8")
    with CatalogStore(db_path) as store:
        store.upsert_file(
            FileRow(
                file_hash=file_hash,
                rel_path="a.md",
                size_bytes=10,
                format="md",
                parsed_at=None,
                parse_status="ok",
                parse_error=None,
                md_cache_path=f"{file_hash}.md",
                page_count=None,
            )
        )
    return root


def _gated_config() -> AppConfig:
    config = load_config(EXAMPLE)
    config.budget.confirm_above_usd = 0.0
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_in = 3.0
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_out = 15.0
    return config


def test_pass1_gate_blocks_card_generation_in_run_query(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    fake = FakeClient({})  # any LLM call would raise "no scripted reply"
    with pytest.raises(BudgetExceededError, match="cataloging"):
        asyncio.run(
            qp.run_query(
                corpus, "q", _gated_config(), mode="route", client=fake, confirm=None
            )
        )
    assert fake.calls == {}  # gate fired before the first call


def test_pass1_gate_blocks_run_estimate(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    fake = FakeClient({})
    with pytest.raises(BudgetExceededError, match="cataloging"):
        asyncio.run(qp.run_estimate(corpus, "q", _gated_config(), client=fake))
    assert fake.calls == {}


def test_pass1_gate_confirm_proceeds(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    seen: list[CostEstimate] = []

    def confirm(estimate: CostEstimate) -> bool:
        seen.append(estimate)
        return True

    card = json.dumps(
        {
            "claims": ["alpha beta"],
            "terms": [],
            "implicit_topics": [],
            "answerable": [],
            "absences": [],
            "entities": [],
            "doc_type": "notes",
        }
    )
    fake = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: [card],
            TaskName.QUERY_ROUTE: [
                json.dumps(
                    {
                        "decisions": [
                            {"file_hash": "a" * 64, "include": False, "reason": "no"}
                        ]
                    }
                )
            ],
        }
    )
    candidates, _ = asyncio.run(
        qp.run_estimate(corpus, "q", _gated_config(), client=fake, confirm=confirm)
    )
    assert candidates == []
    assert seen and seen[0].total_usd > 0  # the gate showed a real projection
    assert len(fake.calls[TaskName.CATALOG_SUMMARIZE]) == 1


def test_sweep_mode_has_no_pass1_gate(tmp_path: Path) -> None:
    """Sweep spends nothing on cards/routing; only the pass-2 gate applies."""
    corpus = _indexed_corpus(tmp_path)
    config = _gated_config()
    config.tasks[TaskName.EXTRACT].cost_per_mtok_in = 1.0  # make pass 2 gated
    fake = FakeClient({})
    with pytest.raises(BudgetExceededError, match="extraction"):
        asyncio.run(
            qp.run_query(corpus, "q", config, mode="sweep", client=fake, confirm=None)
        )
    assert fake.calls == {}
