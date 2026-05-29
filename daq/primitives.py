"""
daq/primitives.py  —  Level 1

Single-instrument operations.  Every function takes the relevant instrument
object(s) as its first argument(s) — no global state, no config knowledge,
fully testable in isolation.

These are the building blocks used by measurement.py (Level 2) and can also
be called directly from the GUI or interactively.

Instrument type hints use string annotations to avoid hard imports.
"""

import logging
import time

import numpy as np

log = logging.getLogger(__name__)


# ===========================================================================
# Stage
# ===========================================================================

def move_stage(stage, x_mm: float | None = None, y_mm: float | None = None,
               deenergize_after: bool = False, settle_s: float = 0.0):
    """
    Move stage to absolute position.

    Parameters
    ----------
    stage            : StageController
    x_mm, y_mm       : Target coordinates in mm. None = do not move that axis.
    deenergize_after : De-energize motor coils after move (reduces electrical noise).
    settle_s         : Wait time after move before returning (s).
    """
    log.debug("move_stage → x=%s mm  y=%s mm  deenergize=%s", x_mm, y_mm, deenergize_after)
    stage.move_to(x_mm=x_mm, y_mm=y_mm, deenergize_after=deenergize_after)
    if settle_s > 0:
        time.sleep(settle_s)


def home_stage(stage):
    """Drive stage to home limit switches and reset origin."""
    log.info("Homing stage")
    stage.home()


def energize_stage(stage):
    """Re-energize motor coils before a move."""
    stage.energize()


def deenergize_stage(stage):
    """De-energize motor coils before a measurement."""
    stage.deenergize()


def stage_position(stage) -> tuple[float, float]:
    """Return current (x_mm, y_mm)."""
    return stage.position()


# ===========================================================================
# MUX
# ===========================================================================

def select_channel(mux, channel: int, settle_s: float = 0.05):
    """
    Activate a single MUX channel (bias + sense, all others off).

    Parameters
    ----------
    mux      : MuxController
    channel  : Channel number 1–96.
    settle_s : Relay settling delay (s).
    """
    log.debug("select_channel %d", channel)
    mux.select(channel, settle_s=settle_s)


def zero_channels(mux):
    """Deactivate all MUX channels."""
    mux.zero()


# ===========================================================================
# Electrometer / bias source  (B2987B)
# ===========================================================================

def set_bias(elec, voltage_v: float, settle_s: float = 0.0):
    """
    Set bias voltage and enable output.

    Parameters
    ----------
    elec      : B2987BController
    voltage_v : Target voltage (V).
    settle_s  : Wait after enabling output (s).
    """
    log.debug("set_bias %.3f V", voltage_v)
    elec.set_bias(voltage_v, settle_s=settle_s)


def ramp_bias(elec, target_v: float, step_v: float = 1.0, step_delay_s: float = 0.1):
    """Ramp bias to target_v in step_v increments."""
    log.info("ramp_bias → %.2f V", target_v)
    elec.ramp_bias(target_v, step_v=step_v, step_delay_s=step_delay_s)


def bias_off(elec):
    """Disable electrometer output."""
    elec.bias_off()


def measure_current(elec) -> float:
    """
    Measure current once at the current bias voltage.

    Returns
    -------
    float — current in amperes.
    """
    return elec.measure_current()


def iv_sweep(elec, voltages, n_per_voltage: int = 5,
             delay_s: float = 0.1):
    """
    Run a full IV sweep using the electrometer's own ammeter.

    Parameters
    ----------
    elec          : B2987BController
    voltages      : Iterable of voltage set-points (V).
                    Accepts np.ndarray, list, np.arange result, etc.
    n_per_voltage : Number of current readings averaged at each voltage.
    delay_s       : Trigger delay between voltage steps (s).

    Returns
    -------
    SweepResult (b2987b.controller.SweepResult)
    """
    log.info("iv_sweep: %d voltages, %d pts/V, delay=%.2f s",
             len(list(voltages)), n_per_voltage, delay_s)
    return elec.sweep(list(voltages), n_per_voltage=n_per_voltage, delay_s=delay_s)


def iv_sweep_external_meter(elec, meter, voltages,
                            n_per_voltage: int = 5,
                            delay_s: float = 0.1,
                            first_point_settle_s: float = 0.5,
                            progress_cb=None):
    """
    Run an IV sweep where `elec` sources voltage and `meter` measures current.

    Used when the bias source and the current meter are physically distinct
    instruments (e.g. B2987B sourcing bias, K6485 picoammeter on the SiPM
    low side). The B2987's internal list-sweep mode cannot be used here
    because it reads its own ammeter, not the external meter.

    Parameters
    ----------
    elec                 : B2987BController (voltage source)
    meter                : Object exposing .read_n(n, delay_s) -> (currents, timestamps).
    voltages             : Iterable of voltage set-points (V).
    n_per_voltage        : Number of meter readings averaged at each voltage.
    delay_s              : Settle time after set_bias and between meter samples (s).
    first_point_settle_s : Extra settle on the first voltage. Useful when the
                           previous bias state was avalanche current; the
                           discharge transient otherwise inflates the first
                           reading.
    progress_cb          : Optional callable(i, n_total, v, mean_i, std_i).
                           Called once per voltage point AFTER its samples
                           are in.  Exceptions inside the callback are
                           swallowed (cb is for UI feedback — must never
                           abort the sweep).

    Returns
    -------
    SweepResult (b2987b.controller.SweepResult). avg_voltage_v / voltage_v
    are NaN because the external meter does not measure voltage.
    """
    from b2987b import SweepResult

    voltages = list(voltages)
    log.info("iv_sweep_external_meter: %d voltages, %d pts/V, delay=%.2f s",
             len(voltages), n_per_voltage, delay_s)

    raw_s, raw_i, raw_t = [], [], []
    run_ts = time.time()

    n_total = len(voltages)
    for ix, v in enumerate(voltages):
        settle = first_point_settle_s if ix == 0 else delay_s
        elec.set_bias(float(v), settle_s=settle)
        if ix == 0 and first_point_settle_s > delay_s:
            try:
                meter.read_n(1, 0.0)
            except Exception:
                pass
        currents, timestamps = meter.read_n(n_per_voltage, delay_s=delay_s)
        raw_s.extend([float(v)] * len(currents))
        raw_i.extend(np.asarray(currents, dtype=np.float64).tolist())
        raw_t.extend(np.asarray(timestamps, dtype=np.float64).tolist())
        if progress_cb is not None:
            try:
                arr = np.asarray(currents, dtype=np.float64)
                mean_i = float(arr.mean()) if arr.size else float("nan")
                std_i  = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
                progress_cb(ix + 1, n_total, float(v), mean_i, std_i)
            except Exception:
                # UI callback: never let it abort the sweep
                pass

    source_v  = np.asarray(raw_s, dtype=np.float64)
    current_a = np.asarray(raw_i, dtype=np.float64)
    timestamp = np.asarray(raw_t, dtype=np.float64)
    voltage_v = np.full_like(source_v, np.nan)

    uniq_v = np.array(voltages, dtype=np.float64)
    avg_i = np.zeros_like(uniq_v)
    err_i = np.zeros_like(uniq_v)
    for k, v in enumerate(uniq_v):
        sel = source_v == v
        samp = current_a[sel]
        avg_i[k] = float(np.mean(samp))
        err_i[k] = (float(np.std(samp, ddof=1) / np.sqrt(samp.size))
                    if samp.size > 1 else 0.0)
    avg_v = np.full_like(uniq_v, np.nan)
    err_v = np.full_like(uniq_v, np.nan)

    return SweepResult(
        source_v      = source_v,
        current_a     = current_a,
        voltage_v     = voltage_v,
        timestamp_s   = timestamp,
        avg_source_v  = uniq_v,
        avg_current_a = avg_i,
        avg_voltage_v = avg_v,
        err_current_a = err_i,
        err_voltage_v = err_v,
        n_per_voltage = n_per_voltage,
        run_timestamp = run_ts,
    )


# ===========================================================================
# Digitizer  (DigitizerResult via daq.digitizer)
# ===========================================================================

def acquire_pulses(digitizer, n_waveforms: int,
                   timeout_s: float = 120.0):
    """
    Acquire n_waveforms and return pulse amplitudes + timestamps.

    Parameters
    ----------
    digitizer   : backend from daq.digitizer.make_digitizer()
    n_waveforms : Number of triggers to acquire.
    timeout_s   : Maximum acquisition time (s).

    Returns
    -------
    DigitizerResult (daq.digitizer.DigitizerResult)
    """
    log.debug("acquire_pulses n=%d", n_waveforms)
    return digitizer.run(n_waveforms=n_waveforms, timeout_s=timeout_s)


# ===========================================================================
# Flux monitor  (Keithley 6485)
# ===========================================================================

def read_flux(k6485, n: int = 10, delay_s: float = 0.05) -> float:
    """
    Read mean photocurrent from the XUV photodiode flux monitor.

    Parameters
    ----------
    k6485   : K6485Driver
    n       : Number of readings to average.
    delay_s : Delay between readings (s).

    Returns
    -------
    float — mean current in amperes.
    """
    currents, _ = k6485.read_n(n, delay_s=delay_s)
    mean_i = float(np.mean(currents))
    log.debug("read_flux: %.3e A (n=%d)", mean_i, n)
    return mean_i


# ===========================================================================
# Slow control  (InfluxDB temperature)
# ===========================================================================

def read_temperature(slowcontrol) -> float:
    """
    Read the current cryostat temperature.

    Returns
    -------
    float — temperature in Kelvin.
    """
    T = slowcontrol.temperature_K()
    log.debug("read_temperature: %.3f K", T)
    return T
