"""Tests for .env loading and provider model listing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from corpusqa.config.schema import ProviderConfig
from corpusqa.gui_backend.env import load_dotenv
from corpusqa.gui_backend.models import ModelListing, list_models


def _transport(
    routes: dict[str, tuple[int, object]], seen: list[str] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        status, body = routes[url]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _list(
    name: str, provider: ProviderConfig, t: httpx.AsyncBaseTransport | None = None
) -> ModelListing:
    return asyncio.run(list_models(name, provider, transport=t))


def test_dotenv_does_not_override_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("A", "keep")
    monkeypatch.delenv("B", raising=False)
    (tmp_path / ".env").write_text('A=x\nexport B="y"\n# c\n')
    try:
        assert load_dotenv(tmp_path / ".env") == ["B"]
        assert os.environ["A"] == "keep" and os.environ["B"] == "y"
    finally:
        os.environ.pop("B", None)


def test_dotenv_quotes_blank_lines_and_missing_file(tmp_path: Path) -> None:
    keys = ("CQA_Q1", "CQA_Q2", "CQA_Q3")
    for k in keys:
        os.environ.pop(k, None)
    (tmp_path / ".env").write_text(
        "\nCQA_Q1='a b'\n  CQA_Q2 = plain \nnot a line\nCQA_Q3=\n"
    )
    try:
        assert load_dotenv(tmp_path / ".env") == list(keys)
        assert os.environ["CQA_Q1"] == "a b" and os.environ["CQA_Q2"] == "plain"
        assert os.environ["CQA_Q3"] == ""
    finally:
        for k in keys:
            os.environ.pop(k, None)
    assert load_dotenv(tmp_path / "missing.env") == []


def test_ollama_tags_prefixed() -> None:
    t = _transport(
        {"http://localhost:11434/api/tags": (200, {"models": [{"name": "gemma4:e4b"}]})}
    )
    p = ProviderConfig(api_base="http://localhost:11434")
    listing = _list("ollama_local", p, t)
    assert [m.id for m in listing.models] == ["ollama/gemma4:e4b"]
    assert listing.models[0].loaded is None and listing.error is None


def test_openrouter_models_prefixed() -> None:
    t = _transport(
        {
            "https://openrouter.ai/api/v1/models": (
                200,
                {"data": [{"id": "anthropic/claude-x", "context_length": 200000}]},
            )
        }
    )
    listing = _list("openrouter", ProviderConfig(), t)
    assert [m.id for m in listing.models] == ["openrouter/anthropic/claude-x"]
    assert listing.models[0].max_context == 200000


def test_openai_compatible_prefixed_sorted_deduped() -> None:
    t = _transport(
        {
            "http://10.0.0.5:8000/v1/models": (
                200,
                {"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]},
            )
        }
    )
    p = ProviderConfig(api_base="http://10.0.0.5:8000/v1")
    listing = _list("mybox", p, t)
    assert [m.id for m in listing.models] == ["openai/a", "openai/b"]


def test_no_kind_returns_empty_without_error() -> None:
    listing = _list("custom", ProviderConfig())
    assert listing.models == [] and listing.error is None


_LMS = [
    {
        "id": "qwen@q4",
        "type": "llm",
        "state": "loaded",
        "loaded_context_length": 16384,
        "max_context_length": 262144,
    },
    {"id": "nomic-embed", "type": "embeddings", "state": "not-loaded"},
    {"id": "vis", "type": "vlm", "state": "not-loaded", "max_context_length": 8192},
    # A speculative-decoding draft head: labelled "llm" but cannot chat.
    {"id": "g/mtp@q8_0", "type": "llm", "arch": "gemma4-assistant"},
]


def test_lm_studio_lists_chat_models_with_context() -> None:
    seen: list[str] = []
    t = _transport({"http://localhost:1234/api/v0/models": (200, {"data": _LMS})}, seen)
    p = ProviderConfig(api_base="http://localhost:1234/v1")
    listing = _list("lm_studio", p, t)
    assert seen == ["http://localhost:1234/api/v0/models"]
    assert [m.id for m in listing.models] == ["lm_studio/qwen@q4", "lm_studio/vis"]
    first, second = listing.models
    assert first.loaded and first.loaded_context == 16384
    assert first.max_context == 262144
    assert second.loaded is False and second.loaded_context is None


def test_lm_studio_falls_back_to_openai_models_on_404() -> None:
    t = _transport(
        {
            "http://localhost:1234/api/v0/models": (404, {}),
            "http://localhost:1234/v1/models": (200, {"data": [{"id": "qwen@q4"}]}),
        }
    )
    p = ProviderConfig(api_base="http://localhost:1234/v1")
    listing = _list("lm_studio", p, t)
    assert [m.id for m in listing.models] == ["lm_studio/qwen@q4"]
    assert listing.models[0].loaded is None and listing.models[0].max_context is None


def test_list_models_unreachable_returns_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    handler: Callable[[httpx.Request], httpx.Response] = boom
    p = ProviderConfig(api_base="http://localhost:11434")
    listing = _list("ollama_local", p, httpx.MockTransport(handler))
    assert listing.models == []
    assert listing.error is not None and "unreachable" in listing.error


def test_dotenv_utf8_bom_does_not_corrupt_first_key(tmp_path: Path) -> None:
    os.environ.pop("CQA_BOM1", None)
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfCQA_BOM1=secret\n")
    try:
        assert load_dotenv(tmp_path / ".env") == ["CQA_BOM1"]
        assert os.environ["CQA_BOM1"] == "secret"
    finally:
        os.environ.pop("CQA_BOM1", None)


def _auth_recorder(url: str, seen: list[str | None]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == url
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("name", "base", "url", "prefix"),
    [
        (
            "mybox",
            "http://10.0.0.5:8000/v1",
            "http://10.0.0.5:8000/v1/models",
            "openai",
        ),
        ("openrouter", None, "https://openrouter.ai/api/v1/models", "openrouter"),
    ],
)
def test_models_list_sends_the_provider_key(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    base: str | None,
    url: str,
    prefix: str,
) -> None:
    monkeypatch.setenv("CQA_LIST_KEY", "sk-test")
    seen: list[str | None] = []
    p = ProviderConfig(api_base=base, api_key_env="CQA_LIST_KEY")
    listing = _list(name, p, _auth_recorder(url, seen))
    assert [m.id for m in listing.models] == [f"{prefix}/m"]
    assert seen == ["Bearer sk-test"]


def test_models_list_without_a_key_sends_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CQA_LIST_KEY", raising=False)
    seen: list[str | None] = []
    url = "http://10.0.0.5:8000/v1/models"
    for env in (None, "CQA_LIST_KEY"):  # no key configured; key var unset
        p = ProviderConfig(api_base="http://10.0.0.5:8000/v1", api_key_env=env)
        _list("mybox", p, _auth_recorder(url, seen))
    assert seen == [None, None]
