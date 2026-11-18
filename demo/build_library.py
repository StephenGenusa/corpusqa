"""Builds the demo library: demo/books/*.md -> .docx with page numbers.

Usage: python demo/build_library.py OUT_DIR

Each book becomes ``OUT_DIR/<Title>.docx``. Headings keep their level,
``*italic*`` and ``**bold**`` become real formatting, and a ``vp_<book>_<page>``
bookmark starts a new "page" about every WORDS_PER_PAGE words -- the same
bookmarks the real corpus volumes carry, so hits show page numbers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

BOOKS = Path(__file__).resolve().parent / "books"
WORDS_PER_PAGE = 280
_RUNS = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")
_UNSAFE = re.compile(r'[<>:"/\\|?*]')


def _bookmark(paragraph: Paragraph, name: str, bookmark_id: int) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.insert(0, end)
    paragraph._p.insert(0, start)


def _add_text(paragraph: Paragraph, text: str) -> None:
    for part in _RUNS.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def build(source: Path, out_dir: Path, book_number: int) -> Path:
    """Converts one Markdown book; returns the .docx path."""
    blocks = [b.strip() for b in source.read_text(encoding="utf-8").split("\n\n")]
    doc = Document()
    title = source.stem
    page = 1
    words_on_page = 0
    first = True
    for block in filter(None, blocks):
        heading = re.match(r"^(#{1,3}) (.*)$", block)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            if level == 1:
                title = text
            paragraph = doc.add_heading(level=level)
            _add_text(paragraph, text)
        else:
            paragraph = doc.add_paragraph()
            _add_text(paragraph, " ".join(block.split()))
        if first or words_on_page >= WORDS_PER_PAGE:
            if not first:
                page += 1
            _bookmark(paragraph, f"vp_{book_number}_{page}", page)
            words_on_page = 0
            first = False
        words_on_page += len(block.split())
    out = out_dir / f"{_UNSAFE.sub('', title.split(':')[0]).strip()}.docx"
    doc.save(str(out))
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(BOOKS.glob("*.md")):
        number = int(source.name.split("-", 1)[0])
        out = build(source, out_dir, number)
        print(f"{out.name}  ({len(source.read_text(encoding='utf-8').split())} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
