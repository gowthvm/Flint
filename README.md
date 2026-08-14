# Flint — Bootable USB Writer

Write ISO/DD disk images to USB drives on Windows, then verify the result.

![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%2064--bit-0078d6)

Flint is a lightweight, Windows-native utility for writing ISO and DD disk
images to USB drives. It writes raw images directly to the physical disk,
optionally re-reads the drive afterwards to confirm the write, and requires
explicit typed confirmation before any destructive action.

## Features

- Drag & drop or browse for an image — a SHA-256 hash is computed for verification
- Drive picker with model, size and serial for each detected USB drive
- Optional post-write verification (SHA-256 compare with mismatch offsets)
- Bad-block scan that retries unreadable sectors and reports their locations
- Expert mode: partition scheme, target system, filesystem and write mode
- Persistence for Linux live images and Windows To Go for Windows images
- Flash history with export/import and per-flash reports

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

## Support

- Report issues and open pull requests on GitHub:
  https://github.com/gowthvm/Flint

## License

- [MIT License](LICENSE) — Copyright (c) 2026 Gowtham G.K
