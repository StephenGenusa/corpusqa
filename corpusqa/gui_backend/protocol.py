"""Message models for the GUI's stdio JSON-lines protocol.

One UTF-8 JSON object per line in each direction. A request carries ``id``
and ``cmd`` (plus optional ``params``); its reply echoes ``id`` with either
``result`` or ``error`` (and, for ``config.save``, ``fields``). Events carry
``event`` and no ``id``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Request(BaseModel):
    """One request line.

    Attributes:
        id: Caller-chosen id echoed by the reply.
        cmd: Command name, e.g. ``run.start``.
        params: Command parameters.
    """

    id: str
    cmd: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConfigSaveParams(_Strict):
    """``config.save``: key paths with new values (None deletes the key)."""

    changes: list[tuple[list[str], Any]]


class ModelsListParams(_Strict):
    """``models.list``: provider key in ``providers``."""

    provider: str


class RunStartParams(_Strict):
    """``run.start`` parameters.

    Attributes:
        folder: Corpus directory.
        question: The user question.
        definition: Optional topic definition.
        relevance: Extraction inclusion bar.
        mode: ``auto | route | sweep``.
        synthesize: Compose a single cited prose answer.
        use_cache: Reuse persisted evaluations.
    """

    folder: str
    question: str = Field(min_length=1)
    definition: str | None = None
    relevance: Literal["recall", "balanced", "strict"] = "recall"
    mode: Literal["auto", "route", "sweep"] = "auto"
    synthesize: bool = False
    use_cache: bool = True


class CostReplyParams(_Strict):
    """``cost.reply``: the answer to a ``cost.confirm`` event."""

    run_id: str
    proceed: bool


class DocGetParams(_Strict):
    """``doc.get``: one indexed document's cached markdown."""

    folder: str
    rel_path: str


class ResultsListParams(_Strict):
    """``results.list``: the corpus whose saved results to list."""

    folder: str


class ResultsLoadParams(_Strict):
    """``results.load``: the results file to open."""

    path: str


class NoticeEvent(_Strict):
    """Informational message; the run continues."""

    event: Literal["notice"] = "notice"
    message: str


class ProgressEvent(_Strict):
    """Progress of the conversion (files) or reading (LLM calls) phase."""

    event: Literal["progress"] = "progress"
    phase: Literal["converting", "reading"]
    done: int
    total: int


class FlagItem(_Strict):
    """A file that did not convert cleanly."""

    rel_path: str
    status: str
    error: str | None


class FlagsEvent(_Strict):
    """Files queries cannot fully see."""

    event: Literal["flags"] = "flags"
    items: list[FlagItem]


class FindingOut(_Strict):
    """One cited finding with its system-derived location."""

    quote: str
    claim: str
    heading_path: list[str]
    page_label: str | None
    start: int | None
    end: int | None
    verified: bool


class FileResultEvent(_Strict):
    """One file's extraction result, sent as soon as it exists."""

    event: Literal["file_result"] = "file_result"
    rel_path: str
    relevant: bool
    findings: list[FindingOut]


class EstimateOut(_Strict):
    """A cost projection, pre-rendered as display lines."""

    total_usd: float
    lines: list[str]


class CostConfirmEvent(_Strict):
    """Asks the GUI to confirm spending; answered by ``cost.reply``."""

    event: Literal["cost.confirm"] = "cost.confirm"
    run_id: str
    estimate: EstimateOut


class DoneEvent(_Strict):
    """Ends every started run, exactly once."""

    event: Literal["done"] = "done"
    failures: list[tuple[str, str]] = Field(default_factory=list)
    answer: str | None = None
    cancelled: bool = False
    results_path: str | None = None


class ErrorEvent(_Strict):
    """A failure not tied to a request (startup, or a run that stops)."""

    event: Literal["error"] = "error"
    message: str


Event = (
    NoticeEvent
    | ProgressEvent
    | FlagsEvent
    | FileResultEvent
    | CostConfirmEvent
    | DoneEvent
    | ErrorEvent
)
