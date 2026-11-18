"""Index pipeline: discovery -> conversion -> cache -> store.

Lives in the library (not the CLI) so it is testable headlessly; the CLI
only renders its report. Per-file results are persisted as they complete
(resumability); a single file's failure never aborts the batch
(design doc section 10).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.config.schema import AppConfig
from corpusqa.errors import IngestError
from corpusqa.ingest import converter
from corpusqa.ingest.discovery import (
    INDEX_DIR_NAME,
    DiscoveredFile,
    DriftKind,
    discover,
)

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

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
        stopped: True when ``should_stop`` ended the run before all files
            were handled.
    """

    parsed: int = 0
    moved: int = 0
    deleted: int = 0
    unchanged: int = 0
    duplicates: int = 0
    flagged: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False


def index_paths(corpus_root: Path) -> tuple[Path, Path, Path]:
    """Returns (index_dir, db_path, md_cache_dir) for a corpus root."""
    index_dir = corpus_root / INDEX_DIR_NAME
    return index_dir, index_dir / "catalog.db", index_dir / "md"


def _corpus_rel_path(path: Path, corpus_root: Path) -> str:
    """Returns ``path`` relative to the corpus root, as stored in the catalog."""
    try:
        return path.resolve().relative_to(corpus_root).as_posix()
    except ValueError as exc:
        raise IngestError(f"--force: {path} is outside the corpus") from exc


def run_index(
    corpus_root: Path,
    config: AppConfig,
    force: list[Path] | None = None,
    force_all: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    only: set[str] | None = None,
) -> IndexReport:
    """Runs the incremental index pipeline over a corpus directory.

    Args:
        corpus_root: Directory of source documents.
        config: Validated application configuration.
        force: Source paths to reconvert even if unchanged.
        force_all: Reconvert every file even if unchanged.
        on_progress: Called as ``(done, total)`` after each file that needed
            conversion; ``total`` counts the files needing conversion.
        should_stop: Checked between files; a true result ends the run early
            (``IndexReport.stopped``).
        only: Convert only these corpus paths (a query over chosen files);
            other files needing conversion are left for a later run. Moves
            and deletions are still recorded. None converts everything.

    Returns:
        A per-run report.

    Raises:
        IngestError: Only for corpus-level failures (unreadable root, a
            ``force`` path that is not a corpus file); per-file failures are
            captured in the report.
    """
    corpus_root = corpus_root.resolve()
    _, db_path, cache_dir = index_paths(corpus_root)
    forced = {_corpus_rel_path(p, corpus_root) for p in (force or [])}
    report = IndexReport()

    converter.check_converter_available()
    with CatalogStore(db_path) as store:
        known = store.known_files()
        found = discover(corpus_root, config.ingest.extensions, known)
        present = {i.rel_path for i in found if i.drift is not DriftKind.DELETED}
        unknown = sorted(forced - present)
        if unknown:
            raise IngestError(f"--force: not a corpus file: {', '.join(unknown)}")

        # Build the Docling converter at most once per run and reuse it for
        # every file: its model weights load on first use, so a per-file
        # converter reloads them each time. Lazy so an index with no parse
        # work (all unchanged) loads nothing.
        converter_cache: list[DocumentConverter] = []

        def get_converter() -> DocumentConverter:
            if not converter_cache:
                converter_cache.append(
                    converter.build_converter(
                        config.ingest.ocr, config.ingest.pdf_backend
                    )
                )
            return converter_cache[0]

        def is_forced(item: DiscoveredFile) -> bool:
            return force_all or item.rel_path in forced

        def needs_conversion(item: DiscoveredFile) -> bool:
            if only is not None and item.rel_path not in only:
                return False
            if item.drift in (DriftKind.NEW, DriftKind.MODIFIED):
                return True
            if item.drift is not DriftKind.UNCHANGED:
                return False
            return is_forced(item) or _cache_is_stale(cache_dir, item.file_hash)

        total = sum(1 for i in found if needs_conversion(i))
        done = 0
        for item in found:
            if should_stop is not None and should_stop():
                report.stopped = True
                break
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
            elif item.drift is DriftKind.UNCHANGED and not needs_conversion(item):
                report.unchanged += 1
            elif not needs_conversion(item):
                continue  # new or modified, but not among the chosen files
            else:  # NEW, MODIFIED, or forced
                if item.drift is DriftKind.MODIFIED:
                    _delete(store, cache_dir, known[item.rel_path])
                elif is_forced(item):
                    # Forced: the new conversion may differ under the same
                    # hash, so nothing derived from the old one may survive.
                    _delete(store, cache_dir, item.file_hash)
                else:
                    # Stale converter version: only the cache files are
                    # replaced. Cards and cached verdicts (hours of model
                    # compute) are kept; queries re-snap verdicts against
                    # the new text without calling a model.
                    _delete_cache_files(cache_dir, item.file_hash)
                _convert_one(
                    store,
                    cache_dir,
                    corpus_root,
                    item,
                    config,
                    report,
                    get_converter(),
                )
                done += 1
                if on_progress is not None:
                    on_progress(done, total)

        store.write_meta(str(corpus_root))
    return report


def _cache_is_stale(cache_dir: Path, file_hash: str) -> bool:
    """True when a file's cached conversion predates ``CONVERTER_VERSION``.

    A missing or unreadable meta file counts as stale; so does a meta without
    ``converter_version``.
    """
    try:
        meta = json.loads((cache_dir / f"{file_hash}.meta.json").read_text("utf-8"))
        version = meta.get("converter_version")
    except (OSError, ValueError, AttributeError):
        return True
    return not isinstance(version, int) or version < converter.CONVERTER_VERSION


def _delete(store: CatalogStore, cache_dir: Path, file_hash: str) -> None:
    """Removes a file's rows (cards and verdicts cascade) and cache artifacts."""
    store.delete_file(file_hash)
    _delete_cache_files(cache_dir, file_hash)


def _delete_cache_files(cache_dir: Path, file_hash: str) -> None:
    """Removes a file's cache artifacts, leaving its catalog rows alone."""
    # .chunks.jsonl: left behind by indexes built before chunking was removed.
    for suffix in (".md", ".meta.json", ".chunks.jsonl", ".pages.json"):
        (cache_dir / f"{file_hash}{suffix}").unlink(missing_ok=True)


def _convert_one(
    store: CatalogStore,
    cache_dir: Path,
    corpus_root: Path,
    item: DiscoveredFile,
    config: AppConfig,
    report: IndexReport,
    document_converter: DocumentConverter | None = None,
) -> None:
    """Converts, caches, and stores one file."""
    source = corpus_root / item.rel_path
    result = converter.convert(
        source,
        item.file_hash,
        cache_dir,
        config.ingest.ocr,
        pdf_backend=config.ingest.pdf_backend,
        document_converter=document_converter,
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
    """

    meta: dict[str, object] | None
    drift: list[DiscoveredFile]
    duplicates: list[DiscoveredFile]
    flagged: list[FileRow]
    file_count: int


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
        )
