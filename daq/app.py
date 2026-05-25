"""
daq/app.py

Entry point for the nEXO SiPM DAQ GUI.

Usage
-----
    python -m daq.app
    python daq/app.py
"""

import sys
import os

# Ensure sibling packages are importable when running from the repo root.
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg in ("keysight2987b-python", "phidget-stage-python", "pulse-mux-python",
             "keithley6485-python", "RTO2024-python", "vx2740-python",
             "rigoldg1022-python"):
    _path = os.path.join(_repo, _pkg)
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

from PyQt5.QtWidgets import QApplication
from daq.gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("nEXO SiPM DAQ")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
