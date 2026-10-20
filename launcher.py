"""Desktop launcher for StreamCut.

Entry point for the packaged Windows build. Starts the FastAPI server in a
background thread, waits until it answers, then opens the editor's default
browser at the local URL. Runs entirely on the user's own machine and IP, so
YouTube's datacenter-IP blocks do not apply.

Intended to be double-clicked. Any failure is shown in a dialog (when one is
available) and written to a log next to the executable, because a windowed
build has no console to print to.
"""

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

APP_NAME = "StreamCut"
HOST = "127.0.0.1"


def log_path() -> Path:
    """Where to write the startup log, next to the exe rather than in the bundle."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / "StreamCut log.txt"


def log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def show_error(message: str) -> None:
    """Best-effort error dialog; falls back to the log when unavailable.

    The dialog blocks until dismissed, which would hang an automated run with no
    one to click it, so STREAMCUT_NO_DIALOG=1 skips it. The build's smoke test
    sets that flag and reads the log instead.
    """
    log("ERROR: " + message)
    if os.environ.get("STREAMCUT_NO_DIALOG") == "1":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, message + "\n\nDetails were written to:\n" + str(log_path()),
            APP_NAME + " could not start", 0x10,
        )
    except Exception:
        pass


def find_free_port() -> int:
    """Pick an open port so a second copy or another app cannot collide."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def wait_until_ready(port: int, timeout: float = 90.0) -> bool:
    """Poll the health endpoint until the server accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main() -> int:
    log("=" * 60)
    log(f"Starting {APP_NAME}")
    log(f"Python {sys.version.split()[0]} | frozen={getattr(sys, 'frozen', False)}")
    log(f"Executable: {sys.executable}")

    try:
        import uvicorn
        from main import app
    except Exception:
        show_error("Could not load the application.\n\n" + traceback.format_exc())
        return 1

    port = find_free_port()
    url = f"http://{HOST}:{port}/"
    log(f"Starting server on {url}")

    config = uvicorn.Config(app, host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)

    def serve() -> None:
        try:
            server.run()
        except Exception:
            log("Server crashed:\n" + traceback.format_exc())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    if not wait_until_ready(port):
        show_error("The local server did not start in time.\n\n" + "See the log for details.")
        return 1

    log("Server is ready. Opening the browser.")
    try:
        webbrowser.open(url)
    except Exception:
        log("Could not open a browser automatically. Open this URL manually: " + url)

    log("StreamCut is running. Close this window to stop it.")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("Interrupted; shutting down.")
        server.should_exit = True

    log("Server stopped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        show_error(traceback.format_exc())
        sys.exit(1)
