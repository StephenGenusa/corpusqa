"""Regression tests for the citation, cost, cache, and migration fixes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.errors import BudgetExceededError
from corpusqa.query import pipeline as qp
from corpusqa.query.budget import CostEstimate
from corpusqa.query.extractor import Finding, _quote_in_source, extract_file
from corpusqa.query.synthesizer import Evidence, render_cite, resolve_citations
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


# --- Fix 1a: citation coverage -------------------------------------------


def _ev() -> list[Evidence]:
    return [
        Evidence(1, "a.md", Finding(claim="c", quote="q", heading_path=["H"]))
    ]


def test_uncited_claim_sentence_is_flagged() -> None:
    text, bad, used, ok_n, bad_n = resolve_citations(
        "The treaty was signed in 1923 and dissolved the union.", _ev()
    )
    assert "[uncited]" in text
    assert bad and bad_n == 1 and used == set()


def test_mixed_cited_and_uncited() -> None:
    text, bad, _, ok_n, bad_n = resolve_citations(
        "A supported finding [1]. An unsupported assertion with several words.",
        _ev(),
    )
    assert "[a.md § H]" in text and "[uncited]" in text
    assert ok_n == 1 and bad_n == 1 and len(bad) == 1


def test_short_fragments_not_flagged() -> None:
    _text, bad, _used, _ok, bad_n = resolve_citations("Yes.", _ev())
    assert not bad and bad_n == 0


# --- Fix 1b: quote verification ------------------------------------------


def test_quote_in_source_normalizes_ws_and_case() -> None:
    assert _quote_in_source("Community  GARDENS", "... community gardens ...")
    assert not _quote_in_source("   ", "anything")
    assert not _quote_in_source("absent phrase", "present text only")


def test_render_cite_marks_unverified_source() -> None:
    item = Evidence(1, "a.md", Finding(claim="c", quote="q", quote_verified=False))
    assert "(unverified)" in render_cite(item)


def test_extract_file_sets_quote_verified() -> None:
    md = "# Title\n\nThe quick brown fox jumps.\n"
    fake = FakeClient(
        {
            TaskName.EXTRACT: [
                json.dumps(
                    {
                        "file_hash": "h",
                        "relevant": True,
                        "confidence": 0.9,
                        "findings": [
                            {"claim": "real", "quote": "quick brown fox",
                             "heading_path": ["Title"], "page_no": None},
                            {"claim": "fake", "quote": "not in the document",
                             "heading_path": ["Title"], "page_no": None},
                        ],
                    }
                )
            ]
        }
    )
    config = load_config(EXAMPLE)
    result = asyncio.run(
        extract_file("q", None, "a.md", "h", md, fake, config)  # type: ignore[arg-type]
    )
    verified = {f.claim: f.quote_verified for f in result.findings}
    assert verified == {"real": True, "fake": False}


# --- Fix 4: eval cache keyed on model ------------------------------------


def test_evaluation_cache_keeps_both_models() -> None:
    d = Path(tempfile.mkdtemp())
    with CatalogStore(d / "catalog.db") as s:
        s.upsert_file(FileRow("h", "a.md", 1, "md", None, "ok", None, "a.md", None))
        s.put_evaluation("r", "qh", "h", "modelA", '{"v":"A"}', question="q")
        s.put_evaluation("r", "qh", "h", "modelB", '{"v":"B"}', question="q")
        assert s.get_evaluation("qh", "h", "modelA") == '{"v":"A"}'
        assert s.get_evaluation("qh", "h", "modelB") == '{"v":"B"}'


# --- Fix 5: migration adds question column without catalog_cards ----------


def test_migration_adds_question_without_catalog_cards() -> None:
    d = Path(tempfile.mkdtemp())
    db = d / "catalog.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE evaluations (eval_id TEXT PRIMARY KEY, run_id TEXT, "
        "file_hash TEXT, question_hash TEXT, model_used TEXT, "
        "result_json TEXT, evaluated_at TEXT)"
    )
    conn.commit()
    conn.close()
    with CatalogStore(db) as s:  # opening runs the migration
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(evaluations)")}
        assert "question" in cols


# --- Fix 2b: cumulative budget gate --------------------------------------


def test_budget_gate_is_cumulative() -> None:
    config = load_config(EXAMPLE)
    config.budget.confirm_above_usd = 1.0
    seen: list[float] = []

    def confirm(est: CostEstimate) -> bool:
        seen.append(est.total_usd)
        return False

    # Two stages each $0.60: individually under $1, together over it.
    with pytest.raises(BudgetExceededError):
        qp._check_budget_gate(
            CostEstimate(total_usd=0.60), config, confirm, "extract", prior_usd=0.60
        )
    assert seen  # confirm was consulted because cumulative exceeded threshold
