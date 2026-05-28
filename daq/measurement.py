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
# Meter selection
# ---------------------------------------------------------------------------

_VALID_METERS = ("b2987", "k6485")


def _check_meter(meter: str) -> None:
    if meter not in _VALID_METERS:
        raise ValueError(
            f"meter must be one of {_VALID_METERS}, got {meter!r}"
        )


# ---------------------------------------------------------------------------
# IV sweep  (single SiPM)
# ---------------------------------------------------------------------------

def iv_sweep(sipm_id: int,
             instruments: dict,
             config,
             meter: str = "b2987",
             illuminated: bool = False,
             voltages=None,
             n_per_point: int | None = None,
             delay_s: float | None = None):
    """
    Run an IV sweep on one SiPM.

    The B2987B is always the bias source. `meter` selects which instrument
    measures the current:

      "b2987"  — B2987's built-in ammeter (one-shot, instrument-side list sweep).
                 On this bench the B2987 ammeter is wired to the photodiode,
                 so this reads the photodiode current vs SiPM bias, not the
                 SiPM IV. Useful as a diagnostic.
      "k6485"  — external picoammeter on the SiPM low side. Slower (Python-side
                 step + sample loop) but gives the actual SiPM IV.

    Parameters
    ----------
    sipm_id     : SiPM identifier (looked up in config for position + channel).
    instruments : Instrument bundle dict.
    config      : ExperimentConfig.
    meter       : "b2987" or "k6485".
    illuminated : True = lamp at lamp position; False = lamp at dark position.
    voltages    : Voltage iterable. Defaults to config.iv_voltages().
    n_per_point : Points per voltage. Defaults to config.iv_n_per_point.
    delay_s     : Trigger delay. Defaults to config.iv_delay_s if set, else 0.1.

    Returns
    -------
    SweepResult  (from b2987b.controller)
    """
    _check_meter(meter)
    voltages    = list(voltages) if voltages is not None else list(config.iv_voltages())
    n_per_point = n_per_point if n_per_point is not None else config.iv_n_per_point
    delay_s     = delay_s if delay_s is not None else getattr(config, "iv_delay_s", 0.1)

    log.info("iv_sweep sipm=%d  illum=%s  meter=%s  %d voltages  %d pts/V",
             sipm_id, illuminated, meter, len(voltages), n_per_point)

    _move_lamp(instruments, illuminated, config)
    _move_to_sipm(instruments, sipm_id, config)

    elec = instruments.get("elec")
    if meter == "b2987":
        result = P.iv_sweep(elec, voltages, n_per_voltage=n_per_point,
                            delay_s=delay_s)
    else:
        k = instruments.get("k6485")
        if k is None:
            raise RuntimeError("iv_sweep meter='k6485' but no K6485 in instruments")
        result = P.iv_sweep_external_meter(elec, k, voltages,
                                           n_per_voltage=n_per_point,
                                           delay_s=delay_s)
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
# Current measurement at zero bias  (single SiPM)
# ---------------------------------------------------------------------------

def current_measure(sipm_id: int,
                    instruments: dict,
                    config,
                    meter: str = "b2987",
                    illuminated: bool = False,
                    n_samples: int | None = None,
                    delay_s: float | None = None):
    """
    Measure current on one SiPM with no bias applied.

    Same setup as iv_sweep (move main stage to the SiPM, select MUX channel,
    position lamp), but the B2987 output is disabled. Useful for picoammeter
    noise floor, leakage at zero bias, dark/illuminated baselines, and
    pre-bias sanity checks.

    Parameters
    ----------
    sipm_id     : SiPM identifier.
    instruments : Instrument bundle dict.
    config      : ExperimentConfig.
    meter       : "b2987" or "k6485" — same dispatch as iv_sweep.
    illuminated : Lamp position (True = lamp, False = dark).
    n_samples   : Number of readings to average. Defaults to config.iv_n_per_point.
    delay_s     : Inter-sample delay (s). Defaults to config.iv_delay_s if set,
                  else 0.1.

    Returns
    -------
    SweepResult — single averaged point. avg_source_v=[0.0],
    avg_current_a=[mean], err_current_a=[stderr of the mean].
    """
    from b2987b import SweepResult

    _check_meter(meter)
    n_samples = n_samples if n_samples is not None else config.iv_n_per_point
    delay_s   = delay_s   if delay_s   is not None else getattr(config, "iv_delay_s", 0.1)

    log.info("current_measure sipm=%d  illum=%s  meter=%s  n=%d",
             sipm_id, illuminated, meter, n_samples)

    _move_lamp(instruments, illuminated, config)
    _move_to_sipm(instruments, sipm_id, config)

    elec = instruments.get("elec")
    P.bias_off(elec)

    run_ts = time.time()
    if meter == "b2987":
        samples = np.empty(n_samples, dtype=np.float64)
        timestamps = np.empty(n_samples, dtype=np.float64)
        for i in range(n_samples):
            samples[i] = float(P.measure_current(elec))
            timestamps[i] = time.time()
            if delay_s > 0 and i < n_samples - 1:
                time.sleep(delay_s)
    else:
        k = instruments.get("k6485")
        if k is None:
            raise RuntimeError("current_measure meter='k6485' but no K6485 in instruments")
        samples, timestamps = k.read_n(n_samples, delay_s=delay_s)
        samples = np.asarray(samples, dtype=np.float64)
        timestamps = np.asarray(timestamps, dtype=np.float64)

    mean_i = float(np.mean(samples))
    err_i  = (float(np.std(samples, ddof=1) / np.sqrt(samples.size))
              if samples.size > 1 else 0.0)

    return SweepResult(
        source_v      = np.zeros(samples.size, dtype=np.float64),
        current_a     = samples,
        voltage_v     = np.full(samples.size, np.nan),
        timestamp_s   = timestamps,
        avg_source_v  = np.array([0.0]),
        avg_current_a = np.array([mean_i]),
        avg_voltage_v = np.array([np.nan]),
        err_current_a = np.array([err_i]),
        err_voltage_v = np.array([np.nan]),
        n_per_voltage = n_samples,
        run_timestamp = run_ts,
    )
