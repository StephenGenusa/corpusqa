"""Direct display of extraction hits -- the default query output.

For retrieval queries ("find any place where X is discussed") the extraction
step already produced the answer: a verbatim quote, its real heading location,
and a verified/unverified flag. This module renders those findings straight to
the reader, grouped by file and heading, with no second LLM call. It is the
trustworthy path -- nothing here paraphrases or re-describes the source.

Synthesis (composing a single prose answer across findings) remains available
behind an explicit flag for questions that genuinely require combining
evidence; see ``synthesizer.synthesize``.
"""

from __future__ import annotations

from dataclasses import dataclass

from corpusqa.query.extractor import ExtractionResult, _normalize_for_match


@dataclass(frozen=True)
class Hit:
    """One finding ready for display.

    Attributes:
        rel_path: Source file path.
        heading_path: Ancestor headings (root to leaf) of the quote's location.
        quote: The verbatim excerpt.
        claim: The model's one-line reason the passage matched.
        verified: Whether ``quote`` was located in the source text.
    """

    rel_path: str
    heading_path: tuple[str, ...]
    quote: str
    claim: str
    verified: bool
    page_no: int | None = None


def collect_hits(
    results: list[tuple[str, ExtractionResult]],
) -> list[Hit]:
    """Flattens relevant findings into deduplicated hits, in document order.

    Overlapping sections of a large file routinely surface the same passage
    more than once; duplicates are collapsed per file by normalized quote
    (falling back to the claim when a quote is empty), matching how synthesis
    numbers evidence so the two views stay consistent.
    """
    hits: list[Hit] = []
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
            hits.append(
                Hit(
                    rel_path=rel_path,
                    heading_path=tuple(finding.heading_path),
                    quote=finding.quote.strip(),
                    claim=finding.claim.strip(),
                    verified=finding.quote_verified,
                    page_no=finding.page_no,
                )
            )
    return hits


def _relevant_without_hits(
    results: list[tuple[str, ExtractionResult]], files_with_hits: set[str]
) -> list[str]:
    """Files judged relevant that yielded no usable findings (a real signal)."""
    out: list[str] = []
    for rel_path, result in results:
        if result.relevant and rel_path not in files_with_hits and rel_path not in out:
            out.append(rel_path)
    return out


def render_hits(
    question: str,
    results: list[tuple[str, ExtractionResult]],
) -> str:
    """Renders extraction hits as grouped, plain-text output for a terminal.

    Groups by file, then by heading path, showing each verbatim quote with its
    'why it matched' claim and an unverified tag when the quote could not be
    located in the source. A header summarizes counts (including how many hits
    are unverified) so source-traceability is visible at a glance.
    """
    hits = collect_hits(results)
    if not hits:
        empty_relevant = _relevant_without_hits(results, set())
        if empty_relevant:
            lines = [
                f"No quotable hits for: \"{question}\"",
                "",
                "These files were judged relevant but produced no verbatim "
                "findings (try --relevance balanced, or --synthesize for a "
                "composed answer):",
            ]
            lines += [f"  {p}" for p in sorted(empty_relevant)]
            return "\n".join(lines)
        return f"No hits found for: \"{question}\""

    files = _ordered_unique(h.rel_path for h in hits)
    unverified = sum(1 for h in hits if not h.verified)
    quality = (
        f" ({len(hits) - unverified} verified, {unverified} unverified)"
        if unverified
        else ""
    )
    plural = "s" if len(hits) != 1 else ""
    fplural = "s" if len(files) != 1 else ""
    lines = [
        f"{len(hits)} hit{plural} across {len(files)} file{fplural}{quality} "
        f"for: \"{question}\"",
    ]

    by_file = _group_by_file(hits)
    for rel_path in files:
        lines.append("")
        lines.append(rel_path)
        for heading, group in _group_by_heading(by_file[rel_path]):
            lines.append(f"  § {heading}" if heading else "  (no heading)")
            for hit in group:
                tag = "" if hit.verified else "  (unverified)"
                page = f" (p.{hit.page_no})" if hit.page_no else ""
                lines.append(f'    "{hit.quote}"{page}{tag}')
                if hit.claim:
                    lines.append(f"      why it matched: {hit.claim}")

    empty_relevant = _relevant_without_hits(results, set(files))
    if empty_relevant:
        lines.append("")
        lines.append("Also judged relevant but with no verbatim findings:")
        lines += [f"  {p}" for p in sorted(empty_relevant)]

    return "\n".join(lines)


def _ordered_unique(items) -> list[str]:
    """Preserves first-seen order while removing duplicates."""
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _group_by_file(hits: list[Hit]) -> dict[str, list[Hit]]:
    grouped: dict[str, list[Hit]] = {}
    for hit in hits:
        grouped.setdefault(hit.rel_path, []).append(hit)
    return grouped


def _group_by_heading(hits: list[Hit]) -> list[tuple[str, list[Hit]]]:
    """Groups a file's hits by their heading path, preserving first-seen order."""
    order: list[str] = []
    groups: dict[str, list[Hit]] = {}
    for hit in hits:
        heading = " > ".join(hit.heading_path)
        if heading not in groups:
            groups[heading] = []
            order.append(heading)
        groups[heading].append(hit)
    return [(heading, groups[heading]) for heading in order]