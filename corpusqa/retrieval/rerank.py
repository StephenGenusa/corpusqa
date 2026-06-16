"""Reranker protocol and both implementations (M6).

``LLMReranker`` is selected automatically when the query carries a custom
topic definition; ``CrossEncoderReranker`` is the default otherwise
(design doc section 4.3). Applied to vector-channel candidates only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from corpusqa.retrieval.vector_store import VectorHit


@dataclass(frozen=True)
class ScoredChunk:
    """A reranked candidate.

    Attributes:
        hit: The original vector hit.
        score: Reranker relevance score, normalized to [0, 1].
    """

    hit: VectorHit
    score: float


class Reranker(Protocol):
    """Reranking seam over vector-channel candidates."""

    async def rerank(
        self,
        query: str,
        candidates: list[VectorHit],
        definition: str | None,
    ) -> list[ScoredChunk]:
        """Scores candidates for relevance to the query.

        Args:
            query: The user question.
            candidates: Vector-channel hits to score.
            definition: Optional user-supplied topic definition; rerankers
                that cannot use it may ignore it.

        Returns:
            Candidates with scores, descending.
        """
        ...
