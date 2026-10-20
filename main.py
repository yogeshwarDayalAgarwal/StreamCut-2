import os
import re
import shutil
import subprocess
import sys
import threading
import importlib.util
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

def _resource_root() -> Path:
    """Directory holding bundled read-only resources (static/, ffmpeg/).

    Under PyInstaller the app runs from a temporary extraction directory
    (``sys._MEIPASS``); running from source it is just this file's directory.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _output_root() -> Path:
    """Writable directory for job output.

    Never lives inside the bundle: a one-file exe's extraction directory is
    deleted on exit. Packaged builds write next to the exe so the editor can
    actually find the finished clips.
    """
    override = os.environ.get("STREAMCUT_OUTPUT_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return (Path(sys.executable).resolve().parent / "StreamCut downloads").resolve()
    return (Path(__file__).resolve().parent / "downloads").resolve()


ROOT = _resource_root()
STATIC = ROOT / "static"
OUTPUT = _output_root()
OUTPUT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="StreamCut", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

JOBS = {}
LOCK = threading.Lock()
# Remembers the last /api/info result per URL so /api/download can reuse the
# orientation without paying for a second yt-dlp extraction.
INFO_CACHE = {}

def validate_youtube_url(url: str) -> str:
    url = url.strip()
    # Limit to standard YouTube URLs. This also reduces SSRF risk.
    patterns = [
        r"^https?://(?:www\.)?youtube\.com/watch\?v=[\w-]{11}(?:[&#].*)?$",
        r"^https?://youtu\.be/[\w-]{11}(?:[?&#].*)?$",
        r"^https?://(?:www\.)?youtube\.com/live/[\w-]{11}(?:[?&#].*)?$",
        r"^https?://(?:www\.)?youtube\.com/shorts/[\w-]{11}(?:[?&#].*)?$",
        r"^https?://m\.youtube\.com/watch\?v=[\w-]{11}(?:[&#].*)?$",
    ]
    if not any(re.match(p, url) for p in patterns):
        raise HTTPException(400, "Enter a valid YouTube video URL.")
    return url

def parse_timestamp(value: str) -> int:
    try:
        parts = [int(x) for x in value.strip().split(":")]
        if len(parts) == 3:
            h, m, s = parts
            if m > 59 or s > 59: raise ValueError()
            total = h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = parts
            if s > 59: raise ValueError()
            total = m * 60 + s
        elif len(parts) == 1:
            total = parts[0]
        else:
            raise ValueError()
        if total < 0: raise ValueError()
        return total
    except Exception:
        raise HTTPException(400, f"Invalid timestamp: {value}. Use HH:MM:SS or MM:SS.")

class InfoRequest(BaseModel):
    url: str

# Resolutions the UI can offer, low to high. Both landscape and portrait sources
# are bucketed onto these values using their SHORT edge, so a 1080x1920 Short
# reports 1080 rather than 1920.
#
# The list starts at 144 because low-resolution sources are common: plenty of
# videos are only published at 240p or 360p, and those must remain downloadable.
QUALITY_BUCKETS = (144, 240, 360, 480, 720, 1080, 1440, 2160)


class DownloadRequest(BaseModel):
    url: str
    start: str
    end: str
    # No lower bound beyond 144: the requested resolution is a cap, and low
    # values are legitimate for low-resolution sources.
    quality: int = Field(ge=144, le=4320)

def format_seconds(value: float) -> str:
    """Format a number of seconds as a plain decimal string for ffmpeg."""
    return f"{max(0.0, float(value)):.3f}"


def ffmpeg_path(name: str = "ffmpeg") -> Optional[str]:
    """Locate ffmpeg/ffprobe, preferring a copy bundled with the application.

    A packaged Windows build ships its own binaries so the editor needs nothing
    installed. Falls back to PATH so running from source keeps working.
    Paths are resolved because yt-dlp receives the parent directory and
    validates it exists.
    """
    bundled = ROOT / "ffmpeg" / (name + ".exe" if os.name == "nt" else name)
    if bundled.exists():
        return str(bundled.resolve())
    bundled_plain = ROOT / "ffmpeg" / name
    if bundled_plain.exists():
        return str(bundled_plain.resolve())
    found = shutil.which(name)
    return str(Path(found).resolve()) if found else None


def ytdlp_command() -> list[str]:
    """Return the yt-dlp invocation, preferring the installed Python module.

    Running ``python -m yt_dlp`` guarantees the same version that
    requirements.txt pins is used everywhere (locally and in the container),
    instead of relying on whatever happens to be on PATH. A system binary is
    only used as a last resort when the module is missing.
    """
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    if shutil.which("yt-dlp") is not None:
        return ["yt-dlp"]
    return []


def ytdlp_ffmpeg_args() -> list[str]:
    """Tell yt-dlp where ffmpeg lives, so post-processing works when bundled."""
    ffmpeg = ffmpeg_path("ffmpeg")
    if ffmpeg:
        return ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    return []


def ytdlp_common_args() -> list[str]:
    """Flags every yt-dlp invocation needs, packaged or not."""
    return ytdlp_network_args() + ytdlp_ffmpeg_args()


def ytdlp_network_args() -> list[str]:
    """Optional transport flags, driven by environment variables.

    A datacenter IP (Render, AWS, DigitalOcean, ...) is frequently refused by
    YouTube before the request is even evaluated, and StreamCut was originally
    hosted on one. The desktop build runs on a residential connection and does
    not need this, but the options remain for anyone redeploying to a server:
    setting STREAMCUT_PROXY_URL routes both metadata and media traffic through a
    residential proxy.

    STREAMCUT_FORCE_IPV4 makes all connections over IPv4, which helps on hosts
    whose IPv6 range is treated more harshly than their IPv4 range.
    """
    args = []
    proxy = os.environ.get("STREAMCUT_PROXY_URL", "").strip()
    if proxy:
        args += ["--proxy", proxy]
    if os.environ.get("STREAMCUT_FORCE_IPV4", "").strip().lower() in ("1", "true", "yes"):
        args += ["--force-ipv4"]
    return args


def require_dependencies():
    missing = []
    if not ytdlp_command():
        missing.append("yt-dlp")
    if ffmpeg_path("ffmpeg") is None:
        missing.append("ffmpeg")
    if missing:
        raise HTTPException(503, "Missing dependencies: " + ", ".join(missing) +
                            ". Install them with: brew install yt-dlp ffmpeg")


# Player clients to try, in order. YouTube serves each client a slightly different
# response, and on datacenter IPs one client may fail with "Failed to extract any
# player response" while another succeeds. Trying more than one makes extraction
# resilient to that without changing behavior on a residential connection.
PLAYER_CLIENT_FALLBACKS = (None, "web,mweb,tv", "android,web")


def ytdlp_metadata(url: str, timeout: int = 60) -> tuple[int, str, str]:
    """Fetch yt-dlp's JSON metadata, retrying with alternate player clients.

    Returns (returncode, stdout, stderr) for the last attempt. The default
    client is tried first; the alternates only run when the failure looks like a
    player-response problem, so the happy path stays a single subprocess.
    """
    base = ytdlp_command()
    net = ytdlp_common_args()
    last = (1, "", "yt-dlp is not installed.")
    for client in PLAYER_CLIENT_FALLBACKS:
        cmd = base + ["--dump-single-json", "--no-warnings", "--skip-download"] + net
        if client:
            cmd += ["--extractor-args", f"youtube:player_client={client}"]
        cmd.append(url)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # Force UTF-8: Windows defaults to cp1252, but yt-dlp emits UTF-8.
                # Video titles with accents, emoji, or CJK text would otherwise
                # raise UnicodeDecodeError and fail extraction.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last = (1, "", "Timed out while reading video information.")
            continue
        if result.returncode == 0:
            return result.returncode, result.stdout, result.stderr
        last = (result.returncode, result.stdout, result.stderr)
        detail = (result.stderr or result.stdout or "").lower()
        # Only fall through to the next client when the failure is about the
        # player response or a bot check. Other errors (private, unavailable)
        # will not be fixed by a different client.
        if not any(word in detail for word in ("player response", "sign in to confirm", "not a bot", "po token")):
            break
    return last

@app.get("/", response_class=HTMLResponse)
def home():
    return (STATIC / "index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    missing = []
    if not ytdlp_command():
        missing.append("yt-dlp")
    if ffmpeg_path("ffmpeg") is None:
        missing.append("ffmpeg")
    return {"ok": not missing, "missing": missing}

@app.post("/api/info")
def video_info(req: InfoRequest):
    require_dependencies()
    url = validate_youtube_url(req.url)
    code, stdout, stderr = ytdlp_metadata(url)
    if code != 0:
        # Keep the UI message actionable while printing the real cause to the
        # console window and StreamCut log file for troubleshooting.
        detail = (stderr or stdout or "yt-dlp exited without an error message").strip()
        print(f"[StreamCut] yt-dlp metadata extraction failed for {url}: {detail[-5000:]}", flush=True)
        lowered = detail.lower()
        if "sign in to confirm" in lowered or "not a bot" in lowered or "po token" in lowered:
            message = "YouTube asked for a sign-in or bot check instead of returning the video. This is usually temporary; try again in a minute."
        elif "video unavailable" in lowered or "private video" in lowered:
            message = "YouTube reports this video as unavailable or private. Check that the video is public and still online."
        elif "javascript runtime" in lowered or "challenge" in lowered:
            message = "YouTube's JavaScript challenge could not be solved. Make sure StreamCut is fully extracted and not blocked by antivirus, then try again."
        elif "player response" in lowered:
            message = "YouTube did not return a usable player response. This is usually a datacenter-IP block; StreamCut works best on a normal home internet connection. See the StreamCut log for details."
        else:
            message = "StreamCut could not read the video's formats from YouTube. See the StreamCut log for the underlying yt-dlp error."
        raise HTTPException(400, message)
    try:
        data = json.loads(stdout)
    except Exception:
        raise HTTPException(502, "Could not parse video information.")
    formats = data.get("formats", [])

    # yt-dlp reports the longer edge as "height" for portrait/vertical videos,
    # so a 1080x1920 Short would otherwise be advertised as "4K". Use the
    # shorter edge as the true resolution and expose it as either 16:9 or 9:16.
    def shorter_edge(f):
        w, h = f.get("width"), f.get("height")
        if not w or not h:
            return None
        try:
            return min(int(w), int(h))
        except (TypeError, ValueError):
            return None

    vertical = bool(data.get("width") and data.get("height")
                    and int(data["height"]) > int(data["width"]))

    qualities = []
    for f in formats:
        if f.get("vcodec") in (None, "none"):
            continue
        edge = shorter_edge(f)
        if edge is None:
            continue
        # Offer standard buckets only, so the UI labels stay meaningful.
        # Non-standard widths map down to the nearest bucket. The list includes
        # low resolutions because plenty of sources are only available at 240p,
        # 360p, or 480p.
        bucket = max((q for q in QUALITY_BUCKETS if q <= edge), default=None)
        if bucket is None:
            # Below the lowest bucket (for example a 100-pixel thumbnail-sized
            # stream). Offer it as the smallest option rather than dropping it,
            # since the requested resolution is only a cap.
            bucket = QUALITY_BUCKETS[0]
        qualities.append(bucket)

    heights = sorted(set(qualities))
    duration = data.get("duration")
    info = {
        "id": data.get("id"),
        "title": data.get("title") or "YouTube video",
        "duration": duration,
        "thumbnail": data.get("thumbnail"),
        "vertical": vertical,
        "qualities": heights,
        "message": None if heights else "No downloadable video formats were found for this video."
    }
    with LOCK:
        INFO_CACHE[url] = info
        # Keep the cache from growing without bound on a long-running server.
        if len(INFO_CACHE) > 50:
            INFO_CACHE.pop(next(iter(INFO_CACHE)))
    return info

@app.post("/api/download")
def create_download(req: DownloadRequest):
    require_dependencies()
    url = validate_youtube_url(req.url)
    start_s, end_s = parse_timestamp(req.start), parse_timestamp(req.end)
    if end_s <= start_s:
        raise HTTPException(400, "End time must be later than start time.")
    if end_s - start_s > 4 * 3600:
        raise HTTPException(400, "Please select a segment of four hours or less.")
    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Cap the SHORT edge at the requested resolution. yt-dlp's default sorting
    # then picks the best stream within that cap. For portrait video the shorter
    # edge is the width, so capping "height" there would treat a 1080x1920 clip
    # as if it were 1920p. Orientation comes from the cached /api/info result
    # when available, so this does not repeat the extraction.
    #
    # There is no lower floor. A low-resolution source such as 240p or 360p must
    # still be downloadable, so the fallbacks step down as far as necessary
    # rather than requiring 720p.
    q = int(req.quality)
    vertical = False
    with LOCK:
        cached = INFO_CACHE.get(url)
    if cached:
        vertical = bool(cached.get("vertical"))
    else:
        try:
            code, stdout, _ = ytdlp_metadata(url)
            if code == 0:
                meta = json.loads(stdout)
                if meta.get("width") and meta.get("height"):
                    vertical = int(meta["height"]) > int(meta["width"])
        except Exception:
            pass

    edge = "width" if vertical else "height"
    fmt = (
        f"bestvideo[{edge}<={q}]+bestaudio"
        f"/best[{edge}<={q}]"
        f"/bestvideo+bestaudio"
        f"/best"
    )

    # Stage 1: fetch the media with yt-dlp's native downloader.
    #
    # We deliberately avoid yt-dlp's --download-sections here. That flag forces
    # the ffmpeg downloader, which cannot use yt-dlp's parallel fragment
    # downloading and measured ~18x slower (185 KB/s vs ~3.4 MB/s here).
    # Downloading natively and cutting in a second ffmpeg pass is dramatically
    # faster while keeping frame-accurate boundaries.
    source_template = str(job_dir / "source.%(ext)s")
    fetch_cmd = ytdlp_command() + ytdlp_common_args() + [
        "--no-warnings", "--newline",
        "--merge-output-format", "mp4",
        "-f", fmt, "-o", source_template, url,
    ]

    # Stage 2: cut the segment.
    #
    # We re-encode rather than stream-copy. Stream copy is faster but only
    # guarantees correct boundaries when both ends land exactly on keyframes;
    # a sweep across random ranges showed copied clips could start late (SSIM
    # ~0.88 for fractional starts) and truncate the tail (SSIM ~0.70-0.90),
    # which would silently drop requested frames. Re-encoding keeps both
    # boundaries frame-accurate, so the requested range is always fully
    # contained. See NOTES.md for the measurements.
    clip_path = job_dir / "clip.mp4"

    with LOCK:
        JOBS[job_id] = {"status": "processing", "progress": "Starting…", "file": None, "error": None}

    def set_progress(message: str):
        with LOCK:
            JOBS[job_id]["progress"] = message

    def run(cmd, on_line=None):
        """Run a subprocess, streaming output lines to a callback."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # UTF-8 with replacement: Windows would otherwise decode yt-dlp and
            # ffmpeg output as cp1252 and crash on non-ASCII characters.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        last_line = ""
        for line in proc.stdout:
            line = line.strip()
            if line:
                last_line = line[-400:]
                if on_line:
                    on_line(line)
        return proc.wait(), last_line

    def worker():
        try:
            set_progress("Downloading source media…")
            code, last = run(fetch_cmd, lambda line: set_progress(line))
            if code != 0:
                with LOCK:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["error"] = last or "Download failed. The requested resolution may not be available."
                    JOBS[job_id]["progress"] = "Failed"
                return

            sources = [p for p in job_dir.iterdir()
                       if p.is_file() and p.name.startswith("source.") and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
            if not sources:
                with LOCK:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["error"] = "Downloaded media was not found."
                    JOBS[job_id]["progress"] = "Failed"
                return
            source = sources[0]

            # Trim exactly to the requested range. -ss before -i seeks to the
            # preceding keyframe and decodes forward, so the first output frame
            # is the requested start frame, and -t covers the requested span.
            cut_start = float(start_s)
            duration = float(end_s) - cut_start

            set_progress("Cutting the selected segment…")
            cut_cmd = [
                ffmpeg_path("ffmpeg") or "ffmpeg",
                "-hide_banner", "-loglevel", "error", "-y",
                "-ss", format_seconds(cut_start),
                "-i", str(source),
                "-t", format_seconds(duration),
                # ultrafast keeps the cut ~4x faster than veryfast while still
                # decoding every frame, so nothing in the range is dropped.
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(clip_path),
            ]
            cut_code, cut_last = run(cut_cmd)
            if cut_code != 0 or not clip_path.exists():
                with LOCK:
                    JOBS[job_id]["status"] = "failed"
                    JOBS[job_id]["error"] = cut_last or "FFmpeg could not cut the segment."
                    JOBS[job_id]["progress"] = "Failed"
                return

            # The full source is large; keep only the finished clip.
            try:
                source.unlink()
            except OSError:
                pass

            with LOCK:
                JOBS[job_id]["status"] = "completed"
                JOBS[job_id]["file"] = str(clip_path)
                JOBS[job_id]["progress"] = "Ready to download"
        except Exception as exc:
            with LOCK:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = str(exc)
                JOBS[job_id]["progress"] = "Failed"
    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return {"job_id": job_id, "status": job["status"], "progress": job["progress"], "error": job["error"],
                "download_url": f"/api/jobs/{job_id}/file" if job["status"] == "completed" else None}

@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str, background_tasks: BackgroundTasks):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "completed" or not job["file"]:
            raise HTTPException(404, "File is not ready.")
        path = Path(job["file"])
    if not path.exists():
        raise HTTPException(404, "The output file no longer exists.")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
