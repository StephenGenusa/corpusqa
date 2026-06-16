"""Query orchestration: cards -> route -> extract -> synthesize.

Headless library function; the CLI renders its outputs and owns the
interactive confirmation. Costs are aggregated per task and recorded to
``run_costs`` (design doc sections 4.4, 7).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from corpusqa.catalog.store import CatalogStore
from corpusqa.catalog.summarizer import CardReport, generate_cards
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import BudgetExceededError, CorpusQAError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.query.budget import CostEstimate, estimate_pass1, estimate_query
from corpusqa.query.extractor import ExtractionResult, extract_file, plan_sections
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
    except (ValueError, OSError, TypeError):
        return []


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

    Raises:
        BudgetExceededError: Cumulative estimate over threshold, not confirmed.
    """
    cumulative = prior_usd + estimate.total_usd
    if cumulative <= config.budget.confirm_above_usd:
        return
    if confirm is not None and confirm(estimate):
        return
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
        pending_tokens.append(
            client.count_tokens(TaskName.CATALOG_SUMMARIZE, markdown)
        )
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
    question_hash = hashlib.sha256(
        f"{question}\x00{definition or ''}\x00{relevance}".encode()
    ).hexdigest()

    with CatalogStore(db_path) as store:
        parseable = store.all_parseable_files()
        if mode == "auto":
            mode = (
                "sweep" if len(parseable) <= config.query.sweep_threshold else "route"
            )

        pass1_usd = 0.0
        if mode == "sweep":
            # no cards, no routing: every parseable file's raw text is judged
            card_report = CardReport()
            candidates = [Candidate(file=f, reason="sweep") for f in parseable]
        else:
            # Gate pass 1 BEFORE it spends: lazy card backfill (every
            # uncarded file's markdown through the catalog model) plus
            # routing was historically the largest unguarded cost.
            pass1_estimate = _pass1_estimate(store, cache_dir, client, config)
            _check_budget_gate(pass1_estimate, config, confirm, "cataloging+routing")
            pass1_usd = pass1_estimate.total_usd
            card_report = await generate_cards(store, cache_dir, client, config)
            candidates = await route(question, definition, store, client, config)
            # A file whose card generation failed has no card row, so the
            # router never sees it. Excluding it would be exactly the silent
            # recall loss the design exists to prevent, so force such files
            # into pass 2 (which judges raw text and corrects the verdict).
            routed = {c.file.file_hash for c in candidates}
            failed_paths = {p for p, _ in card_report.failed}
            for f in parseable:
                if f.file_hash not in routed and f.rel_path in failed_paths:
                    candidates.append(
                        Candidate(file=f, reason="card generation failed; "
                                  "included for recall")
                    )

        extract_model = config.tasks[TaskName.EXTRACT].model
        markdowns: dict[str, str] = {}
        file_tokens: list[int] = []
        cached_eval: dict[str, str | None] = {}
        planned_sections: dict[str, list[str]] = {}
        page_maps: dict[str, list[tuple[int, int]]] = {}
        total_calls = 0
        for cand in candidates:
            h = cand.file.file_hash
            md = (cache_dir / f"{h}.md").read_text(encoding="utf-8")
            markdowns[h] = md
            file_tokens.append(client.count_tokens(TaskName.EXTRACT, md))
            cached_json = (
                store.get_evaluation(question_hash, h, extract_model)
                if use_cache
                else None
            )
            cached_eval[h] = cached_json
            # Only non-cached files do LLM work; plan their sections so the
            # progress total reflects real calls (and reuse the split below).
            if cached_json is None:
                sections = plan_sections(md, client, config)
                planned_sections[h] = sections
                total_calls += len(sections)
                page_maps[h] = _load_page_map(cache_dir, h)
        estimate = estimate_query(file_tokens, config)

        _check_budget_gate(
            estimate, config, confirm, "extraction+synthesis", prior_usd=pass1_usd
        )

        failures: list[tuple[str, str]] = []
        cached = 0
        calls_done = 0
        calls_ticked: dict[str, int] = {}

        def _tick(file_hash: str) -> None:
            nonlocal calls_done
            calls_done += 1
            calls_ticked[file_hash] = calls_ticked.get(file_hash, 0) + 1
            if on_progress is not None and total_calls:
                on_progress(calls_done, total_calls)

        async def one(cand: Candidate) -> tuple[str, ExtractionResult] | None:
            nonlocal cached, calls_done
            h = cand.file.file_hash
            try:
                cached_json = cached_eval[h]
                if cached_json is not None:
                    result = ExtractionResult.model_validate_json(cached_json)
                    cached += 1
                else:
                    result = await extract_file(
                        question,
                        definition,
                        cand.file.rel_path,
                        h,
                        markdowns[h],
                        client,
                        config,
                        relevance=relevance,
                        sections=planned_sections[h],
                        on_section=lambda fh=h: _tick(fh),
                        page_map=page_maps.get(h),
                    )
                    store.put_evaluation(
                        run_id,
                        question_hash,
                        h,
                        extract_model,
                        result.model_dump_json(),
                        question=question,
                    )
            except CorpusQAError as exc:
                failures.append((cand.file.rel_path, str(exc)))
                return None
            finally:
                # If the file ended before all its planned sections ticked
                # (e.g. a section raised), credit the remainder so the bar
                # still reaches 100% rather than stalling short.
                remaining = len(planned_sections.get(h, ())) - calls_ticked.get(h, 0)
                if remaining > 0:
                    calls_done += remaining
                    if on_progress is not None and total_calls:
                        on_progress(calls_done, total_calls)
            return (cand.file.rel_path, result)

        _log.info(
            "run=%s mode=%s candidates=%d relevance=%s calls=%d",
            run_id,
            mode,
            len(candidates),
            relevance,
            total_calls,
        )
        # Show the bar at 0/total before work starts so the user sees scope.
        if on_progress is not None and total_calls:
            on_progress(0, total_calls)
        gathered = await asyncio.gather(*(one(c) for c in candidates))
        results = [g for g in gathered if g is not None]
        store.prune_evaluations(config.query.eval_retention_runs)
        _log.info(
            "run=%s relevant=%d cached=%d failures=%d",
            run_id,
            sum(1 for _, r in results if r.relevant),
            cached,
            len(failures),
        )

        answer = (
            await synthesize(question, results, client, config)
            if synthesize_answer
            else None
        )

        for task, (t_in, t_out, usd) in client.usage_by_task().items():
            store.record_cost(
                run_id, task.value, config.tasks[task].model, t_in, t_out, usd
            )
        return QueryReport(
            answer=answer,
            findings=results,
            candidates=candidates,
            relevant_files=[p for p, r in results if r.relevant],
            estimate=estimate,
            card_report=card_report,
            extraction_failures=failures,
            actual_usage=client.usage_by_task(),
            mode=mode,
            cached_evaluations=cached,
        )


async def run_estimate(
    corpus_root: Path,
    question: str,
    config: AppConfig,
    definition: str | None = None,
    client: LLMTaskClient | None = None,
    confirm: Callable[[CostEstimate], bool] | None = None,
) -> tuple[list[Candidate], CostEstimate]:
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
        ``(candidates, estimate)``.

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
        await generate_cards(store, cache_dir, client, config)
        candidates = await route(question, definition, store, client, config)
        tokens = [
            client.count_tokens(
                TaskName.EXTRACT,
                (cache_dir / f"{c.file.file_hash}.md").read_text(encoding="utf-8"),
            )
            for c in candidates
        ]
        return candidates, estimate_query(tokens, config)