"""Citation numbering, resolution, repair, and sources block (M3)."""

from __future__ import annotations

from corpusqa.query.extractor import ExtractionResult, Finding
from corpusqa.query.synthesizer import (
    number_evidence,
    render_cite,
    resolve_citations,
    sources_block,
)


def _results() -> list[tuple[str, ExtractionResult]]:
    f1 = Finding(claim="gardens help", quote="q1", heading_path=["Ideas"], page_no=None)
    f2 = Finding(
        claim="rivers flow", quote="q2", heading_path=["Hydro", "Flow"], page_no=3
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
