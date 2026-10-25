"""Docling conversion to cached markdown.

Wraps Docling so its API churn is localized here (design doc risk #2;
verified against docling 2.101). Per-file failures never raise -- they are
captured as ``parse_status`` in the result. Legacy ``.doc`` is converted via
LibreOffice headless when a ``soffice`` binary is on PATH, else flagged
``unsupported_format``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpusqa.errors import IngestError

if TYPE_CHECKING:  # docling is heavy; imported lazily at call time
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc.document import DoclingDocument

ZERO_TEXT_THRESHOLD = 200
_SOFFICE_TIMEOUT_S = 120
# Leading chars of an item's text used to locate it in the exported markdown
# when rebuilding the page map; long enough to be unique, short enough to
# survive minor export reflow.
_PAGE_ANCHOR_CHARS = 40
# A table is placed by its first cell at least this long: shorter cells
# ("AM", "4") match too much of the text ahead to locate the table.
_MIN_TABLE_ANCHOR_CHARS = 8
# Running-header demotion: fewer recognized page headers than this teaches
# nothing about where a document's headers sit.
_MIN_PAGE_HEADERS = 3
# Slack around the learned header band (fraction of page height) and text
# size (points), so a header a hair lower or larger than the rest still counts.
_HEADER_BAND_SLACK = 0.01
_HEADER_SIZE_SLACK_PT = 0.5
_log = logging.getLogger("corpusqa.ingest")

STATUS_OK = "ok"
STATUS_ZERO_TEXT = "zero_text"
STATUS_PARSE_FAILED = "parse_failed"
STATUS_UNSUPPORTED = "unsupported_format"
STATUS_PARTIAL = "partial_parse"


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of converting one source file.

    Attributes:
        file_hash: Identity of the source file.
        md_cache_path: Path of the cached markdown, when conversion succeeded.
        parse_status: One of the ``STATUS_*`` constants.
        parse_error: Captured error text when status is not ``ok``.
        page_count: Source page count when the format is paginated, else None.
    """

    file_hash: str
    md_cache_path: Path | None
    parse_status: str
    parse_error: str | None
    page_count: int | None


def check_converter_available() -> None:
    """Fails fast when the conversion stack is broken.

    A broken Docling install (e.g. a dependency-resolution drift in
    transformers/tokenizers) would otherwise surface as one parse_failed
    flag per file instead of one clear abort.

    Raises:
        IngestError: With the underlying import error.
    """
    try:
        import docling.document_converter  # noqa: F401
    except Exception as exc:  # noqa: BLE001 -- any import failure aborts
        raise IngestError(
            f"document converter unavailable ({type(exc).__name__}: {exc}); "
            "check the docling install (try: uv pip install -U docling)"
        ) from exc


def classify_text(extracted_chars: int, page_count: int | None) -> str:
    """Applies the zero-text rule (design doc section 4.1).

    A paginated document with almost no extractable text is most likely a
    scanned image PDF; it is flagged rather than silently cataloged as empty.

    Args:
        extracted_chars: Length of the stripped markdown export.
        page_count: Source page count, when paginated.

    Returns:
        ``STATUS_ZERO_TEXT`` or ``STATUS_OK``.
    """
    if page_count and page_count > 0 and extracted_chars < ZERO_TEXT_THRESHOLD:
        return STATUS_ZERO_TEXT
    return STATUS_OK


def build_converter(ocr: str, pdf_backend: str) -> DocumentConverter:
    """Constructs a Docling converter honoring OCR and backend settings.

    Public so a batch run can build ONE converter and reuse it across files:
    Docling loads its layout/table model weights on the converter's first
    conversion, so a fresh converter per file reloads them every time (the
    "Loading weights" line repeating per document). One shared instance loads
    once.

    ``pypdfium`` is the lower-memory PDF backend; it is the documented
    fallback when the default backend hits native allocation failures
    (std::bad_alloc) on large or complex PDFs.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pdf_options = PdfPipelineOptions()
    # NB: "auto" currently behaves identically to "on" -- there is no
    # per-document detection of whether OCR is needed; both enable it. Kept
    # as a distinct value for forward compatibility and config stability.
    pdf_options.do_ocr = ocr in ("on", "auto")
    pdf_kwargs: dict[str, Any] = {"pipeline_options": pdf_options}
    if pdf_backend == "pypdfium":
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        pdf_kwargs["backend"] = PyPdfiumDocumentBackend
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(**pdf_kwargs)}
    )


# Backward-compatible alias (older call sites/tests may reference the private
# name); both build a fresh converter.
_build_converter = build_converter


def interpret_status(
    docling_status: str, errors: list[str], extracted_chars: int, page_count: int | None
) -> tuple[str, str | None]:
    """Maps a Docling conversion status to corpusqa flags (pure, testable).

    Args:
        docling_status: ``ConversionStatus.value`` from the result.
        errors: Rendered error strings from ``result.errors``.
        extracted_chars: Length of the stripped markdown export.
        page_count: Source page count, when paginated.

    Returns:
        ``(parse_status, parse_error)``. ``partial_success`` maps to
        ``partial_parse`` with the page failures preserved -- a partially
        parsed file is flagged, never silently treated as complete
        (design doc section 4.1: flag visibility is part of provenance).
    """
    if docling_status == "partial_success":
        detail = "; ".join(errors[:10]) if errors else "unreported page failures"
        more = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
        return STATUS_PARTIAL, f"partial conversion: {detail}{more}"
    if docling_status not in ("success",):
        return STATUS_PARSE_FAILED, f"docling status {docling_status}: " + "; ".join(
            errors[:5]
        )
    return classify_text(extracted_chars, page_count), None


def _doc_via_soffice(source: Path, scratch: Path) -> Path:
    """Converts a legacy .doc to .docx via LibreOffice headless.

    Args:
        source: The .doc file.
        scratch: Temp directory for the converted output.

    Returns:
        Path of the produced .docx.

    Raises:
        RuntimeError: If soffice is missing or conversion fails.
    """
    soffice = shutil.which("soffice")
    if soffice is None:
        raise RuntimeError("legacy .doc requires LibreOffice ('soffice' not on PATH)")
    proc = subprocess.run(  # noqa: S603 -- fixed binary, no shell
        [
            soffice,
            "--headless",
            "--convert-to",
            "docx",
            "--outdir",
            str(scratch),
            str(source),
        ],
        capture_output=True,
        timeout=_SOFFICE_TIMEOUT_S,
        check=False,
    )
    produced = scratch / (source.stem + ".docx")
    if proc.returncode != 0 or not produced.exists():
        raise RuntimeError(
            f"soffice conversion failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:300]}"
        )
    return produced


def demote_running_headers(document: DoclingDocument) -> int:
    """Moves running page headers that Docling labelled as headings out of the body.

    Docling recognizes most running headers (``page_header``, which the
    markdown export leaves out) but labels some as section headings. Those
    export as ``##`` lines that land mid-sentence, give quotes the wrong
    section label and break quotes across page breaks. Such a heading sits
    where this document's own page headers sit, at a page-header text size,
    so both are learned from the recognized headers: a heading whose box ends
    within their band and whose height is within their range is moved to the
    furniture layer, as Docling does with page headers. Real headings in the
    band are set larger and stay.

    Returns:
        How many headings were demoted (0 when the document has too few
        recognized page headers to learn from).
    """
    from docling_core.types.doc.document import ContentLayer
    from docling_core.types.doc.labels import DocItemLabel

    def geometry(item: Any) -> tuple[float, float] | None:  # noqa: ANN401 -- docling item
        """Returns (box bottom as a fraction of page height, box height)."""
        if not item.prov:
            return None
        prov = item.prov[0]
        page = document.pages.get(prov.page_no)
        if page is None or not page.size.height:
            return None
        box = prov.bbox.to_top_left_origin(page.size.height)
        return box.b / page.size.height, box.height

    headers = [
        g
        for t in document.texts
        if t.label == DocItemLabel.PAGE_HEADER and (g := geometry(t)) is not None
    ]
    if len(headers) < _MIN_PAGE_HEADERS:
        return 0
    band = max(bottom for bottom, _ in headers) + _HEADER_BAND_SLACK
    low = min(h for _, h in headers) - _HEADER_SIZE_SLACK_PT
    high = max(h for _, h in headers) + _HEADER_SIZE_SLACK_PT
    demoted = 0
    for item in document.texts:
        if item.label != DocItemLabel.SECTION_HEADER:
            continue
        g = geometry(item)
        if g is not None and g[0] <= band and low <= g[1] <= high:
            item.content_layer = ContentLayer.FURNITURE
            demoted += 1
    return demoted


def _table_anchor(item: Any) -> str | None:  # noqa: ANN401 -- docling item
    """Returns a table's first cell text long enough to locate, if any."""
    cells = getattr(getattr(item, "data", None), "table_cells", None) or []
    for cell in cells:
        text = (getattr(cell, "text", "") or "").strip()
        if len(text) >= _MIN_TABLE_ANCHOR_CHARS:
            return text
    return None


def _build_page_map(document: DoclingDocument, markdown: str) -> list[tuple[int, int]]:
    """Builds sorted ``(markdown_offset, page_no)`` marks from docling provenance.

    ``export_to_markdown`` discards the per-item page numbers docling records,
    so we recover them: walk items in document order, locate each item's text in
    the exported markdown at or after the previous match, and emit a mark each
    time the page changes. A table, which has no text of its own, is located by
    its first cell long enough to place, so a page holding only a table still
    gets its mark. Pages run in order, so one locatable item per page is
    enough -- items not found ahead of the cursor (reflowed text, or a repeated
    running header whose only match lies behind) are skipped, which keeps the
    marks in page order. Empty for sources with no page provenance (md/txt) or
    scanned PDFs without a text layer.
    """
    marks: list[tuple[int, int]] = []
    cursor = 0
    last_page: int | None = None
    try:
        items = list(document.iterate_items())
    except Exception as exc:  # noqa: BLE001 -- provenance is best-effort
        _log.warning("page numbers unavailable: reading provenance failed: %s", exc)
        return marks
    for entry in items:
        item = entry[0] if isinstance(entry, tuple) else entry
        text = getattr(item, "text", None) or _table_anchor(item)
        prov = getattr(item, "prov", None)
        if not text or not prov:
            continue
        page = getattr(prov[0], "page_no", None)
        if page is None:
            continue
        anchor = text.strip()[:_PAGE_ANCHOR_CHARS]
        if not anchor:
            continue
        pos = markdown.find(anchor, cursor)
        if pos < 0:
            continue
        cursor = pos + len(anchor)
        if page != last_page:
            marks.append((pos, page))
            last_page = page
    return marks


def convert(
    source: Path,
    file_hash: str,
    cache_dir: Path,
    ocr: str,
    pdf_backend: str = "docling_parse",
    document_converter: DocumentConverter | None = None,
) -> ConversionResult:
    """Converts one file to markdown in the parse cache.

    Writes ``<hash>.md`` and ``<hash>.meta.json`` into ``cache_dir`` on
    success. Markdown is written even for ``zero_text`` files (whatever
    little was extracted), but the flag is preserved so queries can warn.

    Args:
        source: Absolute path of the source file.
        file_hash: Precomputed SHA-256 identity (names the cache files).
        cache_dir: Parse-cache directory (``<index>/md``).
        ocr: ``off | auto | on`` per ingest config.
        pdf_backend: ``docling_parse | pypdfium`` per ingest config.
        document_converter: A prebuilt converter to reuse. When None, one is
            built for this single call -- fine for one-off conversions, but a
            batch should build once (``build_converter``) and pass it in to
            avoid reloading model weights per file.

    Returns:
        The conversion outcome; never raises for per-file failures.
        Partial conversions (some pages failed natively) are flagged
        ``partial_parse``; the extracted portion is still cached.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = source
            if suffix == ".doc":
                target = _doc_via_soffice(source, Path(tmp))
            active = document_converter or build_converter(ocr, pdf_backend)
            result = active.convert(str(target), raises_on_error=False)
            document: DoclingDocument = result.document
            demoted = demote_running_headers(document)
            markdown = document.export_to_markdown()
            page_count = len(document.pages) if document.pages else None
            docling_status = str(result.status.value)
            error_strings = [
                f"{e.module_name}: {e.error_message}" for e in result.errors
            ]
    except Exception as exc:  # noqa: BLE001 -- per-file capture by design
        status = (
            STATUS_UNSUPPORTED
            if suffix == ".doc" and "soffice" in str(exc).lower()
            else STATUS_PARSE_FAILED
        )
        return ConversionResult(
            file_hash=file_hash,
            md_cache_path=None,
            parse_status=status,
            parse_error=f"{type(exc).__name__}: {exc}",
            page_count=None,
        )

    parse_status, parse_error = interpret_status(
        docling_status, error_strings, len(markdown.strip()), page_count
    )
    if demoted:
        _log.info(
            "%s: %d running page headers demoted from headings", source.name, demoted
        )
    md_path = cache_dir / f"{file_hash}.md"
    md_path.write_text(markdown, encoding="utf-8")
    page_map = _build_page_map(document, markdown)
    if page_map:
        (cache_dir / f"{file_hash}.pages.json").write_text(
            json.dumps(page_map), encoding="utf-8"
        )
    meta = {
        "file_hash": file_hash,
        "source_name": source.name,
        "page_count": page_count,
        "extracted_chars": len(markdown.strip()),
        "docling_status": docling_status,
        "errors": error_strings,
        "running_headers_demoted": demoted,
    }
    (cache_dir / f"{file_hash}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return ConversionResult(
        file_hash=file_hash,
        md_cache_path=md_path,
        parse_status=parse_status,
        parse_error=parse_error,
        page_count=page_count,
    )
