"""Running page headers that Docling labels as headings are demoted.

Docling recognizes most running headers (label ``page_header``, kept out of
the markdown), but labels some as ``section_header``. Exported as ``##``
headings they land mid-sentence, give quotes the wrong section label and
break quotes that cross a page break. Such a heading sits where this
document's own page headers sit, at a page-header text size; it is demoted
to a page header. Real headings in that band are larger and stay.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
)

from corpusqa.ingest import converter

PAGE_H = 800.0


def _prov(page: int, top: float, height: float, text: str) -> ProvenanceItem:
    """Provenance for a box whose top edge is ``top`` points below the page top."""
    return ProvenanceItem(
        page_no=page,
        bbox=BoundingBox(
            l=50,
            t=PAGE_H - top,
            r=400,
            b=PAGE_H - top - height,
            coord_origin=CoordOrigin.BOTTOMLEFT,
        ),
        charspan=(0, len(text)),
    )


def _book() -> DoclingDocument:
    doc = DoclingDocument(name="book")
    for page in (1, 2, 3):
        doc.add_page(page_no=page, size=Size(width=600, height=PAGE_H))
        doc.add_text(
            label=DocItemLabel.PAGE_HEADER,
            text="RUNNING TITLE",
            prov=_prov(page, 70, 5, "RUNNING TITLE"),
        )
    # page 1: a real chapter title in the header band, at heading size
    doc.add_heading("PREFACE TO FIRST EDITION", prov=_prov(1, 72, 9, "x"))
    doc.add_text(
        label=DocItemLabel.TEXT,
        text="the varieties of",
        prov=_prov(1, 700, 5, "x"),
    )
    # page 2: a running header misfiled as a heading, splitting a sentence
    doc.add_heading("SIN IN THE FLESH", prov=_prov(2, 70, 5, "x"))
    doc.add_text(
        label=DocItemLabel.TEXT,
        text="Protestantism, are all the result",
        prov=_prov(2, 120, 5, "x"),
    )
    # page 3: a real heading lower on the page, even at a small size
    doc.add_heading("Small Subheading", prov=_prov(3, 400, 5, "x"))
    doc.add_text(label=DocItemLabel.TEXT, text="More text.", prov=_prov(3, 420, 5, "x"))
    return doc


def test_running_header_labelled_as_heading_is_demoted() -> None:
    doc = _book()
    assert converter.demote_running_headers(doc) == 1
    markdown = doc.export_to_markdown()
    assert "SIN IN THE FLESH" not in markdown
    assert "the varieties of\n\nProtestantism" in markdown


def test_real_headings_stay() -> None:
    doc = _book()
    converter.demote_running_headers(doc)
    markdown = doc.export_to_markdown()
    assert "## PREFACE TO FIRST EDITION" in markdown  # in the band, but larger
    assert "## Small Subheading" in markdown  # header-sized, but not in the band


def test_document_without_page_headers_is_left_alone() -> None:
    doc = DoclingDocument(name="memo")
    doc.add_page(page_no=1, size=Size(width=600, height=PAGE_H))
    doc.add_heading("Title", prov=_prov(1, 70, 5, "x"))
    assert converter.demote_running_headers(doc) == 0
    assert "## Title" in doc.export_to_markdown()


def test_convert_records_demoted_headers_in_meta(tmp_path: Path) -> None:
    result = SimpleNamespace(
        document=_book(), status=SimpleNamespace(value="success"), errors=[]
    )
    fake = SimpleNamespace(convert=lambda *_a, **_k: result)
    converter.convert(
        tmp_path / "book.pdf",
        "h" * 64,
        tmp_path / "md",
        "off",
        document_converter=fake,  # type: ignore[arg-type]
    )
    meta = json.loads((tmp_path / "md" / f"{'h' * 64}.meta.json").read_text())
    assert meta["running_headers_demoted"] == 1
