"""Structured (schema-validated) LLM calls with one repair retry.

Policy (design doc section 4.5): request JSON, parse, validate with Pydantic;
on failure send the validation error back once for repair; then raise
``StructuredOutputError`` carrying the raw output. No repair loops -- local
models with unreliable JSON mode get exactly one second chance.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from corpusqa.config.schema import TaskName
from corpusqa.errors import StructuredOutputError
from corpusqa.llm.tasks import Completion, LLMTaskClient
from corpusqa.prompts import structured as structured_prompt

_log = logging.getLogger("corpusqa.llm")

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Removes Markdown code fences some models wrap around JSON."""
    return _FENCE_RE.sub("", text).strip()


def schema_instruction(output_model: type[BaseModel]) -> str:
    """Builds the schema-in-prompt instruction appended to the system turn."""
    schema = json.dumps(output_model.model_json_schema(), indent=2)
    return structured_prompt.SCHEMA_INSTRUCTION.format(schema=schema)


def _complete_array_objects(body: str) -> list[str]:
    """Returns the complete ``{...}`` objects at the top level of ``body``.

    Walks brace depth while respecting JSON string/escape state, so a partial
    trailing object (left dangling by a length-truncated completion) is simply
    not collected. Stops at the array's closing ``]`` when present.
    """
    objs: list[str] = []
    depth = 0
    in_str = False
    esc = False
    start: int | None = None
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objs.append(body[start : i + 1])
                start = None
        elif ch == "]" and depth == 0:
            break
    return objs


def _salvage_truncated(raw: str) -> str | None:
    """Reconstructs valid JSON from an output truncated inside ``findings``.

    A length-capped extraction completion almost always cuts off partway
    through the findings array; the object header (scalar fields plus
    ``"findings": [``) is intact and some finding objects fully serialized.
    Keep the complete ones and close the structure. A brace-balanced object
    that is not valid JSON (a field mangled mid-object) is not complete and
    is dropped. Returns None when the shape does not match (nothing to
    salvage).
    """
    key = raw.find('"findings"')
    if key < 0:
        return None
    open_bracket = raw.find("[", key)
    if open_bracket < 0:
        return None
    objects = [
        obj for obj in _complete_array_objects(raw[open_bracket + 1 :]) if _is_json(obj)
    ]
    return raw[: open_bracket + 1] + ",".join(objects) + "]}"


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _try_validate(
    raw: str, output_model: type[T], on_salvaged: Callable[[], None] | None
) -> T | ValidationError:
    """Validates ``raw``; on failure tries a salvage when ``on_salvaged`` is set.

    ``on_salvaged`` is called when only the salvage validates, i.e. trailing
    content was dropped. Pass None to validate without salvaging.

    Returns:
        The validated model, or the validation error of ``raw`` itself when
        neither it nor its salvage validates.
    """
    try:
        return output_model.model_validate_json(raw)
    except ValidationError as error:
        if on_salvaged is None:
            return error
        salvaged = _salvage_truncated(raw)
        if salvaged is None:
            return error
        try:
            result = output_model.model_validate_json(salvaged)
        except ValidationError:
            return error
        on_salvaged()
        return result


async def complete_structured(
    client: LLMTaskClient,
    task: TaskName,
    messages: list[dict[str, str]],
    output_model: type[T],
    on_truncated: Callable[[], None] | None = None,
    on_completion: Callable[[Completion], None] | None = None,
    on_malformed: Callable[[], None] | None = None,
) -> T:
    """Runs a completion and validates the output against a Pydantic model.

    The JSON schema is embedded in the system prompt and JSON mode is
    requested via ``response_format`` (providers that do not support it
    ignore the parameter; the schema-in-prompt path still applies).

    Args:
        client: The task client to call through.
        task: Task alias to run.
        messages: Chat messages; the first system message (or a prepended
            one) receives the schema instruction.
        output_model: Pydantic model the output must validate against.
        on_truncated: Called when the output was cut off at the length limit
            and only its complete leading part was kept (a warning is logged
            either way), so the caller can treat the result as incomplete.
        on_completion: Called with every completion made (including the repair
            attempt), e.g. to learn which model served the result.
        on_malformed: Called when the output stopped normally but stayed
            invalid through the repair turn, and only its complete leading
            part was kept; the result is incomplete for a different reason
            than truncation, and more ``max_tokens`` would not help.

    Returns:
        The validated model instance.

    Raises:
        StructuredOutputError: If output fails validation after one repair
            attempt.
        LLMError: If the underlying calls fail.
    """
    messages = [dict(m) for m in messages]
    instruction = schema_instruction(output_model)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] += instruction
    else:
        messages.insert(0, {"role": "system", "content": instruction.strip()})

    completion = await client.complete(
        task, messages, response_format={"type": "json_object"}
    )
    if on_completion is not None:
        on_completion(completion)
    raw = _strip_fences(completion.text)
    if not raw:
        # An empty completion is not a JSON defect a repair turn can fix:
        # repairing it only resends a (now larger) prompt. The usual cause is
        # the prompt filling the model's context window so no completion tokens
        # remain -- local OpenAI-compatible servers tend to return empty
        # content in that case. Fail fast with a diagnosis instead of emitting
        # a misleading "Invalid JSON: EOF" after a wasted retry.
        raise StructuredOutputError(
            f"task '{task.value}' returned an empty completion "
            f"(prompt_tokens={completion.tokens_in}, "
            f"completion_tokens={completion.tokens_out}). This usually means "
            "the prompt left no room for output in the model's context window; "
            "lower the task's max_tokens or context_window, or shrink the "
            "input.",
            raw_output=completion.text,
        )

    def truncated_kept() -> None:
        _log.warning(
            "task=%s output was truncated; kept only its complete findings",
            task.value,
        )
        if on_truncated is not None:
            on_truncated()

    # Salvage is for output cut off at the length limit: its complete leading
    # part is all there is, and a repair turn would hit the same limit. Output
    # that stopped normally but is invalid is a JSON defect (e.g. a mangled
    # field mid-object); the repair turn can recover every finding, where a
    # salvage would silently drop the ones after the defect.
    first_truncated = completion.finish_reason == "length"
    validated = _try_validate(
        raw, output_model, truncated_kept if first_truncated else None
    )
    if not isinstance(validated, ValidationError):
        return validated
    first_error = validated

    _log.info(
        "task=%s structured output failed validation; one repair attempt",
        task.value,
    )
    guidance = (
        structured_prompt.TRUNCATED_REPAIR
        if first_truncated
        else structured_prompt.INVALID_REPAIR.format(errors=first_error)
    )
    repair_messages = messages + [
        {"role": "assistant", "content": completion.text},
        {"role": "user", "content": guidance},
    ]
    repaired = await client.complete(
        task, repair_messages, response_format={"type": "json_object"}
    )
    if on_completion is not None:
        on_completion(repaired)
    raw2 = _strip_fences(repaired.text)
    if not raw2:
        raise StructuredOutputError(
            f"task '{task.value}' returned an empty completion on the "
            f"repair attempt (prompt_tokens={repaired.tokens_in}, "
            f"completion_tokens={repaired.tokens_out}); likely the prompt "
            "is too large for the model's context window.",
            raw_output=repaired.text,
        )
    repaired_truncated = repaired.finish_reason == "length"
    validated2 = _try_validate(
        raw2, output_model, truncated_kept if repaired_truncated else None
    )
    if not isinstance(validated2, ValidationError):
        return validated2
    second_error = validated2

    if not (first_truncated or repaired_truncated):
        # Invalid twice without a length stop: keep the complete findings of
        # either attempt as a last resort, reported as malformed.
        def malformed_kept() -> None:
            _log.warning(
                "task=%s output was malformed JSON through the repair turn; "
                "kept only its complete findings",
                task.value,
            )
            if on_malformed is not None:
                on_malformed()

        for attempt in (raw2, raw):
            kept = _try_validate(attempt, output_model, malformed_kept)
            if not isinstance(kept, ValidationError):
                return kept
    hint = (
        " The output was truncated at the length limit; raise this "
        "task's max_tokens or reduce findings per call."
        if first_truncated or repaired_truncated
        else ""
    )
    raise StructuredOutputError(
        f"task '{task.value}' output failed validation after repair: "
        f"{second_error}.{hint}",
        raw_output=raw2,
    ) from second_error
