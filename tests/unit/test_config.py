"""Config schema and loader tests (M1 acceptance)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.config.schema import ProviderConfig, TaskName, provider_kind
from corpusqa.errors import ConfigError
from corpusqa.llm.tasks import LLMTaskClient

VALID = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


def test_example_config_loads() -> None:
    """The shipped example must always validate."""
    config = load_config(VALID)
    assert config.providers["ollama_local"].max_concurrency == 2
    assert config.tasks  # all six present, enforced by validator


def test_missing_task_is_rejected(tmp_path: Path) -> None:
    """Omitting a task category fails at load, not mid-run."""
    text = VALID.read_text(encoding="utf-8").replace("  extract:", "  _extract:")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="extract"):
        load_config(bad)


def test_unknown_provider_reference_is_rejected(tmp_path: Path) -> None:
    """A task pointing at an undefined provider is a hard error."""
    text = VALID.read_text(encoding="utf-8").replace(
        "    provider: ollama_local", "    provider: nope", 1
    )
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="nope"):
        load_config(bad)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """Typos in keys fail fast instead of being silently ignored."""
    text = VALID.read_text(encoding="utf-8") + "\nbudgett:\n  x: 1\n"
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    """A nonexistent path raises ConfigError, not OSError."""
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("name", "base", "kind"),
    [
        ("ollama_local", "http://localhost:11434", "ollama"),
        ("lm_studio", "http://localhost:1234/v1", "lm_studio"),
        ("lmstudio", None, "lm_studio"),
        ("openrouter", None, "openrouter"),
        ("x", "https://openrouter.ai/api/v1", "openrouter"),
        ("mybox", "http://10.0.0.5:8000/v1", "openai_compatible"),
    ],
)
def test_provider_kind_inferred(name: str, base: str | None, kind: str) -> None:
    assert provider_kind(name, ProviderConfig(api_base=base)) == kind


def test_provider_kind_none_without_hints() -> None:
    assert provider_kind("custom", ProviderConfig()) is None


def test_explicit_kind_wins() -> None:
    cfg = ProviderConfig(kind="lm_studio", api_base="http://h:9/v1")
    assert provider_kind("x", cfg) == "lm_studio"


def test_provider_still_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        ProviderConfig.model_validate({"kind": "ollama", "bogus": 1})


def test_lm_studio_call_kwargs_have_placeholder_key_and_no_num_ctx() -> None:
    cfg = load_config(VALID)
    cfg.providers["lm_studio"] = ProviderConfig(
        kind="lm_studio", api_base="http://localhost:1234/v1"
    )
    task = cfg.tasks[TaskName.EXTRACT].model_copy(
        update={"model": "lm_studio/qwen@q4", "provider": "lm_studio"}
    )
    cfg.tasks[TaskName.EXTRACT] = task
    kw = LLMTaskClient(cfg)._call_kwargs(task)
    assert kw["api_key"] == "not-needed"
    assert "num_ctx" not in kw
    assert kw["api_base"] == "http://localhost:1234/v1"


@pytest.mark.parametrize(
    ("kind", "model"),
    [
        ("openai_compatible", "openai/my-model"),
        ("lm_studio", "openai/qwen"),
        (None, "openai/my-model"),  # inferred openai_compatible from api_base
    ],
)
def test_keyless_openai_compatible_provider_never_sends_env_openai_key(
    monkeypatch: pytest.MonkeyPatch, kind: str | None, model: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    cfg = load_config(VALID)
    cfg.providers["mybox"] = ProviderConfig(
        kind=kind,  # type: ignore[arg-type]
        api_base="http://10.0.0.5:8000/v1",
    )
    task = cfg.tasks[TaskName.EXTRACT].model_copy(
        update={"model": model, "provider": "mybox"}
    )
    cfg.tasks[TaskName.EXTRACT] = task
    kw = LLMTaskClient(cfg)._call_kwargs(task)
    assert kw["api_key"] == "not-needed"


def test_lm_studio_kind_drops_json_object_without_model_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import Any

    from corpusqa.llm import tasks as llm_tasks
    from tests.unit.test_pricing import _fake_response

    cfg = load_config(VALID)
    cfg.providers["box"] = ProviderConfig(
        kind="lm_studio", api_base="http://localhost:1234/v1"
    )
    cfg.tasks[TaskName.EXTRACT] = cfg.tasks[TaskName.EXTRACT].model_copy(
        update={"model": "openai/qwen", "provider": "box"}
    )
    sent: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:  # noqa: ANN401
        sent.update(kwargs)
        return _fake_response(kwargs["model"])

    monkeypatch.setattr(llm_tasks.litellm, "acompletion", fake_acompletion)
    asyncio.run(
        LLMTaskClient(cfg).complete(
            TaskName.EXTRACT,
            [{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
        )
    )
    assert "response_format" not in sent


def test_lm_studio_complete_drops_json_object_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from typing import Any

    from corpusqa.llm import tasks as llm_tasks
    from tests.unit.test_pricing import _fake_response

    cfg = load_config(VALID)
    cfg.providers["lm_studio"] = ProviderConfig(
        kind="lm_studio", api_base="http://localhost:1234/v1"
    )
    cfg.tasks[TaskName.EXTRACT] = cfg.tasks[TaskName.EXTRACT].model_copy(
        update={"model": "lm_studio/qwen@q4", "provider": "lm_studio"}
    )
    sent: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:  # noqa: ANN401
        sent.update(kwargs)
        return _fake_response(kwargs["model"])

    monkeypatch.setattr(llm_tasks.litellm, "acompletion", fake_acompletion)
    asyncio.run(
        LLMTaskClient(cfg).complete(
            TaskName.EXTRACT,
            [{"role": "user", "content": "x"}],
            response_format={"type": "json_object"},
        )
    )
    assert "response_format" not in sent
