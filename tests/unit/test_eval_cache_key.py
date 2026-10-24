"""The evaluation cache never serves a verdict the current setup didn't make.

Cached verdicts are keyed on the extraction prompt as well as the question,
so editing a prompt invalidates them; and a verdict produced by a fallback
model is never stored under the primary model's key.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.prompts import extract as extract_prompt
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _reply() -> str:
    return json.dumps(
        {"file_hash": "a" * 64, "relevant": False, "confidence": 0.9, "findings": []}
    )


def _sweep(corpus: Path, fake: FakeClient) -> qp.QueryReport:
    return asyncio.run(
        qp.run_query(
            corpus,
            "q",
            load_config(EXAMPLE),
            mode="sweep",
            client=fake,  # type: ignore[arg-type]
            confirm=lambda _e: True,
        )
    )


def test_unchanged_prompt_serves_cached_verdict(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    _sweep(corpus, FakeClient({TaskName.EXTRACT: [_reply()]}))
    assert _sweep(corpus, FakeClient({})).cached_evaluations == 1


def test_prompt_edit_invalidates_cached_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _sweep(corpus, FakeClient({TaskName.EXTRACT: [_reply()]}))
    monkeypatch.setattr(extract_prompt, "SYSTEM", extract_prompt.SYSTEM + " Edited.")
    fake = FakeClient({TaskName.EXTRACT: [_reply()]})
    report = _sweep(corpus, fake)
    assert report.cached_evaluations == 0
    assert len(fake.calls[TaskName.EXTRACT]) == 1


def test_fallback_served_verdict_is_not_cached(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    _sweep(
        corpus, FakeClient({TaskName.EXTRACT: [_reply()]}, fallback={TaskName.EXTRACT})
    )
    fake = FakeClient({TaskName.EXTRACT: [_reply()]})
    assert _sweep(corpus, fake).cached_evaluations == 0
    assert len(fake.calls[TaskName.EXTRACT]) == 1
