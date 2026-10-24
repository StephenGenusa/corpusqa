"""Per-token model pricing: the one place rates are resolved.

Shared by the pre-flight estimator and the post-call ledger so the two can
never disagree. A model nobody can price is reported as *unpriced* (None),
never silently as free: only a provider on a loopback address is assumed to
cost nothing.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import litellm

from corpusqa.config.schema import ProviderConfig, TaskConfig

Rate = tuple[float, float]
"""USD per million tokens, ``(input, output)``."""


def cost_map_rate(model: str) -> Rate | None:
    """Looks a model up in LiteLLM's cost map.

    Tries the full string, then without the provider prefix, then the last
    path segment (``openrouter/google/x`` -> ``google/x`` -> ``x``).

    Returns:
        The rate, or None when the cost map does not know the model.
    """
    for key in (model, model.split("/", 1)[-1], model.split("/")[-1]):
        info = litellm.model_cost.get(key)
        if info and ("input_cost_per_token" in info or "output_cost_per_token" in info):
            return (
                float(info.get("input_cost_per_token") or 0.0) * 1_000_000,
                float(info.get("output_cost_per_token") or 0.0) * 1_000_000,
            )
    return None


def is_loopback(api_base: str | None) -> bool:
    """True when ``api_base`` points at this machine (localhost/127.x/::1)."""
    if not api_base:
        return False
    host = urlparse(api_base).hostname
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def task_rate(task_cfg: TaskConfig, provider: ProviderConfig) -> Rate | None:
    """Resolves the rate for a task's primary model.

    Order: per-task config override, LiteLLM's cost map, then free when the
    provider is on loopback.

    Returns:
        The rate, or None when the model is unpriced.
    """
    if task_cfg.cost_per_mtok_in is not None or task_cfg.cost_per_mtok_out is not None:
        return (task_cfg.cost_per_mtok_in or 0.0, task_cfg.cost_per_mtok_out or 0.0)
    rate = cost_map_rate(task_cfg.model)
    if rate is not None:
        return rate
    if is_loopback(provider.api_base):
        return (0.0, 0.0)
    return None
