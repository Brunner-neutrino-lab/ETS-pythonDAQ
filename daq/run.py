"""
daq/run.py  —  Level 5

Full experiment run: temperature sweep with resume support.

Orchestrates:
  - Instrument connection
  - Stage position logger (background, 1 Hz)
  - HDF5 run file
  - Run manifest + completion log
  - Temperature loop calling temppoint.run_temperature_point()

Resume behaviour
----------------
On first call:
  - Generates run_manifest.json in run_dir
  - Creates a new HDF5 file

On resume (resume=True, same run_dir):
  - Loads existing manifest
  - Replays run_log.jsonl to find completed steps
  - Skips completed steps, continues from where it left off
  - Appends to the existing HDF5 file

Usage
-----
    from daq.config import ExperimentConfig
    from daq.run import run_experiment

    cfg = ExperimentConfig.from_yaml("run_config.yaml")
    run_experiment(cfg, run_dir="data/run_001", resume=True)
"""

import logging
import os
import sys
import time
from datetime import datetime

log = logging.getLogger(__name__)

# Add sibling package directories to sys.path so instrument packages are importable
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _pkg in ("b2987b", "keithley6485", "phidget-stage", "pulse-mux-python",
             "RTO2024-python", "vx2740-python"):
    _p = os.path.join(_ROOT, _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def run_experiment(config,
                   run_dir:    str  = "data/run_001",
                   resume:     bool = True,
                   on_progress=None):
    """
    Execute the full nEXO SiPM tile characterisation experiment.

    Parameters
    ----------
    config      : ExperimentConfig (loaded from YAML or constructed manually).
    run_dir     : Directory for manifest, log, and HDF5 files.
    resume      : If True and a manifest exists in run_dir, resume from last
                  completed step.  If False, always start fresh (will fail if
                  run_dir already contains a manifest — rename or delete first).
    on_progress : Callable(level, stage, done, total, sipm_id) for GUI updates.
                  level  : "run" | "temp" | "tile" | "sipm"
                  stage  : e.g. "dark_iv", "dark_pulse", "wait", ...
                  done   : items completed so far
                  total  : total items at this level
                  sipm_id: current SiPM id or None
    """
    os.makedirs(run_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Instrument setup
    # ------------------------------------------------------------------
    instruments = _connect_instruments(config)

    # ------------------------------------------------------------------
    # Stage position logger
    # ------------------------------------------------------------------
    from .stage_logger import StagePositionLogger
    stage_logger = StagePositionLogger(
        stage    = instruments.get("stage"),
        log_dir  = os.path.join(run_dir, "stage_logs"),
        max_mb   = config.stage_log_max_mb,
    )
    run_id = os.path.basename(run_dir)
    stage_logger.start(session_id=run_id)

    # ------------------------------------------------------------------
    # Run manifest
    # ------------------------------------------------------------------
    from .resume import RunManifest
    manifest = RunManifest(run_dir=run_dir)

    if manifest.exists() and resume:
        log.info("Resuming existing run from %s", run_dir)
        manifest.load()
    else:
        if manifest.exists() and not resume:
            raise RuntimeError(
                f"run_manifest.json already exists in {run_dir}. "
                "Delete it or set resume=True to continue an existing run."
            )
        log.info("Starting new run in %s", run_dir)
        manifest.generate(config)
        manifest.save()

    log.info("Manifest: %d total steps, %d done, %d remaining",
             manifest.n_total, manifest.n_done, len(manifest.remaining_steps()))

    # ------------------------------------------------------------------
    # HDF5 run file
    # ------------------------------------------------------------------
    from .storage import RunFile, run_filename
    h5_path  = run_filename(run_dir, run_id=run_id)
    run_file = RunFile(h5_path, config=config)
    run_file.open()

    # ------------------------------------------------------------------
    # Temperature sweep
    # ------------------------------------------------------------------
    temperatures  = config.temperatures_K
    n_temps       = len(temperatures)

    try:
        for t_idx, T_K in enumerate(temperatures):
            T_key = f"{T_K:.1f}K"
            log.info("Temperature %d/%d: %.1f K", t_idx + 1, n_temps, T_K)

            if on_progress:
                on_progress("run", "temp_point", t_idx + 1, n_temps, None)

            # Determine if we can skip the stability wait
            # (all steps at this temperature are already done)
            remaining_at_T = [
                s for s in manifest.remaining_steps()
                if abs(s.temperature_K - T_K) < 0.01
            ]
            if not remaining_at_T:
                log.info("All steps at %.1f K already done — skipping", T_K)
                continue

            from .temppoint import run_temperature_point
            run_temperature_point(
                temperature_K = T_K,
                instruments   = instruments,
                config        = config,
                manifest      = manifest,
                run_file      = run_file,
                on_progress   = _make_progress_adapter(on_progress),
                skip_wait     = False,
            )

    finally:
        # Always clean up, even on exception
        run_file.close()
        stage_logger.stop()
        _disconnect_instruments(instruments)

    log.info("Run complete. %d/%d steps done.", manifest.n_done, manifest.n_total)
    log.info("Data: %s", h5_path)


# ---------------------------------------------------------------------------
# Instrument connect / disconnect
# ---------------------------------------------------------------------------

def _connect_instruments(config) -> dict:
    """
    Connect all instruments and return the bundle dict.
    Returns None for instruments that fail to connect (logs a warning).
    """
    instruments = {}

    # B2987B electrometer
    try:
        from b2987b import B2987BController
        elec = B2987BController(visa=config.b2987b_visa, mode="hardware")
        elec.connect()
        elec.configure_sweep(
            source_range  = 1000,
            n_per_voltage = config.iv_n_per_point,
            delay_s       = getattr(config, "iv_delay_s", 0.1),
        )
        instruments["elec"] = elec
        log.info("B2987B connected")
    except Exception as e:
        log.warning("B2987B connection failed: %s", e)
        instruments["elec"] = None

    # Digitizer
    try:
        from .digitizer import make_digitizer
        dig = make_digitizer(config.digitizer_type,
                             address=config.digitizer_address,
                             mode="hardware")
        dig.connect()
        dig.setup(
            channels     = [1],
            pre_us       = config.pulse_pre_us,
            post_us      = config.pulse_post_us,
            threshold_v  = config.pulse_threshold_v,
        )
        instruments["digitizer"] = dig
        log.info("Digitizer (%s) connected", config.digitizer_type)
    except Exception as e:
        log.warning("Digitizer connection failed: %s", e)
        instruments["digitizer"] = None

    # MUX
    try:
        from pulse_mux import MuxController
        mux = MuxController(port=config.mux_port, mode="hardware")
        mux.connect()
        instruments["mux"] = mux
        log.info("MUX connected on %s", config.mux_port)
    except Exception as e:
        log.warning("MUX connection failed: %s", e)
        instruments["mux"] = None

    # K6485 flux monitor
    try:
        from keithley6485 import K6485Driver
        k = K6485Driver(visa=config.k6485_port, mode="hardware")
        k.connect()
        k.reset()
        k.zero_check_off()
        k.set_range("AUTO")
        instruments["k6485"] = k
        log.info("K6485 connected on %s", config.k6485_port)
    except Exception as e:
        log.warning("K6485 connection failed: %s", e)
        instruments["k6485"] = None

    # Main XY stage
    try:
        from phidget_stage import StageController
        stage = StageController(
            serial_x       = config.stage_serial_x,
            serial_y       = config.stage_serial_y,
            serial_limit   = config.stage_serial_limit,
            steps_per_mm_x = config.stage_steps_per_mm_x,
            steps_per_mm_y = config.stage_steps_per_mm_y,
            mode           = "hardware",
        )
        stage.connect()
        instruments["stage"] = stage
        log.info("Stage connected")
    except Exception as e:
        log.warning("Stage connection failed: %s", e)
        instruments["stage"] = None

    # Lamp stage (same type, different serial — treated as single-axis)
    # For now reuse the stage controller; lamp_stage is separate if needed
    instruments["lamp_stage"] = None   # set externally if lamp has its own stage

    # Slow control
    try:
        from .slowcontrol import SlowControl
        sc = SlowControl(config)
        sc.connect()
        instruments["slowcontrol"] = sc
        log.info("SlowControl connected to %s", config.influxdb_url)
    except Exception as e:
        log.warning("SlowControl connection failed: %s", e)
        instruments["slowcontrol"] = None

    return instruments


def _disconnect_instruments(instruments: dict):
    """Disconnect all instruments cleanly."""
    for name, inst in instruments.items():
        if inst is None:
            continue
        try:
            inst.disconnect()
            log.debug("Disconnected %s", name)
        except Exception as e:
            log.warning("Error disconnecting %s: %s", name, e)


# ---------------------------------------------------------------------------
# Progress adapter
# ---------------------------------------------------------------------------

def _make_progress_adapter(on_progress):
    """Wrap temppoint on_progress format into run on_progress format."""
    if on_progress is None:
        return None
    def adapter(stage_name, done, total, sipm_id):
        on_progress("temp", stage_name, done, total, sipm_id)
    return adapter
