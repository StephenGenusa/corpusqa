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
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from corpusqa.config.schema import TaskName
from corpusqa.errors import StructuredOutputError
from corpusqa.llm.tasks import LLMTaskClient

_log = logging.getLogger("corpusqa.llm")

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Removes Markdown code fences some models wrap around JSON."""
    return _FENCE_RE.sub("", text).strip()


def _schema_instruction(output_model: type[BaseModel]) -> str:
    """Builds the schema-in-prompt instruction appended to the system turn."""
    schema = json.dumps(output_model.model_json_schema(), indent=2)
    return (
        "\n\nRespond with a single JSON object conforming to this JSON "
        f"schema. No prose, no Markdown fences.\n{schema}"
    )


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
    Keep the complete ones and close the structure. Returns None when the shape
    does not match (nothing to salvage).
    """
    key = raw.find('"findings"')
    if key < 0:
        return None
    open_bracket = raw.find("[", key)
    if open_bracket < 0:
        return None
    objects = _complete_array_objects(raw[open_bracket + 1 :])
    return raw[: open_bracket + 1] + ",".join(objects) + "]}"


def _try_validate(raw: str, output_model: type[T]) -> T | None:
    """Validates ``raw``; on failure tries a truncation salvage, else None."""
    try:
        return output_model.model_validate_json(raw)
    except ValidationError:
        salvaged = _salvage_truncated(raw)
        if salvaged is None:
            return None
        try:
            return output_model.model_validate_json(salvaged)
        except ValidationError:
            return None


async def complete_structured(
    client: LLMTaskClient,
    task: TaskName,
    messages: list[dict[str, str]],
    output_model: type[T],
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

    Returns:
        The validated model instance.

    Raises:
        StructuredOutputError: If output fails validation after one repair
            attempt.
        LLMError: If the underlying calls fail.
    """
    messages = [dict(m) for m in messages]
    instruction = _schema_instruction(output_model)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] += instruction
    else:
        messages.insert(0, {"role": "system", "content": instruction.strip()})

    completion = await client.complete(
        task, messages, response_format={"type": "json_object"}
    )
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
    validated = _try_validate(raw, output_model)
    if validated is not None:
        return validated

    # Re-derive the validation error for the repair message (salvage failed).
    try:
        output_model.model_validate_json(raw)
    except ValidationError as first_error:
        _log.info(
            "task=%s structured output failed validation; one repair attempt",
            task.value,
        )
        truncated = completion.finish_reason == "length"
        guidance = (
            "Your previous output was cut off before the JSON finished "
            "(it exceeded the length limit). Return STRICTLY VALID, COMPLETE "
            "JSON: include only the most important findings (at most 8), keep "
            "each quote under 25 words, and make sure every brace and bracket "
            "is closed."
            if truncated
            else (
                "Your JSON failed validation with these errors:\n"
                f"{first_error}\n"
                "Return only the corrected JSON object."
            )
        )
        repair_messages = messages + [
            {"role": "assistant", "content": completion.text},
            {"role": "user", "content": guidance},
        ]
        repaired = await client.complete(
            task, repair_messages, response_format={"type": "json_object"}
        )
        raw2 = _strip_fences(repaired.text)
        if not raw2:
            raise StructuredOutputError(
                f"task '{task.value}' returned an empty completion on the "
                f"repair attempt (prompt_tokens={repaired.tokens_in}, "
                f"completion_tokens={repaired.tokens_out}); likely the prompt "
                "is too large for the model's context window.",
                raw_output=repaired.text,
            )
        validated2 = _try_validate(raw2, output_model)
        if validated2 is not None:
            return validated2
        try:
            output_model.model_validate_json(raw2)
        except ValidationError as second_error:
            hint = (
                " The output was truncated at the length limit; raise this "
                "task's max_tokens or reduce findings per call."
                if repaired.finish_reason == "length"
                or completion.finish_reason == "length"
                else ""
            )
            raise StructuredOutputError(
                f"task '{task.value}' output failed validation after repair: "
                f"{second_error}.{hint}",
                raw_output=raw2,
            ) from second_error