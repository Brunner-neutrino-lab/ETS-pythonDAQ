"""
daq/stage_logger.py

Background 1 Hz XY stage position logger with rotating CSV files.

Adapted from ESP32-stepper-controller/log_serial.py (SessionLogger pattern).

Folder structure mirrors that project:
  <log_dir>/<session_id>/
    <session_id>_meta.json
    <session_id>_0001.csv
    <session_id>_0002.csv
    ...

CSV columns: timestamp_s, x_mm, y_mm

The logger runs in a daemon thread so it stops automatically when the
main process exits.  Call stop() explicitly to flush and close files cleanly.

Usage
-----
    from daq.stage_logger import StagePositionLogger

    logger = StagePositionLogger(stage, log_dir="logs", max_mb=8.0)
    logger.start(session_id="run_001")

    # ... run experiment ...

    logger.stop()

The stage object must implement a position() method returning (x_mm, y_mm).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

CSV_HEADER       = "timestamp_s,x_mm,y_mm\n"
DEFAULT_MAX_BYTES = 8_000_000   # 8 MB per file
DEFAULT_POLL_HZ   = 1.0         # 1 sample per second


class StagePositionLogger:
    """
    Logs XY stage position at a fixed rate to rotating CSV files.

    Parameters
    ----------
    stage : object
        Stage controller with a position() -> (x_mm, y_mm) method.
        Pass None to run in simulation mode (logs (0.0, 0.0)).
    log_dir : str
        Root directory for session logs.
    max_mb : float
        Maximum size per CSV file in MB before rotating.
    poll_hz : float
        Logging rate in Hz. Default 1 Hz.
    """

    def __init__(self, stage, log_dir: str = "logs",
                 max_mb: float = 8.0, poll_hz: float = DEFAULT_POLL_HZ):
        self._stage      = stage
        self._log_dir    = log_dir
        self._max_bytes  = int(max_mb * 1_000_000)
        self._period_s   = 1.0 / poll_hz

        self._session_id  = None
        self._session_dir = None
        self._file        = None
        self._file_num    = 0
        self._total_samples = 0
        self._start_time  = None

        self._thread   = None
        self._running  = False
        self._lock     = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, session_id: str | None = None):
        """
        Start logging.

        Parameters
        ----------
        session_id : str, optional
            Identifier for this session (used as sub-directory name).
            Defaults to a timestamp string.
        """
        if self._running:
            return

        self._session_id  = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_dir = os.path.join(self._log_dir, self._session_id)
        os.makedirs(self._session_dir, exist_ok=True)

        self._start_time    = time.time()
        self._file_num      = 0
        self._total_samples = 0

        # Write session metadata
        meta = {
            "session_id":     self._session_id,
            "unix_timestamp": self._start_time,
            "iso_time":       datetime.fromtimestamp(self._start_time).isoformat(),
            "poll_hz":        1.0 / self._period_s,
            "max_file_bytes": self._max_bytes,
        }
        meta_path = os.path.join(
            self._session_dir, f"{self._session_id}_meta.json"
        )
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        self._open_next_file()
        self._running = True
        self._thread  = threading.Thread(
            target=self._poll_loop, daemon=True, name="StagePositionLogger"
        )
        self._thread.start()
        log.info("StagePositionLogger started — session %s", self._session_id)

    def stop(self) -> dict:
        """
        Stop logging and close files.

        Returns
        -------
        dict with session summary (session_id, total_samples, num_files, elapsed_s).
        """
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

        elapsed = time.time() - self._start_time if self._start_time else 0.0
        result  = {
            "session_id":    self._session_id,
            "total_samples": self._total_samples,
            "num_files":     self._file_num,
            "elapsed_s":     elapsed,
        }
        log.info(
            "StagePositionLogger stopped — %d samples, %d file(s), %.1f s",
            self._total_samples, self._file_num, elapsed,
        )
        return result

    @property
    def is_active(self) -> bool:
        return self._running

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def log_directory(self) -> str | None:
        return self._session_dir

    # ------------------------------------------------------------------
    # Background poll loop
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """Daemon thread: query stage position at poll_hz and write to CSV."""
        next_tick = time.monotonic()
        while self._running:
            now = time.monotonic()
            sleep_for = next_tick - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            next_tick += self._period_s

            try:
                if self._stage is not None:
                    x_mm, y_mm = self._stage.position()
                else:
                    x_mm, y_mm = 0.0, 0.0
            except Exception as e:
                log.warning("StagePositionLogger: position() failed: %s", e)
                continue

            ts = time.time()
            self._write_row(ts, x_mm, y_mm)

    def _write_row(self, timestamp_s: float, x_mm: float, y_mm: float):
        line = f"{timestamp_s:.3f},{x_mm:.4f},{y_mm:.4f}\n"
        with self._lock:
            if self._file is None:
                return
            self._file.write(line)
            self._file.flush()
            self._total_samples += 1

            try:
                file_size = os.path.getsize(self._file.name)
            except OSError:
                file_size = self._file.tell()

            if file_size >= self._max_bytes:
                self._open_next_file()

    def _open_next_file(self):
        """Close current file and open the next one in the rotation."""
        if self._file:
            self._file.close()
        self._file_num += 1
        filename = os.path.join(
            self._session_dir,
            f"{self._session_id}_{self._file_num:04d}.csv",
        )
        self._file = open(filename, "w", buffering=1)   # line-buffered
        self._file.write(CSV_HEADER)
        self._file.flush()
        log.debug("StagePositionLogger: opened %s", filename)
