"""Routing shard limits and omitted-decision handling (M7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.catalog.summarizer import CatalogCard
from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.query.budget import route_shard_limits
from corpusqa.query.router import _shard_cards, route
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def test_shards_respect_output_capacity() -> None:
    """Tiny cards in a huge input window must still shard by output room."""
    config = load_config(EXAMPLE)  # route: 1M-token window, 4096 max_tokens
    _, max_cards = route_shard_limits(config)
    client = FakeClient({})
    rendered = ["FILE x\ncard"] * (max_cards * 2 + 5)  # trivially fits input
    shards = _shard_cards(rendered, client, config)  # type: ignore[arg-type]
    assert len(shards) == 3
    assert all(len(s) <= max_cards for s in shards)
    assert sum(len(s) for s in shards) == len(rendered)


def test_shards_still_respect_input_window() -> None:
    config = load_config(EXAMPLE)
    config.tasks[TaskName.QUERY_ROUTE].context_window = 4_096
    # input budget floors at 2_000 tokens; each card is ~1_500 tokens
    client = FakeClient({})
    rendered = ["x" * 6_000] * 4
    shards = _shard_cards(rendered, client, config)  # type: ignore[arg-type]
    assert len(shards) == 4  # one oversized-ish card per shard


def _row(file_hash: str, rel_path: str) -> FileRow:
    return FileRow(
        file_hash=file_hash,
        rel_path=rel_path,
        size_bytes=1,
        format="md",
        parsed_at=None,
        parse_status="ok",
        parse_error=None,
        md_cache_path=f"{file_hash}.md",
        page_count=None,
    )


def test_omitted_decision_is_included_by_default(tmp_path: Path) -> None:
    """A card the model returns no decision for must not vanish silently."""
    config = load_config(EXAMPLE)
    h1, h2 = "1" * 64, "2" * 64
    with CatalogStore(tmp_path / "c.db") as store:
        for h, rel in ((h1, "a.md"), (h2, "b.md")):
            store.upsert_file(_row(h, rel))
            store.upsert_card(h, CatalogCard(claims=[f"claim {rel}"]), "m")

        client = FakeClient(
            {
                TaskName.QUERY_ROUTE: [
                    json.dumps(
                        {
                            "decisions": [
                                # decision for h1 only; h2 omitted entirely
                                {"file_hash": h1, "include": False, "reason": "no"}
                            ]
                        }
                    )
                ]
            }
        )
        candidates = asyncio.run(
            route("q", None, store, client, config)  # type: ignore[arg-type]
        )
    assert [c.file.rel_path for c in candidates] == ["b.md"]
    assert "included by default" in candidates[0].reason


def test_explicit_exclusion_is_honored(tmp_path: Path) -> None:
    config = load_config(EXAMPLE)
    h1 = "3" * 64
    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(_row(h1, "a.md"))
        store.upsert_card(h1, CatalogCard(claims=["x"]), "m")
        client = FakeClient(
            {
                TaskName.QUERY_ROUTE: [
                    json.dumps(
                        {
                            "decisions": [
                                {"file_hash": h1, "include": False, "reason": "no"}
                            ]
                        }
                    )
                ]
            }
        )
        candidates = asyncio.run(
            route("q", None, store, client, config)  # type: ignore[arg-type]
        )
    assert candidates == []
