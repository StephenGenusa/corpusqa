"""Markdown sectioning for oversize files (M3)."""

from __future__ import annotations

from corpusqa.query.extractor import split_markdown


def test_small_text_is_one_section() -> None:
    assert split_markdown("short", 100) == ["short"]


def test_splits_at_shallowest_heading() -> None:
    md = "# A\n" + "a" * 80 + "\n# B\n" + "b" * 80
    parts = split_markdown(md, 100)
    assert len(parts) == 2
    assert parts[0].startswith("# A") and parts[1].startswith("# B")


def test_recurses_into_subheadings() -> None:
    md = "# Top\nintro\n## S1\n" + "x" * 90 + "\n## S2\n" + "y" * 90
    parts = split_markdown(md, 120)
    assert any(p.startswith("## S1") for p in parts)
    assert any(p.startswith("## S2") for p in parts)


def test_headingless_text_hard_splits() -> None:
    parts = split_markdown("z" * 250, 100)
    assert [len(p) for p in parts] == [100, 100, 50]
