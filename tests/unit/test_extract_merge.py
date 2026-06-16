"""extract_file verdict merging: reasoning and confidence survive (M7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.query.extractor import extract_file
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _reply(relevant: bool, confidence: float, reasoning: str, findings: int = 0) -> str:
    return json.dumps(
        {
            "file_hash": "h" * 64,
            "relevant": relevant,
            "confidence": confidence,
            "reasoning": reasoning,
            "findings": [
                {"claim": f"c{i}", "quote": "q", "heading_path": [], "page_no": None}
                for i in range(findings)
            ],
        }
    )


def _run(markdown: str, replies: list[str]):
    config = load_config(EXAMPLE)
    client = FakeClient({TaskName.EXTRACT: replies})
    return asyncio.run(
        extract_file(
            "q",
            None,
            "a.md",
            "h" * 64,
            markdown,
            client,
            config,  # type: ignore[arg-type]
        )
    )


def test_single_section_keeps_reasoning_both_verdicts() -> None:
    relevant = _run("text", [_reply(True, 0.9, "matches the question", 1)])
    assert relevant.relevant and relevant.reasoning == "matches the question"
    assert relevant.confidence == 0.9

    irrelevant = _run("text", [_reply(False, 0.7, "different topic entirely")])
    assert not irrelevant.relevant
    assert irrelevant.reasoning == "different topic entirely"
    assert irrelevant.confidence == 0.7  # the model's verdict, not 0.0


def test_multi_section_merge_relevant() -> None:
    # force two sections: two top-level headings, text over the char budget
    # is not needed -- shrink the budget via a tiny context window instead
    config = load_config(EXAMPLE)
    config.tasks[TaskName.EXTRACT].context_window = 1024  # min allowed
    markdown = "# A\n" + "a" * 1800 + "\n# B\n" + "b" * 1800
    client = FakeClient(
        {
            TaskName.EXTRACT: [
                _reply(False, 0.6, "section A is off-topic"),
                _reply(True, 0.8, "section B answers it", findings=2),
            ]
        }
    )
    result = asyncio.run(
        extract_file("q", None, "a.md", "h" * 64, markdown, client, config)  # type: ignore[arg-type]
    )
    assert result.relevant
    assert result.confidence == 0.8  # max over relevant sections
    assert result.reasoning == "section B answers it"  # irrelevant reasoning dropped
    assert len(result.findings) == 2


def test_multi_section_merge_irrelevant_uses_min_confidence() -> None:
    config = load_config(EXAMPLE)
    config.tasks[TaskName.EXTRACT].context_window = 1024
    markdown = "# A\n" + "a" * 1800 + "\n# B\n" + "b" * 1800
    client = FakeClient(
        {
            TaskName.EXTRACT: [
                _reply(False, 0.9, "A is unrelated"),
                _reply(False, 0.5, "B is unrelated"),
            ]
        }
    )
    result = asyncio.run(
        extract_file("q", None, "a.md", "h" * 64, markdown, client, config)  # type: ignore[arg-type]
    )
    assert not result.relevant
    assert result.confidence == 0.5  # weakest negative verdict bounds the file
    assert result.reasoning == "A is unrelated | B is unrelated"
