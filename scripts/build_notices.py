"""Writes THIRD_PARTY_NOTICES.txt for the packaged app.

Run by ``scripts/build_backend.py`` with the build venv's interpreter, so it
reads the licence files of exactly the package versions PyInstaller bundled.
Standard library only, since it runs inside that venv.

The notices cover:

- corpusqa itself (the repository's LICENSE);
- the Python interpreter the backend is frozen with;
- PyInstaller, whose bootloader starts the frozen backend;
- every runtime dependency in the ``uv export`` list given that is
  installed in the venv (platform-only packages are skipped where they are
  not installed); without a list, every installed package.

A package's text comes from the licence files in its ``.dist-info``; for
wheels that ship none, from ``packaging/notices/overrides.json``. The build
fails, listing every such package, when neither exists. Electron's and
Chromium's licences are not repeated here: electron-builder ships them next
to the app executable (``LICENSE.electron.txt``, ``LICENSES.chromium.html``).

Usage::

    python scripts/build_notices.py [--requirements REQS] --out FILE
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTICES_DIR = ROOT / "packaging" / "notices"
# Licence files kept at the top of a .dist-info (older wheels); newer ones
# put them all under .dist-info/licenses/ (PEP 639).
_LICENSE_NAME = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|COPYRIGHT)", re.IGNORECASE)
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
_RULE = "=" * 78
_THIN = "-" * 78


@dataclass
class Entry:
    """One bundled component and its licence texts."""

    name: str
    version: str
    license: str
    source: str
    texts: list[tuple[str, str]] = field(default_factory=list)


def canonical(name: str) -> str:
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_names(text: str) -> list[str]:
    """Project names pinned (``name==version``) in a requirements export."""
    names = []
    for line in text.splitlines():
        match = _REQUIREMENT.match(line.strip())
        if match:
            names.append(match.group(1))
    return names


def license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """``(file name, text)`` of the licence files in a distribution's metadata."""
    out = []
    for file in dist.files or []:
        parts = file.parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        inside = parts[1:]
        if not inside:
            continue
        wanted = inside[0] == "licenses" and len(inside) > 1
        wanted = wanted or (len(inside) == 1 and bool(_LICENSE_NAME.match(inside[0])))
        if not wanted:
            continue
        try:
            text = Path(str(file.locate())).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        if text.strip():
            out.append(("/".join(inside), text))
    return sorted(out)


def license_label(meta: metadata.PackageMetadata) -> str:
    """Short licence name from metadata: SPDX expression, field or classifiers."""
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    field_value = (meta.get("License") or "").strip()
    if field_value and "\n" not in field_value and len(field_value) <= 60:
        return field_value
    classifiers = [
        c.split(" :: ")[-1]
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::") and c != "License :: OSI Approved"
    ]
    return "; ".join(classifiers) or "see the text below"


def _source(meta: metadata.PackageMetadata) -> str:
    for url in meta.get_all("Project-URL") or []:
        label, _, link = url.partition(",")
        if label.strip().lower() in ("source", "repository", "homepage", "code"):
            return link.strip()
    return (meta.get("Home-page") or "").strip()


def collect(
    names: Iterable[str],
    overrides: dict[str, dict[str, str]],
    texts_dir: Path,
    find: Callable[[str], metadata.Distribution] = metadata.distribution,
) -> tuple[list[Entry], list[str]]:
    """Builds an entry per installed package; lists packages with no text.

    Args:
        names: Project names to cover (absent ones are skipped).
        overrides: ``overrides.json`` packages, keyed by project name: a
            ``text`` file, or a ``note`` for a licence published only online.
        texts_dir: Folder the override ``text`` paths are relative to.
        find: Looks up an installed distribution (injected for tests).

    Returns:
        ``(entries, errors)``, entries sorted by name.
    """
    by_name = {canonical(k): v for k, v in overrides.items()}
    entries: dict[str, Entry] = {}
    errors = []
    for name in names:
        key = canonical(name)
        if key in entries:
            continue
        try:
            dist = find(name)
        except metadata.PackageNotFoundError:
            continue  # not for this platform
        meta = dist.metadata
        entry = Entry(
            name=meta["Name"],
            version=dist.version,
            license=license_label(meta),
            source=_source(meta),
            texts=license_files(dist),
        )
        override = by_name.get(key)
        if not entry.texts and override:
            if "text" in override:
                path = texts_dir / override["text"]
                entry.texts = [(path.name, path.read_text(encoding="utf-8"))]
            else:
                entry.texts = [("note", override["note"])]
            entry.license = override.get("license", entry.license)
            entry.source = override.get("source", entry.source)
        if not entry.texts:
            errors.append(
                f"{entry.name} {entry.version} ({entry.license}): no licence file "
                "in its wheel; add one to packaging/notices/overrides.json"
            )
            continue
        entries[key] = entry
    return sorted(entries.values(), key=lambda e: e.name.lower()), errors


def python_entry() -> Entry:
    """The interpreter the backend is frozen with, and its licence."""
    version = ".".join(map(str, sys.version_info[:3]))
    base = Path(sys.base_prefix)
    major_minor = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    for candidate in (
        base / "lib" / major_minor / "LICENSE.txt",
        base / "LICENSE.txt",
        base / "Lib" / "LICENSE.txt",
    ):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            return Entry(
                "Python",
                version,
                "PSF-2.0",
                "https://www.python.org/",
                [("LICENSE.txt", text)],
            )
    raise SystemExit(f"error: cannot find Python's LICENSE.txt under {base}")


def render(app_license: str, app_version: str, entries: list[Entry]) -> str:
    """The notices file: corpusqa's licence, an index, then each component."""
    lines = [
        f"corpusqa {app_version}",
        "",
        app_license.strip(),
        "",
        _RULE,
        "THIRD-PARTY SOFTWARE",
        _RULE,
        "",
        "This app includes the software listed below, each under its own",
        "licence, reproduced in full after the list. Electron's and Chromium's",
        "licences are in LICENSE.electron.txt and LICENSES.chromium.html next to",
        "the app's executable.",
        "",
    ]
    width = max(len(f"{e.name} {e.version}") for e in entries)
    for e in entries:
        lines.append(f"  {f'{e.name} {e.version}':<{width}}  {e.license}")
    for e in entries:
        lines += ["", _RULE, f"{e.name} {e.version}", f"License: {e.license}"]
        if e.source:
            lines.append(f"Source: {e.source}")
        for file_name, text in e.texts:
            lines += [_THIN, f"[{file_name}]", "", text.strip()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--requirements",
        type=Path,
        help="`uv export` list of the runtime packages "
        "(default: every installed package)",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default="", help="corpusqa version")
    args = parser.parse_args(argv)

    overrides = json.loads((NOTICES_DIR / "overrides.json").read_text(encoding="utf-8"))
    if args.requirements:
        names = requirement_names(args.requirements.read_text(encoding="utf-8"))
    else:
        names = [d.metadata["Name"] for d in metadata.distributions()]
    entries, errors = collect(
        [*names, "pyinstaller"], overrides["packages"], NOTICES_DIR
    )
    if errors:
        print(
            f"error: {len(errors)} bundled package(s) without a licence text:",
            *errors,
            sep="\n  ",
            file=sys.stderr,
        )
        return 1
    entries = sorted([python_entry(), *entries], key=lambda e: e.name.lower())
    app_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(app_license, args.version, entries), encoding="utf-8")
    print(f"wrote {args.out}: {len(entries)} components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
