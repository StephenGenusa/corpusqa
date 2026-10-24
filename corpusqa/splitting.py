"""Splitting markdown to fit a model's context window.

Shared by extraction (sections) and cataloging (card chunks) so both size
text by the real tokenizer, not a chars-per-token guess.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.llm.pricing import cost_map_window

_log = logging.getLogger("corpusqa.query")

# Conservative chars-per-token for the structural (heading-aware) split.
# English prose averages ~4; dense or non-English text (scripture citation,
# transliterations, code, tables) tokenizes below that, so 3 leaves margin.
# The real tokenizer still decides what fits: it hard-splits anything that
# overflows and packs sections up to the budget.
CHARS_PER_TOKEN = 3
# Bumped whenever a change alters where sections fall; part of the
# evaluation-cache key, since a verdict depends on how the file was sectioned.
SPLITTER_VERSION = 3
# Below this, a call carries too little document to be worth making.
_MIN_INPUT_TOKENS = 1_000
# Warnings already logged this process: input budgets are computed once per
# file, and the same misconfiguration should be reported once.
_warned: set[tuple[str, ...]] = set()


def reset_input_budget_warnings() -> None:
    """Forgets which input-budget warnings were logged (for tests)."""
    _warned.clear()


def _warn_once(key: tuple[str, ...], msg: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        _log.warning(msg, *args)


def input_token_budget(config: AppConfig, task: TaskName, overhead: int) -> int:
    """Returns the most document tokens one call of ``task`` may carry.

    The ceiling is the task's window minus ``overhead`` (fixed prompt
    scaffolding) minus a reservation for the model's OWN completion.
    Omitting the completion reservation lets prompt+output exceed the window;
    local OpenAI-compatible servers then often return EMPTY content, which
    surfaces downstream as "Invalid JSON: EOF while parsing". The task's
    ``max_input_tokens`` lowers the budget below that ceiling; it never
    raises it.

    Requests are sized for the primary model; a fallback whose known window
    cannot hold them is reported.
    """
    cfg = config.tasks[task]
    ceiling = max(cfg.context_window - overhead - cfg.max_tokens, _MIN_INPUT_TOKENS)
    budget = ceiling
    if cfg.max_input_tokens is not None:
        budget = min(cfg.max_input_tokens, ceiling)
        if cfg.max_input_tokens > ceiling:
            _warn_once(
                ("cap", task.value, str(cfg.max_input_tokens), str(ceiling)),
                "tasks.%s.max_input_tokens=%d exceeds what its window allows; using %d",
                task.value,
                cfg.max_input_tokens,
                ceiling,
            )
    for fallback in cfg.fallbacks:
        window = cost_map_window(fallback)
        if window is not None and window < budget + overhead:
            _warn_once(
                ("fallback", task.value, fallback, str(budget)),
                "%s requests are sized for %s (up to %d input tokens); fallback "
                "%s has a %d-token window and would truncate or fail",
                task.value,
                cfg.model,
                budget + overhead,
                fallback,
                window,
            )
    return budget


def _heading_pieces(markdown: str, max_chars: int) -> list[str]:
    """Cuts markdown at headings, recursively, into pieces under ``max_chars``.

    Splits at the shallowest heading level that exists, recursing into any
    piece still over ``max_chars``; falls back to hard character splits only
    for heading-free oversize text.
    """
    if len(markdown) <= max_chars:
        return [markdown]
    for level in range(1, 7):
        pattern = re.compile(rf"^(?={'#' * level}\s)", re.MULTILINE)
        parts = [p for p in pattern.split(markdown) if p.strip()]
        if len(parts) > 1:
            return [
                piece for part in parts for piece in _heading_pieces(part, max_chars)
            ]
    return [markdown[i : i + max_chars] for i in range(0, len(markdown), max_chars)]


def _pack(pieces: list[str], fits: Callable[[str], bool]) -> list[str]:
    """Joins adjacent pieces greedily while the joined text still ``fits``.

    PDF conversion can turn every running page header into a heading, and
    one section per heading would leave the window mostly empty.
    """
    out: list[str] = []
    for piece in pieces:
        if out and fits(out[-1] + piece):
            out[-1] += piece
        else:
            out.append(piece)
    return out


def split_markdown(markdown: str, max_chars: int) -> list[str]:
    """Splits markdown into sections under ``max_chars``, heading-first.

    Cuts at headings (see ``_heading_pieces``), then packs adjacent pieces
    back together up to ``max_chars``. Sections inherit their text verbatim
    (heading lines included), so finding provenance is unchanged (design doc
    section 4.4).

    Args:
        markdown: The text to split.
        max_chars: Maximum section size in characters.

    Returns:
        Ordered sections, each at most ``max_chars`` long.
    """
    return _pack(_heading_pieces(markdown, max_chars), lambda s: len(s) <= max_chars)


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

    Cuts at headings (via a char proxy), hard-splits any piece that still
    overflows under the real tokenizer, then packs adjacent pieces up to the
    budget by the real tokenizer too. The char estimate only affects where
    the heading cuts fall, not whether a section fits or how full it is.
    """
    char_budget = max(token_budget * CHARS_PER_TOKEN, 1)
    pieces: list[str] = []
    for piece in _heading_pieces(markdown, char_budget):
        if count_tokens(piece) <= token_budget:
            pieces.append(piece)
        else:
            pieces.extend(hard_split_to_tokens(piece, token_budget, count_tokens))
    return _pack(pieces, lambda s: count_tokens(s) <= token_budget)
