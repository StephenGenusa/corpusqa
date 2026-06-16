"""File discovery, content hashing, and drift detection.

Identity is the SHA-256 of file bytes -- never mtime, which is unreliable
across platforms, copies, and sync tools (design doc section 4.1). Paths are
stored POSIX-normalized relative to the corpus root.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from corpusqa.errors import IngestError

_HASH_BLOCK = 1 << 20  # 1 MiB
INDEX_DIR_NAME = ".corpusqa"


class DriftKind(StrEnum):
    """Classification of a file's state relative to the index."""

    NEW = "new"
    MODIFIED = "modified"  # known path, changed hash -> reparse
    MOVED = "moved"  # known hash, old path gone -> metadata update only
    DUPLICATE = "duplicate"  # content already owned by another on-disk path
    DELETED = "deleted"  # indexed hash no longer present anywhere on disk
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class DiscoveredFile:
    """A file's state relative to the index.

    Attributes:
        rel_path: Path relative to the corpus root, POSIX separators. For
            ``MOVED`` files this is the new path; ``old_rel_path`` holds the
            indexed one. For ``DELETED`` files it is the indexed path.
        file_hash: SHA-256 hex digest (the identity key; identity is content
            only, never the path).
        size_bytes: File size; 0 for ``DELETED`` entries.
        drift: State relative to the current index.
        old_rel_path: For ``MOVED``, the previously indexed path; for
            ``DUPLICATE``, the path that owns this content.
    """

    rel_path: str
    file_hash: str
    size_bytes: int
    drift: DriftKind
    old_rel_path: str | None = None


def hash_file(path: Path) -> str:
    """Computes the streaming SHA-256 of a file.

    Args:
        path: File to hash.

    Returns:
        Lowercase hex digest.

    Raises:
        IngestError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(_HASH_BLOCK):
                digest.update(block)
    except OSError as exc:
        raise IngestError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def discover(
    corpus_root: Path,
    extensions: list[str],
    known: Mapping[str, str],
) -> list[DiscoveredFile]:
    """Walks the corpus and classifies each file against the index.

    The index directory (``.corpusqa``) is always excluded from the walk.

    Args:
        corpus_root: Directory to walk recursively.
        extensions: Lowercase extensions to include (with leading dot).
        known: Indexed state as a mapping ``rel_path -> file_hash``
            (CONTRACT CHANGE M2-C1: drift classification requires the
            indexed state; pure path+extensions could not provide it).

    Returns:
        One entry per on-disk file plus one ``DELETED`` entry per indexed
        hash no longer present anywhere on disk.

    Raises:
        IngestError: If the root is missing or unreadable.
    """
    if not corpus_root.is_dir():
        raise IngestError(f"corpus root is not a directory: {corpus_root}")
    wanted = {e.lower() for e in extensions}
    hash_to_known_path = {h: p for p, h in known.items()}

    # Pass 1: hash everything on disk. Classification needs the complete
    # picture (a hash seen at one path changes how another path classifies),
    # so it cannot happen inside the walk.
    on_disk: list[tuple[str, str, int]] = []  # (rel_path, hash, size)
    for path in sorted(corpus_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in wanted:
            continue
        rel = path.relative_to(corpus_root)
        if INDEX_DIR_NAME in rel.parts:
            continue
        on_disk.append((rel.as_posix(), hash_file(path), path.stat().st_size))

    # Identity is the content hash, so each hash has exactly one owning
    # path per walk. An indexed path that is still on disk unchanged keeps
    # ownership of its hash (this is what prevents the duplicate-copy
    # flip-flop); otherwise the first path in sorted order claims it.
    owner: dict[str, str] = {}
    for rel_posix, file_hash, _ in on_disk:
        if known.get(rel_posix) == file_hash:
            owner.setdefault(file_hash, rel_posix)

    results: list[DiscoveredFile] = []
    modified_paths: set[str] = set()
    for rel_posix, file_hash, size in on_disk:
        old: str | None = None
        if known.get(rel_posix) == file_hash:
            drift = DriftKind.UNCHANGED
        elif file_hash in owner:
            drift = DriftKind.DUPLICATE
            old = owner[file_hash]
        elif rel_posix in known:
            drift = DriftKind.MODIFIED
            modified_paths.add(rel_posix)
            owner[file_hash] = rel_posix
        elif file_hash in hash_to_known_path:
            drift = DriftKind.MOVED
            old = hash_to_known_path[file_hash]
            owner[file_hash] = rel_posix
        else:
            drift = DriftKind.NEW
            owner[file_hash] = rel_posix
        results.append(
            DiscoveredFile(
                rel_path=rel_posix,
                file_hash=file_hash,
                size_bytes=size,
                drift=drift,
                old_rel_path=old,
            )
        )

    # Deletion is keyed by hash, not path: an indexed identity is gone only
    # when its content is absent from the whole corpus. MOVED needs no
    # special-casing here (its hash is on disk); MODIFIED paths are exempt
    # because the index pipeline replaces their old row itself.
    on_disk_hashes = {h for _, h, _ in on_disk}
    for rel_posix, file_hash in known.items():
        if file_hash in on_disk_hashes or rel_posix in modified_paths:
            continue
        results.append(
            DiscoveredFile(
                rel_path=rel_posix,
                file_hash=file_hash,
                size_bytes=0,
                drift=DriftKind.DELETED,
            )
        )
    return results
