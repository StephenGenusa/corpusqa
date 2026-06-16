"""LLM reranking prompt. Tuned in M6."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RerankScores(BaseModel):
    """Structured output: one score per candidate, by index."""

    scores: list[float] = Field(default_factory=list)


SYSTEM = (
    "Score each numbered text chunk 0.0-1.0 for relevance to the question, "
    "judged against the user's definition when one is given."
)

OUTPUT_MODEL = RerankScores
