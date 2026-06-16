"""corpusqa command-line interface.

Composition root: the only module that constructs concrete implementations
behind the protocols. Library code raises typed errors; this layer maps them
to messages and exit codes (0 ok, 1 user/config error, 2 partial failure).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig
from corpusqa.errors import BudgetExceededError, CorpusQAError
from corpusqa.ingest.pipeline import index_paths, run_index, run_status
from corpusqa.logging_setup import configure as configure_logging
from corpusqa.query.budget import CostEstimate
from corpusqa.query.hits import render_hits
from corpusqa.query.pipeline import run_estimate, run_query

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_PARTIAL = 2

_DEFAULT_CONFIG = Path("corpusqa.yaml")


def _build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument surface (design doc section 7)."""
    parser = argparse.ArgumentParser(prog="corpusqa")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="parse, catalog, and embed a directory")
    p_index.add_argument("directory", type=Path)
    p_index.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_index.add_argument("--force", nargs="*", type=Path, default=[])

    p_query = sub.add_parser("query", help="answer a question over the corpus")
    p_query.add_argument("question")
    p_query.add_argument("directory", type=Path, nargs="?", default=Path("."))
    p_query.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_query.add_argument("--all-files", action="store_true")
    p_query.add_argument("--mode", choices=["auto", "route", "sweep"], default="auto")
    p_query.add_argument(
        "--relevance", choices=["recall", "balanced", "strict"], default="recall"
    )
    p_query.add_argument("--no-cache", action="store_true")
    p_query.add_argument("--definition", default=None)
    p_query.add_argument("--yes", action="store_true")
    p_query.add_argument("--show-cost", action="store_true")
    p_query.add_argument(
        "--synthesize",
        action="store_true",
        help="compose a single cited prose answer (extra LLM step) instead of "
        "displaying the raw hits",
    )

    p_status = sub.add_parser("status", help="drift report and flagged files")
    p_status.add_argument("directory", type=Path, nargs="?", default=Path("."))
    p_status.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    p_explain = sub.add_parser(
        "explain", help="show persisted relevance verdicts for one file"
    )
    p_explain.add_argument("file", type=Path, help="path of the corpus file")
    p_explain.add_argument("directory", type=Path, nargs="?", default=Path("."))
    p_explain.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    p_est = sub.add_parser("estimate", help="pass-1 + cost projection, no pass 2")
    p_est.add_argument("question")
    p_est.add_argument("directory", type=Path, nargs="?", default=Path("."))
    p_est.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_est.add_argument("--yes", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args(argv)
    log_dir = None
    if args.command in ("index", "query", "estimate"):
        log_dir = index_paths(args.directory.resolve())[0] / "logs"
    log_path = configure_logging(args.verbose, log_dir)
    if log_path is not None and args.verbose:
        print(f"log: {log_path}", file=sys.stderr)

    try:
        config = load_config(args.config)
    except CorpusQAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR

    try:
        if args.command == "index":
            return _cmd_index(args, config)
        if args.command == "status":
            return _cmd_status(args, config)
        if args.command == "query":
            return _cmd_query(args, config)
        if args.command == "estimate":
            return _cmd_estimate(args, config)
        if args.command == "explain":
            return _cmd_explain(args, config)
    except CorpusQAError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"unknown command '{args.command}'", file=sys.stderr)
    return EXIT_USER_ERROR


def _warn_drift(corpus: Path, config: AppConfig) -> None:
    """Warns (never auto-reindexes) when the corpus drifted from the index."""
    report = run_status(corpus, config)
    if report.drift:
        print(
            f"warning: {len(report.drift)} file(s) changed since indexing; "
            f"queries cannot see them. Run: corpusqa index {corpus}",
            file=sys.stderr,
        )
    for row in report.flagged:
        print(
            f"warning: {row.rel_path} is '{row.parse_status}' -- not fully "
            "visible to queries",
            file=sys.stderr,
        )


def _confirm(estimate: CostEstimate) -> bool:
    """Interactive spend confirmation."""
    print(
        f"estimated cost ${estimate.total_usd:.2f} across "
        f"{estimate.candidate_files} file(s) "
        f"({estimate.total_input_tokens} input tokens)."
    )
    return input("proceed? [y/N] ").strip().lower() in ("y", "yes")


class _Progress:
    """tqdm-backed extraction progress (rate + ETA) on stderr.

    The pipeline reports absolute ``(calls_done, calls_total)`` over LLM
    extraction calls -- one per document section -- so the bar advances within
    large files (not just at file boundaries) and the rate/ETA reflect how
    fast the model is actually serving calls. The bar is created lazily on the
    first report, when the total is known.
    """

    def __init__(self) -> None:
        self._bar = None

    def __call__(self, done: int, total: int) -> None:
        if self._bar is None:
            from tqdm import tqdm

            self._bar = tqdm(
                total=total,
                desc="  evaluating",
                unit="call",
                dynamic_ncols=True,
                file=sys.stderr,
            )
        self._bar.update(done - self._bar.n)  # report is absolute; bar wants a delta
        if done >= total:
            self.close()

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def _cmd_query(args: argparse.Namespace, config: AppConfig) -> int:
    """Runs the full query pipeline and renders the cited answer."""
    import asyncio

    corpus = args.directory
    _warn_drift(corpus, config)
    progress = _Progress()
    try:
        report = asyncio.run(
            run_query(
                corpus,
                args.question,
                config,
                definition=args.definition,
                all_files=args.all_files,
                mode=args.mode,
                relevance=args.relevance,
                use_cache=not args.no_cache,
                synthesize_answer=args.synthesize,
                on_progress=progress,
                confirm=(lambda e: True) if args.yes else _confirm,
            )
        )
    except BudgetExceededError as exc:
        progress.close()
        print(f"aborted: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    finally:
        progress.close()

    if report.card_report.failed:
        for rel_path, error in report.card_report.failed:
            print(f"card failed: {rel_path}: {error}", file=sys.stderr)
    if report.mode == "sweep":
        print(
            f"[sweep: {len(report.candidates)} files evaluated, "
            f"{report.cached_evaluations} from cache]",
            file=sys.stderr,
        )

    if report.answer is not None:  # --synthesize: composed prose answer
        print(report.answer.text)
        print()
        print(report.answer.sources)
        if report.answer.uncited_claims:
            print("\nclaims with unresolved citations:", file=sys.stderr)
            for claim in report.answer.uncited_claims:
                print(f"  {claim}", file=sys.stderr)
    else:  # default: display the grounded hits directly
        print(render_hits(args.question, report.findings))

    if args.show_cost:
        print("\nactual usage:")
        for task, (t_in, t_out, usd) in report.actual_usage.items():
            print(f"  {task.value:<20} in={t_in} out={t_out} ${usd:.4f}")
    if report.extraction_failures:
        for rel_path, error in report.extraction_failures:
            print(f"extraction failed: {rel_path}: {error}", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def _cmd_explain(args: argparse.Namespace, config: AppConfig) -> int:
    """Prints persisted evaluation verdicts for one corpus file."""
    import json

    from corpusqa.catalog.store import CatalogStore
    from corpusqa.ingest.pipeline import index_paths

    del config
    corpus = args.directory.resolve()
    _, db_path, _ = index_paths(corpus)
    if not db_path.exists():
        print(f"error: no index at {corpus}", file=sys.stderr)
        return EXIT_USER_ERROR
    rel = args.file.resolve().relative_to(corpus).as_posix()
    with CatalogStore(db_path) as store:
        file_hash = store.known_files().get(rel)
        if file_hash is None:
            print(f"error: {rel} is not in the index", file=sys.stderr)
            return EXIT_USER_ERROR
        rows = store.evaluations_for_file(file_hash)
    if not rows:
        print(f"no persisted evaluations for {rel}")
        return EXIT_OK
    for question, at, result_json in rows:
        data = json.loads(result_json)
        verdict = "RELEVANT" if data.get("relevant") else "not relevant"
        print(f"{at}  {verdict}  conf={data.get('confidence', 0):.2f}")
        print(f"  q: {question or '(question not recorded)'}")
        if data.get("reasoning"):
            print(f"  why: {data['reasoning']}")
        for finding in data.get("findings", [])[:5]:
            print(f"  - {finding.get('claim', '')[:100]}")
        print()
    return EXIT_OK


def _cmd_estimate(args: argparse.Namespace, config: AppConfig) -> int:
    """Runs pass 1 and prints the cost projection (no pass 2)."""
    import asyncio

    try:
        candidates, estimate = asyncio.run(
            run_estimate(
                args.directory,
                args.question,
                config,
                confirm=(lambda e: True) if args.yes else _confirm,
            )
        )
    except BudgetExceededError as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    print(f"candidates ({len(candidates)}):")
    for cand in candidates:
        print(f"  {cand.file.rel_path}  -- {cand.reason}")
    print(
        f"projected: ${estimate.total_usd:.2f} "
        f"({estimate.total_input_tokens} input tokens) "
        f"per-task: {estimate.per_task_usd}"
    )
    return EXIT_OK


def _cmd_index(args: argparse.Namespace, config: AppConfig) -> int:
    """Runs the index pipeline and renders its report."""
    report = run_index(args.directory, config, force=args.force)
    print(
        f"parsed {report.parsed}, moved {report.moved}, "
        f"deleted {report.deleted}, unchanged {report.unchanged}, "
        f"duplicates {report.duplicates}, chunks {report.chunks_written}"
    )
    if report.flagged:
        print("flagged (not fully visible to queries):", file=sys.stderr)
        for rel_path, status in report.flagged:
            print(f"  {status:<20} {rel_path}", file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def _cmd_status(args: argparse.Namespace, config: AppConfig) -> int:
    """Renders the status report (read-only)."""
    report = run_status(args.directory, config)
    if report.meta is None:
        print("no index found; run: corpusqa index <dir>")
    else:
        print(
            f"indexed {report.file_count} files, {report.chunk_count} chunks; "
            f"last indexed {report.meta.get('last_indexed_at')}"
        )
    if report.drift:
        print("drift (run 'corpusqa index' to update):")
        for item in report.drift:
            extra = f" (was {item.old_rel_path})" if item.old_rel_path else ""
            print(f"  {item.drift.value:<10} {item.rel_path}{extra}")
    else:
        print("no drift.")
    for item in report.duplicates:
        print(
            f"  duplicate  {item.rel_path} (same content as {item.old_rel_path}; "
            "not indexed separately)"
        )
    if report.flagged:
        print("flagged files (queries cannot fully see these):", file=sys.stderr)
        for row in report.flagged:
            print(f"  {row.parse_status:<20} {row.rel_path}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())