"""Prompt fragments shared by several tasks."""

from __future__ import annotations

DEFINITION_BLOCK = (
    "The user defines the topic as follows; judge relevance against "
    "THIS definition, not the common meaning:\n{definition}\n\n"
)
"""Prepended to the question when the user supplies a topic definition."""


def definition_block(definition: str | None) -> str:
    """Renders ``DEFINITION_BLOCK`` for ``definition``, or "" when there is none."""
    return DEFINITION_BLOCK.format(definition=definition) if definition else ""
