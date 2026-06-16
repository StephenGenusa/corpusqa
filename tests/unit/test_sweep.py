"""Sweep mode, evaluation cache, relevance modes, migration (M5)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from corpusqa.catalog.store import CatalogStore
from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.ingest.pipeline import run_index
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("sweepcorpus")
    (root / "a.md").write_text("# A\n\n## Ideas\nGardens grow.", encoding="utf-8")
    (root / "b.md").write_text("# B\n\n## Flow\nRivers run.", encoding="utf-8")
    run_index(root, load_config(EXAMPLE))
    return root


def _extraction(file_hash: str, relevant: bool) -> str:
    return json.dumps(
        {
            "file_hash": file_hash,
            "relevant": relevant,
            "confidence": 0.8,
            "findings": (
                [
                    {
                        "claim": "c",
                        "quote": "q",
                        "heading_path": ["Ideas"],
                        "page_no": None,
                    }
                ]
                if relevant
                else []
            ),
        }
    )


@pytest.mark.slow
def test_sweep_skips_routing_and_caches(corpus: Path) -> None:
    config = load_config(EXAMPLE)
    hashes = list(
        CatalogStore(corpus / ".corpusqa" / "catalog.db").known_files().values()
    )
    fake = FakeClient(
        {
            TaskName.EXTRACT: [_extraction(h, True) for h in hashes],
            TaskName.SYNTHESIZE: ["Both matter [1] [2]."],
        }
    )
    progress: list[tuple[int, int]] = []
    report = asyncio.run(
        qp.run_query(
            corpus,
            "q",
            config,
            mode="sweep",
            client=fake,
            synthesize_answer=True,
            on_progress=lambda d, t: progress.append((d, t)),
        )
    )
    assert report.mode == "sweep"
    assert TaskName.QUERY_ROUTE not in fake.calls  # no routing
    assert TaskName.CATALOG_SUMMARIZE not in fake.calls  # no cards
    assert len(report.candidates) == 2
    assert progress[-1] == (2, 2)
    assert report.answer.resolved_cites == 2

    # second identical run: everything from cache, zero extract calls
    fake2 = FakeClient({TaskName.SYNTHESIZE: ["Still both [1] [2]."]})
    report2 = asyncio.run(qp.run_query(corpus, "q", config, mode="sweep", client=fake2))
    assert report2.cached_evaluations == 2
    assert TaskName.EXTRACT not in fake2.calls

    # different relevance mode = different cache key = re-extract
    fake3 = FakeClient(
        {
            TaskName.EXTRACT: [_extraction(h, False) for h in hashes],
            TaskName.SYNTHESIZE: [],
        }
    )
    report3 = asyncio.run(
        qp.run_query(
            corpus,
            "q",
            config,
            mode="sweep",
            relevance="strict",
            client=fake3,
        )
    )
    assert report3.cached_evaluations == 0
    assert len(fake3.calls[TaskName.EXTRACT]) == 2


@pytest.mark.slow
def test_auto_mode_sweeps_small_corpus(corpus: Path) -> None:
    config = load_config(EXAMPLE)
    hashes = list(
        CatalogStore(corpus / ".corpusqa" / "catalog.db").known_files().values()
    )
    fake = FakeClient(
        {
            TaskName.EXTRACT: [_extraction(h, False) for h in hashes],
            TaskName.SYNTHESIZE: [],
        }
    )
    report = asyncio.run(
        qp.run_query(corpus, "q2", config, client=fake)  # mode defaults auto
    )
    assert report.mode == "sweep"  # 2 files <= threshold 150

    config.query.sweep_threshold = 1
    fake2 = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: [
                json.dumps({"claims": ["x"], "doc_type": "doc"})
            ]
            * 2,
            TaskName.QUERY_ROUTE: [
                json.dumps(
                    {
                        "decisions": [
                            {"file_hash": h, "include": False, "reason": "n"}
                            for h in hashes
                        ]
                    }
                )
            ],
            TaskName.SYNTHESIZE: [],
        }
    )
    report2 = asyncio.run(qp.run_query(corpus, "q3", config, client=fake2))
    assert report2.mode == "route"


def test_relevance_instruction_selected() -> None:
    from corpusqa.prompts.extract import RELEVANCE_INSTRUCTIONS

    assert set(RELEVANCE_INSTRUCTIONS) == {"recall", "balanced", "strict"}


def test_v1_card_table_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE catalog_cards (
             file_hash TEXT PRIMARY KEY, summary TEXT NOT NULL,
             topics TEXT NOT NULL, entities TEXT NOT NULL,
             doc_type TEXT, model_used TEXT NOT NULL, created_at TEXT NOT NULL
           )"""
    )
    conn.commit()
    conn.close()
    with CatalogStore(db) as store:  # opens, migrates, recreates v2 shape
        assert store.all_cards() == []


def test_eval_retention_prunes_old_runs(tmp_path: Path) -> None:
    from corpusqa.catalog.store import FileRow

    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(
            FileRow("h1", "a.md", 1, "md", None, "ok", None, "h1.md", None)
        )
        for i in range(5):
            store.put_evaluation(f"run{i}", f"q{i}", "h1", "m", "{}")
        removed = store.prune_evaluations(keep_runs=2)
        assert removed == 3


def test_converter_check_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    from corpusqa.errors import IngestError
    from corpusqa.ingest.converter import check_converter_available

    real_import = builtins.__import__

    def broken(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("docling"):
            raise ImportError("tokenizers version conflict")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    with pytest.raises(IngestError, match="tokenizers"):
        check_converter_available()



def test_explain_lists_persisted_verdicts(
    corpus: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from corpusqa.cli.main import main

    db = corpus / ".corpusqa" / "catalog.db"
    with CatalogStore(db) as store:
        a_hash = store.known_files()["a.md"]
        store.put_evaluation(
            "runX",
            "qhash",
            a_hash,
            "m",
            _extraction(a_hash, True),
            question="test question",
        )
    monkeypatch.chdir(corpus)
    code = main(["explain", "a.md", str(corpus), "--config", str(EXAMPLE)])
    captured = capsys.readouterr()
    assert code == 0
    assert "RELEVANT" in captured.out
    assert "test question" in captured.out
