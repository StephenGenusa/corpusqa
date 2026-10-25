"""A file with a failed section is a partial failure, never a clean verdict.

When one section of a multi-section file fails structured output, the
passage that mattered may be in it. The file must be reported as failed
(exit 2), its surviving findings still shown, and the incomplete verdict
never cached -- otherwise every rerun serves "not relevant" from cache.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.query import pipeline as qp
from corpusqa.query.extractor import (
    ExtractionResult,
    Finding,
    PartialExtractionError,
    extract_file,
)
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
SECTION_1 = "# One\nThe quick brown fox jumps.\n"
SECTION_2 = "# Two\nThe lazy dog sleeps.\n"


def _reply(quote: str) -> str:
    return json.dumps(
        {
            "file_hash": "h",
            "relevant": True,
            "confidence": 0.8,
            "reasoning": "r",
            "findings": [{"claim": "c", "quote": quote}],
        }
    )


def test_failed_section_raises_partial_with_surviving_findings() -> None:
    fake = FakeClient(
        {
            TaskName.EXTRACT: [
                _reply("quick brown fox"),  # section 1 ok
                "not json",  # section 2 fails...
                "still not json",  # ...and so does its repair
            ]
        }
    )
    with pytest.raises(PartialExtractionError, match="1 of 2 sections") as info:
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                SECTION_1 + SECTION_2,
                fake,  # type: ignore[arg-type]
                load_config(EXAMPLE),
                sections=[SECTION_1, SECTION_2],
            )
        )
    partial = info.value.result
    assert partial.relevant
    assert [f.quote for f in partial.findings] == ["quick brown fox"]


def _partial_extract(*_: Any, **__: Any) -> Any:  # noqa: ANN401 -- extract_file stand-in
    async def run() -> ExtractionResult:
        result = ExtractionResult(
            file_hash="a" * 64,
            relevant=True,
            confidence=0.8,
            findings=[Finding(claim="c", quote="alpha", quote_verified=True)],
        )
        raise PartialExtractionError(result, skipped=1, total=2, last_error="bad")

    return run()


def test_pipeline_reports_partial_file_shows_findings_and_skips_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    monkeypatch.setattr(qp, "extract_file", _partial_extract)

    def run() -> qp.QueryReport:
        return asyncio.run(
            qp.run_query(
                corpus,
                "q",
                load_config(EXAMPLE),
                mode="sweep",
                client=FakeClient({}),  # type: ignore[arg-type]
                confirm=lambda _e: True,
            )
        )

    report = run()
    assert [path for path, _ in report.extraction_failures] == ["a.md"]
    assert "1 of 2 sections" in report.extraction_failures[0][1]
    assert report.relevant_files == ["a.md"]  # surviving findings still shown
    assert report.findings[0][1].findings[0].quote == "alpha"

    again = run()  # not served from cache: the partial verdict was never stored
    assert again.cached_evaluations == 0
    assert [path for path, _ in again.extraction_failures] == ["a.md"]


def test_truncated_section_output_makes_the_file_partial() -> None:
    truncated = _reply("quick brown fox")[:-2] + ', {"claim": "d", "quo'
    fake = FakeClient({TaskName.EXTRACT: [(truncated, "length")]})
    with pytest.raises(PartialExtractionError, match="truncated") as info:
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                SECTION_1,
                fake,  # type: ignore[arg-type]
                load_config(EXAMPLE),
                sections=[SECTION_1],
            )
        )
    assert [f.quote for f in info.value.result.findings] == ["quick brown fox"]


def test_malformed_section_output_is_partial_and_not_blamed_on_max_tokens() -> None:
    # Stopped normally but invalid twice (reply and repair): the complete
    # findings are kept, the file is partial, and the message names the
    # defect rather than sending the user to raise max_tokens.
    malformed = _reply("quick brown fox")[:-2] + ', {"claim": "d", "page_no_null"]}'
    fake = FakeClient({TaskName.EXTRACT: [malformed, malformed]})
    with pytest.raises(PartialExtractionError, match="malformed") as info:
        asyncio.run(
            extract_file(
                "q",
                None,
                "a.md",
                "h",
                SECTION_1,
                fake,  # type: ignore[arg-type]
                load_config(EXAMPLE),
                sections=[SECTION_1],
            )
        )
    assert "max_tokens" not in str(info.value)
    assert info.value.malformed == 1
    assert [f.quote for f in info.value.result.findings] == ["quick brown fox"]
