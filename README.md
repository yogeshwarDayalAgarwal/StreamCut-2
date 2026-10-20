# StreamCut — YouTube segment downloader (Windows desktop app)

StreamCut is a responsive browser UI with a YouTube embedded preview, start/end time inputs, draggable trim handles, a seek bar, source-quality lookup, and a backend job that uses yt-dlp + FFmpeg to process the selected range. It ships as a single self-contained `StreamCut.exe` that runs on the editor's own machine.

## Important limitations

- This is an MVP (minimum viable product) rather than a finished commercial product.
- The browser preview uses YouTube's embedded player. Some videos do not allow embedding.
- The backend uses `yt-dlp` to inspect and process source media. Only use it with videos you own or are authorized to process, and follow YouTube's terms and applicable law. It does not bypass DRM or access controls.
- Format availability depends on the source. The interface excludes options below 720p and lists source heights found by yt-dlp. If the exact chosen height is absent, the backend may choose the nearest available height at or below the selected cap, but never intentionally below 720p.
- Some YouTube videos/livestreams may be unavailable, require authentication, or be incompatible with segment extraction. Keyframes can affect exact cut boundaries.
- This backend is intended for local testing or a trusted environment. Before public deployment, add authentication, per-user rate limits, request/job quotas, storage cleanup, concurrency limits, and operational monitoring. Do not expose an unrestricted download endpoint to the public internet.
- A YouTube embed does not grant a general download API; use the backend only for authorized media processing.

## Requirements

- Python 3.10+
- `yt-dlp`
- FFmpeg

## Windows desktop app (recommended for editors)

The packaged Windows build runs StreamCut entirely on the user's own machine and
IP. This avoids the datacenter-IP blocks that make the hosted version unreliable
(see "YouTube blocking" below).

**Building it** — two options:

*On a Windows machine that has Python:*

1. Double-click `Build Windows exe.bat`.
2. Wait. It creates a build environment, downloads FFmpeg (~110 MB, one time),
   and produces `dist\StreamCut.exe`.

*Without any Windows machine, via GitHub Actions:*

1. Push this repository to GitHub (the workflow lives in
   `.github/workflows/build-windows.yml`).
2. Open the repository's **Actions** tab, pick **Build Windows exe**, and click
   **Run workflow**.
3. When it finishes (~5 minutes), download the **StreamCut-Windows** artifact
   from the run's summary page. It contains `StreamCut.exe` and the editor guide.
4. For a permanent download link, push a version tag instead:
   `git tag v1.0.0 && git push origin v1.0.0`. The exe is attached to a Release.

The runner only *builds* the exe; it never downloads from YouTube, so YouTube's
datacenter-IP blocking does not apply. The exe still runs on the editor's own
residential connection. The workflow also smoke-tests the built exe, so a broken
build fails in CI instead of reaching the editor.

**Using it:**

1. Copy `StreamCut.exe` anywhere on the target machine.
2. Double-click it. A console window opens and the browser opens automatically.
3. Finished clips land in a `StreamCut downloads` folder next to the exe.
4. Close the console window to quit.

Python and FFmpeg are bundled inside the exe. The editor installs nothing.

`launcher.py` is the entry point and `StreamCut.spec` is the PyInstaller
definition. `fetch_ffmpeg.py` downloads the FFmpeg binaries the build bundles.

### macOS (Homebrew)

```bash
brew install python yt-dlp ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

### Windows

Install Python from https://www.python.org/downloads/windows/ and FFmpeg from https://ffmpeg.org/download.html. Install yt-dlp with:

```powershell
py -m pip install -U yt-dlp
```

Then in PowerShell, from this folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload
```

Make sure `yt-dlp` and `ffmpeg` are available on PATH. If they are not, the API reports the missing dependencies.

### Open the app

Visit http://127.0.0.1:8000 in your browser.

## Architecture

- `static/index.html`: responsive frontend with a YouTube IFrame API player, draggable trim timeline, quality picker, and job progress UI wired to the API below
- `main.py`: FastAPI endpoints for metadata, quality lookup, asynchronous segment processing, and file delivery
- `downloads/`: temporary outputs; create a cleanup policy before using beyond local testing
- `NOTES.md`: measurements and reasoning behind the download/cut pipeline

### How a download is processed

Each job runs in two stages:

1. yt-dlp downloads the source with its **native** downloader (fast, parallel fragments).
2. ffmpeg cuts the segment, **re-encoding** so both boundaries are frame-accurate.

Re-encoding is deliberate. Stream-copying is much faster but silently drops
requested frames when the range does not land on keyframes. See `NOTES.md` for
the measured comparisons.

## API endpoints

- `GET /api/health`
- `POST /api/info` with `{"url":"https://www.youtube.com/watch?v=..."}`
- `POST /api/download` with `{"url":"...","start":"00:10:00","end":"00:15:00","quality":1080}`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/file`

## Building the Windows app

The app ships as a single `StreamCut.exe` that bundles Python and FFmpeg. The
editor installs nothing.

**Build it once, on a Windows machine with Python installed:**

1. Double-click `Build Windows exe.bat`.
2. Wait. It creates a build environment, downloads FFmpeg (~110 MB, one time),
   and produces `dist\StreamCut.exe`.

**Ship these two files to the editor:**

- `dist\StreamCut.exe`
- `HOW TO USE - Editor.txt`

**How the editor uses it:**

1. Put `StreamCut.exe` anywhere.
2. Double-click it. A console window opens, and the browser opens automatically.
3. Finished clips land in a `StreamCut downloads` folder next to the exe.
4. Close the console window to quit.

### Why an app instead of a website

A hosted version was tried first and abandoned. YouTube blocks downloads from
datacenter IP ranges, which is exactly what cloud hosts use. Running locally
means requests come from a normal residential connection, so the block does not
apply. It also removes hosting costs, a public URL that needs protecting, and a
server to maintain.

### Keeping yt-dlp current

If downloads start failing with an HTTP 403 while metadata still works, the
yt-dlp pin in `requirements.txt` is almost certainly stale. Bump `yt-dlp>=...`
to the newest release and rebuild.

If you want the editor to always get the newest yt-dlp without a rebuild,
`Build Windows exe.bat` can be changed to install the latest at build time, at
the cost of reproducible builds.


