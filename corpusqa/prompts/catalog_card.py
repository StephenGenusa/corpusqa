"""Extraction-inventory card prompt (M5: replaces summarization)."""

from __future__ import annotations

from pydantic import BaseModel


class CatalogCardInput(BaseModel):
    """Inputs to card generation.

    Attributes:
        rel_path: File path for context.
        doc_markdown: Head of the file's markdown within budget.
    """

    rel_path: str
    doc_markdown: str


SYSTEM = (
    "You build an extraction INVENTORY used to route arbitrary future "
    "questions to files. Do not summarize -- summaries collapse the "
    "particulars that routing needs. Instead, inventory the specifics: "
    "every distinct claim the document makes; key terms WITH the sense the "
    "document uses them in (not the common sense); topics discussed without "
    "being named; concrete questions the document could answer; and notable "
    "absences -- things a reader would expect this document type to address "
    "that it does not. Prefer many specific entries over few general ones."
)

TEMPLATE = "File: {rel_path}\n\n<document>\n{doc_markdown}\n</document>"
