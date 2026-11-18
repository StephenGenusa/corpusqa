"""Group answers: distinct answers from a run's hits, citations kept by the program."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import BudgetExceededError, ProviderUnavailableError
from corpusqa.llm.tasks import Completion
from corpusqa.query.grouping import Grouping, collect_hits, group_answers, to_saved
from corpusqa.query.results_file import (
    SavedFile,
    SavedFinding,
    SavedRun,
    load_results,
    save_grouping,
)
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def _f(quote: str, start: int | None = None, claim: str = "c") -> SavedFinding:
    end = None if start is None else start + len(quote)
    return SavedFinding(claim=claim, quote=quote, start=start, end=end)


def _run(files: dict[str, list[SavedFinding]]) -> SavedRun:
    return SavedRun(
        format_version=2,
        question="What was the joy?",
        relevance="recall",
        mode_requested="sweep",
        corpus="/c",
        started_at="2026-11-17T00:00:00+00:00",
        tasks={},
        files=[
            SavedFile(
                rel_path=name,
                file_hash=f"h-{name}",
                status="relevant" if fs else "not_relevant",
                findings=fs,
            )
            for name, fs in files.items()
        ],
    )


def _config(window: int = 32768, output: int = 2048) -> AppConfig:
    config = load_config(EXAMPLE)
    synth = config.tasks[TaskName.SYNTHESIZE]
    config.tasks[TaskName.SYNTHESIZE] = synth.model_copy(
        update={
            "context_window": window,
            "cost_per_mtok_in": 0.0,
            "cost_per_mtok_out": 0.0,
        }
    )
    config.query = config.query.model_copy(update={"group_output_tokens": output})
    config.budget = config.budget.model_copy(update={"confirm_above_usd": 1e9})
    return config


class _Script(FakeClient):
    """Answers grouping calls from a function of the prompt."""

    def __init__(self, reply: Any) -> None:  # noqa: ANN401
        super().__init__({})
        self.reply = reply
        self.prompts: list[str] = []
        self.max_tokens: list[int | None] = []

    async def complete(
        self, task: TaskName, messages: list[dict[str, str]], **kw: Any
    ) -> Completion:
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        self.max_tokens.append(kw.get("max_tokens"))
        out = self.reply(prompt, len(self.prompts))
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, Completion):
            return out
        return Completion(
            text=json.dumps(out),
            model="fake",
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
            priced=True,
            fallback=False,
            finish_reason="stop",
        )


def _numbers(prompt: str, tag: str) -> list[int]:
    out = []
    for line in prompt.splitlines():
        if line.startswith(f"[{tag}") and "]" in line:
            out.append(int(line[len(tag) + 1 : line.index("]")]))
    return out


def _go(run: SavedRun, client: FakeClient, config: AppConfig, **kw: Any) -> Grouping:
    return asyncio.run(group_answers(run, config, client, **kw))  # type: ignore[arg-type]


def test_overlapping_and_identical_quotes_are_one_hit_for_the_model() -> None:
    run = _run(
        {
            "a": [_f("the joy set before him", 10), _f("joy set before", 14)],
            "b": [_f("The  joy set before him!"), _f("the joy set before him")],
        }
    )
    hits = collect_hits(run)
    assert [h.number for h in hits] == [1, 2, 3, 4]
    seen: list[int] = []

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        nums = _numbers(prompt, "")
        seen.extend(nums)
        return {"groups": [{"answer": "Immortality", "hits": nums}]}

    grouping = _go(run, _Script(reply), _config())
    # a's overlapping spans collapse; b's two near-identical quotes collapse.
    assert seen == [1, 3]
    assert [h.number for h in grouping.groups[0].hits] == [1, 2, 3, 4]
    assert grouping.groups[0].volumes == 2


def test_unknown_numbers_ignored_and_left_out_hits_kept_visible() -> None:
    run = _run({"a": [_f("one"), _f("two"), _f("three")]})

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            return {
                "groups": [
                    {"answer": "A", "hits": [1, 99]},
                    {"answer": "B", "hits": [1, 2]},  # 1 already placed
                ]
            }
        return {"groups": [{"answer": "A", "statements": [1]}]}

    grouping = _go(run, _Script(reply), _config())
    by_answer = {g.answer: [h.number for h in g.hits] for g in grouping.groups}
    assert by_answer == {"A": [1], "B": [2]}
    assert [h.number for h in grouping.ungrouped] == [3]


def test_merge_unions_hits_and_ranks_by_volumes_then_hits() -> None:
    run = _run(
        {
            "v1": [_f("kingdom one"), _f("resurrection")],
            "v2": [_f("kingdom two")],
            "v3": [_f("kingdom three"), _f("the bride")],
        }
    )

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            return {
                "groups": [
                    {"answer": "The kingdom", "hits": [1, 3]},
                    {"answer": "Resurrection", "hits": [2]},
                    {"answer": "Reign in the kingdom", "hits": [4]},
                    {"answer": "The bride", "hits": [5]},
                ]
            }
        statements = dict(
            line[1:].split("] ", 1)
            for line in prompt.splitlines()
            if line.startswith("[")
        )
        kingdom = [int(k) for k, v in statements.items() if "ingdom" in v]
        rest = [int(k) for k in statements if int(k) not in kingdom]
        return {
            "groups": [{"answer": "The kingdom of God", "statements": kingdom}]
            + [{"answer": statements[str(k)], "statements": [k]} for k in rest]
        }

    grouping = _go(run, _Script(reply), _config())
    assert grouping.groups[0].answer == "The kingdom of God"
    assert [h.number for h in grouping.groups[0].hits] == [1, 3, 4]
    assert grouping.groups[0].volumes == 3
    # Ties (same volumes and hits) keep document order.
    assert [g.answer for g in grouping.groups[1:]] == ["Resurrection", "The bride"]


def test_small_window_batches_hits_and_still_merges() -> None:
    run = _run({f"v{i}": [_f(f"answer text number {i} " * 20)] for i in range(12)})
    calls = {"group": 0, "merge": 0}

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            calls["group"] += 1
            nums = _numbers(prompt, "")
            return {"groups": [{"answer": f"Same {n}", "hits": [n]} for n in nums]}
        calls["merge"] += 1
        nums = _numbers(prompt, "")
        return {"groups": [{"answer": "All the same", "statements": nums}]}

    progress: list[tuple[int, int]] = []
    grouping = _go(
        run,
        _Script(reply),
        _config(window=3000, output=512),
        on_progress=lambda d, t: progress.append((d, t)),
    )
    assert calls["group"] > 1 and calls["merge"] >= 1
    assert len(grouping.groups) == 1 and len(grouping.groups[0].hits) == 12
    assert progress[-1][0] == progress[-1][1]


def test_failed_batch_is_reported_and_its_hits_kept() -> None:
    run = _run({"a": [_f("one"), _f("two")]})

    def reply(prompt: str, _n: int) -> Any:  # noqa: ANN401
        return {"not": "valid"}

    grouping = _go(run, _Script(reply), _config())
    assert not grouping.groups
    assert [h.number for h in grouping.ungrouped] == [1, 2]
    assert grouping.problems and "grouping" in grouping.problems[0]


def test_provider_unavailable_stops_grouping() -> None:
    run = _run({"a": [_f("one")]})
    client = _Script(lambda _p, _n: ProviderUnavailableError("no models loaded"))
    with pytest.raises(ProviderUnavailableError):
        _go(run, client, _config())


def test_context_comes_from_unchanged_sources_only() -> None:
    text = "before " * 10 + "THE QUOTE" + " after" * 10
    start = text.index("THE QUOTE")
    run = _run({"a": [_f("THE QUOTE", start)], "b": [_f("other", 0)]})
    config = _config()
    config.query = config.query.model_copy(update={"group_context_chars": 20})
    client = _Script(lambda p, _n: {"groups": []})

    def markdown_for(rel_path: str, file_hash: str) -> str | None:
        return text if rel_path == "a" else None

    _go(run, client, config, markdown_for=markdown_for)
    assert "before before" in client.prompts[0]
    assert "after after" in client.prompts[0]


def test_cost_gate_asks_and_decline_stops() -> None:
    run = _run({"a": [_f("one")]})
    config = _config()
    config.budget = config.budget.model_copy(update={"confirm_above_usd": 0.0})
    synth = config.tasks[TaskName.SYNTHESIZE]
    config.tasks[TaskName.SYNTHESIZE] = synth.model_copy(
        update={"cost_per_mtok_in": 1000.0, "cost_per_mtok_out": 1000.0}
    )
    asked: list[float] = []
    client = _Script(lambda _p, _n: {"groups": []})

    def confirm(estimate: Any) -> bool:  # noqa: ANN401
        asked.append(estimate.total_usd)
        return False

    with pytest.raises(BudgetExceededError):
        _go(run, client, config, confirm=confirm)
    assert asked and asked[0] > 0 and not client.prompts


def test_no_hits_needs_no_calls() -> None:
    client = _Script(lambda _p, _n: AssertionError("no call expected"))
    grouping = _go(_run({"a": []}), client, _config())
    assert grouping.groups == [] and grouping.ungrouped == []


def test_grouping_is_saved_into_the_results_file(tmp_path: Path) -> None:
    from corpusqa.query.results_file import ResultsWriter

    w = ResultsWriter.create(
        tmp_path,
        question="q",
        definition=None,
        relevance="recall",
        mode="sweep",
        config=_config(),
    )
    from corpusqa.query.extractor import ExtractionResult, Finding

    w.add_file(
        "a",
        "h-a",
        ExtractionResult(
            file_hash="h-a",
            relevant=True,
            confidence=1.0,
            findings=[Finding(claim="c", quote="one"), Finding(claim="c", quote="two")],
        ),
    )
    w.finish("finished")
    run = load_results(w.path)

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            return {"groups": [{"answer": "Both", "hits": [1, 2]}]}
        return {"groups": [{"answer": "Both", "statements": [1]}]}

    grouping = _go(run, _Script(reply), _config())
    save_grouping(w.path, to_saved(grouping))
    again = load_results(w.path)
    assert again.grouping is not None
    group = again.grouping.groups[0]
    assert group.answer == "Both" and group.volumes == 1
    assert [(r.file, r.index) for r in group.hits] == [("a", 0), ("a", 1)]
    assert again.files == run.files  # the rest of the file is untouched


def test_a_failed_batch_is_split_and_retried_before_giving_up() -> None:
    run = _run({"a": [_f("one"), _f("two"), _f("three"), _f("four")]})

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            nums = _numbers(prompt, "")
            if len(nums) > 2:  # e.g. a reasoning model ran out of output room
                return {"not": "valid"}
            return {"groups": [{"answer": "Same", "hits": nums}]}
        return {"groups": [{"answer": "Same", "statements": _numbers(prompt, "")}]}

    client = _Script(reply)
    grouping = _go(run, client, _config())
    assert [h.number for h in grouping.ungrouped] == []
    assert [h.number for h in grouping.groups[0].hits] == [1, 2, 3, 4]
    assert grouping.problems == []
    # the failed call and its repair turn, 2 halves, 1 merge
    assert len(client.prompts) == 5


def test_a_single_hit_that_still_fails_is_reported_not_grouped() -> None:
    run = _run({"a": [_f("one"), _f("two")]})

    def reply(prompt: str, _n: int) -> Any:  # noqa: ANN401
        if "<hits>" in prompt and "[2]" in prompt:
            return {"not": "valid"}
        if "<hits>" in prompt:
            return {"groups": [{"answer": "One", "hits": [1]}]}
        return {"groups": [{"answer": "One", "statements": [1]}]}

    grouping = _go(run, _Script(reply), _config())
    assert [h.number for h in grouping.ungrouped] == [2]
    assert len(grouping.problems) == 1 and "not grouped" in grouping.problems[0]


def test_hit_numbers_the_model_copies_into_an_answer_are_removed() -> None:
    run = _run({"a": [_f("one"), _f("two")]})

    def reply(prompt: str, _n: int) -> dict[str, Any]:
        if "<hits>" in prompt:
            return {
                "groups": [{"answer": "Take locks in order [1, 2]", "hits": [1, 2]}]
            }
        return {
            "groups": [{"answer": "Take locks in one order. [1]", "statements": [1]}]
        }

    grouping = _go(run, _Script(reply), _config())
    # One statement needs no merge; the first pass's answer is cleaned.
    assert grouping.groups[0].answer == "Take locks in order"


def _thought_too_long(limit: int) -> Completion:
    """A reasoning model that spent its whole allowance and wrote nothing."""
    return Completion(
        text="",
        model="fake",
        tokens_in=500,
        tokens_out=limit,
        cost_usd=0.0,
        priced=True,
        fallback=False,
        finish_reason="length",
    )


def test_a_call_that_runs_out_of_output_is_retried_with_more_room() -> None:
    run = _run({"a": [_f("one")], "b": [_f("two")]})
    config = _config(window=32768, output=2048)

    def reply(prompt: str, n: int) -> Any:  # noqa: ANN401
        if "<hits>" in prompt:
            return {
                "groups": [
                    {"answer": "One", "hits": [1]},
                    {"answer": "Two", "hits": [2]},
                ]
            }
        if client.max_tokens[-1] == 2048:
            return _thought_too_long(2048)
        return {"groups": [{"answer": "Both", "statements": [1, 2]}]}

    client = _Script(reply)
    grouping = _go(run, client, config)
    assert grouping.problems == []
    assert [g.answer for g in grouping.groups] == ["Both"]
    # The retry gets what the window leaves after the prompt, not the default.
    assert client.max_tokens[-1] is not None and client.max_tokens[-1] > 2048
    assert client.max_tokens[-1] < 32768


def test_running_out_of_output_is_explained_as_such() -> None:
    run = _run({"a": [_f("one")]})
    client = _Script(lambda _p, _n: _thought_too_long(client.max_tokens[-1] or 0))
    grouping = _go(run, client, _config())
    assert grouping.ungrouped and grouping.problems
    assert "output allowance" in grouping.problems[0]
