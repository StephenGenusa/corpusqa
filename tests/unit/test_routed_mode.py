"""Routed mode end-to-end (item 6): recall-bias inclusion and the
false-positive correction pass, with a scripted client over a real index.

Single-candidate per test so the FIFO FakeClient stays deterministic under the
extraction gather.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.ingest.pipeline import index_paths, run_index
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _corpus(tmp_path: Path) -> Path:
    (tmp_path / "planted.md").write_text(
        "# Planted\n\n## Topic\nThe quick brown fox jumps over the lazy dog here.\n",
        encoding="utf-8",
    )
    (tmp_path / "noise.txt").write_text(
        "Completely unrelated content about tax filing deadlines.",
        encoding="utf-8",
    )
    run_index(tmp_path, load_config(EXAMPLE))
    return tmp_path


def _hashes(root: Path) -> dict[str, str]:
    from corpusqa.catalog.store import CatalogStore

    with CatalogStore(index_paths(root)[1]) as store:
        return store.known_files()


def _card(claim: str) -> str:
    return json.dumps(
        {
            "claims": [claim],
            "terms": [],
            "implicit_topics": [],
            "answerable": [],
            "absences": [],
            "entities": [],
            "doc_type": "notes",
        }
    )


def _extraction(relevant: bool) -> str:
    return json.dumps(
        {
            "file_hash": "ignored",
            "relevant": relevant,
            "confidence": 0.8 if relevant else 0.2,
            "reasoning": "scripted",
            "findings": (
                [
                    {
                        "claim": "match",
                        "quote": "the quick brown fox jumps over the lazy dog",
                        "heading_path": [],
                        "page_no": None,
                        "quote_verified": True,
                    }
                ]
                if relevant
                else []
            ),
        }
    )


@pytest.mark.slow
def test_routed_recall_bias_includes_omitted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relevant file the router omits is still routed (recall bias),
    while a file it explicitly excludes is not."""
    corpus = _corpus(tmp_path)
    h = _hashes(corpus)
    planted, noise = h["planted.md"], h["noise.txt"]

    fake = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: [_card("planted"), _card("noise")],
            TaskName.QUERY_ROUTE: [
                # planted OMITTED (no decision) -> recall bias includes it;
                # noise EXPLICITLY excluded -> stays out.
                json.dumps(
                    {"decisions": [{"file_hash": noise, "include": False, "reason": "no"}]}
                )
            ],
            TaskName.EXTRACT: [_extraction(True)],  # single candidate: planted
        }
    )
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _c: fake)

    report = asyncio.run(
        qp.run_query(corpus, "where is the fox?", load_config(EXAMPLE), mode="route")
    )
    routed = {c.file.rel_path for c in report.candidates}
    assert routed == {"planted.md"}  # omitted relevant rescued; excluded stays out
    assert report.relevant_files == ["planted.md"]


@pytest.mark.slow
def test_routed_full_read_corrects_false_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file the router includes but that is not actually relevant is dropped
    by the full-read extraction pass."""
    corpus = _corpus(tmp_path)
    h = _hashes(corpus)
    planted, noise = h["planted.md"], h["noise.txt"]

    fake = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: [_card("planted"), _card("noise")],
            TaskName.QUERY_ROUTE: [
                # Router INCLUDES planted (a false positive for this question)
                # and excludes noise -> single candidate: planted.
                json.dumps(
                    {
                        "decisions": [
                            {"file_hash": planted, "include": True, "reason": "maybe"},
                            {"file_hash": noise, "include": False, "reason": "no"},
                        ]
                    }
                )
            ],
            TaskName.EXTRACT: [_extraction(False)],  # full read says: not relevant
        }
    )
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _c: fake)

    report = asyncio.run(
        qp.run_query(corpus, "unrelated question", load_config(EXAMPLE), mode="route")
    )
    routed = {c.file.rel_path for c in report.candidates}
    assert routed == {"planted.md"}  # router included it
    assert report.relevant_files == []  # but the full read corrected it out
