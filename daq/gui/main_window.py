"""
daq/gui/main_window.py

Main application window.

QMainWindow with a QTabWidget containing all tabs:
  0. Connections
  1. Config / Magic Numbers
  2. Level 1 — Primitives
  3. Level 2 — Single SiPM
  4. Level 3 — Tile Sweep
  5. Level 4 — Temperature Point
  6. Level 5 — Full Run
"""

from PyQt5.QtWidgets import QMainWindow, QTabWidget, QStatusBar
from PyQt5.QtCore import QTimer

from .hub              import InstrumentHub
from .connections_tab  import ConnectionsTab
from .config_tab       import ConfigTab
from .level1_tab       import Level1Tab
from .level2_tab       import Level2Tab
from .level3_tab       import Level3Tab
from .level4_tab       import Level4Tab
from .level5_tab       import Level5Tab
from .raster_tab       import RasterTab
from .alignment_tab    import AlignmentTab


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("nEXO SiPM DAQ")
        self.resize(900, 720)

        self._hub = InstrumentHub()
        self._build_ui()
        self._start_status_timer()

    def _build_ui(self):
        tabs = QTabWidget()

        tabs.addTab(ConnectionsTab(self._hub), "Connections")
        tabs.addTab(ConfigTab(self._hub),      "Config")
        tabs.addTab(Level1Tab(self._hub),      "Level 1 — Primitives")
        tabs.addTab(Level2Tab(self._hub),      "Level 2 — Single SiPM")
        tabs.addTab(Level3Tab(self._hub),      "Level 3 — Tile Sweep")
        tabs.addTab(Level4Tab(self._hub),      "Level 4 — Temp Point")
        tabs.addTab(Level5Tab(self._hub),      "Level 5 — Full Run")
        tabs.addTab(RasterTab(self._hub),      "Raster Scan")
        tabs.addTab(AlignmentTab(self._hub),   "Alignment")

        self.setCentralWidget(tabs)

        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._update_status()

    def _start_status_timer(self):
        """Refresh the status bar every 5 s."""
        timer = QTimer(self)
        timer.timeout.connect(self._update_status)
        timer.start(5000)

    def _update_status(self):
        s = self._hub.status
        parts = [
            f"elec:{_short(s['elec'])}",
            f"dig:{_short(s['dig'])}",
            f"mux:{_short(s['mux'])}",
            f"k6485:{_short(s['k6485'])}",
            f"stage:{_short(s['stage'])}",
            f"sc:{_short(s['sc'])}",
        ]
        self._statusbar.showMessage("  |  ".join(parts))


def _short(status: str) -> str:
    if status.startswith("OK"):
        return "OK"
    if status == "disconnected":
        return "—"
    return status[:20]
