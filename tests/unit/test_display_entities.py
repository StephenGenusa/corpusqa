"""Terminal output shows the converter's "&amp;" escapes as the printed text."""

from __future__ import annotations

from corpusqa.query.extractor import ExtractionResult, Finding
from corpusqa.query.grouping import render_groups
from corpusqa.query.hits import render_hits
from corpusqa.query.results_file import (
    SavedFile,
    SavedFinding,
    SavedGroup,
    SavedGrouping,
    SavedHitRef,
    SavedRun,
)

QUOTE = "exalted him,’ &amp;c. (Phil. 2:8, 9) &lt;sic&gt;"
SHOWN = "exalted him,’ &c. (Phil. 2:8, 9) <sic>"


def test_render_hits_unescapes_quotes() -> None:
    result = ExtractionResult(
        file_hash="h",
        relevant=True,
        confidence=1.0,
        findings=[Finding(claim="c", quote=QUOTE, quote_verified=True)],
    )
    out = render_hits("q", [("v10.docx", result)])
    assert SHOWN in out and "&amp;" not in out


def test_render_groups_unescapes_quotes_and_answers() -> None:
    run = SavedRun(
        format_version=2,
        question="q",
        relevance="recall",
        mode_requested="sweep",
        corpus="/c",
        started_at="2026-11-17T00:00:00+00:00",
        tasks={},
        files=[
            SavedFile(
                rel_path="v10.docx",
                file_hash="h",
                status="relevant",
                findings=[SavedFinding(claim="c", quote=QUOTE)],
            )
        ],
        grouping=SavedGrouping(
            model="m",
            created_at="2026-11-17T00:00:00+00:00",
            groups=[
                SavedGroup(
                    answer="Exaltation &amp;c.",
                    hits=[SavedHitRef(file="v10.docx", index=0)],
                    volumes=1,
                )
            ],
        ),
    )
    out = render_groups(run)
    assert SHOWN in out and "Exaltation &c." in out and "&amp;" not in out
