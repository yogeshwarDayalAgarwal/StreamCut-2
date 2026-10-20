"""Download a static FFmpeg build into ./ffmpeg for the packaged Windows app.

The editor should not have to install FFmpeg, so the build fetches a static
build once and PyInstaller bundles it. Uses the long-standing gyan.dev Windows
builds, which are self-contained single executables.

Run automatically by "Build Windows exe.bat".
"""

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

# "essentials" is a smaller archive that still contains ffmpeg.exe and
# ffprobe.exe, which is all StreamCut uses.
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
TARGET = Path(__file__).resolve().parent / "ffmpeg"


def download(url: str) -> bytes:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        chunks, read = [], 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
            if total:
                pct = read * 100 // total
                print(f"\r  {pct:3d}%  {read // (1 << 20)} MB", end="", flush=True)
        print()
        return b"".join(chunks)


def main() -> int:
    if (TARGET / "ffmpeg.exe").exists() and (TARGET / "ffprobe.exe").exists():
        print("FFmpeg already present in ./ffmpeg")
        return 0

    try:
        blob = download(FFMPEG_URL)
    except Exception as exc:
        print(f"ERROR: could not download FFmpeg: {exc}", file=sys.stderr)
        print("Download it manually and place ffmpeg.exe and ffprobe.exe in ./ffmpeg", file=sys.stderr)
        return 1

    TARGET.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe"}
    found = set()

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for member in archive.namelist():
            name = Path(member).name.lower()
            if name in wanted and not member.endswith("/"):
                print(f"Extracting {name}")
                with archive.open(member) as src, open(TARGET / name, "wb") as dst:
                    dst.write(src.read())
                found.add(name)

    missing = wanted - found
    if missing:
        print(f"ERROR: archive did not contain: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    print(f"FFmpeg installed to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
