"""
daq/webgui/webcam.py

USB webcam (Logitech C525 / any UVC device) integration for the NiceGUI shell.

A single background thread keeps `/dev/video0` open and continuously refreshes
a latest-frame JPEG buffer.  Two FastAPI endpoints are exposed:

  - GET /webcam.mjpeg   multipart MJPEG stream (auto-renders in <img src>)
  - GET /webcam.jpg     latest frame as a single JPEG snapshot

Multiple browser tabs all share the same capture — opencv only opens
`/dev/video0` once, regardless of how many viewers are connected.

`build_page()` returns the NiceGUI tab body (just an <img> pointing at the
stream + a couple of controls).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterator

import cv2
from fastapi.responses import Response, StreamingResponse
from nicegui import app, ui

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton frame grabber
# ---------------------------------------------------------------------------

class _WebcamGrabber:
    """One background thread + one shared latest-frame JPEG buffer.

    Calling start() is idempotent; the first call spawns the grabber thread,
    later calls are no-ops.  The thread is daemonised so it dies with the
    process — no shutdown hook needed.
    """

    def __init__(self,
                 device:  int  = 0,
                 width:   int  = 1280,
                 height:  int  = 720,
                 fps:     int  = 15,
                 quality: int  = 80):
        self.device  = device
        self.width   = width
        self.height  = height
        self.fps     = fps
        self.quality = quality

        self._lock    = threading.Lock()
        self._started = False
        self._stop    = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_jpeg: bytes | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        """Idempotently start the grabber thread."""
        with self._lock:
            if self._started:
                return
            self._stop.clear()
            self._started = True
            self._thread  = threading.Thread(
                target=self._run, name="webcam-grabber", daemon=True,
            )
            self._thread.start()
            log.info("webcam grabber starting (device=%s, %dx%d @ %d fps, q=%d)",
                     self.device, self.width, self.height, self.fps, self.quality)

    def latest_jpeg(self) -> bytes | None:
        return self._latest_jpeg   # bytes are immutable; atomic read

    def last_error(self) -> str | None:
        return self._last_error

    def _run(self) -> None:
        # cv2.CAP_V4L2 forces the V4L2 backend on Linux (avoids GStreamer
        # falling back to a slower path).
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self._last_error = f"cannot open /dev/video{self.device}"
            log.error("webcam: %s", self._last_error)
            self._started = False
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        # Some UVC cameras return blank frames for the first few reads while
        # exposure settles — drain a handful before publishing.
        for _ in range(4):
            cap.read()

        period_s = 1.0 / max(1, self.fps)
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)]
        log.info("webcam grabber running")

        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok or frame is None:
                    # Transient read miss — pause briefly and try again
                    self._last_error = "read returned no frame"
                    time.sleep(0.05)
                    continue
                ok2, buf = cv2.imencode(".jpg", frame, jpeg_params)
                if ok2:
                    self._latest_jpeg = bytes(buf)
                    self._last_error  = None
                dt = time.monotonic() - t0
                if dt < period_s:
                    time.sleep(period_s - dt)
        finally:
            try: cap.release()
            except Exception: pass
            with self._lock:
                self._latest_jpeg = None
                self._started     = False
            log.info("webcam grabber stopped")


# Module-level singleton — exactly one capture across all viewers
_CAM = _WebcamGrabber()


# ---------------------------------------------------------------------------
# MJPEG generator + FastAPI routes
# ---------------------------------------------------------------------------

def _mjpeg_iter(target_fps: int = 25) -> Iterator[bytes]:
    """Yield multipart-MJPEG chunks until the client disconnects."""
    _CAM.start()
    boundary = b"--frame"
    period_s = 1.0 / max(1, target_fps)
    # Wait briefly for the first frame so the browser doesn't see an empty body
    deadline = time.monotonic() + 3.0
    while _CAM.latest_jpeg() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    while True:
        jpeg = _CAM.latest_jpeg()
        if jpeg is None:
            time.sleep(0.1)
            continue
        yield (boundary + b"\r\n"
               + b"Content-Type: image/jpeg\r\n"
               + b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
               + jpeg + b"\r\n")
        time.sleep(period_s)


_routes_registered = False


def register_routes() -> None:
    """Idempotently register the /webcam.mjpeg and /webcam.jpg routes on
    the NiceGUI FastAPI app.  Safe to call from module-import paths that
    may execute more than once under --reload."""
    global _routes_registered
    if _routes_registered:
        return
    _routes_registered = True

    def _authed() -> bool:
        """Read the auth flag from the per-request user storage.  Same
        cookie/session that the @ui.page('/') login flow writes."""
        try:
            return bool(app.storage.user.get("authenticated", False))
        except Exception:
            return False

    @app.get("/webcam.mjpeg")
    def webcam_stream():
        if not _authed():
            return Response(b"login required", status_code=401,
                            media_type="text/plain")
        return StreamingResponse(
            _mjpeg_iter(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/webcam.jpg")
    def webcam_snapshot():
        if not _authed():
            return Response(b"login required", status_code=401,
                            media_type="text/plain")
        _CAM.start()
        # Wait briefly for the first frame in case the user just opened the page
        deadline = time.monotonic() + 3.0
        while _CAM.latest_jpeg() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        jpeg = _CAM.latest_jpeg()
        if jpeg is None:
            msg = (_CAM.last_error() or "webcam not available").encode()
            return Response(content=msg, status_code=503, media_type="text/plain")
        return Response(content=jpeg, media_type="image/jpeg")


# Register at import time so any caller pulling in this module gets the routes
register_routes()


# ---------------------------------------------------------------------------
# NiceGUI tab body
# ---------------------------------------------------------------------------

def build_page() -> None:
    """Render the webcam tab inside whatever container the caller provides."""
    ui.label(
        "Logitech C525 (/dev/video0). MJPEG stream at /webcam.mjpeg, "
        "single-shot snapshot at /webcam.jpg."
    ).classes("text-gray-400 text-sm")

    # The <img> tag with a multipart stream src is the lowest-friction way
    # to render a live MJPEG feed in a browser.  Cache-bust to avoid a stale
    # frame being shown if the browser was caching aggressively.
    ui.html(
        '<img id="webcam-feed" src="/webcam.mjpeg" '
        'style="max-width:100%; height:auto; border:1px solid var(--line); '
        'border-radius:6px; background:#000;" '
        'alt="webcam stream" />'
    )

    def snapshot():
        ui.download("/webcam.jpg", filename="webcam_snapshot.jpg")

    def reload_stream():
        # Force the browser to re-open the stream (e.g. after camera replug)
        ui.run_javascript(
            "(() => { const i = document.getElementById('webcam-feed'); "
            "if (i) i.src = '/webcam.mjpeg?t=' + Date.now(); })()"
        )

    with ui.row().classes("gap-2 mt-2"):
        ui.button("save snapshot", on_click=snapshot).props("color=primary")
        ui.button("reload stream", on_click=reload_stream)
