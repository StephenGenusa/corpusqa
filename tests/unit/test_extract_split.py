"""Markdown sectioning for oversize files (M3)."""

from __future__ import annotations

from corpusqa.splitting import split_markdown


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
    # the short intro packs in with S1; S2 cannot join them
    assert parts[0].startswith("# Top") and "## S1" in parts[0]
    assert parts[1].startswith("## S2")


def test_headingless_text_hard_splits() -> None:
    parts = split_markdown("z" * 250, 100)
    assert [len(p) for p in parts] == [100, 100, 50]


def test_packs_small_sections_up_to_the_budget() -> None:
    # Docling turns running page headers into headings, so a book can have a
    # heading every page; splitting at each one leaves the window mostly empty.
    md = "".join(f"## H{i}\n" + "w" * 20 + "\n" for i in range(10))  # 26 chars each
    parts = split_markdown(md, 100)
    assert len(parts) == 4  # 3 + 3 + 3 + 1
    assert all(len(p) <= 100 for p in parts)


def test_packing_keeps_text_verbatim_and_in_order() -> None:
    md = "# A\n" + "a" * 30 + "\n## A1\n" + "b" * 70 + "\n# B\n" + "c" * 150
    parts = split_markdown(md, 100)
    assert "".join(parts) == md
    assert all(len(p) <= 100 for p in parts)
