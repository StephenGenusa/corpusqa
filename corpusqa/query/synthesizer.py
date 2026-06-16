"""Reduce step: cited answer assembly (design doc section 8).

The synthesize prompt receives findings as numbered evidence items and may
cite only those numbers; this module maps numbers back to
``(file, heading_path, page)`` and renders the human-readable form.
Unresolvable cites get one repair retry, then a visible ``[uncited]``
marker -- never silently dropped. This makes "no citation, no claim"
machine-checkable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.prompts import synthesize as synth_prompt
from corpusqa.query.extractor import (
    ExtractionResult,
    Finding,
    _normalize_for_match,
)

_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_REDUCE_OVERHEAD_TOKENS = 3_000
_MAX_REDUCE_DEPTH = 3


@dataclass(frozen=True)
class Evidence:
    """One numbered evidence item.

    Attributes:
        number: 1-based citation number.
        rel_path: Source file path.
        finding: The underlying finding.
    """

    number: int
    rel_path: str
    finding: Finding


@dataclass(frozen=True)
class CitedAnswer:
    """Final answer with resolved citations.

    Attributes:
        text: Answer body with inline rendered citations.
        sources: Rendered sources block.
        uncited_claims: Sentences whose evidence numbers failed to resolve
            after repair (surfaced, never silently dropped).
        resolved_cites: Citation numbers that resolved to evidence.
        unresolved_cites: Citation numbers that did not.
    """

    text: str
    sources: str
    uncited_claims: list[str]
    resolved_cites: int = 0
    unresolved_cites: int = 0


def number_evidence(
    results: list[tuple[str, ExtractionResult]],
) -> list[Evidence]:
    """Assigns global citation numbers across all relevant findings.

    Overlapping sections of a large file routinely surface the SAME passage
    more than once; those duplicates are collapsed (per file, by normalized
    quote, falling back to the claim) so the evidence fed to synthesis -- and
    the citations the reader sees -- are not flooded with repeats. This also
    keeps the synthesize prompt smaller, which matters for big files.

    Args:
        results: ``(rel_path, extraction)`` pairs in document order.

    Returns:
        Numbered evidence in stable order.
    """
    evidence: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for rel_path, result in results:
        if not result.relevant:
            continue
        for finding in result.findings:
            norm_quote, _ = _normalize_for_match(finding.quote)
            key = (
                rel_path,
                norm_quote or f"claim:{finding.claim.strip().casefold()}",
            )
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                Evidence(number=len(evidence) + 1, rel_path=rel_path, finding=finding)
            )
    return evidence


def _heading_is_filename(heading: str, rel_path: str) -> bool:
    """True when a heading just repeats the file's own name/stem.

    Docling sometimes makes the document title (== the file name) the top
    heading, yielding citations like ``foo.pdf § foo.pdf``. Such a heading adds
    nothing, so callers drop it.
    """
    name = rel_path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    h = heading.strip().casefold()
    return h in {name.casefold(), stem.casefold()}


def render_cite(item: Evidence) -> str:
    """Renders the citation contract form for one evidence item.

    Heading-less documents (flat TXT, unstructured PDFs) -- and headings that
    merely repeat the file name -- fall back to page number alone, or bare
    path. A finding whose quote could not be located in the source is tagged so
    the reader sees the claim is not source-traceable rather than trusting it
    silently.
    """
    page = f", p.{item.finding.page_no}" if item.finding.page_no else ""
    unverified = " (unverified)" if not item.finding.quote_verified else ""
    heading = item.finding.heading_path[-1] if item.finding.heading_path else ""
    if heading and not _heading_is_filename(heading, item.rel_path):
        return f"[{item.rel_path} § {heading}{page}{unverified}]"
    if page:
        return f"[{item.rel_path}{page}{unverified}]"
    return f"[{item.rel_path}{unverified}]"


def _evidence_block(evidence: list[Evidence]) -> str:
    """Renders the numbered evidence list for the synthesize prompt.

    The model is shown ONLY the number, claim, and quote -- never the rendered
    citation (file/heading/page). Showing it invites the model to copy that
    text into prose, which then collides with the assembler's own expansion of
    ``[n]`` and produces doubled citations.
    """
    lines = []
    for item in evidence:
        lines.append(
            f"[{item.number}]\n"
            f"    claim: {item.finding.claim}\n"
            f"    quote: {item.finding.quote}"
        )
    return "\n".join(lines)


def _is_claim_like(sentence: str) -> bool:
    """Heuristic for a sentence that asserts something needing a citation.

    Conservative: must contain letters and at least four words. This avoids
    flagging headers, list labels, and short connective fragments while
    catching declarative claims a model emitted without any evidence number.
    """
    stripped = sentence.strip()
    if not any(ch.isalpha() for ch in stripped):
        return False
    return len(stripped.split()) >= 4


_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_BULLET_RE = re.compile(r"^(\s*)[\*\-\+]\s+")


def _is_structural(stripped_line: str) -> bool:
    """A line that is a heading or a lead-in (ends with ':') -- not a claim."""
    return stripped_line.startswith("#") or stripped_line.endswith(":")


def _clean_markdown(line: str) -> str:
    """Strips terminal-noisy Markdown decoration, keeping text and indentation.

    Headings lose their ``#`` markers; bullet markers normalize to ``- ``. The
    synthesize prompt already asks for plain prose; this is the safety net for
    when the model emits Markdown anyway.
    """
    line = _MD_HEADING_RE.sub("", line)
    return _MD_BULLET_RE.sub(r"\1- ", line)


def resolve_citations(
    answer: str, evidence: list[Evidence]
) -> tuple[str, list[str], set[int], int, int]:
    """Replaces ``[n]`` markers with rendered citations and enforces coverage.

    Two failure modes are caught and made visible, never silent:
    1. A ``[n]`` that does not resolve to an evidence item becomes
       ``[uncited]``.
    2. A claim-like sentence carrying NO citation marker at all has
       ``[uncited]`` appended -- the "no citation, no claim" guarantee, which
       a per-number check alone does not provide.

    The answer's own line structure (paragraphs, any lists) is preserved:
    resolution is line by line, with sentence-level coverage WITHIN each line,
    so the rendered output stays readable instead of collapsing to one line.

    Args:
        answer: Model output containing numeric cites.
        evidence: The numbered evidence list.

    Returns:
        ``(rendered_text, bad_sentences, used_numbers, resolved, unresolved)``
        where ``bad_sentences`` holds every sentence marked ``[uncited]``.
    """
    by_number = {e.number: e for e in evidence}
    used: set[int] = set()
    counts = {"ok": 0, "bad": 0}

    def _sub(match: re.Match[str]) -> str:
        rendered: list[str] = []
        seen: set[str] = set()  # collapse [n, n] / repeated references
        for token in match.group(1).split(","):
            n = int(token)
            if n in by_number:
                used.add(n)
                counts["ok"] += 1
                cite = render_cite(by_number[n])
                if cite not in seen:
                    seen.add(cite)
                    rendered.append(cite)
            else:
                counts["bad"] += 1
                rendered.append("[uncited]")
        return " ".join(rendered)

    out_lines: list[str] = []
    bad: list[str] = []
    for raw_line in answer.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            out_lines.append("")
            continue
        structural = _is_structural(stripped)
        parts: list[str] = []
        # Sentence granularity within the line lets coverage be judged per
        # claim; the split keeps terminal punctuation with each sentence.
        for sentence in re.split(r"(?<=[.!?])\s+", raw_line):
            if not sentence.strip():
                continue
            had_cite = _CITE_RE.search(sentence) is not None
            rendered = _CITE_RE.sub(_sub, sentence)
            if not had_cite and not structural and _is_claim_like(rendered):
                counts["bad"] += 1
                rendered = f"{rendered.rstrip()} [uncited]"
            if "[uncited]" in rendered:
                bad.append(rendered.strip())
            parts.append(rendered)
        out_lines.append(_clean_markdown(" ".join(parts)))

    rendered_text = "\n".join(out_lines).strip()
    return rendered_text, bad, used, counts["ok"], counts["bad"]


def sources_block(evidence: list[Evidence], used: set[int]) -> str:
    """Renders the terminal Sources block for cited files."""
    by_file: dict[str, set[str]] = {}
    for item in evidence:
        if item.number not in used:
            continue
        heading = item.finding.heading_path[-1] if item.finding.heading_path else ""
        if not heading or _heading_is_filename(heading, item.rel_path):
            heading = (
                f"p.{item.finding.page_no}" if item.finding.page_no else "(whole file)"
            )
        by_file.setdefault(item.rel_path, set()).add(heading)
    lines = ["Sources:"]
    for rel_path in sorted(by_file):
        sections = ", ".join(sorted(by_file[rel_path]))
        lines.append(f"  {rel_path} — {sections}")
    return "\n".join(lines)


async def synthesize(
    question: str,
    results: list[tuple[str, ExtractionResult]],
    client: LLMTaskClient,
    config: AppConfig,
) -> CitedAnswer:
    """Produces the final cited answer from extraction results.

    Evidence exceeding the synthesize window is reduced hierarchically:
    file groups are partial-synthesized with global numbers preserved,
    then merged. Unresolvable citations get one repair retry, then are
    marked ``[uncited]``.

    Args:
        question: The user question, verbatim.
        results: ``(rel_path, extraction)`` pairs.
        client: Task client.
        config: Application configuration.

    Returns:
        The final answer; empty evidence yields an explicit no-answer text.
    """
    evidence = number_evidence(results)
    if not evidence:
        return CitedAnswer(
            text=(
                "No relevant content was found in the corpus for this "
                "question (no file passed extraction)."
            ),
            sources="Sources: none",
            uncited_claims=[],
        )

    block = _evidence_block(evidence)
    synth_cfg = config.tasks[TaskName.SYNTHESIZE]
    window = synth_cfg.context_window
    # Reserve fixed scaffolding AND the model's own completion. Sizing the
    # evidence block to the whole window leaves no room for output, and the
    # server then returns an EMPTY completion -- which previously surfaced as a
    # blank answer with an empty Sources block.
    budget_tokens = max(window - _REDUCE_OVERHEAD_TOKENS - synth_cfg.max_tokens, 2_000)
    if client.count_tokens(TaskName.SYNTHESIZE, block) > budget_tokens:
        block = await _hierarchical_reduce(question, evidence, budget_tokens, client)

    answer = await _synthesize_once(question, block, client)
    text, bad, used, ok_n, bad_n = resolve_citations(answer, evidence)
    if bad:  # one repair round trip, then surface what remains
        repair = (
            f"{synth_prompt.SYSTEM}\n\nYour previous answer had citation "
            f"problems: every sentence that makes a claim must end with a "
            f"valid evidence citation [n] (valid numbers are 1..{len(evidence)}), "
            f"and you must not cite numbers outside that range. Rewrite the "
            f"answer so every claim carries a valid citation; drop any claim "
            f"you cannot support with the given evidence."
        )
        answer = await _synthesize_once(question, block, client, system=repair)
        text, bad, used, ok_n, bad_n = resolve_citations(answer, evidence)

    # Guarantee a response. If the model returned nothing usable (e.g. an
    # over-window completion came back empty), assemble a grounded digest
    # straight from the evidence so findings are never silently dropped.
    if not text.strip():
        text, used = _fallback_digest(evidence)
        bad, ok_n, bad_n = [], len(used), 0

    return CitedAnswer(
        text=text,
        sources=sources_block(evidence, used),
        uncited_claims=bad,
        resolved_cites=ok_n,
        unresolved_cites=bad_n,
    )


async def _synthesize_once(
    question: str,
    evidence_block: str,
    client: LLMTaskClient,
    system: str | None = None,
) -> str:
    """Runs one synthesize completion."""
    completion = await client.complete(
        TaskName.SYNTHESIZE,
        [
            {"role": "system", "content": system or synth_prompt.SYSTEM},
            {
                "role": "user",
                "content": (f"Question:\n{question}\n\nEvidence:\n{evidence_block}"),
            },
        ],
    )
    return completion.text


async def _hierarchical_reduce(
    question: str,
    evidence: list[Evidence],
    budget_tokens: int,
    client: LLMTaskClient,
) -> str:
    """Condenses oversize evidence into a digest that fits the synth window.

    Evidence is packed into token-bounded batches -- NOT grouped by file, so a
    single huge file is still split -- and each batch is condensed while
    preserving the global ``[n]`` numbers. If the concatenated digests still
    exceed budget, the digests themselves are condensed, repeating until they
    fit or no further progress is made. Every model call is guarded: an empty
    return falls back to a deterministic claim digest so evidence numbers are
    never lost to the merge.
    """
    digests = await _condense_evidence(question, evidence, budget_tokens, client)
    merged = "\n\n".join(d for d in digests if d.strip())
    depth = 0
    while (
        merged
        and client.count_tokens(TaskName.SYNTHESIZE, merged) > budget_tokens
        and depth < _MAX_REDUCE_DEPTH
    ):
        digests = await _condense_texts(question, digests, budget_tokens, client)
        new_merged = "\n\n".join(d for d in digests if d.strip())
        if new_merged == merged:  # no progress -- stop rather than loop
            break
        merged = new_merged
        depth += 1
    return merged


def _pack_by_tokens(
    units: list[str], budget_tokens: int, client: LLMTaskClient
) -> list[list[int]]:
    """Greedily groups unit indices so each group's text fits ``budget_tokens``.

    A single unit larger than the budget goes in its own group (it cannot be
    split further here); the caller's model call may still truncate it, but the
    empty-completion guard covers that.
    """
    groups: list[list[int]] = []
    cur: list[int] = []
    cur_tok = 0
    for i, text in enumerate(units):
        tok = client.count_tokens(TaskName.SYNTHESIZE, text)
        if cur and cur_tok + tok > budget_tokens:
            groups.append(cur)
            cur, cur_tok = [], 0
        cur.append(i)
        cur_tok += tok
    if cur:
        groups.append(cur)
    return groups


async def _condense_evidence(
    question: str,
    evidence: list[Evidence],
    budget_tokens: int,
    client: LLMTaskClient,
) -> list[str]:
    """Condenses evidence into per-batch digests preserving ``[n]`` numbers."""
    blocks = [_evidence_block([item]) for item in evidence]
    digests: list[str] = []
    for group in _pack_by_tokens(blocks, budget_tokens, client):
        batch = [evidence[i] for i in group]
        completion = await client.complete(
            TaskName.SYNTHESIZE,
            [
                {"role": "system", "content": synth_prompt.PARTIAL_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{question}\n\nEvidence:\n{_evidence_block(batch)}"
                    ),
                },
            ],
        )
        digests.append(completion.text.strip() or _claims_digest(batch))
    return digests


async def _condense_texts(
    question: str,
    digests: list[str],
    budget_tokens: int,
    client: LLMTaskClient,
) -> list[str]:
    """Condenses prose digests further when their concatenation still overflows.

    Preserves ``[n]`` numbers. Empty returns fall back to a token-bounded
    truncation of the input so the outer loop keeps making progress.
    """
    out: list[str] = []
    for group in _pack_by_tokens(digests, budget_tokens, client):
        joined = "\n\n".join(digests[i] for i in group)
        completion = await client.complete(
            TaskName.SYNTHESIZE,
            [
                {"role": "system", "content": synth_prompt.PARTIAL_SYSTEM},
                {
                    "role": "user",
                    "content": (f"Question:\n{question}\n\nDigests:\n{joined}"),
                },
            ],
        )
        out.append(completion.text.strip() or joined[: budget_tokens * 4])
    return out


def _claims_digest(items: list[Evidence]) -> str:
    """Deterministic digest of a batch: each claim with its evidence number."""
    parts = [
        f"{item.finding.claim.strip()} [{item.number}]"
        for item in items
        if item.finding.claim.strip()
    ]
    return " ".join(parts)


def _fallback_digest(evidence: list[Evidence]) -> tuple[str, set[int]]:
    """Assembles a grounded answer directly from evidence, no model involved.

    Used only when synthesis returns nothing usable, so the user always gets
    the extracted findings with their citations rather than a blank response.
    Returns ``(rendered_text, used_numbers)``.
    """
    by_file: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_file.setdefault(item.rel_path, []).append(item)
    # Trailing ':' marks this as a lead-in so coverage does not flag it.
    lines = [
        "Synthesis did not return a usable answer; the extracted findings are "
        "listed below with their sources:"
    ]
    for rel_path in sorted(by_file):
        lines.append("")
        for item in by_file[rel_path]:
            claim = item.finding.claim.strip()
            if claim:
                lines.append(f"- {claim} [{item.number}]")
    text, _bad, used, _ok, _bad_n = resolve_citations("\n".join(lines), evidence)
    return text, used