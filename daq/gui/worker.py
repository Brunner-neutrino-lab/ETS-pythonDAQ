"""
daq/gui/worker.py

Generic QThread worker for running blocking DAQ operations in the background
without freezing the GUI.

Usage
-----
    def my_task():
        # long-running operation
        return result

    worker = DAQWorker(my_task)
    worker.finished.connect(on_done)
    worker.error.connect(on_error)
    worker.log_msg.connect(log_widget.appendPlainText)
    worker.start()
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback
import logging


class DAQWorker(QThread):
    """
    Run a callable in a background thread, emitting signals on completion.

    Signals
    -------
    finished(object)   — emitted with the return value of fn() on success
    error(str)         — emitted with traceback string on exception
    log_msg(str)       — emitted for log lines (if log_handler used)
    progress(int, int) — emitted as (done, total) for progress bars
    """

    finished = pyqtSignal(object)
    error    = pyqtSignal(str)
    log_msg  = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn     = fn
        self._args   = args
        self._kwargs = kwargs
        self._install_log_bridge()

    def run(self):
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception:
            self.error.emit(traceback.format_exc())

    def _install_log_bridge(self):
        """Route Python logging to log_msg signal during this thread's run."""
        signal = self.log_msg

        class SignalHandler(logging.Handler):
            def emit(self, record):
                try:
                    signal.emit(self.format(record))
                except RuntimeError:
                    pass  # Qt object already deleted

        handler = SignalHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s  %(name)s: %(message)s"))
        logging.getLogger("daq").addHandler(handler)
        self._log_handler = handler

    def __del__(self):
        try:
            logging.getLogger("daq").removeHandler(self._log_handler)
        except Exception:
            pass
