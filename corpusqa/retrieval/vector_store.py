"""Vector store protocol and LanceDB implementation (M5).

The protocol keeps the store swappable (dependency inversion); concrete
classes are constructed only in the composition root.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from corpusqa.ingest.chunker import Chunk


@dataclass(frozen=True)
class VectorHit:
    """A nearest-neighbor result.

    Attributes:
        chunk_id: Identifier joining back to the catalog ``chunks`` table.
        file_hash: Owning file identity.
        text: Chunk text.
        score: Similarity score (higher is closer).
    """

    chunk_id: str
    file_hash: str
    text: str
    score: float


class VectorStore(Protocol):
    """Minimal embedded vector store surface (design doc section 4.3)."""

    def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Inserts chunks with their vectors."""
        ...

    def delete_by_file(self, file_hash: str) -> None:
        """Removes all chunks for a file (drift handling)."""
        ...

    def search(self, vector: list[float], top_k: int) -> list[VectorHit]:
        """Returns the ``top_k`` nearest chunks."""
        ...

    def count(self) -> int:
        """Returns the number of stored chunks."""
        ...
