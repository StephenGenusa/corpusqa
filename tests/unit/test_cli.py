"""CLI surface tests (M1/M2 acceptance)."""

from __future__ import annotations

from pathlib import Path

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
