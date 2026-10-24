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


def _cost_map_keys(model: str) -> tuple[str, str, str]:
    """Returns cost-map lookup keys, most specific first.

    The full string, then without the provider prefix, then the last path
    segment (``openrouter/google/x`` -> ``google/x`` -> ``x``).
    """
    return (model, model.split("/", 1)[-1], model.split("/")[-1])


def cost_map_window(model: str) -> int | None:
    """Looks up a model's input window in LiteLLM's cost map.

    Returns:
        ``max_input_tokens``, or None when the cost map does not know it.
    """
    for key in _cost_map_keys(model):
        window = (litellm.model_cost.get(key) or {}).get("max_input_tokens")
        if window:
            return int(window)
    return None


def cost_map_rate(model: str) -> Rate | None:
    """Looks a model up in LiteLLM's cost map (see ``_cost_map_keys``).

    Returns:
        The rate, or None when the cost map does not know the model.
    """
    for key in _cost_map_keys(model):
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

    Each side (input, output) resolves on its own, in order: the per-task
    config override, LiteLLM's cost map, then free when the provider is on
    loopback. Setting only one override never prices the other side at $0.

    Returns:
        The rate, or None when either side is unpriced.
    """
    known = cost_map_rate(task_cfg.model)
    if known is None and is_loopback(provider.api_base):
        known = (0.0, 0.0)
    rate_in = task_cfg.cost_per_mtok_in
    rate_out = task_cfg.cost_per_mtok_out
    if rate_in is None:
        rate_in = known[0] if known is not None else None
    if rate_out is None:
        rate_out = known[1] if known is not None else None
    if rate_in is None or rate_out is None:
        return None
    return (rate_in, rate_out)
