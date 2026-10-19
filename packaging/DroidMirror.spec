# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition for DroidMirror.

Build:

    .venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm packaging\\DroidMirror.spec

Result: ``dist\\DroidMirror.exe`` -- a single file with Python, Qt, FFmpeg and
adb inside it.

Two different directories matter at runtime, and the split is deliberate:

* ``sys._MEIPASS`` -- the temporary directory the one-file bootloader unpacks
  into. Read-only data shipped from here lives and dies with the process, so
  only the scrcpy server jar rides along.
* the folder holding ``DroidMirror.exe`` -- everything the app *writes*:
  ``settings.json``, ``profiles\\``, ``icons\\``, the cached server jar and
  ``crash.log``. ``droidmirror.config.project_root()`` resolves there when
  ``sys.frozen`` is set, so state survives restarts.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
PT = ROOT / "vendor" / "platform-tools"

#: Read-only payload. The jar is copied out to the writable cache on first
#: launch (``server.ensure_server_jar``), which is what makes an offline first
#: run work; downloads were the only way to get it otherwise.
datas = []
_jar = ROOT / "vendor" / "scrcpy-server-v4.1"
if _jar.is_file():
    datas.append((str(_jar), "vendor"))

#: adb is a subprocess, not an import, so it has to be listed explicitly.
#: PyInstaller follows the DLL imports of adb.exe by itself, but naming the two
#: companion DLLs makes the bundle independent of that analysis succeeding.
binaries = [
    (str(PT / name), "vendor/platform-tools")
    for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
    if (PT / name).is_file()
]

#: ``pypresence`` is imported inside a try/except at call time (optional
#: feature), so static analysis never sees it.
hiddenimports = collect_submodules("pypresence")

#: Not imported anywhere in the app. Excluding them is worth ~200 MB with
#: PySide6, whose wheel ships every Qt module whether you use it or not.
excludes = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtGraphs",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "tkinter",
    "pytest",
    "_pytest",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

_icon = ROOT / "packaging" / "DroidMirror.ico"

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DroidMirror",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Set DROIDMIRROR_CONSOLE=1 before building to get a console .exe that can
    # be run from a terminal for diagnostics (`DroidMirror.exe --check`).
    console=os.environ.get("DROIDMIRROR_CONSOLE") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_icon) if _icon.is_file() else None,
)
