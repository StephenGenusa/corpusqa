# Building the self-contained desktop app

The installed app needs no separate Python: the backend (the corpusqa
library, Docling, LiteLLM) is frozen with PyInstaller and shipped inside the
Electron app as `resources/backend/`.

Builds are per OS. PyInstaller does not cross-compile, so build the Linux
installers on Linux and the Windows installer on Windows, with the same two
commands.

## Requirements

- Python 3.11 (with `venv` and `pip`) to run the build script
- [uv](https://docs.astral.sh/uv/), used to pin dependencies to `uv.lock`
- Node.js 20+ and npm
- Roughly 10 GB of free disk for the build venv, PyInstaller work files and
  installers; network access for the PyTorch, PyPI and electron-builder
  downloads

## Build

```
python scripts/build_backend.py            # CPU PyTorch (default)
cd gui && npm install && npm run dist
```

`build_backend.py` creates an isolated venv at `build/backend-venv`,
installs PyTorch from the PyTorch wheel index (CPU wheels by default), then
`pip install .[build]`, then runs PyInstaller with `packaging/backend.spec`.
Every dependency is pinned to the version in `uv.lock` (the versions the
test suite runs with), which needs [uv](https://docs.astral.sh/uv/) on PATH
(`pip install uv`); without it the build stops with an error. To build with
the newest, untested versions instead, pass `--unpinned`.
The result is `dist/corpusqa-backend/corpusqa-backend` (`.exe` on Windows);
the script prints its size and build time. Rerunning reuses the venv.

`npm run dist` runs electron-builder, which copies `dist/corpusqa-backend/`
and `corpusqa.example.yaml` into the app's resources. Output in `gui/dist/`:

- Linux: `corpusqa-<version>.AppImage` and `corpusqa-gui_<version>_amd64.deb`
- Windows: `corpusqa Setup <version>.exe` (NSIS)

Typical sizes for a CPU build on Linux: backend folder about 1.6 GB,
AppImage about 630 MB, `.deb` about 490 MB.

### CPU or CUDA PyTorch

```
python scripts/build_backend.py --torch cuda
```

PyTorch is used only by Docling's PDF layout and table models. `.docx` and
text conversion do not use it, each file is converted once and cached, and
the LLM runs in Ollama or LM Studio on the GPU either way. The CPU build is
the default because it is several times smaller; build with CUDA only if
first-time PDF conversion speed matters more than download size. Switching
variants reinstalls torch in the build venv.

## Design notes

- **Onedir, not onefile.** A onefile build unpacks its whole contents
  (gigabytes) to a temp folder on every launch.
- **Console executable, not windowed.** The GUI talks to the backend in JSON
  lines over stdin/stdout. A console-subsystem executable always has fds
  0/1/2 when Electron spawns it with pipes; the backend then moves fd 1 to
  stderr so library output cannot corrupt the protocol stream
  (`_claim_stdout` in `corpusqa/gui_backend/server.py`). On Windows,
  Electron's `windowsHide` keeps the console window from appearing.
- **Hidden data.** The spec collects everything from `docling`,
  `docling_core`, `docling_parse`, `docling_ibm_models`, `litellm` (its model
  price map and tokenizer JSON files), `tiktoken`/`tiktoken_ext` (token
  counting for the extraction budget), `docx`/`pptx` (python-docx/-pptx
  part templates), `latex2mathml` and `rapidocr`; PyInstaller's import
  analysis cannot see those data files or plugin modules. python-docx and
  python-pptx are also shipped as source files (`module_collection_mode`),
  because they find their templates through paths such as
  `docx/parts/../templates`, which need the package folders on disk.
  Without that, every `.docx` fails to convert.

## Where the installed app keeps its settings

The config and secrets live in the per-user app data folder, not next to the
program:

| OS      | Folder                                            |
|---------|---------------------------------------------------|
| Linux   | `~/.config/corpusqa-gui/` (or `$XDG_CONFIG_HOME`) |
| Windows | `%APPDATA%\corpusqa-gui\`                         |

On first launch the app copies the bundled example to `corpusqa.yaml` there;
it never overwrites an existing one. Put API keys in `.env` in the same
folder (for example `OPENROUTER_API_KEY=...`). Settings → **Open config
folder** shows the folder in the file manager.

## Not bundled

- **LibreOffice**, needed only to convert legacy `.doc` files. Install it
  and make sure `soffice` is on PATH.
- **Ollama** and **LM Studio**, which run the local models.
- **Docling's PDF layout models.** They are downloaded from Hugging Face into
  the user cache on the first PDF conversion. `.docx` and text files need
  no download.

## Checking a build

The frozen backend speaks the same protocol as `python -m
corpusqa.gui_backend`:

```
printf '{"id":"1","cmd":"config.get"}\n' | \
  dist/corpusqa-backend/corpusqa-backend --config corpusqa.yaml
```

prints one JSON reply line. `--help` shows the options, including
`--env-file PATH` (default: `.env` next to the config file).
