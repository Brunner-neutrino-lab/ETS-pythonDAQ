"""
daq/gui/level4_tab.py

Level 4 — Temperature point tab.

Run the full measurement sequence at one temperature:
  wait for stable T → dark IV → dark pulse → [illuminated IV → illuminated pulse]
"""

import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QLineEdit, QCheckBox, QFileDialog, QProgressBar,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class Level4Tab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub    = hub
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- Output ----
        grp = QGroupBox("Output")
        fl  = QFormLayout(grp)
        self._run_dir = QLineEdit()
        self._run_id  = QLineEdit("run_001")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_dir)
        row = QHBoxLayout()
        row.addWidget(self._run_dir, 1)
        row.addWidget(btn_browse)
        fl.addRow("Run directory:", _wrap(row))
        fl.addRow("Run ID:", self._run_id)
        root.addWidget(grp)

        # ---- Temperature point ----
        grp = QGroupBox("Temperature Point")
        fl  = QFormLayout(grp)
        self._temp_k      = QDoubleSpinBox(); self._temp_k.setRange(1, 400); self._temp_k.setSuffix(" K"); self._temp_k.setValue(165.0)
        self._skip_wait   = QCheckBox("Skip temperature wait (already at target)")
        self._temp_tol    = QDoubleSpinBox(); self._temp_tol.setRange(0.01, 5); self._temp_tol.setSuffix(" K"); self._temp_tol.setDecimals(2)
        self._temp_stable = QDoubleSpinBox(); self._temp_stable.setRange(10, 7200); self._temp_stable.setSuffix(" s")
        fl.addRow("Temperature:", self._temp_k)
        fl.addRow("", self._skip_wait)
        fl.addRow("Tolerance:", self._temp_tol)
        fl.addRow("Stable hold:", self._temp_stable)
        root.addWidget(grp)

        # ---- Run button + progress ----
        self._btn_run  = QPushButton("Run Temperature Point")
        self._btn_run.clicked.connect(self._run)
        root.addWidget(self._btn_run)

        self._stage_lbl = QLabel("Stage: —")
        root.addWidget(self._stage_lbl)

        self._progress = QProgressBar()
        self._progress.setFormat("%v / %m SiPMs")
        root.addWidget(self._progress)

        # ---- Log ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Progress log…")
        root.addWidget(self._log)

    def showEvent(self, event):
        super().showEvent(event)
        cfg = self._hub.config
        self._run_dir.setText(cfg.data_dir)
        self._temp_tol.setValue(cfg.temp_tolerance_K)
        self._temp_stable.setValue(cfg.temp_stable_s)

    # ------------------------------------------------------------------

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Run Directory")
        if d:
            self._run_dir.setText(d)

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _check_ready(self) -> bool:
        if not self._hub.config._sipms:
            self._log_line("No channel map loaded."); return False
        if not self._run_dir.text().strip():
            self._log_line("Run directory is empty."); return False
        return True

    def _run(self):
        if not self._check_ready(): return

        temp_K     = self._temp_k.value()
        skip_wait  = self._skip_wait.isChecked()
        hub        = self._hub
        n_sipms    = len(hub.config.sipm_list())

        self._progress.setMaximum(n_sipms)
        self._progress.setValue(0)
        self._log_line(f"Starting temperature point @ {temp_K:.1f} K — {n_sipms} SiPMs…")
        if skip_wait:
            self._log_line("  (temperature wait skipped)")

        mdir = os.path.join(self._run_dir.text().strip(), self._run_id.text().strip())
        os.makedirs(mdir, exist_ok=True)

        from daq.resume import RunManifest
        from daq.storage import RunFile, run_filename

        manifest = RunManifest(mdir, hub.config)
        manifest.generate(hub.config)
        manifest.save()
        run_file = RunFile(run_filename(self._run_dir.text().strip(), self._run_id.text().strip()))

        _done_count = [0]

        def _on_progress(stage_name, done, total, sipm_id):
            _done_count[0] = done
            self._progress.setValue(done)
            self._stage_lbl.setText(f"Stage: {stage_name}")
            self._log_line(f"  [{done}/{total}] {stage_name} — SiPM {sipm_id}")

        def _fn():
            from daq.temppoint import run_temperature_point
            run_temperature_point(
                temperature_K = temp_K,
                instruments   = hub.instruments,
                config        = hub.config,
                manifest      = manifest,
                run_file      = run_file,
                on_progress   = _on_progress,
                skip_wait     = skip_wait,
            )
            return f"Temperature point {temp_K:.1f} K complete"

        w = DAQWorker(_fn)
        w.finished.connect(lambda msg: self._log_line(msg))
        w.error.connect(lambda tb: self._log_line(f"ERROR: {tb.splitlines()[-1]}"))
        w.log_msg.connect(self._log_line)
        self._worker = w
        w.start()


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
