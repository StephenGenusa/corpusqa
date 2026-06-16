"""SQLite catalog store. The only module that emits SQL.

Connections use WAL mode with foreign keys enabled; rows are written per
file as work completes so every fan-out stage is crash-resumable
(design doc section 4.2). Keys are content hashes; paths are metadata.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from corpusqa.catalog.summarizer import CatalogCard
from corpusqa.ingest.chunker import Chunk

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS files (
    file_hash      TEXT PRIMARY KEY,
    rel_path       TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    format         TEXT NOT NULL,
    parsed_at      TEXT,
    parse_status   TEXT NOT NULL,
    parse_error    TEXT,
    md_cache_path  TEXT,
    page_count     INTEGER
);

CREATE TABLE IF NOT EXISTS catalog_cards (
    file_hash    TEXT PRIMARY KEY REFERENCES files(file_hash) ON DELETE CASCADE,
    card_json    TEXT NOT NULL,
    model_used   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    eval_id        TEXT PRIMARY KEY,         -- question_hash:file_hash:model
    run_id         TEXT NOT NULL,
    file_hash      TEXT NOT NULL REFERENCES files(file_hash) ON DELETE CASCADE,
    question_hash  TEXT NOT NULL,        -- sha256(question+definition+relevance)
    question       TEXT NOT NULL DEFAULT '',
    model_used     TEXT NOT NULL,
    result_json    TEXT NOT NULL,            -- serialized ExtractionResult
    evaluated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_lookup
    ON evaluations(question_hash, file_hash, model_used);
CREATE INDEX IF NOT EXISTS idx_eval_run ON evaluations(run_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    file_hash    TEXT NOT NULL REFERENCES files(file_hash) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    page_no      INTEGER,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS index_meta (
    rowid_one        INTEGER PRIMARY KEY CHECK (rowid_one = 1),
    schema_version   INTEGER NOT NULL,
    embed_model      TEXT,
    embed_dim        INTEGER,
    corpus_root      TEXT NOT NULL,
    last_indexed_at  TEXT
);

CREATE TABLE IF NOT EXISTS run_costs (
    run_id      TEXT NOT NULL,
    task        TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL,
    tokens_out  INTEGER NOT NULL,
    cost_usd    REAL NOT NULL,
    at          TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FileRow:
    """One row of the ``files`` table."""

    file_hash: str
    rel_path: str
    size_bytes: int
    format: str
    parsed_at: str | None
    parse_status: str
    parse_error: str | None
    md_cache_path: str | None
    page_count: int | None


class CatalogStore:
    """Access layer over the SQLite catalog database.

    Usable as a context manager; commits are per write method so partial
    progress survives crashes (resumability by construction).
    """

    def __init__(self, db_path: Path) -> None:
        """Opens (creating if needed) the catalog database.

        Args:
            db_path: Path to ``catalog.db`` under the index directory.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate_if_needed()
        self._conn.executescript(DDL)
        self._conn.commit()

    def _migrate_if_needed(self) -> None:
        """Brings a pre-v2 database forward in place.

        Two independent migrations, each guarded only by its own table's
        existence (they must not be nested: a DB can have an old-shape
        ``evaluations`` table without ``catalog_cards``, and the column add
        still has to run). Cards regenerate lazily, so dropping is cheap and
        avoids ALTER gymnastics.
        """
        cards = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='catalog_cards'"
        ).fetchone()
        if cards is not None:
            cols = {
                r[1] for r in self._conn.execute("PRAGMA table_info(catalog_cards)")
            }
            if "card_json" not in cols:
                self._conn.execute("DROP TABLE catalog_cards")
                self._conn.commit()
        evals = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE name='evaluations'"
        ).fetchone()
        if evals is not None:
            eval_cols = {
                r[1] for r in self._conn.execute("PRAGMA table_info(evaluations)")
            }
            if "question" not in eval_cols:
                self._conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN question "
                    "TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()

    def close(self) -> None:
        """Closes the connection."""
        self._conn.close()

    def __enter__(self) -> CatalogStore:
        """Returns self for context-manager use."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Closes on exit."""
        self.close()

    # -- files ------------------------------------------------------------

    def known_files(self) -> dict[str, str]:
        """Returns the indexed state as ``rel_path -> file_hash``."""
        rows = self._conn.execute("SELECT rel_path, file_hash FROM files")
        return {r["rel_path"]: r["file_hash"] for r in rows}

    def upsert_file(self, row: FileRow) -> None:
        """Inserts or replaces one file row (commit per call)."""
        self._conn.execute(
            """INSERT OR REPLACE INTO files
               (file_hash, rel_path, size_bytes, format, parsed_at,
                parse_status, parse_error, md_cache_path, page_count)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                row.file_hash,
                row.rel_path,
                row.size_bytes,
                row.format,
                row.parsed_at,
                row.parse_status,
                row.parse_error,
                row.md_cache_path,
                row.page_count,
            ),
        )
        self._conn.commit()

    def update_rel_path(self, file_hash: str, rel_path: str) -> None:
        """Updates the path of a moved file (no reparse)."""
        self._conn.execute(
            "UPDATE files SET rel_path = ? WHERE file_hash = ?",
            (rel_path, file_hash),
        )
        self._conn.commit()

    def delete_file(self, file_hash: str) -> None:
        """Removes a file row; cards and chunks cascade."""
        self._conn.execute("DELETE FROM files WHERE file_hash = ?", (file_hash,))
        self._conn.commit()

    @staticmethod
    def _to_file_row(row: sqlite3.Row) -> FileRow:
        return FileRow(
            file_hash=row["file_hash"],
            rel_path=row["rel_path"],
            size_bytes=row["size_bytes"],
            format=row["format"],
            parsed_at=row["parsed_at"],
            parse_status=row["parse_status"],
            parse_error=row["parse_error"],
            md_cache_path=row["md_cache_path"],
            page_count=row["page_count"],
        )

    def files_ok(self) -> list[FileRow]:
        """Returns queryable files (ok or partial), ordered by path."""
        rows = self._conn.execute(
            """SELECT * FROM files
               WHERE parse_status IN ('ok', 'partial_parse')
               ORDER BY rel_path"""
        ).fetchall()
        return [self._to_file_row(r) for r in rows]

    def get_file(self, file_hash: str) -> FileRow | None:
        """Fetches one file row by hash."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        if row is None:
            return None
        return FileRow(
            file_hash=row["file_hash"],
            rel_path=row["rel_path"],
            size_bytes=row["size_bytes"],
            format=row["format"],
            parsed_at=row["parsed_at"],
            parse_status=row["parse_status"],
            parse_error=row["parse_error"],
            md_cache_path=row["md_cache_path"],
            page_count=row["page_count"],
        )

    def flagged_files(self) -> list[FileRow]:
        """Returns files whose ``parse_status`` is not ``ok``."""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE parse_status != 'ok' ORDER BY rel_path"
        ).fetchall()
        return [
            FileRow(
                file_hash=r["file_hash"],
                rel_path=r["rel_path"],
                size_bytes=r["size_bytes"],
                format=r["format"],
                parsed_at=r["parsed_at"],
                parse_status=r["parse_status"],
                parse_error=r["parse_error"],
                md_cache_path=r["md_cache_path"],
                page_count=r["page_count"],
            )
            for r in rows
        ]

    def file_count(self) -> int:
        """Returns the number of indexed files."""
        return int(self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def files_needing_cards(self) -> list[FileRow]:
        """Returns parsed files (ok/partial) lacking a catalog card.

        Keying card generation on absence makes the LLM fan-out resumable:
        a crash mid-catalog resumes by skipping files already carded.
        """
        rows = self._conn.execute(
            """SELECT f.* FROM files f
               LEFT JOIN catalog_cards c ON c.file_hash = f.file_hash
               WHERE c.file_hash IS NULL
                 AND f.parse_status IN ('ok', 'partial_parse')
               ORDER BY f.rel_path"""
        ).fetchall()
        return [self._to_file_row(r) for r in rows]

    # -- catalog cards ------------------------------------------------------

    def upsert_card(self, file_hash: str, card: CatalogCard, model: str) -> None:
        """Inserts or replaces one catalog card (commit per call)."""
        self._conn.execute(
            """INSERT OR REPLACE INTO catalog_cards
               (file_hash, card_json, model_used, created_at)
               VALUES (?,?,?,?)""",
            (file_hash, card.model_dump_json(), model, _now()),
        )
        self._conn.commit()

    def all_cards(self) -> list[tuple[FileRow, CatalogCard]]:
        """Returns every (file, card) pair for routing."""
        rows = self._conn.execute(
            """SELECT f.*, c.card_json
               FROM files f JOIN catalog_cards c ON c.file_hash = f.file_hash
               ORDER BY f.rel_path"""
        ).fetchall()
        return [
            (self._to_file_row(r), CatalogCard.model_validate_json(r["card_json"]))
            for r in rows
        ]

    def all_parseable_files(self) -> list[FileRow]:
        """Returns files visible to queries (sweep candidates; no cards needed)."""
        rows = self._conn.execute(
            """SELECT * FROM files
               WHERE parse_status IN ('ok', 'partial_parse')
               ORDER BY rel_path"""
        ).fetchall()
        return [self._to_file_row(r) for r in rows]

    # -- evaluations (sweep cache + resumability) ---------------------------

    def get_evaluation(
        self, question_hash: str, file_hash: str, model: str
    ) -> str | None:
        """Returns the cached result_json for (question, file, model), if any."""
        row = self._conn.execute(
            """SELECT result_json FROM evaluations
               WHERE question_hash = ? AND file_hash = ? AND model_used = ?""",
            (question_hash, file_hash, model),
        ).fetchone()
        return row["result_json"] if row else None

    def put_evaluation(
        self,
        run_id: str,
        question_hash: str,
        file_hash: str,
        model: str,
        result_json: str,
        question: str = "",
    ) -> None:
        """Persists one evaluation (commit per call: crash-resumable)."""
        self._conn.execute(
            """INSERT OR REPLACE INTO evaluations
               (eval_id, run_id, file_hash, question_hash, question,
                model_used, result_json, evaluated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                f"{question_hash}:{file_hash}:{model}",
                run_id,
                file_hash,
                question_hash,
                question,
                model,
                result_json,
                _now(),
            ),
        )
        self._conn.commit()

    def evaluations_for_file(self, file_hash: str) -> list[tuple[str, str, str]]:
        """Returns ``(question, evaluated_at, result_json)`` rows, newest first."""
        rows = self._conn.execute(
            """SELECT question, evaluated_at, result_json FROM evaluations
               WHERE file_hash = ? ORDER BY evaluated_at DESC""",
            (file_hash,),
        ).fetchall()
        return [(r["question"], r["evaluated_at"], r["result_json"]) for r in rows]

    def prune_evaluations(self, keep_runs: int) -> int:
        """Keeps only the most recent ``keep_runs`` run_ids; returns rows removed."""
        cursor = self._conn.execute(
            """DELETE FROM evaluations WHERE run_id NOT IN (
                 SELECT run_id FROM (
                   SELECT run_id, MAX(evaluated_at) AS latest
                   FROM evaluations GROUP BY run_id
                   ORDER BY latest DESC LIMIT ?))""",
            (keep_runs,),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    # -- costs ----------------------------------------------------------------

    def record_cost(
        self,
        run_id: str,
        task: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        """Appends one cost row for a run/task aggregate."""
        self._conn.execute(
            "INSERT INTO run_costs VALUES (?,?,?,?,?,?,?)",
            (run_id, task, model, tokens_in, tokens_out, cost_usd, _now()),
        )
        self._conn.commit()

    # -- chunks -----------------------------------------------------------

    def replace_chunks(self, file_hash: str, chunks: list[Chunk]) -> None:
        """Replaces all chunk provenance rows for a file."""
        self._conn.execute("DELETE FROM chunks WHERE file_hash = ?", (file_hash,))
        self._conn.executemany(
            """INSERT INTO chunks
               (chunk_id, file_hash, chunk_index, heading_path, page_no,
                char_start, char_end)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    c.chunk_id,
                    c.file_hash,
                    c.chunk_index,
                    json.dumps(c.heading_path, ensure_ascii=False),
                    c.page_no,
                    c.char_start,
                    c.char_end,
                )
                for c in chunks
            ],
        )
        self._conn.commit()

    def chunk_count(self) -> int:
        """Returns the number of chunk provenance rows."""
        return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    # -- meta -------------------------------------------------------------

    def write_meta(self, corpus_root: str) -> None:
        """Writes/refreshes the single ``index_meta`` row."""
        self._conn.execute(
            """INSERT INTO index_meta
               (rowid_one, schema_version, corpus_root, last_indexed_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(rowid_one) DO UPDATE SET
                 schema_version = excluded.schema_version,
                 corpus_root = excluded.corpus_root,
                 last_indexed_at = excluded.last_indexed_at""",
            (SCHEMA_VERSION, corpus_root, _now()),
        )
        self._conn.commit()

    def read_meta(self) -> dict[str, object] | None:
        """Returns the ``index_meta`` row as a dict, or None on fresh DBs."""
        row = self._conn.execute("SELECT * FROM index_meta").fetchone()
        return dict(row) if row is not None else None
