# Flint — Windows-native Bootable USB & Disk Image Writer

Write ISO/DD disk images to USB drives on Windows, then verify the result.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%2064--bit-0078d6)
[![Website](https://img.shields.io/badge/website-flintweb.vercel.app-0078d6)](https://flintweb.vercel.app)

Flint is a lightweight, **Windows-native** utility for writing ISO and DD disk
images to USB drives. It writes raw images directly to the physical disk,
optionally re-reads the drive afterwards to confirm the write, and requires
explicit typed confirmation before any destructive action.

![Flint flashing an ISO to a USB drive](https://flintweb.vercel.app/assets/screenshot.png)

The full manual — user guide, CLI reference, troubleshooting and FAQ — lives
on the [Flint website](https://flintweb.vercel.app).

## Features

- Drag & drop or browse for an image — a SHA-256 hash is computed for verification
- Drive picker with model, size and serial for each detected USB drive
- Optional post-write verification (SHA-256 compare with mismatch offsets)
- Bad-block scan that retries unreadable sectors and reports their locations
- Expert mode: partition scheme, target system, filesystem and write mode
- Persistence for Linux live images and Windows To Go for Windows images
- Flash history with export/import and per-flash reports
- Back up a drive to an image file, or clone a drive onto another drive
- SHA-256 sidecar files (`image.iso.sha256`) validate the image before flashing
- Wipe with selectable standards: zero fill, single random pass (NIST), or
  DoD 5220.22-M (three passes: zeros, ones, random)
- Headless/scriptable mode (`flint flash`, `verify`, `wipe`, `backup`,
  `clone`, `queue`, `flash-all`) for automation and IT imaging workflows

## Download

- [Latest release](https://github.com/gowthvm/Flint/releases/latest) — download
  `flint.exe` (portable, no installation required).
- Windows 10/11, 64-bit.

> SmartScreen: the executable is currently unsigned, so Windows may show a
> "Windows protected your PC" warning. Click **More info → Run anyway**.
> Verify the download against the published SHA-256 checksum first:
>
> ```powershell
> certutil -hashfile flint.exe SHA256
> ```
>
> and compare the result with `flint.exe.sha256` on the release page.

### Install via pip

Windows users with Python 3.10+ can install Flint from PyPI:

```powershell
pip install flint-usb
```

This installs both a GUI and a CLI — no SmartScreen warning, no download
verification needed (pip generates the launcher locally):

```powershell
flint          # open the GUI
flintw         # GUI without console window
flint --help   # CLI usage
```

The installer pulls in PyQt6, psutil, pywin32 and wmi. On first run Flint
prompts for administrator privileges automatically. The native writer
extension is compiled into the wheel for full write performance.

## Quick start

1. **Pick an image** — drag & drop an ISO/IMG onto the drop zone, or click it
   to browse (Ctrl+O).
2. **Choose a target drive** — click the drive card and select from the list
   (F5 refreshes).
3. **Flash** — click "Flash drive". Confirm the target by typing the drive
   serial or name when prompted.

Flint runs elevated, so it will ask for administrator permission when started.
Every write and wipe is irreversible — the typed confirmation is your last
guard against wiping the wrong drive.

## Verification

- **Verify after write** re-reads the drive after writing (streaming SHA-256,
  live speed and remaining time).
- **Verify using SHA256** compares the read-back digest against the image and
  reports the offsets of any mismatched regions.
- **Bad-block scan** retries failed reads up to the configured number of times
  (default 3) and reports the 4096-aligned offsets of sectors that never read
  back; unreadable chunks are skipped so the rest of the image is still checked.
- On mismatch, Flint offers to retry the write or abort. A cancelled
  verification is reported as completed-but-unverified — never as a false
  success.

## Expert mode

Expert mode is enabled by default and can be turned off with the toggle on the
write page. It adds:

- **Partition scheme** (GPT / MBR / Auto), **target system** (UEFI / Legacy /
  Auto) and **filesystem** (FAT32 / NTFS / exFAT).
- **Write mode**: raw (DD) or file copy. File-copy mode repartitions and
  formats the drive, then copies the image contents onto it — it is Windows-only,
  requires elevation, and is skipped for hybrid ISOs, which are always written
  raw so their boot record survives.
- **Buffer size** for raw writes (4–64 MiB) and an optional **native writer**
  using unbuffered disk I/O for maximum throughput.
- **Persistence** (Linux) and **Windows To Go** (Windows) options — see below.
- Inline **?** buttons beside every option open the in-app reference.

> **Security warning:** every option on this panel repartitions or rewrites a
> physical drive. Wrong combinations can make a drive unbootable or erase it
> without recovery. Only use these options when you know what your target
> firmware and bootloader require; back up data first.

## Persistence and Windows To Go

- **Persistence** keeps changes between reboots on live Linux sticks
  (Ubuntu `casper-rw`, Debian live overlay). It requires WSL with an ext4
  tool to format the persistence image.
- **Windows To Go** applies a Windows installation ISO to the drive so it
  boots as a portable Windows installation (requires NTFS and elevation).
- Both features require file-copy mode, are mutually exclusive, and are only
  shown for supported images.

## Safety & limitations

- 64-bit Windows only.
- Flint writes raw images directly to disks — this **irreversibly erases data**.
  Always confirm the target and back up important data before use.
- Before a raw write to a FAT32 target, Flint refuses images containing files
  over 4 GiB (impossible on FAT32) unless you switch to NTFS/exFAT.
- Flash history is stored locally on your machine.

## Back up, clone and wipe

- **Back up** (drive picker → "Backup this drive to an image…") streams a USB
  drive into a `.img` file, locking the drive's volumes while reading. The
  backup's SHA-256 is shown in the completion report.
- **Clone** (drive picker → "Clone this drive to another…") copies a drive
  onto a second drive byte-for-byte. The target must be at least as large as
  the source, must be a different drive, and requires the same typed
  confirmation as a flash.
- **Wipe** methods (the ▾ menu next to "Wipe drive"):
  - **Zero fill (fast)** — single pass of zeros
  - **Random data (NIST)** — single pass of random data (NIST SP 800-88 clear)
  - **DoD 5220.22-M (3 passes)** — zeros, then ones, then random data

## Checksum sidecars

If a `*.sha256` file sits next to your image (`ubuntu.iso.sha256` or
`ubuntu.sha256`), Flint reads it, verifies the image digest against it, and
shows the result under the image source. A mismatch blocks flashing — a
corrupt or wrong image can never erase a drive by accident.

## Headless mode

Every feature is available headless for imaging labs, scripts and CI.
`flint` below is the `flint.exe` you downloaded. Commands that need it
relaunch elevated automatically (one UAC prompt); `list`, `doctor` and
`completions` need no privileges at all. The older `--cli` prefix is still
accepted as a compatibility alias:

```text
flint list
flint flash  --image image.iso --drive E: --confirm <serial> [--verify]
flint verify --drive E: [--sha256 <hex> --image image.iso]
flint wipe   --drive E: --confirm <serial> [--method zero|random|nist|dod]
flint backup --drive E: --out backup.img [--confirm <serial>]
flint clone  --from E: --to F: --confirm <serial of --to>
flint queue  --file list.txt --drive E: --confirm <serial>
flint flash-all --image image.iso [--image image2.iso ...] --confirm ARM [--timeout <seconds>]
flint doctor
flint completions | Out-File -Append $PROFILE
flint help [<command>]
```

- `--drive` accepts a serial number, volume letter (`E:`) or physical path
  (`\\.\PHYSICALDRIVE1`); it only selects the drive. `--confirm` is the
  safety check: it must match the full serial of the drive being destroyed,
  validated against the live drive list — a wrong serial can never match
  another drive. `flint list` prints every detected drive with the exact
  serial `--confirm` expects.
- `flash-all` is fleet mode: it writes every `--image` to every drive that
  is — or becomes — plugged in, until the time budget expires (default
  3600 s). Arming requires the literal word `ARM`.
- When `--confirm` is omitted on an interactive terminal, the serial is
  prompted for; a piped command without `--confirm` is refused, never
  guessed.
- `verify` without a digest runs a read-only bad-block scan; with `--sha256`
  it compares only the image's byte range against the drive, so `--image` is
  required to know how many bytes to check.
- The queue file holds one image path per line (`#` comments allowed); images
  are flashed to the same drive in order, stopping on the first failure.
- `--json` switches all output to NDJSON (progress, results, drive lists);
  `FLINT_PROGRESS=json` is equivalent and `FLINT_VERIFY=1` makes `flash`
  verify by default.
- **Streams are split**: data and the final `RESULT ok|fail|canceled: …`
  line go to stdout; `FLINT <pct> <speed>MB/s ETA <s>s` progress and notes
  go to stderr, so scripts capture stdout as pure data without `2>&1` noise.
- Exit codes: `0` ok, `1` failure, `2` cancelled, `3` usage/validation,
  `4` elevation denied.

## Signing

The release workflow signs `flint.exe` automatically when the
`WINDOWS_SIGNING_PFX` (base64 PFX) and `WINDOWS_SIGNING_PASSWORD` repository
secrets are set. To sign locally once you have a certificate:

```powershell
.\scripts\sign.ps1 -PfxPath .\cert.pfx -PfxPassword 'secret'
```

## Support

- User guide and full documentation: https://flintweb.vercel.app
- Report issues and open pull requests on GitHub:
  https://github.com/gowthvm/Flint

## License

- [MIT License](LICENSE) — Copyright (c) 2026 Gowtham G.K
