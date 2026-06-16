"""SQLite store tests (M2 acceptance)."""

from __future__ import annotations

from pathlib import Path

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.ingest.chunker import Chunk


def _row(file_hash: str = "h1", rel_path: str = "a.md") -> FileRow:
    return FileRow(
        file_hash=file_hash,
        rel_path=rel_path,
        size_bytes=10,
        format="md",
        parsed_at="2026-01-01T00:00:00+00:00",
        parse_status="ok",
        parse_error=None,
        md_cache_path=f"{file_hash}.md",
        page_count=None,
    )


def test_upsert_known_and_meta(tmp_path: Path) -> None:
    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(_row())
        assert store.known_files() == {"a.md": "h1"}
        store.write_meta("/corpus")
        meta = store.read_meta()
        assert meta is not None and meta["corpus_root"] == "/corpus"


def test_delete_cascades_chunks(tmp_path: Path) -> None:
    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(_row())
        store.replace_chunks(
            "h1",
            [Chunk("h1:0", "h1", 0, ["A"], None, 0, 5, "hello")],
        )
        assert store.chunk_count() == 1
        store.delete_file("h1")
        assert store.chunk_count() == 0
        assert store.known_files() == {}


def test_flagged_files_and_move(tmp_path: Path) -> None:
    with CatalogStore(tmp_path / "c.db") as store:
        bad = FileRow(
            file_hash="h2",
            rel_path="scan.pdf",
            size_bytes=5,
            format="pdf",
            parsed_at=None,
            parse_status="zero_text",
            parse_error=None,
            md_cache_path=None,
            page_count=12,
        )
        store.upsert_file(_row())
        store.upsert_file(bad)
        assert [r.rel_path for r in store.flagged_files()] == ["scan.pdf"]
        store.update_rel_path("h1", "moved/a.md")
        assert store.known_files()["moved/a.md"] == "h1"
