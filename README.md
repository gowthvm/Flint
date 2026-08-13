# Flint — Bootable USB Writer (Windows)

Flint is a lightweight, Windows-native utility for writing ISO/DD images to USB
drives. It's built with PyQt6 and uses low-level Windows disk APIs for reliable
raw-image writes and optional read-back verification.

Download
- Latest Windows executable: https://github.com/gowthvm/Flint/releases/latest

Quick start
- Run the downloaded `flint.exe` on a Windows machine (recommended).
- From source:
  ```powershell
  python -m pip install -r requirements.txt
  pythonw flint.pyw
  ```

Build (for maintainers)
1. Install dev dependencies: `pip install -r requirements.txt pyinstaller`
2. Build using the included spec: `python -m PyInstaller --clean --noconfirm flint.spec`
3. The built EXE appears under `dist/`.

Key behaviors
- Drag & drop or browse for an image; SHA-256 is calculated to enable verification.
- Select a target drive from the drive picker (model, size, serial are shown).
- Destructive actions require typed confirmation to reduce accidental data loss.
- Optional post-write verification re-reads the drive and compares SHA-256.
- History of flashes is stored in `%APPDATA%\Flint\history.json`.

Safety & limitations
- Target platform: 64-bit Windows only.
- Flint writes raw images directly to disks — this will irreversibly erase data.
  Always confirm the target and back up important data before use.
- Use elevated permissions when required; Flint will prompt to elevate.

Support & contribution
- Open issues and pull requests on GitHub: https://github.com/gowthvm/Flint

License
- Refer to the repository for license information (if absent, request clarification).