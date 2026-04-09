"""
daq/gui/connections_tab.py

Connections tab — connect/disconnect each instrument individually.
Shows a status indicator (green/red) and the IDN response for each.
"""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QLabel, QLineEdit, QComboBox, QFormLayout, QSizePolicy,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor

from .worker import DAQWorker


def _status_label(text="disconnected") -> QLabel:
    lbl = QLabel(text)
    lbl.setMinimumWidth(320)
    lbl.setWordWrap(True)
    _set_status_style(lbl, text)
    return lbl


def _set_status_style(lbl: QLabel, text: str):
    ok = text.startswith("OK")
    lbl.setText(text)
    colour = "#2d7a2d" if ok else "#8b0000"
    lbl.setStyleSheet(f"color: {colour}; font-weight: bold;")


def _make_row(label_text: str, widget: QWidget, btn_connect: QPushButton,
              btn_disconnect: QPushButton, status_lbl: QLabel) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(QLabel(label_text))
    row.addWidget(widget, 1)
    row.addWidget(btn_connect)
    row.addWidget(btn_disconnect)
    row.addWidget(status_lbl, 1)
    return row


class ConnectionsTab(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self._hub     = hub
        self._workers = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)

        # ---- B2987B ----
        grp = QGroupBox("B2987B Electrometer")
        fl  = QFormLayout(grp)
        self._visa_elec = QLineEdit(self._hub.config.b2987b_visa)
        self._st_elec   = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_elec)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_elec)
        fl.addRow("VISA:", self._visa_elec)
        fl.addRow(_hbox(btn_c, btn_d, self._st_elec))
        root.addWidget(grp)

        # ---- Digitizer ----
        grp = QGroupBox("Digitizer")
        fl  = QFormLayout(grp)
        self._dig_type = QComboBox()
        self._dig_type.addItems(["rto2024", "vx2740"])
        self._dig_type.setCurrentText(self._hub.config.digitizer_type)
        self._dig_addr = QLineEdit(self._hub.config.digitizer_address)
        self._st_dig   = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_dig)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_dig)
        fl.addRow("Type:", self._dig_type)
        fl.addRow("IP address:", self._dig_addr)
        fl.addRow(_hbox(btn_c, btn_d, self._st_dig))
        root.addWidget(grp)

        # ---- MUX ----
        grp = QGroupBox("96-ch IV-Pulse MUX")
        fl  = QFormLayout(grp)
        self._mux_port = QLineEdit(self._hub.config.mux_port)
        self._st_mux   = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_mux)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_mux)
        fl.addRow("Serial port:", self._mux_port)
        fl.addRow(_hbox(btn_c, btn_d, self._st_mux))
        root.addWidget(grp)

        # ---- K6485 ----
        grp = QGroupBox("Keithley 6485 Flux Monitor")
        fl  = QFormLayout(grp)
        self._k6485_port = QLineEdit(self._hub.config.k6485_port)
        self._st_k6485   = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_k6485)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_k6485)
        fl.addRow("Serial port / VISA:", self._k6485_port)
        fl.addRow(_hbox(btn_c, btn_d, self._st_k6485))
        root.addWidget(grp)

        # ---- Stage ----
        grp = QGroupBox("Phidget XY Stage")
        fl  = QFormLayout(grp)
        self._ser_x   = QLineEdit(str(self._hub.config.stage_serial_x))
        self._ser_y   = QLineEdit(str(self._hub.config.stage_serial_y))
        self._ser_lim = QLineEdit(str(self._hub.config.stage_serial_limit))
        self._st_stg  = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_stage)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_stage)
        btn_h = QPushButton("Home");       btn_h.clicked.connect(self._home_stage)
        fl.addRow("Serial X:", self._ser_x)
        fl.addRow("Serial Y:", self._ser_y)
        fl.addRow("Serial Limit Hub:", self._ser_lim)
        fl.addRow(_hbox(btn_c, btn_d, btn_h, self._st_stg))
        root.addWidget(grp)

        # ---- Slow control ----
        grp = QGroupBox("Slow Control (InfluxDB)")
        fl  = QFormLayout(grp)
        self._sc_url   = QLineEdit(self._hub.config.influxdb_url)
        self._sc_org   = QLineEdit(self._hub.config.influxdb_org)
        self._sc_token = QLineEdit(self._hub.config.influxdb_token)
        self._sc_token.setEchoMode(QLineEdit.Password)
        self._sc_field = QLineEdit(self._hub.config.influxdb_rtd_field)
        self._st_sc    = _status_label()
        btn_c = QPushButton("Connect");    btn_c.clicked.connect(self._connect_sc)
        btn_d = QPushButton("Disconnect"); btn_d.clicked.connect(self._disconnect_sc)
        fl.addRow("URL:", self._sc_url)
        fl.addRow("Org:", self._sc_org)
        fl.addRow("Token:", self._sc_token)
        fl.addRow("RTD field:", self._sc_field)
        fl.addRow(_hbox(btn_c, btn_d, self._st_sc))
        root.addWidget(grp)

        root.addStretch()

    # ------------------------------------------------------------------
    # Connect/disconnect helpers (each runs in a worker thread)
    # ------------------------------------------------------------------

    def _run(self, key: str, fn, status_lbl: QLabel):
        """Run fn() in background, update status_lbl on completion."""
        def task():
            fn()
            return self._hub.status[key]

        w = DAQWorker(task)
        w.finished.connect(lambda txt: _set_status_style(status_lbl, txt))
        w.error.connect(lambda tb: _set_status_style(status_lbl, f"ERROR: {tb.splitlines()[-1]}"))
        self._workers[key] = w
        w.start()

    def _push_config(self):
        """Push text field values back into hub.config before connecting."""
        cfg = self._hub.config
        cfg.b2987b_visa         = self._visa_elec.text().strip()
        cfg.digitizer_type      = self._dig_type.currentText()
        cfg.digitizer_address   = self._dig_addr.text().strip()
        cfg.mux_port            = self._mux_port.text().strip()
        cfg.k6485_port          = self._k6485_port.text().strip()
        cfg.stage_serial_x      = int(self._ser_x.text())
        cfg.stage_serial_y      = int(self._ser_y.text())
        cfg.stage_serial_limit  = int(self._ser_lim.text())
        cfg.influxdb_url        = self._sc_url.text().strip()
        cfg.influxdb_org        = self._sc_org.text().strip()
        cfg.influxdb_token      = self._sc_token.text().strip()
        cfg.influxdb_rtd_field  = self._sc_field.text().strip()

    def _connect_elec(self):
        self._push_config()
        self._run("elec", self._hub.connect_elec, self._st_elec)

    def _disconnect_elec(self):
        self._hub.disconnect_elec()
        _set_status_style(self._st_elec, self._hub.status["elec"])

    def _connect_dig(self):
        self._push_config()
        self._run("dig", self._hub.connect_dig, self._st_dig)

    def _disconnect_dig(self):
        self._hub.disconnect_dig()
        _set_status_style(self._st_dig, self._hub.status["dig"])

    def _connect_mux(self):
        self._push_config()
        self._run("mux", self._hub.connect_mux, self._st_mux)

    def _disconnect_mux(self):
        self._hub.disconnect_mux()
        _set_status_style(self._st_mux, self._hub.status["mux"])

    def _connect_k6485(self):
        self._push_config()
        self._run("k6485", self._hub.connect_k6485, self._st_k6485)

    def _disconnect_k6485(self):
        self._hub.disconnect_k6485()
        _set_status_style(self._st_k6485, self._hub.status["k6485"])

    def _connect_stage(self):
        self._push_config()
        self._run("stage", self._hub.connect_stage, self._st_stg)

    def _disconnect_stage(self):
        self._hub.disconnect_stage()
        _set_status_style(self._st_stg, self._hub.status["stage"])

    def _home_stage(self):
        if self._hub.stage is None:
            return
        w = DAQWorker(self._hub.stage.home)
        w.finished.connect(lambda _: _set_status_style(self._st_stg, "OK — homed"))
        w.error.connect(lambda tb: _set_status_style(self._st_stg, f"ERROR: {tb.splitlines()[-1]}"))
        self._workers["stage_home"] = w
        w.start()

    def _connect_sc(self):
        self._push_config()
        self._run("sc", self._hub.connect_sc, self._st_sc)

    def _disconnect_sc(self):
        self._hub.disconnect_sc()
        _set_status_style(self._st_sc, self._hub.status["sc"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hbox(*widgets) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    for wgt in widgets:
        h.addWidget(wgt)
    h.addStretch()
    return w
