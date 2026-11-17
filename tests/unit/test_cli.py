"""CLI surface tests (M1/M2 acceptance)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from corpusqa.cli.main import EXIT_OK, EXIT_USER_ERROR, main

EXAMPLE = str(Path(__file__).resolve().parents[2] / "corpusqa.example.yaml")


def test_bad_config_exits_one(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers: {}\ntasks: {}\n", encoding="utf-8")
    assert main(["status", ".", "--config", str(bad)]) == EXIT_USER_ERROR


def test_status_on_empty_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    assert main(["status", str(corpus), "--config", EXAMPLE]) == EXIT_OK


def test_query_without_index_exits_one(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.chdir(tmp_path)  # query runs against the cwd corpus
    assert main(["query", "q", "--config", EXAMPLE]) == EXIT_USER_ERROR


def test_status_is_read_only(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    main(["status", str(corpus), "--config", EXAMPLE])
    assert not (corpus / ".corpusqa").exists()


def test_query_accepts_directory_argument(tmp_path: Path) -> None:
    corpus = tmp_path / "elsewhere"
    corpus.mkdir()
    # no index there -> clean error, proving the path was used, not cwd
    assert main(["query", "q", str(corpus), "--config", EXAMPLE]) == EXIT_USER_ERROR


def test_estimate_accepts_directory_argument(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.chdir(tmp_path)  # cwd has no corpus; only the argument can be the path
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    captured: list[Path] = []

    import corpusqa.cli.main as cli

    async def fake_estimate(
        root: Path, *_: object, **__: object
    ) -> tuple[list[object], object]:
        captured.append(root)
        raise SystemExit(0)  # short-circuit; we only care about the path

    mp.setattr(cli, "run_estimate", fake_estimate)
    with pytest.raises(SystemExit):
        cli.main(["estimate", "q", str(elsewhere), "--config", EXAMPLE])
    assert captured == [elsewhere]


def test_logging_vv_captures_prompts_in_file_only(tmp_path: Path) -> None:
    import logging

    from corpusqa.logging_setup import configure

    path = configure(2, tmp_path / "logs")
    assert path is not None
    logging.getLogger("corpusqa.llm").debug("PROMPT=%r", ["x"])
    assert "PROMPT" in path.read_text(encoding="utf-8")


def test_logging_default_is_stderr_only(tmp_path: Path) -> None:
    from corpusqa.logging_setup import configure

    assert configure(0, None) is None


def test_query_exits_partial_when_cards_failed(monkeypatch: object) -> None:
    import pytest

    import corpusqa.cli.main as cli
    from corpusqa.catalog.summarizer import CardReport
    from corpusqa.query.budget import CostEstimate
    from corpusqa.query.pipeline import QueryReport

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    async def fake_run_query(*_: object, **__: object) -> QueryReport:
        return QueryReport(
            answer=None,
            candidates=[],
            relevant_files=[],
            estimate=CostEstimate(),
            card_report=CardReport(failed=[("a.md", "card call failed")]),
        )

    mp.setattr(cli, "run_query", fake_run_query)
    mp.setattr(cli, "_warn_drift", lambda *_: None)
    assert (
        cli.main(["query", "q", ".", "--config", EXAMPLE, "--yes"]) == cli.EXIT_PARTIAL
    )


def test_estimate_exits_partial_when_cards_failed(monkeypatch: object) -> None:
    import pytest

    import corpusqa.cli.main as cli
    from corpusqa.catalog.summarizer import CardReport
    from corpusqa.query.budget import CostEstimate

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    async def fake_estimate(
        *_: object, **__: object
    ) -> tuple[list[object], CostEstimate, CardReport]:
        return [], CostEstimate(), CardReport(failed=[("a.md", "card call failed")])

    mp.setattr(cli, "run_estimate", fake_estimate)
    assert (
        cli.main(["estimate", "q", ".", "--config", EXAMPLE, "--yes"])
        == cli.EXIT_PARTIAL
    )


def test_explain_file_outside_corpus_exits_one(tmp_path: Path) -> None:
    from corpusqa.catalog.store import CatalogStore
    from corpusqa.ingest.pipeline import index_paths

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    index_dir, db_path, _ = index_paths(corpus)
    index_dir.mkdir()
    CatalogStore(db_path).close()
    outside = tmp_path / "elsewhere.pdf"
    outside.write_text("x", encoding="utf-8")
    code = main(["explain", str(outside), str(corpus), "--config", EXAMPLE])
    assert code == EXIT_USER_ERROR


def test_gui_without_npm_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert main(["gui"]) == EXIT_USER_ERROR
    assert "Install Node.js" in capsys.readouterr().err


def test_gui_passes_python_and_config_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    assert main(["gui", "--config", "my.yaml"]) == EXIT_OK
    cmd, kwargs = calls[-1]
    assert cmd == ["/fake/npm", "start"]
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["CORPUSQA_PYTHON"] == sys.executable
    assert env["CORPUSQA_CONFIG"] == str((tmp_path / "my.yaml").resolve())
    assert str(kwargs["cwd"]).endswith("gui")


def test_gui_installs_dependencies_when_node_modules_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import corpusqa.cli.main as cli

    (tmp_path / "gui").mkdir()
    monkeypatch.setattr(cli, "_repo_root", lambda: tmp_path)
    cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert main(["gui"]) == EXIT_OK
    assert cmds == [["/fake/npm", "install"], ["/fake/npm", "start"]]


def test_gui_keyboard_interrupt_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(shutil, "which", lambda _: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", interrupted)
    assert main(["gui"]) == 130


def test_query_save_writes_results_file(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    import pytest

    import corpusqa.cli.main as cli
    from corpusqa.catalog.summarizer import CardReport
    from corpusqa.ingest.pipeline import index_paths
    from corpusqa.query.budget import CostEstimate
    from corpusqa.query.pipeline import QueryReport
    from corpusqa.query.results_file import load_results

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch) and isinstance(
        capsys, pytest.CaptureFixture
    )
    index_dir, db, _ = index_paths(tmp_path.resolve())
    index_dir.mkdir()
    db.write_bytes(b"")
    seen: dict[str, object] = {}

    async def fake_run_query(*_: object, **kw: object) -> QueryReport:
        seen["results_file"] = kw["results_file"]
        return QueryReport(
            answer=None,
            candidates=[],
            relevant_files=[],
            estimate=CostEstimate(),
            card_report=CardReport(),
        )

    mp.setattr(cli, "run_query", fake_run_query)
    mp.setattr(cli, "_warn_drift", lambda *_: None)
    argv = ["query", "q?", str(tmp_path), "--config", EXAMPLE, "--yes", "--save"]
    assert cli.main(argv) == cli.EXIT_OK
    writer = seen["results_file"]
    assert writer is not None
    path = writer.path  # type: ignore[attr-defined]
    assert load_results(path).question == "q?"
    assert f"results saved to {path}" in capsys.readouterr().err


def test_summarize_groups_newest_results_and_saves(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    import pytest

    import corpusqa.cli.main as cli
    import corpusqa.query.grouping as grouping_mod
    from corpusqa.config import load_config
    from corpusqa.query.extractor import ExtractionResult, Finding
    from corpusqa.query.grouping import AnswerGroup, Grouping, collect_hits
    from corpusqa.query.results_file import ResultsWriter, load_results

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch) and isinstance(
        capsys, pytest.CaptureFixture
    )
    w = ResultsWriter.create(
        tmp_path,
        question="What was the joy?",
        definition=None,
        relevance="recall",
        mode="sweep",
        config=load_config(Path(EXAMPLE)),
    )
    w.add_file(
        "v10.docx",
        "h",
        ExtractionResult(
            file_hash="h",
            relevant=True,
            confidence=1.0,
            findings=[Finding(claim="c", quote="the kingdom", page_label="418")],
        ),
    )
    w.finish("finished")

    async def fake_group(run: object, *_: object, **__: object) -> Grouping:
        hits = collect_hits(run)  # type: ignore[arg-type]
        return Grouping(groups=[AnswerGroup("The kingdom", hits)], model="m")

    mp.setattr(grouping_mod, "group_answers", fake_group)
    argv = ["summarize", "--dir", str(tmp_path), "--config", EXAMPLE, "--yes"]
    assert cli.main(argv) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "1. The kingdom  (1 hit in 1 file)" in out
    assert 'v10.docx p. 418: "the kingdom"' in out
    saved = load_results(w.path).grouping
    assert saved is not None and saved.groups[0].answer == "The kingdom"


def test_summarize_without_results_says_how(tmp_path: Path, capsys: object) -> None:
    import pytest

    import corpusqa.cli.main as cli

    assert isinstance(capsys, pytest.CaptureFixture)
    argv = ["summarize", "--dir", str(tmp_path), "--config", EXAMPLE]
    assert cli.main(argv) == cli.EXIT_USER_ERROR
    assert "--save" in capsys.readouterr().err
