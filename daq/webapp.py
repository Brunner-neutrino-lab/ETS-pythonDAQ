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

from nicegui import ui

from daq.webgui import shell   # noqa: F401 — registers the @ui.page("/") route


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

    ui.run(host=args.host, port=args.port, reload=args.reload,
           title="nEXO SiPM DAQ", show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
