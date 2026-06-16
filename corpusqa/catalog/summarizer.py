"""LLM catalog-card generation (design doc section 4.2)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import CorpusQAError
from corpusqa.llm.structured import complete_structured
from corpusqa.llm.tasks import LLMTaskClient

if TYPE_CHECKING:  # store imports CatalogCard from here; avoid a cycle
    from corpusqa.catalog.store import CatalogStore, FileRow

_PROMPT_OVERHEAD_TOKENS = 1_500
# Cap per-field entries in a merged card so a very large file does not produce
# an unbounded card; routing needs representative specifics, not everything.
_MAX_CARD_ITEMS = 50


class TermUse(BaseModel):
    """One term and the sense the document actually uses it in.

    Attributes:
        term: The word or phrase as it appears.
        sense: The in-document meaning, stated specifically (not the
            dictionary meaning).
    """

    term: str
    sense: str


class CatalogCard(BaseModel):
    """Extraction inventory of one file (M5: replaces summary cards).

    Routing needs particulars, not compression: every field is an inventory
    of specifics the router can match a question against. Summaries were
    rejected for collapsing exactly the distinctions custom-definition
    queries depend on.

    Unknown keys are rejected: a card produced against an older schema must
    fail validation visibly, not silently validate to an empty card (the
    failure mode that masked the stale ``--mock`` harness after M5).

    Attributes:
        claims: Specific assertions the document makes, one per entry.
        terms: Key terms with their in-document sense.
        implicit_topics: Topics discussed without being named explicitly.
        answerable: Concrete questions this document could answer.
        absences: Notable gaps given the document type (what it does NOT
            address that a reader might expect it to).
        entities: Named people, projects, systems, or works.
        doc_type: Free-text document type guess.
    """

    model_config = ConfigDict(extra="forbid")

    claims: list[str] = Field(default_factory=list)
    terms: list[TermUse] = Field(default_factory=list)
    implicit_topics: list[str] = Field(default_factory=list)
    answerable: list[str] = Field(default_factory=list)
    absences: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    doc_type: str | None = None


@dataclass
class CardReport:
    """Summary of a card-generation run.

    Attributes:
        generated: Cards written this run.
        hierarchical: Files too large for one card call, summarized by
            chunk-then-merge (map over budget-sized chunks, deterministic
            union of the partial cards) instead of being truncated.
        failed: ``(rel_path, error)`` per file whose card generation failed;
            failures never abort the batch.
    """

    generated: int = 0
    hierarchical: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def card_budget_chars(config: AppConfig) -> int:
    """Computes the markdown char budget for one card call.

    Public because the pass-1 cost estimator must truncate identically to
    ``generate_cards`` or its token counts diverge from real spend.
    """
    window = config.tasks[TaskName.CATALOG_SUMMARIZE].context_window
    usable = max(window - _PROMPT_OVERHEAD_TOKENS, 1_000)
    return usable * 4  # ~4 chars/token; conservative for budgeting


def _chunk_for_cards(markdown: str, budget: int) -> list[str]:
    """Splits markdown into budget-sized character chunks for map-reduce."""
    return [markdown[i : i + budget] for i in range(0, len(markdown), budget)]


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving dedup of stripped, non-empty strings, capped."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        norm = item.strip()
        key = norm.casefold()
        if norm and key not in seen:
            seen.add(key)
            out.append(norm)
        if len(out) >= _MAX_CARD_ITEMS:
            break
    return out


def _merge_cards(cards: list["CatalogCard"]) -> "CatalogCard":
    """Deterministically unions partial cards into one (no LLM call).

    Each list field is concatenated then deduplicated (case-insensitive,
    order-preserving, capped); ``terms`` dedup on the term; ``doc_type`` takes
    the first non-empty. Plain code, so the merge cannot hallucinate or drift.
    """
    terms: list[TermUse] = []
    seen_terms: set[str] = set()
    for card in cards:
        for term in card.terms:
            key = term.term.strip().casefold()
            if term.term.strip() and key not in seen_terms:
                seen_terms.add(key)
                terms.append(term)
            if len(terms) >= _MAX_CARD_ITEMS:
                break
    doc_type = next((c.doc_type for c in cards if c.doc_type), None)
    return CatalogCard(
        claims=_dedup([c for card in cards for c in card.claims]),
        terms=terms,
        implicit_topics=_dedup([t for card in cards for t in card.implicit_topics]),
        answerable=_dedup([a for card in cards for a in card.answerable]),
        absences=_dedup([a for card in cards for a in card.absences]),
        entities=_dedup([e for card in cards for e in card.entities]),
        doc_type=doc_type,
    )


async def _card_for_text(
    client: LLMTaskClient, rel_path: str, text: str
) -> "CatalogCard":
    """One catalog-card LLM call over a chunk (or whole file)."""
    from corpusqa.prompts import catalog_card as card_prompt

    messages = [
        {"role": "system", "content": card_prompt.SYSTEM},
        {
            "role": "user",
            "content": card_prompt.TEMPLATE.format(
                rel_path=rel_path, doc_markdown=text
            ),
        },
    ]
    return await complete_structured(
        client, TaskName.CATALOG_SUMMARIZE, messages, CatalogCard
    )


async def generate_cards(
    store: CatalogStore,
    cache_dir: Path,
    client: LLMTaskClient,
    config: AppConfig,
) -> CardReport:
    """Generates catalog cards for all files lacking one.

    Cards are written to the store as each completes (crash-resumable);
    a rerun simply continues with the remainder.

    Args:
        store: Open catalog store.
        cache_dir: Parse-cache directory holding ``<hash>.md`` files.
        client: Task client (per-provider concurrency lives inside it).
        config: Application configuration.

    Returns:
        A per-run report; per-file failures are captured, not raised.
    """
    pending = store.files_needing_cards()
    report = CardReport()
    budget = card_budget_chars(config)

    async def one(row: FileRow) -> None:
        try:
            markdown = (cache_dir / f"{row.file_hash}.md").read_text(encoding="utf-8")
            if len(markdown) > budget:
                # Too big for one pass: summarize each budget-sized chunk, then
                # merge the partial cards deterministically (no extra LLM call).
                chunks = _chunk_for_cards(markdown, budget)
                partials = await asyncio.gather(
                    *(_card_for_text(client, row.rel_path, c) for c in chunks)
                )
                card = _merge_cards(list(partials))
                report.hierarchical += 1
            else:
                card = await _card_for_text(client, row.rel_path, markdown)
            store.upsert_card(
                row.file_hash,
                card,
                config.tasks[TaskName.CATALOG_SUMMARIZE].model,
            )
            report.generated += 1
        except (CorpusQAError, OSError) as exc:
            report.failed.append((row.rel_path, str(exc)))

    await asyncio.gather(*(one(r) for r in pending))
    return report
