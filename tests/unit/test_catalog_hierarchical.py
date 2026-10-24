"""Catalog cards for oversize files: chunk -> partial cards -> merge (item 7)."""

from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import types

from corpusqa.catalog.summarizer import (
    CatalogCard,
    TermUse,
    _merge_cards,
    card_token_budget,
    generate_cards,
)
from corpusqa.config.schema import TaskName


def test_merge_cards_dedups_and_unions() -> None:
    c1 = CatalogCard(
        claims=["A", "B"],
        terms=[TermUse(term="grace", sense="unmerited favor")],
        entities=["Paul"],
        doc_type="essay",
    )
    c2 = CatalogCard(
        claims=["B", "C"],
        terms=[TermUse(term="Grace", sense="dup case")],  # dedups on term
        entities=["Paul", "John"],
        doc_type="other",
    )
    merged = _merge_cards([c1, c2])
    assert merged.claims == ["A", "B", "C"]
    assert [t.term for t in merged.terms] == ["grace"]
    assert merged.entities == ["Paul", "John"]
    assert merged.doc_type == "essay"  # first non-empty


def _cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        tasks={
            TaskName.CATALOG_SUMMARIZE: types.SimpleNamespace(
                context_window=2048, max_tokens=512, model="m"
            )
        }
    )


class _Store:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.upserts: list = []

    def files_needing_cards(self) -> list:
        return self._rows

    def upsert_card(self, file_hash: str, card: CatalogCard, model: str) -> None:
        self.upserts.append((file_hash, card))


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, task: TaskName, text: str) -> int:
        return max(1, len(text) // 4)

    async def complete(self, task, messages, **kw):
        self.calls += 1
        payload = {
            "claims": [f"claim-{self.calls}"],
            "terms": [],
            "implicit_topics": [],
            "answerable": [],
            "absences": [],
            "entities": [],
            "doc_type": "essay",
        }
        return types.SimpleNamespace(
            text=json.dumps(payload), tokens_in=1, tokens_out=1, finish_reason="stop"
        )


def test_oversize_file_is_chunked_and_merged() -> None:
    cfg = _cfg()
    budget = card_token_budget(cfg)
    row = types.SimpleNamespace(file_hash="h", rel_path="big.md")
    client = _Client()
    with tempfile.TemporaryDirectory() as d:
        cache = pathlib.Path(d)
        # _Client counts ~4 chars/token: about 3x the token budget.
        (cache / "h.md").write_text("A" * (budget * 4 * 3 + 40), encoding="utf-8")
        report = asyncio.run(generate_cards(_Store([row]), cache, client, cfg))
    assert report.generated == 1
    assert report.hierarchical == 1
    assert client.calls >= 3  # one call per chunk, merged with no extra LLM call


def test_small_file_uses_single_call() -> None:
    cfg = _cfg()
    row = types.SimpleNamespace(file_hash="h", rel_path="small.md")
    client = _Client()
    with tempfile.TemporaryDirectory() as d:
        cache = pathlib.Path(d)
        (cache / "h.md").write_text("short content", encoding="utf-8")
        report = asyncio.run(generate_cards(_Store([row]), cache, client, cfg))
    assert report.generated == 1
    assert report.hierarchical == 0
    assert client.calls == 1


class _DenseClient(_Client):
    """Tokenizes at 1 char/token: denser than any chars-per-token proxy."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def count_tokens(self, task: TaskName, text: str) -> int:
        return max(1, len(text))

    async def complete(self, task, messages, **kw):
        self.sent.append(messages[-1]["content"])
        return await super().complete(task, messages, **kw)


def test_dense_file_is_split_by_real_token_count() -> None:
    cfg = _cfg()
    window = cfg.tasks[TaskName.CATALOG_SUMMARIZE].context_window
    client = _DenseClient()
    row = types.SimpleNamespace(file_hash="h", rel_path="dense.md")
    with tempfile.TemporaryDirectory() as d:
        cache = pathlib.Path(d)
        # Fewer chars than 4 x window, but more real tokens than the window.
        (cache / "h.md").write_text("B" * (window * 2), encoding="utf-8")
        report = asyncio.run(generate_cards(_Store([row]), cache, client, cfg))
    assert report.generated == 1 and report.hierarchical == 1
    assert all(len(text) < window for text in client.sent)  # each call fits
