"""End-to-end query over an indexed fixture corpus with a scripted client.

Real Docling indexing; FakeClient at the llm seam (the M3 acceptance test:
end-to-end cited answers on the fixture corpus).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.errors import BudgetExceededError
from corpusqa.ingest.pipeline import run_index
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("qcorpus")
    (root / "skills.md").write_text(
        "# Skills\n\n## Ideas\nCommunity gardens and tool libraries.\n",
        encoding="utf-8",
    )
    (root / "rivers.txt").write_text(
        "Sabine river flow rates and seasonal data.", encoding="utf-8"
    )
    run_index(root, load_config(EXAMPLE))
    return root


def _hashes(root: Path) -> dict[str, str]:
    from corpusqa.catalog.store import CatalogStore
    from corpusqa.ingest.pipeline import index_paths

    with CatalogStore(index_paths(root)[1]) as store:
        return store.known_files()  # rel_path -> hash


def _card(claim: str) -> str:
    return json.dumps(
        {
            "claims": [claim],
            "terms": [],
            "implicit_topics": ["t1", "t2"],
            "answerable": [],
            "absences": [],
            "entities": [],
            "doc_type": "notes",
        }
    )


@pytest.mark.slow
def test_query_end_to_end_with_citations(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = _hashes(corpus)
    skills, rivers = hashes["skills.md"], hashes["rivers.txt"]

    fake = FakeClient(
        {
            TaskName.CATALOG_SUMMARIZE: [
                _card("brainstorm skills"),
                _card("river data"),
            ],
            TaskName.QUERY_ROUTE: [
                json.dumps(
                    {
                        "decisions": [
                            {
                                "file_hash": skills,
                                "include": True,
                                "reason": "brainstorming file",
                            },
                            {
                                "file_hash": rivers,
                                "include": False,
                                "reason": "hydrology only",
                            },
                        ]
                    }
                )
            ],
            TaskName.EXTRACT: [
                json.dumps(
                    {
                        "file_hash": skills,
                        "relevant": True,
                        "confidence": 0.9,
                        "findings": [
                            {
                                "claim": "Community gardens proposed",
                                "quote": "Community gardens",
                                "heading_path": ["Skills", "Ideas"],
                                "page_no": None,
                            }
                        ],
                    }
                )
            ],
            TaskName.SYNTHESIZE: ["The corpus proposes community gardens [1]."],
        }
    )
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _config: fake)

    report = asyncio.run(
        qp.run_query(
            corpus,
            "what brainstorming ideas exist?",
            load_config(EXAMPLE),
            mode="route",
            synthesize_answer=True,
        )
    )
    assert report.card_report.generated == 2
    assert [c.file.rel_path for c in report.candidates] == ["skills.md"]
    assert report.relevant_files == ["skills.md"]
    assert "[skills.md § Ideas]" in report.answer.text
    assert "skills.md" in report.answer.sources
    assert not report.answer.uncited_claims
    # actuals recorded per task
    assert TaskName.EXTRACT in report.actual_usage

    # cards persisted: a second run regenerates nothing (resumability).
    # Both files are excluded EXPLICITLY: an omitted decision is now
    # included by default (recall bias), so an empty list no longer means
    # "nothing routed".
    fake2 = FakeClient(
        {
            TaskName.QUERY_ROUTE: [
                json.dumps(
                    {
                        "decisions": [
                            {"file_hash": skills, "include": False, "reason": "n"},
                            {"file_hash": rivers, "include": False, "reason": "n"},
                        ]
                    }
                )
            ],
            TaskName.SYNTHESIZE: [],
        }
    )
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _config: fake2)
    report2 = asyncio.run(
        qp.run_query(corpus, "anything?", load_config(EXAMPLE), mode="route", synthesize_answer=True)
    )
    assert report2.card_report.generated == 0
    assert "No relevant content" in report2.answer.text


@pytest.mark.slow
def test_budget_gate_blocks_unconfirmed_spend(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(EXAMPLE)
    config.budget.confirm_above_usd = 0.0
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_in = 3.0
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_out = 15.0

    fake = FakeClient({TaskName.QUERY_ROUTE: [json.dumps({"decisions": []})]})
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _config: fake)
    with pytest.raises(BudgetExceededError):
        asyncio.run(qp.run_query(corpus, "q", config, all_files=True, confirm=None))


@pytest.mark.slow
def test_citation_repair_then_uncited_marker(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashes = _hashes(corpus)
    skills, rivers = hashes["skills.md"], hashes["rivers.txt"]
    extraction = json.dumps(
        {
            "file_hash": skills,
            "relevant": True,
            "confidence": 0.9,
            "findings": [
                {"claim": "c", "quote": "q", "heading_path": ["Ideas"], "page_no": None}
            ],
        }
    )
    fake = FakeClient(
        {
            TaskName.QUERY_ROUTE: [
                json.dumps(
                    {
                        "decisions": [
                            {"file_hash": skills, "include": True, "reason": "r"},
                            # explicit exclude: omitted decisions are now
                            # included by default (recall bias)
                            {"file_hash": rivers, "include": False, "reason": "n"},
                        ]
                    }
                )
            ],
            TaskName.EXTRACT: [extraction],
            TaskName.SYNTHESIZE: [
                "Bad cite [9].",  # first attempt: unresolvable
                "Still bad [42].",  # repair attempt also fails
            ],
        }
    )
    monkeypatch.setattr(qp, "LLMTaskClient", lambda _config: fake)
    report = asyncio.run(qp.run_query(corpus, "q", load_config(EXAMPLE), mode="route", synthesize_answer=True))
    assert "[uncited]" in report.answer.text
    assert report.answer.uncited_claims  # surfaced, not dropped
    assert len(fake.calls[TaskName.SYNTHESIZE]) == 2  # exactly one repair
