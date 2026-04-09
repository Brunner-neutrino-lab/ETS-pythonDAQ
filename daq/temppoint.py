"""
daq/temppoint.py  —  Level 4

Everything that happens at one temperature set-point:

  1. Wait for temperature to stabilise  (slowcontrol)
  2. Dark IV sweep across all SiPMs     (tile_iv_sweep, illuminated=False)
  3. Dark pulse run across all SiPMs    (tile_pulse_run, illuminated=False)
  4. [if illuminated temp]
     4a. Illuminated IV sweep           (tile_iv_sweep, illuminated=True)
     4b. Illuminated pulse run          (tile_pulse_run, illuminated=True)

Skips to the correct step when resuming a partial run.
"""

import logging

from . import tile as T
from .resume import RunManifest
from .storage import RunFile

log = logging.getLogger(__name__)


def run_temperature_point(temperature_K: float,
                          instruments: dict,
                          config,
                          manifest: RunManifest | None = None,
                          run_file: RunFile | None = None,
                          on_progress=None,
                          skip_wait: bool = False):
    """
    Execute the full measurement sequence at one temperature.

    Parameters
    ----------
    temperature_K : Target temperature in Kelvin.
    instruments   : Instrument bundle (stage, lamp_stage, mux, elec,
                    digitizer, k6485, slowcontrol).
    config        : ExperimentConfig.
    manifest      : RunManifest — enables resume (skip completed steps).
    run_file      : Open RunFile for data storage.
    on_progress   : Callable(stage_name, done, total, sipm_id).
                    stage_name is one of:
                      "wait", "dark_iv", "dark_pulse",
                      "illum_iv", "illum_pulse"
    skip_wait     : If True, skip the temperature stability wait.
                    Useful when re-entering a partially completed point.
    """
    T_key    = f"{temperature_K:.1f}K"
    do_illum = temperature_K in set(config.illuminated_temperatures_K)
    sipm_ids = [e.sipm_id for e in config.sipm_list()]

    log.info("=== Temperature point: %.1f K  (illuminated=%s) ===",
             temperature_K, do_illum)

    # ------------------------------------------------------------------
    # 1. Command setpoint, then wait for temperature stability
    # ------------------------------------------------------------------
    sc = instruments.get("slowcontrol")

    if not skip_wait:
        if sc is not None:
            # Command the setpoint first; gracefully skip if not implemented
            try:
                sc.set_setpoint(temperature_K)
                log.info("Setpoint commanded: %.1f K", temperature_K)
            except NotImplementedError:
                log.warning(
                    "set_setpoint() not implemented — assuming temperature is "
                    "set manually. Waiting for stability at %.1f K.", temperature_K
                )

            if on_progress:
                on_progress("wait", 0, 1, None)
            sc.wait_for_stable(
                target_K    = temperature_K,
                tolerance_K = config.temp_tolerance_K,
                stable_s    = config.temp_stable_s,
                on_update   = _make_temp_update_cb(on_progress),
            )
        else:
            log.warning("slowcontrol not provided — skipping temperature wait")

    # ------------------------------------------------------------------
    # Helper: progress adapter
    # ------------------------------------------------------------------
    def progress(stage_name):
        if on_progress is None:
            return None
        return lambda done, total, sid: on_progress(stage_name, done, total, sid)

    common = dict(
        instruments   = instruments,
        config        = config,
        temperature_K = temperature_K,
        manifest      = manifest,
        run_file      = run_file,
    )

    # ------------------------------------------------------------------
    # 2. Dark IV
    # ------------------------------------------------------------------
    log.info("--- Dark IV sweep ---")
    T.tile_iv_sweep(
        sipm_ids    = sipm_ids,
        illuminated = False,
        on_progress = progress("dark_iv"),
        **common,
    )

    # ------------------------------------------------------------------
    # 3. Dark pulse
    # ------------------------------------------------------------------
    log.info("--- Dark pulse run ---")
    T.tile_pulse_run(
        sipm_ids    = sipm_ids,
        illuminated = False,
        on_progress = progress("dark_pulse"),
        **common,
    )

    # ------------------------------------------------------------------
    # 4. Illuminated (coldest temperature only)
    # ------------------------------------------------------------------
    if do_illum:
        log.info("--- Illuminated IV sweep ---")
        T.tile_iv_sweep(
            sipm_ids    = sipm_ids,
            illuminated = True,
            on_progress = progress("illum_iv"),
            **common,
        )

        log.info("--- Illuminated pulse run ---")
        T.tile_pulse_run(
            sipm_ids    = sipm_ids,
            illuminated = True,
            on_progress = progress("illum_pulse"),
            **common,
        )

    log.info("=== Temperature point %.1f K complete ===", temperature_K)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_temp_update_cb(on_progress):
    """Return a slowcontrol on_update callback that feeds into on_progress."""
    if on_progress is None:
        return None
    def cb(T_K, stable_elapsed_s):
        on_progress("wait", stable_elapsed_s, 1, None)
    return cb
