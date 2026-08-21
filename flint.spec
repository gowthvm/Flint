# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

hidden = [
    "wmi",
    "win32api",
    "win32con",
    "win32file",
    "win32timezone",
    "pythoncom",
    "pywintypes",
    "psutil",
]
# Include the native writer only if it was compiled.
if os.path.exists(os.path.join("core", "_native_writer.pyd")) or os.path.exists(
    os.path.join("core", "_native_writer.so")
):
    hidden.append("core._native_writer")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("ui/reference.html", "ui"), ("flint.ico", ".")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="flint",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="flint.ico",
    uac_admin=True,
    uac_uiaccess=False,
    version="version_info.txt",
)
