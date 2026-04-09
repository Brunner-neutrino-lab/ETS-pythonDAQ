"""
daq/gui/alignment_tab.py

Coordinate Alignment tab.

Workflow
--------
1. Pick a reference SiPM (usually a corner device).
2. Run a quick line scan in X, Y, or a small box centred on its nominal
   position (using the current offset, so re-running after a partial
   alignment is safe).
3. The tab computes the weighted centroid of the response and displays the
   found stage position.
4. Click "Set as Origin" — config.set_origin() is called, updating the
   offset so that reference SiPM maps to the found position.  All other
   SiPM and named positions shift by the same amount.

Manual mode
-----------
If you already know the actual stage position of the reference SiPM
(e.g. from a previous alignment or a ruler measurement), you can skip
the scan and enter the coordinates directly, then click "Set as Origin".

Reset
-----
"Clear Offset" resets the offset to (0, 0), i.e. channel map == stage.
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QComboBox, QFrame,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from .worker import DAQWorker


class AlignmentTab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub    = hub
        self._worker = None
        self._last_centroid = None   # (x_mm, y_mm) from most recent scan
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- Current offset display ----
        grp = QGroupBox("Current Coordinate Offset")
        fl  = QFormLayout(grp)
        self._lbl_ox  = QLabel("0.000 mm")
        self._lbl_oy  = QLabel("0.000 mm")
        font = QFont(); font.setBold(True)
        self._lbl_ox.setFont(font)
        self._lbl_oy.setFont(font)
        btn_clear = QPushButton("Clear Offset (reset to 0, 0)")
        btn_clear.clicked.connect(self._clear_offset)
        fl.addRow("Offset X:", self._lbl_ox)
        fl.addRow("Offset Y:", self._lbl_oy)
        fl.addRow(btn_clear)
        root.addWidget(grp)

        # ---- Reference SiPM ----
        grp = QGroupBox("Reference SiPM")
        fl  = QFormLayout(grp)
        self._ref_combo  = QComboBox(); self._ref_combo.setMinimumWidth(220)
        self._lbl_nom_x  = QLabel("—")
        self._lbl_nom_y  = QLabel("—")
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self._refresh_sipms)
        self._ref_combo.currentIndexChanged.connect(self._update_nominal_labels)
        fl.addRow("SiPM:", _hbox(self._ref_combo, btn_refresh))
        fl.addRow("Nominal X (channel map):", self._lbl_nom_x)
        fl.addRow("Nominal Y (channel map):", self._lbl_nom_y)
        root.addWidget(grp)

        # ---- Scan parameters ----
        grp = QGroupBox("Alignment Scan")
        fl  = QFormLayout(grp)
        self._scan_type = QComboBox()
        self._scan_type.addItems(["Line X", "Line Y", "Box (2D)"])
        self._scan_width  = QDoubleSpinBox(); self._scan_width.setRange(0.1, 50); self._scan_width.setValue(8.0); self._scan_width.setSuffix(" mm"); self._scan_width.setDecimals(2)
        self._scan_n      = QSpinBox();       self._scan_n.setRange(3, 200); self._scan_n.setValue(17)
        self._scan_bias   = QDoubleSpinBox(); self._scan_bias.setRange(0, 1000); self._scan_bias.setValue(30.0); self._scan_bias.setSuffix(" V"); self._scan_bias.setDecimals(3)
        self._scan_npt    = QSpinBox();       self._scan_npt.setRange(1, 100); self._scan_npt.setValue(3)
        self._scan_settle = QDoubleSpinBox(); self._scan_settle.setRange(0, 5); self._scan_settle.setValue(0.05); self._scan_settle.setSuffix(" s"); self._scan_settle.setDecimals(3)
        btn_scan = QPushButton("Run Alignment Scan")
        btn_scan.clicked.connect(self._run_scan)
        fl.addRow("Scan type:", self._scan_type)
        fl.addRow("Scan width:", self._scan_width)
        fl.addRow("N points (per axis):", self._scan_n)
        fl.addRow("Bias:", self._scan_bias)
        fl.addRow("N meas/position:", self._scan_npt)
        fl.addRow("Settle delay:", self._scan_settle)
        fl.addRow(btn_scan)
        root.addWidget(grp)

        # ---- Result + set origin ----
        grp = QGroupBox("Found Position / Set Origin")
        fl  = QFormLayout(grp)

        self._lbl_found_x = QLabel("—")
        self._lbl_found_y = QLabel("—")
        self._lbl_found_x.setFont(font)
        self._lbl_found_y.setFont(font)

        # Manual entry (override centroid or skip scan entirely)
        self._man_x = QDoubleSpinBox(); self._man_x.setRange(-500, 500); self._man_x.setSuffix(" mm"); self._man_x.setDecimals(4)
        self._man_y = QDoubleSpinBox(); self._man_y.setRange(-500, 500); self._man_y.setSuffix(" mm"); self._man_y.setDecimals(4)

        btn_use_centroid = QPushButton("Use Centroid")
        btn_use_centroid.setToolTip("Copy centroid result into manual fields")
        btn_use_centroid.clicked.connect(self._copy_centroid_to_manual)

        btn_set_origin = QPushButton("Set as Origin for Selected SiPM")
        btn_set_origin.clicked.connect(self._set_origin)

        fl.addRow("Centroid X:", self._lbl_found_x)
        fl.addRow("Centroid Y:", self._lbl_found_y)

        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setFrameShadow(QFrame.Sunken)
        fl.addRow(line)

        fl.addRow("Actual X (manual):", self._man_x)
        fl.addRow("Actual Y (manual):", self._man_y)
        fl.addRow(_hbox(btn_use_centroid, btn_set_origin))
        root.addWidget(grp)

        # ---- Log ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)
        self._log.setPlaceholderText("Alignment log…")
        root.addWidget(self._log)

        root.addStretch()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_sipms()
        self._update_offset_display()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _update_offset_display(self):
        cfg = self._hub.config
        self._lbl_ox.setText(f"{cfg.position_offset_x_mm:+.4f} mm")
        self._lbl_oy.setText(f"{cfg.position_offset_y_mm:+.4f} mm")

    def _refresh_sipms(self):
        self._ref_combo.blockSignals(True)
        self._ref_combo.clear()
        sipms = self._hub.config.sipm_list() if self._hub.config._sipms else []
        for s in sipms:
            self._ref_combo.addItem(
                f"SiPM {s.sipm_id}  (ch {s.mux_channel}  nom {s.x_mm:.1f}, {s.y_mm:.1f} mm)",
                userData=s.sipm_id
            )
        self._ref_combo.blockSignals(False)
        self._update_nominal_labels()

    def _update_nominal_labels(self):
        sipm_id = self._ref_combo.currentData()
        if sipm_id is None:
            self._lbl_nom_x.setText("—")
            self._lbl_nom_y.setText("—")
            return
        x, y = self._hub.config.sipm_position_raw(sipm_id)
        self._lbl_nom_x.setText(f"{x:.4f} mm")
        self._lbl_nom_y.setText(f"{y:.4f} mm")

    def _selected_sipm(self):
        sipm_id = self._ref_combo.currentData()
        if sipm_id is None:
            self._log_line("No SiPM selected (load a channel map first).")
        return sipm_id

    # ------------------------------------------------------------------
    # Alignment scan
    # ------------------------------------------------------------------

    def _run_scan(self):
        sipm_id = self._selected_sipm()
        if sipm_id is None: return
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        if self._hub.mux is None:
            self._log_line("MUX not connected."); return
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return

        # Build spec centred on the current (offset-corrected) nominal position
        cx, cy   = self._hub.config.sipm_position(sipm_id)
        ch       = self._hub.config.sipm_channel(sipm_id)
        bias     = self._scan_bias.value()
        half     = self._scan_width.value() / 2.0
        n        = self._scan_n.value()
        npt      = self._scan_npt.value()
        settle   = self._scan_settle.value()
        scan_type = self._scan_type.currentText()

        from daq.raster import RasterSpec
        if scan_type == "Line X":
            spec = RasterSpec.linspace(ch, bias,
                                       cx - half, cx + half, n,
                                       cy, cy, 1,
                                       n_per_point=npt, settle_s=settle,
                                       label=f"align_X_SiPM{sipm_id}")
        elif scan_type == "Line Y":
            spec = RasterSpec.linspace(ch, bias,
                                       cx, cx, 1,
                                       cy - half, cy + half, n,
                                       n_per_point=npt, settle_s=settle,
                                       label=f"align_Y_SiPM{sipm_id}")
        else:  # Box
            spec = RasterSpec.linspace(ch, bias,
                                       cx - half, cx + half, n,
                                       cy - half, cy + half, n,
                                       n_per_point=npt, settle_s=settle,
                                       label=f"align_box_SiPM{sipm_id}")

        self._log_line(f"Starting {scan_type} alignment scan on SiPM {sipm_id} "
                       f"centred at ({cx:.3f}, {cy:.3f}) mm…")

        hub = self._hub

        def _on_progress(done, total, pt):
            if done % max(1, total // 5) == 0:
                self._log_line(
                    f"  {done}/{total}  ({pt.x_mm:.3f}, {pt.y_mm:.3f})  "
                    f"I={pt.current_a:+.3e} A"
                )

        def _fn():
            from daq.raster import raster_scan
            return raster_scan(hub.stage, hub.mux, hub.elec, spec,
                               on_progress=_on_progress)

        w = DAQWorker(_fn)
        w.finished.connect(self._on_scan_done)
        w.error.connect(lambda tb: self._log_line(f"ERROR: {tb.splitlines()[-1]}"))
        self._worker = w
        w.start()

    def _on_scan_done(self, result):
        from daq.raster import centroid_1d
        try:
            cx, cy = centroid_1d(result)
        except ValueError as e:
            self._log_line(f"Centroid failed: {e}")
            return

        self._last_centroid = (cx, cy)
        self._lbl_found_x.setText(f"{cx:+.4f} mm")
        self._lbl_found_y.setText(f"{cy:+.4f} mm")
        self._log_line(f"Centroid: ({cx:+.4f}, {cy:+.4f}) mm")

    def _copy_centroid_to_manual(self):
        if self._last_centroid is None:
            self._log_line("No centroid computed yet — run a scan first.")
            return
        cx, cy = self._last_centroid
        self._man_x.setValue(cx)
        self._man_y.setValue(cy)

    # ------------------------------------------------------------------
    # Set origin
    # ------------------------------------------------------------------

    def _set_origin(self):
        sipm_id = self._selected_sipm()
        if sipm_id is None: return

        actual_x = self._man_x.value()
        actual_y = self._man_y.value()

        old_ox = self._hub.config.position_offset_x_mm
        old_oy = self._hub.config.position_offset_y_mm

        self._hub.config.set_origin(sipm_id, actual_x, actual_y)

        new_ox = self._hub.config.position_offset_x_mm
        new_oy = self._hub.config.position_offset_y_mm

        self._update_offset_display()
        self._log_line(
            f"Origin set using SiPM {sipm_id} @ ({actual_x:+.4f}, {actual_y:+.4f}) mm\n"
            f"  Offset: ({old_ox:+.4f}, {old_oy:+.4f}) -> ({new_ox:+.4f}, {new_oy:+.4f}) mm\n"
            f"  All positions shifted by ({new_ox - old_ox:+.4f}, {new_oy - old_oy:+.4f}) mm"
        )

    def _clear_offset(self):
        old_ox = self._hub.config.position_offset_x_mm
        old_oy = self._hub.config.position_offset_y_mm
        self._hub.config.clear_offset()
        self._update_offset_display()
        self._log_line(
            f"Offset cleared  (was {old_ox:+.4f}, {old_oy:+.4f} mm)"
        )


# ---------------------------------------------------------------------------

def _hbox(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for wgt in widgets:
        h.addWidget(wgt)
    h.addStretch()
    return w
