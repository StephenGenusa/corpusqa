"""Verified-quote snapping and page-number recovery (items 2/3/4)."""

from __future__ import annotations

import asyncio
import json
import types

from corpusqa.config.schema import TaskName
from corpusqa.ingest.converter import _build_page_map
from corpusqa.query.extractor import (
    _SNAP_SEPARATOR,
    _match_spans,
    _normalize_for_match,
    _page_at,
    extract_file,
)

_SOURCE = (
    "# Chapter 3\n"
    "Sin in the flesh is a physical principle of the animal nature, defiling "
    "and corrupting the whole man, and bringing forth death in the body.\n"
    "Unrelated trailing text.\n"
)


def _spans(quote: str, source: str = _SOURCE):
    norm, smap = _normalize_for_match(source)
    return _match_spans(quote, norm, smap)


def test_noisy_quote_snaps_to_exact_source() -> None:
    # Wrong case, smart-ish punctuation, and an extra word at each edge.
    noisy = "ZZZ a PHYSICAL principle of the animal nature, defiling and corrupting the whole man and"
    spans = _spans(noisy)
    assert spans
    snapped = _SNAP_SEPARATOR.join(_SOURCE[s:e] for s, e in spans)
    assert snapped in _SOURCE  # display text is verbatim from the file
    assert "PHYSICAL" not in snapped  # the model's casing is discarded


def test_ellipsis_quote_joins_exact_spans() -> None:
    quote = "a physical principle of the animal nature ... bringing forth death in the body"
    spans = _spans(quote)
    assert len(spans) == 2
    joined = _SNAP_SEPARATOR.join(_SOURCE[s:e] for s, e in spans)
    assert "…" in joined
    for s, e in spans:
        assert _SOURCE[s:e] in _SOURCE


def test_paraphrase_does_not_verify() -> None:
    assert not _spans("a bodily law that pollutes and ruins all of humankind")


def test_page_at_bisects() -> None:
    pm = [(0, 1), (80, 2), (200, 5)]
    assert _page_at(pm, 0) == 1
    assert _page_at(pm, 79) == 1
    assert _page_at(pm, 80) == 2
    assert _page_at(pm, 250) == 5
    assert _page_at(None, 10) is None
    assert _page_at([], 10) is None


def test_build_page_map_marks_page_changes() -> None:
    def item(text: str, page: int):
        return types.SimpleNamespace(
            text=text, prov=[types.SimpleNamespace(page_no=page)]
        )

    md = (
        "The orchard pest plan begins here with the program scope.\n\n"
        "Dormant oil is applied before bud break to smother mites.\n\n"
        "Harvest interval restrictions are listed in the appendix here.\n"
    )
    doc = types.SimpleNamespace(
        iterate_items=lambda: [
            (item("The orchard pest plan begins here with the program scope.", 1), 0),
            (item("Dormant oil is applied before bud break to smother mites.", 2), 0),
            (item("Harvest interval restrictions are listed in the appendix here.", 3), 0),
        ]
    )
    marks = _build_page_map(doc, md)
    assert [p for _, p in marks] == [1, 2, 3]
    for off, _page in marks:
        assert 0 <= off < len(md)


def test_extract_file_stamps_real_heading_and_page() -> None:
    quote = "a physical principle of the animal nature, defiling and corrupting the whole man"

    class FakeClient:
        def count_tokens(self, task: TaskName, text: str) -> int:
            return max(1, len(text) // 4)

        async def complete(self, task, messages, **kw):
            payload = {
                "file_hash": "h",
                "relevant": True,
                "confidence": 0.7,
                "reasoning": "r",
                "findings": [
                    {
                        "claim": "why it matched",
                        "quote": quote,
                        "heading_path": ["MODEL JUNK"],  # must be discarded
                        "page_no": 99,  # must be discarded
                        "quote_verified": True,
                    }
                ],
            }
            return types.SimpleNamespace(
                text=json.dumps(payload), tokens_in=1, tokens_out=1, finish_reason="stop"
            )

    config = types.SimpleNamespace(
        tasks={
            TaskName.EXTRACT: types.SimpleNamespace(
                context_window=8000, max_tokens=512, model="m"
            )
        }
    )
    result = asyncio.run(
        extract_file(
            "q",
            None,
            "f.pdf",
            "h",
            _SOURCE,
            FakeClient(),  # type: ignore[arg-type]
            config,  # type: ignore[arg-type]
            sections=[_SOURCE],
            page_map=[(0, 7)],
        )
    )
    finding = result.findings[0]
    assert finding.quote_verified
    assert finding.quote in _SOURCE  # snapped to source
    assert finding.heading_path == ["Chapter 3"]  # derived, not "MODEL JUNK"
    assert finding.page_no == 7  # from page map, not the model's 99
