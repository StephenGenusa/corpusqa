"""Page map for ``.docx`` sources, recovered from ``vp_*`` bookmarks.

Docling's docx backend records no page provenance (Word has no fixed
pagination), but the corpus's volumes carry ``vp_<volume>_<page>`` bookmarks
marking where each printed page *starts*, often mid-sentence. This module
reads those bookmarks straight from ``word/document.xml`` and anchors each to
an offset in the exported markdown by locating the text that follows it.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from defusedxml.ElementTree import fromstring as safe_fromstring

from corpusqa.query.extractor import PageMap, _normalize_for_match

_log = logging.getLogger("corpusqa.ingest")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_BOOKMARK_RE = re.compile(r"^vp_[^_]+_(.+)$")
# Run text kept after each bookmark to anchor it in the markdown.
_MARK_TEXT_CHARS = 80
# Anchor prefixes tried longest first: docling may drop or reflow text late in
# the 80 characters, so a shorter prefix can still place the mark. Shorter than
# the floor matches too much of the text ahead to be trusted.
_ANCHOR_LENGTHS = (80, 40, 20)
# Docling joins, splits and re-spaces paragraphs and dashes differently from
# the XML, so matching ignores spaces and table pipes altogether (lengths above
# are counted in those characters). The shortest prefixes recur in a volume, so they are
# trusted only near the cursor: a page is a few thousand characters. Even the
# long prefixes are bounded: boilerplate (ad blocks) recurs every issue, and one
# false far-ahead match would push the cursor past every true mark in between.
_SHORT_ANCHOR_WINDOW = 30_000
_LONG_ANCHOR_WINDOW = 100_000


def docx_page_marks(source: Path) -> list[tuple[str, str]]:
    """Reads page-start bookmarks from a ``.docx`` in document order.

    Args:
        source: Path of the ``.docx`` file.

    Returns:
        ``(page_label, text_after_bookmark)`` for each bookmark named
        ``vp_<volume>_<label>``; the text is the following ~80 characters of
        run text, crossing paragraph boundaries (paragraphs are joined by a
        space). Other bookmarks are ignored.

    Raises:
        defusedxml.DefusedXmlException: The XML declares a DTD or entities.
    """
    with zipfile.ZipFile(source) as zf:
        # The file is untrusted input: defusedxml refuses DTDs and entity
        # declarations (entity-expansion bombs, external entities).
        root = safe_fromstring(zf.read("word/document.xml"))
    parts: list[str] = []
    length = 0
    marks: list[tuple[str, int]] = []  # (label, offset into the joined text)
    for el in root.iter():
        tag = el.tag
        if tag == f"{{{_W}}}bookmarkStart":
            m = _BOOKMARK_RE.match(el.get(f"{{{_W}}}name", ""))
            if m:
                marks.append((m.group(1), length))
            continue
        if tag == f"{{{_W}}}t":
            piece = el.text or ""
        elif tag in (f"{{{_W}}}p", f"{{{_W}}}tab", f"{{{_W}}}br"):
            piece = " "
        else:
            continue
        parts.append(piece)
        length += len(piece)
    text = "".join(parts)
    return [(label, text[pos:].lstrip()[:_MARK_TEXT_CHARS]) for label, pos in marks]


def build_docx_page_map(source: Path, markdown: str) -> PageMap:
    """Anchors a ``.docx``'s page bookmarks into its exported markdown.

    Text before the first bookmark is page 1, so the map starts ``(0, "1")``.
    Each bookmark is located by its following text at or after the previous
    mark, matched after the extractor's normalization so markdown escaping and
    quote folding do not break the match. Bookmarks that cannot be placed are
    skipped and counted in a warning.

    Args:
        source: Path of the ``.docx`` file.
        markdown: Docling's markdown export of that file.

    Returns:
        Sorted ``(markdown_offset, page_label)`` marks; empty when the document
        has no ``vp_`` bookmarks. A span whose following bookmarks could not be
        placed is labelled as a range ("22\u201323"): its text may lie on any
        of those pages, and a label must never claim a page it may not be on.
    """
    marks = docx_page_marks(source)
    if not marks:
        return []
    normalized, index_map = _compact(markdown)
    # [offset, first_label, last_label]: last_label moves past the first when
    # the marks right after this one could not be placed, so the span reads as
    # a range instead of silently inheriting this page's label alone.
    spans: list[list[int | str]] = [[0, "1", "1"]]
    cursor = 0  # position in normalized space
    unplaced: list[str] = []
    max_jump = 0
    for label, text in marks:
        pos = _locate(text, normalized, cursor)
        if pos < 0:
            unplaced.append(label)
            spans[-1][2] = label
            continue
        max_jump = max(max_jump, pos - cursor)
        cursor = pos
        offset = index_map[pos]
        offset = max(offset, int(spans[-1][0]))
        spans.append([offset, label, label])
    if unplaced:
        _log.warning(
            "%s: %d of %d page bookmarks could not be placed in the markdown "
            "(largest cursor jump %d chars; unplaced: %s)",
            source.name,
            len(unplaced),
            len(marks),
            max_jump,
            ", ".join(unplaced[:40]) + (" ..." if len(unplaced) > 40 else ""),
        )
    _log.debug("%s: largest page-anchor cursor jump %d chars", source.name, max_jump)
    return [
        (int(off), first if first == last else f"{first}\u2013{last}")
        for off, first, last in ((o, str(f), str(la)) for o, f, la in spans)
    ]


def _compact(text: str) -> tuple[str, list[int]]:
    """Normalizes ``text``, drops spaces and table pipes, maps to original offsets."""
    norm, idx = _normalize_for_match(text)
    kept = [k for k, ch in enumerate(norm) if ch not in " |"]
    return "".join(norm[k] for k in kept), [idx[k] for k in kept]


def _locate(text: str, compact: str, cursor: int) -> int:
    """Finds ``text`` in compacted markdown at or after ``cursor``, or -1."""
    anchor = _compact(text)[0]
    if len(anchor) < _ANCHOR_LENGTHS[-1]:
        return -1
    for n in _ANCHOR_LENGTHS:
        short = n == _ANCHOR_LENGTHS[-1]
        end = cursor + (_SHORT_ANCHOR_WINDOW if short else _LONG_ANCHOR_WINDOW)
        pos = compact.find(anchor[:n], cursor, end)
        if pos >= 0:
            return pos
    return -1
