"""Query orchestration: cards -> route -> extract -> synthesize.

Headless library function; the CLI renders its outputs and owns the
interactive confirmation. Costs are aggregated per task and recorded to
``run_costs`` (design doc sections 4.4, 7).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from corpusqa.catalog.store import CatalogStore, FileRow
from corpusqa.catalog.summarizer import CardReport, generate_cards
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import BudgetExceededError, CorpusQAError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.llm.tasks import Completion, LLMTaskClient
from corpusqa.query.budget import CostEstimate, estimate_pass1, estimate_query
from corpusqa.query.extractor import (
    ExtractionResult,
    PartialExtractionError,
    extract_file,
    extraction_fingerprint,
    plan_sections,
    section_token_budget,
)
from corpusqa.query.router import Candidate, route
from corpusqa.query.synthesizer import CitedAnswer, synthesize

_log = logging.getLogger("corpusqa.query")


def _load_page_map(cache_dir: Path, file_hash: str) -> list[tuple[int, int]]:
    """Loads the ``(offset, page)`` marks written at index time, or [] if none."""
    path = cache_dir / f"{file_hash}.pages.json"
    if not path.exists():
        return []
    try:
        return [(int(o), int(p)) for o, p in json.loads(path.read_text("utf-8"))]
    except (ValueError, OSError, TypeError) as exc:
        _log.warning(
            "page numbers unavailable: %s is unreadable (%s); re-index with "
            "--force to rebuild it",
            path.name,
            exc,
        )
        return []


def _include_failed_cards(
    candidates: list[Candidate], parseable: list[FileRow], card_report: CardReport
) -> None:
    """Adds files whose card generation failed to ``candidates`` in place.

    Such a file has no card row, so the router never sees it. Excluding it
    would be exactly the silent recall loss the design exists to prevent, so
    it goes to pass 2, which judges raw text and corrects the verdict.
    """
    routed = {c.file.file_hash for c in candidates}
    failed_paths = {p for p, _ in card_report.failed}
    for f in parseable:
        if f.file_hash not in routed and f.rel_path in failed_paths:
            candidates.append(
                Candidate(file=f, reason="card generation failed; included for recall")
            )


def _check_budget_gate(
    estimate: CostEstimate,
    config: AppConfig,
    confirm: Callable[[CostEstimate], bool] | None,
    stage: str,
    prior_usd: float = 0.0,
) -> None:
    """Aborts (or asks) when CUMULATIVE projected cost exceeds the threshold.

    Every spending stage passes through here BEFORE its first LLM call:
    cataloging+routing (pass 1) and extraction+synthesis (pass 2). The
    threshold applies to the run as a whole, so ``prior_usd`` carries
    already-projected spend from earlier stages; otherwise two stages each
    just under the threshold could together spend ~2x it without ever
    prompting.

    A stage with an unpriced task always asks: its true cost is unknown, so
    a $0 projection proves nothing.

    Raises:
        BudgetExceededError: Cumulative estimate over threshold (or a task
            unpriced), not confirmed.
    """
    cumulative = prior_usd + estimate.total_usd
    if cumulative <= config.budget.confirm_above_usd and not estimate.unpriced_tasks:
        return
    if confirm is not None and confirm(estimate):
        return
    if estimate.unpriced_tasks:
        raise BudgetExceededError(
            f"{stage}: no price is known for task(s) "
            f"{', '.join(estimate.unpriced_tasks)}, so the cost of this run "
            "cannot be projected and it was not confirmed (set "
            "cost_per_mtok_in/cost_per_mtok_out for the task, or use --yes)"
        )
    raise BudgetExceededError(
        f"estimated cumulative cost ${cumulative:.2f} (this stage: {stage} "
        f"${estimate.total_usd:.2f}) exceeds the "
        f"${config.budget.confirm_above_usd:.2f} threshold and was not "
        "confirmed (use --yes to skip the gate)"
    )


def _pass1_estimate(
    store: CatalogStore,
    cache_dir: Path,
    client: LLMTaskClient,
    config: AppConfig,
) -> CostEstimate:
    """Projects card-generation + routing cost from the current catalog.

    Big files are cataloged by chunk-then-merge, so their full length is sent
    (across several calls), not a truncated prefix. The projection counts the
    whole markdown to track real spend rather than under-counting.
    """
    pending_tokens: list[int] = []
    for row in store.files_needing_cards():
        markdown = (cache_dir / f"{row.file_hash}.md").read_text(encoding="utf-8")
        pending_tokens.append(client.count_tokens(TaskName.CATALOG_SUMMARIZE, markdown))
    return estimate_pass1(pending_tokens, len(store.all_parseable_files()), config)


@dataclass
class QueryReport:
    """Everything the CLI needs to render one query run.

    Attributes:
        answer: The synthesized cited answer, or None when synthesis was not
            requested (the default; hits are rendered from ``findings``).
        findings: ``(rel_path, extraction)`` pairs for every evaluated file --
            the raw hits the default output renders directly.
        candidates: Files selected by pass 1.
        relevant_files: Files pass 2 judged relevant.
        estimate: The pre-flight projection.
        card_report: Card generation activity this run (lazy backfill).
        mode: ``route`` or ``sweep`` -- what actually ran (after auto).
        cached_evaluations: Files answered from the evaluation cache.
        extraction_failures: ``(rel_path, error)`` pairs; partial results
            are still rendered (exit code 2 at the CLI).
        unpriced_calls: Calls per task whose serving model has no known
            price; ``actual_usage`` understates cost when non-empty.
    """

    answer: CitedAnswer | None
    candidates: list[Candidate]
    relevant_files: list[str]
    estimate: CostEstimate
    card_report: CardReport
    findings: list[tuple[str, ExtractionResult]] = field(default_factory=list)
    extraction_failures: list[tuple[str, str]] = field(default_factory=list)
    actual_usage: dict[TaskName, tuple[int, int, float]] = field(default_factory=dict)
    mode: str = "route"
    cached_evaluations: int = 0
    unpriced_calls: dict[TaskName, int] = field(default_factory=dict)


async def run_query(
    corpus_root: Path,
    question: str,
    config: AppConfig,
    *,
    definition: str | None = None,
    all_files: bool = False,
    mode: str = "auto",
    relevance: str = "recall",
    use_cache: bool = True,
    synthesize_answer: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    confirm: Callable[[CostEstimate], bool] | None = None,
    client: LLMTaskClient | None = None,
) -> QueryReport:
    """Answers a question over an indexed corpus.

    Catalog cards are backfilled lazily for any files missing them (keeps
    ``index`` LLM-free and the cataloging resumable).

    Args:
        corpus_root: Directory of source documents (must be indexed).
        question: The user question.
        config: Application configuration.
        definition: Optional user topic definition.
        all_files: Legacy alias for ``mode='sweep'``.
        mode: ``auto | route | sweep``. ``sweep`` evaluates every parseable
            file's raw text (no cards, no routing -- the recall guarantee).
            ``auto`` sweeps at or below ``query.sweep_threshold`` files.
        relevance: ``recall | balanced | strict`` extraction inclusion bar.
        use_cache: Reuse persisted evaluations for the same
            (question, definition, relevance, file, model); repeated and
            crash-interrupted sweeps become cheap.
        synthesize_answer: When True, run the reduce step to compose a single
            cited prose answer (``report.answer``). Default False: the report
            carries the raw findings and the CLI displays the hits directly --
            no extra LLM call, nothing paraphrased.
        on_progress: Called with (calls_done, calls_total) as each extraction
            LLM call (one per section) completes. The total counts only
            non-cached files' sections -- actual LLM work -- so the bar and ETA
            track LLM throughput, not file count. Not called when there is no
            LLM work to do.
        client: Injectable task client (M4-C1 test/eval seam); a real
            ``LLMTaskClient`` is constructed when omitted.
        confirm: Callback deciding whether to proceed at the given
            estimate; required when the estimate exceeds
            ``budget.confirm_above_usd``. None means non-interactive.

    Returns:
        The query report.

    Raises:
        BudgetExceededError: Estimate over threshold and not confirmed.
        CorpusQAError: For corpus-level failures (no index, no cards).
    """
    corpus_root = corpus_root.resolve()
    _, db_path, cache_dir = index_paths(corpus_root)
    if not db_path.exists():
        raise CorpusQAError(
            f"no index at {corpus_root}; run: corpusqa index {corpus_root}"
        )
    client = client or LLMTaskClient(config)
    run_id = uuid.uuid4().hex[:12]

    if all_files:
        mode = "sweep"
    question_hash = _question_hash(question, definition, relevance, config)

    with CatalogStore(db_path) as store:
        parseable = store.all_parseable_files()
        if mode == "auto":
            mode = (
                "sweep" if len(parseable) <= config.query.sweep_threshold else "route"
            )
        candidates, card_report, pass1_usd = await _select_candidates(
            mode,
            question,
            definition,
            store,
            parseable,
            cache_dir,
            client,
            config,
            confirm,
        )
        plan = _plan_pass2(
            candidates, store, cache_dir, client, config, question_hash, use_cache
        )
        estimate = estimate_query(plan.file_tokens, config)
        _check_budget_gate(
            estimate, config, confirm, "extraction+synthesis", prior_usd=pass1_usd
        )

        _log.info(
            "run=%s mode=%s candidates=%d relevance=%s calls=%d",
            run_id,
            mode,
            len(candidates),
            relevance,
            plan.total_calls,
        )
        runner = _Pass2Runner(
            plan=plan,
            store=store,
            client=client,
            config=config,
            question=question,
            definition=definition,
            relevance=relevance,
            run_id=run_id,
            question_hash=question_hash,
            on_progress=on_progress,
        )
        results = await runner.run(candidates)
        store.prune_evaluations(config.query.eval_retention_runs)
        _log.info(
            "run=%s relevant=%d cached=%d failures=%d",
            run_id,
            sum(1 for _, r in results if r.relevant),
            runner.cached,
            len(runner.failures),
        )

        answer = (
            await synthesize(question, results, client, config)
            if synthesize_answer
            else None
        )
        _record_costs(store, run_id, client, config)
        return QueryReport(
            answer=answer,
            findings=results,
            candidates=candidates,
            relevant_files=[p for p, r in results if r.relevant],
            estimate=estimate,
            card_report=card_report,
            extraction_failures=runner.failures,
            actual_usage=client.usage_by_task(),
            unpriced_calls=client.unpriced_by_task(),
            mode=mode,
            cached_evaluations=runner.cached,
        )


def _question_hash(
    question: str, definition: str | None, relevance: str, config: AppConfig
) -> str:
    """Evaluation-cache key for one question under the current setup.

    The extraction-prompt fingerprint is part of it: a prompt edit must not
    keep serving verdicts the current prompt never produced. So is the
    section size, since a verdict depends on how the file was sectioned.
    """
    return hashlib.sha256(
        f"{question}\x00{definition or ''}\x00{relevance}\x00"
        f"{extraction_fingerprint()}\x00{section_token_budget(config)}".encode()
    ).hexdigest()


async def _select_candidates(
    mode: str,
    question: str,
    definition: str | None,
    store: CatalogStore,
    parseable: list[FileRow],
    cache_dir: Path,
    client: LLMTaskClient,
    config: AppConfig,
    confirm: Callable[[CostEstimate], bool] | None,
) -> tuple[list[Candidate], CardReport, float]:
    """Picks the files pass 2 reads.

    Sweep takes every parseable file with no LLM spend. Route gates pass 1
    BEFORE it spends (lazy card backfill plus routing was historically the
    largest unguarded cost), then routes and adds files whose card failed.

    Returns:
        ``(candidates, card_report, pass1_projected_usd)``.
    """
    if mode == "sweep":
        return [Candidate(file=f, reason="sweep") for f in parseable], CardReport(), 0.0
    pass1_estimate = _pass1_estimate(store, cache_dir, client, config)
    _check_budget_gate(pass1_estimate, config, confirm, "cataloging+routing")
    card_report = await generate_cards(store, cache_dir, client, config)
    candidates = await route(question, definition, store, client, config)
    _include_failed_cards(candidates, parseable, card_report)
    return candidates, card_report, pass1_estimate.total_usd


@dataclass
class _Pass2Plan:
    """Per-candidate inputs for extraction, prepared before the pass-2 gate.

    Attributes:
        markdowns: Cached markdown by file hash.
        file_tokens: Extract-model token count per candidate, for the estimate.
        cached_eval: Cached verdict JSON by file hash (None when not cached).
        sections: Planned sections for each file that needs LLM work.
        page_maps: Page marks for each file that needs LLM work.
        total_calls: Extraction calls to make (for the progress total).
    """

    markdowns: dict[str, str] = field(default_factory=dict)
    file_tokens: list[int] = field(default_factory=list)
    cached_eval: dict[str, str | None] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    page_maps: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    total_calls: int = 0


def _plan_pass2(
    candidates: list[Candidate],
    store: CatalogStore,
    cache_dir: Path,
    client: LLMTaskClient,
    config: AppConfig,
    question_hash: str,
    use_cache: bool,
) -> _Pass2Plan:
    """Loads each candidate's markdown and cached verdict; plans the rest.

    Only non-cached files do LLM work; their sections are planned here so the
    progress total reflects real calls, and the same split is reused below.
    """
    extract_model = config.tasks[TaskName.EXTRACT].model
    plan = _Pass2Plan()
    for cand in candidates:
        h = cand.file.file_hash
        md = (cache_dir / f"{h}.md").read_text(encoding="utf-8")
        plan.markdowns[h] = md
        plan.file_tokens.append(client.count_tokens(TaskName.EXTRACT, md))
        cached_json = (
            store.get_evaluation(question_hash, h, extract_model) if use_cache else None
        )
        plan.cached_eval[h] = cached_json
        if cached_json is None:
            sections = plan_sections(md, client, config)
            plan.sections[h] = sections
            plan.total_calls += len(sections)
            plan.page_maps[h] = _load_page_map(cache_dir, h)
    return plan


@dataclass
class _Pass2Runner:
    """Runs extraction over all candidates concurrently, with progress.

    Attributes:
        failures: ``(rel_path, error)`` for files that failed or were partial.
        cached: Files answered from the evaluation cache.
    """

    plan: _Pass2Plan
    store: CatalogStore
    client: LLMTaskClient
    config: AppConfig
    question: str
    definition: str | None
    relevance: str
    run_id: str
    question_hash: str
    on_progress: Callable[[int, int], None] | None
    failures: list[tuple[str, str]] = field(default_factory=list)
    cached: int = 0
    _calls_done: int = 0
    _calls_ticked: dict[str, int] = field(default_factory=dict)

    async def run(
        self, candidates: list[Candidate]
    ) -> list[tuple[str, ExtractionResult]]:
        """Extracts every candidate; returns results for those that produced one."""
        # Show the bar at 0/total before work starts so the user sees scope.
        self._report_progress()
        gathered = await asyncio.gather(*(self._one(c) for c in candidates))
        return [g for g in gathered if g is not None]

    def _report_progress(self) -> None:
        if self.on_progress is not None and self.plan.total_calls:
            self.on_progress(self._calls_done, self.plan.total_calls)

    def _tick(self, file_hash: str) -> None:
        self._calls_done += 1
        self._calls_ticked[file_hash] = self._calls_ticked.get(file_hash, 0) + 1
        self._report_progress()

    async def _one(self, cand: Candidate) -> tuple[str, ExtractionResult] | None:
        h = cand.file.file_hash
        try:
            cached_json = self.plan.cached_eval[h]
            if cached_json is not None:
                result = ExtractionResult.model_validate_json(cached_json)
                self.cached += 1
            else:
                result = await self._extract(cand)
        except PartialExtractionError as exc:
            # Show what survived, but report the file and never cache it.
            self.failures.append((cand.file.rel_path, str(exc)))
            result = exc.result
        except CorpusQAError as exc:
            self.failures.append((cand.file.rel_path, str(exc)))
            return None
        finally:
            # If the file ended before all its planned sections ticked (e.g. a
            # section raised), credit the remainder so the bar still reaches
            # 100% rather than stalling short.
            planned = len(self.plan.sections.get(h, ()))
            remaining = planned - self._calls_ticked.get(h, 0)
            if remaining > 0:
                self._calls_done += remaining
                self._report_progress()
        return (cand.file.rel_path, result)

    async def _extract(self, cand: Candidate) -> ExtractionResult:
        """Runs extraction for one uncached file and caches a clean verdict."""
        h = cand.file.file_hash
        fallback_served: list[Completion] = []
        result = await extract_file(
            self.question,
            self.definition,
            cand.file.rel_path,
            h,
            self.plan.markdowns[h],
            self.client,
            self.config,
            relevance=self.relevance,
            sections=self.plan.sections[h],
            on_section=functools.partial(self._tick, h),
            page_map=self.plan.page_maps.get(h),
            on_completion=lambda c: fallback_served.append(c) if c.fallback else None,
        )
        # The key names the primary model; a verdict a fallback model
        # produced must not be served as the primary's.
        if not fallback_served:
            self.store.put_evaluation(
                self.run_id,
                self.question_hash,
                h,
                self.config.tasks[TaskName.EXTRACT].model,
                result.model_dump_json(),
                question=self.question,
            )
        return result


def _record_costs(
    store: CatalogStore, run_id: str, client: LLMTaskClient, config: AppConfig
) -> None:
    """Persists this run's actual per-task usage."""
    for task, (t_in, t_out, usd) in client.usage_by_task().items():
        store.record_cost(
            run_id, task.value, config.tasks[task].model, t_in, t_out, usd
        )


async def run_estimate(
    corpus_root: Path,
    question: str,
    config: AppConfig,
    definition: str | None = None,
    client: LLMTaskClient | None = None,
    confirm: Callable[[CostEstimate], bool] | None = None,
) -> tuple[list[Candidate], CostEstimate, CardReport]:
    """Runs pass 1 and projects pass-2/synthesize cost.

    Pass 1 itself costs money (card backfill + routing), so it is gated
    exactly like ``run_query``: a projection above the threshold requires
    confirmation before any LLM call.

    Args:
        corpus_root: Directory of source documents (must be indexed).
        question: The user question.
        config: Application configuration.
        definition: Optional user topic definition.
        client: Injectable task client (M4-C1 test/eval seam).
        confirm: Callback deciding whether to proceed when the pass-1
            estimate exceeds ``budget.confirm_above_usd``; None means
            non-interactive (the gate aborts).

    Returns:
        ``(candidates, estimate, card_report)``. Files whose card generation
        failed are included in ``candidates``, exactly as ``run_query`` would
        send them to pass 2.

    Raises:
        BudgetExceededError: Pass-1 estimate over threshold, not confirmed.
        CorpusQAError: If no index exists.
    """
    corpus_root = corpus_root.resolve()
    _, db_path, cache_dir = index_paths(corpus_root)
    if not db_path.exists():
        raise CorpusQAError(f"no index at {corpus_root}")
    client = client or LLMTaskClient(config)
    with CatalogStore(db_path) as store:
        pass1_estimate = _pass1_estimate(store, cache_dir, client, config)
        _check_budget_gate(pass1_estimate, config, confirm, "cataloging+routing")
        card_report = await generate_cards(store, cache_dir, client, config)
        candidates = await route(question, definition, store, client, config)
        _include_failed_cards(candidates, store.all_parseable_files(), card_report)
        tokens = [
            client.count_tokens(
                TaskName.EXTRACT,
                (cache_dir / f"{c.file.file_hash}.md").read_text(encoding="utf-8"),
            )
            for c in candidates
        ]
        return candidates, estimate_query(tokens, config), card_report
