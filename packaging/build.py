"""Build a standalone SkyStamp app for the current OS with PyInstaller.

    pip install -e ".[dev]" && python packaging/build.py

Output: dist/SkyStamp.app (macOS), dist/SkyStamp/SkyStamp.exe (Windows), dist/SkyStamp/ (Linux).
ffmpeg comes from the imageio-ffmpeg wheel, so nothing else needs installing.
"""

import sys
from pathlib import Path

import PyInstaller.__main__

root = Path(__file__).resolve().parent.parent
args = [
    str(root / "packaging" / "launcher.py"),
    "--name", "SkyStamp",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--collect-data", "skystamp",
    "--collect-all", "imageio_ffmpeg",
    "--collect-data", "certifi",
    # Trim Qt modules we don't use
    *[f"--exclude-module=PySide6.{m}" for m in (
        "QtWebEngineCore", "QtWebEngineWidgets", "QtQml", "QtQuick", "Qt3DCore", "QtMultimedia",
        "QtCharts", "QtDataVisualization", "QtPdf", "QtSql", "QtTest", "QtBluetooth", "QtNetwork")],
]
icon = root / "packaging" / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
if icon.exists():
    args += ["--icon", str(icon)]
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "io.github.vld666.skystamp"]
PyInstaller.__main__.run(args)
