"""Evaluation harness: routing recall/precision and citation validity.

Runs the QA pairs in ``tests/eval/qa_pairs.yaml`` against an indexed corpus
and prints a one-screen table (design doc section 11). Two modes:

* routing-only (default): runs pass 1 per pair -- cheap, measures the
  metric that matters most given the recall-biased design.
* ``--query``: full pipeline per pair; adds citation-validity and
  ``must_cite`` checks. Costs real money in live mode.

``--mock`` substitutes a deterministic keyword-overlap client so the
harness itself is testable (and CI-runnable) with zero LLM calls. Mock
numbers measure the HARNESS, not the prompts -- never quote them as
routing quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import CorpusQAError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.llm.tasks import Completion


class QAPair(BaseModel):
    """One evaluation case.

    Attributes:
        id: Short unique label for the table row.
        question: The question to route/answer.
        definition: Optional user topic definition.
        expected_files: rel_paths routing must include (recall).
        forbidden_files: rel_paths routing should exclude (precision signal).
        must_cite: Substrings required in answer text or sources
            (full-query mode only).
    """

    id: str
    question: str
    definition: str | None = None
    expected_files: list[str] = Field(default_factory=list)
    forbidden_files: list[str] = Field(default_factory=list)
    must_cite: list[str] = Field(default_factory=list)


@dataclass
class PairResult:
    """Metrics for one pair.

    Attributes:
        pair_id: The pair label.
        recall: |expected ∩ candidates| / |expected| (1.0 when none expected
            and none required).
        precision: |expected ∩ candidates| / |candidates| (1.0 on empty).
        forbidden_hits: Forbidden files that were routed in.
        candidates: Candidate count.
        cite_validity: Resolved cites / total cites (None in routing mode).
        must_cite_ok: All must_cite substrings present (None in routing mode).
        missing: Expected files not selected.
        stray: Selected files that were neither expected nor forbidden.
        error: Failure text when the pair could not run.
    """

    pair_id: str
    recall: float = 0.0
    precision: float = 0.0
    forbidden_hits: list[str] = field(default_factory=list)
    candidates: int = 0
    cite_validity: float | None = None
    must_cite_ok: bool | None = None
    missing: list[str] = field(default_factory=list)
    stray: list[str] = field(default_factory=list)
    error: str | None = None


def load_pairs(path: Path) -> list[QAPair]:
    """Loads and validates the QA pairs file.

    Args:
        path: Path to ``qa_pairs.yaml``.

    Returns:
        The validated pairs.

    Raises:
        CorpusQAError: On unreadable or invalid files.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return [QAPair.model_validate(p) for p in data["pairs"]]
    except (OSError, KeyError, ValueError) as exc:
        raise CorpusQAError(f"cannot load pairs from {path}: {exc}") from exc


def score_routing(pair: QAPair, candidate_paths: set[str]) -> PairResult:
    """Computes routing metrics for one pair (pure, unit-testable).

    Args:
        pair: The evaluation case.
        candidate_paths: rel_paths selected by pass 1.

    Returns:
        The partially filled result (citation fields left None).
    """
    expected = set(pair.expected_files)
    hit = expected & candidate_paths
    recall = (len(hit) / len(expected)) if expected else 1.0
    precision = (len(hit) / len(candidate_paths)) if candidate_paths else 1.0
    return PairResult(
        pair_id=pair.id,
        recall=recall,
        precision=precision,
        forbidden_hits=sorted(set(pair.forbidden_files) & candidate_paths),
        candidates=len(candidate_paths),
        missing=sorted(expected - candidate_paths),
        stray=sorted(candidate_paths - expected - set(pair.forbidden_files)),
    )


def score_citations(
    result: PairResult, pair: QAPair, answer_text: str, sources: str
) -> PairResult:
    """Adds citation metrics to a routing result (pure, unit-testable).

    Validity counts rendered citation markers: ``[path § heading...]`` are
    resolved, ``[uncited]`` are not.

    Args:
        result: The routing-stage result to extend.
        pair: The evaluation case.
        answer_text: Rendered answer body.
        sources: Rendered sources block.

    Returns:
        The result with ``cite_validity`` and ``must_cite_ok`` filled.
    """
    resolved = len(re.findall(r"\[[^\[\]]+ § [^\[\]]+\]", answer_text))
    unresolved = answer_text.count("[uncited]")
    total = resolved + unresolved
    result.cite_validity = (resolved / total) if total else 1.0
    haystack = answer_text + "\n" + sources
    result.must_cite_ok = all(s in haystack for s in pair.must_cite)
    return result


async def run_pairs(
    corpus_root: Path,
    pairs: list[QAPair],
    config: AppConfig,
    *,
    full_query: bool,
    sweep: bool = False,
    client_factory: Any | None = None,  # noqa: ANN401 -- test seam, any client-shaped factory
) -> list[PairResult]:
    """Evaluates all pairs against an indexed corpus.

    Args:
        corpus_root: The fixture (or real) corpus directory, already indexed.
        pairs: The evaluation cases.
        config: Application configuration.
        full_query: Also run pass 2 + synthesis for citation metrics.
        sweep: Run in sweep mode; selection is then post-extraction
            ``relevant_files`` (sweep has no routing stage to score), so
            recall measures extraction judgment, not selection.
        client_factory: Test seam; replaces ``LLMTaskClient`` when given.

    Returns:
        One result per pair; per-pair failures are captured, not raised.
    """
    from corpusqa.query import pipeline as qp

    results: list[PairResult] = []
    for pair in pairs:
        try:
            client = client_factory(config) if client_factory else None
            if full_query or sweep:
                report = await qp.run_query(
                    corpus_root,
                    pair.question,
                    config,
                    definition=pair.definition,
                    mode="sweep" if sweep else "route",
                    synthesize_answer=True,
                    confirm=lambda _e: True,
                    client=client,
                )
                paths = (
                    set(report.relevant_files)
                    if sweep
                    else {c.file.rel_path for c in report.candidates}
                )
                result = score_routing(pair, paths)
                result = score_citations(
                    result, pair, report.answer.text, report.answer.sources
                )
                total = report.answer.resolved_cites + report.answer.unresolved_cites
                if total:
                    result.cite_validity = report.answer.resolved_cites / total
            else:
                candidates, _ = await qp.run_estimate(
                    corpus_root,
                    pair.question,
                    config,
                    pair.definition,
                    client=client,
                    confirm=lambda _e: True,
                )
                result = score_routing(pair, {c.file.rel_path for c in candidates})
        except CorpusQAError as exc:
            result = PairResult(pair_id=pair.id, error=str(exc)[:80])
        results.append(result)
    return results


def render_table(results: list[PairResult], verbose: bool = False) -> str:
    """Renders the one-screen metrics table with an aggregate row."""
    header = (
        f"{'pair':<28}{'recall':>7}{'prec':>7}{'cand':>6}"
        f"{'forb':>6}{'cites':>7}{'must':>6}"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        if r.error:
            lines.append(f"{r.pair_id:<28}ERROR: {r.error}")
            continue
        cites = f"{r.cite_validity:.2f}" if r.cite_validity is not None else "-"
        must = (
            {True: "ok", False: "MISS"}[r.must_cite_ok]
            if r.must_cite_ok is not None
            else "-"
        )
        lines.append(
            f"{r.pair_id:<28}{r.recall:>7.2f}{r.precision:>7.2f}"
            f"{r.candidates:>6}{len(r.forbidden_hits):>6}{cites:>7}{must:>6}"
        )
        if verbose:
            for label, items in (
                ("missing", r.missing),
                ("stray", r.stray),
                ("forbidden", r.forbidden_hits),
            ):
                if items:
                    lines.append(f"    {label}: {', '.join(items[:6])}")
    scored = [r for r in results if not r.error]
    if scored:
        mean_recall = sum(r.recall for r in scored) / len(scored)
        mean_prec = sum(r.precision for r in scored) / len(scored)
        lines.append("-" * len(header))
        lines.append(
            f"{'MEAN (' + str(len(scored)) + ' pairs)':<28}"
            f"{mean_recall:>7.2f}{mean_prec:>7.2f}"
        )
    return "\n".join(lines)


class MockTaskClient:
    """Deterministic keyword-overlap client for harness self-testing.

    Routes by word overlap between question+definition and each card block;
    extracts a single finding per routed file; synthesizes by citing every
    evidence number. Measures the harness plumbing, never prompt quality.
    """

    def __init__(self, _config: AppConfig) -> None:
        """Accepts and ignores config (factory-compatible)."""
        self._ledger: dict[TaskName, list[Completion]] = {}

    @staticmethod
    def _words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{4,}", text.lower())}

    async def complete(
        self,
        task: TaskName,
        messages: list[dict[str, str]],
        **_: Any,  # noqa: ANN401
    ) -> Completion:
        """Produces deterministic JSON/text per task."""
        user = messages[-1]["content"]
        if task is TaskName.CATALOG_SUMMARIZE:
            body = user.split("<document>")[-1]
            words = sorted(self._words(body))[:8] or ["misc"]
            text = json.dumps(
                {
                    "claims": [" ".join(words)],
                    "terms": [],
                    "implicit_topics": words[:5],
                    "answerable": [],
                    "absences": [],
                    "entities": [],
                    "doc_type": "doc",
                }
            )
        elif task is TaskName.QUERY_ROUTE:
            question = user.split("Catalog cards:")[0]
            q_words = self._words(question)
            decisions = []
            # One block per rendered card: "FILE <hash>" up to the next
            # "FILE " or end of prompt. Matching the whole block (path,
            # claims, terms, ...) instead of a single labeled line keeps
            # this parser stable across card-render changes -- the drift
            # that previously broke mock mode silently.
            for file_hash, body in re.findall(
                r"FILE (\w+)\n(.*?)(?=\nFILE |\Z)", user, re.DOTALL
            ):
                overlap = len(q_words & self._words(body))
                decisions.append(
                    {
                        "file_hash": file_hash,
                        "include": overlap >= 2,
                        "reason": f"overlap={overlap}",
                    }
                )
            text = json.dumps({"decisions": decisions})
        elif task is TaskName.EXTRACT:
            file_hash = re.search(r"hash (\w+)", user).group(1)  # type: ignore[union-attr]
            heading = re.search(r"^##\s+(.+)$", user, re.MULTILINE)
            text = json.dumps(
                {
                    "file_hash": file_hash,
                    "relevant": True,
                    "confidence": 0.8,
                    "reasoning": "mock keyword-overlap verdict",
                    "findings": [
                        {
                            "claim": "mock finding",
                            "quote": "q",
                            "heading_path": [heading.group(1) if heading else "-"],
                            "page_no": None,
                        }
                    ],
                }
            )
        else:  # SYNTHESIZE / partials
            numbers = re.findall(r"^\[(\d+)\]", user, re.MULTILINE)
            text = " ".join(f"Mock claim [{n}]." for n in numbers) or "Nothing."
        completion = Completion(
            text=text,
            model="mock",
            tokens_in=len(user) // 4,
            tokens_out=len(text) // 4,
            cost_usd=0.0,
        )
        self._ledger.setdefault(task, []).append(completion)
        return completion

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns zero vectors (vector channel arrives in M5)."""
        return [[0.0] * 4 for _ in texts]

    def count_tokens(self, _task: TaskName, text: str) -> int:
        """~4 chars/token heuristic."""
        return max(1, len(text) // 4)

    def usage_by_task(self) -> dict[TaskName, tuple[int, int, float]]:
        """Aggregates the mock ledger."""
        return {
            t: (sum(c.tokens_in for c in cs), sum(c.tokens_out for c in cs), 0.0)
            for t, cs in self._ledger.items()
        }


def main(argv: list[str] | None = None) -> int:
    """``corpusqa-eval`` entry point.

    Returns:
        0 on success; 1 on errors or when ``--fail-under`` is not met.
    """
    parser = argparse.ArgumentParser(prog="corpusqa-eval")
    parser.add_argument("corpus", type=Path, help="indexed corpus directory")
    parser.add_argument(
        "--pairs",
        type=Path,
        default=Path("tests/eval/qa_pairs.yaml"),
    )
    parser.add_argument("--config", type=Path, default=Path("corpusqa.yaml"))
    parser.add_argument(
        "--query",
        action="store_true",
        help="full pipeline incl. citations (costs money)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="deterministic mock client (harness self-test)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "sweep arm: full-corpus raw-text evaluation (implies full "
            "pipeline; selection scored on post-extraction relevance)"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list missing/stray/forbidden files per pair",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit 1 if mean routing recall is below this",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        pairs = load_pairs(args.pairs)
        if not index_paths(args.corpus.resolve())[1].exists():
            raise CorpusQAError(
                f"corpus not indexed; run: corpusqa index {args.corpus}"
            )
        results = asyncio.run(
            run_pairs(
                args.corpus,
                pairs,
                config,
                full_query=args.query,
                sweep=args.sweep,
                client_factory=MockTaskClient if args.mock else None,
            )
        )
    except CorpusQAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(render_table(results, verbose=args.verbose))
    if args.mock:
        print("\n(mock mode: numbers measure the harness, not the prompts)")
    if args.fail_under is not None:
        scored = [r for r in results if not r.error]
        mean_recall = sum(r.recall for r in scored) / len(scored) if scored else 0.0
        if mean_recall < args.fail_under:
            print(
                f"FAIL: mean recall {mean_recall:.2f} < {args.fail_under}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())