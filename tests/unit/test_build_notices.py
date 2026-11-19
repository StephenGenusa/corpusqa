"""scripts/build_notices.py: the licence notices shipped with the app."""

from __future__ import annotations

import email.message
import importlib.util
import json
import sys
from importlib import metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_notices.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_notices", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass looks its module up here
    spec.loader.exec_module(module)
    return module


bn = _load()


def _dist(name: str, license_expr: str | None = None) -> SimpleNamespace:
    """A distribution with metadata and no files (no licence file shipped)."""
    meta = email.message.Message()
    meta["Name"] = name
    if license_expr:
        meta["License-Expression"] = license_expr
    return SimpleNamespace(metadata=meta, version="1.0", files=[])


def _finder(*dists: SimpleNamespace):
    by_name = {bn.canonical(d.metadata["Name"]): d for d in dists}

    def find(name: str) -> SimpleNamespace:
        try:
            return by_name[bn.canonical(name)]
        except KeyError:
            raise metadata.PackageNotFoundError(name) from None

    return find


def test_requirement_names_reads_pins_and_skips_the_rest() -> None:
    text = (
        "# exported\naiohttp==3.14.1\n    # via litellm\n"
        "pywin32==312 ; sys_platform == 'win32'\n-e .\n"
    )
    assert bn.requirement_names(text) == ["aiohttp", "pywin32"]


def test_a_package_without_any_licence_text_fails_the_build(tmp_path: Path) -> None:
    entries, errors = bn.collect(
        ["Bare_Pkg"], {}, tmp_path, find=_finder(_dist("Bare_Pkg", "MIT"))
    )
    assert entries == []
    assert len(errors) == 1 and "Bare_Pkg 1.0 (MIT)" in errors[0]
    assert "overrides.json" in errors[0]


def test_overrides_supply_a_text_or_a_note(tmp_path: Path) -> None:
    (tmp_path / "texts").mkdir()
    (tmp_path / "texts" / "a-LICENSE.txt").write_text(
        "A licence text", encoding="utf-8"
    )
    overrides = {
        "a-pkg": {
            "license": "MIT",
            "text": "texts/a-LICENSE.txt",
            "source": "https://a",
        },
        "b_pkg": {"license": "Vendor EULA", "note": "See https://b/eula"},
    }
    entries, errors = bn.collect(
        ["A.Pkg", "b-pkg", "not-installed"],
        overrides,
        tmp_path,
        find=_finder(_dist("A.Pkg"), _dist("b-pkg")),
    )
    assert errors == []
    a, b = entries
    assert (a.name, a.license, a.source, a.texts) == (
        "A.Pkg",
        "MIT",
        "https://a",
        [("a-LICENSE.txt", "A licence text")],
    )
    assert (b.license, b.texts) == ("Vendor EULA", [("note", "See https://b/eula")])


def test_licence_files_come_from_the_installed_metadata() -> None:
    files = bn.license_files(metadata.distribution("pydantic"))
    assert files and all(
        name.startswith("licenses/") or "LICENSE" in name for name, _ in files
    )
    assert any("Permission is hereby granted" in text for _, text in files)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"License-Expression": "Apache-2.0"}, "Apache-2.0"),
        ({"License": "BSD"}, "BSD"),
        (
            {
                "License": "x" * 200,
                "Classifier": "License :: OSI Approved :: MIT License",
            },
            "MIT License",
        ),
        ({}, "see the text below"),
    ],
)
def test_licence_label(fields: dict[str, str], expected: str) -> None:
    meta = email.message.Message()
    for key, value in fields.items():
        meta[key] = value
    assert bn.license_label(meta) == expected


def test_render_puts_corpusqa_first_then_an_index_then_the_texts() -> None:
    entries = [
        bn.Entry("zeta", "2.0", "MIT", "https://z", [("LICENSE", "Zeta text")]),
        bn.Entry(
            "Alpha", "1.0", "BSD-3-Clause", "", [("licenses/COPYING", "Alpha text")]
        ),
    ]
    text = bn.render("MIT License\nCopyright me", "0.1.2", entries)
    assert text.startswith("corpusqa 0.1.2\n\nMIT License\nCopyright me")
    assert "zeta 2.0" in text and "BSD-3-Clause" in text
    assert text.index("Zeta text") > text.index("THIRD-PARTY SOFTWARE")
    assert "Source: https://z" in text and "[licenses/COPYING]" in text
    assert "LICENSES.chromium.html" in text


def test_every_override_text_exists() -> None:
    notices = ROOT / "packaging" / "notices"
    packages = json.loads((notices / "overrides.json").read_text(encoding="utf-8"))[
        "packages"
    ]
    for name, entry in packages.items():
        assert ("text" in entry) != ("note" in entry), name
        if "text" in entry:
            assert (notices / entry["text"]).read_text(encoding="utf-8").strip(), name


def test_python_entry_finds_the_interpreter_licence() -> None:
    entry = bn.python_entry()
    assert entry.license == "PSF-2.0"
    assert "PYTHON SOFTWARE FOUNDATION" in entry.texts[0][1].upper()
