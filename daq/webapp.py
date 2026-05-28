"""
daq/webapp.py

Web-based DAQ entry point.

Usage
-----
    python -m daq.webapp                  # bind 0.0.0.0:8765 (lab subnet)
    python -m daq.webapp --port 9000      # custom port
    python -m daq.webapp --host 127.0.0.1 # localhost only

Coexists with the PyQt5 GUI in daq/gui/ — both share the layered API
in daq/ (primitives → measurement → tile → temppoint → run).
"""

import argparse
import logging
import os
import socket

# Load secrets from a gitignored .env at the repo root before importing anything
# that reads them (e.g. DAQ_INFLUX_TOKEN consumed by daq/slowcontrol.py).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from nicegui import app, ui

from daq.webgui import shell   # noqa: F401 — registers the @ui.page("/") + /login routes
# Import webcam at startup so its FastAPI routes (/webcam.mjpeg, /webcam.jpg)
# are registered even when no user has visited the webcam tab yet.  These
# routes are gated by the same login check as the rest of the app.
from daq.webgui import webcam as _webcam   # noqa: F401 — registers /webcam.* routes
from daq import connection_state
from daq import labbook

# Pre-fill instrument addresses with whatever was last successfully used.
# The Connections tab inputs read from HUB.config — populating it before
# the page is built means "Connect all" can just press connect on each.
_applied = connection_state.load_into_config(shell.HUB.config)
if _applied:
    logging.getLogger("daq.webapp").info(
        "loaded last-known addresses: %s", _applied
    )

# Serve the plots/ directory so the status page can render thumbnails.
# Files written by daq.plotting land in <repo>/plots/*.png.
_PLOTS_DIR = os.path.join(os.path.dirname(__file__), "..", "plots")
if os.path.isdir(_PLOTS_DIR):
    app.add_static_files("/plots-img", _PLOTS_DIR)

# Lab-book attachment images.
app.add_static_files("/labbook-img", labbook.attachments_dir())


# Endpoint for clipboard-pasted images. A JS paste listener installed by
# _build_labbook_tab() POSTs image blobs here; the lab book tab's poll
# timer picks them up from labbook._paste_queue and adds them to the
# pending attachments for the next "post entry".
from fastapi import UploadFile, File, HTTPException  # noqa: E402 (local import keeps it adjacent to use)

@app.post("/labbook-paste")
async def labbook_paste(file: UploadFile = File(...)):
    # Auth — same cookie/session as the @ui.page('/') login flow writes.
    # An unauthenticated paste would let anyone with the URL silently
    # upload images into the lab book, so block at the route level.
    try:
        if not app.storage.user.get("authenticated", False):
            raise HTTPException(status_code=401, detail="login required")
    except HTTPException:
        raise
    except Exception:
        # No request context — refuse rather than risk a free-for-all.
        raise HTTPException(status_code=401, detail="login required")
    content = await file.read()
    fname = labbook.save_attachment(file.filename or "pasted.png", content)
    labbook.queue_pasted(fname)
    return {"filename": fname}


def _release_instruments():
    """
    Disconnect every instrument the hub has open. Called on webapp shutdown
    (SIGTERM/SIGINT/normal exit) so VISA sessions are released cleanly.

    Without this, the B2987's VXI-11 service in particular can get stuck
    holding a session, blocking the next connection attempt until the
    instrument is power-cycled.
    """
    # Print directly so the message is visible even if the logger has
    # already shut down by the time we reach this handler.
    import sys as _sys
    print("[daq.webapp] releasing instrument sessions...", flush=True)
    released = []
    for name in ("elec", "dig", "mux", "k6485", "wfg", "stage", "sc"):
        if getattr(shell.HUB, name, None) is None:
            continue
        fn = getattr(shell.HUB, f"disconnect_{name}", None)
        if fn is None:
            continue
        try:
            fn()
            released.append(name)
        except Exception as e:
            print(f"[daq.webapp] error releasing {name}: {e}", file=_sys.stderr, flush=True)
    print(f"[daq.webapp] released: {released or '(nothing was connected)'}", flush=True)


app.on_shutdown(_release_instruments)


def main():
    parser = argparse.ArgumentParser(description="ETS DAQ web GUI")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default 0.0.0.0 = lab subnet)")
    parser.add_argument("--port", type=int, default=8765,
                        help="Port (default 8765)")
    parser.add_argument("--reload", action="store_true",
                        help="Hot-reload on file changes (dev)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-18s %(levelname)-7s %(message)s")

    # Friendly banner: print the URL operators should open
    try:
        host_for_url = socket.gethostbyname(socket.gethostname())
    except Exception:
        host_for_url = "<this-host>"
    log = logging.getLogger("daq.webapp")
    log.info("Web DAQ starting at http://%s:%d/  (also http://localhost:%d/)",
             host_for_url, args.port, args.port)

    # storage_secret enables app.storage.user (server-side, cookie-keyed),
    # which the session/who's-connected tracker in daq/webgui/sessions.py uses
    # to remember each user's self-declared display name. Override with
    # DAQ_STORAGE_SECRET in .env if you want it stable across deploys.
    storage_secret = os.environ.get("DAQ_STORAGE_SECRET", "ets-daq-lab-default")

    # Custom favicon: a 4x4 SiPM array with one cell glowing UV-violet
    # under a diagonal VUV excimer photon beam.
    favicon_path = os.path.join(os.path.dirname(__file__),
                                "webgui", "static", "favicon.svg")

    ui.run(host=args.host, port=args.port, reload=args.reload,
           title="nEXO SiPM DAQ", show=False,
           favicon=favicon_path,
           storage_secret=storage_secret)


if __name__ in {"__main__", "__mp_main__"}:
    main()
