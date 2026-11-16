"""Run logging: stderr summaries plus a per-run log file.

Verbosity contract (design doc section 10):
- default: WARNING to stderr; INFO+ to the run log file.
- ``-v``: INFO to stderr (per-call task/model/token/cost summaries).
- ``-vv``: DEBUG everywhere -- full prompt and response capture in the log
  file (essential for prompt tuning). DEBUG is file-only; stderr stays at
  INFO even under ``-vv`` to keep the terminal readable.

Log files live under ``<corpus>/.corpusqa/logs/run-<timestamp>.log`` in a
``key=value`` style. Read-only commands (status, explain) log to stderr only.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

_FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def configure(verbosity: int, log_dir: Path | None = None) -> Path | None:
    """Configures corpusqa logging for one CLI run.

    Args:
        verbosity: 0 (default), 1 (``-v``), or 2+ (``-vv``).
        log_dir: Directory for the run log file; None for stderr-only
            (read-only commands).

    Returns:
        The log file path, or None when no file was configured.
    """
    root = logging.getLogger("corpusqa")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.WARNING if verbosity == 0 else logging.INFO)
    stderr_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stderr_handler)

    if log_dir is None:
        return None
    file_handler, log_path = _file_handler(log_dir, verbosity)
    root.addHandler(file_handler)
    return log_path


def _file_handler(log_dir: Path, verbosity: int) -> tuple[logging.Handler, Path]:
    """Creates ``<log_dir>/run-<timestamp>.log`` and a handler writing to it."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"run-{stamp}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    return file_handler, log_path


@contextlib.contextmanager
def run_log_file(log_dir: Path, verbosity: int = 0) -> Iterator[Path]:
    """Adds a per-run log file to corpusqa logging for the block's duration.

    For a long-lived process (the GUI backend) that serves several runs: each
    gets its own file, as each CLI run does.

    Args:
        log_dir: Directory for the run log file (created if missing).
        verbosity: 0/1 log INFO+ to the file; 2+ logs DEBUG.

    Yields:
        The log file path.
    """
    handler, log_path = _file_handler(log_dir, verbosity)
    root = logging.getLogger("corpusqa")
    root.addHandler(handler)
    try:
        yield log_path
    finally:
        root.removeHandler(handler)
        handler.close()
