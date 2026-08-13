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

Expert options (partition & boot)
- Enable **Expert mode** from the options menu (⋯) or the checkbox on the Write page.
- Choose a **partition scheme** (GPT / MBR / Auto — GPT for UEFI targets, MBR for Legacy),
  **target system** (UEFI / Legacy / Auto) and **filesystem** (FAT32 / NTFS / exFAT).
- Choose a **write mode**: Raw (DD) or File copy.
  File-copy mode repartitions the drive (`diskpart`), formats it (`format.com`) and
  copies the ISO contents onto it (`robocopy`) instead of a raw byte write.
- File-copy mode is Windows-only, requires elevation, and is skipped for hybrid
  ISOs (ISO9660 + bootable MBR), which are always written raw so their boot record
  survives. Verification is skipped after file-copy writes because the drive is
  not byte-identical to the image.
- Hybrid detection is a fast in-process heuristic on the first 36 KiB (no
  external tools such as `isoinfo`/`7z` required): it checks the ISO9660 marker,
  the MBR boot signature, a non-empty MBR partition table and/or the syslinux
  `ISOHYBRID` marker, and corroborates with the El Torito boot record. When a
  hybrid ISO is loaded, file-copy and partition options are disabled and a
  "Hybrid ISO detected — raw write recommended" tooltip is shown; when unsure,
  raw (DD) remains the default.
- Expert choices persist in `%APPDATA%\Flint\settings.json`.

Expert options (persistence & Windows To Go)
- When a Linux ISO is selected (casper / filesystem.squashfs / live detected in
  its ISO9660 tree), an optional **Persistence** toggle appears in Expert mode.
  Persistence keeps changes between reboots on live Linux sticks.
  - Ubuntu-style images get a `casper-rw` ext4 image at the drive root and the
    boot config (`grub.cfg`, `isolinux.cfg`, `syslinux.cfg`, `extlinux.conf`)
    is patched to pass the `persistent` kernel option.
  - Debian-live images get a `live/persistence.conf` overlay instead.
  - The ext4 image is formatted with `wsl mke2fs` (falling back to a native
    `mke2fs`/`makefs` when WSL has no distribution). When no tool is available
    the image is still created but left unformatted — an explicit warning is
    shown — so it must be formatted as ext4 manually for persistence to work.
- When a Windows installation ISO is selected (sources/install.wim, .esd or
  .swm — including UDF-only images found via a raw scan), an optional
  **Windows To Go** toggle appears. It applies the image with
  `dism /Apply-Image /Index:1` and installs boot files with
  `bcdboot ... /f ALL`, so the stick boots as a portable Windows install.
  - Windows To Go requires NTFS (selected automatically) and elevation; the
    first edition in the image (index 1) is used by default.
  - Persistence and Windows To Go are mutually exclusive and both require
    File copy mode.

Safety & limitations
- Target platform: 64-bit Windows only.
- Flint writes raw images directly to disks — this will irreversibly erase data.
  Always confirm the target and back up important data before use.
- Use elevated permissions when required; Flint will prompt to elevate.

Support & contribution
- Open issues and pull requests on GitHub: https://github.com/gowthvm/Flint

License
- Refer to the repository for license information (if absent, request clarification).