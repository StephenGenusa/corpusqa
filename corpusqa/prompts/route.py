"""Routing prompt (pass 1). Tuned in M3; recall-biased by instruction."""

from __future__ import annotations

from pydantic import BaseModel


class RouteInput(BaseModel):
    """Inputs to one routing call.

    Attributes:
        question: The user question, verbatim.
        definition: Optional user topic definition, verbatim.
        cards_block: Rendered catalog cards (one shard).
    """

    question: str
    definition: str | None
    cards_block: str


SYSTEM = (
    "You select which files from a catalog might help answer a question. "
    "Be recall-biased: include a file if it is PLAUSIBLY relevant; only "
    "exclude files that are clearly unrelated. A later stage discards false "
    "positives cheaply; a file you exclude is lost silently."
)

TEMPLATE = (
    "Question:\n{question}\n\n"
    "{definition_block}"
    "Catalog cards:\n{cards_block}\n\n"
    "For EVERY card, return a decision."
)
