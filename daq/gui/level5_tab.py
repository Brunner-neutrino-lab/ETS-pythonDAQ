"""
daq/gui/level5_tab.py

Level 5 — Full run tab.

Start, stop, and monitor a complete experiment run across all temperatures.
Supports resume from a previous run directory.
"""

import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QLineEdit, QCheckBox, QFileDialog, QProgressBar,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class Level5Tab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub    = hub
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- Run setup ----
        grp = QGroupBox("Run Setup")
        fl  = QFormLayout(grp)
        self._run_dir    = QLineEdit()
        self._run_id     = QLineEdit("run_001")
        self._resume_chk = QCheckBox("Resume from existing run")
        self._resume_chk.setChecked(True)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_dir)
        row = QHBoxLayout()
        row.addWidget(self._run_dir, 1)
        row.addWidget(btn_browse)
        fl.addRow("Data directory:", _wrap(row))
        fl.addRow("Run ID:", self._run_id)
        fl.addRow("", self._resume_chk)
        root.addWidget(grp)

        # ---- Status ----
        grp = QGroupBox("Run Status")
        fl  = QFormLayout(grp)
        self._status_temp    = QLabel("Temperature: —")
        self._status_stage   = QLabel("Stage: —")
        self._status_steps   = QLabel("Steps: —")
        fl.addRow(self._status_temp)
        fl.addRow(self._status_stage)
        fl.addRow(self._status_steps)
        root.addWidget(grp)

        # ---- Controls ----
        self._btn_start = QPushButton("Start Run")
        self._btn_stop  = QPushButton("Stop (after current step)")
        self._btn_start.clicked.connect(self._start)
        self._btn_stop.clicked.connect(self._stop)
        self._btn_stop.setEnabled(False)
        root.addWidget(_hbox(self._btn_start, self._btn_stop))

        # ---- Progress bars ----
        grp = QGroupBox("Progress")
        fl  = QFormLayout(grp)
        self._prog_sipm  = QProgressBar(); self._prog_sipm.setFormat("SiPMs: %v / %m")
        self._prog_temp  = QProgressBar(); self._prog_temp.setFormat("Temps: %v / %m")
        fl.addRow("SiPMs:", self._prog_sipm)
        fl.addRow("Temperature points:", self._prog_temp)
        root.addWidget(grp)

        # ---- Log ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Run log…")
        root.addWidget(self._log)

    def showEvent(self, event):
        super().showEvent(event)
        self._run_dir.setText(self._hub.config.data_dir)

    # ------------------------------------------------------------------

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Data Directory")
        if d:
            self._run_dir.setText(d)

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _check_ready(self) -> bool:
        if not self._hub.config._sipms:
            self._log_line("No channel map loaded — load one in Config tab first."); return False
        if not self._run_dir.text().strip():
            self._log_line("Data directory is empty."); return False
        return True

    def _start(self):
        if not self._check_ready(): return

        hub    = self._hub
        resume = self._resume_chk.isChecked()
        run_id = self._run_id.text().strip()
        run_dir = os.path.join(self._run_dir.text().strip(), run_id)

        temps   = hub.config.temperatures_K
        n_sipms = len(hub.config.sipm_list())

        self._prog_temp.setMaximum(len(temps))
        self._prog_temp.setValue(0)
        self._prog_sipm.setMaximum(n_sipms)
        self._prog_sipm.setValue(0)

        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)

        self._log_line(f"Starting run — {len(temps)} temperatures, {n_sipms} SiPMs each")
        if resume:
            self._log_line("  Resume mode: will skip completed steps")

        _temp_idx = [0]

        def _on_progress(stage_name, done, total, sipm_id):
            self._prog_sipm.setValue(done)
            self._status_stage.setText(f"Stage: {stage_name}")
            self._status_steps.setText(f"SiPMs: {done}/{total}")
            if sipm_id:
                self._log_line(f"  [{done}/{total}] {stage_name} — SiPM {sipm_id}")

        def _fn():
            from daq.run import run_experiment
            # Patch on_progress to also update temp bar
            original_config = hub.config

            class _ProgressWrapper:
                def __call__(self_w, stage_name, done, total, sipm_id):
                    _on_progress(stage_name, done, total, sipm_id)

            run_experiment(
                config     = hub.config,
                run_dir    = run_dir,
                resume     = resume,
                on_progress = _ProgressWrapper(),
            )
            return "Run complete."

        w = DAQWorker(_fn)
        w.finished.connect(self._on_run_done)
        w.error.connect(self._on_run_error)
        w.log_msg.connect(self._log_line)
        self._worker = w
        w.start()

    def _stop(self):
        """Signal the worker to stop after the current step.
        The cleanest mechanism is to set a flag that the run loop checks.
        For now we terminate the thread (best-effort, non-destructive since
        HDF5 data is written after each step)."""
        if self._worker and self._worker.isRunning():
            self._log_line("Stop requested — will finish current SiPM then halt.")
            self._worker.requestInterruption()
        self._btn_stop.setEnabled(False)

    def _on_run_done(self, msg):
        self._log_line(str(msg))
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._status_stage.setText("Stage: done")

    def _on_run_error(self, tb: str):
        self._log_line(f"ERROR: {tb.splitlines()[-1]}")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)


# ---------------------------------------------------------------------------

def _hbox(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for wgt in widgets:
        h.addWidget(wgt)
    h.addStretch()
    return w


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
