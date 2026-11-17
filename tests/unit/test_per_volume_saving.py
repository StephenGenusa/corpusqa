"""Volumes are read a few at a time, so each finishes (and is cached) early.

Starting every volume at once made them share the provider's slots round-
robin: all advanced in lockstep and none finished, or was cached, until the
very end of a many-hour run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import ProviderUnavailableError
from corpusqa.ingest.pipeline import run_index
from corpusqa.llm.tasks import Completion
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
_VERDICT = json.dumps(
    {"relevant": False, "confidence": 0.5, "reasoning": "r", "findings": []}
)


def _config(concurrency: int) -> AppConfig:
    config = load_config(EXAMPLE)
    extract = config.tasks[TaskName.EXTRACT]
    config.providers[extract.provider] = config.providers[extract.provider].model_copy(
        update={"max_concurrency": concurrency}
    )
    # Small sections, so every volume needs several calls.
    config.tasks[TaskName.EXTRACT] = extract.model_copy(
        update={"max_input_tokens": 120}
    )
    return config


def _corpus(root: Path, files: int) -> Path:
    para = "Words about the subject at hand, repeated for length. " * 12
    for i in range(files):
        body = "\n\n".join(f"## Part {j}\n\n{para}" for j in range(5))
        (root / f"vol{i}.md").write_text(f"# Volume {i}\n\n{body}", encoding="utf-8")
    run_index(root, load_config(EXAMPLE))
    return root


class _Recorder(FakeClient):
    """Records which file each call reads and how many files are in flight."""

    def __init__(self, fail_after: int | None = None) -> None:
        super().__init__({})
        self.order: list[str] = []
        self.fail_after = fail_after

    async def complete(
        self, task: TaskName, messages: list[dict[str, str]], **_: Any
    ) -> Completion:
        content = messages[-1]["content"]
        name = content.split("File: ", 1)[1].split(" ", 1)[0]
        if self.fail_after is not None and len(self.order) >= self.fail_after:
            raise ProviderUnavailableError("gone")
        self.order.append(name)
        await asyncio.sleep(0.001)
        return Completion(
            text=_VERDICT,
            model="fake",
            tokens_in=100,
            tokens_out=10,
            cost_usd=0.0,
            priced=True,
            fallback=False,
            finish_reason="stop",
        )


def _run(root: Path, config: AppConfig, client: FakeClient, **kw: Any) -> Any:
    return asyncio.run(
        qp.run_query(
            root,
            "q",
            config,
            mode="sweep",
            client=client,
            confirm=lambda _e: True,
            **kw,
        )
    )


def _max_in_flight(order: list[str]) -> int:
    """Most files started but not yet finished at any point in ``order``."""
    last = {name: i for i, name in enumerate(order)}
    started: set[str] = set()
    worst = 0
    for i, name in enumerate(order):
        started.add(name)
        worst = max(worst, len(started))
        if last[name] == i:
            started.discard(name)
    return worst


def test_one_slot_reads_each_volume_to_the_end_first(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 4)
    rec = _Recorder()
    _run(root, _config(1), rec)
    assert len(rec.order) > 8  # several sections per volume
    # Each volume's calls are contiguous.
    runs = [n for i, n in enumerate(rec.order) if i == 0 or rec.order[i - 1] != n]
    assert sorted(runs) == sorted(set(rec.order))


def test_never_more_volumes_in_flight_than_slots(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 6)
    rec = _Recorder()
    _run(root, _config(2), rec)
    assert _max_in_flight(rec.order) <= 2


def test_stopped_run_keeps_finished_volumes_cached(tmp_path: Path) -> None:
    root = _corpus(tmp_path, 4)
    config = _config(1)
    probe = _Recorder()
    _run(root, config, probe)
    per_file = len(probe.order) // 4

    # A fresh question; the provider dies partway through the third volume.
    stopped = _Recorder(fail_after=2 * per_file + 1)
    with pytest.raises(ProviderUnavailableError):
        _run(root, config, stopped, definition="new")

    # The rerun reads only the two volumes that had not finished.
    rerun = _Recorder()
    _run(root, config, rerun, definition="new")
    assert len(set(rerun.order)) == 2
    assert not set(rerun.order) & set(stopped.order[: 2 * per_file])
