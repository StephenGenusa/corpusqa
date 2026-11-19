"""Citation numbering, resolution, repair, and sources block (M3)."""

from __future__ import annotations

import asyncio
import types

from corpusqa.config.schema import TaskName
from corpusqa.query.extractor import ExtractionResult, Finding
from corpusqa.query.synthesizer import (
    CiteRef,
    number_evidence,
    render_cite,
    resolve_citations,
    sources_block,
    synthesize,
)
from tests.unit.fakes import FakeClient


def _results() -> list[tuple[str, ExtractionResult]]:
    f1 = Finding(
        claim="gardens help", quote="q1", heading_path=["Ideas"], page_label=None
    )
    f2 = Finding(
        claim="rivers flow", quote="q2", heading_path=["Hydro", "Flow"], page_label="3"
    )
    return [
        (
            "skills.md",
            ExtractionResult(
                file_hash="h1", relevant=True, confidence=0.9, findings=[f1]
            ),
        ),
        (
            "rivers.pdf",
            ExtractionResult(
                file_hash="h2", relevant=True, confidence=0.8, findings=[f2]
            ),
        ),
        ("noise.md", ExtractionResult(file_hash="h3", relevant=False, confidence=0.9)),
    ]


def test_numbering_skips_irrelevant_and_is_global() -> None:
    evidence = number_evidence(_results())
    assert [e.number for e in evidence] == [1, 2]
    assert evidence[1].rel_path == "rivers.pdf"


def test_render_cite_contract_form() -> None:
    evidence = number_evidence(_results())
    assert render_cite(evidence[0]) == "[skills.md § Ideas]"
    assert render_cite(evidence[1]) == "[rivers.pdf § Flow, p.3]"


def test_resolution_and_sources() -> None:
    evidence = number_evidence(_results())
    text, bad, used, ok_n, bad_n = resolve_citations(
        "Gardens are good [1]. Rivers move [2]. Both matter [1, 2].", evidence
    )
    assert "[skills.md § Ideas]" in text and "[rivers.pdf § Flow, p.3]" in text
    assert not bad and used == {1, 2}
    block = sources_block(evidence, used)
    assert "skills.md" in block and "rivers.pdf" in block


def test_unresolvable_number_marks_uncited() -> None:
    evidence = number_evidence(_results())
    text, bad, used, ok_n, bad_n = resolve_citations(
        "Claim with ghost cite [7].", evidence
    )
    assert (ok_n, bad_n) == (0, 1)
    assert "[uncited]" in text
    assert bad and "ghost" in bad[0]
    assert used == set()


# --- the finding behind each citation ---------------------------------------


def _same_place() -> list[tuple[str, ExtractionResult]]:
    """Three findings in one file and section, the middle a repeat of the first."""

    def finding(claim: str, quote: str) -> Finding:
        return Finding(
            claim=claim, quote=quote, heading_path=["Sermon"], page_label="250"
        )

    return [
        (
            "vol.docx",
            ExtractionResult(
                file_hash="h",
                relevant=True,
                confidence=0.9,
                findings=[
                    finding("dominion", "all peoples"),
                    finding("dominion again", "all  peoples"),
                    finding("inheritance", "all things"),
                ],
            ),
        )
    ]


def test_evidence_records_the_findings_place_in_its_file() -> None:
    evidence = number_evidence(_same_place())
    # The repeat is collapsed; the third finding keeps its own position.
    assert [(e.number, e.index) for e in evidence] == [(1, 0), (2, 2)]


def test_findings_that_render_alike_keep_a_citation_each() -> None:
    evidence = number_evidence(_same_place())
    assert render_cite(evidence[0]) == render_cite(evidence[1])
    cited: list = []
    text, *_ = resolve_citations(
        "He inherits [2]. All of it [1, 2, 1].", evidence, cited
    )
    cite = "[vol.docx § Sermon, p.250]"
    assert text == f"He inherits {cite}. All of it {cite} {cite}."
    assert [e.index for e in cited] == [2, 0, 2]


def test_synthesize_returns_the_cited_findings_in_order() -> None:
    fake = FakeClient({TaskName.SYNTHESIZE: ["Things [2] and peoples [1]."]})
    config = types.SimpleNamespace(
        tasks={
            TaskName.SYNTHESIZE: types.SimpleNamespace(
                context_window=8000, max_tokens=512
            )
        }
    )
    answer = asyncio.run(synthesize("q", _same_place(), fake, config))  # type: ignore[arg-type]
    assert answer.citations == [CiteRef("vol.docx", 2), CiteRef("vol.docx", 0)]


def test_fallback_digest_records_its_citations() -> None:
    fake = FakeClient({TaskName.SYNTHESIZE: [""]})
    config = types.SimpleNamespace(
        tasks={
            TaskName.SYNTHESIZE: types.SimpleNamespace(
                context_window=8000, max_tokens=512
            )
        }
    )
    answer = asyncio.run(synthesize("q", _same_place(), fake, config))  # type: ignore[arg-type]
    assert answer.citations == [CiteRef("vol.docx", 0), CiteRef("vol.docx", 2)]
