"""Eval harness mechanics: scoring functions and mock-mode end-to-end."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from corpusqa.catalog.store import FileRow
from corpusqa.catalog.summarizer import CatalogCard
from corpusqa.config.schema import TaskName
from corpusqa.evalkit import (
    MockTaskClient,
    QAPair,
    load_pairs,
    render_table,
    score_citations,
    score_routing,
)
from corpusqa.prompts import route as route_prompt
from corpusqa.query.extractor import ExtractionResult
from corpusqa.query.router import RouteResponse, _render_card

PAIRS = Path(__file__).resolve().parents[2] / "tests/eval/qa_pairs.yaml"


def test_pairs_file_is_valid() -> None:
    pairs = load_pairs(PAIRS)
    assert len(pairs) == 16
    assert any(p.definition for p in pairs)  # custom-definition archetype


def test_routing_scores() -> None:
    pair = QAPair(
        id="x",
        question="q",
        expected_files=["a.md", "b.md"],
        forbidden_files=["z.md"],
    )
    r = score_routing(pair, {"a.md", "z.md", "noise.md"})
    assert r.recall == 0.5
    assert r.precision == pytest.approx(1 / 3)
    assert r.forbidden_hits == ["z.md"]


def test_negative_control_scores_perfect_on_empty() -> None:
    pair = QAPair(id="neg", question="q")
    r = score_routing(pair, set())
    assert r.recall == 1.0 and r.precision == 1.0


def test_citation_scoring() -> None:
    pair = QAPair(id="c", question="q", must_cite=["a.md"])
    r = score_routing(pair, set())
    r = score_citations(
        r,
        pair,
        answer_text="Claim [a.md § Ideas]. Bad [uncited].",
        sources="Sources:\n  a.md — Ideas",
    )
    assert r.cite_validity == 0.5
    assert r.must_cite_ok is True


def test_table_renders_one_screen() -> None:
    pair = QAPair(id="t", question="q", expected_files=["a.md"])
    table = render_table([score_routing(pair, {"a.md"})])
    assert "MEAN" in table and "t" in table


# -- mock-mode regression: the mock must track the REAL prompt surfaces ----
# The M5 card-shape change silently broke MockTaskClient (it parsed a
# "summary:" line the router no longer rendered, and emitted old-shape card
# JSON). These tests pin the mock to the real renderer and real schemas.


def _mock() -> MockTaskClient:
    return MockTaskClient(None)  # type: ignore[arg-type]


def test_mock_catalog_output_is_a_valid_current_card() -> None:
    completion = asyncio.run(
        _mock().complete(
            TaskName.CATALOG_SUMMARIZE,
            [{"role": "user", "content": "<document>\ngardens and tools\n"}],
        )
    )
    card = CatalogCard.model_validate_json(completion.text)  # extra=forbid
    assert card.claims and card.implicit_topics


def test_mock_routing_parses_the_real_card_render() -> None:
    """Route decisions must come from cards rendered by router._render_card."""
    row = FileRow(
        file_hash="f" * 64,
        rel_path="skills.md",
        size_bytes=1,
        format="md",
        parsed_at=None,
        parse_status="ok",
        parse_error=None,
        md_cache_path=None,
        page_count=None,
    )
    card = CatalogCard(
        claims=["community gardens and tool libraries help neighborhoods"],
        implicit_topics=["brainstorming"],
        doc_type="notes",
    )
    prompt = route_prompt.TEMPLATE.format(
        question="what ideas exist about community gardens projects?",
        definition_block="",
        cards_block=_render_card(row, card),
    )
    completion = asyncio.run(
        _mock().complete(TaskName.QUERY_ROUTE, [{"role": "user", "content": prompt}])
    )
    response = RouteResponse.model_validate_json(completion.text)
    assert len(response.decisions) == 1
    decision = response.decisions[0]
    assert decision.file_hash == "f" * 64
    assert decision.include  # keyword overlap with the rendered claims


def test_mock_extract_output_validates_with_reasoning() -> None:
    completion = asyncio.run(
        _mock().complete(
            TaskName.EXTRACT,
            [
                {
                    "role": "user",
                    "content": (
                        "File: a.md (hash "
                        + "a" * 64
                        + ")\n\n<document>\n## Ideas\ntext\n</document>"
                    ),
                }
            ],
        )
    )
    result = ExtractionResult.model_validate_json(completion.text)
    assert result.relevant and result.reasoning
