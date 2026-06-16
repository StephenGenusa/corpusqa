"""Zero-text classification and soffice-absent path (M2 acceptance)."""

from __future__ import annotations

from pathlib import Path

from corpusqa.ingest.converter import (
    STATUS_OK,
    STATUS_UNSUPPORTED,
    STATUS_ZERO_TEXT,
    classify_text,
    convert,
)


def test_zero_text_requires_pagination() -> None:
    assert classify_text(extracted_chars=10, page_count=12) == STATUS_ZERO_TEXT
    assert classify_text(extracted_chars=10, page_count=None) == STATUS_OK
    assert classify_text(extracted_chars=10, page_count=0) == STATUS_OK
    assert classify_text(extracted_chars=5000, page_count=12) == STATUS_OK


def test_legacy_doc_without_soffice_is_flagged(
    tmp_path: Path, monkeypatch: object
) -> None:
    import shutil

    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setattr(shutil, "which", lambda _: None)
    doc = tmp_path / "old.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0 legacy")
    result = convert(doc, "h" * 64, tmp_path / "cache", ocr="off")
    assert result.parse_status == STATUS_UNSUPPORTED
    assert result.parse_error is not None and "soffice" in result.parse_error.lower()


def test_partial_success_is_flagged_not_silent() -> None:
    from corpusqa.ingest.converter import STATUS_PARTIAL, interpret_status

    status, error = interpret_status(
        "partial_success",
        [f"preprocess: std::bad_alloc page {n}" for n in range(24, 42)],
        extracted_chars=50_000,
        page_count=41,
    )
    assert status == STATUS_PARTIAL
    assert error is not None and "bad_alloc" in error and "+8 more" in error


def test_success_status_falls_through_to_zero_text_rule() -> None:
    from corpusqa.ingest.converter import interpret_status

    assert interpret_status("success", [], 10, 12) == (STATUS_ZERO_TEXT, None)
    assert interpret_status("success", [], 9000, 12) == (STATUS_OK, None)


def test_failure_status_maps_to_parse_failed() -> None:
    from corpusqa.ingest.converter import STATUS_PARSE_FAILED, interpret_status

    status, error = interpret_status("failure", ["backend: boom"], 0, None)
    assert status == STATUS_PARSE_FAILED
    assert error is not None and "boom" in error
