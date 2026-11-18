"""Querying a chosen subset of a folder, and the wording-help chat."""

# Fixtures are imported from test_gui_backend; pytest injects them by name.
# ruff: noqa: F811

from __future__ import annotations

import json
import typing
from collections.abc import Callable
from pathlib import Path

import pytest

from corpusqa.cli.main import _corpus_paths
from corpusqa.config.schema import TaskName
from corpusqa.gui_backend.protocol import RunStartParams
from corpusqa.prompts.chat import SYSTEM, ChatContext, build_system
from corpusqa.query.results_file import load_results
from tests.unit.fakes import FakeClient
from tests.unit.test_gui_backend import (  # noqa: F401 -- pytest fixtures
    Harness,
    _extraction,
    _start,
    _write_config,
    fake_convert,
    harness,
    md_corpus,
)


@pytest.fixture()
def three_docs(md_corpus: Path) -> Path:
    (md_corpus / "rivers.md").write_text("# Rivers\n\nRivers run.\n", encoding="utf-8")
    sub = md_corpus / "vol"
    sub.mkdir()
    (sub / "hills.md").write_text("# Hills\n\nHills rise.\n", encoding="utf-8")
    (md_corpus / "notes.xyz").write_text("not a document", encoding="utf-8")
    return md_corpus


def test_files_list_shows_documents_and_which_are_converted(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(three_docs, files=["gardens.md"]))
    h.event("done")

    h.send({"id": "f", "cmd": "files.list", "params": {"folder": str(three_docs)}})
    files = h.reply("f")["result"]["files"]
    assert [(f["rel_path"], f["converted"]) for f in files] == [
        ("gardens.md", True),
        ("rivers.md", False),
        ("vol/hills.md", False),
    ]
    assert all(f["size"] > 0 for f in files)


def test_files_list_of_a_missing_folder_is_an_error(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path))
    h.send({"id": "f", "cmd": "files.list", "params": {"folder": str(tmp_path / "x")}})
    assert "not a folder" in h.reply("f")["error"]


def test_run_over_chosen_files_converts_reads_and_saves_only_those(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction(), _extraction(False)]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(three_docs, files=["vol/hills.md", "gardens.md"]))
    done = h.event("done")

    read = {r["rel_path"] for r in h.events() if r.get("event") == "file_result"}
    assert read == {"gardens.md", "vol/hills.md"}
    assert len(fake.calls[TaskName.EXTRACT]) == 2
    run = load_results(Path(done["results_path"]))
    assert run.selection == ["gardens.md", "vol/hills.md"]

    h.send({"id": "o", "cmd": "results.load", "params": {"path": done["results_path"]}})
    assert h.reply("o")["result"]["run"]["selection"] == ["gardens.md", "vol/hills.md"]

    # Unchosen files were not converted.
    h.send({"id": "f", "cmd": "files.list", "params": {"folder": str(three_docs)}})
    converted = {
        f["rel_path"] for f in h.reply("f")["result"]["files"] if f["converted"]
    }
    assert converted == {"gardens.md", "vol/hills.md"}


def test_run_over_all_files_saves_no_selection(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.EXTRACT: [_extraction(False)] * 3})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_start(three_docs))
    done = h.event("done")
    assert load_results(Path(done["results_path"])).selection is None


def test_chosen_files_none_of_which_exist_is_an_error(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path), client=FakeClient({}))
    h.send(_start(three_docs, files=["gone.md"]))
    assert "none of the chosen files" in h.event("error")["message"]


def test_empty_file_list_is_rejected(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path))
    h.send(_start(three_docs, files=[]))
    assert "invalid params" in h.reply("run")["error"]


def test_auto_mode_counts_only_the_chosen_files(
    three_docs: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    # Threshold 1: three files would route; one chosen file sweeps.
    fake = FakeClient({TaskName.EXTRACT: [_extraction()]})
    h = harness(_write_config(tmp_path, sweep_threshold=1), client=fake)
    h.send(_start(three_docs, mode="auto", files=["gardens.md"]))
    done = h.event("done")
    assert load_results(Path(done["results_path"])).mode == "sweep"
    assert TaskName.QUERY_ROUTE not in fake.calls


def test_cli_file_paths_accept_disk_or_corpus_relative(
    three_docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(three_docs / "vol")
    assert _corpus_paths(three_docs, None) is None
    assert _corpus_paths(three_docs, ["hills.md", "rivers.md", "gone.md"]) == [
        "vol/hills.md",  # found on disk from the working directory
        "rivers.md",  # taken as corpus-relative
        "gone.md",
    ]


# -- wording-help chat --------------------------------------------------------


def _chat(**over: object) -> dict[str, object]:
    params: dict[str, object] = {
        "provider": "local",
        "model": "ollama/chat-model",
        "messages": [{"role": "user", "content": "How should I ask this?"}],
        "context": {
            "question": "Does the text say A or B?",
            "definition": "",
            "documents_chosen": 3,
            "documents_total": 60,
        },
    }
    params.update(over)
    return {"id": "c", "cmd": "chat.send", "params": params}


def test_chat_replies_with_the_program_described_and_the_fields_given(
    md_corpus: Path, tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    fake = FakeClient({TaskName.SYNTHESIZE: ["<question>Better?</question>"]})
    h = harness(_write_config(tmp_path), client=fake)
    h.send(_chat())
    result = h.reply("c")["result"]
    assert result == {
        "text": "<question>Better?</question>",
        "model": "fake-synthesize",
        "truncated": False,
    }
    [messages] = fake.calls[TaskName.SYNTHESIZE]
    assert messages[0]["role"] == "system"
    assert "reads ONE section at a time" in messages[0]["content"]
    assert "Question: Does the text say A or B?" in messages[0]["content"]
    assert "Documents: 3 of 60" in messages[0]["content"]
    assert messages[1:] == [{"role": "user", "content": "How should I ask this?"}]
    # Nothing is written anywhere.
    assert not (md_corpus / ".corpusqa").exists()


def test_chat_uses_its_own_model_without_saving_it(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    from corpusqa.gui_backend import server

    config_path = _write_config(tmp_path)
    before = config_path.read_text(encoding="utf-8")
    seen: list[str] = []

    def factory(config: object) -> FakeClient:
        seen.append(config.tasks[TaskName.SYNTHESIZE].model)  # type: ignore[attr-defined]
        return FakeClient({TaskName.SYNTHESIZE: ["ok"]})

    h = Harness(lambda hh: server.Server(config_path, hh, hh, client_factory=factory))
    try:
        h.send(_chat())
        assert h.reply("c")["result"]["text"] == "ok"
    finally:
        h.close()
    assert seen == ["ollama/chat-model"]
    assert config_path.read_text(encoding="utf-8") == before


def test_chat_with_an_unknown_provider_or_no_message_is_an_error(
    tmp_path: Path, harness: Callable[..., Harness]
) -> None:
    h = harness(_write_config(tmp_path), client=FakeClient({}))
    h.send(_chat(provider="nope"))
    assert "unknown provider 'nope'" in h.reply("c")["error"]
    h.clear()
    h.send(_chat(messages=[]))
    assert "invalid params" in h.reply("c")["error"]


def test_chat_prompt_names_the_settings_the_program_has() -> None:
    hints = typing.get_type_hints(RunStartParams)
    for value in typing.get_args(hints["relevance"]) + typing.get_args(hints["mode"]):
        assert f'"{value}"' in SYSTEM, value
    # The labels the prompt quotes are the ones the GUI shows.
    renderer = Path(__file__).resolve().parents[2] / "gui" / "renderer"
    gui = "".join(
        (renderer / name).read_text(encoding="utf-8")
        for name in ("index.html", "settings.js")
    )
    for label in ("Group answers", "Write an answer from the findings", "Definition"):
        assert label in SYSTEM and label in gui, label


def test_chat_context_describes_all_or_some_documents() -> None:
    assert "Documents: all 60" in build_system(ChatContext(documents_total=60))
    assert "Documents: unknown" in build_system(ChatContext())
    text = build_system(ChatContext(question="  ", synthesize=True))
    assert "Question: (empty)" in text and "synthesize): on" in text


def test_saved_selection_survives_a_reload(tmp_path: Path) -> None:
    from corpusqa.query.results_file import ResultsWriter
    from tests.unit.test_grouping import _config

    w = ResultsWriter.create(
        tmp_path,
        question="q",
        definition=None,
        relevance="recall",
        mode="sweep",
        config=_config(),
        selection=["b.md", "a.md"],
    )
    assert json.loads(w.path.read_text())["selection"] == ["a.md", "b.md"]
    assert load_results(w.path).selection == ["a.md", "b.md"]
