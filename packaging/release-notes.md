Desktop builds of the corpusqa GUI. Each one includes the backend, so no
separate Python install is needed. A local model server (LM Studio or Ollama)
or a hosted provider is still required. LibreOffice is needed only for legacy
`.doc` files.

| System | File |
|---|---|
| Linux (x86-64) | `corpusqa-*-linux-x86_64.AppImage`, or `corpusqa-*-linux-amd64.deb` for Debian or Ubuntu |
| Windows (x64) | `corpusqa-*-windows-x64-setup.exe` |
| macOS (Apple silicon) | `corpusqa-*-macos-arm64.dmg`, or the `.zip` |

`SHA256SUMS.txt` lists each file's checksum.

**These builds are not code-signed,** so each system warns the first time the
app is opened:

- **Windows:** SmartScreen shows "Windows protected your PC". Click
  **More info**, then **Run anyway**.
- **macOS:** the system says the app cannot be opened. Drag it to
  Applications, try to open it once, then go to **System Settings → Privacy &
  Security** and click **Open Anyway**. You can also run
  `xattr -dr com.apple.quarantine /Applications/corpusqa.app` in Terminal.
- **Linux:** make the AppImage executable (`chmod +x corpusqa-*.AppImage`)
  and run it. Some distributions also need `libfuse2`.

On first launch the app creates its config in the per-user settings folder.
Settings → **Open config folder** shows where it is. Put API keys in `.env`
in that folder.
