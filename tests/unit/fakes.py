"""Scripted stand-in for LLMTaskClient (the single mock seam)."""

from __future__ import annotations

from typing import Any

from corpusqa.config.schema import TaskName
from corpusqa.llm.tasks import Completion


class FakeClient:
    """Returns scripted replies per task, FIFO; counts tokens by ~4 chars."""

    def __init__(self, scripts: dict[TaskName, list[str]]) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: dict[TaskName, list[list[dict[str, str]]]] = {}
        self._ledger: dict[TaskName, list[Completion]] = {}

    async def complete(
        self, task: TaskName, messages: list[dict[str, str]], **_: Any
    ) -> Completion:
        self.calls.setdefault(task, []).append(messages)
        try:
            text = self._scripts[task].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(f"no scripted reply left for {task}") from exc
        completion = Completion(
            text=text,
            model=f"fake-{task.value}",
            tokens_in=sum(len(m["content"]) // 4 for m in messages),
            tokens_out=len(text) // 4,
            cost_usd=0.001,
        )
        self._ledger.setdefault(task, []).append(completion)
        return completion

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    def count_tokens(self, task: TaskName, text: str) -> int:
        return max(1, len(text) // 4)

    def usage_by_task(self) -> dict[TaskName, tuple[int, int, float]]:
        return {
            t: (
                sum(c.tokens_in for c in cs),
                sum(c.tokens_out for c in cs),
                sum(c.cost_usd for c in cs),
            )
            for t, cs in self._ledger.items()
        }
