# PyInstaller build definition for the StreamCut Windows desktop app.
#
# Produces a single StreamCut.exe that bundles Python, all Python dependencies,
# and (when present) the ffmpeg/ folder next to this file, so the editor needs
# nothing installed.
#
# Build with:  pyinstaller --clean --noconfirm StreamCut.spec

import os
import sys
from pathlib import Path

block_cipher = None

ROOT = Path(os.path.abspath(SPECPATH))

# Read-only resources appended to the bundle. PyInstaller unpacks these to a
# temporary directory at runtime, which the app locates via sys._MEIPASS.
datas = [
    (str(ROOT / "static"), "static"),
]

# Bundle ffmpeg/ffprobe when the build script has fetched them. Without these
# the exe still runs, but cutting requires ffmpeg on PATH.
ffmpeg_dir = ROOT / "ffmpeg"
if ffmpeg_dir.is_dir():
    datas.append((str(ffmpeg_dir), "ffmpeg"))

hiddenimports = [
    # The application module. Imported inside a function in launcher.py, which
    # PyInstaller's static analysis can miss, so it is listed explicitly.
    "main",
    # yt-dlp loads extractors dynamically, so PyInstaller cannot see them.
    # collect_all below covers the bulk; these guard the common entry points.
    "yt_dlp",
    "yt_dlp.extractor",
    "yt_dlp.postprocessor",
    "yt_dlp.compat",
    # FastAPI/uvicorn pieces imported by name at runtime.
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # Proxy support for SOCKS (STREAMCUT_PROXY_URL).
    "socks",
]

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PySide2", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# yt-dlp and its JS-challenge package ship data files that must come along.
#
# collect_all() returns datas as hook-style (src, dest_dir) 2-tuples, but entries
# appended to Analysis.datas afterwards must be (dest_name, src, "DATA") 3-tuples.
# Appending them raw fails the build with
# "ValueError: not enough values to unpack (expected 3, got 2)".
#
# format_binaries_and_datas() is PyInstaller's own converter. Using it (rather
# than expanding entries by hand) matters because some collect_all datas entries
# point at whole directories, such as the .dist-info folder; those must be
# expanded into their individual files, or the writer later fails with
# "IsADirectoryError".
from PyInstaller.building.build_main import format_binaries_and_datas
from PyInstaller.utils.hooks import collect_all

for package in ("yt_dlp", "yt_dlp_ejs"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        for dest_name, src_name in format_binaries_and_datas(pkg_datas, workingdir=str(ROOT)):
            a.datas.append((dest_name, src_name, "DATA"))
        a.binaries += pkg_binaries
        a.hiddenimports += pkg_hidden
    except Exception as exc:  # pragma: no cover - build-time diagnostics
        print(f"[spec] could not collect {package}: {exc}", file=sys.stderr)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# PyInstaller 6.x dropped the `zipfiles` argument that 4.x/5.x EXE calls passed
# between `a.binaries` and `a.datas`. Including it raises
# "ValueError: not enough values to unpack (expected 3, got 2)" at build time.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StreamCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True keeps a window open showing progress; useful for the editor to
    # see what is happening and to close the app when finished.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
