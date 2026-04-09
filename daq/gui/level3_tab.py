"""
daq/gui/level3_tab.py

Level 3 — Tile sweep tab.

Run an IV sweep or pulse run across all SiPMs on the tile, saving to HDF5.
Requires a channel map to be loaded and a run directory to be set.
"""

import os

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QLineEdit, QCheckBox, QFileDialog, QProgressBar,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class Level3Tab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub    = hub
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- Run file ----
        grp = QGroupBox("Output")
        fl  = QFormLayout(grp)
        self._run_dir  = QLineEdit()
        self._run_id   = QLineEdit("run_001")
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_dir)
        row = QHBoxLayout()
        row.addWidget(self._run_dir, 1)
        row.addWidget(btn_browse)
        fl.addRow("Run directory:", _wrap(row))
        fl.addRow("Run ID:", self._run_id)
        root.addWidget(grp)

        # ---- Sweep options ----
        grp = QGroupBox("Sweep Options")
        fl  = QFormLayout(grp)
        self._illum_check = QCheckBox("Illuminated")
        self._temp_k      = QDoubleSpinBox(); self._temp_k.setRange(1, 400); self._temp_k.setSuffix(" K"); self._temp_k.setValue(300.0)
        self._flux_int    = QSpinBox(); self._flux_int.setRange(1, 96); self._flux_int.setValue(8)
        fl.addRow("Temperature:", self._temp_k)
        fl.addRow("", self._illum_check)
        fl.addRow("Flux check every N SiPMs:", self._flux_int)
        root.addWidget(grp)

        # ---- IV sweep controls ----
        grp = QGroupBox("IV Sweep")
        fl  = QFormLayout(grp)
        self._iv_start = QDoubleSpinBox(); self._iv_start.setRange(-1000, 1000); self._iv_start.setSuffix(" V")
        self._iv_stop  = QDoubleSpinBox(); self._iv_stop.setRange(-1000, 1000);  self._iv_stop.setSuffix(" V")
        self._iv_step  = QDoubleSpinBox(); self._iv_step.setRange(0.001, 10);    self._iv_step.setSuffix(" V"); self._iv_step.setDecimals(3)
        self._iv_npt   = QSpinBox();       self._iv_npt.setRange(1, 100)
        btn_iv_cfg  = QPushButton("From Config")
        btn_run_iv  = QPushButton("Run Tile IV Sweep")
        btn_iv_cfg.clicked.connect(self._iv_from_config)
        btn_run_iv.clicked.connect(self._run_tile_iv)
        fl.addRow("Start:", self._iv_start)
        fl.addRow("Stop:",  self._iv_stop)
        fl.addRow("Step:",  self._iv_step)
        fl.addRow("Points per voltage:", self._iv_npt)
        fl.addRow(_hbox(btn_iv_cfg, btn_run_iv))
        root.addWidget(grp)

        # ---- Pulse controls ----
        grp = QGroupBox("Pulse Acquisition")
        fl  = QFormLayout(grp)
        self._p_bias  = QDoubleSpinBox(); self._p_bias.setRange(0, 1000); self._p_bias.setSuffix(" V")
        self._p_nwfm  = QSpinBox();       self._p_nwfm.setRange(1, 10_000_000); self._p_nwfm.setSingleStep(1000)
        btn_p_cfg   = QPushButton("From Config")
        btn_run_p   = QPushButton("Run Tile Pulse")
        btn_p_cfg.clicked.connect(self._pulse_from_config)
        btn_run_p.clicked.connect(self._run_tile_pulse)
        fl.addRow("Bias voltage:", self._p_bias)
        fl.addRow("N waveforms:",  self._p_nwfm)
        fl.addRow(_hbox(btn_p_cfg, btn_run_p))
        root.addWidget(grp)

        # ---- Progress ----
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
        self._iv_from_config()
        self._pulse_from_config()
        self._run_dir.setText(self._hub.config.data_dir)

    # ------------------------------------------------------------------

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Run Directory")
        if d:
            self._run_dir.setText(d)

    def _iv_from_config(self):
        cfg = self._hub.config
        self._iv_start.setValue(cfg.iv_voltage_start)
        self._iv_stop.setValue(cfg.iv_voltage_stop)
        self._iv_step.setValue(cfg.iv_voltage_step)
        self._iv_npt.setValue(cfg.iv_n_per_point)

    def _pulse_from_config(self):
        cfg = self._hub.config
        self._p_bias.setValue(cfg.pulse_bias_v)
        self._p_nwfm.setValue(cfg.pulse_n_waveforms)

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _check_ready(self) -> bool:
        if not self._hub.config._sipms:
            self._log_line("No channel map loaded — load one in Config tab first."); return False
        if not self._run_dir.text().strip():
            self._log_line("Run directory is empty."); return False
        return True

    def _make_run_file(self):
        from daq.storage import RunFile, run_filename
        path = run_filename(self._run_dir.text().strip(), self._run_id.text().strip())
        return RunFile(path)

    def _make_manifest(self):
        from daq.resume import RunManifest
        mdir = os.path.join(self._run_dir.text().strip(), self._run_id.text().strip())
        os.makedirs(mdir, exist_ok=True)
        return RunManifest(mdir, self._hub.config)

    # ------------------------------------------------------------------
    # Tile IV
    # ------------------------------------------------------------------

    def _run_tile_iv(self):
        if not self._check_ready(): return

        import numpy as np
        voltages    = list(np.arange(self._iv_start.value(),
                                     self._iv_stop.value() + self._iv_step.value() * 0.5,
                                     self._iv_step.value()))
        n_per       = self._iv_npt.value()
        illuminated = self._illum_check.isChecked()
        temp_K      = self._temp_k.value()
        flux_int    = self._flux_int.value()
        hub         = self._hub
        sipms       = [s.sipm_id for s in hub.config.sipm_list()]

        self._progress.setMaximum(len(sipms))
        self._progress.setValue(0)
        self._log_line(f"Starting tile IV sweep — {len(sipms)} SiPMs, {len(voltages)} voltages…")

        run_file = self._make_run_file()
        manifest = self._make_manifest()
        manifest.generate(hub.config)
        manifest.save()

        def _on_progress(done, total, sipm_id):
            self._progress.setValue(done)
            self._log_line(f"  [{done}/{total}] SiPM {sipm_id} done")

        def _fn():
            from daq.tile import tile_iv_sweep
            tile_iv_sweep(
                sipm_ids    = sipms,
                instruments = hub.instruments,
                config      = hub.config,
                temperature_K = temp_K,
                illuminated = illuminated,
                voltages    = voltages,
                n_per_point = n_per,
                flux_interval = flux_int,
                manifest    = manifest,
                run_file    = run_file,
                on_progress = _on_progress,
            )
            return f"Tile IV sweep complete — {len(sipms)} SiPMs"

        w = DAQWorker(_fn)
        w.finished.connect(lambda msg: self._log_line(msg))
        w.error.connect(lambda tb: self._log_line(f"ERROR: {tb.splitlines()[-1]}"))
        w.log_msg.connect(self._log_line)
        self._worker = w
        w.start()

    # ------------------------------------------------------------------
    # Tile Pulse
    # ------------------------------------------------------------------

    def _run_tile_pulse(self):
        if not self._check_ready(): return

        bias_v      = self._p_bias.value()
        n_waveforms = self._p_nwfm.value()
        illuminated = self._illum_check.isChecked()
        temp_K      = self._temp_k.value()
        flux_int    = self._flux_int.value()
        hub         = self._hub
        sipms       = [s.sipm_id for s in hub.config.sipm_list()]

        self._progress.setMaximum(len(sipms))
        self._progress.setValue(0)
        self._log_line(f"Starting tile pulse run — {len(sipms)} SiPMs, {n_waveforms} waveforms each…")

        run_file = self._make_run_file()
        manifest = self._make_manifest()
        manifest.generate(hub.config)
        manifest.save()

        def _on_progress(done, total, sipm_id):
            self._progress.setValue(done)
            self._log_line(f"  [{done}/{total}] SiPM {sipm_id} done")

        def _fn():
            from daq.tile import tile_pulse_run
            tile_pulse_run(
                sipm_ids    = sipms,
                instruments = hub.instruments,
                config      = hub.config,
                temperature_K = temp_K,
                illuminated = illuminated,
                bias_v      = bias_v,
                n_waveforms = n_waveforms,
                flux_interval = flux_int,
                manifest    = manifest,
                run_file    = run_file,
                on_progress = _on_progress,
            )
            return f"Tile pulse run complete — {len(sipms)} SiPMs"

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
