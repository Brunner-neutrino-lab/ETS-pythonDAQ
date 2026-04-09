"""
daq/gui/level1_tab.py

Level 1 — Primitives tab.

Manual single-instrument operations:
  Stage: move to XY, home, energize/de-energize, read position
  MUX:   select channel, zero all
  Electrometer: set bias, ramp bias, bias off, read current
  Digitizer: acquire N waveforms
  Flux monitor: read current
  Temperature: read temperature
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QSizePolicy,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class Level1Tab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub     = hub
        self._workers = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- Stage ----
        grp = QGroupBox("Stage (Level 1)")
        fl  = QFormLayout(grp)
        self._stg_x    = QDoubleSpinBox(); self._stg_x.setRange(-500, 500); self._stg_x.setSuffix(" mm"); self._stg_x.setDecimals(3)
        self._stg_y    = QDoubleSpinBox(); self._stg_y.setRange(-500, 500); self._stg_y.setSuffix(" mm"); self._stg_y.setDecimals(3)
        self._stg_pos  = QLabel("—")
        btn_move       = QPushButton("Move To")
        btn_home       = QPushButton("Home")
        btn_enrg       = QPushButton("Energize")
        btn_denrg      = QPushButton("De-energize")
        btn_read_pos   = QPushButton("Read Position")
        btn_move.clicked.connect(self._stage_move)
        btn_home.clicked.connect(self._stage_home)
        btn_enrg.clicked.connect(self._stage_energize)
        btn_denrg.clicked.connect(self._stage_deenergize)
        btn_read_pos.clicked.connect(self._stage_read_pos)
        fl.addRow("X:", self._stg_x)
        fl.addRow("Y:", self._stg_y)
        fl.addRow(_hbox(btn_move, btn_home, btn_enrg, btn_denrg, btn_read_pos))
        fl.addRow("Position:", self._stg_pos)
        root.addWidget(grp)

        # ---- MUX ----
        grp = QGroupBox("MUX (Level 1)")
        fl  = QFormLayout(grp)
        self._mux_ch  = QSpinBox(); self._mux_ch.setRange(1, 96)
        self._mux_st  = QLabel("—")
        btn_sel  = QPushButton("Select Channel")
        btn_zero = QPushButton("Zero All")
        btn_sel.clicked.connect(self._mux_select)
        btn_zero.clicked.connect(self._mux_zero)
        fl.addRow("Channel:", self._mux_ch)
        fl.addRow(_hbox(btn_sel, btn_zero))
        fl.addRow("Status:", self._mux_st)
        root.addWidget(grp)

        # ---- Electrometer ----
        grp = QGroupBox("Electrometer (Level 1)")
        fl  = QFormLayout(grp)
        self._elec_v       = QDoubleSpinBox(); self._elec_v.setRange(0, 1000); self._elec_v.setSuffix(" V"); self._elec_v.setDecimals(3)
        self._elec_ramp_v  = QDoubleSpinBox(); self._elec_ramp_v.setRange(0, 1000); self._elec_ramp_v.setSuffix(" V"); self._elec_ramp_v.setDecimals(3)
        self._elec_step_v  = QDoubleSpinBox(); self._elec_step_v.setRange(0.001, 10); self._elec_step_v.setSuffix(" V"); self._elec_step_v.setValue(1.0); self._elec_step_v.setDecimals(3)
        self._elec_curr    = QLabel("—")
        btn_set_bias  = QPushButton("Set Bias")
        btn_ramp      = QPushButton("Ramp Bias")
        btn_bias_off  = QPushButton("Bias Off")
        btn_read_curr = QPushButton("Read Current")
        btn_set_bias.clicked.connect(self._elec_set_bias)
        btn_ramp.clicked.connect(self._elec_ramp)
        btn_bias_off.clicked.connect(self._elec_bias_off)
        btn_read_curr.clicked.connect(self._elec_read_curr)
        fl.addRow("Bias voltage:", self._elec_v)
        fl.addRow(_hbox(btn_set_bias, btn_bias_off, btn_read_curr))
        fl.addRow("Ramp target:", self._elec_ramp_v)
        fl.addRow("Ramp step:", self._elec_step_v)
        fl.addRow(_hbox(btn_ramp))
        fl.addRow("Current:", self._elec_curr)
        root.addWidget(grp)

        # ---- Digitizer ----
        grp = QGroupBox("Digitizer (Level 1)")
        fl  = QFormLayout(grp)
        self._dig_n    = QSpinBox(); self._dig_n.setRange(1, 1_000_000); self._dig_n.setValue(1000); self._dig_n.setSingleStep(1000)
        self._dig_info = QLabel("—")
        btn_acq = QPushButton("Acquire Pulses")
        btn_acq.clicked.connect(self._dig_acquire)
        fl.addRow("N waveforms:", self._dig_n)
        fl.addRow(_hbox(btn_acq))
        fl.addRow("Result:", self._dig_info)
        root.addWidget(grp)

        # ---- Flux + Temperature ----
        grp = QGroupBox("Flux / Temperature (Level 1)")
        fl  = QFormLayout(grp)
        self._flux_n   = QSpinBox(); self._flux_n.setRange(1, 20); self._flux_n.setValue(3)
        self._flux_val = QLabel("—")
        self._temp_val = QLabel("—")
        btn_flux = QPushButton("Read Flux")
        btn_temp = QPushButton("Read Temperature")
        btn_flux.clicked.connect(self._read_flux)
        btn_temp.clicked.connect(self._read_temp)
        fl.addRow("Flux N samples:", self._flux_n)
        fl.addRow(_hbox(btn_flux))
        fl.addRow("Flux current:", self._flux_val)
        fl.addRow(_hbox(btn_temp))
        fl.addRow("Temperature:", self._temp_val)
        root.addWidget(grp)

        # ---- Log ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(180)
        self._log.setPlaceholderText("Operation log…")
        root.addWidget(self._log)

        root.addStretch()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _run(self, key, fn, on_done=None):
        w = DAQWorker(fn)
        w.finished.connect(lambda result: self._on_done(key, result, on_done))
        w.error.connect(lambda tb: self._log_line(f"ERROR [{key}]: {tb.splitlines()[-1]}"))
        self._workers[key] = w
        w.start()

    def _on_done(self, key, result, callback):
        if callback:
            callback(result)
        else:
            self._log_line(f"[{key}] done: {result}")

    # ------------------------------------------------------------------
    # Stage
    # ------------------------------------------------------------------

    def _stage_move(self):
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        x, y = self._stg_x.value(), self._stg_y.value()
        def _fn():
            from daq.primitives import move_stage
            move_stage(self._hub.stage, x, y, deenergize_after=False)
            return f"moved to ({x:.3f}, {y:.3f}) mm"
        self._run("stg_move", _fn, lambda r: self._log_line(r))

    def _stage_home(self):
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        def _fn():
            from daq.primitives import home_stage
            home_stage(self._hub.stage)
            return "homed"
        self._run("stg_home", _fn, lambda r: self._log_line(f"Stage {r}"))

    def _stage_energize(self):
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        def _fn():
            from daq.primitives import energize_stage
            energize_stage(self._hub.stage)
            return "energized"
        self._run("stg_enrg", _fn, lambda r: self._log_line(f"Stage {r}"))

    def _stage_deenergize(self):
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        def _fn():
            from daq.primitives import deenergize_stage
            deenergize_stage(self._hub.stage)
            return "de-energized"
        self._run("stg_denrg", _fn, lambda r: self._log_line(f"Stage {r}"))

    def _stage_read_pos(self):
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        def _fn():
            from daq.primitives import stage_position
            return stage_position(self._hub.stage)
        def _show(pos):
            txt = f"({pos[0]:.3f}, {pos[1]:.3f}) mm"
            self._stg_pos.setText(txt)
            self._log_line(f"Stage position: {txt}")
        self._run("stg_pos", _fn, _show)

    # ------------------------------------------------------------------
    # MUX
    # ------------------------------------------------------------------

    def _mux_select(self):
        if self._hub.mux is None:
            self._log_line("MUX not connected."); return
        ch = self._mux_ch.value()
        def _fn():
            from daq.primitives import select_channel
            select_channel(self._hub.mux, ch)
            return f"channel {ch} selected"
        def _show(msg):
            self._mux_st.setText(msg)
            self._log_line(f"MUX: {msg}")
        self._run("mux_sel", _fn, _show)

    def _mux_zero(self):
        if self._hub.mux is None:
            self._log_line("MUX not connected."); return
        def _fn():
            from daq.primitives import zero_channels
            zero_channels(self._hub.mux)
            return "all channels zeroed"
        def _show(msg):
            self._mux_st.setText(msg)
            self._log_line(f"MUX: {msg}")
        self._run("mux_zero", _fn, _show)

    # ------------------------------------------------------------------
    # Electrometer
    # ------------------------------------------------------------------

    def _elec_set_bias(self):
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return
        v = self._elec_v.value()
        def _fn():
            from daq.primitives import set_bias
            set_bias(self._hub.elec, v)
            return f"bias set to {v:.3f} V"
        self._run("elec_set", _fn, lambda r: self._log_line(r))

    def _elec_ramp(self):
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return
        target = self._elec_ramp_v.value()
        step   = self._elec_step_v.value()
        def _fn():
            from daq.primitives import ramp_bias
            ramp_bias(self._hub.elec, target, step)
            return f"ramped to {target:.3f} V"
        self._run("elec_ramp", _fn, lambda r: self._log_line(r))

    def _elec_bias_off(self):
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return
        def _fn():
            from daq.primitives import bias_off
            bias_off(self._hub.elec)
            return "bias off"
        self._run("elec_off", _fn, lambda r: self._log_line(r))

    def _elec_read_curr(self):
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return
        def _fn():
            from daq.primitives import measure_current
            return measure_current(self._hub.elec)
        def _show(val):
            txt = f"{val:.4e} A"
            self._elec_curr.setText(txt)
            self._log_line(f"Current: {txt}")
        self._run("elec_curr", _fn, _show)

    # ------------------------------------------------------------------
    # Digitizer
    # ------------------------------------------------------------------

    def _dig_acquire(self):
        if self._hub.dig is None:
            self._log_line("Digitizer not connected."); return
        n = self._dig_n.value()
        def _fn():
            from daq.primitives import acquire_pulses
            return acquire_pulses(self._hub.dig, n)
        def _show(result):
            msg = f"acquired {result.n_waveforms} waveforms"
            self._dig_info.setText(msg)
            self._log_line(msg)
        self._run("dig_acq", _fn, _show)

    # ------------------------------------------------------------------
    # Flux / Temperature
    # ------------------------------------------------------------------

    def _read_flux(self):
        if self._hub.k6485 is None:
            self._log_line("Flux monitor not connected."); return
        n = self._flux_n.value()
        def _fn():
            from daq.primitives import read_flux
            return read_flux(self._hub.k6485, n)
        def _show(val):
            txt = f"{val:.4e} A"
            self._flux_val.setText(txt)
            self._log_line(f"Flux: {txt}")
        self._run("flux", _fn, _show)

    def _read_temp(self):
        if self._hub.sc is None:
            self._log_line("Slow control not connected."); return
        def _fn():
            from daq.primitives import read_temperature
            return read_temperature(self._hub.sc)
        def _show(val):
            txt = f"{val:.3f} K"
            self._temp_val.setText(txt)
            self._log_line(f"Temperature: {txt}")
        self._run("temp", _fn, _show)


# ---------------------------------------------------------------------------

def _hbox(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for wgt in widgets:
        h.addWidget(wgt)
    h.addStretch()
    return w
