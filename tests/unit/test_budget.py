"""Pre-flight estimation math (M3)."""

from __future__ import annotations

import math
from pathlib import Path

from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.query.budget import estimate_pass1, estimate_query, route_shard_limits

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def test_estimate_uses_per_task_overrides() -> None:
    config = load_config(EXAMPLE)
    # extract is local with 0.0 overrides; force synthesize to known rates
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_in = 3.0
    config.tasks[TaskName.SYNTHESIZE].cost_per_mtok_out = 15.0
    est = estimate_query([10_000, 30_000], config)
    assert est.candidate_files == 2
    assert est.per_task_usd["extract"] == 0.0
    # synth in = 2 * extract max_tokens (2048) = 4096; out = 8192
    expected = (4096 * 3.0 + 8192 * 15.0) / 1_000_000
    assert est.per_task_usd["synthesize"] == round(expected, 4)
    assert abs(est.total_usd - expected) < 1e-3


def test_estimate_pass1_covers_cataloging_and_routing() -> None:
    config = load_config(EXAMPLE)
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_in = 3.0
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_out = 15.0
    config.tasks[TaskName.QUERY_ROUTE].cost_per_mtok_in = 0.3
    config.tasks[TaskName.QUERY_ROUTE].cost_per_mtok_out = 2.5

    pending = [10_000, 30_000]  # two files still need cards
    total_cards = 5  # routing reads every card, pending or not
    est = estimate_pass1(pending, total_cards, config)

    cat_cfg = config.tasks[TaskName.CATALOG_SUMMARIZE]
    cat_out = cat_cfg.max_tokens * 2
    expected_cat = (40_000 * 3.0 + cat_out * 15.0) / 1_000_000

    route_in = total_cards * cat_cfg.max_tokens
    input_budget, max_cards = route_shard_limits(config)
    shards = max(math.ceil(route_in / input_budget), math.ceil(total_cards / max_cards))
    route_out = config.tasks[TaskName.QUERY_ROUTE].max_tokens * shards
    expected_route = (route_in * 0.3 + route_out * 2.5) / 1_000_000

    assert est.per_task_usd["catalog_summarize"] == round(expected_cat, 4)
    assert est.per_task_usd["query_route"] == round(expected_route, 4)
    assert abs(est.total_usd - (expected_cat + expected_route)) < 1e-3
    assert est.candidate_files == 2
    assert est.total_input_tokens == 40_000 + route_in


def test_estimate_pass1_empty_catalog_is_free_when_no_cards() -> None:
    config = load_config(EXAMPLE)
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_in = 3.0
    config.tasks[TaskName.CATALOG_SUMMARIZE].cost_per_mtok_out = 15.0
    est = estimate_pass1([], 0, config)
    assert est.total_usd == 0.0
    assert est.per_task_usd["query_route"] == 0.0


def test_route_shard_limits_output_bound() -> None:
    config = load_config(EXAMPLE)
    _, max_cards = route_shard_limits(config)
    route_cfg = config.tasks[TaskName.QUERY_ROUTE]
    # every shard's decisions must fit the output budget with reserve
    assert max_cards * 60 + 200 <= route_cfg.max_tokens
    assert max_cards >= 1
