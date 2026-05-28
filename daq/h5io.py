"""
daq/h5io.py

Canonical HDF5 writers for the DAQ. Every site that produces a `.h5` file
should call into here so that dataset names, attribute keys, and compression
are identical regardless of which UI or script produced the file.

Writers take an *open `h5py.Group`* (not a path) — the caller controls where
in the file the data lands. That lets one helper serve RunFile's deep
hierarchy, measurement_store's per-file timestamped subgroups, bench_test's
flat top-level groups, and shell.py's single-named-group save.

Locked dataset names
--------------------
IV               source_v, current_a, err_current,
                 voltage_v, timestamp_s,
                 avg_source_v, avg_current_a, avg_voltage_v,
                 err_current_a, err_voltage_v,
                 raw_source_v, raw_current_a, photodiode_current_a
Pulse            amplitudes_v, amplitudes_adc, timestamps_s,
                 waveforms, time_axis_s         (inside ch{N}/ subgroups
                                                 when multi-channel)
Current samples  current_a, timestamp_s, source_v, voltage_v

Locked top-level attrs
----------------------
schema_version, measurement_type, run_start_utc,
sipm_id, temperature_K, illuminated
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import h5py
    _H5PY_AVAILABLE = True
except ImportError:
    _H5PY_AVAILABLE = False


SCHEMA_VERSION = 2

_GZIP = "gzip"


def _require_h5py():
    if not _H5PY_AVAILABLE:
        raise ImportError("pip install h5py")


def _ds(grp, name, arr, *, dtype=None, compress=True):
    """Create a dataset only if `arr` is not None; gzip by default."""
    if arr is None:
        return None
    a = np.asarray(arr, dtype=dtype) if dtype is not None else np.asarray(arr)
    kw = {"compression": _GZIP} if compress and a.size else {}
    return grp.create_dataset(name, data=a, **kw)


def _apply_attrs(obj, attrs):
    if not attrs:
        return
    for k, v in attrs.items():
        if v is None:
            continue
        obj.attrs[k] = v


# ---------------------------------------------------------------------------
# Top-level file attrs
# ---------------------------------------------------------------------------

def write_top_attrs(f, *,
                    measurement_type: str,
                    sipm_id: Optional[int] = None,
                    temperature_K: Optional[float] = None,
                    illuminated: Optional[bool] = None,
                    extra: Optional[dict] = None) -> None:
    """Write the canonical top-level attrs on a file or group."""
    f.attrs["schema_version"]   = SCHEMA_VERSION
    f.attrs["measurement_type"] = str(measurement_type)
    f.attrs["run_start_utc"]    = float(time.time())
    if sipm_id is not None:
        f.attrs["sipm_id"] = int(sipm_id)
    if temperature_K is not None:
        f.attrs["temperature_K"] = float(temperature_K)
    if illuminated is not None:
        f.attrs["illuminated"] = int(bool(illuminated))
    _apply_attrs(f, extra)


# ---------------------------------------------------------------------------
# IV sweeps
# ---------------------------------------------------------------------------

def write_iv(grp, *,
             source_v,
             current_a,
             err_current=None,
             voltage_v=None,
             timestamp_s=None,
             avg_source_v=None,
             avg_current_a=None,
             avg_voltage_v=None,
             err_current_a=None,
             err_voltage_v=None,
             raw_source_v=None,
             raw_current_a=None,
             photodiode_current_a=None,
             attrs: Optional[dict] = None) -> None:
    """Write an IV sweep into `grp` using canonical dataset names.

    Only the source_v and current_a fields are required. Every other field
    is written if the caller supplies it, skipped otherwise.
    """
    _ds(grp, "source_v",             source_v,             dtype=np.float64)
    _ds(grp, "current_a",            current_a,            dtype=np.float64)
    _ds(grp, "err_current",          err_current,          dtype=np.float64)
    _ds(grp, "voltage_v",            voltage_v,            dtype=np.float64)
    _ds(grp, "timestamp_s",          timestamp_s,          dtype=np.float64)
    _ds(grp, "avg_source_v",         avg_source_v,         dtype=np.float64)
    _ds(grp, "avg_current_a",        avg_current_a,        dtype=np.float64)
    _ds(grp, "avg_voltage_v",        avg_voltage_v,        dtype=np.float64)
    _ds(grp, "err_current_a",        err_current_a,        dtype=np.float64)
    _ds(grp, "err_voltage_v",        err_voltage_v,        dtype=np.float64)
    _ds(grp, "raw_source_v",         raw_source_v,         dtype=np.float64)
    _ds(grp, "raw_current_a",        raw_current_a,        dtype=np.float64)
    _ds(grp, "photodiode_current_a", photodiode_current_a, dtype=np.float64)

    grp.attrs["timestamp"] = time.time()
    _apply_attrs(grp, attrs)


def write_sweep_result(grp, result, *, attrs: Optional[dict] = None) -> None:
    """Unpack a B2987 SweepResult into `grp` via :func:`write_iv`."""
    write_iv(grp,
             source_v       = result.source_v,
             current_a      = result.current_a,
             voltage_v      = getattr(result, "voltage_v", None),
             timestamp_s    = getattr(result, "timestamp_s", None),
             avg_source_v   = getattr(result, "avg_source_v", None),
             avg_current_a  = getattr(result, "avg_current_a", None),
             avg_voltage_v  = getattr(result, "avg_voltage_v", None),
             err_current_a  = getattr(result, "err_current_a", None),
             err_voltage_v  = getattr(result, "err_voltage_v", None),
             attrs          = attrs)
    npv = getattr(result, "n_per_voltage", None)
    if npv is not None:
        grp.attrs["n_per_voltage"] = int(npv)
    rt = getattr(result, "run_timestamp", None)
    if rt is not None:
        grp.attrs["run_timestamp_s"] = float(rt)


# ---------------------------------------------------------------------------
# Pulse acquisitions
# ---------------------------------------------------------------------------

def write_pulse(grp, *,
                amplitudes_v=None,
                amplitudes_adc=None,
                timestamps_s=None,
                waveforms=None,
                time_axis_s=None,
                channel: Optional[int] = None,
                attrs: Optional[dict] = None) -> None:
    """Write one channel's pulse data into `grp` (or a `ch{N}/` subgroup).

    Pass either or both of amplitudes_v (volts) and amplitudes_adc
    (raw counts). They are physically distinct quantities, not aliases.
    """
    target = grp.require_group(f"ch{channel}") if channel is not None else grp

    _ds(target, "amplitudes_v",   amplitudes_v,   dtype=np.float32)
    _ds(target, "amplitudes_adc", amplitudes_adc, dtype=np.float32)
    _ds(target, "timestamps_s",   timestamps_s,   dtype=np.float64)
    _ds(target, "waveforms",      waveforms,      dtype=np.float32)
    _ds(target, "time_axis_s",    time_axis_s,    dtype=np.float32)

    _apply_attrs(target, attrs)


def write_pulse_multichannel(grp, result, *,
                             attrs: Optional[dict] = None) -> None:
    """Unpack a DigitizerResult into per-channel `ch{N}/` subgroups."""
    amps  = getattr(result, "amplitudes_v", None) or {}
    ts    = getattr(result, "timestamps",   None) or {}
    waves = getattr(result, "waveforms_v",  None) or {}

    channels = set(amps) | set(ts) | set(waves)
    for ch in sorted(channels):
        write_pulse(grp,
                    amplitudes_v = amps.get(ch),
                    timestamps_s = ts.get(ch),
                    waveforms    = waves.get(ch) if waves.get(ch) is not None and np.asarray(waves.get(ch)).size else None,
                    channel      = ch)

    ta = getattr(result, "time_axis", None)
    if ta is not None and np.asarray(ta).size:
        _ds(grp, "time_axis_s", ta, dtype=np.float32)

    grp.attrs["timestamp"] = time.time()
    if hasattr(result, "n_waveforms"):
        grp.attrs["n_waveforms"] = int(result.n_waveforms)
    src = getattr(result, "source", None)
    if src:
        grp.attrs["source"] = str(src)
    rt = getattr(result, "run_timestamp", None)
    if rt is not None:
        grp.attrs["run_timestamp_s"] = float(rt)
    ch_ids = getattr(result, "channel_ids", None)
    if ch_ids:
        grp.attrs["channel_ids"] = list(ch_ids)

    _apply_attrs(grp, attrs)


# ---------------------------------------------------------------------------
# Current samples (K6485 baseline, electrometer current_measure, etc.)
# ---------------------------------------------------------------------------

def write_current_samples(grp, *,
                          current_a,
                          timestamp_s=None,
                          source_v=None,
                          voltage_v=None,
                          attrs: Optional[dict] = None) -> None:
    """Write a current time-series; auto-populates mean_a/std_a/n attrs."""
    arr = np.asarray(current_a, dtype=np.float64)
    _ds(grp, "current_a",   arr)
    _ds(grp, "timestamp_s", timestamp_s, dtype=np.float64)
    _ds(grp, "source_v",    source_v,    dtype=np.float64)
    _ds(grp, "voltage_v",   voltage_v,   dtype=np.float64)

    grp.attrs["timestamp"] = time.time()
    grp.attrs["n"]      = int(arr.size)
    grp.attrs["mean_a"] = float(arr.mean()) if arr.size else float("nan")
    grp.attrs["std_a"]  = float(arr.std(ddof=1)) if arr.size > 1 else 0.0

    _apply_attrs(grp, attrs)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def _ts_now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ms_now() -> int:
    return int(time.time() * 1000)


def run_filename(data_dir: str, prefix: str = "run", run_id: Optional[str] = None) -> str:
    """`<data_dir>/<prefix>_YYYYMMDD_HHMMSS.h5` (or `<run_id>.h5` if given)."""
    os.makedirs(data_dir, exist_ok=True)
    name = run_id or f"{prefix}_{_ts_now()}"
    return os.path.join(data_dir, f"{name}.h5")


def bench_filename(data_dir: str) -> str:
    return run_filename(data_dir, prefix="bench")


def elec_sweep_filename(data_dir: str) -> str:
    return run_filename(data_dir, prefix="elec_sweep")


def per_measurement_filename(base_dir, *,
                             sipm_id: Optional[int] = None,
                             temperature_K: Optional[float] = None) -> Path:
    """L2 → data/sipm{N}_T{K:.1f}K/<unix_ms>.h5
       L1 → data/L1/<unix_ms>.h5"""
    base = Path(base_dir)
    if sipm_id is not None and temperature_K is not None:
        folder = base / f"sipm{int(sipm_id)}_T{float(temperature_K):.1f}K"
    else:
        folder = base / "L1"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_ms_now()}.h5"
