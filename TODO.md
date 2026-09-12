# TODO — Future Features

Backlog of features and improvements to consider for future releases.
Items are grouped by priority tier. Update this file as items are picked up.

---

## v1.10.0 — Competitive Edge

- [ ] **ARM64 builds** — Windows on ARM is growing. Flint is x64-only. Need cross-compilation or native ARM build.
- [ ] **Keyboard shortcuts for queue** — Ctrl+Q add, Delete remove, Ctrl+E expert toggle. Power users expect this.
- [ ] **ToggleSwitch accessibility names** — Every toggle reports "Verify after write" to screen readers. Add per-instance `accessible_name` param.
- [ ] **Settings file locking** — Two Flint instances can race on `set_many()`. Add `msvcrt.locking` best-effort lock.
- [ ] **Test coverage: `diskpart.py`** — Zero tests for partition formatting, ISO mounting, DISM image application.
- [ ] **Test coverage: `eject.py`** — Zero tests for drive ejection via SetupAPI/cfgmgr32.
- [ ] **CI: test on Python 3.10 + 3.14** — Only tests on 3.11 currently. Expand matrix.
- [ ] **`setup.py` reads version from `core/version.py`** — Stop hardcoding version in two places.

## v1.10.0 — Features

- [ ] **Direct Windows ISO download** — Rufus built-in feature. Users shouldn't have to hunt for ISOs.
- [ ] **Windows OOBE auto-config** — Local account, privacy settings. Natural extension of install media creation.
- [ ] **UEFI:NTFS bootloader** — Enable booting NTFS-formatted UEFI media for large Windows ISOs.
- [ ] **DOS/FreeDOS bootable USB** — Niche but expected by hardware/BIOS update users.

## v2.0.0 — Innovation

- [ ] **Fake flash drive detection** — Compare reported capacity vs actual writable sectors. Counterfeit drives are rampant. Rufus's most-loved safety feature.
- [ ] **Post-flash QEMU boot test** — Verify the USB actually boots before handing it to a client. Unique differentiator.
- [ ] **Distro catalog with auto-download** — Browse/download Ubuntu, Fedora, etc. from within Flint. "App store" for bootable media.
- [ ] **Multi-ISO boot (Ventoy-inspired)** — One drive, many ISOs. Killer feature for IT techs.
- [ ] **Plugin framework** — Let the community extend Flint: auto-install configs, custom menus, themes.
- [ ] **Forensic audit trail export** — JSON + markdown audit trail for IT/compliance workflows.
- [ ] **Parallel multi-drive writing** — Write same ISO to multiple USBs simultaneously for fleet deployment.
- [ ] **VHD/VHDX/FFU image creation** — Drive imaging for backup/provisioning workflows.
- [ ] **Linux persistence via GUI slider** — Native slider instead of requiring WSL.

---

## Completed

- [x] v1.9.0 — CLI system disk guard (refuse to flash/wipe/clone the OS drive)
- [x] v1.9.0 — kernel32() ctypes config at module init (eliminates per-call overhead)
- [x] v1.9.0 — CLI flags: --partition-scheme, --filesystem, --write-mode
- [x] v1.9.0 — Fake flash drive detection (non-destructive probe + write-back verify)
- [x] v1.9.0 — Auto-eject after write (settings toggle)
- [x] v1.9.0 — Auto-detect Linux ISOs and use file-copy mode (Rufus parity)
- [x] v1.8.0 — Auto-extract compressed images (.zip, .gz, .xz, .zst)
- [x] v1.8.0 — Dark/light theme auto-switch (Windows system theme)
- [x] v1.8.0 — SHA-256 hash clipboard copy from verify page
- [x] v1.8.0 — Consolidated Win32 helpers into `core/deviceio.py`
- [x] v1.8.0 — Verify speed/ETA rolling window fix
- [x] v1.8.0 — Decompression failure surfaces user-visible error
- [x] v1.7.0 — Unbuffered I/O for raw device writes
- [x] v1.7.0 — WipeWorker transient USB error retries
- [x] v1.6.0 — Transient USB write error retries with exponential backoff
