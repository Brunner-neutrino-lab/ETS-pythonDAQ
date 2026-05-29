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
        self.elec     = None
        self.dig      = None
        self.mux      = None
        self.k6485    = None
        self.stage    = None
        self.sc       = None
        self.wfg      = None     # Rigol DG1022 (legacy; kept for bench scripts)
        self.ks33500b = None     # Keysight 33500B (the visible WFG in the shell)
        self.nge100   = None

        # Human-readable status for each instrument
        self.status: dict[str, str] = {
            "elec":     "disconnected",
            "dig":      "disconnected",
            "mux":      "disconnected",
            "k6485":    "disconnected",
            "stage":    "disconnected",
            "sc":       "disconnected",
            "wfg":      "disconnected",
            "ks33500b": "disconnected",
            "nge100":   "disconnected",
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
            "wfg":         self.wfg,
            "ks33500b":    self.ks33500b,
            "nge100":      self.nge100,
        }

    # NOTE on the construct-then-connect-then-assign pattern below:
    # Each connect_* method builds the controller in a *local* variable,
    # calls .connect() on it, and only assigns to self.<instr> AFTER
    # connect succeeds. If connect raises, self.<instr> stays None so
    # subsequent attempts will retry properly (instead of short-circuiting
    # on a half-built object and falsely reporting "OK").

    def connect_elec(self):
        if self.elec is not None:
            # Already connected — re-clicking Connect would open a second
            # VXI-11 session that the B2987 refuses. Just refresh the status.
            self.status["elec"] = f"OK — {self.elec.identify()}"
            return
        from b2987b import B2987BController
        c = B2987BController(visa=self.config.b2987b_visa, mode="hardware")
        c.connect()
        c.configure_sweep(
            n_per_voltage=self.config.iv_n_per_point,
            delay_s=getattr(self.config, "iv_delay_s", 0.1),
        )
        self.elec = c
        self.status["elec"] = f"OK — {self.elec.identify()}"

    def disconnect_elec(self):
        if self.elec:
            self.elec.disconnect()
            self.elec = None
        self.status["elec"] = "disconnected"

    def connect_dig(self):
        if self.dig is not None:
            self.status["dig"] = f"OK — {self.dig.identify()}"
            return
        from daq.digitizer import make_digitizer
        c = make_digitizer(self.config.digitizer_type,
                           address=self.config.digitizer_address,
                           mode="hardware")
        c.connect()
        c.setup(
            channels    = [1],
            pre_us      = self.config.pulse_pre_us,
            post_us     = self.config.pulse_post_us,
            threshold_v = self.config.pulse_threshold_v,
        )
        self.dig = c
        self.status["dig"] = f"OK — {self.dig.identify()}"

    def disconnect_dig(self):
        if self.dig:
            self.dig.disconnect()
            self.dig = None
        self.status["dig"] = "disconnected"

    def connect_mux(self):
        if self.mux is not None:
            self.status["mux"] = f"OK — MUX on {self.config.mux_port}"
            return
        from pulse_mux import MuxController
        c = MuxController(port=self.config.mux_port, mode="hardware")
        c.connect()
        self.mux = c
        self.status["mux"] = f"OK — MUX on {self.config.mux_port}"

    def disconnect_mux(self):
        if self.mux:
            self.mux.disconnect()
            self.mux = None
        self.status["mux"] = "disconnected"

    def connect_k6485(self):
        if self.k6485 is not None:
            self.status["k6485"] = f"OK — K6485 on {self.config.k6485_port}"
            return
        from keithley6485 import K6485Driver
        c = K6485Driver(
            visa              = self.config.k6485_port,
            mode              = "hardware",
            baud_rate         = self.config.k6485_baud_rate,
            read_termination  = self.config.k6485_read_termination,
            write_termination = self.config.k6485_write_termination,
        )
        c.connect()
        c.reset()
        c.zero_check_off()
        c.set_range("AUTO")
        self.k6485 = c
        self.status["k6485"] = f"OK — K6485 on {self.config.k6485_port}"

    def disconnect_k6485(self):
        if self.k6485:
            self.k6485.disconnect()
            self.k6485 = None
        self.status["k6485"] = "disconnected"

    def connect_stage(self):
        if self.stage is not None:
            self.status["stage"] = "OK — Stage connected"
            return
        from phidget_stage import StageController
        c = StageController(
            serial_x       = self.config.stage_serial_x,
            serial_y       = self.config.stage_serial_y,
            serial_limit   = self.config.stage_serial_limit,
            steps_per_mm_x = self.config.stage_steps_per_mm_x,
            steps_per_mm_y = self.config.stage_steps_per_mm_y,
            mode           = "hardware",
        )
        c.connect()
        self.stage = c
        self.status["stage"] = "OK — Stage connected"

    def disconnect_stage(self):
        if self.stage:
            self.stage.disconnect()
            self.stage = None
        self.status["stage"] = "disconnected"

    def connect_sc(self):
        if self.sc is not None:
            try:
                self.status["sc"] = f"OK — {self.sc.temperature_K():.2f} K"
            except Exception as e:
                self.status["sc"] = f"OK — connected (no T: {type(e).__name__})"
            return
        from daq.slowcontrol import SlowControl
        c = SlowControl(self.config)
        c.connect()
        # The InfluxDB client is connected; reading temperature is a
        # separate concern. If the configured RTD field has no recent
        # data we still consider the connection "up" — the user can
        # pick the right field in run_config.yaml.
        self.sc = c
        try:
            T = self.sc.temperature_K()
            self.status["sc"] = f"OK — {T:.2f} K"
        except Exception as e:
            self.status["sc"] = (
                f"OK — connected (no '{self.config.influxdb_rtd_field}' "
                f"data: {type(e).__name__})"
            )

    def disconnect_sc(self):
        if self.sc:
            self.sc.disconnect()
            self.sc = None
        self.status["sc"] = "disconnected"

    def connect_wfg(self):
        if self.wfg is not None:
            self.status["wfg"] = f"OK — {self.wfg.identify()}"
            return
        from dg1022 import DG1022Controller
        c = DG1022Controller(visa=self.config.wfg_visa, mode="hardware")
        c.connect()
        self.wfg = c
        self.status["wfg"] = f"OK — {self.wfg.identify()}"

    def disconnect_wfg(self):
        if self.wfg:
            self.wfg.disconnect()
            self.wfg = None
        self.status["wfg"] = "disconnected"

    def connect_ks33500b(self):
        if self.ks33500b is not None:
            self.status["ks33500b"] = f"OK — {self.ks33500b.identify()}"
            return
        from ks33500b import KS33500BController
        c = KS33500BController(visa=self.config.ks33500b_visa, mode="hardware")
        c.connect()
        self.ks33500b = c
        self.status["ks33500b"] = f"OK — {self.ks33500b.identify()}"

    def disconnect_ks33500b(self):
        if self.ks33500b:
            self.ks33500b.disconnect()
            self.ks33500b = None
        self.status["ks33500b"] = "disconnected"

    def connect_nge100(self):
        if self.nge100 is not None:
            try:    self.status["nge100"] = f"OK — {self.nge100.idn() or self.nge100.identify()}"
            except: self.status["nge100"] = "OK"
            return
        from nge100 import NGE100Controller
        c = NGE100Controller(resource=self.config.nge100_resource,
                             mode="hardware")
        c.connect()
        self.nge100 = c
        try:    self.status["nge100"] = f"OK — {self.nge100.idn() or self.nge100.identify()}"
        except: self.status["nge100"] = "OK"

    def disconnect_nge100(self):
        if self.nge100:
            self.nge100.disconnect()
            self.nge100 = None
        self.status["nge100"] = "disconnected"
