"""Token budgeting and pre-flight cost estimation. Implemented in M3.

Window sizes come from per-task config (authoritative), token counts from
``LLMTaskClient.count_tokens``. Estimates above ``budget.confirm_above_usd``
require interactive confirmation unless ``--yes``; the budgeter aborts
before spend, not after.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from corpusqa.config.schema import AppConfig, TaskConfig, TaskName

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
        candidate_files: Number of files entering pass 2.
        total_input_tokens: Projected pass-2 + synthesize input tokens.
    """

    per_task_usd: dict[str, float] = field(default_factory=dict)
    total_usd: float = 0.0
    candidate_files: int = 0
    total_input_tokens: int = 0


def _rate(task_cfg: TaskConfig) -> tuple[float, float]:
    """Returns (usd_per_mtok_in, usd_per_mtok_out) for a task.

    Per-task config overrides are authoritative; otherwise LiteLLM's cost
    map is consulted; unknown models price at 0 (and the estimate says so
    implicitly -- design doc risk #6 keeps overrides as the reliable path).
    """
    if task_cfg.cost_per_mtok_in is not None or task_cfg.cost_per_mtok_out is not None:
        return (task_cfg.cost_per_mtok_in or 0.0, task_cfg.cost_per_mtok_out or 0.0)
    try:
        import litellm

        info = litellm.model_cost.get(task_cfg.model) or litellm.model_cost.get(
            task_cfg.model.split("/")[-1], {}
        )
        return (
            float(info.get("input_cost_per_token", 0.0)) * 1_000_000,
            float(info.get("output_cost_per_token", 0.0)) * 1_000_000,
        )
    except Exception:  # noqa: BLE001 -- pricing lookup is best-effort
        return (0.0, 0.0)


def estimate_query(
    file_tokens: list[int],
    config: AppConfig,
) -> CostEstimate:
    """Projects pass-2 + synthesize cost before any spend.

    Output tokens are estimated at each task's ``max_tokens`` ceiling --
    deliberately conservative: the estimate's job is to prevent surprise
    spend, not to be flattering.

    Args:
        file_tokens: Extract-model token count per candidate file.
        config: Application configuration.

    Returns:
        The projection used for the confirmation gate and ``estimate``.
    """
    extract_cfg = config.tasks[TaskName.EXTRACT]
    synth_cfg = config.tasks[TaskName.SYNTHESIZE]
    ex_in_rate, ex_out_rate = _rate(extract_cfg)
    sy_in_rate, sy_out_rate = _rate(synth_cfg)

    extract_in = sum(file_tokens)
    extract_out = extract_cfg.max_tokens * len(file_tokens)
    synth_in = extract_out  # findings feed the reduce step
    synth_out = synth_cfg.max_tokens

    extract_usd = (extract_in * ex_in_rate + extract_out * ex_out_rate) / 1_000_000
    synth_usd = (synth_in * sy_in_rate + synth_out * sy_out_rate) / 1_000_000
    return CostEstimate(
        per_task_usd={
            TaskName.EXTRACT.value: round(extract_usd, 4),
            TaskName.SYNTHESIZE.value: round(synth_usd, 4),
        },
        total_usd=round(extract_usd + synth_usd, 4),
        candidate_files=len(file_tokens),
        total_input_tokens=extract_in + synth_in,
    )


def estimate_pass1(
    pending_card_tokens: list[int],
    total_card_count: int,
    config: AppConfig,
) -> CostEstimate:
    """Projects card-generation + routing cost before any pass-1 spend.

    Cataloging was historically the largest unguarded spend: a fresh corpus
    sends every file's (truncated) markdown to the catalog model with no
    gate. This estimate closes that hole; ``run_query``/``run_estimate``
    check it against the budget threshold BEFORE generating cards or
    routing.

    Args:
        pending_card_tokens: Catalog-model token count of the (already
            truncated-to-budget) markdown for each file still lacking a
            card. Empty when the catalog is fully built.
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
    cat_in_rate, cat_out_rate = _rate(catalog_cfg)
    rt_in_rate, rt_out_rate = _rate(route_cfg)

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
            TaskName.CATALOG_SUMMARIZE.value: round(catalog_usd, 4),
            TaskName.QUERY_ROUTE.value: round(route_usd, 4),
        },
        total_usd=round(catalog_usd + route_usd, 4),
        candidate_files=len(pending_card_tokens),
        total_input_tokens=catalog_in + route_in,
    )
