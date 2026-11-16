# PyInstaller spec for the frozen GUI backend (onedir).
#
# Build with `python scripts/build_backend.py`, not by hand: it prepares the
# isolated venv this spec expects. Output: dist/corpusqa-backend/.
#
# Onedir, not onefile: onefile would unpack gigabytes (torch, Docling) to a
# temp folder on every launch. Console subsystem (console=True), not
# windowed: Electron spawns the backend with stdio pipes, and the protocol
# needs fds 0/1/2 to exist (see corpusqa/gui_backend/server.py
# _claim_stdout); Electron's `windowsHide` keeps the console window hidden.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 -- defined by PyInstaller

datas = [(str(ROOT / "corpusqa.example.yaml"), ".")]
binaries = []
hiddenimports = []

# Packages that load data files, plugins or lazily imported submodules at
# runtime, which PyInstaller's import analysis cannot see:
# - docling*: format backends/pipelines, docling_parse's native resources
# - litellm: model price map and tokenizer JSON files
# - tiktoken/tiktoken_ext: encodings registered as plugins (token counting)
# - docx/pptx (python-docx/-pptx): default part templates (.docx conversion)
# - latex2mathml: its symbol table (equations in documents)
# - rapidocr: config and ONNX models (PDF OCR when ingest.ocr is on/auto)
for package in (
    "docling",
    "docling_core",
    "docling_parse",
    "docling_ibm_models",
    "litellm",
    "tiktoken",
    "tiktoken_ext",
    "docx",
    "pptx",
    "latex2mathml",
    "rapidocr",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(  # noqa: F821
    [str(ROOT / "corpusqa" / "gui_backend" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Never used by the backend; keeps the bundle smaller.
    excludes=["tkinter", "IPython", "pytest"],
    # python-docx/-pptx find their templates via paths such as
    # docx/parts/../templates, which need the subpackage folders on disk;
    # "pyz+py" also writes their .py sources, creating those folders.
    module_collection_mode={"docx": "pyz+py", "pptx": "pyz+py"},
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="corpusqa-backend",
    console=True,
    strip=False,
    upx=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="corpusqa-backend",
)
