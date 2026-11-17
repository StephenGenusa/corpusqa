"""Results file: a run's question, settings and findings, saved as it goes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from corpusqa.config import load_config
from corpusqa.query.extractor import ExtractionResult, Finding
from corpusqa.query.results_file import (
    RESULTS_FORMAT_VERSION,
    ResultsFileError,
    ResultsWriter,
    list_results,
    load_results,
)
from corpusqa.query.synthesizer import CitedAnswer

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"
_WHEN = datetime(2026, 11, 17, 14, 25, 3, tzinfo=UTC)


def _writer(root: Path, question: str = 'What was "the joy"?') -> ResultsWriter:
    return ResultsWriter.create(
        root,
        question=question,
        definition="the joy of Heb 12:2",
        relevance="recall",
        mode="auto",
        config=load_config(EXAMPLE),
        now=_WHEN,
    )


def _result(relevant: bool) -> ExtractionResult:
    findings = (
        [
            Finding(
                claim="c",
                quote="the joy",
                heading_path=["H"],
                page_label="418",
                start=10,
                end=17,
                quote_verified=True,
            )
        ]
        if relevant
        else []
    )
    return ExtractionResult(
        file_hash="h",
        relevant=relevant,
        confidence=0.9,
        reasoning="r",
        findings=findings,
    )


def test_file_is_written_at_creation_under_the_corpus(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    assert w.path.parent == tmp_path / ".corpusqa" / "results"
    assert w.path.name == "20261117T142503Z-what-was-the-joy.json"
    run = load_results(w.path)
    assert run.format_version == RESULTS_FORMAT_VERSION
    assert run.outcome == "running" and run.finished_at is None
    assert run.question == 'What was "the joy"?'
    assert run.definition == "the joy of Heb 12:2"
    assert run.corpus == str(tmp_path.resolve())
    extract = run.tasks["extract"]
    assert extract.model and extract.provider


def test_each_file_is_saved_as_it_arrives(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.add_file("a.docx", "ha", _result(True))
    assert [f.rel_path for f in load_results(w.path).files] == ["a.docx"]
    w.add_file("b.docx", "hb", _result(False))
    w.add_file("c.docx", "hc", _result(True), error="2 of 9 sections failed")
    w.add_file("d.docx", "hd", None, error="task 'extract' failed: boom")
    files = {f.rel_path: f for f in load_results(w.path).files}
    assert files["a.docx"].status == "relevant"
    assert files["a.docx"].findings[0].page_label == "418"
    assert files["a.docx"].findings[0].start == 10
    assert files["b.docx"].status == "not_relevant" and not files["b.docx"].findings
    assert files["c.docx"].status == "partial"
    assert files["c.docx"].error == "2 of 9 sections failed"
    assert files["c.docx"].findings  # what survived is kept
    assert files["d.docx"].status == "failed" and files["d.docx"].confidence is None


def test_answer_mode_and_outcome(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.set_mode("sweep")
    w.set_answer(
        CitedAnswer(
            text="It was the kingdom [1].",
            sources="[1] a.docx p. 418",
            uncited_claims=[],
        )
    )
    w.finish("cancelled", "stopped by user")
    run = load_results(w.path)
    assert run.mode == "sweep"
    assert run.answer is not None and run.answer.text.startswith("It was")
    assert run.outcome == "cancelled" and run.error == "stopped by user"
    assert run.finished_at is not None


def test_same_second_same_question_gets_a_new_file(tmp_path: Path) -> None:
    first = _writer(tmp_path)
    second = _writer(tmp_path)
    assert first.path != second.path and second.path.exists()


def test_slug_is_safe_and_bounded(tmp_path: Path) -> None:
    w = _writer(tmp_path, question="../../etc/passwd? " + "word " * 40)
    assert w.path.parent == tmp_path / ".corpusqa" / "results"
    stem = w.path.stem.split("-", 1)[1]
    assert len(stem) <= 60 and "/" not in stem and ".." not in stem


def test_write_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    w.path.parent.chmod(0o500)
    try:
        w.add_file("a.docx", "ha", _result(True))
    finally:
        w.path.parent.chmod(0o700)
    assert w.write_error is not None


def test_load_rejects_newer_format_and_garbage(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    data = json.loads(w.path.read_text(encoding="utf-8"))
    data["format_version"] = RESULTS_FORMAT_VERSION + 1
    w.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ResultsFileError, match="newer"):
        load_results(w.path)
    w.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ResultsFileError, match="not a corpusqa results file"):
        load_results(w.path)
    with pytest.raises(ResultsFileError):
        load_results(tmp_path / "missing.json")


def test_list_results_newest_first_and_skips_bad_files(tmp_path: Path) -> None:
    old = ResultsWriter.create(
        tmp_path,
        question="old",
        definition=None,
        relevance="recall",
        mode="auto",
        config=load_config(EXAMPLE),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    new = _writer(tmp_path)
    new.add_file("a.docx", "ha", _result(True))
    (old.path.parent / "junk.json").write_text("nope", encoding="utf-8")
    listed = list_results(tmp_path)
    assert [s.path for s in listed] == [new.path, old.path]
    assert listed[0].findings == 1 and listed[0].files == 1
    assert list_results(tmp_path / "nowhere") == []


def test_run_query_records_every_file_and_the_outcome(tmp_path: Path) -> None:
    import asyncio

    from corpusqa.query import pipeline as qp
    from tests.unit.test_per_volume_saving import _config, _corpus, _Recorder

    root = _corpus(tmp_path, 3)
    config = _config(1)
    w = ResultsWriter.create(
        root,
        question="q",
        definition=None,
        relevance="recall",
        mode="auto",
        config=config,
    )
    asyncio.run(
        qp.run_query(
            root,
            "q",
            config,
            client=_Recorder(),
            confirm=lambda _e: True,
            results_file=w,
        )
    )
    run = load_results(w.path)
    assert run.outcome == "finished" and run.mode == "sweep"
    assert sorted(f.rel_path for f in run.files) == ["vol0.md", "vol1.md", "vol2.md"]
    assert all(f.status == "not_relevant" and f.file_hash for f in run.files)


def test_failed_run_keeps_finished_files_and_says_why(tmp_path: Path) -> None:
    import asyncio

    from corpusqa.errors import ProviderUnavailableError
    from corpusqa.query import pipeline as qp
    from tests.unit.test_per_volume_saving import _config, _corpus, _Recorder

    root = _corpus(tmp_path, 3)
    config = _config(1)
    probe = _Recorder()
    asyncio.run(
        qp.run_query(root, "probe", config, client=probe, confirm=lambda _e: True)
    )
    per_file = len(probe.order) // 3
    w = ResultsWriter.create(
        root,
        question="q",
        definition=None,
        relevance="recall",
        mode="sweep",
        config=config,
    )
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(
            qp.run_query(
                root,
                "q",
                config,
                mode="sweep",
                client=_Recorder(fail_after=per_file + 1),
                confirm=lambda _e: True,
                results_file=w,
            )
        )
    run = load_results(w.path)
    assert run.outcome == "failed" and "gone" in (run.error or "")
    assert len(run.files) == 1 and run.files[0].status == "not_relevant"


def test_version_one_files_still_load(tmp_path: Path) -> None:
    w = _writer(tmp_path)
    data = json.loads(w.path.read_text(encoding="utf-8"))
    data["format_version"] = 1
    data.pop("grouping", None)
    w.path.write_text(json.dumps(data), encoding="utf-8")
    run = load_results(w.path)
    assert run.format_version == 1 and run.grouping is None
