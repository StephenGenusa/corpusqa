"""Model listing for the configured providers."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from corpusqa.config.schema import ProviderConfig, provider_kind

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_LM_STUDIO_CHAT_TYPES = frozenset({"llm", "vlm"})


class ModelInfo(BaseModel):
    """One selectable model.

    Attributes:
        id: LiteLLM model string (provider-prefixed).
        loaded: Whether the model is loaded (LM Studio only; else None).
        loaded_context: Context length it is loaded with, if known.
        max_context: Largest context the model supports, if known.
    """

    id: str
    loaded: bool | None = None
    loaded_context: int | None = None
    max_context: int | None = None


class ModelListing(BaseModel):
    """Result of listing a provider's models.

    Attributes:
        models: Models sorted by id, deduplicated.
        error: Human-readable failure, or None.
    """

    models: list[ModelInfo]
    error: str | None = None


def _finish(models: list[ModelInfo]) -> ModelListing:
    unique = {m.id: m for m in models}
    return ModelListing(models=[unique[k] for k in sorted(unique)])


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _data_entries(body: object) -> list[dict[str, Any]]:
    """Returns dict entries of ``body["data"]`` (or ``body`` if a list)."""
    raw = body.get("data") if isinstance(body, dict) else body
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and isinstance(e.get("id"), str)]


async def list_models(
    name: str,
    provider: ProviderConfig,
    timeout_s: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelListing:
    """Lists models a provider serves, as LiteLLM model strings.

    Args:
        name: Provider key in ``providers``.
        provider: Its configuration.
        timeout_s: Per-request timeout in seconds.
        transport: Optional httpx transport (tests inject a mock).

    Returns:
        The listing; network failures yield an empty list plus ``error``.
    """
    kind = provider_kind(name, provider)
    if kind is None:
        return ModelListing(models=[])
    base = (provider.api_base or "").rstrip("/")
    if kind == "openrouter":
        base = base or _OPENROUTER_BASE
    url = base
    try:
        async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as http:
            if kind == "ollama":
                url = f"{base}/api/tags"
                body = await _get(http, url)
                raw = body.get("models") if isinstance(body, dict) else None
                names = [
                    e["name"]
                    for e in (raw if isinstance(raw, list) else [])
                    if isinstance(e, dict) and isinstance(e.get("name"), str)
                ]
                return _finish([ModelInfo(id=f"ollama/{n}") for n in names])
            if kind == "lm_studio":
                url = f"{base.removesuffix('/v1')}/api/v0/models"
                return await _lm_studio(http, base)
            url = f"{base}/models"
            body = await _get(http, url)
            entries = _data_entries(body)
            if kind == "openrouter":
                return _finish(
                    [
                        ModelInfo(
                            id=f"openrouter/{e['id']}",
                            max_context=_int_or_none(e.get("context_length")),
                        )
                        for e in entries
                    ]
                )
            return _finish([ModelInfo(id=f"openai/{e['id']}") for e in entries])
    except (httpx.HTTPError, ValueError) as exc:
        reason = str(exc) or type(exc).__name__
        return ModelListing(models=[], error=f"{name} unreachable at {url}: {reason}")


async def _get(http: httpx.AsyncClient, url: str) -> object:
    response = await http.get(url)
    response.raise_for_status()
    return response.json()


async def _lm_studio(http: httpx.AsyncClient, base: str) -> ModelListing:
    root = base.removesuffix("/v1")
    response = await http.get(f"{root}/api/v0/models")
    if response.status_code == 404:
        entries = _data_entries(await _get(http, f"{base}/models"))
        return _finish([ModelInfo(id=f"lm_studio/{e['id']}") for e in entries])
    response.raise_for_status()
    return _finish(
        [
            ModelInfo(
                id=f"lm_studio/{e['id']}",
                loaded=e.get("state") == "loaded",
                loaded_context=_int_or_none(e.get("loaded_context_length")),
                max_context=_int_or_none(e.get("max_context_length")),
            )
            for e in _data_entries(response.json())
            if e.get("type") in _LM_STUDIO_CHAT_TYPES
        ]
    )
