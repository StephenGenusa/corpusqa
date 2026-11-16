"""Index pipeline behavior with a mocked converter (no Docling needed).

Covers the hash-only identity contract (duplicate copies are counted, never
indexed twice, never flip-flopped) and that ingest config (pdf_backend)
actually reaches the converter.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corpusqa.catalog.store import CatalogStore
from corpusqa.catalog.summarizer import CatalogCard
from corpusqa.config import load_config
from corpusqa.config.schema import TaskName
from corpusqa.errors import IngestError
from corpusqa.ingest import converter, pipeline
from corpusqa.ingest.pipeline import index_paths, run_index, run_status
from corpusqa.query import pipeline as qp
from tests.unit.fakes import FakeClient

EXAMPLE = Path(__file__).resolve().parents[2] / "corpusqa.example.yaml"


@pytest.fixture()
def mock_converter(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Replaces Docling with a trivial passthrough; records call kwargs."""
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(pipeline.converter, "check_converter_available", lambda: None)

    def fake_convert(
        source: Path,
        file_hash: str,
        cache_dir: Path,
        ocr: str,
        pdf_backend: str = "docling_parse",
        document_converter: object | None = None,
    ) -> converter.ConversionResult:
        calls.append({"source": source, "ocr": ocr, "pdf_backend": pdf_backend})
        cache_dir.mkdir(parents=True, exist_ok=True)
        md_path = cache_dir / f"{file_hash}.md"
        md_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        (cache_dir / f"{file_hash}.meta.json").write_text(
            json.dumps({"converter_version": converter.CONVERTER_VERSION}),
            encoding="utf-8",
        )
        (cache_dir / f"{file_hash}.pages.json").write_text(
            json.dumps([[0, "1"]]), encoding="utf-8"
        )
        return converter.ConversionResult(
            file_hash=file_hash,
            md_cache_path=md_path,
            parse_status=converter.STATUS_OK,
            parse_error=None,
            page_count=None,
        )

    monkeypatch.setattr(pipeline.converter, "convert", fake_convert)
    return calls


def test_duplicate_copies_do_not_flip_flop(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    report = run_index(root, config)
    assert report.parsed == 1

    # copy the file: the copy is counted, not indexed, and the indexed
    # rel_path never changes
    (root / "a_copy.md").write_text("# A\nalpha", encoding="utf-8")
    with CatalogStore(index_paths(root)[1]) as store:
        before = store.known_files()

    for _ in range(3):  # repeated runs must be a fixed point
        report = run_index(root, config)
        assert report.duplicates == 1
        assert report.parsed == 0 and report.moved == 0 and report.deleted == 0
        with CatalogStore(index_paths(root)[1]) as store:
            assert store.known_files() == before

    status = run_status(root, config)
    assert not status.drift  # duplicates are not actionable drift
    assert [d.rel_path for d in status.duplicates] == ["a_copy.md"]
    assert status.duplicates[0].old_rel_path == "a.md"


def test_pdf_backend_reaches_converter(
    tmp_path: Path,
    mock_converter: list[dict[str, object]],
) -> None:
    config = load_config(EXAMPLE)
    config.ingest.pdf_backend = "pypdfium"
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "doc.md").write_text("# Doc\ntext", encoding="utf-8")
    run_index(root, config)
    assert mock_converter and all(
        call["pdf_backend"] == "pypdfium" for call in mock_converter
    )


def test_bare_force_reconverts_every_file(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    (root / "b.md").write_text("# B\nbeta", encoding="utf-8")
    run_index(root, config)
    report = run_index(root, config, force_all=True)
    assert report.parsed == 2 and report.unchanged == 0


def test_force_path_not_in_the_corpus_is_an_error(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    run_index(root, config)
    with pytest.raises(IngestError, match="nope.md"):
        run_index(root, config, force=[root / "nope.md"])
    with pytest.raises(IngestError, match="outside.md"):
        run_index(root, config, force=[tmp_path / "outside.md"])


def test_cli_bare_force_means_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corpusqa.cli.main as cli

    seen: list[dict[str, object]] = []

    def fake_run_index(_root: Path, _config: object, **kwargs: object) -> object:
        seen.append(kwargs)
        raise SystemExit(0)  # short-circuit; only the arguments matter

    monkeypatch.setattr(cli, "run_index", fake_run_index)
    with pytest.raises(SystemExit):
        cli.main(["index", str(tmp_path), "--config", str(EXAMPLE), "--force"])
    assert seen == [{"force": [], "force_all": True}]


def test_forced_reparse_drops_stale_verdicts_and_artifacts(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    # A forced re-parse can change the markdown (a converter fix) while the
    # file hash stays the same, so verdicts and cards made from the old text
    # must not survive it.
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    run_index(root, config)
    _, db_path, cache_dir = index_paths(root)
    with CatalogStore(db_path) as store:
        file_hash = store.known_files()["a.md"]
        store.put_evaluation("run1", "qh", file_hash, "m", "{}")
    (cache_dir / f"{file_hash}.chunks.jsonl").write_text("{}", encoding="utf-8")

    run_index(root, config, force_all=True)
    with CatalogStore(db_path) as store:
        assert store.get_evaluation("qh", file_hash, "m") is None
    assert not (cache_dir / f"{file_hash}.chunks.jsonl").exists()


def test_stale_converter_version_reconverts(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    run_index(root, config)
    assert run_index(root, config).unchanged == 1

    cache = index_paths(root)[2]
    (meta,) = cache.glob("*.meta.json")
    meta.write_text(json.dumps({"converter_version": 1}), encoding="utf-8")
    report = run_index(root, config)
    assert report.parsed == 1 and report.unchanged == 0

    meta.write_text(json.dumps({}), encoding="utf-8")  # missing version: stale
    assert run_index(root, config).parsed == 1


def test_should_stop_ends_between_files(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha", encoding="utf-8")
    (root / "b.md").write_text("# B\nbeta", encoding="utf-8")
    calls: list[tuple[int, int]] = []
    report = run_index(
        root,
        config,
        on_progress=lambda d, t: calls.append((d, t)),
        should_stop=lambda: len(calls) >= 1,
    )
    assert report.stopped and report.parsed == 1
    assert calls == [(1, 2)]


def test_stale_reconversion_keeps_verdicts_and_cards(
    tmp_path: Path, mock_converter: list[dict[str, object]]
) -> None:
    # Cached verdicts are hours of model compute: a converter-version bump
    # replaces the cache files but keeps every DB row, and the next query
    # re-snaps the cached verdict against the new text without an LLM call.
    config = load_config(EXAMPLE)
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a.md").write_text("# A\nalpha beta", encoding="utf-8")
    run_index(root, config)
    _, db_path, cache_dir = index_paths(root)
    with CatalogStore(db_path) as store:
        file_hash = store.known_files()["a.md"]
        store.upsert_card(file_hash, CatalogCard(claims=["x"]), "m")
    verdict = json.dumps(
        {
            "file_hash": file_hash,
            "relevant": True,
            "confidence": 0.8,
            "findings": [
                {"claim": "c", "quote": "alpha beta", "heading_path": []},
            ],
        }
    )
    asyncio.run(
        qp.run_query(
            root,
            "q",
            config,
            mode="sweep",
            client=FakeClient({TaskName.EXTRACT: [verdict]}),
            confirm=lambda _e: True,
        )
    )
    (cache_dir / f"{file_hash}.pages.json").unlink()
    (cache_dir / f"{file_hash}.meta.json").write_text("{}", encoding="utf-8")

    report = run_index(root, config)
    assert report.parsed == 1
    assert (cache_dir / f"{file_hash}.pages.json").exists()
    with CatalogStore(db_path) as store:
        assert store.files_needing_cards() == []  # card survived
        con = store._conn  # noqa: SLF001 -- count rows directly
        assert con.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1

    fake = FakeClient({})
    got: dict[str, qp.ExtractionResult] = {}
    asyncio.run(
        qp.run_query(
            root,
            "q",
            config,
            mode="sweep",
            client=fake,
            on_file_result=lambda rel, r: got.__setitem__(rel, r),
            confirm=lambda _e: True,
        )
    )
    assert fake.calls == {}
    assert got["a.md"].findings[0].page_label == "1"
