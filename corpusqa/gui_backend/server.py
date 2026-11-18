"""Stdio JSON-lines backend for the Electron GUI.

Threads: stdin is read on a dedicated thread that hands requests to the
asyncio loop with ``call_soon_threadsafe``. Two commands are answered on the
reader thread itself, because they must work while the loop is blocked:
``cost.reply`` (the library's ``confirm`` callback is synchronous and blocks
the loop on a ``threading.Event``) and ``run.cancel``. Conversion runs in a
worker thread (``asyncio.to_thread``) and stops through ``should_stop``; the
query runs on the loop and stops through ``Task.cancel``.

Every output line is written under one lock, so events from the loop, the
conversion worker and the reader thread never interleave. Only protocol
lines reach stdout: ``main`` moves file descriptor 1 onto stderr before any
library code runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

from pydantic import BaseModel, ValidationError

from corpusqa.catalog.store import CatalogStore
from corpusqa.config import load_config
from corpusqa.config.schema import (
    AppConfig,
    ProviderConfig,
    TaskName,
    provider_kind,
)
from corpusqa.config.writer import config_as_dict, save_config_changes
from corpusqa.errors import BudgetExceededError, ConfigError, CorpusQAError
from corpusqa.gui_backend.env import load_dotenv
from corpusqa.gui_backend.models import ModelListing, list_models
from corpusqa.gui_backend.protocol import (
    ChatSendParams,
    ConfigSaveParams,
    CostConfirmEvent,
    CostReplyParams,
    DocGetParams,
    DoneEvent,
    ErrorEvent,
    EstimateOut,
    Event,
    FileResultEvent,
    FilesListParams,
    FindingOut,
    FlagItem,
    FlagsEvent,
    GroupStartParams,
    ModelsListParams,
    NoticeEvent,
    ProgressEvent,
    Request,
    ResultsListParams,
    ResultsLoadParams,
    RunStartParams,
)
from corpusqa.ingest.discovery import INDEX_DIR_NAME
from corpusqa.ingest.pipeline import index_paths, run_index
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.logging_setup import configure as configure_logging
from corpusqa.logging_setup import run_log_file
from corpusqa.prompts.chat import ChatContext, build_system
from corpusqa.query.budget import CostEstimate
from corpusqa.query.extractor import ExtractionResult
from corpusqa.query.grouping import group_answers, source_text_lookup, to_saved
from corpusqa.query.pipeline import _load_page_map, run_query
from corpusqa.query.results_file import (
    ResultsWriter,
    SavedRun,
    list_results,
    load_results,
    save_grouping,
)

_log = logging.getLogger("corpusqa.gui_backend")

_FIELD_LINE = re.compile(r"^\s+(\S+): (.*)$")

ListModels = Callable[[str, ProviderConfig], Awaitable[ModelListing]]
ClientFactory = Callable[[AppConfig], LLMTaskClient]


class LineReader(Protocol):
    """Input stream: ``readline`` returns ``""`` at end of input."""

    def readline(self) -> str:
        """Returns the next line, or ``""`` at EOF."""
        ...


class LineWriter(Protocol):
    """Output stream for protocol lines."""

    def write(self, text: str, /) -> int:
        """Writes text."""
        ...

    def flush(self) -> None:
        """Flushes buffered output."""
        ...


def _unwritable_message(folder: Path, exc: BaseException) -> str:
    """Explains that the corpus folder must be writable."""
    index_dir = index_paths(folder)[0]
    return (
        f"cannot write to {index_dir}: corpusqa stores converted text in "
        f"<folder>/.corpusqa, so the folder must be writable ({exc})"
    )


def _writable_index_dir(folder: Path) -> Path:
    """Returns the corpus's index directory after checking it can be written.

    Raises:
        _PreflightError: When the folder (or an existing ``.corpusqa``) is
            read-only, with a message saying why it must be writable.
    """
    index_dir = index_paths(folder.resolve())[0]
    try:
        index_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryFile(dir=index_dir):
            pass
    except OSError as exc:
        raise _PreflightError(_unwritable_message(folder, exc)) from exc
    return index_dir


class _PreflightError(Exception):
    """A pre-work check failed; the message is shown to the user."""


def _field_errors(message: str) -> dict[str, str]:
    """Extracts ``dotted.path: msg`` lines from a ConfigError message."""
    fields: dict[str, str] = {}
    for line in message.splitlines():
        match = _FIELD_LINE.match(line)
        if match:
            fields[match.group(1)] = match.group(2)
    return fields


def _estimate_lines(estimate: CostEstimate) -> list[str]:
    lines = [f"{task}: ${usd:.4f}" for task, usd in estimate.per_task_usd.items()]
    if estimate.candidate_files:
        lines.append(
            f"{estimate.candidate_files} file(s), "
            f"{estimate.total_input_tokens} input tokens"
        )
    if estimate.unpriced_tasks:
        lines.append(
            f"No price is known for: {', '.join(estimate.unpriced_tasks)}; "
            "their cost is not in this estimate."
        )
    return lines


_ROUTE_TASKS = (TaskName.CATALOG_SUMMARIZE, TaskName.QUERY_ROUTE)


def _resolve_auto_mode(
    folder: Path, config: AppConfig, files: list[str] | None = None
) -> str:
    """Resolves ``auto`` exactly as ``run_query`` does, from the catalog."""
    with CatalogStore(index_paths(folder.resolve())[1]) as store:
        rows = store.all_parseable_files()
    wanted = None if files is None else set(files)
    parseable = sum(1 for r in rows if wanted is None or r.rel_path in wanted)
    return "sweep" if parseable <= config.query.sweep_threshold else "route"


def _bare_model(model: str) -> str:
    """Drops a LiteLLM provider prefix: ``lm_studio/qwen`` -> ``qwen``."""
    return model.split("/", 1)[1] if "/" in model else model


def _read_doc(folder: str, rel_path: str) -> dict[str, object]:
    """Loads one indexed file's cached markdown and page map."""
    root = Path(folder).resolve()
    _, db_path, cache_dir = index_paths(root)
    if not db_path.exists():
        raise CorpusQAError(f"no index at {root}")
    with CatalogStore(db_path) as store:
        file_hash = store.known_files().get(rel_path)
    if file_hash is None:
        raise CorpusQAError(f"{rel_path} is not in the index")
    try:
        markdown = (cache_dir / f"{file_hash}.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusQAError(f"no converted text for {rel_path}: {exc}") from exc
    page_map = [
        [offset, label] for offset, label in _load_page_map(cache_dir, file_hash)
    ]
    return {"markdown": markdown, "page_map": page_map, "file_hash": file_hash}


def _list_saved(folder: str) -> dict[str, object]:
    return {
        "results": [
            {
                "path": str(s.path),
                "question": s.question,
                "started_at": s.started_at,
                "outcome": s.outcome,
                "files": s.files,
                "findings": s.findings,
            }
            for s in list_results(Path(folder))
        ]
    }


def _list_files(folder: str, extensions: list[str]) -> dict[str, object]:
    """Lists a folder's documents for choosing a subset.

    A plain walk (no hashing), so it is quick on a large folder. ``converted``
    says whether the index already holds the file's text; a file that changed
    since is still reported converted and is reconverted when queried.
    """
    root = Path(folder).resolve()
    if not root.is_dir():
        raise CorpusQAError(f"not a folder: {root}")
    wanted = {e.lower() for e in extensions}
    known: dict[str, str] = {}
    db_path = index_paths(root)[1]
    if db_path.exists():
        try:
            with CatalogStore(db_path) as store:
                known = store.known_files()
        except sqlite3.Error as exc:
            _log.warning("cannot read index for %s: %s", root, exc)
    files = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if INDEX_DIR_NAME in rel.parts or path.suffix.lower() not in wanted:
            continue
        try:
            if not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        rel_path = rel.as_posix()
        files.append(
            {"rel_path": rel_path, "size": size, "converted": rel_path in known}
        )
    return {"files": files}


_CHAT_MAX_TOKENS = 1500
_CHAT_TEMPERATURE = 0.4


def _chat_config(config: AppConfig, provider: str, model: str) -> AppConfig:
    """A config whose synthesize task is the chat model (not saved)."""
    if provider not in config.providers:
        raise CorpusQAError(f"unknown provider '{provider}'")
    task = config.tasks[TaskName.SYNTHESIZE].model_copy(
        update={
            "provider": provider,
            "model": model,
            "max_tokens": _CHAT_MAX_TOKENS,
            "temperature": _CHAT_TEMPERATURE,
            "fallbacks": [],
        }
    )
    tasks = dict(config.tasks)
    tasks[TaskName.SYNTHESIZE] = task
    return config.model_copy(update={"tasks": tasks})


def _load_saved(path: str) -> dict[str, object]:
    """Loads a results file and says, per file, whether its source still matches.

    ``sources`` maps each saved file to ``same`` (offsets still apply),
    ``changed`` (the source was reconverted with different content) or
    ``missing`` (not in the corpus index any more, or no index at all).
    """
    run = load_results(Path(path))
    db_path = index_paths(Path(run.corpus))[1]
    known: dict[str, str] = {}
    if db_path.exists():
        try:
            with CatalogStore(db_path) as store:
                known = store.known_files()
        except sqlite3.Error as exc:
            _log.warning("cannot read index for %s: %s", run.corpus, exc)
    sources = {
        f.rel_path: (
            "missing"
            if f.rel_path not in known
            else "same"
            if known[f.rel_path] == f.file_hash
            else "changed"
        )
        for f in run.files
    }
    return {"run": run.model_dump(), "sources": sources}


class Server:
    """Serves the GUI protocol over a pair of line streams.

    Args:
        config_path: The ``corpusqa.yaml`` the GUI edits and runs with.
        instream: Request lines.
        outstream: Reply and event lines (nothing else is written to it).
        env_path: ``.env`` to load at startup; defaults to the one next to
            the config file.
        client_factory: Builds the LLM client for a run (tests inject a
            fake); None lets ``run_query`` build the real one.
        list_models_fn: Model lister (tests inject a stub).
        verbosity: Run log file detail: 0/1 INFO, 2+ DEBUG.
    """

    def __init__(
        self,
        config_path: Path,
        instream: LineReader,
        outstream: LineWriter,
        *,
        env_path: Path | None = None,
        client_factory: ClientFactory | None = None,
        list_models_fn: ListModels = list_models,
        verbosity: int = 0,
    ) -> None:
        """Initializes the server; nothing runs until ``serve``."""
        self._config_path = config_path.absolute()
        self._env_path = env_path or self._config_path.parent / ".env"
        self._in = instream
        self._out = outstream
        self._client_factory = client_factory
        self._list_models = list_models_fn
        self._verbosity = verbosity
        self._write_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[Request | None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # run state (written on the loop; read by the reader thread)
        self._run_task: asyncio.Task[None] | None = None
        self._results_file: ResultsWriter | None = None
        self._run_active = False
        self._phase = "idle"
        self._cancel = threading.Event()
        # cost confirmation handshake (loop blocks; reader thread releases)
        self._confirm_event = threading.Event()
        self._confirm_run_id: str | None = None
        self._confirm_answer = False

    # -- output ----------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                self._out.write(line)
                self._out.flush()
        except Exception:  # noqa: BLE001 -- a dead pipe must not kill a run
            _log.exception("cannot write protocol line")

    def emit(self, event: Event) -> None:
        """Writes one event line; never raises."""
        try:
            self._write(event.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 -- callbacks must never raise
            _log.exception("cannot emit %s event", type(event).__name__)

    def _reply(self, req_id: str, result: dict[str, Any] | BaseModel) -> None:
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json")
        self._write({"id": req_id, "result": result})

    def _reply_error(self, req_id: str, message: str, **extra: object) -> None:
        self._write({"id": req_id, "error": message, **extra})

    # -- lifecycle -------------------------------------------------------

    async def serve(self) -> None:
        """Serves requests until the input stream ends.

        At end of input a running query is cancelled and in-flight requests
        are allowed to finish before returning.
        """
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._startup()
        threading.Thread(target=self._read_input, name="stdin", daemon=True).start()
        while (request := await self._queue.get()) is not None:
            self._spawn(self._handle(request))
        self._cancel_in_loop()
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _startup(self) -> None:
        try:
            names = load_dotenv(self._env_path)
            if names:
                _log.info("loaded %s from %s", ", ".join(names), self._env_path)
        except (OSError, UnicodeError) as exc:
            self.emit(ErrorEvent(message=f"cannot read {self._env_path}: {exc}"))
        try:
            load_config(self._config_path)
        except ConfigError as exc:
            self.emit(ErrorEvent(message=str(exc)))

    def _spawn(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _to_loop(self, callback: Callable[..., None], *args: object) -> None:
        assert self._loop is not None
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:  # loop already closed
            _log.debug("loop closed; dropped %s", callback)

    # -- reader thread ---------------------------------------------------

    def _read_input(self) -> None:
        assert self._queue is not None
        queue = self._queue
        while True:
            try:
                line = self._in.readline()
            except Exception:  # noqa: BLE001 -- unreadable input ends the session
                _log.exception("cannot read input")
                line = ""
            if not line:
                break
            if not line.strip():
                continue
            try:
                request = Request.model_validate_json(line)
            except ValidationError as exc:
                self.emit(ErrorEvent(message=f"invalid request: {exc}"))
                continue
            if request.cmd == "cost.reply":
                self._on_cost_reply(request)
            elif request.cmd == "run.cancel":
                self._on_cancel(request)
            else:
                self._to_loop(queue.put_nowait, request)
        self._cancel.set()
        self._release_confirm()
        self._to_loop(queue.put_nowait, None)

    def _on_cost_reply(self, request: Request) -> None:
        try:
            params = CostReplyParams.model_validate(request.params)
        except ValidationError as exc:
            self._reply_error(request.id, f"invalid params for cost.reply: {exc}")
            return
        if params.run_id != self._confirm_run_id:
            self._reply_error(
                request.id, f"no cost confirmation is pending for run {params.run_id}"
            )
            return
        self._confirm_answer = params.proceed
        self._confirm_event.set()
        self._reply(request.id, {})

    def _on_cancel(self, request: Request) -> None:
        active = self._run_active
        if active:
            self._cancel.set()
            self._release_confirm()
            self._to_loop(self._cancel_in_loop)
        self._reply(request.id, {"cancelled": active})

    def _release_confirm(self) -> None:
        self._confirm_answer = False
        self._confirm_event.set()

    def _cancel_in_loop(self) -> None:
        # Conversion runs in a worker thread that Task.cancel cannot stop; it
        # polls should_stop instead, and the run reports once it returns.
        if self._run_task is not None and self._phase != "converting":
            self._run_task.cancel()

    # -- requests --------------------------------------------------------

    async def _handle(self, request: Request) -> None:
        handlers: dict[str, Callable[[Request], Awaitable[None]]] = {
            "config.get": self._config_get,
            "config.save": self._config_save,
            "models.list": self._models_list,
            "run.start": self._run_start,
            "doc.get": self._doc_get,
            "results.list": self._results_list,
            "results.load": self._results_load,
            "group.start": self._group_start,
            "files.list": self._files_list,
            "chat.send": self._chat_send,
        }
        handler = handlers.get(request.cmd)
        if handler is None:
            self._reply_error(request.id, f"unknown command '{request.cmd}'")
            return
        try:
            await handler(request)
        except ValidationError as exc:
            self._reply_error(request.id, f"invalid params for {request.cmd}: {exc}")
        except CorpusQAError as exc:
            self._reply_error(request.id, str(exc))
        except Exception as exc:  # noqa: BLE001 -- report, keep serving
            _log.exception("%s failed", request.cmd)
            self._reply_error(request.id, f"{type(exc).__name__}: {exc}")

    async def _config_get(self, request: Request) -> None:
        config = load_config(self._config_path)
        self._reply(
            request.id,
            {"config": config_as_dict(config), "path": str(self._config_path)},
        )

    async def _config_save(self, request: Request) -> None:
        params = ConfigSaveParams.model_validate(request.params)
        changes = [(tuple(keys), value) for keys, value in params.changes]
        try:
            config = save_config_changes(self._config_path, changes)
        except ConfigError as exc:
            message = str(exc)
            self._reply_error(request.id, message, fields=_field_errors(message))
            return
        self._reply(request.id, {"config": config_as_dict(config)})

    async def _models_list(self, request: Request) -> None:
        params = ModelsListParams.model_validate(request.params)
        try:
            config = load_config(self._config_path)
            provider = config.providers.get(params.provider)
            if provider is None:
                listing = ModelListing(
                    models=[], error=f"unknown provider '{params.provider}'"
                )
            else:
                listing = await self._list_models(params.provider, provider)
        except Exception as exc:  # noqa: BLE001 -- e.g. ValueError on a bad port
            listing = ModelListing(models=[], error=f"{params.provider}: {exc}")
        self._reply(request.id, listing)

    async def _doc_get(self, request: Request) -> None:
        params = DocGetParams.model_validate(request.params)
        self._reply(
            request.id,
            await asyncio.to_thread(_read_doc, params.folder, params.rel_path),
        )

    async def _results_list(self, request: Request) -> None:
        params = ResultsListParams.model_validate(request.params)
        self._reply(request.id, await asyncio.to_thread(_list_saved, params.folder))

    async def _results_load(self, request: Request) -> None:
        params = ResultsLoadParams.model_validate(request.params)
        self._reply(request.id, await asyncio.to_thread(_load_saved, params.path))

    async def _files_list(self, request: Request) -> None:
        params = FilesListParams.model_validate(request.params)
        config = load_config(self._config_path)
        self._reply(
            request.id,
            await asyncio.to_thread(
                _list_files, params.folder, config.ingest.extensions
            ),
        )

    async def _chat_send(self, request: Request) -> None:
        """Answers one turn of the wording-help chat (nothing is saved)."""
        params = ChatSendParams.model_validate(request.params)
        config = _chat_config(
            load_config(self._config_path), params.provider, params.model
        )
        client = (
            self._client_factory(config)
            if self._client_factory
            else LLMTaskClient(config)
        )
        system = build_system(ChatContext(**params.context.model_dump()))
        messages = [{"role": "system", "content": system}] + [
            m.model_dump() for m in params.messages
        ]
        completion = await client.complete(TaskName.SYNTHESIZE, messages)
        self._reply(
            request.id,
            {
                "text": completion.text,
                "model": completion.model,
                "truncated": completion.finish_reason == "length",
            },
        )

    async def _run_start(self, request: Request) -> None:
        params = RunStartParams.model_validate(request.params)
        if self._run_active:
            self._reply_error(request.id, "a run is already in progress")
            return
        folder = Path(params.folder)
        if not folder.is_dir():
            self._reply_error(request.id, f"not a folder: {folder}")
            return
        # Re-read from disk so changes saved in the settings drawer apply.
        config = load_config(self._config_path)
        run_id = uuid.uuid4().hex[:12]
        self._run_active = True
        self._cancel.clear()
        self._phase = "starting"
        # Reply before the task runs so {started} precedes the run's events.
        self._run_task = self._spawn(
            self._run(self._execute(params, config, run_id), folder, run_id)
        )
        self._reply(request.id, {"started": True})

    async def _group_start(self, request: Request) -> None:
        params = GroupStartParams.model_validate(request.params)
        if self._run_active:
            self._reply_error(request.id, "a run is already in progress")
            return
        path = Path(params.path)
        saved = await asyncio.to_thread(load_results, path)  # errors reply
        config = load_config(self._config_path)
        run_id = uuid.uuid4().hex[:12]
        self._run_active = True
        self._cancel.clear()
        self._phase = "starting"
        self._run_task = self._spawn(
            self._run(
                self._group(path, saved, config, run_id),
                Path(saved.corpus),
                run_id,
            )
        )
        self._reply(request.id, {"started": True})

    # -- the run ---------------------------------------------------------

    async def _run(
        self, work: Coroutine[Any, Any, DoneEvent], folder: Path, run_id: str
    ) -> None:
        done = DoneEvent()
        self._results_file = None
        try:
            done = await work
        except asyncio.CancelledError:
            done = DoneEvent(cancelled=True)
        except BudgetExceededError:
            # The gate only raises when confirm returned False: the user
            # declined in the GUI (or cancelled while it was asking).
            done = DoneEvent(cancelled=True)
        except (_PreflightError, CorpusQAError) as exc:
            self.emit(ErrorEvent(message=str(exc)))
        except (PermissionError, sqlite3.OperationalError) as exc:
            # The folder became unwritable mid-run, or its catalog is
            # read-only ("unable to open database file", "readonly database").
            _log.warning("run %s cannot write its index: %s", run_id, exc)
            self.emit(ErrorEvent(message=_unwritable_message(folder, exc)))
        except Exception as exc:  # noqa: BLE001 -- the GUI must hear about it
            _log.exception("run %s failed", run_id)
            self.emit(ErrorEvent(message=f"{type(exc).__name__}: {exc}"))
        if self._cancel.is_set():
            done = done.model_copy(update={"cancelled": True})
        saved = self._results_file
        if saved is not None:
            if saved.write_error:
                self.emit(
                    NoticeEvent(
                        message=f"Could not save the results file: {saved.write_error}"
                    )
                )
            else:
                done = done.model_copy(update={"results_path": str(saved.path)})
        self._run_task = None
        self._phase = "idle"
        self._run_active = False
        self.emit(done)

    async def _group(
        self, path: Path, saved: SavedRun, config: AppConfig, run_id: str
    ) -> DoneEvent:
        """Groups a saved run's hits into distinct answers and stores them."""
        self._phase = "preflight"
        await self._preflight(config, [TaskName.SYNTHESIZE])
        if self._cancel.is_set():
            return DoneEvent(cancelled=True)
        corpus = Path(saved.corpus)
        index_dir = index_paths(corpus)[0]
        logs = index_dir / "logs" if index_dir.is_dir() else path.parent / "logs"
        with run_log_file(logs, self._verbosity) as log_path:
            _log.info("gui group run=%s results=%s log=%s", run_id, path, log_path)
            self._phase = "grouping"
            client = (
                self._client_factory(config)
                if self._client_factory
                else LLMTaskClient(config)
            )
            grouping = await group_answers(
                saved,
                config,
                client,
                markdown_for=await asyncio.to_thread(source_text_lookup, corpus),
                on_progress=lambda d, t: self._progress("grouping", d, t),
                confirm=lambda estimate: self._confirm(run_id, estimate),
            )
            await asyncio.to_thread(save_grouping, path, to_saved(grouping))
        for problem in grouping.problems:
            self.emit(NoticeEvent(message=problem))
        return DoneEvent(results_path=str(path))

    async def _execute(
        self, params: RunStartParams, config: AppConfig, run_id: str
    ) -> DoneEvent:
        self._phase = "preflight"
        # Check only the tasks this run uses. Auto mode picks sweep or route
        # from the parseable file count, known only after conversion, so its
        # routing tasks are checked then (still before any LLM spend).
        tasks = [TaskName.EXTRACT]
        if params.synthesize:
            tasks.append(TaskName.SYNTHESIZE)
        if params.mode == "route":
            tasks.extend(_ROUTE_TASKS)
        await self._preflight(config, tasks)
        if self._cancel.is_set():
            return DoneEvent(cancelled=True)
        # Only now touch the folder: a failed preflight leaves it untouched.
        index_dir = _writable_index_dir(Path(params.folder))
        # Each run logs to <corpus>/.corpusqa/logs, as a CLI query does.
        with run_log_file(index_dir / "logs", self._verbosity) as log_path:
            _log.info("gui run=%s folder=%s log=%s", run_id, params.folder, log_path)
            return await self._convert_and_query(params, config, run_id)

    async def _convert_and_query(
        self, params: RunStartParams, config: AppConfig, run_id: str
    ) -> DoneEvent:
        folder = Path(params.folder)
        self._phase = "converting"
        report = await asyncio.to_thread(
            run_index,
            folder,
            config,
            on_progress=lambda d, t: self._progress("converting", d, t),
            should_stop=self._cancel.is_set,
            only=None if params.files is None else set(params.files),
        )
        self._phase = "querying"
        self._emit_flags(folder)
        if report.stopped or self._cancel.is_set():
            return DoneEvent(cancelled=True)

        mode: str = params.mode
        if mode == "auto":
            mode = _resolve_auto_mode(folder, config, params.files)
            if mode == "route":
                self._phase = "preflight"
                await self._preflight(config, list(_ROUTE_TASKS))
                self._phase = "querying"

        client = self._client_factory(config) if self._client_factory else None
        self._results_file = ResultsWriter.create(
            folder,
            question=params.question,
            definition=params.definition or None,
            relevance=params.relevance,
            mode=params.mode,
            config=config,
            selection=params.files,
        )
        result = await run_query(
            folder,
            params.question,
            config,
            definition=params.definition or None,
            mode=mode,
            relevance=params.relevance,
            use_cache=params.use_cache,
            synthesize_answer=params.synthesize,
            on_progress=lambda d, t: self._progress("reading", d, t),
            on_file_result=self._file_result,
            confirm=lambda estimate: self._confirm(run_id, estimate),
            client=client,
            results_file=self._results_file,
            files=params.files,
        )
        return DoneEvent(
            failures=[*result.card_report.failed, *result.extraction_failures],
            answer=result.answer.text if result.answer is not None else None,
        )

    async def _preflight(self, config: AppConfig, tasks: list[TaskName]) -> None:
        self._check_api_keys(config, tasks)
        await self._lm_studio_preflight(config, tasks)

    def _check_api_keys(self, config: AppConfig, tasks: list[TaskName]) -> None:
        for task in (config.tasks[t] for t in tasks):
            provider = config.providers[task.provider]
            var = provider.api_key_env
            if var and not os.environ.get(var):
                raise _PreflightError(
                    f"{var} is not set (needed by provider '{task.provider}'); "
                    f"set it in your environment or in .env ({self._env_path})"
                )

    async def _lm_studio_preflight(
        self, config: AppConfig, tasks: list[TaskName]
    ) -> None:
        listings: dict[str, ModelListing] = {}
        noticed: set[tuple[str, int]] = set()
        for task in (config.tasks[t] for t in tasks):
            provider = config.providers[task.provider]
            if provider_kind(task.provider, provider) != "lm_studio":
                continue
            listing = listings.get(task.provider)
            if listing is None:
                try:
                    listing = await self._list_models(task.provider, provider)
                except Exception as exc:  # noqa: BLE001 -- e.g. a malformed port
                    listing = ModelListing(
                        models=[], error=f"{type(exc).__name__}: {exc}"
                    )
                listings[task.provider] = listing
            if listing.error:
                raise _PreflightError(f"cannot reach LM Studio: {listing.error}")
            model = _bare_model(task.model)
            want = task.context_window
            info = next((m for m in listing.models if _bare_model(m.id) == model), None)
            if info is None:
                raise _PreflightError(
                    f"LM Studio has no chat model named {model}. Pick a model "
                    "from the Model list (draft/assistant models cannot answer)."
                )
            if info.loaded is False:
                # Without just-in-time loading LM Studio rejects every request
                # with "No models loaded"; a run would fail all its sections.
                raise _PreflightError(
                    f"{model} is not loaded in LM Studio. Load it (Developer "
                    f"tab, or: lms load {model}) with a context length of at "
                    f"least {want}, then run again."
                )
            if info.loaded:
                have = info.loaded_context
                if have is not None and have < want:
                    raise _PreflightError(
                        f"LM Studio loaded {model} with a {have}-token context; "
                        f"corpusqa is set to {want}. Reload it in LM Studio with "
                        "at least "
                        f"{want}, or lower Context window in Settings."
                    )
            elif (model, want) not in noticed:
                # Load state unknown (an LM Studio without /api/v0).
                noticed.add((model, want))
                self.emit(
                    NoticeEvent(
                        message=(
                            f"LM Studio did not report whether {model} is "
                            "loaded; make sure it is, with a context length of "
                            f"at least {want}."
                        )
                    )
                )

    def _progress(
        self,
        phase: Literal["converting", "reading", "grouping"],
        done: int,
        total: int,
    ) -> None:
        try:
            self.emit(ProgressEvent(phase=phase, done=done, total=total))
        except Exception:  # noqa: BLE001 -- callbacks must never raise
            _log.exception("progress event failed")

    def _file_result(self, rel_path: str, result: ExtractionResult) -> None:
        try:
            self.emit(
                FileResultEvent(
                    rel_path=rel_path,
                    relevant=result.relevant,
                    findings=[
                        FindingOut(
                            quote=f.quote,
                            claim=f.claim,
                            heading_path=f.heading_path,
                            page_label=f.page_label,
                            start=f.start,
                            end=f.end,
                            verified=f.quote_verified,
                        )
                        for f in result.findings
                    ],
                )
            )
        except Exception:  # noqa: BLE001 -- an exception here aborts run_query
            _log.exception("file_result event failed for %s", rel_path)

    def _emit_flags(self, folder: Path) -> None:
        try:
            with CatalogStore(index_paths(folder.resolve())[1]) as store:
                rows = store.flagged_files()
        except Exception:  # noqa: BLE001 -- flags are advisory
            _log.exception("cannot read flagged files")
            return
        if rows:
            self.emit(
                FlagsEvent(
                    items=[
                        FlagItem(
                            rel_path=r.rel_path,
                            status=r.parse_status,
                            error=r.parse_error,
                        )
                        for r in rows
                    ]
                )
            )

    def _confirm(self, run_id: str, estimate: CostEstimate) -> bool:
        """Blocks the loop until the GUI answers (``cost.reply``) or cancels."""
        self._confirm_answer = False
        self._confirm_event.clear()
        self._confirm_run_id = run_id
        try:
            if self._cancel.is_set():
                return False
            self.emit(
                CostConfirmEvent(
                    run_id=run_id,
                    estimate=EstimateOut(
                        total_usd=estimate.total_usd, lines=_estimate_lines(estimate)
                    ),
                )
            )
            self._confirm_event.wait()
            return self._confirm_answer and not self._cancel.is_set()
        finally:
            self._confirm_run_id = None


def _claim_stdout() -> TextIO:
    """Reserves the real stdout for protocol lines; fd 1 becomes stderr.

    Library output (print, tqdm, native code, child processes) then lands on
    stderr and can never corrupt the protocol stream. Works on raw file
    descriptors, so it also holds when ``sys.stdout`` is None (a windowed
    frozen build still receives the parent's pipes as fds 0-2).
    """
    if sys.stdout is not None:
        sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", newline="\n")


def run_backend(
    config_path: Path, verbosity: int = 0, env_path: Path | None = None
) -> int:
    """Runs the backend on the process's stdin/stdout until stdin closes.

    Args:
        config_path: The configuration file.
        verbosity: 0 (warnings), 1 (info) or 2+ (debug) logging on stderr.
        env_path: ``.env`` to load at startup; None means the one next to
            the config file.

    Returns:
        Process exit code.
    """
    out = _claim_stdout()
    configure_logging(verbosity, None)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    with open(  # noqa: PTH123 -- fd 0, not a path
        0, encoding="utf-8", errors="replace", closefd=False
    ) as instream:
        server = Server(
            config_path, instream, out, env_path=env_path, verbosity=verbosity
        )
        asyncio.run(server.serve())
    out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m corpusqa.gui_backend``.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(prog="corpusqa gui-backend")
    parser.add_argument("--config", type=Path, default=Path("corpusqa.yaml"))
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=".env file to load (default: the one next to the config file)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)
    return run_backend(args.config, args.verbose, args.env_file)
