"""Index pipeline: discovery -> conversion -> cache -> store.

Lives in the library (not the CLI) so it is testable headlessly; the CLI
only renders its report. Per-file results are persisted as they complete
(resumability); a single file's failure never aborts the batch
(design doc section 10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.config.schema import AppConfig
from corpusqa.ingest import chunker, converter
from corpusqa.ingest.discovery import (
    INDEX_DIR_NAME,
    DiscoveredFile,
    DriftKind,
    discover,
)

_log = logging.getLogger("corpusqa.ingest")


@dataclass
class IndexReport:
    """Summary of one index run.

    Attributes:
        parsed: Files converted this run (new/modified/forced).
        moved: Files whose path metadata was updated.
        deleted: Files removed from the index.
        unchanged: Files skipped as up to date.
        duplicates: On-disk copies of content owned by another path
            (identity is the content hash; copies are not indexed twice).
        flagged: ``(rel_path, parse_status)`` for non-ok conversions.
        chunks_written: Total chunk rows written this run.
    """

    parsed: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    duplicates: int = 0
    flagged: list[tuple[str, str]] = field(default_factory=list)
    chunks_written: int = 0


def index_paths(corpus_root: Path) -> tuple[Path, Path, Path]:
    """Returns (index_dir, db_path, md_cache_dir) for a corpus root."""
    index_dir = corpus_root / INDEX_DIR_NAME
    return index_dir, index_dir / "catalog.db", index_dir / "md"


def run_index(
    corpus_root: Path,
    config: AppConfig,
    force: list[Path] | None = None,
) -> IndexReport:
    """Runs the incremental index pipeline over a corpus directory.

    Args:
        corpus_root: Directory of source documents.
        config: Validated application configuration.
        force: Source paths to reconvert even if unchanged.

    Returns:
        A per-run report.

    Raises:
        IngestError: Only for corpus-level failures (unreadable root);
            per-file failures are captured in the report.
    """
    corpus_root = corpus_root.resolve()
    _, db_path, cache_dir = index_paths(corpus_root)
    forced = {p.resolve().relative_to(corpus_root).as_posix() for p in (force or [])}
    report = IndexReport()

    converter.check_converter_available()
    with CatalogStore(db_path) as store:
        known = store.known_files()
        found = discover(corpus_root, config.ingest.extensions, known)

        # Build the Docling converter at most once per run and reuse it for
        # every file: its model weights load on first use, so a per-file
        # converter reloads them each time. Lazy so an index with no parse
        # work (all unchanged) loads nothing.
        converter_cache: list[object] = []

        def get_converter() -> object:
            if not converter_cache:
                converter_cache.append(
                    converter.build_converter(
                        config.ingest.ocr, config.ingest.pdf_backend
                    )
                )
            return converter_cache[0]

        for item in found:
            if item.drift is DriftKind.DELETED:
                _delete(store, cache_dir, item.file_hash)
                report.deleted += 1
            elif item.drift is DriftKind.DUPLICATE:
                # Content already indexed under item.old_rel_path; identity
                # is the hash, so a second copy is recorded nowhere.
                report.duplicates += 1
            elif item.drift is DriftKind.MOVED:
                store.update_rel_path(item.file_hash, item.rel_path)
                report.moved += 1
            elif item.drift is DriftKind.UNCHANGED and item.rel_path not in forced:
                report.unchanged += 1
            else:  # NEW, MODIFIED, or forced
                if item.drift is DriftKind.MODIFIED:
                    _delete(store, cache_dir, known[item.rel_path])
                _convert_one(
                    store, cache_dir, corpus_root, item, config, report,
                    get_converter(),
                )

        store.write_meta(str(corpus_root))
    return report


def _delete(store: CatalogStore, cache_dir: Path, file_hash: str) -> None:
    """Removes a file's rows and cache artifacts."""
    store.delete_file(file_hash)
    for suffix in (".md", ".meta.json", ".chunks.jsonl", ".pages.json"):
        (cache_dir / f"{file_hash}{suffix}").unlink(missing_ok=True)


def _convert_one(
    store: CatalogStore,
    cache_dir: Path,
    corpus_root: Path,
    item: DiscoveredFile,
    config: AppConfig,
    report: IndexReport,
    document_converter: object | None = None,
) -> None:
    """Converts, caches, stores, and chunks one file."""
    source = corpus_root / item.rel_path
    result = converter.convert(
        source,
        item.file_hash,
        cache_dir,
        config.ingest.ocr,
        pdf_backend=config.ingest.pdf_backend,
        document_converter=document_converter,  # type: ignore[arg-type]
    )
    store.upsert_file(
        FileRow(
            file_hash=item.file_hash,
            rel_path=item.rel_path,
            size_bytes=item.size_bytes,
            format=source.suffix.lower().lstrip("."),
            parsed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            parse_status=result.parse_status,
            parse_error=result.parse_error,
            md_cache_path=(result.md_cache_path.name if result.md_cache_path else None),
            page_count=result.page_count,
        )
    )
    _log.info(
        "parsed path=%s status=%s pages=%s",
        item.rel_path,
        result.parse_status,
        result.page_count,
    )
    report.parsed += 1
    if result.parse_status != converter.STATUS_OK:
        report.flagged.append((item.rel_path, result.parse_status))

    usable = (converter.STATUS_OK, converter.STATUS_PARTIAL)
    if result.parse_status in usable and result.document is not None:
        markdown = result.md_cache_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        chunks = chunker.chunk_document(
            result.document,
            item.file_hash,
            markdown,
            config.ingest.chunk_target_tokens,
        )
        chunker.write_chunk_texts(chunks, cache_dir, item.file_hash)
        store.replace_chunks(item.file_hash, chunks)
        report.chunks_written += len(chunks)


@dataclass(frozen=True)
class StatusReport:
    """Index status: drift, flags, and meta.

    Attributes:
        meta: The ``index_meta`` row, or None when no index exists.
        drift: Discoveries an ``index`` run would act on (not unchanged,
            not duplicates -- duplicates are reported separately because
            indexing leaves them alone by design).
        duplicates: On-disk copies of content owned by another path.
        flagged: Files invisible or partially visible to queries.
        file_count: Indexed files.
        chunk_count: Indexed chunk provenance rows.
    """

    meta: dict[str, object] | None
    drift: list[DiscoveredFile]
    duplicates: list[DiscoveredFile]
    flagged: list[FileRow]
    file_count: int
    chunk_count: int


def run_status(corpus_root: Path, config: AppConfig) -> StatusReport:
    """Computes the status report without modifying the index.

    Args:
        corpus_root: Directory of source documents.
        config: Validated application configuration.

    Returns:
        The status report (empty index yields meta=None and all-NEW drift).
    """
    corpus_root = corpus_root.resolve()
    _, db_path, _ = index_paths(corpus_root)
    if not db_path.exists():  # read-only: never create an index as a side effect
        found = discover(corpus_root, config.ingest.extensions, known={})
        return StatusReport(
            meta=None,
            drift=[f for f in found if f.drift is not DriftKind.DUPLICATE],
            duplicates=[f for f in found if f.drift is DriftKind.DUPLICATE],
            flagged=[],
            file_count=0,
            chunk_count=0,
        )
    with CatalogStore(db_path) as store:
        known = store.known_files()
        found = discover(corpus_root, config.ingest.extensions, known)
        return StatusReport(
            meta=store.read_meta(),
            drift=[
                f
                for f in found
                if f.drift not in (DriftKind.UNCHANGED, DriftKind.DUPLICATE)
            ],
            duplicates=[f for f in found if f.drift is DriftKind.DUPLICATE],
            flagged=store.flagged_files(),
            file_count=store.file_count(),
            chunk_count=store.chunk_count(),
        )