"""
daq/gui/raster_tab.py

Raster Scan tab.

The tab has three sections:

  1. Spec Builder  — define one RasterSpec (channel, bias, x/y linspace, options)
  2. Spec List     — ordered list of specs to run; add, remove, clear, or auto-fill
                     from the tile channel map
  3. Run + Results — execute multi_raster(), show progress, display results,
                     save each scan's CSV

All scans run in a DAQWorker background thread.
"""

import os
import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QLabel, QDoubleSpinBox, QSpinBox, QPlainTextEdit,
    QLineEdit, QCheckBox, QFileDialog, QProgressBar, QListWidget,
    QListWidgetItem, QSplitter,
)
from PyQt5.QtCore import Qt

from .worker import DAQWorker


class RasterTab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub    = hub
        self._specs  = []      # list[RasterSpec]
        self._worker = None
        self._results = []     # list[RasterResult] from last run
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter, 1)

        # ---- top: builder + list ----
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self._build_spec_builder(), 1)
        top_layout.addWidget(self._build_spec_list(),    1)
        splitter.addWidget(top)

        # ---- bottom: run + results ----
        splitter.addWidget(self._build_run_panel())
        splitter.setSizes([420, 280])

    # ---- Spec builder ----

    def _build_spec_builder(self) -> QGroupBox:
        grp = QGroupBox("Spec Builder")
        fl  = QFormLayout(grp)

        self._b_ch    = QSpinBox();       self._b_ch.setRange(1, 96)
        self._b_bias  = QDoubleSpinBox(); self._b_bias.setRange(0, 1000); self._b_bias.setSuffix(" V"); self._b_bias.setDecimals(3)
        self._b_xs    = QDoubleSpinBox(); self._b_xs.setRange(-500, 500); self._b_xs.setSuffix(" mm"); self._b_xs.setDecimals(3)
        self._b_xe    = QDoubleSpinBox(); self._b_xe.setRange(-500, 500); self._b_xe.setSuffix(" mm"); self._b_xe.setDecimals(3)
        self._b_xn    = QSpinBox();       self._b_xn.setRange(1, 1000); self._b_xn.setValue(11)
        self._b_ys    = QDoubleSpinBox(); self._b_ys.setRange(-500, 500); self._b_ys.setSuffix(" mm"); self._b_ys.setDecimals(3)
        self._b_ye    = QDoubleSpinBox(); self._b_ye.setRange(-500, 500); self._b_ye.setSuffix(" mm"); self._b_ye.setDecimals(3)
        self._b_yn    = QSpinBox();       self._b_yn.setRange(1, 1000); self._b_yn.setValue(11)
        self._b_npt   = QSpinBox();       self._b_npt.setRange(1, 1000); self._b_npt.setValue(3)
        self._b_stl   = QDoubleSpinBox(); self._b_stl.setRange(0, 10); self._b_stl.setSuffix(" s"); self._b_stl.setDecimals(3); self._b_stl.setValue(0.05)
        self._b_denrg = QCheckBox("De-energize between moves"); self._b_denrg.setChecked(True)
        self._b_lbl   = QLineEdit()

        fl.addRow("Channel:", self._b_ch)
        fl.addRow("Bias:", self._b_bias)
        fl.addRow("X start:", self._b_xs)
        fl.addRow("X stop:", self._b_xe)
        fl.addRow("X points:", self._b_xn)
        fl.addRow("Y start:", self._b_ys)
        fl.addRow("Y stop:", self._b_ye)
        fl.addRow("Y points:", self._b_yn)
        fl.addRow("N per position:", self._b_npt)
        fl.addRow("Settle delay:", self._b_stl)
        fl.addRow("", self._b_denrg)
        fl.addRow("Label:", self._b_lbl)

        self._b_info = QLabel("")
        fl.addRow(self._b_info)

        btn_add = QPushButton("Add to List ▶")
        btn_add.clicked.connect(self._add_spec)
        fl.addRow(btn_add)

        # Update info when params change
        for w in (self._b_xn, self._b_yn, self._b_npt):
            w.valueChanged.connect(self._update_builder_info)
        self._update_builder_info()

        return grp

    # ---- Spec list ----

    def _build_spec_list(self) -> QGroupBox:
        grp = QGroupBox("Spec List")
        vl  = QVBoxLayout(grp)

        self._spec_list = QListWidget()
        self._spec_list.setMinimumWidth(280)
        vl.addWidget(self._spec_list, 1)

        # Tile helper sub-group
        tile_grp = QGroupBox("Auto-fill from Tile Map")
        tfl = QFormLayout(tile_grp)
        self._t_xw   = QDoubleSpinBox(); self._t_xw.setRange(0, 100); self._t_xw.setSuffix(" mm"); self._t_xw.setValue(8.0); self._t_xw.setDecimals(2)
        self._t_yw   = QDoubleSpinBox(); self._t_yw.setRange(0, 100); self._t_yw.setSuffix(" mm"); self._t_yw.setValue(8.0); self._t_yw.setDecimals(2)
        self._t_xn   = QSpinBox(); self._t_xn.setRange(1, 200); self._t_xn.setValue(9)
        self._t_yn   = QSpinBox(); self._t_yn.setRange(1, 200); self._t_yn.setValue(9)
        self._t_bias = QDoubleSpinBox(); self._t_bias.setRange(0, 1000); self._t_bias.setSuffix(" V"); self._t_bias.setDecimals(3)
        self._t_npt  = QSpinBox(); self._t_npt.setRange(1, 1000); self._t_npt.setValue(3)
        btn_tile = QPushButton("Append Tile Specs")
        btn_tile.clicked.connect(self._add_tile_specs)
        tfl.addRow("X width:", self._t_xw)
        tfl.addRow("Y width:", self._t_yw)
        tfl.addRow("X points:", self._t_xn)
        tfl.addRow("Y points:", self._t_yn)
        tfl.addRow("Bias:", self._t_bias)
        tfl.addRow("N/pos:", self._t_npt)
        tfl.addRow(btn_tile)
        vl.addWidget(tile_grp)

        btn_row = QHBoxLayout()
        btn_up   = QPushButton("▲")
        btn_down = QPushButton("▼")
        btn_del  = QPushButton("Remove")
        btn_clr  = QPushButton("Clear All")
        btn_up.clicked.connect(self._move_up)
        btn_down.clicked.connect(self._move_down)
        btn_del.clicked.connect(self._remove_spec)
        btn_clr.clicked.connect(self._clear_specs)
        for b in (btn_up, btn_down, btn_del, btn_clr):
            btn_row.addWidget(b)
        vl.addLayout(btn_row)

        return grp

    # ---- Run + results ----

    def _build_run_panel(self) -> QGroupBox:
        grp = QGroupBox("Run")
        vl  = QVBoxLayout(grp)

        # Save directory
        fl = QFormLayout()
        self._save_dir = QLineEdit()
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_save_dir)
        row = QHBoxLayout()
        row.addWidget(self._save_dir, 1)
        row.addWidget(btn_browse)
        fl.addRow("Save directory:", _wrap(row))
        vl.addLayout(fl)

        # Buttons
        btn_row = QHBoxLayout()
        self._btn_run  = QPushButton("Run All Specs")
        self._btn_stop = QPushButton("Stop")
        self._btn_save = QPushButton("Save Results CSV…")
        self._btn_run.clicked.connect(self._run)
        self._btn_stop.clicked.connect(self._stop)
        self._btn_save.clicked.connect(self._save_results)
        self._btn_stop.setEnabled(False)
        self._btn_save.setEnabled(False)
        for b in (self._btn_run, self._btn_stop, self._btn_save):
            btn_row.addWidget(b)
        vl.addLayout(btn_row)

        # Progress
        self._prog_spec  = QProgressBar(); self._prog_spec.setFormat("Spec %v / %m")
        self._prog_point = QProgressBar(); self._prog_point.setFormat("Point %v / %m")
        vl.addWidget(self._prog_spec)
        vl.addWidget(self._prog_point)

        # Log / results
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("Scan log and results…")
        vl.addWidget(self._log, 1)

        return grp

    # ------------------------------------------------------------------
    # Spec builder helpers
    # ------------------------------------------------------------------

    def _update_builder_info(self):
        nx  = self._b_xn.value()
        ny  = self._b_yn.value()
        npt = self._b_npt.value()
        self._b_info.setText(f"{nx}×{ny} = {nx*ny} positions  ×{npt} = {nx*ny*npt} measurements")

    def _make_spec(self):
        from daq.raster import RasterSpec
        return RasterSpec.linspace(
            channel  = self._b_ch.value(),
            bias_v   = self._b_bias.value(),
            x_start  = self._b_xs.value(),
            x_stop   = self._b_xe.value(),
            num_x    = self._b_xn.value(),
            y_start  = self._b_ys.value(),
            y_stop   = self._b_ye.value(),
            num_y    = self._b_yn.value(),
            n_per_point        = self._b_npt.value(),
            settle_s           = self._b_stl.value(),
            deenergize_between = self._b_denrg.isChecked(),
            label              = self._b_lbl.text().strip(),
        )

    def _add_spec(self):
        spec = self._make_spec()
        self._specs.append(spec)
        self._spec_list.addItem(QListWidgetItem(spec.summary()))

    def _add_tile_specs(self):
        if not self._hub.config._sipms:
            self._log_line("No channel map loaded — load one in Config tab first.")
            return
        from daq.raster import tile_raster_specs
        new_specs = tile_raster_specs(
            config             = self._hub.config,
            x_width_mm         = self._t_xw.value(),
            y_width_mm         = self._t_yw.value(),
            num_x              = self._t_xn.value(),
            num_y              = self._t_yn.value(),
            bias_v             = self._t_bias.value(),
            n_per_point        = self._t_npt.value(),
            settle_s           = self._b_stl.value(),
            deenergize_between = self._b_denrg.isChecked(),
        )
        for spec in new_specs:
            self._specs.append(spec)
            self._spec_list.addItem(QListWidgetItem(spec.summary()))
        self._log_line(f"Added {len(new_specs)} tile specs ({self._t_xn.value()}×{self._t_yn.value()} pts each)")

    def _remove_spec(self):
        row = self._spec_list.currentRow()
        if row < 0: return
        self._spec_list.takeItem(row)
        self._specs.pop(row)

    def _clear_specs(self):
        self._spec_list.clear()
        self._specs.clear()

    def _move_up(self):
        row = self._spec_list.currentRow()
        if row <= 0: return
        self._specs[row - 1], self._specs[row] = self._specs[row], self._specs[row - 1]
        item = self._spec_list.takeItem(row)
        self._spec_list.insertItem(row - 1, item)
        self._spec_list.setCurrentRow(row - 1)

    def _move_down(self):
        row = self._spec_list.currentRow()
        if row < 0 or row >= self._spec_list.count() - 1: return
        self._specs[row + 1], self._specs[row] = self._specs[row], self._specs[row + 1]
        item = self._spec_list.takeItem(row)
        self._spec_list.insertItem(row + 1, item)
        self._spec_list.setCurrentRow(row + 1)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _browse_save_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save directory")
        if d:
            self._save_dir.setText(d)

    def _log_line(self, msg: str):
        self._log.appendPlainText(msg)

    def _run(self):
        if not self._specs:
            self._log_line("No specs in list — add at least one."); return
        if self._hub.stage is None:
            self._log_line("Stage not connected."); return
        if self._hub.mux is None:
            self._log_line("MUX not connected."); return
        if self._hub.elec is None:
            self._log_line("Electrometer not connected."); return

        specs = list(self._specs)  # snapshot
        hub   = self._hub

        self._prog_spec.setMaximum(len(specs))
        self._prog_spec.setValue(0)
        self._prog_point.setMaximum(specs[0].n_points if specs else 1)
        self._prog_point.setValue(0)

        self._btn_run.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_save.setEnabled(False)
        self._results = []

        self._log_line(f"Starting {len(specs)} spec(s)…")

        def _on_progress(spec_idx, n_specs, done, total, pt):
            self._prog_spec.setValue(spec_idx)
            self._prog_point.setMaximum(total)
            self._prog_point.setValue(done)
            self._log_line(
                f"  spec {spec_idx+1}/{n_specs}  pt {done}/{total}"
                f"  ({pt.x_mm:.3f}, {pt.y_mm:.3f}) mm"
                f"  I={pt.current_a:+.4e} A  ±{pt.current_std:.2e}"
            )

        def _fn():
            from daq.raster import multi_raster
            return multi_raster(
                stage = hub.stage,
                mux   = hub.mux,
                elec  = hub.elec,
                specs = specs,
                on_progress = _on_progress,
            )

        w = DAQWorker(_fn)
        w.finished.connect(self._on_run_done)
        w.error.connect(lambda tb: self._on_run_error(tb))
        w.log_msg.connect(self._log_line)
        self._worker = w
        w.start()

    def _on_run_done(self, results):
        self._results = results
        self._prog_spec.setValue(self._prog_spec.maximum())
        self._prog_point.setValue(self._prog_point.maximum())
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_save.setEnabled(True)
        self._log_line(f"Done — {len(results)} scan(s) complete.")
        for r in results:
            pts = len(r.points)
            if pts:
                imin = r.current_a.min()
                imax = r.current_a.max()
                self._log_line(
                    f"  [{r.spec.label or f'ch{r.spec.channel}'}]"
                    f"  {pts} pts  I∈[{imin:+.3e}, {imax:+.3e}] A"
                )
        # Auto-save if directory is set
        save_dir = self._save_dir.text().strip()
        if save_dir:
            self._do_save(save_dir)

    def _on_run_error(self, tb: str):
        self._log_line(f"ERROR: {tb.splitlines()[-1]}")
        self._btn_run.setEnabled(True)
        self._btn_stop.setEnabled(False)

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._log_line("Stop requested.")
        self._btn_stop.setEnabled(False)

    def _save_results(self):
        save_dir = self._save_dir.text().strip()
        if not save_dir:
            save_dir = QFileDialog.getExistingDirectory(self, "Save directory")
            if not save_dir:
                return
            self._save_dir.setText(save_dir)
        self._do_save(save_dir)

    def _do_save(self, save_dir: str):
        os.makedirs(save_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, r in enumerate(self._results):
            lbl = r.spec.label or f"ch{r.spec.channel}"
            fname = os.path.join(save_dir, f"raster_{ts}_{i:03d}_{lbl}.csv")
            r.to_csv(fname)
            self._log_line(f"  Saved: {fname}")


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
