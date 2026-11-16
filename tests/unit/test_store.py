"""SQLite store tests (M2 acceptance)."""

from __future__ import annotations

from pathlib import Path

from corpusqa.catalog.store import CatalogStore, FileRow


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


def test_delete_file_removes_row(tmp_path: Path) -> None:
    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(_row())
        store.delete_file("h1")
        assert store.known_files() == {}


def test_upsert_file_update_keeps_cards_and_evaluations(tmp_path: Path) -> None:
    from corpusqa.catalog.summarizer import CatalogCard

    with CatalogStore(tmp_path / "c.db") as store:
        store.upsert_file(_row())
        store.upsert_card("h1", CatalogCard(claims=["x"]), "m")
        store.put_evaluation("r", "qh", "h1", "m", "{}")
        store.upsert_file(_row(rel_path="b.md"))  # same hash: an update
        assert store.known_files() == {"b.md": "h1"}
        assert store.get_evaluation("qh", "h1", "m") == "{}"
        assert store.files_needing_cards() == []


def test_legacy_chunks_table_is_dropped(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "c.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY)")
    with CatalogStore(db):
        pass
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "chunks" not in tables


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
