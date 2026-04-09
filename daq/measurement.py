"""
daq/measurement.py  —  Level 2

Single-SiPM compound measurements.

Each function handles one complete measurement cycle for one device:
  1. Move stage to device position (or named position for lamp/photodiode)
  2. Select MUX channel
  3. Position light source (dark position or lamp position)
  4. Set bias
  5. Acquire data
  6. Return result

Light source positioning
------------------------
  illuminated=False → lamp moves to config.named_position("dark")
  illuminated=True  → lamp moves to config.named_position("lamp")
  The lamp stage is separate from the main XY stage.  Pass lamp_stage=None
  to skip lamp positioning (e.g. when the lamp is fixed or manually set).

Instruments bundle
------------------
All Level 2 functions accept an `instruments` dict with keys:
  "stage"      : StageController (main XY stage)
  "lamp_stage" : StageController or None (1-axis lamp stage)
  "mux"        : MuxController
  "elec"       : B2987BController
  "digitizer"  : digitizer backend from daq.digitizer.make_digitizer()
  "k6485"      : K6485Driver  (flux monitor)
  "slowcontrol": SlowControl  (temperature reader)

Not all instruments are required for every function — pass None for unused ones.
"""

import logging
import time

import numpy as np

from . import primitives as P

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _move_lamp(instruments: dict, illuminated: bool, config):
    """Move lamp stage to dark or lamp position as specified in config."""
    lamp = instruments.get("lamp_stage")
    if lamp is None:
        return
    if illuminated:
        x, y = config.named_position("lamp")
        log.debug("Lamp → lamp position (%.1f, %.1f) mm", x, y)
    else:
        x, y = config.named_position("dark")
        log.debug("Lamp → dark position (%.1f, %.1f) mm", x, y)
    P.move_stage(lamp, x_mm=x, y_mm=y,
                 deenergize_after=config.stage_deenergize)


def _move_to_sipm(instruments: dict, sipm_id: int, config):
    """Move main XY stage to a SiPM's position and select its MUX channel."""
    stage = instruments.get("stage")
    mux   = instruments.get("mux")
    x, y  = config.sipm_position(sipm_id)
    ch    = config.sipm_channel(sipm_id)
    if stage is not None:
        log.debug("Stage → SiPM %d @ (%.1f, %.1f) mm", sipm_id, x, y)
        P.move_stage(stage, x_mm=x, y_mm=y,
                     deenergize_after=config.stage_deenergize)
    if mux is not None:
        P.select_channel(mux, ch)


# ---------------------------------------------------------------------------
# IV sweep  (single SiPM)
# ---------------------------------------------------------------------------

def iv_sweep(sipm_id: int,
             instruments: dict,
             config,
             illuminated: bool = False,
             voltages=None,
             n_per_point: int | None = None,
             delay_s: float | None = None):
    """
    Run an IV sweep on one SiPM.

    Parameters
    ----------
    sipm_id     : SiPM identifier (looked up in config for position + channel).
    instruments : Instrument bundle dict.
    config      : ExperimentConfig.
    illuminated : True = lamp at lamp position; False = lamp at dark position.
    voltages    : Voltage iterable. Defaults to config.iv_voltages().
    n_per_point : Points per voltage. Defaults to config.iv_n_per_point.
    delay_s     : Trigger delay. Defaults to config.iv_delay_s if set, else 0.1.

    Returns
    -------
    SweepResult  (from b2987b.controller)
    """
    voltages    = list(voltages) if voltages is not None else list(config.iv_voltages())
    n_per_point = n_per_point if n_per_point is not None else config.iv_n_per_point
    delay_s     = delay_s if delay_s is not None else getattr(config, "iv_delay_s", 0.1)

    log.info("iv_sweep sipm=%d  illum=%s  %d voltages  %d pts/V",
             sipm_id, illuminated, len(voltages), n_per_point)

    _move_lamp(instruments, illuminated, config)
    _move_to_sipm(instruments, sipm_id, config)

    elec = instruments.get("elec")
    result = P.iv_sweep(elec, voltages, n_per_voltage=n_per_point, delay_s=delay_s)
    P.bias_off(elec)

    return result


# ---------------------------------------------------------------------------
# Pulse acquisition  (single SiPM)
# ---------------------------------------------------------------------------

def pulse_run(sipm_id: int,
              instruments: dict,
              config,
              illuminated: bool = False,
              bias_v: float | None = None,
              n_waveforms: int | None = None):
    """
    Acquire pulses from one SiPM.

    Parameters
    ----------
    sipm_id     : SiPM identifier.
    instruments : Instrument bundle dict.
    config      : ExperimentConfig.
    illuminated : Lamp position (True = lamp, False = dark).
    bias_v      : Bias voltage (V). Defaults to config.pulse_bias_v.
    n_waveforms : Number of triggers. Defaults to config.pulse_n_waveforms.

    Returns
    -------
    DigitizerResult  (from daq.digitizer)
    """
    bias_v      = bias_v      if bias_v      is not None else config.pulse_bias_v
    n_waveforms = n_waveforms if n_waveforms is not None else config.pulse_n_waveforms

    log.info("pulse_run sipm=%d  illum=%s  bias=%.2f V  n=%d",
             sipm_id, illuminated, bias_v, n_waveforms)

    _move_lamp(instruments, illuminated, config)
    _move_to_sipm(instruments, sipm_id, config)

    elec      = instruments.get("elec")
    digitizer = instruments.get("digitizer")

    P.set_bias(elec, bias_v, settle_s=0.2)
    result = P.acquire_pulses(digitizer, n_waveforms)
    P.bias_off(elec)

    return result


# ---------------------------------------------------------------------------
# Flux reading
# ---------------------------------------------------------------------------

def flux_reading(instruments: dict, config) -> float:
    """
    Move to the photodiode position and read photocurrent.

    Returns
    -------
    float — mean current in amperes.
    """
    stage = instruments.get("stage")
    k6485 = instruments.get("k6485")
    mux   = instruments.get("mux")

    # Park MUX (no channel selected during flux read)
    if mux is not None:
        P.zero_channels(mux)

    # Move to photodiode
    if stage is not None:
        x, y = config.named_position("photodiode")
        log.debug("Stage → photodiode (%.1f, %.1f) mm", x, y)
        P.move_stage(stage, x_mm=x, y_mm=y,
                     deenergize_after=config.stage_deenergize)

    # Read
    flux_a = P.read_flux(k6485)
    log.info("flux_reading: %.3e A", flux_a)
    return flux_a
