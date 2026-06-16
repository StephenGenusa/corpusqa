"""Drift classification tests (M2 acceptance)."""

from __future__ import annotations

from pathlib import Path

from corpusqa.ingest.discovery import DriftKind, discover, hash_file


def _setup(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("bravo", encoding="utf-8")
    (root / "ignore.xyz").write_text("nope", encoding="utf-8")
    return root


EXT = [".md", ".txt"]


def test_fresh_corpus_is_all_new(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    found = discover(root, EXT, known={})
    assert {f.rel_path for f in found} == {"a.md", "sub/b.txt"}
    assert all(f.drift is DriftKind.NEW for f in found)


def test_index_dir_and_extensions_excluded(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    (root / ".corpusqa").mkdir()
    (root / ".corpusqa" / "stray.md").write_text("x", encoding="utf-8")
    names = {f.rel_path for f in discover(root, EXT, known={})}
    assert "ignore.xyz" not in names
    assert not any(".corpusqa" in n for n in names)


def test_unchanged_modified_moved_deleted(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    known = {f.rel_path: f.file_hash for f in discover(root, EXT, known={})}

    (root / "a.md").write_text("# A\nalpha CHANGED", encoding="utf-8")  # modified
    (root / "sub" / "b.txt").rename(root / "b_moved.txt")  # moved
    (root / "c.md").write_text("new file", encoding="utf-8")  # new
    known["ghost.md"] = "0" * 64  # deleted

    by_path = {f.rel_path: f for f in discover(root, EXT, known)}
    assert by_path["a.md"].drift is DriftKind.MODIFIED
    assert by_path["b_moved.txt"].drift is DriftKind.MOVED
    assert by_path["b_moved.txt"].old_rel_path == "sub/b.txt"
    assert by_path["c.md"].drift is DriftKind.NEW
    assert by_path["ghost.md"].drift is DriftKind.DELETED


def test_duplicate_copy_is_stable_not_moved(tmp_path: Path) -> None:
    """A copy of indexed content must not flip-flop the indexed path."""
    root = _setup(tmp_path)
    known = {f.rel_path: f.file_hash for f in discover(root, EXT, known={})}

    content = (root / "a.md").read_text(encoding="utf-8")
    (root / "a_copy.md").write_text(content, encoding="utf-8")

    by_path = {f.rel_path: f for f in discover(root, EXT, known)}
    assert by_path["a.md"].drift is DriftKind.UNCHANGED  # keeps ownership
    assert by_path["a_copy.md"].drift is DriftKind.DUPLICATE
    assert by_path["a_copy.md"].old_rel_path == "a.md"
    assert not any(f.drift is DriftKind.MOVED for f in by_path.values())

    # and it stays that way on every subsequent walk (no flip-flop)
    again = {f.rel_path: f.drift for f in discover(root, EXT, known)}
    assert again["a.md"] is DriftKind.UNCHANGED
    assert again["a_copy.md"] is DriftKind.DUPLICATE


def test_two_new_identical_files_one_owner(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "x.md").write_text("same bytes", encoding="utf-8")
    (root / "y.md").write_text("same bytes", encoding="utf-8")
    by_path = {f.rel_path: f for f in discover(root, EXT, known={})}
    assert by_path["x.md"].drift is DriftKind.NEW  # first in sort order
    assert by_path["y.md"].drift is DriftKind.DUPLICATE
    assert by_path["y.md"].old_rel_path == "x.md"


def test_deletion_is_keyed_by_hash_not_path(tmp_path: Path) -> None:
    """A hash still on disk under any path is never DELETED."""
    root = _setup(tmp_path)
    known = {f.rel_path: f.file_hash for f in discover(root, EXT, known={})}

    # move a.md AND leave a copy behind: hash present twice, old path gone
    content = (root / "a.md").read_text(encoding="utf-8")
    (root / "a.md").unlink()
    (root / "renamed.md").write_text(content, encoding="utf-8")

    found = discover(root, EXT, known)
    by_path = {f.rel_path: f for f in found}
    assert by_path["renamed.md"].drift is DriftKind.MOVED
    assert not any(f.drift is DriftKind.DELETED for f in found)

    # now remove the content entirely: DELETED fires exactly once, by hash
    (root / "renamed.md").unlink()
    found = discover(root, EXT, known)
    deleted = [f for f in found if f.drift is DriftKind.DELETED]
    assert [(f.rel_path, f.file_hash) for f in deleted] == [("a.md", known["a.md"])]


def test_path_edited_into_duplicate_cleans_stale_row(tmp_path: Path) -> None:
    """Editing a file to match another indexed file retires its old hash."""
    root = _setup(tmp_path)
    known = {f.rel_path: f.file_hash for f in discover(root, EXT, known={})}
    old_a_hash = known["a.md"]

    (root / "a.md").write_text("bravo", encoding="utf-8")  # == sub/b.txt

    found = discover(root, EXT, known)
    states = {(f.rel_path, f.drift) for f in found}
    assert ("sub/b.txt", DriftKind.UNCHANGED) in states
    # the path is now a duplicate of sub/b.txt ...
    duplicate = next(f for f in found if f.drift is DriftKind.DUPLICATE)
    assert duplicate.rel_path == "a.md"
    assert duplicate.old_rel_path == "sub/b.txt"
    # ... and its retired old hash is DELETED (same rel_path, two entries)
    deleted = [f for f in found if f.drift is DriftKind.DELETED]
    assert [(f.rel_path, f.file_hash) for f in deleted] == [("a.md", old_a_hash)]


def test_hash_is_content_only(tmp_path: Path) -> None:
    one = tmp_path / "one.md"
    two = tmp_path / "two.md"
    one.write_text("same bytes", encoding="utf-8")
    two.write_text("same bytes", encoding="utf-8")
    assert hash_file(one) == hash_file(two)
