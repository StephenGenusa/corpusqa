"""Index pipeline behavior with a mocked converter (no Docling needed).

Covers the hash-only identity contract (duplicate copies are counted, never
indexed twice, never flip-flopped) and that ingest config (pdf_backend)
actually reaches the converter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corpusqa.catalog.store import CatalogStore
from corpusqa.config import load_config
from corpusqa.errors import IngestError
from corpusqa.ingest import converter, pipeline
from corpusqa.ingest.pipeline import index_paths, run_index, run_status

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
