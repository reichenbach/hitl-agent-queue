# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "hitl_tray_launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[(str(ROOT / "src" / "hitl_queue" / "app.html"), "hitl_queue")],
    hiddenimports=[
        "hitl_queue.app",
        "hitl_queue.tray",
        "pystray._darwin",
        "pystray._gtk",
        "pystray._win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HITL Tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="HITL Tray",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="HITL Tray.app",
        icon=None,
        bundle_identifier="com.hitlagentqueue.tray",
        version="0.1.0",
        info_plist={"LSUIElement": True},
    )
