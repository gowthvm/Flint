# Flint

A dark, Windows-native tool for writing ISO/DD images to USB drives, built on
PyQt6 with raw-disk Windows API writes.

## Features

- Drag-and-drop ISO selection with SHA-256 hashing
- Drive picker (model, size, bus, serial) with identity re-check before writing;
  unformatted drives are listed too
- Raw image writes in 4 MB chunks with live speed / ETA / cancel; volume is
  locked + dismounted during writes and `FlushFileBuffers` runs before success
- Optional post-write verification: read-back SHA-256 comparison of the drive;
  bootability check (MBR / GPT signatures) and safe eject after finishing
- Independent **Verify** page: compare a drive against an image or a pasted
  SHA-256, or hash a whole drive; full-disk wipe mode
- History (`%APPDATA%\Flint\history.json`) with export / import / clear and
  copyable flash reports
- Dark, light and high-contrast themes; resizable, keyboard-friendly window;
  taskbar progress, keep-awake, system tray toasts with sound
- Single-instance guard (second launch raises the running window), crash
  logging to `%LOCALAPPDATA%\Flint\crash.log`

## Build

```
pip install -r requirements.txt
pyinstaller flint.spec
```

The onefile build `dist\flint.exe` runs elevated (`uac_admin`).

## Run from source

```
pythonw flint.pyw
```

Double-clicking `flint.pyw` works too. `pythonw` runs windowless, so no
console window appears (use `python main.py` only when you want console
output for diagnostics). Flint restarts itself elevated via UAC when needed,
still windowless.

## Scope

- Target platform: 64-bit Windows (exe is x64-only).
- Raw image mode only: one ISO/DD image per drive. Multi-boot images such as
  Ventoy are **not** supported and not planned.
- The elevated window may briefly appear and hide behind other windows; this is
  the standard Windows UAC/Secure Desktop behaviour.

## Warning

Flint is intended for **casual verification only** and is provided without
warranty. Always back up your data, and verify the result independently with a
trusted tool and a known-good image. **This software can permanently destroy
data.** The developer is not liable for data loss, damage, or any other harm
caused by this software. Identifying the correct target drive is the user's
responsibility: Flint refuses to continue if a selected drive changes identity
or disconnect between selection and write.