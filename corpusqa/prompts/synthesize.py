"""Synthesis prompt (reduce). Tuned in M3.

The model may cite ONLY numbered evidence items; the assembler resolves
numbers to (file, heading, page). This makes 'no citation, no claim'
machine-checkable (design doc section 8).
"""

from __future__ import annotations

SYSTEM = (
    "You compile an answer from numbered evidence items. Every claim must "
    "cite evidence as [n] or [n, m]. Do not introduce claims without an "
    "evidence number, and do not infer connections the evidence does not "
    "itself make. Deduplicate overlapping excerpts that reference the same "
    "passage (cite all their numbers once). Organize findings thematically, "
    "grouping related evidence across documents. Preserve disagreements "
    "between sources explicitly. Cite using ONLY the bracketed numbers; never "
    "write out file names, section titles, or page numbers yourself -- the "
    "assembler renders those from the numbers. Write in plain prose paragraphs "
    "for a plain-text terminal: do not use Markdown headings (#), bullet or "
    "list symbols, tables, or bold/italic markup."
)


PARTIAL_SYSTEM = (
    "You condense evidence from ONE file for a later merge. Preserve every "
    "[n] citation number VERBATIM next to the claim it supports; never "
    "renumber, never drop numbers. Output a compact bullet-free digest."
)