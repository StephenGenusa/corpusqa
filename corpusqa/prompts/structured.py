"""Structured-output instructions appended to any JSON-producing task."""

from __future__ import annotations

SCHEMA_INSTRUCTION = (
    "\n\nRespond with a single JSON object conforming to this JSON "
    "schema. No prose, no Markdown fences.\n{schema}"
)

TRUNCATED_REPAIR = (
    "Your previous output was cut off before the JSON finished "
    "(it exceeded the length limit). Return STRICTLY VALID, COMPLETE "
    "JSON: include only the most important findings (at most 8), keep "
    "each quote under 25 words, and make sure every brace and bracket "
    "is closed."
)

INVALID_REPAIR = (
    "Your JSON failed validation with these errors:\n"
    "{errors}\n"
    "Return only the corrected JSON object."
)
