"""
daq/measurement_store.py

Per-measurement HDF5 writer for the webapp L1 and L2 tabs.

One file per click, named with millisecond-precision unix time.

L2 (sipm/T context) — data/sipm{N}_T{K:.1f}K/<unix_ms>.h5
    Standardized hierarchical layout:
        /                                       attrs: sipm_id, temperature_K,
                                                       run_start_utc,
                                                       measurement_type,
                                                       illuminated,
                                                       schema_version
        /iv/<dark|illuminated>/<unix_ms>/        SweepResult datasets + meter attr
        /current_measure/<dark|illuminated>/<unix_ms>/   SweepResult + meter
        /pulse/<dark|illuminated>/<unix_ms>/     DigitizerResult datasets + bias_v

    Two files for the same (sipm, T) but different measurement types share
    a layout — they can be merged with h5repack into a single per-(sipm, T)
    file without path collisions.

L1 (untagged) — data/L1/<unix_ms>.h5
    Flat layout — no /dark, no /illuminated, no /sipm wrapping. Just the
    raw datasets at root plus a `measurement_type` attr.

All dataset names and attribute keys come from :mod:`daq.h5io` so the
on-disk schema is identical to every other writer in the codebase.
"""

import logging
import time
from pathlib import Path

import h5py
import numpy as np

from daq import h5io
from daq.h5io import SCHEMA_VERSION  # re-export

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _l2_dir(base_dir, sipm_id, temperature_K: float) -> Path:
    """Folder for a per-(sipm, T) save.  `sipm_id` may be None — then
    files land in `T{K}K_anon/` (still per-T so they don't all collect
    in a single anon dir)."""
    if sipm_id is None:
        return Path(base_dir) / f"T{float(temperature_K):.1f}K_anon"
    return Path(base_dir) / f"sipm{int(sipm_id)}_T{float(temperature_K):.1f}K"


def _l1_dir(base_dir) -> Path:
    return Path(base_dir) / "L1"


def _illum(illuminated: bool) -> str:
    return "illuminated" if illuminated else "dark"


def _write_optional_attrs(group, **kw) -> None:
    """Write each kw to group.attrs, skipping any value that's None.

    Used for the optional identifier fields (sipm_id, mux_channel,
    center_x_mm, center_y_mm): only present in the file if the
    operator entered them on the L2 page.
    """
    for k, v in kw.items():
        if v is None:
            continue
        try:
            group.attrs[k] = v
        except (TypeError, ValueError):
            group.attrs[k] = str(v)


# ---------------------------------------------------------------------------
# L2 public API
# ---------------------------------------------------------------------------

def save_l2_iv_sweep(result, *, temperature_K, illuminated, meter,
                     sipm_id=None, mux_channel=None,
                     center_x_mm=None, center_y_mm=None,
                     dark_x_mm=None, dark_y_mm=None,
                     base_dir="data") -> Path:
    """Persist an IV sweep SweepResult.

    Folder: `data/sipm{N}_T{K}/<ms>.h5` if sipm_id given,
            `data/T{K}K_anon/<ms>.h5`   otherwise.
    Identifier attrs (sipm_id, mux_channel, center_x_mm, center_y_mm) are
    only written when not None — the L2 page lets the operator leave
    them blank.
    """
    ms = _now_ms()
    folder = _l2_dir(base_dir, sipm_id, temperature_K)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="iv",
                             sipm_id=sipm_id,
                             temperature_K=temperature_K,
                             illuminated=illuminated)
        _write_optional_attrs(f,
                              mux_channel=mux_channel,
                              center_x_mm=center_x_mm,
                              center_y_mm=center_y_mm,
                              dark_x_mm=dark_x_mm,
                              dark_y_mm=dark_y_mm)
        g = f.create_group(f"iv/{_illum(illuminated)}/{ms}")
        h5io.write_sweep_result(g, result, attrs={"meter": str(meter)})
    log.info("L2 IV saved to %s", path)
    return path


def save_l2_current_measure(result, *, temperature_K, illuminated, meter,
                            sipm_id=None, mux_channel=None,
                            center_x_mm=None, center_y_mm=None,
                     dark_x_mm=None, dark_y_mm=None,
                            base_dir="data") -> Path:
    """Persist a current_measure SweepResult (single averaged point)."""
    ms = _now_ms()
    folder = _l2_dir(base_dir, sipm_id, temperature_K)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="current_measure",
                             sipm_id=sipm_id,
                             temperature_K=temperature_K,
                             illuminated=illuminated)
        _write_optional_attrs(f,
                              mux_channel=mux_channel,
                              center_x_mm=center_x_mm,
                              center_y_mm=center_y_mm,
                              dark_x_mm=dark_x_mm,
                              dark_y_mm=dark_y_mm)
        g = f.create_group(f"current_measure/{_illum(illuminated)}/{ms}")
        h5io.write_sweep_result(g, result, attrs={
            "meter":    str(meter),
            "mean_a":   float(result.avg_current_a[0]),
            "stderr_a": float(result.err_current_a[0]),
        })
    log.info("L2 current_measure saved to %s", path)
    return path


def save_l2_pulse_run(result, *, temperature_K, illuminated, bias_v,
                      sipm_id=None, mux_channel=None,
                      center_x_mm=None, center_y_mm=None,
                     dark_x_mm=None, dark_y_mm=None,
                      capture_ch=None, capture_thr_adc=None,
                      aux_trigger_ch=None, aux_trigger_thr_adc=None,
                      base_dir="data") -> Path:
    """Persist a pulse_run DigitizerResult.

    Trigger-config attrs (capture / aux_trigger channels + thresholds)
    are written when given so analysis code can see how the trigger
    chain was set up — the VX2740 results don't carry that on their own.
    """
    ms = _now_ms()
    folder = _l2_dir(base_dir, sipm_id, temperature_K)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="pulse",
                             sipm_id=sipm_id,
                             temperature_K=temperature_K,
                             illuminated=illuminated)
        _write_optional_attrs(f,
                              mux_channel=mux_channel,
                              center_x_mm=center_x_mm,
                              center_y_mm=center_y_mm,
                              dark_x_mm=dark_x_mm,
                              dark_y_mm=dark_y_mm)
        g = f.create_group(f"pulse/{_illum(illuminated)}/{ms}")
        h5io.write_pulse_multichannel(g, result,
                                      attrs={"bias_v": float(bias_v)})
        _write_optional_attrs(g,
                              capture_ch=capture_ch,
                              capture_thr_adc=capture_thr_adc,
                              aux_trigger_ch=aux_trigger_ch,
                              aux_trigger_thr_adc=aux_trigger_thr_adc)
    log.info("L2 pulse saved to %s", path)
    return path


def save_l2_scan(*, positions_mm, mean_current_a, std_current_a,
                  raw_current_a=None,
                  temperature_K, axis, bias_v, meter,
                  light_mode, light_freq_hz, light_amp_v, light_width_s,
                  n_per_point, settle_s,
                  sipm_id=None, mux_channel=None,
                  center_x_mm=None, center_y_mm=None,
                  dark_x_mm=None, dark_y_mm=None,
                  base_dir="data") -> Path:
    """Persist a 1D line scan along X or Y.

    Same optional-attrs convention as the other L2 savers: sipm_id,
    mux_channel, center_{x,y}_mm are only written if not None.
    """
    ms = _now_ms()
    folder = _l2_dir(base_dir, sipm_id, temperature_K)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    positions = np.asarray(positions_mm, dtype=np.float64)
    means     = np.asarray(mean_current_a, dtype=np.float64)
    stds      = np.asarray(std_current_a,  dtype=np.float64)
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="scan",
                             sipm_id=sipm_id,
                             temperature_K=temperature_K,
                             illuminated=True)
        _write_optional_attrs(f,
                              mux_channel=mux_channel,
                              center_x_mm=center_x_mm,
                              center_y_mm=center_y_mm,
                              dark_x_mm=dark_x_mm,
                              dark_y_mm=dark_y_mm)
        g = f.create_group(f"scan/{str(axis).lower()}/{ms}")
        g.create_dataset("position_mm",    data=positions, compression="gzip")
        g.create_dataset("mean_current_a", data=means,     compression="gzip")
        g.create_dataset("std_current_a",  data=stds,      compression="gzip")
        if raw_current_a is not None:
            g.create_dataset("raw_current_a",
                             data=np.asarray(raw_current_a, dtype=np.float64),
                             compression="gzip")
        # Always-present scan attrs.
        for k, v in {
            "axis":          str(axis).lower(),
            "bias_v":        float(bias_v),
            "meter":         str(meter),
            "n_per_point":   int(n_per_point),
            "settle_s":      float(settle_s),
            "light_mode":    str(light_mode),
            "light_freq_hz": float(light_freq_hz),
            "light_amp_v":   float(light_amp_v),
            "light_width_s": float(light_width_s),
        }.items():
            g.attrs[k] = v
        # Optional contextual attrs.
        _write_optional_attrs(g,
                              mux_channel=mux_channel,
                              center_x_mm=center_x_mm,
                              center_y_mm=center_y_mm,
                              dark_x_mm=dark_x_mm,
                              dark_y_mm=dark_y_mm)
    log.info("L2 scan saved to %s", path)
    return path


# ---------------------------------------------------------------------------
# L1 public API — flat, no sipm/T/illuminated wrapping
# ---------------------------------------------------------------------------

def save_l1_waveform(result, *, channel, threshold_adc, pre_us, post_us,
                     base_dir="data") -> Path:
    """Persist an L1 single-waveform DigitizerResult."""
    ms = _now_ms()
    folder = _l1_dir(base_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="waveform",
                             extra={
                                 "channel":       int(channel),
                                 "threshold_adc": int(threshold_adc),
                                 "pre_us":        float(pre_us),
                                 "post_us":       float(post_us),
                             })
        h5io.write_pulse_multichannel(f, result)
    log.info("L1 waveform saved to %s", path)
    return path


def save_l1_current_samples(samples, timestamps=None, *, instrument, n,
                            delay_s, range_label=None,
                            base_dir="data") -> Path:
    """Persist N current samples from the K6485 or B2987 ammeter."""
    ms = _now_ms()
    folder = _l1_dir(base_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ms}.h5"
    extra = {
        "instrument": str(instrument),
        "n_samples":  int(n),
        "delay_s":    float(delay_s),
    }
    if range_label is not None:
        extra["range"] = str(range_label)
    with h5py.File(path, "w") as f:
        h5io.write_top_attrs(f,
                             measurement_type="current_samples",
                             extra=extra)
        h5io.write_current_samples(f,
                                   current_a=np.asarray(samples, dtype=np.float64),
                                   timestamp_s=timestamps)
    log.info("L1 current_samples saved to %s", path)
    return path
