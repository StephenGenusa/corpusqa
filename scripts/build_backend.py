"""Builds the frozen GUI backend with PyInstaller.

Creates an isolated venv at ``build/backend-venv`` (so a CUDA torch in the
development venv never leaks into the bundle), installs PyTorch from the
matching PyTorch wheel index, installs corpusqa with its ``build`` extra
(pinned to the versions in ``uv.lock``; needs ``uv``), then runs
PyInstaller with ``packaging/backend.spec``.

Output: ``dist/corpusqa-backend/corpusqa-backend[.exe]`` (onedir). Run it on
each target OS; PyInstaller does not cross-compile.

Usage::

    python scripts/build_backend.py [--torch cpu|cuda] [--unpinned]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / "build" / "backend-venv"
DIST_DIR = ROOT / "dist" / "corpusqa-backend"
SPEC = ROOT / "packaging" / "backend.spec"
TORCH_INDEX = {
    "cpu": "https://download.pytorch.org/whl/cpu",
    "cuda": "https://download.pytorch.org/whl/cu128",
}


def _venv_python(venv_dir: Path) -> Path:
    """Returns the interpreter path inside a venv on this OS."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run(*cmd: str | Path) -> None:
    """Runs a command from the repo root, echoing it; raises on failure."""
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT)


def _dir_size(path: Path) -> int:
    """Returns the total size in bytes of the files under ``path``."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _installed_torch(python: Path) -> str | None:
    """Returns the torch version installed in the venv, or None."""
    proc = subprocess.run(
        [str(python), "-c", "import torch; print(torch.__version__)"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def pin_mode(uv_available: bool, unpinned: bool) -> str:
    """Decides how dependency versions are chosen for the build.

    Args:
        uv_available: Whether ``uv`` is on PATH.
        unpinned: The ``--unpinned`` opt-out was given.

    Returns:
        ``pinned`` (constrain to ``uv.lock``), ``unpinned`` (newest from
        PyPI, explicitly requested) or ``fail`` (uv missing, no opt-out).
    """
    if unpinned:
        return "unpinned"
    return "pinned" if uv_available else "fail"


def _lock_constraints(unpinned: bool) -> Path | None:
    """Writes ``uv.lock``'s pins as a pip constraints file.

    The bundle then ships the dependency versions the test suite ran with
    rather than whatever is newest on PyPI. Constraints only limit versions;
    they never add packages (e.g. CUDA wheels a CPU torch does not need).

    Args:
        unpinned: Skip pinning and install the newest versions.

    Returns:
        The constraints file, or None for an explicitly unpinned build.

    Raises:
        SystemExit: ``uv`` is not installed and ``unpinned`` is False.
    """
    uv = shutil.which("uv")
    mode = pin_mode(uv is not None, unpinned)
    if mode == "fail":
        raise SystemExit(
            "error: uv is required to pin to uv.lock (the tested dependency "
            "versions). Install it (https://docs.astral.sh/uv/getting-started/"
            "installation/, e.g. `pip install uv`) or pass --unpinned to build "
            "with the newest, untested versions."
        )
    if mode == "unpinned" or uv is None:
        print(
            "warning: --unpinned: installing the newest (untested) dependencies",
            file=sys.stderr,
        )
        return None
    out = ROOT / "build" / "constraints.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        uv,
        "export",
        "--frozen",
        "--no-hashes",
        "--no-dev",
        "--no-emit-project",
        "--extra",
        "build",
        "--quiet",
        "-o",
        out,
    )
    return out


def build(torch: str, unpinned: bool = False) -> Path:
    """Prepares the build venv and runs PyInstaller.

    Args:
        torch: ``cpu`` or ``cuda`` PyTorch wheels.
        unpinned: Install the newest dependencies instead of ``uv.lock``'s.

    Returns:
        The backend executable.
    """
    if torch == "cuda" and sys.platform == "darwin":
        raise SystemExit("error: PyTorch has no CUDA build for macOS; use --torch cpu")
    # First, so a missing uv stops the build before any venv or download.
    constraints = _lock_constraints(unpinned)
    if not VENV_DIR.exists():
        print(f"creating {VENV_DIR}", flush=True)
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
    python = _venv_python(VENV_DIR)
    pip = (python, "-m", "pip")
    _run(*pip, "install", "--upgrade", "pip")
    pinned: tuple[str | Path, ...] = ("-c", constraints) if constraints else ()

    installed = _installed_torch(python)
    # macOS wheels carry no "+cpu" suffix and are never CUDA.
    is_cuda = (
        installed is not None and "+cpu" not in installed and sys.platform != "darwin"
    )
    if installed is not None and is_cuda != (torch == "cuda"):
        # Switching variants: pip would otherwise keep the installed one.
        _run(*pip, "uninstall", "-y", "torch", "torchvision")
    # Only torch itself comes from the PyTorch index (a lock pin such as
    # torch==2.12.0 matches its 2.12.0+cpu); its dependencies come from PyPI
    # in the next step, which keeps the installed variant.
    _run(
        *pip,
        "install",
        "--no-deps",
        "torch",
        "torchvision",
        *pinned,
        "--index-url",
        TORCH_INDEX[torch],
    )
    _run(*pip, "install", f"{ROOT}[build]", *pinned)
    # Non-editable, and always reinstalled: PyInstaller analyses the installed
    # package, which must match the working tree even at the same version.
    _run(*pip, "install", "--no-deps", "--force-reinstall", str(ROOT))

    _run(
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        ROOT / "dist",
        "--workpath",
        ROOT / "build" / "pyinstaller",
        SPEC,
    )
    exe = DIST_DIR / ("corpusqa-backend.exe" if os.name == "nt" else "corpusqa-backend")
    if not exe.is_file():
        raise SystemExit(f"error: PyInstaller did not produce {exe}")
    return exe


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--torch",
        choices=sorted(TORCH_INDEX),
        default="cpu",
        help="PyTorch variant to bundle (default: cpu; cuda is ~3x larger)",
    )
    parser.add_argument(
        "--unpinned",
        action="store_true",
        help="do not pin to uv.lock (newest, untested versions; no uv needed)",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    exe = build(args.torch, args.unpinned)
    minutes = (time.monotonic() - started) / 60
    size_gb = _dir_size(DIST_DIR) / 1e9
    print(f"built {exe}")
    print(f"bundle size: {size_gb:.2f} GB ({DIST_DIR}); took {minutes:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
