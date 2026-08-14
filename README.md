# Flint — Bootable USB Writer (Windows)
 _______  ___      ___   __    _  _______ 
|       ||   |    |   | |  |  | ||       |
|    ___||   |    |   | |   |_| ||_     _|
|   |___ |   |    |   | |       |  |   |  
|    ___||   |___ |   | |  _    |  |   |  
|   |    |       ||   | | | |   |  |   |  
|___|    |_______||___| |_|  |__|  |___|  

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

Releases & Signing
- The CI workflow (`.github/workflows/build-windows.yml`) builds `flint.exe`,
  computes its SHA-256 checksum (`certutil -hashfile dist\flint.exe SHA256`)
  and uploads both as workflow artifacts and as GitHub release assets.
  Trigger it from the Actions tab (workflow_dispatch) or by pushing to master.
  Releases are created as **drafts** (tagged `build-<run-number>`) and need
  manual publishing on the GitHub releases page — workflows dispatched from
  branches other than `master` used to create mislabeled live releases.
- Code signing is **optional**: it runs only when both repository secrets are
  set (Settings → Secrets and variables → Actions):
  - `WINDOWS_SIGNING_PFX` — your code-signing certificate as a **base64 string**
    of the password-protected `.pfx` (private key included)
  - `WINDOWS_SIGNING_PASSWORD` — the PFX password
- When the secrets are present, the workflow decodes the PFX on the runner,
  imports it into a temporary user certificate store, signs the EXE with
  `signtool sign /fd SHA256 /a /f cert.pfx /p <password>`, then deletes both
  the PFX file and the imported certificate. When they are missing, the build
  logs a clear "signing skipped" warning and still publishes the checksum
  (for the unsigned build).
- Generate a PFX:
  1. Self-signed (for testing only — SmartScreen will still warn):
     ```powershell
     $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Flint" -CertStoreLocation Cert:\CurrentUser\My
     $pw = ConvertTo-SecureString -String "change-me" -AsPlainText -Force
     Export-PfxCertificate -Cert $cert -FilePath flint-signing.pfx -Password $pw
     ```
  2. Production: buy an OV/EV code-signing certificate from a trusted CA and
     export it with its private key as a password-protected PFX.
- Set the secret from the PFX (base64, no line breaks):
  ```powershell
  [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes("flint-signing.pfx"))
  ```
- Verify a local build's checksum:
  ```powershell
  certutil -hashfile dist\flint.exe SHA256
  ```
- Never commit the PFX to the repository — it is consumed from secrets only.
  Download builds from the releases page: https://github.com/gowthvm/Flint/releases/latest

Key behaviors
- Drag & drop or browse for an image; SHA-256 is calculated to enable verification.
- Select a target drive from the drive picker (model, size, serial are shown).
- Destructive actions require typed confirmation to reduce accidental data loss.
- Optional post-write verification re-reads the drive and compares SHA-256.
- History of flashes is stored in `%APPDATA%\Flint\history.json`.

Advanced options (Expert Mode)
- Enable **Expert mode** from the options menu (⋯) or the toggle on the Write page.
  The choice persists in `%APPDATA%\Flint\settings.json`. Inline **?** help
  buttons beside each option open the in-app reference (`ui/reference.html`).
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
- **Security warning:** every option on this panel repartitions or rewrites a
  physical drive. Wrong combinations can make a drive unbootable or erase it
  without recovery. Only use these options when you know what your target
  firmware and bootloader require; back up data first.

Persistence and Windows To Go
- When a Linux ISO is selected (casper / filesystem.squashfs / live detected in
  its ISO9660 tree), an optional **Persistence** toggle appears in Expert mode.
  Persistence keeps changes between reboots on live Linux sticks.
  - Ubuntu-style images get a `casper-rw` ext4 image at the drive root and the
    boot config (`grub.cfg`, `isolinux.cfg`, `syslinux.cfg`, `extlinux.conf`)
    is patched to pass the `persistent` kernel option.
  - Debian-live images get a `live/persistence.conf` overlay instead.
- When a Windows installation ISO is selected (sources/install.wim, .esd or
  .swm — including UDF-only images found via a raw scan), an optional
  **Windows To Go** toggle appears. It applies the image with
  `dism /Apply-Image /Index:1` and installs boot files with
  `bcdboot ... /f ALL`, so the stick boots as a portable Windows install.
- External tool dependencies: Persistence needs `wsl mke2fs` (falling back to a
  native `mke2fs`/`makefs` when WSL has no distribution) to format the ext4
  image; when no tool is available the image is still created but left
  unformatted — an explicit warning is shown — so it must be formatted as ext4
  manually for persistence to work. Windows To Go needs `dism` and `bcdboot`
  (bundled with Windows). `diskpart`, `format.com` and `robocopy` back file-copy
  mode generally.
- Limitations: Windows To Go requires NTFS (selected automatically) and
  elevation; the first edition in the image (index 1) is used by default.
  Persistence and Windows To Go are mutually exclusive and both require
  File copy mode. These features are only meaningful for the distros named
  above; they are hidden for other images.

Performance & Native writer
- Raw writes use buffered chunks of 8 MiB by default; the buffer size is
  configurable in Expert mode (4–64 MiB) and stored in settings.
- Enable the optional **native writer** in Expert mode to use the compiled
  extension (`core/_native_writer.c`): it writes through Windows
  `CreateFile`/`WriteFile` with `FILE_FLAG_NO_BUFFERING` and sector-aligned
  buffers for the highest raw throughput, and reports progress like the Python
  path.
- Compile it with `python setup.py build_ext --inplace` (needs a C compiler;
  the Windows CI workflow does this before packaging). When the extension is
  not built, writes automatically fall back to pure-Python buffered IO — the
  native writer is strictly optional and the toggle simply does nothing if the
  extension is missing.
- After rebuilding the extension, make sure no stale
  `core/_native_writer.cp3xx-win_amd64.pyd` from an older Python build is
  lying around: it shadows the module and can silently deactivate the native
  writer (delete it, then rebuild).

Verification & bad-block scan
- After a raw write, "Verify after write" reads the drive back (streaming
  SHA-256, speed in MB/s is reported while it runs).
- "Verify using SHA256" compares the read-back digest against the image and
  reports the offsets of any mismatched regions.
- "Bad-block scan" retries failed reads up to the configured number of times
  (1–10, default 3) and reports the 4096-aligned offsets of sectors that
  never read back. Unreadable chunks are skipped on both the drive and the
  source image, so the rest of the image is still verified.
- When the check finds mismatches or unreadable sectors, Flint offers to
  retry the write or abort. A cancelled verification reports the write as
  completed-but-unverified — never as a false success. Verification is
  skipped after file-copy writes (the drive is not byte-identical to the
  image).
- Before a raw write to a FAT32 target, Flint checks the image for files over
  4 GiB (impossible on FAT32) and refuses to proceed unless you switch the
  filesystem to NTFS/exFAT or write raw (DD).

Safety & limitations
- Target platform: 64-bit Windows only.
- Flint writes raw images directly to disks — this will irreversibly erase data.
  Always confirm the target and back up important data before use.
- Use elevated permissions when required; Flint will prompt to elevate.
  Note that the packaged executable requests elevation at launch
  (`uac_admin=True` in `flint.spec`), so the whole app runs elevated rather
  than elevating per-operation. Revisit this flag if you ever add a
  non-elevated workflow.

Support & contribution
- Open issues and pull requests on GitHub: https://github.com/gowthvm/Flint

License
- Refer to the repository for license information (if absent, request clarification).