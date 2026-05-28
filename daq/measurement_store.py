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


def _l2_dir(base_dir, sipm_id: int, temperature_K: float) -> Path:
    return Path(base_dir) / f"sipm{int(sipm_id)}_T{float(temperature_K):.1f}K"


def _l1_dir(base_dir) -> Path:
    return Path(base_dir) / "L1"


def _illum(illuminated: bool) -> str:
    return "illuminated" if illuminated else "dark"


# ---------------------------------------------------------------------------
# L2 public API
# ---------------------------------------------------------------------------

def save_l2_iv_sweep(result, *, sipm_id, temperature_K, illuminated, meter,
                     base_dir="data") -> Path:
    """Persist an IV sweep SweepResult to a new file under
    `data/sipm{N}_T{K}/`."""
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
        g = f.create_group(f"iv/{_illum(illuminated)}/{ms}")
        h5io.write_sweep_result(g, result, attrs={"meter": str(meter)})
    log.info("L2 IV saved to %s", path)
    return path


def save_l2_current_measure(result, *, sipm_id, temperature_K, illuminated,
                            meter, base_dir="data") -> Path:
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
        g = f.create_group(f"current_measure/{_illum(illuminated)}/{ms}")
        h5io.write_sweep_result(g, result, attrs={
            "meter":    str(meter),
            "mean_a":   float(result.avg_current_a[0]),
            "stderr_a": float(result.err_current_a[0]),
        })
    log.info("L2 current_measure saved to %s", path)
    return path


def save_l2_pulse_run(result, *, sipm_id, temperature_K, illuminated, bias_v,
                      base_dir="data") -> Path:
    """Persist a pulse_run DigitizerResult."""
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
        g = f.create_group(f"pulse/{_illum(illuminated)}/{ms}")
        h5io.write_pulse_multichannel(g, result,
                                      attrs={"bias_v": float(bias_v)})
    log.info("L2 pulse saved to %s", path)
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
