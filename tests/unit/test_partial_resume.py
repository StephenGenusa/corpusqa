"""A partial volume keeps its finished sections; a rerun reads only the rest.

A volume with a failed section is still never served as a complete verdict.
Its finished sections are stored separately, and the next run of the same
question sends only the missing sections to the model. The volume becomes a
cached verdict only once every section is done.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from corpusqa.catalog.store import CatalogStore
from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.ingest.pipeline import index_paths
from corpusqa.query import pipeline as qp
from corpusqa.query.extractor import PartialExtractionError, extract_file
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
S1 = "# A\nalpha beta"  # the indexed file's text, split in two below
S2 = "gamma delta"


def _reply(quote: str, relevant: bool = True) -> str:
    return json.dumps(
        {
            "file_hash": "h",
            "relevant": relevant,
            "confidence": 0.8,
            "reasoning": "r",
            "findings": [{"claim": "c", "quote": quote}] if relevant else [],
        }
    )


BAD = "not json"  # a section reply that fails, with its repair turn


def test_partial_error_says_which_sections_finished() -> None:
    fake = FakeClient({TaskName.EXTRACT: [_reply("alpha"), BAD, BAD]})
    with pytest.raises(PartialExtractionError) as info:
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                S1 + "\n\n" + S2,
                fake,  # type: ignore[arg-type]
                load_config(EXAMPLE),
                sections=[S1, S2],
            )
        )
    done = info.value.sections
    assert done is not None and done[1] is None
    assert done[0] is not None and [f.quote for f in done[0].findings] == ["alpha"]


def test_prior_sections_are_reused_and_only_the_rest_is_read() -> None:
    first = FakeClient({TaskName.EXTRACT: [_reply("alpha"), BAD, BAD]})
    md = S1 + "\n\n" + S2
    config = load_config(EXAMPLE)
    with pytest.raises(PartialExtractionError) as info:
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                md,
                first,
                config,  # type: ignore[arg-type]
                sections=[S1, S2],
            )
        )
    second = FakeClient({TaskName.EXTRACT: [_reply("gamma")]})
    ticks: list[None] = []
    result = asyncio.run(
        extract_file(
            "q",
            None,
            "a.md",
            "h",
            md,
            second,
            config,  # type: ignore[arg-type]
            sections=[S1, S2],
            prior=info.value.sections,
            on_section=lambda: ticks.append(None),
        )
    )
    assert len(second.calls[TaskName.EXTRACT]) == 1
    assert len(ticks) == 1  # progress counts model calls only
    assert [f.quote for f in result.findings] == ["alpha", "gamma"]
    assert all(f.quote_verified for f in result.findings)


def _two_sections(monkeypatch: pytest.MonkeyPatch, sections: list[str]) -> None:
    monkeypatch.setattr(qp, "plan_sections", lambda *_a, **_k: list(sections))


def _run(corpus: Path, client: FakeClient, **kw: Any) -> qp.QueryReport:
    progress: list[tuple[int, int]] = []
    report = asyncio.run(
        qp.run_query(
            corpus,
            "q",
            load_config(EXAMPLE),
            mode="sweep",
            client=client,  # type: ignore[arg-type]
            confirm=lambda _e: True,
            on_progress=lambda d, t: progress.append((d, t)),
            **kw,
        )
    )
    report.progress = progress  # type: ignore[attr-defined]
    return report


def test_rerun_reads_only_the_failed_section_then_caches_the_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _two_sections(monkeypatch, [S1, S2])

    first = _run(corpus, FakeClient({TaskName.EXTRACT: [_reply("alpha"), BAD, BAD]}))
    assert [p for p, _ in first.extraction_failures] == ["a.md"]

    second_client = FakeClient({TaskName.EXTRACT: [_reply("beta")]})
    second = _run(corpus, second_client)
    assert len(second_client.calls[TaskName.EXTRACT]) == 1  # only the failed one
    assert second.progress[-1] == (1, 1)  # type: ignore[attr-defined]
    assert second.extraction_failures == []
    assert [f.quote for f in second.findings[0][1].findings] == ["alpha", "beta"]

    third = _run(corpus, FakeClient({}))  # now a complete, cached verdict
    assert third.cached_evaluations == 1
    _, db_path, _ = index_paths(corpus)
    with CatalogStore(db_path) as store:
        assert store.partial_evaluation_count() == 0  # cleared once complete


def test_a_second_failure_keeps_what_both_runs_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _two_sections(monkeypatch, [S1, S2])
    _run(corpus, FakeClient({TaskName.EXTRACT: [BAD, BAD, _reply("beta")]}))
    _run(corpus, FakeClient({TaskName.EXTRACT: [BAD, BAD]}))  # fails again
    third_client = FakeClient({TaskName.EXTRACT: [_reply("alpha")]})
    third = _run(corpus, third_client)
    assert len(third_client.calls[TaskName.EXTRACT]) == 1
    assert [f.quote for f in third.findings[0][1].findings] == ["alpha", "beta"]


def test_stored_sections_are_ignored_when_the_split_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _two_sections(monkeypatch, [S1, S2])
    _run(corpus, FakeClient({TaskName.EXTRACT: [_reply("alpha"), BAD, BAD]}))
    _two_sections(monkeypatch, ["# A\nalpha", " beta"])  # e.g. a new section size
    client = FakeClient({TaskName.EXTRACT: [_reply("alpha"), _reply("beta")]})
    _run(corpus, client)
    assert len(client.calls[TaskName.EXTRACT]) == 2


def test_no_cache_ignores_stored_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _two_sections(monkeypatch, [S1, S2])
    _run(corpus, FakeClient({TaskName.EXTRACT: [_reply("alpha"), BAD, BAD]}))
    client = FakeClient({TaskName.EXTRACT: [_reply("alpha"), _reply("beta")]})
    _run(corpus, client, use_cache=False)
    assert len(client.calls[TaskName.EXTRACT]) == 2
