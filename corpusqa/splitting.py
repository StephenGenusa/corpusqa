"""Splitting markdown to fit a model's context window.

Shared by extraction (sections) and cataloging (card chunks) so both size
text by the real tokenizer, not a chars-per-token guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Conservative chars-per-token for the structural (heading-aware) split.
# English prose averages ~4; dense or non-English text (scripture citation,
# transliterations, code, tables) tokenizes below that, so 3 leaves margin.
# The real tokenizer is still consulted per piece to hard-split anything that
# overflows regardless.
CHARS_PER_TOKEN = 3


def split_markdown(markdown: str, max_chars: int) -> list[str]:
    """Splits markdown into sections under ``max_chars``, heading-first.

    Splits at the shallowest heading level that exists, recursively; falls
    back to hard character splits only for heading-free oversize text.
    Sections inherit their text verbatim (heading lines included), so
    finding provenance is unchanged (design doc section 4.4).

    Args:
        markdown: The text to split.
        max_chars: Maximum section size in characters.

    Returns:
        Ordered sections, each at most ``max_chars`` long.
    """
    if len(markdown) <= max_chars:
        return [markdown]
    for level in range(1, 7):
        pattern = re.compile(rf"^(?={'#' * level}\s)", re.MULTILINE)
        parts = [p for p in pattern.split(markdown) if p.strip()]
        if len(parts) > 1:
            out: list[str] = []
            for part in parts:
                out.extend(split_markdown(part, max_chars))
            return out
    return [markdown[i : i + max_chars] for i in range(0, len(markdown), max_chars)]


def hard_split_to_tokens(
    text: str,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    """Greedily splits ``text`` into fragments that each fit ``token_budget``.

    Used only when a structurally-split section still exceeds the budget under
    the real tokenizer (text denser than the char proxy). Grows a char window
    to the largest prefix that fits, shrinking when it overshoots. Always makes
    forward progress, so it terminates.
    """
    out: list[str] = []
    start = 0
    n = len(text)
    approx = max(token_budget * CHARS_PER_TOKEN, 1)
    while start < n:
        end = min(start + approx, n)
        while end - start > 1 and count_tokens(text[start:end]) > token_budget:
            end = start + max((end - start) // 2, 1)
        out.append(text[start:end])
        start = end
    return out


def split_within_token_budget(
    markdown: str,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> list[str]:
    """Splits markdown so every section fits ``token_budget`` real tokens.

    First does the heading-aware structural split (via a char proxy), then
    enforces the actual token budget per section, hard-splitting any that still
    overflow. This decouples correctness from the chars-per-token estimate: the
    estimate only affects how cleanly sections fall on heading boundaries, not
    whether they fit the model window.
    """
    char_budget = max(token_budget * CHARS_PER_TOKEN, 1)
    sections: list[str] = []
    for piece in split_markdown(markdown, char_budget):
        if count_tokens(piece) <= token_budget:
            sections.append(piece)
        else:
            sections.extend(hard_split_to_tokens(piece, token_budget, count_tokens))
    return sections
