"""
daq/gui/config_tab.py

Magic Numbers + Config tab.

Editable fields for all experiment parameters, plus Load / Save YAML buttons
and a channel map file selector.
"""

import os
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QFileDialog, QScrollArea, QSizePolicy, QPlainTextEdit,
)
from PyQt5.QtCore import Qt
import numpy as np


class ConfigTab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub = hub
        self._build_ui()
        self._load_from_config()

    def _build_ui(self):
        # Wrap everything in a scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner  = QWidget()
        root   = QVBoxLayout(inner)
        root.setSpacing(10)
        scroll.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.addWidget(scroll)

        # ---- Load / Save ----
        btn_load = QPushButton("Load YAML…")
        btn_save = QPushButton("Save YAML…")
        btn_apply = QPushButton("Apply to Hub")
        btn_estimate = QPushButton("Estimate Run Time")
        btn_load.clicked.connect(self._load_yaml)
        btn_save.clicked.connect(self._save_yaml)
        btn_apply.clicked.connect(self._apply)
        btn_estimate.clicked.connect(self._estimate)
        top_row = QHBoxLayout()
        for b in (btn_load, btn_save, btn_apply, btn_estimate):
            top_row.addWidget(b)
        top_row.addStretch()
        root.addLayout(top_row)

        # ---- Channel map ----
        grp = QGroupBox("Channel Map")
        fl  = QFormLayout(grp)
        self._map_file = QLineEdit()
        btn_map = QPushButton("Browse…")
        btn_map.clicked.connect(self._browse_map)
        row = QHBoxLayout()
        row.addWidget(self._map_file, 1)
        row.addWidget(btn_map)
        fl.addRow("CSV file:", _wrap(row))
        btn_reload = QPushButton("Reload Map")
        btn_reload.clicked.connect(self._reload_map)
        self._map_status = QLabel("")
        fl.addRow(btn_reload, self._map_status)
        root.addWidget(grp)

        # ---- Data / logs ----
        grp = QGroupBox("Data & Logs")
        fl  = QFormLayout(grp)
        self._data_dir = QLineEdit()
        self._log_dir  = QLineEdit()
        self._log_mb   = QDoubleSpinBox(); self._log_mb.setRange(1, 500); self._log_mb.setSuffix(" MB")
        fl.addRow("Data directory:", self._data_dir)
        fl.addRow("Log directory:",  self._log_dir)
        fl.addRow("Stage log max file:", self._log_mb)
        root.addWidget(grp)

        # ---- IV sweep ----
        grp = QGroupBox("IV Sweep")
        fl  = QFormLayout(grp)
        self._iv_start = QDoubleSpinBox(); self._iv_start.setRange(-1000, 1000); self._iv_start.setSuffix(" V")
        self._iv_stop  = QDoubleSpinBox(); self._iv_stop.setRange(-1000, 1000);  self._iv_stop.setSuffix(" V")
        self._iv_step  = QDoubleSpinBox(); self._iv_step.setRange(0.001, 10);    self._iv_step.setSuffix(" V"); self._iv_step.setDecimals(3)
        self._iv_npt   = QSpinBox();       self._iv_npt.setRange(1, 100)
        self._iv_delay = QDoubleSpinBox(); self._iv_delay.setRange(0, 10);       self._iv_delay.setSuffix(" s"); self._iv_delay.setDecimals(3)
        self._iv_info  = QLabel("")
        fl.addRow("Start:", self._iv_start)
        fl.addRow("Stop:",  self._iv_stop)
        fl.addRow("Step:",  self._iv_step)
        fl.addRow("Points per voltage:", self._iv_npt)
        fl.addRow("Settle delay:", self._iv_delay)
        fl.addRow("", self._iv_info)
        for w in (self._iv_start, self._iv_stop, self._iv_step):
            w.valueChanged.connect(self._update_iv_info)
        root.addWidget(grp)

        # ---- Pulse acquisition ----
        grp = QGroupBox("Pulse Acquisition")
        fl  = QFormLayout(grp)
        self._p_bias   = QDoubleSpinBox(); self._p_bias.setRange(0, 1000); self._p_bias.setSuffix(" V")
        self._p_nwfm   = QSpinBox();       self._p_nwfm.setRange(1, 10_000_000); self._p_nwfm.setSingleStep(1000)
        self._p_pre    = QDoubleSpinBox(); self._p_pre.setRange(0.1, 1000); self._p_pre.setSuffix(" µs")
        self._p_post   = QDoubleSpinBox(); self._p_post.setRange(0.1, 1000); self._p_post.setSuffix(" µs")
        self._p_thr    = QDoubleSpinBox(); self._p_thr.setRange(0.001, 10); self._p_thr.setSuffix(" V"); self._p_thr.setDecimals(4)
        fl.addRow("Bias voltage:", self._p_bias)
        fl.addRow("N waveforms:",  self._p_nwfm)
        fl.addRow("Pre-trigger:",  self._p_pre)
        fl.addRow("Post-trigger:", self._p_post)
        fl.addRow("Threshold:",    self._p_thr)
        root.addWidget(grp)

        # ---- Temperature schedule ----
        grp = QGroupBox("Temperature Schedule")
        fl  = QFormLayout(grp)
        self._temps       = QLineEdit()
        self._illum_temps = QLineEdit()
        self._temp_tol    = QDoubleSpinBox(); self._temp_tol.setRange(0.01, 5); self._temp_tol.setSuffix(" K"); self._temp_tol.setDecimals(2)
        self._temp_stable = QDoubleSpinBox(); self._temp_stable.setRange(10, 7200); self._temp_stable.setSuffix(" s")
        fl.addRow("Temperatures (K, comma-sep):", self._temps)
        fl.addRow("Illuminated temps:", self._illum_temps)
        fl.addRow("Tolerance:", self._temp_tol)
        fl.addRow("Stable hold:", self._temp_stable)
        root.addWidget(grp)

        # ---- Stage magic numbers ----
        grp = QGroupBox("Stage")
        fl  = QFormLayout(grp)
        self._spmx   = QDoubleSpinBox(); self._spmx.setRange(1, 10000); self._spmx.setSuffix(" steps/mm")
        self._spmy   = QDoubleSpinBox(); self._spmy.setRange(1, 10000); self._spmy.setSuffix(" steps/mm")
        self._velx   = QDoubleSpinBox(); self._velx.setRange(1, 20000); self._velx.setSuffix(" steps/s")
        self._vely   = QDoubleSpinBox(); self._vely.setRange(1, 20000); self._vely.setSuffix(" steps/s")
        fl.addRow("Steps/mm X:", self._spmx)
        fl.addRow("Steps/mm Y:", self._spmy)
        fl.addRow("Velocity X:", self._velx)
        fl.addRow("Velocity Y:", self._vely)
        root.addWidget(grp)

        # ---- Flux ----
        grp = QGroupBox("Flux Calibration")
        fl  = QFormLayout(grp)
        self._flux_int = QSpinBox(); self._flux_int.setRange(1, 96)
        fl.addRow("Check every N SiPMs:", self._flux_int)
        root.addWidget(grp)

        # ---- Estimate output ----
        self._est_out = QPlainTextEdit()
        self._est_out.setReadOnly(True)
        self._est_out.setMaximumHeight(200)
        self._est_out.setPlaceholderText("Time estimate will appear here…")
        root.addWidget(self._est_out)

        root.addStretch()

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load_from_config(self):
        cfg = self._hub.config
        self._map_file.setText(cfg.channel_map_file)
        self._data_dir.setText(cfg.data_dir)
        self._log_dir.setText(cfg.log_dir)
        self._log_mb.setValue(cfg.stage_log_max_mb)
        self._iv_start.setValue(cfg.iv_voltage_start)
        self._iv_stop.setValue(cfg.iv_voltage_stop)
        self._iv_step.setValue(cfg.iv_voltage_step)
        self._iv_npt.setValue(cfg.iv_n_per_point)
        self._iv_delay.setValue(getattr(cfg, "iv_delay_s", 0.1))
        self._p_bias.setValue(cfg.pulse_bias_v)
        self._p_nwfm.setValue(cfg.pulse_n_waveforms)
        self._p_pre.setValue(cfg.pulse_pre_us)
        self._p_post.setValue(cfg.pulse_post_us)
        self._p_thr.setValue(cfg.pulse_threshold_v)
        self._temps.setText(", ".join(str(t) for t in cfg.temperatures_K))
        self._illum_temps.setText(", ".join(str(t) for t in cfg.illuminated_temperatures_K))
        self._temp_tol.setValue(cfg.temp_tolerance_K)
        self._temp_stable.setValue(cfg.temp_stable_s)
        self._spmx.setValue(cfg.stage_steps_per_mm_x)
        self._spmy.setValue(cfg.stage_steps_per_mm_y)
        self._velx.setValue(cfg.stage_velocity_x)
        self._vely.setValue(cfg.stage_velocity_y)
        self._flux_int.setValue(cfg.flux_check_interval)
        self._update_iv_info()

    def _apply(self):
        cfg = self._hub.config
        cfg.channel_map_file        = self._map_file.text().strip()
        cfg.data_dir                = self._data_dir.text().strip()
        cfg.log_dir                 = self._log_dir.text().strip()
        cfg.stage_log_max_mb        = self._log_mb.value()
        cfg.iv_voltage_start        = self._iv_start.value()
        cfg.iv_voltage_stop         = self._iv_stop.value()
        cfg.iv_voltage_step         = self._iv_step.value()
        cfg.iv_n_per_point          = self._iv_npt.value()
        cfg.iv_delay_s              = self._iv_delay.value()
        cfg.pulse_bias_v            = self._p_bias.value()
        cfg.pulse_n_waveforms       = self._p_nwfm.value()
        cfg.pulse_pre_us            = self._p_pre.value()
        cfg.pulse_post_us           = self._p_post.value()
        cfg.pulse_threshold_v       = self._p_thr.value()
        cfg.temperatures_K          = [float(t.strip()) for t in self._temps.text().split(",") if t.strip()]
        cfg.illuminated_temperatures_K = [float(t.strip()) for t in self._illum_temps.text().split(",") if t.strip()]
        cfg.temp_tolerance_K        = self._temp_tol.value()
        cfg.temp_stable_s           = self._temp_stable.value()
        cfg.stage_steps_per_mm_x    = self._spmx.value()
        cfg.stage_steps_per_mm_y    = self._spmy.value()
        cfg.stage_velocity_x        = self._velx.value()
        cfg.stage_velocity_y        = self._vely.value()
        cfg.flux_check_interval     = self._flux_int.value()

    def _load_yaml(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Config", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        from daq.config import ExperimentConfig
        self._hub.config = ExperimentConfig.from_yaml(path)
        self._load_from_config()

    def _save_yaml(self):
        self._apply()
        path, _ = QFileDialog.getSaveFileName(self, "Save Config", "run_config.yaml", "YAML (*.yaml *.yml)")
        if not path:
            return
        self._hub.config.to_yaml(path)

    def _browse_map(self):
        path, _ = QFileDialog.getOpenFileName(self, "Channel Map CSV", "", "CSV (*.csv)")
        if path:
            self._map_file.setText(path)

    def _reload_map(self):
        path = self._map_file.text().strip()
        if not os.path.exists(path):
            self._map_status.setText(f"File not found: {path}")
            return
        self._hub.config.load_channel_map(path)
        n = len(self._hub.config.sipm_list())
        self._map_status.setText(f"{n} SiPMs loaded")

    def _update_iv_info(self):
        try:
            v = np.arange(self._iv_start.value(),
                          self._iv_stop.value() + self._iv_step.value() * 0.5,
                          self._iv_step.value())
            self._iv_info.setText(f"{len(v)} voltage points")
        except Exception:
            pass

    def _estimate(self):
        self._apply()
        if not self._hub.config._sipms:
            self._est_out.setPlainText("Load a channel map first.")
            return
        from daq.estimate_time import estimate
        import io, contextlib
        est = estimate(self._hub.config)
        lines = []
        for label, s in est.rows:
            h, rem = divmod(int(s), 3600)
            m = rem // 60
            lines.append(f"  {label:<55}  {h:2d}h{m:02d}m")
        tot = est.total_s
        h, rem = divmod(int(tot), 3600)
        m = rem // 60
        lines.append(f"\n  {'TOTAL':<55}  {h:2d}h{m:02d}m  ({tot/3600:.2f} h)")
        self._est_out.setPlainText("\n".join(lines))


# ---------------------------------------------------------------------------

def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
