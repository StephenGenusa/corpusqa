"""Group answers: the distinct answers among a run's hits, as a ranked list.

Three steps, each sized to the ``synthesize`` task's context window:

1. **Collapse duplicates (no model).** Within a file, hits whose spans
   overlap or whose quotes match after normalization are one *unit*; the
   model sees one representative per unit.
2. **Group (map).** Batches of units -- claim, quote and optionally the text
   around it -- go to the model, which returns ``{answer, hits}`` groups.
   Every number is checked: unknown numbers are ignored, a unit placed twice
   keeps its first group, and units the model leaves out are kept in a
   visible "not grouped" list.
3. **Merge (reduce).** Only the short answer statements go to the model,
   which says which statements mean the same thing; the program unions their
   hit lists itself. Repeats in batches while the statements overflow.

Hit numbers never pass through model-written prose, so a citation cannot be
dropped or invented by paraphrase. A failed call is reported in
``problems`` and its hits stay visible; only a provider error that every
call would hit (``ProviderUnavailableError``) stops the whole job.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from corpusqa.config.schema import AppConfig, TaskName
from corpusqa.errors import CorpusQAError, ProviderUnavailableError
from corpusqa.ingest.pipeline import index_paths
from corpusqa.llm.pricing import task_rate
from corpusqa.llm.structured import complete_structured
from corpusqa.llm.tasks import LLMTaskClient
from corpusqa.prompts import group as group_prompt
from corpusqa.query.budget import CostEstimate
from corpusqa.query.pipeline import _check_budget_gate
from corpusqa.query.results_file import (
    SavedFinding,
    SavedGroup,
    SavedGrouping,
    SavedHitRef,
    SavedRun,
)

# System prompt + JSON schema + question, per call (conservative).
_CALL_OVERHEAD_TOKENS = 1_500
# Never pack less than this much input, however small the window.
_MIN_INPUT_TOKENS = 500
# The reply lists every number in the batch; keep it well under the reserve.
_MAX_UNITS_PER_CALL = 80
_MAX_STATEMENTS_PER_CALL = 200
_MAX_MERGE_ROUNDS = 4


@dataclass(frozen=True)
class Hit:
    """One finding of a saved run, numbered in document order (1-based)."""

    number: int
    rel_path: str
    file_hash: str
    index: int
    finding: SavedFinding


@dataclass
class AnswerGroup:
    """A distinct answer and every hit that gives it."""

    answer: str
    hits: list[Hit]

    @property
    def volumes(self) -> int:
        """Distinct files among the hits."""
        return len({h.rel_path for h in self.hits})


@dataclass
class Grouping:
    """Result of ``group_answers``."""

    groups: list[AnswerGroup] = field(default_factory=list)
    ungrouped: list[Hit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    model: str = ""


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _GroupOut(_Reply):
    answer: str
    hits: list[int]


class _GroupReply(_Reply):
    groups: list[_GroupOut]


class _MergeOut(_Reply):
    answer: str
    statements: list[int]


class _MergeReply(_Reply):
    groups: list[_MergeOut]


def collect_hits(run: SavedRun) -> list[Hit]:
    """Numbers every finding of the run's relevant and partial files."""
    hits: list[Hit] = []
    for f in run.files:
        if f.status not in ("relevant", "partial"):
            continue
        for i, finding in enumerate(f.findings):
            hits.append(Hit(len(hits) + 1, f.rel_path, f.file_hash, i, finding))
    return hits


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _units(hits: list[Hit]) -> list[list[Hit]]:
    """Collapses same-file duplicates; each unit's first hit represents it."""
    units: list[list[Hit]] = []
    for hit in hits:
        f = hit.finding
        for unit in units:
            rep = unit[0]
            if rep.rel_path != hit.rel_path:
                continue
            same_text = _norm(rep.finding.quote) == _norm(f.quote)
            overlap = (
                f.start is not None
                and f.end is not None
                and any(
                    u.finding.start is not None
                    and u.finding.end is not None
                    and f.start < u.finding.end
                    and u.finding.start < f.end
                    for u in unit
                )
            )
            if same_text or overlap:
                unit.append(hit)
                break
        else:
            units.append([hit])
    return units


def _hit_block(hit: Hit, context: str | None) -> str:
    f = hit.finding
    where = hit.rel_path + (f" · p. {f.page_label}" if f.page_label else "")
    lines = [
        f"[{hit.number}] {where}",
        f"    claim: {f.claim}",
        f"    quote: {f.quote}",
    ]
    if context:
        lines.append(f"    context: {context}")
    return "\n".join(lines)


def _context(
    hit: Hit,
    chars: int,
    markdown_for: Callable[[str, str], str | None] | None,
    cache: dict[str, str | None],
) -> str | None:
    f = hit.finding
    if not chars or markdown_for is None or f.start is None or f.end is None:
        return None
    if hit.rel_path not in cache:
        cache[hit.rel_path] = markdown_for(hit.rel_path, hit.file_hash)
    text = cache[hit.rel_path]
    if text is None:
        return None
    before = text[max(0, f.start - chars) : f.start]
    after = text[f.end : f.end + chars]
    return " ".join(f"{before}«{text[f.start : f.end]}»{after}".split())


def _pack(
    blocks: list[str], budget: int, cap: int, client: LLMTaskClient
) -> list[list[int]]:
    """Greedy batches of block indices within ``budget`` tokens and ``cap`` items."""
    batches: list[list[int]] = []
    current: list[int] = []
    used = 0
    for i, block in enumerate(blocks):
        tokens = client.count_tokens(TaskName.SYNTHESIZE, block)
        if current and (used + tokens > budget or len(current) >= cap):
            batches.append(current)
            current, used = [], 0
        current.append(i)
        used += tokens
    if current:
        batches.append(current)
    return batches


def _estimate(input_tokens: int, calls: int, config: AppConfig) -> CostEstimate:
    task = config.tasks[TaskName.SYNTHESIZE]
    rate = task_rate(task, config.providers[task.provider])
    out_tokens = calls * config.query.group_output_tokens
    in_tokens = input_tokens + calls * _CALL_OVERHEAD_TOKENS
    usd = 0.0 if rate is None else (in_tokens * rate[0] + out_tokens * rate[1]) / 1e6
    return CostEstimate(
        per_task_usd={TaskName.SYNTHESIZE.value: usd},
        total_usd=usd,
        total_input_tokens=in_tokens,
        unpriced_tasks=() if rate is not None else (TaskName.SYNTHESIZE.value,),
    )


async def group_answers(
    run: SavedRun,
    config: AppConfig,
    client: LLMTaskClient,
    *,
    markdown_for: Callable[[str, str], str | None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    confirm: Callable[[CostEstimate], bool] | None = None,
) -> Grouping:
    """Finds the distinct answers among a saved run's hits.

    Args:
        run: The saved run (its question and hits).
        config: Configuration; the ``synthesize`` task's model and window are
            used, with ``query.group_context_chars`` and
            ``query.group_output_tokens``.
        client: Task client.
        markdown_for: Returns a file's converted text when its hash still
            matches (else None); used for the text around each hit.
        on_progress: Called with (calls_done, calls_planned).
        confirm: Cost-gate callback, as for queries.

    Returns:
        Groups ranked by distinct files then hits (ties in document order),
        hits the model did not place, and any failed calls.

    Raises:
        BudgetExceededError: The projected cost needs confirmation that was
            not given.
        ProviderUnavailableError: The provider cannot serve any call.
    """
    task_cfg = config.tasks[TaskName.SYNTHESIZE]
    result = Grouping(model=task_cfg.model)
    hits = collect_hits(run)
    if not hits:
        return result
    units = _units(hits)
    by_number = {u[0].number: u for u in units}
    chars = config.query.group_context_chars
    cache: dict[str, str | None] = {}
    blocks = [
        _hit_block(u[0], _context(u[0], chars, markdown_for, cache)) for u in units
    ]
    budget = max(
        task_cfg.context_window
        - config.query.group_output_tokens
        - _CALL_OVERHEAD_TOKENS,
        _MIN_INPUT_TOKENS,
    )
    batches = _pack(blocks, budget, _MAX_UNITS_PER_CALL, client)
    input_tokens = sum(client.count_tokens(TaskName.SYNTHESIZE, b) for b in blocks)
    # One merge call per ~budget of statements is a fair guess up front.
    planned = len(batches) + (1 if len(batches) > 0 else 0)
    _check_budget_gate(
        _estimate(input_tokens, planned, config), config, confirm, "grouping"
    )

    done = 0

    def tick() -> None:
        nonlocal done, planned
        done += 1
        planned = max(planned, done)
        if on_progress is not None:
            on_progress(done, planned)

    if on_progress is not None:
        on_progress(0, planned)

    # Pass 1: group each batch of units.
    statements: list[tuple[str, list[int]]] = []  # (answer, unit numbers)
    placed: set[int] = set()
    for b, batch in enumerate(batches, start=1):
        numbers = {units[i][0].number for i in batch}
        messages = [
            {"role": "system", "content": group_prompt.GROUP_SYSTEM},
            {
                "role": "user",
                "content": group_prompt.GROUP_TEMPLATE.format(
                    question=run.question, hits="\n".join(blocks[i] for i in batch)
                ),
            },
        ]
        try:
            reply = await complete_structured(
                client,
                TaskName.SYNTHESIZE,
                messages,
                _GroupReply,
                max_tokens=config.query.group_output_tokens,
            )
        except ProviderUnavailableError:
            raise
        except CorpusQAError as exc:
            result.problems.append(
                f"grouping batch {b} of {len(batches)} failed ({exc}); "
                "its hits are listed as not grouped"
            )
            tick()
            continue
        for g in reply.groups:
            mine = [
                n for n in dict.fromkeys(g.hits) if n in numbers and n not in placed
            ]
            if mine and g.answer.strip():
                placed.update(mine)
                statements.append((g.answer.strip(), mine))
        tick()

    # Pass 2: merge statements that mean the same thing.
    statements = await _merge(
        run.question, statements, budget, config, client, result, tick
    )

    for answer, unit_numbers in statements:
        group_hits = sorted(
            (h for n in unit_numbers for h in by_number[n]), key=lambda h: h.number
        )
        result.groups.append(AnswerGroup(answer, group_hits))
    result.groups.sort(key=lambda g: (-g.volumes, -len(g.hits), g.hits[0].number))
    result.ungrouped = sorted(
        (h for n, unit in by_number.items() if n not in placed for h in unit),
        key=lambda h: h.number,
    )
    if on_progress is not None:
        on_progress(done, done)
    return result


async def _merge(
    question: str,
    statements: list[tuple[str, list[int]]],
    budget: int,
    config: AppConfig,
    client: LLMTaskClient,
    result: Grouping,
    tick: Callable[[], None],
) -> list[tuple[str, list[int]]]:
    """Repeatedly merges same-meaning statements; hit lists are unioned here."""
    for _round in range(_MAX_MERGE_ROUNDS):
        if len(statements) < 2:
            break
        # Similar wording tends to sort together, so later rounds' batches
        # meet the statements earlier batches could not see side by side.
        statements = sorted(statements, key=lambda s: s[0].lower())
        lines = [f"[{i + 1}] {answer}" for i, (answer, _) in enumerate(statements)]
        batches = _pack(lines, budget, _MAX_STATEMENTS_PER_CALL, client)
        merged: list[tuple[str, list[int]]] = []
        for batch in batches:
            if len(batch) == 1:
                merged.append(statements[batch[0]])
                continue
            local = {i + 1 for i in batch}
            messages = [
                {"role": "system", "content": group_prompt.MERGE_SYSTEM},
                {
                    "role": "user",
                    "content": group_prompt.MERGE_TEMPLATE.format(
                        question=question, statements="\n".join(lines[i] for i in batch)
                    ),
                },
            ]
            try:
                reply = await complete_structured(
                    client,
                    TaskName.SYNTHESIZE,
                    messages,
                    _MergeReply,
                    max_tokens=config.query.group_output_tokens,
                )
            except ProviderUnavailableError:
                raise
            except CorpusQAError as exc:
                result.problems.append(
                    f"merging similar answers failed ({exc}); those answers are "
                    "listed separately"
                )
                merged.extend(statements[i] for i in batch)
                tick()
                continue
            used: set[int] = set()
            for g in reply.groups:
                members = [
                    s
                    for s in dict.fromkeys(g.statements)
                    if s in local and s not in used
                ]
                if not members:
                    continue
                used.update(members)
                answer = g.answer.strip() or statements[members[0] - 1][0]
                numbers = [n for s in members for n in statements[s - 1][1]]
                merged.append((answer, numbers))
            merged.extend(statements[i - 1] for i in sorted(local - used))
            tick()
        if len(merged) == len(statements) or len(batches) == 1:
            return merged
        statements = merged
    return statements


def to_saved(grouping: Grouping) -> SavedGrouping:
    """Converts a grouping to its results-file form."""

    def ref(h: Hit) -> SavedHitRef:
        return SavedHitRef(file=h.rel_path, index=h.index)

    return SavedGrouping(
        model=grouping.model,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        groups=[
            SavedGroup(
                answer=g.answer, hits=[ref(h) for h in g.hits], volumes=g.volumes
            )
            for g in grouping.groups
        ],
        ungrouped=[ref(h) for h in grouping.ungrouped],
        problems=list(grouping.problems),
    )


def source_text_lookup(corpus_root: Path) -> Callable[[str, str], str | None]:
    """Returns ``markdown_for``: a file's converted text if its hash still matches.

    A file reconverted with different content (or no longer indexed) gives
    None, so no stale surrounding text is shown with its hits.
    """
    from corpusqa.catalog.store import CatalogStore

    _, db_path, cache_dir = index_paths(corpus_root)
    known: dict[str, str] = {}
    if db_path.exists():
        with CatalogStore(db_path) as store:
            known = store.known_files()

    def markdown_for(rel_path: str, file_hash: str) -> str | None:
        if known.get(rel_path) != file_hash:
            return None
        try:
            return (cache_dir / f"{file_hash}.md").read_text(encoding="utf-8")
        except OSError:
            return None

    return markdown_for


def render_groups(run: SavedRun, quotes_per_group: int | None = 3) -> str:
    """Plain-text listing of a run's grouped answers, for the terminal.

    Args:
        run: A saved run with ``grouping`` set.
        quotes_per_group: Hits shown under each answer; None shows all.
    """
    grouping = run.grouping
    if grouping is None:
        return "(not grouped yet)"
    findings = {f.rel_path: f.findings for f in run.files}

    def line(ref: SavedHitRef) -> str:
        f = findings[ref.file][ref.index]
        where = ref.file + (f" p. {f.page_label}" if f.page_label else "")
        flag = "" if f.verified else " (unverified)"
        return f'     - {where}: "{html.unescape(f.quote)}"{flag}'

    out = [f"Distinct answers to: {run.question}", ""]
    for i, g in enumerate(grouping.groups, start=1):
        n = len(g.hits)
        out.append(
            f"{i:>3}. {html.unescape(g.answer)}  ({n} hit{'s' if n != 1 else ''} in "
            f"{g.volumes} file{'s' if g.volumes != 1 else ''})"
        )
        shown = g.hits if quotes_per_group is None else g.hits[:quotes_per_group]
        out.extend(line(r) for r in shown)
        if len(shown) < n:
            out.append(f"       … and {n - len(shown)} more")
    if grouping.ungrouped:
        out += ["", f"Not grouped ({len(grouping.ungrouped)}):"]
        out.extend(line(r) for r in grouping.ungrouped)
    for problem in grouping.problems:
        out.append(f"warning: {problem}")
    return "\n".join(out)
