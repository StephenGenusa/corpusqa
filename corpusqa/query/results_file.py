"""Results file: one run's question, settings and findings as JSON.

Written to ``<corpus>/.corpusqa/results/<started>-<question-slug>.json`` when
the run starts and rewritten (atomically) after every file and at the end, so
an interrupted run still leaves everything finished so far. A results file
can be reloaded (GUI "Open results…") without any model calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import CorpusQAError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.query.extractor import ExtractionResult
from corpusqa.query.synthesizer import CitedAnswer

_log = logging.getLogger("corpusqa.query")

# 2: adds ``grouping``. Version-1 files (no grouping) still load.
RESULTS_FORMAT_VERSION = 2
RESULTS_DIR_NAME = "results"
_SLUG_MAX = 60

Outcome = Literal["running", "finished", "cancelled", "failed"]
FileStatus = Literal["relevant", "not_relevant", "partial", "failed"]


class ResultsFileError(CorpusQAError):
    """A results file is missing, unreadable, malformed or from a newer version."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SavedFinding(_Model):
    """One finding as saved (system-derived fields included)."""

    claim: str
    quote: str
    heading_path: list[str] = Field(default_factory=list)
    page_label: str | None = None
    start: int | None = None
    end: int | None = None
    verified: bool = True


class SavedFile(_Model):
    """One file's outcome in the run.

    Attributes:
        rel_path: Path within the corpus.
        file_hash: Source hash when judged; a different current hash means
            the source changed since (offsets no longer apply).
        status: ``relevant``, ``not_relevant``, ``partial`` (some sections
            failed; surviving findings kept) or ``failed``.
        confidence: Verdict confidence; None when the file failed.
        reasoning: The model's justification of the verdict.
        error: Why the file failed or is partial.
        findings: Cited findings (relevant or partial files).
    """

    rel_path: str
    file_hash: str
    status: FileStatus
    confidence: float | None = None
    reasoning: str = ""
    error: str | None = None
    findings: list[SavedFinding] = Field(default_factory=list)


class SavedAnswer(_Model):
    """The synthesized answer, when synthesis ran."""

    text: str
    sources: str
    uncited_claims: list[str] = Field(default_factory=list)


class SavedHitRef(_Model):
    """A hit: finding ``index`` of the saved file ``file``."""

    file: str
    index: int


class SavedGroup(_Model):
    """One distinct answer and the hits that give it."""

    answer: str
    hits: list[SavedHitRef]
    volumes: int


class SavedGrouping(_Model):
    """Distinct answers found among a run's hits ("Group answers").

    Attributes:
        model: Model that grouped them.
        created_at: ISO-8601 UTC time.
        groups: Distinct answers, most widely supported first.
        ungrouped: Hits the model did not place (kept, never dropped).
        problems: Grouping calls that failed and what happened.
    """

    model: str
    created_at: str
    groups: list[SavedGroup] = Field(default_factory=list)
    ungrouped: list[SavedHitRef] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class TaskUse(_Model):
    """Provider and model a task was configured with."""

    provider: str
    model: str


class SavedRun(_Model):
    """A whole results file.

    Attributes:
        format_version: Schema version of this file.
        question: The question, verbatim.
        definition: The topic definition, or None.
        relevance: ``recall | balanced | strict``.
        mode_requested: Mode asked for (``auto`` included).
        mode: Mode that actually ran, once known.
        corpus: Absolute corpus folder.
        started_at: ISO-8601 UTC start time.
        finished_at: ISO-8601 UTC end time; None while running.
        outcome: ``running``, ``finished``, ``cancelled`` or ``failed``.
        error: Why the run was cancelled or failed.
        tasks: Provider and model per task.
        files: Per-file outcomes, in the order they finished.
        answer: Synthesized answer, if any.
        grouping: Distinct answers among the hits, once grouped.
    """

    format_version: int
    question: str
    definition: str | None = None
    relevance: str
    mode_requested: str
    mode: str | None = None
    corpus: str
    started_at: str
    finished_at: str | None = None
    outcome: Outcome = "running"
    error: str | None = None
    tasks: dict[str, TaskUse]
    files: list[SavedFile] = Field(default_factory=list)
    answer: SavedAnswer | None = None
    grouping: SavedGrouping | None = None


@dataclass(frozen=True)
class ResultsSummary:
    """One line of a results listing."""

    path: Path
    question: str
    started_at: str
    outcome: str
    files: int
    findings: int


def results_dir(corpus_root: Path) -> Path:
    """Returns the folder holding a corpus's results files."""
    return index_paths(corpus_root.resolve())[0] / RESULTS_DIR_NAME


def _slug(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > _SLUG_MAX:
            break
        slug = candidate
    return slug or "question"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


class ResultsWriter:
    """Writes one run's results file as the run progresses.

    Writing never raises into the run: a failure (e.g. a full disk) is
    logged once and kept in ``write_error``, and later writes are retried.
    """

    def __init__(self, path: Path, run: SavedRun) -> None:
        """Initializes the writer and writes the file once.

        Args:
            path: Where the file goes.
            run: Its initial content.
        """
        self.path = path
        self._run = run
        self._files: dict[str, SavedFile] = {}
        self.write_error: str | None = None
        self._write()

    @classmethod
    def create(
        cls,
        corpus_root: Path,
        *,
        question: str,
        definition: str | None,
        relevance: str,
        mode: str,
        config: AppConfig,
        now: datetime | None = None,
    ) -> ResultsWriter:
        """Starts a results file for a new run under the corpus.

        Args:
            corpus_root: The corpus folder (its ``.corpusqa`` must be writable).
            question: The question, verbatim.
            definition: Optional topic definition.
            relevance: Extraction inclusion bar.
            mode: Requested mode.
            config: Configuration the run uses (providers and models recorded).
            now: Start time (tests); defaults to the current time.

        Returns:
            The writer; its file already exists.
        """
        started = now or datetime.now(UTC)
        folder = results_dir(corpus_root)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{started.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{_slug(question)}"
        path = folder / f"{stem}.json"
        n = 2
        while path.exists():
            path = folder / f"{stem}-{n}.json"
            n += 1
        run = SavedRun(
            format_version=RESULTS_FORMAT_VERSION,
            question=question,
            definition=definition,
            relevance=relevance,
            mode_requested=mode,
            corpus=str(corpus_root.resolve()),
            started_at=_iso(started),
            tasks={
                task.value: TaskUse(
                    provider=config.tasks[task].provider,
                    model=config.tasks[task].model,
                )
                for task in TaskName
            },
        )
        return cls(path, run)

    def set_mode(self, mode: str) -> None:
        """Records the mode that actually ran."""
        self._run.mode = mode
        self._write()

    def add_file(
        self,
        rel_path: str,
        file_hash: str,
        result: ExtractionResult | None,
        error: str | None = None,
    ) -> None:
        """Records one file's outcome and rewrites the file.

        Args:
            rel_path: Path within the corpus.
            file_hash: The judged source's hash.
            result: Its verdict; None when the file failed outright.
            error: Why it failed, or why it is partial (with a result).
        """
        status: FileStatus
        if result is None:
            status = "failed"
        elif error is not None:
            status = "partial"
        else:
            status = "relevant" if result.relevant else "not_relevant"
        self._files[rel_path] = SavedFile(
            rel_path=rel_path,
            file_hash=file_hash,
            status=status,
            confidence=result.confidence if result is not None else None,
            reasoning=result.reasoning if result is not None else "",
            error=error,
            findings=[
                SavedFinding(
                    claim=f.claim,
                    quote=f.quote,
                    heading_path=f.heading_path,
                    page_label=f.page_label,
                    start=f.start,
                    end=f.end,
                    verified=f.quote_verified,
                )
                for f in (result.findings if result is not None else [])
            ],
        )
        self._write()

    def set_answer(self, answer: CitedAnswer) -> None:
        """Records the synthesized answer."""
        self._run.answer = SavedAnswer(
            text=answer.text,
            sources=answer.sources,
            uncited_claims=list(answer.uncited_claims),
        )
        self._write()

    def finish(self, outcome: Outcome, error: str | None = None) -> None:
        """Records how the run ended and when."""
        self._run.outcome = outcome
        self._run.error = error
        self._run.finished_at = _iso(datetime.now(UTC))
        self._write()

    def _write(self) -> None:
        self._run.files = list(self._files.values())
        try:
            _atomic_write(self.path, self._run.model_dump_json(indent=2))
        except OSError as exc:
            if self.write_error is None:
                _log.warning("cannot write results file %s: %s", self.path, exc)
            self.write_error = f"{type(exc).__name__}: {exc}"
            return
        self.write_error = None


def _atomic_write(path: Path, text: str) -> None:
    """Replaces ``path`` with ``text`` so readers never see a partial file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_grouping(path: Path, grouping: SavedGrouping) -> SavedRun:
    """Stores (replacing any earlier) grouped answers in a results file.

    Raises:
        ResultsFileError: The file cannot be read, is not a results file, or
            cannot be written.
    """
    run = load_results(path)
    run.format_version = RESULTS_FORMAT_VERSION
    run.grouping = grouping
    try:
        _atomic_write(path, run.model_dump_json(indent=2))
    except OSError as exc:
        raise ResultsFileError(f"cannot write {path}: {exc}") from exc
    return run


def load_results(path: Path) -> SavedRun:
    """Reads and validates a results file.

    Raises:
        ResultsFileError: Missing/unreadable file, not a results file, or a
            format newer than this version understands.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResultsFileError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResultsFileError(f"{path} is not a corpusqa results file") from exc
    version = data.get("format_version") if isinstance(data, dict) else None
    if isinstance(version, int) and version > RESULTS_FORMAT_VERSION:
        raise ResultsFileError(
            f"{path} was written by a newer corpusqa (results format {version}); "
            "update corpusqa to open it"
        )
    try:
        return SavedRun.model_validate(data)
    except ValidationError as exc:
        raise ResultsFileError(f"{path} is not a corpusqa results file") from exc


def list_results(corpus_root: Path) -> list[ResultsSummary]:
    """Lists a corpus's results files, newest first; unreadable ones skipped."""
    folder = results_dir(corpus_root)
    if not folder.is_dir():
        return []
    out: list[ResultsSummary] = []
    for path in folder.glob("*.json"):
        if path.name.startswith(".tmp-"):
            continue
        try:
            run = load_results(path)
        except ResultsFileError as exc:
            _log.info("skipping %s: %s", path, exc)
            continue
        out.append(
            ResultsSummary(
                path=path,
                question=run.question,
                started_at=run.started_at,
                outcome=run.outcome,
                files=len(run.files),
                findings=sum(len(f.findings) for f in run.files),
            )
        )
    out.sort(key=lambda s: (s.started_at, s.path.name), reverse=True)
    return out
