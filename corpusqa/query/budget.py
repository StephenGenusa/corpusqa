"""Token budgeting and pre-flight cost estimation. Implemented in M3.

Window sizes come from per-task config (authoritative), token counts from
``LLMTaskClient.count_tokens``. Estimates above ``budget.confirm_above_usd``
require interactive confirmation unless ``--yes``; the budgeter aborts
before spend, not after.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.llm.pricing import Rate, task_rate

# Fixed scaffolding of one extract call (schema instruction + system +
# template + question), shared by section sizing and the pass-2 estimate.
EXTRACT_PROMPT_OVERHEAD_TOKENS = 2_000
# Routing shard limits, shared by the router (actual sharding) and the
# pass-1 estimator (projected shard count) so the two never disagree.
ROUTE_SHARD_OVERHEAD_TOKENS = 2_000
# Output budget consumed per card: one RouteDecision JSON object (64-char
# hash, include flag, one-line reason) is ~50 tokens; 60 is the safe ceiling.
ROUTE_DECISION_TOKENS = 60
ROUTE_OUTPUT_RESERVE_TOKENS = 200  # JSON envelope and slack


def route_shard_limits(config: AppConfig) -> tuple[int, int]:
    """Returns ``(input_token_budget, max_cards_per_shard)`` for routing.

    Sharding must respect BOTH directions of the window: the cards must fit
    the input context, and -- the historically missed constraint -- the
    decisions for every card in the shard must fit ``max_tokens`` of output,
    or the JSON truncates mid-list and the whole call fails validation.
    """
    cfg = config.tasks[TaskName.QUERY_ROUTE]
    input_budget = max(cfg.context_window - ROUTE_SHARD_OVERHEAD_TOKENS, 2_000)
    max_cards = max(
        1, (cfg.max_tokens - ROUTE_OUTPUT_RESERVE_TOKENS) // ROUTE_DECISION_TOKENS
    )
    return input_budget, max_cards


@dataclass(frozen=True)
class CostEstimate:
    """Pre-flight projection for a query run.

    Attributes:
        per_task_usd: Projected cost keyed by task name.
        total_usd: Sum across tasks.
        candidate_files: Number of files pass 2 will read (files with a
            cached verdict make no calls).
        total_input_tokens: Projected pass-2 + synthesize input tokens.
        unpriced_tasks: Tasks whose model has no known price; their cost is
            not in ``total_usd``, so the gate asks before spending.
    """

    per_task_usd: dict[str, float] = field(default_factory=dict)
    total_usd: float = 0.0
    candidate_files: int = 0
    total_input_tokens: int = 0
    unpriced_tasks: tuple[str, ...] = ()


def _rates(config: AppConfig, tasks: tuple[TaskName, ...]) -> list[Rate | None]:
    """Resolves each task's primary-model rate (None when unpriced)."""
    return [
        task_rate(config.tasks[t], config.providers[config.tasks[t].provider])
        for t in tasks
    ]


def _unpriced(tasks: tuple[TaskName, ...], rates: list[Rate | None]) -> tuple[str, ...]:
    return tuple(t.value for t, r in zip(tasks, rates, strict=True) if r is None)


def estimate_query(
    section_tokens: list[list[int]],
    config: AppConfig,
) -> CostEstimate:
    """Projects pass-2 + synthesize cost before any spend.

    Every section is one extract call carrying the prompt scaffolding, and
    output tokens are estimated at each task's ``max_tokens`` ceiling per
    call -- deliberately conservative: the estimate's job is to prevent
    surprise spend, not to be flattering.

    Args:
        section_tokens: For each file pass 2 will read, the extract-model
            token count of each of its sections.
        config: Application configuration.

    Returns:
        The projection used for the confirmation gate and ``estimate``.
    """
    extract_cfg = config.tasks[TaskName.EXTRACT]
    synth_cfg = config.tasks[TaskName.SYNTHESIZE]
    tasks = (TaskName.EXTRACT, TaskName.SYNTHESIZE)
    rates = _rates(config, tasks)
    ex_in_rate, ex_out_rate = rates[0] or (0.0, 0.0)
    sy_in_rate, sy_out_rate = rates[1] or (0.0, 0.0)

    calls = sum(len(sections) for sections in section_tokens)
    extract_in = sum(map(sum, section_tokens)) + calls * EXTRACT_PROMPT_OVERHEAD_TOKENS
    extract_out = extract_cfg.max_tokens * calls
    synth_in = extract_out  # findings feed the reduce step
    synth_out = synth_cfg.max_tokens

    extract_usd = (extract_in * ex_in_rate + extract_out * ex_out_rate) / 1_000_000
    synth_usd = (synth_in * sy_in_rate + synth_out * sy_out_rate) / 1_000_000
    return CostEstimate(
        per_task_usd={
            TaskName.EXTRACT.value: extract_usd,
            TaskName.SYNTHESIZE.value: synth_usd,
        },
        total_usd=extract_usd + synth_usd,
        candidate_files=len(section_tokens),
        total_input_tokens=extract_in + synth_in,
        unpriced_tasks=_unpriced(tasks, rates),
    )


def estimate_pass1(
    pending_card_tokens: list[int],
    total_card_count: int,
    config: AppConfig,
) -> CostEstimate:
    """Projects card-generation + routing cost before any pass-1 spend.

    Cataloging was historically the largest unguarded spend: a fresh corpus
    sends every file's whole markdown to the catalog model with no
    gate. This estimate closes that hole; ``run_query``/``run_estimate``
    check it against the budget threshold BEFORE generating cards or
    routing.

    Args:
        pending_card_tokens: Catalog-model token count of the whole markdown
            of each file still lacking a card (big files are chunked, not
            truncated, so all of it is sent). Empty when the catalog is built.
        total_card_count: Number of cards routing will read (all parseable
            files), pending or not.
        config: Application configuration.

    Returns:
        The projection. Output tokens are estimated at each task's
        ``max_tokens`` ceiling; per-card routing input is bounded by the
        catalog model's ``max_tokens`` (a rendered card cannot exceed the
        output that produced it) -- conservative on both axes by design.
    """
    catalog_cfg = config.tasks[TaskName.CATALOG_SUMMARIZE]
    route_cfg = config.tasks[TaskName.QUERY_ROUTE]
    tasks = (TaskName.CATALOG_SUMMARIZE, TaskName.QUERY_ROUTE)
    rates = _rates(config, tasks)
    cat_in_rate, cat_out_rate = rates[0] or (0.0, 0.0)
    rt_in_rate, rt_out_rate = rates[1] or (0.0, 0.0)

    catalog_in = sum(pending_card_tokens)
    catalog_out = catalog_cfg.max_tokens * len(pending_card_tokens)

    route_in = total_card_count * catalog_cfg.max_tokens
    if total_card_count:
        input_budget, max_cards = route_shard_limits(config)
        shards = max(
            math.ceil(route_in / input_budget),
            math.ceil(total_card_count / max_cards),
        )
    else:
        shards = 0
    route_out = route_cfg.max_tokens * shards

    catalog_usd = (catalog_in * cat_in_rate + catalog_out * cat_out_rate) / 1_000_000
    route_usd = (route_in * rt_in_rate + route_out * rt_out_rate) / 1_000_000
    return CostEstimate(
        per_task_usd={
            TaskName.CATALOG_SUMMARIZE.value: catalog_usd,
            TaskName.QUERY_ROUTE.value: route_usd,
        },
        total_usd=catalog_usd + route_usd,
        candidate_files=len(pending_card_tokens),
        total_input_tokens=catalog_in + route_in,
        unpriced_tasks=_unpriced(tasks, rates),
    )
