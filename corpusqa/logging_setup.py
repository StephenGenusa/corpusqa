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

import logging
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
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"run-{stamp}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)
    return log_path
