"""Pass-2 per-file extraction (map step). Implemented in M3.

Operates on whole-file cached markdown; splits on top-level headings
(recursively) only when the file exceeds the extract model's window, with
section splits inheriting full heading provenance. Document text is wrapped
in explicit delimiters and treated as data, not instructions.
"""

from __future__ import annotations

import re
import unicodedata
import logging
from collections.abc import Callable

from pydantic import BaseModel, Field

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import CorpusQAError
from corpusqa.llm.structured import complete_structured
from corpusqa.llm.tasks import LLMTaskClient

_log = logging.getLogger("corpusqa.query")


class Finding(BaseModel):
    """One extracted, citable finding.

    Attributes:
        claim: The extracted statement, in the model's words.
        quote: Short supporting excerpt, copied verbatim from the document.
        heading_path: Ancestor headings of the source location, root to leaf.
            Derived by ``extract_file`` from where the quote actually occurs in
            the markdown -- NOT taken from the model, which cannot see the
            document's heading structure reliably and tends to invent it.
        page_no: Source page. The markdown export carries no page information,
            so this is left None rather than fabricated; real page numbers
            require a conversion-time page map (not yet built).
        quote_verified: Whether ``quote`` was located in the source markdown
            under robust normalization (unicode/ligature/quote/dash folding,
            de-hyphenation, markdown-decoration and whitespace/case folding).
            The model does not set this; ``extract_file`` overwrites it. False
            means the cited text could not be traced to source -- surfaced,
            never silently trusted.
    """

    claim: str
    quote: str
    heading_path: list[str] = Field(default_factory=list)
    page_no: int | None = None
    quote_verified: bool = True


class ExtractionResult(BaseModel):
    """Per-file output of the extract task.

    Attributes:
        file_hash: Identity of the examined file.
        relevant: Final relevance verdict (this is where pass-1 recall bias
            is corrected).
        confidence: Verdict confidence in [0, 1].
        reasoning: One-to-three sentence justification of the verdict;
            surfaced by ``corpusqa explain``.
        findings: Cited findings; empty when ``relevant`` is False.
    """

    file_hash: str
    relevant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    findings: list[Finding] = Field(default_factory=list)


_PROMPT_OVERHEAD_TOKENS = 2_000
# Conservative chars-per-token for sizing the document portion of a section.
# The previous value was an implicit 4 (English prose average); dense or
# non-English text (scripture citation, transliterations, code, tables)
# tokenizes below that, so a section sized at 4 chars/token can exceed the
# window. 3 leaves structural margin; the real tokenizer is still consulted
# per section below to hard-split anything that overflows regardless.
_CHARS_PER_TOKEN = 3
_MIN_SECTION_TOKENS = 1_000
_HEADING_RE = re.compile(r"^(#{1,6})\s", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_WS_RE = re.compile(r"\s+")

# Surface characters folded together during quote matching. PDF-to-markdown
# extraction routinely substitutes these for their ASCII equivalents, and the
# model quotes the clean reading; folding them lets a genuine quote verify.
_PUNCT_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",  # single quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',  # double quotes
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",  # dashes/minus
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",                  # nbsp variants
}
# Markdown decoration the model omits from quoted text.
_DECORATION = set("*_`")

# Ellipsis forms used to elide a span inside a quote ("A ... B" / "A … B").
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\s*\.\s*\.+|\u2026)\s*")
# Minimum normalized length for a fragment to anchor a multi-part match; short
# fragments ("the", "and") would match almost anywhere.
_MIN_FRAGMENT_CHARS = 12
# Minimum verbatim run for the end-trim fallback to accept a near-verbatim
# quote. A full clause this long is not shared by a genuine paraphrase, so
# trimming only the quote's ends cannot launder a paraphrase into a match.
_MIN_VERIFY_RUN_CHARS = 40
# Max words trimmed from each end of a quote when the exact span is not found
# (covers a model adding/dropping a word or stray punctuation at the edges).
_MAX_TRIM_WORDS = 3
# Joins the verbatim spans of an elided ("A ... B") quote when snapping to
# source text, marking the elision the reader should expect.
_SNAP_SEPARATOR = " … "


def _normalize_for_match(text: str) -> tuple[str, list[int]]:
    """Folds PDF/markdown surface noise for quote matching.

    Returns ``(normalized, index_map)`` where ``index_map[i]`` is the offset in
    the ORIGINAL ``text`` that produced normalized character ``i`` (one output
    char may map back to one input char even when a ligature expanded to
    several). The map lets a match in normalized space be traced to a real
    offset in the source, which is how provenance is recovered.

    Folds applied: NFKD plus diacritic removal (handles ligatures like fi and
    accents), unicode quote/dash/space variants to ASCII, soft hyphens and
    hyphenated line breaks joined, markdown emphasis dropped, whitespace
    collapsed, case folded.
    """
    out: list[str] = []
    idx: list[int] = []
    prev_space = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Hyphenated line break: "word-\n  word" -> "wordword".
        if ch == "-" and i + 1 < n and text[i + 1] in " \t\r\n":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j].isalpha():
                i = j
                continue
        if ch == "\u00ad":  # soft hyphen
            i += 1
            continue
        for dec in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(dec):  # drop accents/marks
                continue
            dec = _PUNCT_FOLD.get(dec, dec)
            if dec in _DECORATION:
                continue
            if dec.isspace():
                if not prev_space and out:
                    out.append(" ")
                    idx.append(i)
                    prev_space = True
                continue
            for folded in dec.casefold():
                out.append(folded)
                idx.append(i)
            prev_space = False
        i += 1
    while out and out[-1] == " ":  # drop any trailing space, keeping idx aligned
        out.pop()
        idx.pop()
    return "".join(out), idx


def _quote_in_source(quote: str, source: str) -> bool:
    """Reports whether ``quote`` occurs in ``source`` under robust folding.

    The folding (see ``_normalize_for_match``) absorbs the systematic
    differences between a clean model quote and raw PDF-derived markdown
    (ligatures, smart quotes, dashes, hyphenation, emphasis, whitespace, case)
    so that genuinely grounded quotes verify while paraphrases still do not.
    An empty quote can never be verified.
    """
    norm_source, source_map = _normalize_for_match(source)
    return bool(_match_spans(quote, norm_source, source_map))


def _quote_fragments(quote: str) -> list[str]:
    """Normalized, decoration-stripped fragments of a model quote.

    Two habits defeat a naive verbatim match even when the model quoted the
    source faithfully: wrapping the excerpt in quotation marks, and using an
    ellipsis to elide a span ("A ... B"). So the raw quote is split on ellipsis
    first, each part normalized, then any surrounding quote characters trimmed.
    A paraphrase still fails -- every returned fragment must occur verbatim.
    """
    fragments: list[str] = []
    for part in _ELLIPSIS_RE.split(quote):
        norm, _ = _normalize_for_match(part)
        norm = norm.strip("'\" ")
        if norm:
            fragments.append(norm)
    return fragments


def _span_at(
    norm_source: str, source_map: list[int], pos: int, length: int
) -> tuple[int, int]:
    """Maps a normalized [pos, pos+length) hit back to original-text [start,end)."""
    start = source_map[pos]
    end = source_map[pos + length - 1] + 1
    return start, end


def _find_span_with_trim(
    fragment: str, norm_source: str, source_map: list[int]
) -> tuple[int, int] | None:
    """Locates ``fragment`` in the normalized source, returning its source span.

    Exact match first; then trims up to ``_MAX_TRIM_WORDS`` words from each end
    and matches the longest surviving span (>= ``_MIN_VERIFY_RUN_CHARS``). This
    rescues quotes verbatim in their core but carrying an extra/missing word at
    an edge -- a common local-model habit -- without admitting paraphrases,
    whose interior words differ. Returns original-text ``(start, end)`` or None.
    """
    pos = norm_source.find(fragment)
    if pos >= 0:
        return _span_at(norm_source, source_map, pos, len(fragment))
    words = fragment.split(" ")
    if len(words) < 4:
        return None
    # Ascending total trim => the first hit is the longest surviving span.
    for total in range(1, 2 * _MAX_TRIM_WORDS + 1):
        for left in range(0, min(total, _MAX_TRIM_WORDS) + 1):
            right = total - left
            if right > _MAX_TRIM_WORDS or left + right >= len(words):
                continue
            span = " ".join(words[left : len(words) - right])
            if len(span) < _MIN_VERIFY_RUN_CHARS:
                continue
            hit = norm_source.find(span)
            if hit >= 0:
                return _span_at(norm_source, source_map, hit, len(span))
    return None


def _match_spans(
    quote: str, norm_source: str, source_map: list[int]
) -> list[tuple[int, int]]:
    """Returns the original-text spans a quote matches, or [] if unverified.

    A single-fragment quote must occur contiguously (with an end-trim
    fallback). An ellipsis-elided quote ("A ... B") matches only if each
    SUBSTANTIVE fragment occurs verbatim and in order; each fragment's own span
    is returned so the displayed quote can be rebuilt from real source text
    without pulling in the elided middle.
    """
    fragments = _quote_fragments(quote)
    if not fragments:
        return []
    if len(fragments) == 1:
        span = _find_span_with_trim(fragments[0], norm_source, source_map)
        return [span] if span else []

    substantive = [f for f in fragments if len(f) >= _MIN_FRAGMENT_CHARS]
    if len(substantive) < 2:
        longest = max(fragments, key=len)
        span = _find_span_with_trim(longest, norm_source, source_map)
        return [span] if span else []

    spans: list[tuple[int, int]] = []
    search_from = 0
    for fragment in substantive:
        pos = norm_source.find(fragment, search_from)
        if pos < 0:
            return []  # every substantive fragment must occur, in order
        spans.append(_span_at(norm_source, source_map, pos, len(fragment)))
        search_from = pos + len(fragment)
    return spans


def _locate_quote(quote: str, norm_source: str, source_map: list[int]) -> int:
    """Original-text start offset of ``quote``, or -1 (compat wrapper)."""
    spans = _match_spans(quote, norm_source, source_map)
    return spans[0][0] if spans else -1


def _page_at(page_map: list[tuple[int, int]] | None, offset: int) -> int | None:
    """Page number for a markdown ``offset`` from sorted (offset, page) marks.

    ``page_map`` is non-decreasing in page; the page is the mark with the
    greatest offset <= ``offset``. None when there is no map or the offset
    precedes the first mark.
    """
    if not page_map:
        return None
    lo, hi, page = 0, len(page_map), None
    while lo < hi:  # rightmost mark whose offset <= target
        mid = (lo + hi) // 2
        if page_map[mid][0] <= offset:
            page = page_map[mid][1]
            lo = mid + 1
        else:
            hi = mid
    return page


def _heading_path_at(markdown: str, offset: int) -> list[str]:
    """Builds the real heading path (root to leaf) in effect at ``offset``.

    Walks the markdown's ``#`` headings up to ``offset``, maintaining a level
    stack, so the returned path reflects the document's actual structure rather
    than the model's guess.
    """
    stack: dict[int, str] = {}
    pos = 0
    for line in markdown.splitlines(keepends=True):
        if pos >= offset:
            break
        match = _HEADING_LINE_RE.match(line)
        if match:
            level = len(match.group(1))
            stack[level] = match.group(2).strip()
            for deeper in [lvl for lvl in stack if lvl > level]:
                del stack[deeper]
        pos += len(line)
    return [stack[lvl] for lvl in sorted(stack)]


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


def _hard_split_to_tokens(
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
    approx = max(token_budget * _CHARS_PER_TOKEN, 1)
    while start < n:
        end = min(start + approx, n)
        while end - start > 1 and count_tokens(text[start:end]) > token_budget:
            end = start + max((end - start) // 2, 1)
        out.append(text[start:end])
        start = end
    return out


def _split_within_token_budget(
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
    char_budget = max(token_budget * _CHARS_PER_TOKEN, 1)
    sections: list[str] = []
    for piece in split_markdown(markdown, char_budget):
        if count_tokens(piece) <= token_budget:
            sections.append(piece)
        else:
            sections.extend(_hard_split_to_tokens(piece, token_budget, count_tokens))
    return sections


def plan_sections(
    markdown: str,
    client: LLMTaskClient,
    config: AppConfig,
) -> list[str]:
    """Splits one file's markdown into the sections extraction will send.

    Exposed so callers can size work (e.g. a progress total of total LLM
    calls) and pass the result straight into ``extract_file``, avoiding a
    second split. Sizing rationale lives on ``_split_within_token_budget``.
    """
    extract_cfg = config.tasks[TaskName.EXTRACT]
    # Document-token budget = window minus fixed prompt scaffolding (schema
    # instruction + system + template + question) minus a reservation for the
    # model's OWN completion. Omitting the completion reservation lets
    # prompt+output exceed the window; local OpenAI-compatible servers then
    # often return EMPTY content, which surfaces downstream as
    # "Invalid JSON: EOF while parsing". The reservation keeps the request
    # inside the window.
    doc_token_budget = max(
        extract_cfg.context_window - _PROMPT_OVERHEAD_TOKENS - extract_cfg.max_tokens,
        _MIN_SECTION_TOKENS,
    )
    return _split_within_token_budget(
        markdown,
        doc_token_budget,
        lambda text: client.count_tokens(TaskName.EXTRACT, text),
    )


async def extract_file(
    question: str,
    definition: str | None,
    rel_path: str,
    file_hash: str,
    markdown: str,
    client: LLMTaskClient,
    config: AppConfig,
    relevance: str = "recall",
    sections: list[str] | None = None,
    on_section: Callable[[], None] | None = None,
    page_map: list[tuple[int, int]] | None = None,
) -> ExtractionResult:
    """Runs pass-2 extraction over one file (sectioned when oversize).

    Args:
        question: The user question, verbatim.
        definition: Optional user topic definition, verbatim.
        rel_path: File path for context.
        file_hash: File identity (authoritative; model echo is discarded).
        markdown: The cached whole-file markdown.
        client: Task client.
        config: Application configuration.
        relevance: ``recall | balanced | strict`` inclusion bar.
        sections: Pre-planned sections (from ``plan_sections``); when None the
            file is split here. Passing them in avoids splitting twice when the
            caller already planned the work (e.g. for a progress total).
        on_section: Called once after each section's LLM result, for progress
            reporting. One call == one extraction LLM round-trip completed.
        page_map: Sorted ``(markdown_offset, page_no)`` marks from indexing;
            used to stamp each verified finding's source page. None when the
            source carries no page provenance (md/txt, scanned).

    Returns:
        The merged extraction result. ``relevant`` is True if any section
        was relevant; findings are concatenated in document order. The
        ``reasoning`` of every contributing verdict is preserved (it is
        what ``corpusqa explain`` surfaces), and confidence is merged per
        verdict: the max over relevant sections when relevant, else the
        min over all sections (the file is irrelevant only if every
        section is, so the weakest negative verdict bounds the whole).
    """
    from corpusqa.prompts import extract as extract_prompt

    if sections is None:
        sections = plan_sections(markdown, client, config)
    definition_block = (
        f"The user defines the topic as follows; judge relevance against "
        f"THIS definition, not the common meaning:\n{definition}\n\n"
        if definition
        else ""
    )

    # Normalize the whole-file markdown once; every finding's quote is located
    # against it to (a) verify and (b) recover real provenance.
    norm_source, source_map = _normalize_for_match(markdown)

    section_results: list[ExtractionResult] = []
    section_failures = 0
    last_error: CorpusQAError | None = None
    for section in sections:
        system = (
            extract_prompt.SYSTEM
            + " "
            + extract_prompt.RELEVANCE_INSTRUCTIONS[relevance]
        )
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": extract_prompt.TEMPLATE.format(
                    question=question,
                    definition_block=definition_block,
                    rel_path=rel_path,
                    file_hash=file_hash,
                    doc_markdown=section,
                ),
            },
        ]
        try:
            result = await complete_structured(
                client, TaskName.EXTRACT, messages, ExtractionResult
            )
        except CorpusQAError as exc:
            # One section's structured-output failure (e.g. a truncated JSON
            # the salvage could not recover) must not discard the whole file's
            # findings. Skip it and keep going; only an all-sections failure is
            # fatal.
            section_failures += 1
            last_error = exc
            _log.warning(
                "extract: skipped a section of %s after structured failure: %s",
                rel_path,
                exc,
            )
            if on_section is not None:
                on_section()
            continue
        for finding in result.findings:
            spans = _match_spans(finding.quote, norm_source, source_map)
            finding.quote_verified = bool(spans)
            finding.page_no = None
            if spans:
                # Snap the displayed quote to the EXACT source text the model
                # pointed at (joined for an elided "A ... B" quote), so what the
                # reader sees is verbatim from the file, not the model's wording.
                finding.quote = _SNAP_SEPARATOR.join(
                    markdown[start:end].strip() for start, end in spans
                )
                # Provenance is system-derived, not trusted from the model:
                # real heading path and page from where the quote actually sits.
                finding.heading_path = _heading_path_at(markdown, spans[0][0])
                finding.page_no = _page_at(page_map, spans[0][0])
        section_results.append(result)
        if on_section is not None:
            on_section()

    if not section_results:
        # Every section failed -- surface it (the file is recorded as failed).
        if last_error is not None:
            raise last_error
        return ExtractionResult(
            file_hash=file_hash, relevant=False, confidence=0.0, reasoning=""
        )
    if section_failures:
        _log.warning(
            "extract: %s completed with %d/%d sections skipped",
            rel_path,
            section_failures,
            len(sections),
        )

    relevant_results = [r for r in section_results if r.relevant]
    if relevant_results:
        return ExtractionResult(
            file_hash=file_hash,
            relevant=True,
            confidence=max(r.confidence for r in relevant_results),
            reasoning=_merge_reasoning(relevant_results),
            findings=[f for r in relevant_results for f in r.findings],
        )
    return ExtractionResult(
        file_hash=file_hash,
        relevant=False,
        confidence=min(r.confidence for r in section_results),
        reasoning=_merge_reasoning(section_results),
    )


def _merge_reasoning(results: list[ExtractionResult]) -> str:
    """Joins distinct non-empty section reasonings in document order."""
    seen: list[str] = []
    for result in results:
        reasoning = result.reasoning.strip()
        if reasoning and reasoning not in seen:
            seen.append(reasoning)
    return " | ".join(seen)