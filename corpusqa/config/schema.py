"""Pydantic schema for the corpusqa YAML configuration.

Validation policy: unknown keys and unknown task names are hard errors
(fail fast, design doc section 6). Secrets are referenced only by environment
variable name, never stored in configuration.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskName(StrEnum):
    """Closed set of LLM task categories (design doc section 4.5)."""

    CATALOG_SUMMARIZE = "catalog_summarize"
    QUERY_ROUTE = "query_route"
    EXTRACT = "extract"
    SYNTHESIZE = "synthesize"


class _StrictModel(BaseModel):
    """Base model forbidding unknown keys."""

    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_StrictModel):
    """A named provider endpoint.

    Attributes:
        api_base: Override base URL (e.g. local Ollama endpoint).
        api_key_env: Name of the environment variable holding the API key.
        max_concurrency: Semaphore size for fan-out calls to this provider.
    """

    api_base: str | None = None
    api_key_env: str | None = None
    max_concurrency: int = Field(default=4, ge=1, le=64)


class TaskConfig(_StrictModel):
    """Model binding and parameters for one task category.

    Attributes:
        model: LiteLLM model string (e.g. ``"ollama/qwen3:14b"``).
        provider: Key into ``providers``; resolves api_base/key/concurrency.
        temperature: Sampling temperature.
        max_tokens: Completion token cap.
        context_window: Authoritative window size for the budgeter. LiteLLM's
            model map covers known models, but local/finetuned ones need this.
        cost_per_mtok_in: USD per million input tokens; overrides LiteLLM's
            cost map when set (local models: 0.0).
        cost_per_mtok_out: USD per million output tokens override.
        fallbacks: Ordered LiteLLM model strings tried on failure/timeout.
    """

    model: str
    provider: str
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1)
    context_window: int = Field(default=32_768, ge=1024)
    cost_per_mtok_in: float | None = Field(default=None, ge=0.0)
    cost_per_mtok_out: float | None = Field(default=None, ge=0.0)
    fallbacks: list[str] = Field(default_factory=list)


class IngestConfig(_StrictModel):
    """Ingest-time settings."""

    ocr: str = Field(default="off", pattern="^(off|auto|on)$")
    pdf_backend: str = Field(
        default="docling_parse", pattern="^(docling_parse|pypdfium)$"
    )
    chunk_target_tokens: int = Field(default=512, ge=64, le=4096)
    extensions: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".doc", ".md", ".markdown", ".txt"]
    )


class QueryConfig(_StrictModel):
    """Query-pipeline settings.

    Attributes:
        sweep_threshold: ``--mode auto`` sweeps when the parseable file
            count is at or below this; above it, routing is used and sweep
            remains available explicitly.
        eval_retention_runs: Evaluation-cache rows are pruned to the most
            recent N run_ids.
    """

    sweep_threshold: int = Field(default=150, ge=0)
    eval_retention_runs: int = Field(default=20, ge=1)


class BudgetConfig(_StrictModel):
    """Spend-control settings."""

    confirm_above_usd: float = Field(default=1.0, ge=0.0)


class AppConfig(_StrictModel):
    """Root configuration object.

    Attributes:
        providers: Named provider endpoints.
        tasks: One entry per ``TaskName`` member; all six are required so a
            misconfigured task fails at startup, not mid-run.
        query: Query-pipeline settings.
        ingest: Ingest-time settings.
        budget: Spend-control settings.
    """

    providers: dict[str, ProviderConfig]
    tasks: dict[TaskName, TaskConfig]
    query: QueryConfig = Field(default_factory=QueryConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="after")
    def _check_cross_references(self) -> AppConfig:
        """Validates task completeness and provider references."""
        missing = [t.value for t in TaskName if t not in self.tasks]
        if missing:
            raise ValueError(f"missing task configs: {', '.join(missing)}")
        for name, task in self.tasks.items():
            if task.provider not in self.providers:
                raise ValueError(
                    f"task '{name.value}' references unknown provider '{task.provider}'"
                )
        return self
