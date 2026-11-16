"""Page map for .docx sources, built from ``vp_*`` bookmarks."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from corpusqa.ingest.docx_pages import build_docx_page_map, docx_page_marks


def make_docx(tmp_path: Path, items: list[str]) -> Path:
    """Builds a docx; ``BM:<name>`` items become bookmarks, others paragraphs."""
    doc = Document()
    for n, item in enumerate(items):
        if item.startswith("BM:"):
            para = doc.add_paragraph()
            start = OxmlElement("w:bookmarkStart")
            start.set(qn("w:id"), str(n))
            start.set(qn("w:name"), item[3:])
            para._p.append(start)
            end = OxmlElement("w:bookmarkEnd")
            end.set(qn("w:id"), str(n))
            para._p.append(end)
        else:
            doc.add_paragraph(item)
    path = tmp_path / "t.docx"
    doc.save(str(path))
    return path


ITEMS = [
    "Title page text.",
    "BM:vp_10_2",
    "a palpable subversion",
    "BM:vp_10_3",
    "of the New Institution.",
]


def test_marks_follow_bookmarks_in_order(tmp_path: Path) -> None:
    src = make_docx(tmp_path, ITEMS)
    labels = [label for label, _ in docx_page_marks(src)]
    assert labels == ["2", "3"]


def test_marks_text_crosses_paragraphs_and_ignores_other_bookmarks(
    tmp_path: Path,
) -> None:
    src = make_docx(tmp_path, ["BM:art_5", "x", "BM:vp_10_2", "one", "two"])
    marks = docx_page_marks(src)
    assert marks == [("2", "one two")]


def test_page_map_anchors_mid_sentence_and_defaults_page_one(tmp_path: Path) -> None:
    src = make_docx(tmp_path, ITEMS)
    md = "Title page text.\n\na palpable subversion of the New Institution."
    pm = build_docx_page_map(src, md)
    assert pm[0] == (0, "1")
    assert md[pm[2][0] :].startswith("of the New Institution")
    assert [label for _, label in pm] == ["1", "2", "3"]


def test_anchoring_tolerates_markdown_escaping(tmp_path: Path) -> None:
    src = make_docx(tmp_path, ["intro", "BM:vp_10_2", "snake_case “quoted” word"])
    md = 'intro\n\nsnake\\_case "quoted" word'
    pm = build_docx_page_map(src, md)
    assert [label for _, label in pm] == ["1", "2"]
    assert md[pm[1][0] :].startswith("snake")


def test_roman_and_suffix_labels_kept(tmp_path: Path) -> None:
    src = make_docx(
        tmp_path, ["BM:vp_10_iv", "preface words here", "BM:vp_10_1a", "more words"]
    )
    labels = [label for label, _ in docx_page_marks(src)]
    assert labels == ["iv", "1a"]


def test_unplaceable_bookmark_is_skipped(tmp_path: Path) -> None:
    src = make_docx(
        tmp_path,
        [
            "intro",
            "BM:vp_10_2",
            "nowhere to be found",
            "BM:vp_10_3",
            "tail of the document text",
        ],
    )
    pm = build_docx_page_map(src, "intro\n\ntail of the document text")
    assert [label for _, label in pm] == ["1", "3"]


def test_docx_without_bookmarks_has_no_page_map(tmp_path: Path) -> None:
    src = make_docx(tmp_path, ["plain", "text"])
    assert build_docx_page_map(src, "text") == []


def test_anchoring_ignores_spacing_and_table_pipes(tmp_path: Path) -> None:
    src = make_docx(
        tmp_path,
        [
            "intro",
            "BM:vp_10_2",
            "Books in Supply   Carriage included.",
            "Nett Price here",
        ],
    )
    md = "intro\n\n| Books in Supply | Carriage  included. | Nett Price here |"
    pm = build_docx_page_map(src, md)
    assert [label for _, label in pm] == ["1", "2"]
    assert md[pm[1][0] :].startswith("Books")


def test_far_ahead_boilerplate_match_does_not_derail_later_marks(
    tmp_path: Path,
) -> None:
    boiler = "Books in Supply carriage included nett price list for all readers, " * 2
    src = make_docx(
        tmp_path,
        [
            "intro",
            "BM:vp_10_2",
            boiler,
            "BM:vp_10_3",
            "third page text is right here in the markdown",
            "BM:vp_10_4",
            "fourth page text is also right here in the markdown",
        ],
    )
    filler = "lorem ipsum dolor " * 9000  # well past any anchor window
    md = (
        "intro\n\nthird page text is right here in the markdown\n\n"
        "fourth page text is also right here in the markdown\n\n"
        f"{filler}\n\n{boiler}"
    )
    pm = build_docx_page_map(src, md)
    assert [label for _, label in pm] == ["1", "3", "4"]
    assert md[pm[1][0] :].startswith("third page")
