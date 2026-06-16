"""Task-aliased LLM access over LiteLLM.

This module is the single LiteLLM chokepoint (design doc section 4.5): all
completions and embeddings anywhere in corpusqa go through ``LLMTaskClient``.
That centralizes provider resolution, concurrency limits, retries/fallbacks,
and cost accounting, and gives tests one seam to mock.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
from dataclasses import dataclass
from typing import Any

import litellm

from corpusqa.config.schema import AppConfig, TaskConfig, TaskName
from corpusqa.errors import LLMError

_NUM_RETRIES = 3
_log = logging.getLogger("corpusqa.llm")


@dataclass(frozen=True)
class Completion:
    """A completed LLM call with usage accounting.

    Attributes:
        text: The assistant message content.
        model: The model that actually served the call (post-fallback).
        tokens_in: Prompt tokens consumed.
        tokens_out: Completion tokens produced.
        cost_usd: Cost in USD, using the per-task override when configured,
            otherwise LiteLLM's cost map (0.0 when neither knows the model).
        finish_reason: Why generation stopped ('stop', 'length', ...);
            'length' means the output hit max_tokens and may be truncated.
    """

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    finish_reason: str | None = None


class LLMTaskClient:
    """Routes task-aliased LLM calls to configured providers.

    Holds one asyncio semaphore per provider so fan-out stages (cataloging,
    extraction) respect per-provider concurrency limits regardless of which
    tasks share a provider.
    """

    def __init__(self, config: AppConfig) -> None:
        """Initializes the client from validated configuration.

        Args:
            config: The application configuration.
        """
        litellm.suppress_debug_info = True  # keep provider-list noise off stdout
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            # LiteLLM logs lookup failures itself (version-dependent) before
            # raising; those are ours to handle, not the user's to read.
            logging.getLogger(name).setLevel(logging.CRITICAL)
        # LiteLLM's OpenRouter config re-resolves the provider-stripped model
        # string internally (llms/openrouter/chat/transformation.py); when the
        # remainder is not itself a known model, that lookup raises/logs
        # "LLM Provider NOT provided". Registering the stripped names makes
        # the lookup succeed, removing the noise at its source.
        for task_cfg in config.tasks.values():
            prefix = "openrouter/"
            if task_cfg.model.startswith(prefix):
                litellm.openrouter_models.add(task_cfg.model[len(prefix) :])
        self._config = config
        self._semaphores: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(p.max_concurrency)
            for name, p in config.providers.items()
        }
        self._ledger: dict[TaskName, list[Completion]] = {}

    def price_per_mtok(self, task: TaskName) -> tuple[float, float]:
        """Returns (input, output) USD per million tokens for a task's model.

        Per-task config overrides are authoritative; otherwise LiteLLM's
        cost map is consulted; unknown models price at 0.0 (and should be
        given explicit overrides in config for meaningful estimates).
        """
        cfg = self._task(task)
        if cfg.cost_per_mtok_in is not None or cfg.cost_per_mtok_out is not None:
            return (cfg.cost_per_mtok_in or 0.0, cfg.cost_per_mtok_out or 0.0)
        info = litellm.model_cost.get(cfg.model) or litellm.model_cost.get(
            cfg.model.split("/", 1)[-1], {}
        )
        return (
            float(info.get("input_cost_per_token", 0.0)) * 1_000_000,
            float(info.get("output_cost_per_token", 0.0)) * 1_000_000,
        )

    def _task(self, task: TaskName) -> TaskConfig:
        return self._config.tasks[task]

    def _call_kwargs(self, task_cfg: TaskConfig) -> dict[str, Any]:  # noqa: ANN401 -- LiteLLM passthrough
        """Builds LiteLLM kwargs from task + provider config."""
        provider = self._config.providers[task_cfg.provider]
        kwargs: dict[str, Any] = {
            "model": task_cfg.model,
            "num_retries": _NUM_RETRIES,
        }
        if task_cfg.fallbacks:
            kwargs["fallbacks"] = task_cfg.fallbacks
        if provider.api_base is not None:
            kwargs["api_base"] = provider.api_base
        if provider.api_key_env is not None:
            key = os.environ.get(provider.api_key_env)
            if not key:
                raise LLMError(
                    f"environment variable '{provider.api_key_env}' is not set "
                    f"(required by provider '{task_cfg.provider}')"
                )
            kwargs["api_key"] = key
        return kwargs

    def _cost(
        self,
        task_cfg: TaskConfig,
        response: Any,  # noqa: ANN401 -- LiteLLM response object
        t_in: int,
        t_out: int,
    ) -> float:
        """Computes call cost.

        A per-task override only describes the task's PRIMARY model. When a
        fallback served the call, the served model differs and the override
        no longer applies (a local-priced-0 task that fell back to a paid
        provider must not be booked at $0); price the actual model via
        LiteLLM instead. ``response.model`` is the post-fallback model.
        """
        served = str(getattr(response, "model", "") or "")
        primary = task_cfg.model
        is_primary = served == primary or served == primary.split("/", 1)[-1]
        has_override = (
            task_cfg.cost_per_mtok_in is not None
            or task_cfg.cost_per_mtok_out is not None
        )
        if has_override and is_primary:
            cin = (task_cfg.cost_per_mtok_in or 0.0) * t_in / 1_000_000
            cout = (task_cfg.cost_per_mtok_out or 0.0) * t_out / 1_000_000
            return cin + cout
        try:
            return float(litellm.completion_cost(completion_response=response))
        except Exception:  # noqa: BLE001 -- unknown model in cost map is benign
            return 0.0

    async def complete(
        self,
        task: TaskName,
        messages: list[dict[str, str]],
        **overrides: Any,  # noqa: ANN401 -- LiteLLM passthrough
    ) -> Completion:
        """Runs a completion for the given task alias.

        Args:
            task: Task category; resolves model, params, and provider.
            messages: Chat messages in OpenAI format.
            **overrides: Per-call LiteLLM parameter overrides
                (e.g. ``response_format`` for JSON mode).

        Returns:
            The completion with usage and cost accounting.

        Raises:
            LLMError: If the call fails after retries and fallbacks.
        """
        task_cfg = self._task(task)
        kwargs = self._call_kwargs(task_cfg)
        kwargs.update(
            temperature=task_cfg.temperature,
            max_tokens=task_cfg.max_tokens,
            messages=messages,
        )
        kwargs.update(overrides)
        # The semaphore bounds the PRIMARY provider's concurrency. When a
        # call falls back to a model on a different provider, LiteLLM runs
        # the fallback inside this same acompletion under the primary's
        # semaphore; the fallback provider's own limit is not separately
        # enforced. Acceptable for a single-user tool; revisit if fallbacks
        # to a rate-limited provider become common.
        async with self._semaphores[task_cfg.provider]:
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as exc:
                raise LLMError(f"task '{task.value}' failed: {exc}") from exc
        usage = getattr(response, "usage", None)
        t_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        t_out = int(getattr(usage, "completion_tokens", 0) or 0)
        text = response.choices[0].message.content or ""
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        completion = Completion(
            text=text,
            model=str(getattr(response, "model", task_cfg.model)),
            tokens_in=t_in,
            tokens_out=t_out,
            cost_usd=self._cost(task_cfg, response, t_in, t_out),
            finish_reason=str(finish_reason) if finish_reason else None,
        )
        self._ledger.setdefault(task, []).append(completion)
        _log.info(
            "call task=%s model=%s tokens_in=%d tokens_out=%d cost_usd=%.6f",
            task.value,
            completion.model,
            completion.tokens_in,
            completion.tokens_out,
            completion.cost_usd,
        )
        _log.debug("prompt task=%s messages=%r", task.value, messages)
        _log.debug("response task=%s text=%r", task.value, completion.text)
        return completion

    def task_model(self, task: TaskName) -> str:
        """Returns the configured model string for a task."""
        return self._task(task).model

    def context_window(self, task: TaskName) -> int:
        """Returns the configured (authoritative) window size for a task."""
        return self._task(task).context_window

    def count_tokens(self, task: TaskName, text: str) -> int:
        """Counts tokens for ``text`` under the task's model tokenizer.

        Args:
            task: Task whose model tokenizer to use.
            text: Text to count.

        Returns:
            Token count: LiteLLM's tokenizer for the model when one can be
            resolved, else a ~4-chars/token heuristic. Provider-prefixed
            strings (e.g. ``openrouter/...``) often have no tokenizer
            mapping; budgeting precision does not warrant failing or
            emitting noise over it.
        """
        try:
            sink = io.StringIO()
            # Some LiteLLM versions print/log the lookup failure before
            # raising; contain it regardless of version.
            with (
                contextlib.redirect_stdout(sink),
                contextlib.redirect_stderr(sink),
            ):
                return int(
                    litellm.token_counter(model=self._task(task).model, text=text)
                )
        except Exception:  # noqa: BLE001 -- tokenizer lookup is best-effort
            return max(1, len(text) // 4)

    def usage_by_task(self) -> dict[TaskName, tuple[int, int, float]]:
        """Returns accumulated actual usage per task.

        Returns:
            ``{task: (tokens_in, tokens_out, cost_usd)}`` aggregated over
            every completion this client has run (the post-run cost report;
            the pre-flight estimate is separate by design).
        """
        out: dict[TaskName, tuple[int, int, float]] = {}
        for task, completions in self._ledger.items():
            out[task] = (
                sum(c.tokens_in for c in completions),
                sum(c.tokens_out for c in completions),
                round(sum(c.cost_usd for c in completions), 6),
            )
        return out