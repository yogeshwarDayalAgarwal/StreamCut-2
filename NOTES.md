# StreamCut engineering notes

Measurements and decisions behind the current download pipeline. Useful when
YouTube changes something and this needs revisiting.

## Why the download step is split in two

`yt-dlp --download-sections` was the original approach. That flag forces the
ffmpeg downloader, and ffmpeg cannot use yt-dlp's parallel fragment
downloading. Measured on the same video and format:

| Downloader | Throughput |
| --- | --- |
| `--downloader ffmpeg` (what `--download-sections` forces) | ~185 KB/s |
| `--downloader native` (default) | ~3.4 MB/s |

That is roughly an 18x difference. So the pipeline now:

1. Downloads the source with yt-dlp's **native** downloader.
2. Cuts the segment with a separate **ffmpeg** pass.

A 60-second 1080p clip went from ~91s to ~37s end to end.

## Why we re-encode instead of stream-copying

`ffmpeg -c copy` is far faster (~0.15s) but only lands correctly when both the
start and end of the range fall exactly on keyframes. A sweep across ten
random ranges showed copied clips could:

- start late for fractional starts (SSIM ~0.88 at `17.3s` and `103.7s`), and
- truncate the tail (SSIM ~0.70-0.90 at the end frame).

Both cases drop requested frames, which is unacceptable for an editor who
expects the named range to be present. Re-encoding keeps both boundaries
frame-accurate, so the requested range is always fully contained.

Verified frame accuracy on real API output (SSIM, higher is better):

| Comparison | SSIM |
| --- | --- |
| clip@0s vs source@60.00s | 0.989 |
| clip@59s vs source@119.00s | 0.992 |
| clip@0s vs source@60.04s (next frame, control) | 0.940 |

The control being clearly worse is what proves the cut starts on the exact
requested frame rather than one before or after.

## Encoder choice

Measured for a 60-second 1080p cut. All produced exactly 1500 frames at the
source's 25fps, so none of them drop frames:

| Preset | Time | Size |
| --- | --- | --- |
| `veryfast` crf20 | 34s | 19 MB |
| `ultrafast` crf20 | 14s | 40 MB |
| `ultrafast` crf23 | 9s | 28 MB |
| stream copy | 0.15s | lossless, but boundaries drift |

`ultrafast` crf20 is the current default: roughly 2.5x faster than `veryfast`
with the same frame count, which keeps encoding off the critical path on
modest laptop CPUs. Reduce crf to 23 if size matters more than quality on
large jobs.

## yt-dlp version requirement

Downloads failed with **HTTP 403 Forbidden** from YouTube's media CDN while
metadata extraction still worked, which made it look like a PO token problem.
It was not. The deciding factor is the yt-dlp version:

| yt-dlp version | Result |
| --- | --- |
| 2026.06.09 | 403 Forbidden |
| 2026.08.19 | works |

Both versions reported Deno as an available JS runtime and logged the same
`jsc:deno` challenge solving, so the older build was simply running an outdated
solver. requirements.txt pins `yt-dlp>=2026.8.19` for this reason. When YouTube
breaks the download step again, bump that pin rather than adding workarounds.

## "Failed to extract any player response" (datacenter IPs)

A failure that appears when the app runs on a datacenter IP (Render, AWS,
DigitalOcean, ...) but never locally:

```
ERROR: [youtube] <id>: Failed to extract any player response
```

This is environmental. YouTube serves different responses per player client,
and from datacenter IPs one client can fail entirely while another succeeds.
`ytdlp_metadata()` in `main.py` handles it by trying the default client first
and only falling back to alternates (`web,mweb,tv` then `android,web`) when the
error text looks like a player-response or bot-check problem. Other errors
(private, unavailable) fail fast without retrying, since a different client
would not help.

The fallbacks make extraction resilient but are not a guarantee. If YouTube
starts hard-blocking an IP range, the durable fixes are a proxy (`--proxy`) or
running the app on a different network. The `/api/info` error message says as
much when this happens.

Since StreamCut now ships as a local desktop app, this failure mode only comes
up if someone redeploys it to a server.

## Portrait / vertical videos

yt-dlp reports the **longer edge** as `height`. A 1080x1920 Short therefore
arrives with `height=1920`, which the original logic would have advertised as
"4K" and matched with a `height=720` selector that grabbed the wrong stream.

Both `/api/info` and `/api/download` now work off the **short edge**:

- `/api/info` computes `min(width, height)` and buckets it to 720/1080/1440/2160,
  and returns a `vertical` flag the UI shows as "Vertical".
- `/api/download` caps the short edge (`height<=N` for landscape, `width<=N` for
  portrait) so a vertical 1080p request gets 720x1280, not 480x854 or 1080x1920.

Orientation is read from the `/api/info` cache (`INFO_CACHE`) when present, so a
download does not repeat the metadata extraction.

## YouTube blocking datacenter IPs

YouTube blocks traffic from datacenter IP ranges. This is a network-layer
decision made before the request reaches yt-dlp, so no flag, client list, or
cookie setting fixes it. See the section above for the error text. The remedies
are a residential proxy (`STREAMCUT_PROXY_URL`) or running from a network YouTube
treats as residential.

This was discovered while running StreamCut as a hosted web app, and it is the
reason the project now ships as a **Windows desktop app** instead: it runs on
the editor's own machine, so requests leave from a normal residential
connection and the block does not apply. It also removes hosting costs, a public
URL to protect, and a server to maintain.

## Windows desktop build notes

Packaging targets that matter for a one-file PyInstaller build:

- **Paths.** `static/` is read from the bundle (`sys._MEIPASS`); job output must
  NOT be, because a one-file exe's extraction directory is deleted on exit.
  `_resource_root()` and `_output_root()` in `main.py` handle the split, and
  packaged output goes to a `StreamCut downloads` folder next to the exe.
- **FFmpeg.** Bundled in a `ffmpeg/` folder inside the bundle. `ffmpeg_path()`
  prefers it, resolves the path, and `--ffmpeg-location` is passed to yt-dlp so
  merging works. Falls back to PATH when running from source.
- **yt-dlp extractors** are loaded dynamically, so PyInstaller cannot see them.
  `StreamCut.spec` uses `collect_all` for `yt_dlp` and `yt_dlp_ejs`.
- **PyInstaller 6.x TOC format.** `collect_all()` returns `datas` as hook-style
  2-tuples `(src, dest_dir)`, but entries appended to `Analysis.datas` after the
  fact must be 3-tuples `(dest_name, src, "DATA")`. Appending the raw 2-tuples
  fails with `ValueError: not enough values to unpack (expected 3, got 2)`.
  Convert them with `format_binaries_and_datas()` rather than by hand: some
  `collect_all` entries point at whole directories such as the `.dist-info`
  folder, and naive conversion makes the writer fail with `IsADirectoryError`.
  The same `ValueError` appears if `a.zipfiles` is passed to `EXE()`, which 6.x
  removed. Both were fixed together.
- **Ports.** The launcher picks a free port rather than hardcoding 8000, so a
  second copy or another app cannot collide with it.
- **Console build.** `console=True` keeps a window open. That gives the editor
  visible progress and an obvious way to quit, and lets startup errors surface
  instead of vanishing.
- **Subprocess encoding.** `main.py` passes `encoding="utf-8"` to every
  `subprocess.run`/`Popen`. Windows would otherwise decode yt-dlp and ffmpeg
  output as cp1252 and raise `UnicodeDecodeError` on any video whose title
  contains accents, emoji, or CJK text.

### Verifying the build without Windows

PyInstaller's analysis phase is platform-independent, so a macOS or Linux build
catches missing hidden imports, bad spec paths, and TOC-format mistakes before
ever touching Windows. Do that when changing `StreamCut.spec`:

```bash
python -m venv build-venv
./build-venv/bin/pip install -r requirements.txt pyinstaller
./build-venv/bin/python -m PyInstaller --clean --noconfirm \
    --distpath dist-mac-test --workpath build-mac-test StreamCut.spec
```

A successful run proves the spec resolves every import. The resulting binary is
macOS-only and is not what ships to the editor.

The exe is large (~200 MB) mostly because of FFmpeg. That is the deliberate
tradeoff for requiring zero installation on the target machine.

`ffmpeg/`, `build/`, `dist/`, `build-venv/` and the runtime log are gitignored;
`Build Windows exe.bat` refetches FFmpeg when needed.

## If it is ever hosted again

The project started as a hosted web app and was moved to a desktop app because
YouTube blocks downloads from datacenter IP ranges. If it is ever deployed to a
server again, these notes still apply:

- The image needs Deno installed, because yt-dlp uses a JavaScript runtime to
  solve YouTube's challenge. Metadata can succeed without it while downloads
  fail.
- Copy and install `requirements.txt` before the rest of the source so that
  application edits do not invalidate the dependency layer.
- A container's filesystem is ephemeral. Finished clips only need to live long
  enough for the browser to fetch them.
- A free-tier plan sleeps after idle periods and has limited CPU, so the first
  request after a sleep pays a cold-start cost. A `STREAMCUT_PROXY_URL`
  residential proxy is the only way to make YouTube extraction reliable from a
  datacenter IP.
