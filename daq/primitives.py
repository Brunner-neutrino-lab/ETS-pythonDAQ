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
    Run a full IV sweep.

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
