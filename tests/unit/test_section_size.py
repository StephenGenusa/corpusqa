"""Extraction section size is a per-task knob, and part of the cache key.

``tasks.extract.max_input_tokens`` caps how much document text each extract
call sees, so it sits beside the model it was tuned for and sizes can be
compared without code changes. Unset, sections fill what the extract window
allows; a value above that is capped, with a warning.
A verdict depends on how the file was sectioned, so cached verdicts never
cross section sizes or splitter versions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import corpusqa.cli.main as cli
from corpusqa import splitting
from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.query import pipeline as qp
from corpusqa.query.extractor import plan_sections
from tests.unit.fakes import FakeClient
from tests.unit.test_budget_gates import _indexed_corpus

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
# 40 headed pages of ~400 chars (~100 fake tokens) each: ~4,000 tokens.
BOOK = "".join(f"## Page {i}\n" + "word " * 78 + "\n" for i in range(40))


def _config() -> AppConfig:
    return load_config(EXAMPLE)


def test_unset_section_size_fills_the_extract_window() -> None:
    assert len(plan_sections(BOOK, FakeClient({}), _config())) == 1  # type: ignore[arg-type]


def test_max_input_tokens_caps_section_size() -> None:
    config = _config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 1_000
    fake = FakeClient({})
    sections = plan_sections(BOOK, fake, config)  # type: ignore[arg-type]
    tokens = [fake.count_tokens(TaskName.EXTRACT, s) for s in sections]
    assert len(sections) > 1
    assert all(t <= 1_000 for t in tokens)
    # packed by the real tokenizer: every section but the last is within one
    # page (~100 tokens) of the cap, not short of it by a chars-per-token guess
    assert all(t > 1_000 - 110 for t in tokens[:-1])
    assert "".join(sections) == BOOK


def test_max_input_tokens_above_the_window_is_capped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 10_000_000
    with caplog.at_level(logging.WARNING, logger="corpusqa.query"):
        sections = plan_sections(BOOK, FakeClient({}), config)  # type: ignore[arg-type]
    assert len(sections) == 1
    assert "max_input_tokens" in caplog.text


def _reply() -> str:
    return json.dumps(
        {"file_hash": "a" * 64, "relevant": False, "confidence": 0.9, "findings": []}
    )


def _sweep(corpus: Path, config: AppConfig, fake: FakeClient) -> qp.QueryReport:
    return asyncio.run(
        qp.run_query(
            corpus,
            "q",
            config,
            mode="sweep",
            client=fake,  # type: ignore[arg-type]
            confirm=lambda _e: True,
        )
    )


def test_changing_section_size_invalidates_cached_verdicts(tmp_path: Path) -> None:
    corpus = _indexed_corpus(tmp_path)
    config = _config()
    config.tasks[TaskName.EXTRACT].max_input_tokens = 2_000
    _sweep(corpus, config, FakeClient({TaskName.EXTRACT: [_reply()]}))
    config.tasks[TaskName.EXTRACT].max_input_tokens = 4_000
    report = _sweep(corpus, config, FakeClient({TaskName.EXTRACT: [_reply()]}))
    assert report.cached_evaluations == 0


def test_splitter_change_invalidates_cached_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _indexed_corpus(tmp_path)
    _sweep(corpus, _config(), FakeClient({TaskName.EXTRACT: [_reply()]}))
    monkeypatch.setattr(splitting, "SPLITTER_VERSION", splitting.SPLITTER_VERSION + 1)
    report = _sweep(corpus, _config(), FakeClient({TaskName.EXTRACT: [_reply()]}))
    assert report.cached_evaluations == 0


def _query_config(monkeypatch: pytest.MonkeyPatch, *flags: str) -> AppConfig:
    seen: list[AppConfig] = []

    async def fake_run_query(
        _root: Path, _q: str, config: AppConfig, **_: object
    ) -> None:
        seen.append(config)
        raise SystemExit(0)  # short-circuit; only the config matters

    monkeypatch.setattr(cli, "run_query", fake_run_query)
    monkeypatch.setattr(cli, "_warn_drift", lambda *_: None)
    with pytest.raises(SystemExit):
        cli.main(["query", "q", "--config", str(EXAMPLE), *flags])
    return seen[0]


def test_cli_section_tokens_flag_overrides_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = _query_config(monkeypatch, "--section-tokens", "6000")
    assert config.tasks[TaskName.EXTRACT].max_input_tokens == 6_000


def test_cli_rejects_a_too_small_section_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code = cli.main(["query", "q", "--config", str(EXAMPLE), "--section-tokens", "10"])
    assert code == cli.EXIT_USER_ERROR
