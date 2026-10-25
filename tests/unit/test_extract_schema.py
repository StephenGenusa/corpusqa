"""The extract model is shown only the fields it writes.

Heading path, page number and quote verification are system-derived, and
the file hash is known to the system. Listing them in the schema invited the
model to write them anyway: wasted output, and on gemma4:e4b a finding
mangled into ``"page_no_null", "quote_verified_true"``. Developer docstrings
in the schema cost ~780 input tokens per call and are not instructions.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.query.extractor import extract_file
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
SECTION = "# One\nThe quick brown fox jumps.\n"
SYSTEM_ONLY = ("heading_path", "page_no", "quote_verified", "file_hash")


def _extract(reply: str) -> tuple[str, object]:
    fake = FakeClient({TaskName.EXTRACT: [reply]})
    result = asyncio.run(
        extract_file(
            "q",
            None,
            "a.md",
            "h" * 64,
            SECTION,
            fake,  # type: ignore[arg-type]
            load_config(EXAMPLE),
            sections=[SECTION],
        )
    )
    return fake.calls[TaskName.EXTRACT][0][0]["content"], result


def _reply(**finding: object) -> str:
    return json.dumps(
        {
            "relevant": True,
            "confidence": 0.8,
            "reasoning": "r",
            "findings": [{"claim": "c", "quote": "quick brown fox", **finding}],
        }
    )


def test_schema_shows_only_model_written_fields() -> None:
    system, _ = _extract(_reply())
    for field in SYSTEM_ONLY:
        assert field not in system
    for field in ("claim", "quote", "relevant", "confidence", "reasoning"):
        assert f'"{field}"' in system


def test_schema_carries_no_developer_docstrings() -> None:
    system, _ = _extract(_reply())
    assert "Attributes:" not in system
    assert "extract_file" not in system


def test_system_fields_are_filled_by_the_system() -> None:
    _, result = _extract(_reply())
    assert result.file_hash == "h" * 64  # type: ignore[attr-defined]
    finding = result.findings[0]  # type: ignore[attr-defined]
    assert finding.quote_verified and finding.heading_path == ["One"]


def test_model_output_with_system_fields_still_parses_and_is_overridden() -> None:
    _, result = _extract(
        _reply(heading_path=["Invented"], page_no=99, quote_verified=False)
    )
    finding = result.findings[0]  # type: ignore[attr-defined]
    assert finding.heading_path == ["One"] and finding.page_no is None
    assert finding.quote_verified
