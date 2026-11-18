"""corpusqa command-line interface.

Composition root: the only module that constructs concrete implementations
behind the protocols. Library code raises typed errors; this layer maps them
to messages and exit codes (0 ok, 1 user/config error, 2 partial failure).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from pydantic import ValidationError

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskConfig, TaskName
from corpusqa.errors import BudgetExceededError, ConfigError, CorpusQAError
from corpusqa.ingest.pipeline import index_paths, run_index, run_status
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.logging_setup import configure as configure_logging
from corpusqa.query.budget import CostEstimate
from corpusqa.query.hits import render_hits
from corpusqa.query.pipeline import run_estimate, run_query
from corpusqa.query.results_file import (
    ResultsWriter,
    list_results,
    load_results,
    save_grouping,
)

if TYPE_CHECKING:
    from tqdm import tqdm

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_PARTIAL = 2

_DEFAULT_CONFIG = Path("corpusqa.yaml")


def _with_cli_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    """Applies CLI flags that override config, validated like the YAML."""
    section_tokens = getattr(args, "section_tokens", None)
    if section_tokens is None:
        return config
    extract = config.tasks[TaskName.EXTRACT]
    try:
        config.tasks[TaskName.EXTRACT] = TaskConfig.model_validate(
            {**extract.model_dump(), "max_input_tokens": section_tokens}
        )
    except ValidationError as exc:
        msg = exc.errors()[0]["msg"]
        raise ConfigError(f"--section-tokens {section_tokens}: {msg}") from exc
    return config


def _build_parser() -> argparse.ArgumentParser:
    """Builds the CLI argument surface (design doc section 7)."""
    parser = argparse.ArgumentParser(prog="corpusqa")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="parse and cache a directory (LLM-free)")
    p_index.add_argument("directory", type=Path)
    p_index.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_index.add_argument(
        "--force",
        nargs="*",
        type=Path,
        default=None,
        help="reconvert these files even if unchanged; with no paths, every file",
    )

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
    p_query.add_argument(
        "--section-tokens",
        type=int,
        default=None,
        help="most document tokens per extract call (overrides "
        "tasks.extract.max_input_tokens; capped at what its window allows)",
    )
    p_query.add_argument("--definition", default=None)
    p_query.add_argument("--yes", action="store_true")
    p_query.add_argument("--show-cost", action="store_true")
    p_query.add_argument(
        "--save",
        action="store_true",
        help="save the run (question, settings, every file's verdict and "
        "findings) to <directory>/.corpusqa/results/*.json as it goes",
    )
    p_query.add_argument(
        "--file",
        action="append",
        dest="files",
        metavar="PATH",
        help="query only this document (path within the directory); repeat "
        "for several. Default: every document",
    )
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

    p_sum = sub.add_parser(
        "summarize",
        help="group a saved run's hits into the distinct answers they give",
    )
    p_sum.add_argument(
        "results",
        type=Path,
        nargs="?",
        default=None,
        help="results file (default: the newest under <directory>/.corpusqa/results)",
    )
    p_sum.add_argument("--dir", dest="directory", type=Path, default=Path("."))
    p_sum.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_sum.add_argument("--yes", action="store_true")
    p_sum.add_argument(
        "--all-hits", action="store_true", help="list every hit under each answer"
    )

    p_launch = sub.add_parser(
        "gui", help="launch the desktop GUI from a source checkout (development)"
    )
    p_launch.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)

    p_gui = sub.add_parser(
        "gui-backend", help="stdio JSON-lines backend for the desktop GUI"
    )
    p_gui.add_argument("--config", type=Path, default=_DEFAULT_CONFIG)
    p_gui.add_argument(
        "--env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=".env file to load (default: the one next to the config file)",
    )

    return parser


def _repo_root() -> Path:
    """Returns the source checkout root (parent of the ``corpusqa`` package)."""
    return Path(__file__).resolve().parents[2]


def _cmd_gui(config_path: Path) -> int:
    """Starts the Electron GUI from ``<repo>/gui``, installing npm deps if needed.

    Args:
        config_path: Config file the GUI and its backend should use.

    Returns:
        Exit code of ``npm start``, or 1 when Node.js or the gui folder is missing.
    """
    npm = shutil.which("npm")
    if npm is None:
        print(
            "error: Install Node.js (https://nodejs.org) to use the GUI",
            file=sys.stderr,
        )
        return EXIT_USER_ERROR
    gui_dir = _repo_root() / "gui"
    if not gui_dir.is_dir():
        print(f"error: GUI sources not found at {gui_dir}", file=sys.stderr)
        return EXIT_USER_ERROR
    if not (gui_dir / "node_modules").is_dir():
        installed = subprocess.run([npm, "install"], cwd=gui_dir, check=False)
        if installed.returncode != 0:
            return EXIT_USER_ERROR
    env = {
        **os.environ,
        "CORPUSQA_PYTHON": sys.executable,
        "CORPUSQA_CONFIG": str(config_path.resolve()),
    }
    try:
        return subprocess.run(
            [npm, "start"], cwd=gui_dir, env=env, check=False
        ).returncode
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = _build_parser().parse_args(argv)
    if args.command == "gui":
        return _cmd_gui(args.config)
    if args.command == "gui-backend":
        from corpusqa.gui_backend.server import run_backend

        return run_backend(args.config, args.verbose, args.env_file)
    log_dir = None
    if args.command in ("index", "query", "estimate"):
        log_dir = index_paths(args.directory.resolve())[0] / "logs"
    log_path = configure_logging(args.verbose, log_dir)
    if log_path is not None and args.verbose:
        print(f"log: {log_path}", file=sys.stderr)

    try:
        config = _with_cli_overrides(load_config(args.config), args)
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
        if args.command == "summarize":
            return _cmd_summarize(args, config)
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
    if estimate.unpriced_tasks:
        print(
            f"unpriced: {', '.join(estimate.unpriced_tasks)} -- no price is "
            "known for these models, so their cost is NOT in that estimate."
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

    def __init__(self, desc: str = "  evaluating") -> None:
        self._bar: tqdm[NoReturn] | None = None
        self._desc = desc

    def __call__(self, done: int, total: int) -> None:
        if self._bar is None:
            from tqdm import tqdm

            self._bar = tqdm(
                total=total,
                desc=self._desc,
                unit="call",
                dynamic_ncols=True,
                file=sys.stderr,
            )
        if total != self._bar.total:
            self._bar.total = total  # a plan can grow (e.g. extra merge rounds)
            self._bar.refresh()
        self._bar.update(done - self._bar.n)  # report is absolute; bar wants a delta
        if done >= total:
            self.close()

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def _corpus_paths(corpus: Path, given: list[str] | None) -> list[str] | None:
    """Turns ``--file`` values into corpus-relative paths (None: all files).

    Accepts a path relative to the corpus or one on disk inside it.
    """
    if not given:
        return None
    root = corpus.resolve()
    out: list[str] = []
    for value in given:
        on_disk = Path(value).resolve()  # relative to the working directory
        if on_disk.is_file() and on_disk.is_relative_to(root):
            out.append(on_disk.relative_to(root).as_posix())
        else:
            out.append(Path(value).as_posix())  # taken as corpus-relative
    return out


def _cmd_query(args: argparse.Namespace, config: AppConfig) -> int:
    """Runs the full query pipeline and renders the cited answer."""
    import asyncio

    corpus = args.directory
    _warn_drift(corpus, config)
    progress = _Progress()
    files = _corpus_paths(corpus, args.files)
    saved = None
    if args.save and index_paths(corpus.resolve())[1].exists():
        saved = ResultsWriter.create(
            corpus,
            question=args.question,
            definition=args.definition,
            relevance=args.relevance,
            mode="sweep" if args.all_files else args.mode,
            config=config,
            selection=files,
        )
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
                results_file=saved,
                files=files,
            )
        )
    except BudgetExceededError as exc:
        progress.close()
        print(f"aborted: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    finally:
        progress.close()
        if saved is not None:
            if saved.write_error:
                print(
                    f"warning: could not save results: {saved.write_error}",
                    file=sys.stderr,
                )
            else:
                print(f"results saved to {saved.path}", file=sys.stderr)

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
    for task, calls in report.unpriced_calls.items():
        print(
            f"warning: {calls} {task.value} call(s) were served by a model with "
            "no known price; their cost is not counted.",
            file=sys.stderr,
        )
    for rel_path, error in report.extraction_failures:
        print(f"extraction failed: {rel_path}: {error}", file=sys.stderr)
    if report.extraction_failures or report.card_report.failed:
        return EXIT_PARTIAL
    return EXIT_OK


def _cmd_summarize(args: argparse.Namespace, config: AppConfig) -> int:
    """Groups a saved run's hits into distinct answers and prints them."""
    import asyncio

    from corpusqa.query.grouping import (
        group_answers,
        render_groups,
        source_text_lookup,
        to_saved,
    )

    path = args.results
    if path is None:
        saved = list_results(args.directory)
        if not saved:
            print(
                f"error: no saved results under {args.directory}; run "
                "`corpusqa query ... --save` first or name a results file",
                file=sys.stderr,
            )
            return EXIT_USER_ERROR
        path = saved[0].path
    run = load_results(path)
    print(f"grouping {path}", file=sys.stderr)
    progress = _Progress("  grouping")
    try:
        grouping = asyncio.run(
            group_answers(
                run,
                config,
                LLMTaskClient(config),
                markdown_for=source_text_lookup(Path(run.corpus)),
                on_progress=progress,
                confirm=(lambda e: True) if args.yes else _confirm,
            )
        )
    except BudgetExceededError as exc:
        print(f"aborted: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    finally:
        progress.close()
    run = save_grouping(path, to_saved(grouping))
    print(render_groups(run, None if args.all_hits else 3))
    return EXIT_PARTIAL if grouping.problems else EXIT_OK


def _cmd_explain(args: argparse.Namespace, config: AppConfig) -> int:
    """Prints persisted evaluation verdicts for one corpus file."""
    import json

    from corpusqa.catalog.store import CatalogStore

    del config
    corpus = args.directory.resolve()
    _, db_path, _ = index_paths(corpus)
    if not db_path.exists():
        print(f"error: no index at {corpus}", file=sys.stderr)
        return EXIT_USER_ERROR
    try:
        rel = args.file.resolve().relative_to(corpus).as_posix()
    except ValueError:
        print(f"error: {args.file} is not inside the corpus {corpus}", file=sys.stderr)
        return EXIT_USER_ERROR
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
        candidates, estimate, card_report = asyncio.run(
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
    per_task = ", ".join(f"{t}=${usd:.4f}" for t, usd in estimate.per_task_usd.items())
    print(
        f"projected: ${estimate.total_usd:.2f} "
        f"({estimate.total_input_tokens} input tokens) "
        f"per-task: {per_task}"
    )
    if estimate.unpriced_tasks:
        print(
            f"unpriced: {', '.join(estimate.unpriced_tasks)} -- no price is "
            "known for these models, so their cost is NOT in that projection."
        )
    for rel_path, error in card_report.failed:
        print(f"card failed: {rel_path}: {error}", file=sys.stderr)
    return EXIT_PARTIAL if card_report.failed else EXIT_OK


def _cmd_index(args: argparse.Namespace, config: AppConfig) -> int:
    """Runs the index pipeline and renders its report."""
    report = run_index(
        args.directory,
        config,
        force=args.force or [],
        force_all=args.force == [],
    )
    print(
        f"parsed {report.parsed}, moved {report.moved}, "
        f"deleted {report.deleted}, unchanged {report.unchanged}, "
        f"duplicates {report.duplicates}"
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
            f"indexed {report.file_count} files; "
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
