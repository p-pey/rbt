# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the XAUUSD Smart Trading Assistant.

Build with:   python packaging/build.py          (recommended: also runs the self-check)
or directly:  pyinstaller packaging/xauassist.spec --noconfirm --clean

Default = one-FOLDER build (dist/XAUAssistant/). It starts faster and triggers antivirus false
positives less often than one-file. Set XAU_ONEFILE=1 for a single XAUAssistant.exe.
Works on Windows (the real target); it also builds on Linux/macOS, which is how the spec is
smoke-tested without a Windows machine.
"""
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller
ONEFILE = os.environ.get("XAU_ONEFILE") == "1"
NAME = "XAUAssistant"


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


# ---- data files: land under sys._MEIPASS, where default_paths() looks for them ----
datas = [
    (str(ROOT / "assets" / "sounds" / "buy.wav"), "assets/sounds"),
    (str(ROOT / "assets" / "sounds" / "sell.wav"), "assets/sounds"),
    (str(ROOT / "assets" / "icon.ico"), "assets"),
    (str(ROOT / "config" / "config.example.toml"), "config"),
    (str(ROOT / "config" / "secrets.example.toml"), "config"),
]
binaries = []
hiddenimports = []

# Everything under xauassist is imported lazily in places (Qt, backtest), so include it all.
hiddenimports += collect_submodules("xauassist")

# Windows time-zone database (zoneinfo has no system tzdata on Windows).
if has("tzdata"):
    datas += collect_data_files("tzdata")
    hiddenimports += collect_submodules("tzdata")

# keyring discovers its backends dynamically; PyInstaller cannot see that.
if has("keyring"):
    hiddenimports += collect_submodules("keyring.backends")
    if sys.platform == "win32":
        hiddenimports += ["win32ctypes.core", "win32ctypes.core.ctypes"]

# MetaTrader5 is a compiled extension that needs numpy at import time.
if has("MetaTrader5"):
    hiddenimports += ["MetaTrader5", "numpy"]

excludes = [
    "tkinter", "pytest", "_pytest", "mypy", "ruff", "pip", "setuptools", "wheel", "pydoc",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

icon = str(ROOT / "assets" / "icon.ico") if sys.platform == "win32" else None

if ONEFILE:
    exe = EXE(  # noqa: F821
        pyz, a.scripts, a.binaries, a.datas, [],
        name=NAME, debug=False, strip=False,
        upx=False,  # UPX-packed executables are flagged by antivirus far more often
        console=False,  # windowed: no black console window (output goes to logs/)
        icon=icon,
    )
else:
    exe = EXE(  # noqa: F821
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=NAME, debug=False, strip=False, upx=False,
        console=False,
        icon=icon,
    )
    coll = COLLECT(  # noqa: F821
        exe, a.binaries, a.datas,
        strip=False, upx=False,
        name=NAME,
    )
