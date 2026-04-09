"""
daq/gui/level2_tab.py

Level 2 — Single-SiPM measurement tab.

Run an IV sweep or pulse acquisition on one selected SiPM.
Results are displayed inline; not saved to HDF5 (that is Level 3+).
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QComboBox, QCheckBox,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class Level2Tab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub     = hub
        self._workers = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- SiPM selector ----
        grp = QGroupBox("SiPM Selection")
        fl  = QFormLayout(grp)
        self._sipm_combo = QComboBox()
        self._sipm_combo.setMinimumWidth(200)
        btn_refresh = QPushButton("Refresh List")
        btn_refresh.clicked.connect(self._refresh_sipms)
        self._illum_check = QCheckBox("Illuminated")
        fl.addRow("SiPM:", _hbox(self._sipm_combo, btn_refresh))
        fl.addRow("", self._illum_check)
        root.addWidget(grp)

        # ---- IV sweep ----
        grp = QGroupBox("IV Sweep")
        fl  = QFormLayout(grp)
        self._iv_start = QDoubleSpinBox(); self._iv_start.setRange(-1000, 1000); self._iv_start.setSuffix(" V")
        self._iv_stop  = QDoubleSpinBox(); self._iv_stop.setRange(-1000, 1000);  self._iv_stop.setSuffix(" V")
        self._iv_step  = QDoubleSpinBox(); self._iv_step.setRange(0.001, 10);    self._iv_step.setSuffix(" V"); self._iv_step.setDecimals(3)
        self._iv_npt   = QSpinBox();       self._iv_npt.setRange(1, 100)
        self._iv_delay = QDoubleSpinBox(); self._iv_delay.setRange(0, 10);       self._iv_delay.setSuffix(" s"); self._iv_delay.setDecimals(3)
        btn_iv_cfg   = QPushButton("From Config")
        btn_run_iv   = QPushButton("Run IV Sweep")
        btn_iv_cfg.clicked.connect(self._iv_from_config)
        btn_run_iv.clicked.connect(self._run_iv)
        fl.addRow("Start:", self._iv_start)
        fl.addRow("Stop:",  self._iv_stop)
        fl.addRow("Step:",  self._iv_step)
        fl.addRow("Points per voltage:", self._iv_npt)
        fl.addRow("Settle delay:", self._iv_delay)
        fl.addRow(_hbox(btn_iv_cfg, btn_run_iv))
        root.addWidget(grp)

        # ---- Pulse acquisition ----
        grp = QGroupBox("Pulse Acquisition")
        fl  = QFormLayout(grp)
        self._p_bias  = QDoubleSpinBox(); self._p_bias.setRange(0, 1000); self._p_bias.setSuffix(" V")
        self._p_nwfm  = QSpinBox();       self._p_nwfm.setRange(1, 10_000_000); self._p_nwfm.setSingleStep(1000)
        btn_p_cfg    = QPushButton("From Config")
        btn_run_p    = QPushButton("Run Pulse")
        btn_p_cfg.clicked.connect(self._pulse_from_config)
        btn_run_p.clicked.connect(self._run_pulse)
        fl.addRow("Bias voltage:", self._p_bias)
        fl.addRow("N waveforms:",  self._p_nwfm)
        fl.addRow(_hbox(btn_p_cfg, btn_run_p))
        root.addWidget(grp)

        # ---- Log / results ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Results will appear here…")
        root.addWidget(self._log)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_sipms()
        self._iv_from_config()
        self._pulse_from_config()

    # ------------------------------------------------------------------

    def _refresh_sipms(self):
        self._sipm_combo.clear()
        sipms = self._hub.config.sipm_list() if self._hub.config._sipms else []
        for s in sipms:
            self._sipm_combo.addItem(f"SiPM {s.sipm_id} (ch {s.mux_channel})", userData=s.sipm_id)
        if not sipms:
            self._sipm_combo.addItem("(no channel map loaded)")

    def _iv_from_config(self):
        cfg = self._hub.config
        self._iv_start.setValue(cfg.iv_voltage_start)
        self._iv_stop.setValue(cfg.iv_voltage_stop)
        self._iv_step.setValue(cfg.iv_voltage_step)
        self._iv_npt.setValue(cfg.iv_n_per_point)
        self._iv_delay.setValue(getattr(cfg, "iv_delay_s", 0.1))

    def _pulse_from_config(self):
        cfg = self._hub.config
        self._p_bias.setValue(cfg.pulse_bias_v)
        self._p_nwfm.setValue(cfg.pulse_n_waveforms)

    def _selected_sipm_id(self):
        data = self._sipm_combo.currentData()
        return data  # None if no map

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _run_iv(self):
        sipm_id = self._selected_sipm_id()
        if sipm_id is None:
            self._log_line("No SiPM selected (load a channel map first)."); return

        import numpy as np
        voltages  = list(np.arange(self._iv_start.value(),
                                   self._iv_stop.value() + self._iv_step.value() * 0.5,
                                   self._iv_step.value()))
        n_per     = self._iv_npt.value()
        delay     = self._iv_delay.value()
        illuminated = self._illum_check.isChecked()
        hub       = self._hub

        self._log_line(f"Starting IV sweep on SiPM {sipm_id} ({len(voltages)} voltages, {'illuminated' if illuminated else 'dark'})…")

        def _fn():
            from daq.measurement import iv_sweep
            return iv_sweep(
                sipm_id     = sipm_id,
                instruments = hub.instruments,
                config      = hub.config,
                illuminated = illuminated,
                voltages    = voltages,
                n_per_point = n_per,
                delay_s     = delay,
            )

        def _show(result):
            lines = [f"IV sweep done — {len(result.voltages)} points"]
            for v, i, e in zip(result.voltages, result.currents, result.current_errs):
                lines.append(f"  {v:+8.3f} V  {i:+.4e} A  ±{e:.2e}")
            self._log_line("\n".join(lines))

        w = DAQWorker(_fn)
        w.finished.connect(_show)
        w.error.connect(lambda tb: self._log_line(f"ERROR: {tb.splitlines()[-1]}"))
        self._workers["iv"] = w
        w.start()

    def _run_pulse(self):
        sipm_id = self._selected_sipm_id()
        if sipm_id is None:
            self._log_line("No SiPM selected (load a channel map first)."); return

        bias_v      = self._p_bias.value()
        n_waveforms = self._p_nwfm.value()
        illuminated = self._illum_check.isChecked()
        hub         = self._hub

        self._log_line(f"Starting pulse run on SiPM {sipm_id} ({n_waveforms} waveforms, {'illuminated' if illuminated else 'dark'})…")

        def _fn():
            from daq.measurement import pulse_run
            return pulse_run(
                sipm_id     = sipm_id,
                instruments = hub.instruments,
                config      = hub.config,
                illuminated = illuminated,
                bias_v      = bias_v,
                n_waveforms = n_waveforms,
            )

        def _show(result):
            self._log_line(f"Pulse run done — {result.n_waveforms} waveforms acquired")

        w = DAQWorker(_fn)
        w.finished.connect(_show)
        w.error.connect(lambda tb: self._log_line(f"ERROR: {tb.splitlines()[-1]}"))
        self._workers["pulse"] = w
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
