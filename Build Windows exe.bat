@echo off
REM Build StreamCut.exe for Windows.
REM
REM Run this on a Windows machine once. It downloads a static FFmpeg build,
REM installs the build dependencies, and produces dist\StreamCut.exe.
REM The result is a single file the editor can double-click.

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo  Building StreamCut.exe
echo ============================================================
echo.

REM --- 1. Python present? -------------------------------------------------
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required to BUILD the exe ^(not to run it^).
  echo Install it from https://www.python.org/downloads/windows/ and tick
  echo "Add python.exe to PATH", then run this script again.
  pause
  exit /b 1
)

REM --- 2. Build virtual environment --------------------------------------
if not exist "build-venv\Scripts\python.exe" (
  echo [1/5] Creating a build environment...
  py -m venv build-venv || goto :failed
) else (
  echo [1/5] Reusing the existing build environment.
)

set PY=build-venv\Scripts\python.exe

echo [2/5] Installing dependencies...
"%PY%" -m pip install --upgrade pip --quiet || goto :failed
"%PY%" -m pip install -r requirements.txt --quiet || goto :failed
REM Pinned to 6.x: StreamCut.spec uses the 6.x TOC format. A future major
REM release could change it again, so the build pins a known-good range.
"%PY%" -m pip install "pyinstaller>=6.0,<7" --quiet || goto :failed

REM --- 3. FFmpeg ----------------------------------------------------------
if exist "ffmpeg\ffmpeg.exe" (
  echo [3/5] FFmpeg already present.
) else (
  echo [3/5] Downloading FFmpeg ^(about 100 MB, one time^)...
  "%PY%" fetch_ffmpeg.py || goto :failed
)

REM --- 4. Build -----------------------------------------------------------
echo [4/5] Building the executable ^(this takes a few minutes^)...
"%PY%" -m PyInstaller --clean --noconfirm StreamCut.spec || goto :failed

REM --- 5. Done ------------------------------------------------------------
echo.
echo [5/5] Done.
echo.
echo   Output: %CD%\dist\StreamCut.exe
echo.
echo   Give that single file to the editor. It bundles Python and FFmpeg,
echo   so nothing needs to be installed.
echo.
pause
exit /b 0

:failed
echo.
echo BUILD FAILED. Scroll up for the error.
pause
exit /b 1
