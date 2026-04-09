"""
daq/gui/hub.py

Shared instrument state for the GUI.

InstrumentHub holds:
  - The ExperimentConfig
  - Connected instrument objects (or None if not connected)
  - Connection status strings

All GUI tabs hold a reference to the same hub instance and read/write
instruments through it.  The Connections tab is responsible for calling
connect() / disconnect() on each instrument.
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from daq.config import ExperimentConfig


class InstrumentHub:
    """
    Central shared state for all GUI tabs.

    Attributes
    ----------
    config : ExperimentConfig
    elec   : B2987BController | None
    dig    : digitizer backend | None
    mux    : MuxController | None
    k6485  : K6485Driver | None
    stage  : StageController | None
    sc     : SlowControl | None
    """

    def __init__(self):
        self.config = ExperimentConfig()

        # Instrument objects — None until connected
        self.elec   = None
        self.dig    = None
        self.mux    = None
        self.k6485  = None
        self.stage  = None
        self.sc     = None

        # Human-readable status for each instrument
        self.status: dict[str, str] = {
            "elec":   "disconnected",
            "dig":    "disconnected",
            "mux":    "disconnected",
            "k6485":  "disconnected",
            "stage":  "disconnected",
            "sc":     "disconnected",
        }

    @property
    def instruments(self) -> dict:
        """Return the instrument bundle dict expected by measurement.py etc."""
        return {
            "elec":        self.elec,
            "digitizer":   self.dig,
            "mux":         self.mux,
            "k6485":       self.k6485,
            "stage":       self.stage,
            "lamp_stage":  None,   # future
            "slowcontrol": self.sc,
        }

    def connect_elec(self):
        from b2987b import B2987BController
        self.elec = B2987BController(visa=self.config.b2987b_visa, mode="hardware")
        self.elec.connect()
        self.elec.configure_sweep(
            n_per_voltage=self.config.iv_n_per_point,
            delay_s=getattr(self.config, "iv_delay_s", 0.1),
        )
        self.status["elec"] = f"OK — {self.elec.identify()}"

    def disconnect_elec(self):
        if self.elec:
            self.elec.disconnect()
            self.elec = None
        self.status["elec"] = "disconnected"

    def connect_dig(self):
        from daq.digitizer import make_digitizer
        self.dig = make_digitizer(self.config.digitizer_type,
                                  address=self.config.digitizer_address,
                                  mode="hardware")
        self.dig.connect()
        self.dig.setup(
            channels    = [1],
            pre_us      = self.config.pulse_pre_us,
            post_us     = self.config.pulse_post_us,
            threshold_v = self.config.pulse_threshold_v,
        )
        self.status["dig"] = f"OK — {self.dig.identify()}"

    def disconnect_dig(self):
        if self.dig:
            self.dig.disconnect()
            self.dig = None
        self.status["dig"] = "disconnected"

    def connect_mux(self):
        from pulse_mux import MuxController
        self.mux = MuxController(port=self.config.mux_port, mode="hardware")
        self.mux.connect()
        self.status["mux"] = f"OK — MUX on {self.config.mux_port}"

    def disconnect_mux(self):
        if self.mux:
            self.mux.disconnect()
            self.mux = None
        self.status["mux"] = "disconnected"

    def connect_k6485(self):
        from keithley6485 import K6485Driver
        self.k6485 = K6485Driver(visa=self.config.k6485_port, mode="hardware")
        self.k6485.connect()
        self.k6485.reset()
        self.k6485.zero_check_off()
        self.k6485.set_range("AUTO")
        self.status["k6485"] = f"OK — K6485 on {self.config.k6485_port}"

    def disconnect_k6485(self):
        if self.k6485:
            self.k6485.disconnect()
            self.k6485 = None
        self.status["k6485"] = "disconnected"

    def connect_stage(self):
        from phidget_stage import StageController
        self.stage = StageController(
            serial_x       = self.config.stage_serial_x,
            serial_y       = self.config.stage_serial_y,
            serial_limit   = self.config.stage_serial_limit,
            steps_per_mm_x = self.config.stage_steps_per_mm_x,
            steps_per_mm_y = self.config.stage_steps_per_mm_y,
            mode           = "hardware",
        )
        self.stage.connect()
        self.status["stage"] = "OK — Stage connected"

    def disconnect_stage(self):
        if self.stage:
            self.stage.disconnect()
            self.stage = None
        self.status["stage"] = "disconnected"

    def connect_sc(self):
        from daq.slowcontrol import SlowControl
        self.sc = SlowControl(self.config)
        self.sc.connect()
        T = self.sc.temperature_K()
        self.status["sc"] = f"OK — {T:.2f} K"

    def disconnect_sc(self):
        if self.sc:
            self.sc.disconnect()
            self.sc = None
        self.status["sc"] = "disconnected"
