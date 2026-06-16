"""Pass-1 candidate selection: recall-biased catalog routing.

Card lists exceeding the route model's window are sharded across calls and
the results unioned. The vector channel described in v0.1 was not built
(measurement left it no recall gap to close); routing is the sole pass-1
selector (design doc section 2.1, 4.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.catalog.summarizer import CatalogCard
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.llm.structured import complete_structured
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.prompts import route as route_prompt
from corpusqa.query.budget import route_shard_limits

_log = logging.getLogger("corpusqa.query")


class RouteDecision(BaseModel):
    """Routing verdict for one file.

    Attributes:
        file_hash: The file judged.
        include: Whether the file is plausibly relevant (recall-biased).
        reason: One-line justification, kept for logging and evals.
    """

    file_hash: str
    include: bool
    reason: str


class RouteResponse(BaseModel):
    """Structured output of one routing call."""

    decisions: list[RouteDecision] = Field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    """A file selected for pass-2 extraction.

    Attributes:
        file: The file row.
        reason: Why it was selected (routing reason, or the bypass note).
    """

    file: FileRow
    reason: str


def _render_card(file: FileRow, card: CatalogCard) -> str:
    """Renders one inventory card for the routing prompt."""

    def block(label: str, items: list[str]) -> str:
        return f"{label}: " + ("; ".join(items) if items else "-")

    terms = "; ".join(f"{t.term} = {t.sense}" for t in card.terms) or "-"
    return (
        f"FILE {file.file_hash}\n"
        f"path: {file.rel_path} | type: {card.doc_type or '-'}\n"
        f"{block('claims', card.claims)}\n"
        f"terms: {terms}\n"
        f"{block('implicit', card.implicit_topics)}\n"
        f"{block('answers', card.answerable)}\n"
        f"{block('absences', card.absences)}\n"
        f"{block('entities', card.entities)}\n"
    )


def _shard_cards(
    rendered: list[str], client: LLMTaskClient, config: AppConfig
) -> list[list[int]]:
    """Groups card indices into shards the route model can fully process.

    A shard is bounded in BOTH directions: its cards must fit the input
    window, and its decisions must fit ``max_tokens`` of output. The output
    bound is the binding one for long-context route models (a 1M-token
    window holds thousands of cards whose decisions cannot fit a 4K output
    budget; the JSON would truncate mid-list and fail validation).
    """
    input_budget, max_cards = route_shard_limits(config)
    shards: list[list[int]] = [[]]
    used = 0
    for index, text in enumerate(rendered):
        tokens = client.count_tokens(TaskName.QUERY_ROUTE, text)
        full = used + tokens > input_budget or len(shards[-1]) >= max_cards
        if shards[-1] and full:
            shards.append([])
            used = 0
        shards[-1].append(index)
        used += tokens
    return shards


async def route(
    question: str,
    definition: str | None,
    store: CatalogStore,
    client: LLMTaskClient,
    config: AppConfig,
) -> list[Candidate]:
    """Selects candidate files for the question via catalog routing.

    Args:
        question: The user question, verbatim.
        definition: Optional user topic definition, verbatim.
        store: Open catalog store.
        client: Task client.
        config: Application configuration.

    Returns:
        Candidates in catalog order. Files the router judged irrelevant are
        excluded; pass 2 corrects any remaining false positives.
    """
    pairs = store.all_cards()
    if not pairs:
        return []
    rendered = [_render_card(f, c) for f, c in pairs]
    by_hash = {f.file_hash: f for f, _ in pairs}

    included: dict[str, str] = {}
    decided: set[str] = set()
    definition_block = (
        f"The user defines the topic as follows; judge relevance against "
        f"THIS definition, not the common meaning:\n{definition}\n\n"
        if definition
        else ""
    )
    for shard in _shard_cards(rendered, client, config):
        cards_block = "\n".join(rendered[i] for i in shard)
        messages = [
            {"role": "system", "content": route_prompt.SYSTEM},
            {
                "role": "user",
                "content": route_prompt.TEMPLATE.format(
                    question=question,
                    definition_block=definition_block,
                    cards_block=cards_block,
                ),
            },
        ]
        response = await complete_structured(
            client, TaskName.QUERY_ROUTE, messages, RouteResponse
        )
        for decision in response.decisions:
            if decision.file_hash not in by_hash:
                continue
            decided.add(decision.file_hash)
            if decision.include:
                included.setdefault(decision.file_hash, decision.reason)

    # The prompt demands a decision for EVERY card, but a model may still
    # omit some. An omitted card must not become a silent exclusion (the
    # exact "silent data loss" the recall-biased design exists to prevent),
    # so undecided files are included by default and logged.
    for file_hash in by_hash:
        if file_hash not in decided:
            _log.warning(
                "route: no decision returned for %s; included by default",
                by_hash[file_hash].rel_path,
            )
            included.setdefault(
                file_hash, "no routing decision returned; included by default"
            )

    # Candidates in catalog order (pairs order), per the contract above.
    return [
        Candidate(file=f, reason=included[f.file_hash])
        for f, _ in pairs
        if f.file_hash in included
    ]
