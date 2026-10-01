"""Client-independent execution and observable state for Level-3 runs."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from . import primitives as P
from .ivprogress import IVLiveAccumulator, format_point_summary, format_sample_status
from .resume import RunManifest
from .sequence import build_sequence_steps, run_sequence
from .storage import RunFile, run_filename


log = logging.getLogger("daq.l3job")


@dataclass(frozen=True)
class L3JobRequest:
    specs: tuple
    run_dir: str
    run_id: str
    resume: bool
    iv_timeout_s: float


@dataclass(frozen=True)
class L3JobSnapshot:
    generation: int
    status: str
    run_id: str
    started: Optional[float]
    finished: Optional[float]
    n_entries: int
    n_steps: int
    entry_idx: Optional[int]
    entry_progress: float
    step_name: str
    step_progress: float
    iv_status: str
    summary: Optional[dict]
    error: Optional[str]
    logs: tuple


class L3Job:
    """Own an L3 acquisition independently of any NiceGUI client."""

    def __init__(self, max_log_lines: int = 100):
        self._lock = threading.RLock()
        self._running = False
        self._generation = 0
        self._status = "idle"
        self._run_id = ""
        self._started = None
        self._finished = None
        self._n_entries = 0
        self._n_steps = 0
        self._entry_idx = None
        self._entry_progress = 0.0
        self._step_name = ""
        self._step_progress = 0.0
        self._iv_status = "IV live — idle"
        self._summary = None
        self._error = None
        self._logs = deque(maxlen=max_log_lines)
        self._log_seq = 0
        self._iv = IVLiveAccumulator()
        self._abort = {"flag": False}

    def claim(self, request: L3JobRequest) -> bool:
        """Atomically reserve the singleton job before scheduling its worker."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._generation += 1
            self._status = "running"
            self._run_id = request.run_id
            self._started = time.time()
            self._finished = None
            self._n_entries = len(request.specs)
            self._n_steps = len(build_sequence_steps(request.specs))
            self._entry_idx = None
            self._entry_progress = 0.0
            self._step_name = ""
            self._step_progress = 0.0
            self._iv_status = "IV live — waiting for first sample"
            self._summary = None
            self._error = None
            self._logs.clear()
            self._log_seq = 0
            self._iv.reset()
            self._abort["flag"] = False
            self._append_log_locked(
                f"running {self._n_entries} entries ({self._n_steps} steps), "
                f"IV timeout {request.iv_timeout_s:g} s"
            )
            return True

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def request_abort(self, message: str) -> None:
        with self._lock:
            if not self._running:
                return
            self._abort["flag"] = True
            self._append_log_locked(message)

    def fail_to_start(self, exc: Exception) -> None:
        """Release a claim when the application cannot schedule its worker."""
        self._fail(exc)
        with self._lock:
            self._running = False

    def append_log(self, message: str) -> None:
        with self._lock:
            self._append_log_locked(message)

    def _append_log_locked(self, message: str) -> None:
        self._log_seq += 1
        stamp = time.strftime("%H:%M:%S")
        self._logs.append((self._log_seq, f"[{stamp}] {message}"))

    def _record_progress(self, entry_idx, n_entries, step_name, done, total):
        with self._lock:
            fraction = (float(done) / float(total)) if total else 0.0
            self._entry_idx = int(entry_idx)
            self._entry_progress = (
                (float(entry_idx) + fraction) / float(n_entries)
                if n_entries else 0.0
            )
            self._step_name = str(step_name)
            self._step_progress = fraction
            self._append_log_locked(
                f"entry {entry_idx + 1}/{n_entries} · {step_name} · "
                f"{done}/{total}"
            )

    def _record_iv(self, entry_idx, n_entries, illuminated, voltage,
                   currents, _timestamps, n_per_voltage, n_voltages):
        with self._lock:
            sample, point = self._iv.add(
                entry_idx, n_entries, illuminated, voltage, currents,
                n_per_voltage, n_voltages,
            )
            if sample is not None:
                self._iv_status = format_sample_status(sample)
            if point is not None:
                self._append_log_locked(format_point_summary(point))

    def _complete(self, summary: dict) -> None:
        with self._lock:
            self._summary = dict(summary)
            self._status = "aborted" if summary.get("aborted") else "done"
            self._finished = time.time()
            self._append_log_locked(f"{self._status} — {summary}")

    def _fail(self, exc: Exception) -> None:
        with self._lock:
            self._status = "failed"
            self._error = f"{type(exc).__name__}: {exc}"
            self._finished = time.time()
            self._append_log_locked(f"FAIL: {self._error}")

    def snapshot(self) -> L3JobSnapshot:
        with self._lock:
            return L3JobSnapshot(
                generation=self._generation,
                status=self._status,
                run_id=self._run_id,
                started=self._started,
                finished=self._finished,
                n_entries=self._n_entries,
                n_steps=self._n_steps,
                entry_idx=self._entry_idx,
                entry_progress=self._entry_progress,
                step_name=self._step_name,
                step_progress=self._step_progress,
                iv_status=self._iv_status,
                summary=dict(self._summary) if self._summary is not None else None,
                error=self._error,
                logs=tuple(self._logs),
            )

    def run_claimed(self, request: L3JobRequest,
                    instruments: dict, config: Any) -> dict:
        """Run a previously claimed job; no browser or UI object is accepted."""
        with self._lock:
            if not self._running:
                raise RuntimeError("L3 job must be claimed before execution")

        run_file = None
        try:
            steps = build_sequence_steps(request.specs)
            manifest_dir = os.path.join(request.run_dir, request.run_id)
            manifest = RunManifest(manifest_dir)
            if manifest.exists() and request.resume:
                manifest.load()
            else:
                manifest.set_steps(steps)
                manifest.save()

            run_file = RunFile(
                run_filename(request.run_dir, prefix=request.run_id),
                config=config,
            )
            run_file.open()
            summary = run_sequence(
                list(request.specs), instruments, config,
                run_file=run_file,
                manifest=manifest,
                on_progress=self._record_progress,
                abort=self._abort,
                on_iv_progress=self._record_iv,
                iv_timeout_s=float(request.iv_timeout_s),
            )
            self._complete(summary)
            return summary
        except Exception as exc:
            self._fail(exc)
            raise
        finally:
            if run_file is not None:
                try:
                    run_file.close()
                except Exception:
                    log.exception("could not close L3 RunFile")
            elec = instruments.get("elec")
            if elec is not None:
                try:
                    P.bias_off(elec)
                except Exception:
                    log.exception("could not switch bias off after L3 run")
            with self._lock:
                self._running = False
