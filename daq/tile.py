"""
daq/tile.py  —  Level 3

Tile-level sweeps: iterate over a list of SiPMs calling Level 2 functions,
with periodic flux calibration checks and optional resume support.

Functions
---------
  tile_iv_sweep(sipm_ids, ...)      — IV sweep across a set of SiPMs
  tile_pulse_run(sipm_ids, ...)     — Pulse acquisition across a set of SiPMs

Both functions:
  - Accept a `manifest` and `run_file` for resume and data storage.
  - Skip already-completed steps when manifest is provided.
  - Insert a flux_reading() every `flux_interval` devices.
  - Call an optional `on_progress(done, total, sipm_id)` callback.
"""

import logging

from . import measurement as M
from .resume import RunManifest
from .storage import RunFile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IV sweep over a set of SiPMs
# ---------------------------------------------------------------------------

def tile_iv_sweep(sipm_ids: list[int],
                  instruments: dict,
                  config,
                  temperature_K: float,
                  illuminated: bool = False,
                  voltages=None,
                  n_per_point: int | None = None,
                  flux_interval: int | None = None,
                  manifest: RunManifest | None = None,
                  run_file: RunFile | None = None,
                  on_progress=None):
    """
    Run an IV sweep on each SiPM in sipm_ids.

    Parameters
    ----------
    sipm_ids      : Ordered list of SiPM IDs to measure.
    instruments   : Instrument bundle (see measurement.py).
    config        : ExperimentConfig.
    temperature_K : Current temperature (attached to data as metadata).
    illuminated   : Lamp position.
    voltages      : Voltage iterable. Defaults to config.iv_voltages().
    n_per_point   : Points per voltage. Defaults to config.iv_n_per_point.
    flux_interval : Insert flux check every N SiPMs.
                    Defaults to config.flux_check_interval.
    manifest      : RunManifest for skip-if-done logic.
    run_file      : RunFile to write results into.
    on_progress   : Callable(done, total, sipm_id) for GUI updates.

    Returns
    -------
    dict mapping sipm_id → SweepResult for completed measurements.
    """
    flux_interval = flux_interval if flux_interval is not None \
                    else config.flux_check_interval
    cond          = "illum" if illuminated else "dark"
    T_key         = f"{temperature_K:.1f}K"
    total         = len(sipm_ids)
    results       = {}
    flux_count    = 0

    for done_count, sipm_id in enumerate(sipm_ids):
        step_id = f"iv_{cond}_ch{sipm_id}_{T_key}"

        if manifest is not None and manifest.is_done(step_id):
            log.info("Skipping %s (already done)", step_id)
            flux_count += 1
            if on_progress:
                on_progress(done_count + 1, total, sipm_id)
            continue

        log.info("[%d/%d] IV sweep  sipm=%d  illum=%s  T=%.1f K",
                 done_count + 1, total, sipm_id, illuminated, temperature_K)

        result = M.iv_sweep(
            sipm_id     = sipm_id,
            instruments = instruments,
            config      = config,
            illuminated = illuminated,
            voltages    = voltages,
            n_per_point = n_per_point,
        )
        results[sipm_id] = result

        # Write to HDF5
        if run_file is not None:
            ch = config.sipm_channel(sipm_id)
            x, y = config.sipm_position(sipm_id)
            T_meas = _measured_temperature_K(instruments)
            iv_attrs = {
                "mux_channel":          ch,
                "x_mm":                 x,
                "y_mm":                 y,
                "bias_v":               float(result.avg_source_v[-1]) if len(result.avg_source_v) else 0.0,
                "temperature_K_setpoint": temperature_K,
            }
            if T_meas is not None:
                iv_attrs["temperature_K_measured"] = T_meas
            run_file.write_iv(
                sipm_id       = sipm_id,
                temperature_K = temperature_K,
                illuminated   = illuminated,
                source_v      = result.avg_source_v,
                current_a     = result.avg_current_a,
                err_current   = result.err_current_a,
                attrs         = iv_attrs,
            )

        # Mark done
        if manifest is not None:
            hdf5_group = f"/{sipm_id}/{T_key}/{'illuminated' if illuminated else 'dark'}/iv"
            manifest.mark_done(step_id,
                               hdf5_path  = run_file._path if run_file else None,
                               hdf5_group = hdf5_group)

        flux_count += 1
        if flux_count >= flux_interval:
            _do_flux_check(sipm_id, temperature_K, instruments, config,
                           manifest, run_file, T_key)
            flux_count = 0

        if on_progress:
            on_progress(done_count + 1, total, sipm_id)

    return results


# ---------------------------------------------------------------------------
# Pulse run over a set of SiPMs
# ---------------------------------------------------------------------------

def tile_pulse_run(sipm_ids: list[int],
                   instruments: dict,
                   config,
                   temperature_K: float,
                   illuminated: bool = False,
                   bias_v: float | None = None,
                   n_waveforms: int | None = None,
                   flux_interval: int | None = None,
                   manifest: RunManifest | None = None,
                   run_file: RunFile | None = None,
                   on_progress=None):
    """
    Run pulse acquisitions on each SiPM in sipm_ids.

    Parameters
    ----------
    sipm_ids      : Ordered list of SiPM IDs.
    instruments   : Instrument bundle.
    config        : ExperimentConfig.
    temperature_K : Current temperature.
    illuminated   : Lamp position.
    bias_v        : Bias voltage. Defaults to config.pulse_bias_v.
    n_waveforms   : Number of triggers. Defaults to config.pulse_n_waveforms.
    flux_interval : Flux check interval. Defaults to config.flux_check_interval.
    manifest      : RunManifest for skip-if-done.
    run_file      : RunFile for data storage.
    on_progress   : Callable(done, total, sipm_id).

    Returns
    -------
    dict mapping sipm_id → DigitizerResult for completed measurements.
    """
    flux_interval = flux_interval if flux_interval is not None \
                    else config.flux_check_interval
    cond          = "illum" if illuminated else "dark"
    T_key         = f"{temperature_K:.1f}K"
    total         = len(sipm_ids)
    results       = {}
    flux_count    = 0

    for done_count, sipm_id in enumerate(sipm_ids):
        step_id = f"pulse_{cond}_ch{sipm_id}_{T_key}"

        if manifest is not None and manifest.is_done(step_id):
            log.info("Skipping %s (already done)", step_id)
            flux_count += 1
            if on_progress:
                on_progress(done_count + 1, total, sipm_id)
            continue

        log.info("[%d/%d] Pulse run  sipm=%d  illum=%s  T=%.1f K",
                 done_count + 1, total, sipm_id, illuminated, temperature_K)

        result = M.pulse_run(
            sipm_id     = sipm_id,
            instruments = instruments,
            config      = config,
            illuminated = illuminated,
            bias_v      = bias_v,
            n_waveforms = n_waveforms,
        )
        results[sipm_id] = result

        # Write to HDF5
        if run_file is not None:
            ch = config.sipm_channel(sipm_id)
            x, y = config.sipm_position(sipm_id)
            T_meas = _measured_temperature_K(instruments)
            pulse_attrs = {
                "mux_channel":            ch,
                "x_mm":                   x,
                "y_mm":                   y,
                "bias_v":                 bias_v if bias_v is not None else config.pulse_bias_v,
                "temperature_K_setpoint": temperature_K,
            }
            if T_meas is not None:
                pulse_attrs["temperature_K_measured"] = T_meas
            run_file.write_pulse(
                sipm_id       = sipm_id,
                temperature_K = temperature_K,
                illuminated   = illuminated,
                result        = result,
                attrs         = pulse_attrs,
            )

        if manifest is not None:
            hdf5_group = f"/{sipm_id}/{T_key}/{'illuminated' if illuminated else 'dark'}/pulse"
            manifest.mark_done(step_id,
                               hdf5_path  = run_file._path if run_file else None,
                               hdf5_group = hdf5_group)

        flux_count += 1
        if flux_count >= flux_interval:
            _do_flux_check(sipm_id, temperature_K, instruments, config,
                           manifest, run_file, T_key)
            flux_count = 0

        if on_progress:
            on_progress(done_count + 1, total, sipm_id)

    return results


# ---------------------------------------------------------------------------
# Internal: measured temperature helper
# ---------------------------------------------------------------------------

def _measured_temperature_K(instruments: dict) -> float | None:
    """
    Read the current temperature from slowcontrol if available.
    Returns None without raising if slowcontrol is absent or the query fails.
    """
    sc = instruments.get("slowcontrol")
    if sc is None:
        return None
    try:
        return sc.temperature_K()
    except Exception as e:
        log.debug("Could not read measured temperature: %s", e)
        return None


# ---------------------------------------------------------------------------
# Internal: flux check with logging
# ---------------------------------------------------------------------------

def _do_flux_check(last_sipm_id, temperature_K, instruments, config,
                   manifest, run_file, T_key):
    step_id = f"flux_after_ch{last_sipm_id}_{T_key}"

    if manifest is not None and manifest.is_done(step_id):
        log.info("Skipping flux check %s (already done)", step_id)
        return

    log.info("Flux check after sipm=%d  T=%.1f K", last_sipm_id, temperature_K)
    flux_a = M.flux_reading(instruments, config)

    if run_file is not None:
        flux_attrs = {
            "temperature_K_setpoint": temperature_K,
            "after_sipm_id":          last_sipm_id,
        }
        T_meas = _measured_temperature_K(instruments)
        if T_meas is not None:
            flux_attrs["temperature_K_measured"] = T_meas
        run_file.write_flux(flux_a, attrs=flux_attrs)

    if manifest is not None:
        manifest.mark_done(step_id,
                           hdf5_path = run_file._path if run_file else None,
                           extra     = {"flux_a": flux_a})
