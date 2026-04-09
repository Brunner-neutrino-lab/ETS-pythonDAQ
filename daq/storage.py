"""
daq/storage.py

HDF5 data writer for IV sweeps and pulse acquisitions.

File layout
-----------
One HDF5 file per run (opened once, kept open throughout).

    run_YYYYMMDD_HHMMSS.h5
    /
    ├── meta/
    │   ├── config          (JSON string — full ExperimentConfig)
    │   └── channel_map     (JSON string — SiPM id/channel/position list)
    │
    ├── <sipm_id>/                   e.g. /42/
    │   └── <temperature_K>/         e.g. /42/165.0K/
    │       ├── dark/
    │       │   ├── iv/
    │       │   │   ├── source_v     float64 (n_points,)
    │       │   │   ├── current_a    float64 (n_points,)
    │       │   │   ├── err_current  float64 (n_points,)
    │       │   │   └── attrs: timestamp, bias_v, mux_channel, x_mm, y_mm
    │       │   └── pulse/
    │       │       ├── amplitudes_v float32 (n_pulses,)
    │       │       ├── timestamps   float64 (n_pulses,)
    │       │       └── attrs: n_waveforms, bias_v, source, timestamp, ...
    │       └── illuminated/
    │           ├── iv/     (same layout)
    │           └── pulse/  (same layout)
    │
    └── flux/
        └── <timestamp_s>    float64 scalar (one dataset per flux reading)

Usage
-----
    from daq.storage import RunFile

    with RunFile("data/run_001.h5", config=cfg) as rf:
        rf.write_iv(sipm_id=1, temperature_K=165.0, illuminated=False,
                    source_v=voltages, current_a=currents, err_current=errors,
                    attrs={"bias_v": 49.0, "mux_channel": 1})

        rf.write_pulse(sipm_id=1, temperature_K=165.0, illuminated=False,
                       result=digitizer_result,
                       attrs={"bias_v": 49.0, "n_waveforms": 10000})

        rf.write_flux(flux_a=1.23e-8)
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import h5py
    _H5PY_AVAILABLE = True
except ImportError:
    _H5PY_AVAILABLE = False


def _require_h5py():
    if not _H5PY_AVAILABLE:
        raise ImportError("pip install h5py")


def run_filename(data_dir: str, run_id: Optional[str] = None) -> str:
    """Return a timestamped HDF5 filename in data_dir."""
    run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, f"{run_id}.h5")


class RunFile:
    """
    HDF5 run file writer.

    Parameters
    ----------
    path : str
        File path.  Created if it doesn't exist; opened for appending if it does
        (enables resume — data already written is preserved).
    config : ExperimentConfig, optional
        Written to /meta/config as a JSON blob on first open.
    """

    def __init__(self, path: str, config=None):
        _require_h5py()
        self._path   = path
        self._config = config
        self._file   = None

    def open(self):
        if self._file is not None:
            return
        self._file = h5py.File(self._path, "a")
        if "meta" not in self._file:
            self._write_meta()
        log.info("RunFile opened: %s", self._path)

    def close(self):
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
            log.info("RunFile closed: %s", self._path)

    # ------------------------------------------------------------------
    # IV data
    # ------------------------------------------------------------------

    def write_iv(self,
                 sipm_id:       int,
                 temperature_K: float,
                 illuminated:   bool,
                 source_v:      np.ndarray,
                 current_a:     np.ndarray,
                 err_current:   Optional[np.ndarray] = None,
                 attrs:         Optional[dict] = None):
        """
        Write an IV sweep result.

        Parameters
        ----------
        sipm_id       : SiPM identifier.
        temperature_K : Measurement temperature.
        illuminated   : True for illuminated, False for dark.
        source_v      : Commanded voltage array (V).
        current_a     : Mean current array (A).
        err_current   : Standard error in current (A). Optional.
        attrs         : Extra attributes (bias_v, mux_channel, x_mm, y_mm, ...).
        """
        grp = self._iv_group(sipm_id, temperature_K, illuminated)

        grp.create_dataset("source_v",   data=np.asarray(source_v,   dtype=np.float64), compression="gzip")
        grp.create_dataset("current_a",  data=np.asarray(current_a,  dtype=np.float64), compression="gzip")
        if err_current is not None:
            grp.create_dataset("err_current", data=np.asarray(err_current, dtype=np.float64), compression="gzip")

        grp.attrs["timestamp"]     = time.time()
        grp.attrs["temperature_K"] = temperature_K
        grp.attrs["illuminated"]   = int(illuminated)
        grp.attrs["sipm_id"]       = sipm_id
        if attrs:
            for k, v in attrs.items():
                grp.attrs[k] = v

        self._file.flush()
        log.debug("IV written: sipm=%d  T=%.1f K  illum=%s  n=%d",
                  sipm_id, temperature_K, illuminated, len(source_v))

    # ------------------------------------------------------------------
    # Pulse data
    # ------------------------------------------------------------------

    def write_pulse(self,
                    sipm_id:       int,
                    temperature_K: float,
                    illuminated:   bool,
                    result,
                    channel:       Optional[int] = None,
                    attrs:         Optional[dict] = None):
        """
        Write a pulse acquisition result (DigitizerResult).

        Parameters
        ----------
        sipm_id       : SiPM identifier.
        temperature_K : Measurement temperature.
        illuminated   : True for illuminated, False for dark.
        result        : DigitizerResult from daq.digitizer.
        channel       : Digitizer channel to store. If None, stores all channels.
        attrs         : Extra attributes.
        """
        grp = self._pulse_group(sipm_id, temperature_K, illuminated)

        channels = [channel] if channel is not None else list(result.amplitudes_v.keys())
        for ch in channels:
            amps = result.amplitudes_v.get(ch, np.array([], dtype=np.float32))
            ts   = result.timestamps.get(ch,   np.array([], dtype=np.float64))
            cgrp = grp.require_group(f"ch{ch}")
            cgrp.create_dataset("amplitudes_v", data=np.asarray(amps, dtype=np.float32), compression="gzip")
            cgrp.create_dataset("timestamps",   data=np.asarray(ts,   dtype=np.float64), compression="gzip")

        grp.attrs["timestamp"]     = time.time()
        grp.attrs["temperature_K"] = temperature_K
        grp.attrs["illuminated"]   = int(illuminated)
        grp.attrs["sipm_id"]       = sipm_id
        grp.attrs["n_waveforms"]   = result.n_waveforms
        grp.attrs["source"]        = result.source
        if attrs:
            for k, v in attrs.items():
                grp.attrs[k] = v

        self._file.flush()
        log.debug("Pulse written: sipm=%d  T=%.1f K  illum=%s  n_waveforms=%d",
                  sipm_id, temperature_K, illuminated, result.n_waveforms)

    # ------------------------------------------------------------------
    # Flux readings
    # ------------------------------------------------------------------

    def write_flux(self, flux_a: float, attrs: Optional[dict] = None):
        """
        Append a flux reading to /flux/.

        Parameters
        ----------
        flux_a : float
            Photocurrent in amperes.
        attrs : dict, optional
            Extra attributes (temperature_K, sipm_id_before, ...).
        """
        ts   = time.time()
        key  = f"{ts:.3f}"
        grp  = self._file.require_group("flux")
        ds   = grp.create_dataset(key, data=flux_a)
        ds.attrs["timestamp"] = ts
        if attrs:
            for k, v in attrs.items():
                ds.attrs[k] = v
        self._file.flush()
        log.debug("Flux written: %.3e A", flux_a)

    # ------------------------------------------------------------------
    # Group helpers
    # ------------------------------------------------------------------

    def _temp_key(self, temperature_K: float) -> str:
        return f"{temperature_K:.1f}K"

    def _cond_key(self, illuminated: bool) -> str:
        return "illuminated" if illuminated else "dark"

    def _iv_group(self, sipm_id: int, temperature_K: float, illuminated: bool):
        path = f"{sipm_id}/{self._temp_key(temperature_K)}/{self._cond_key(illuminated)}/iv"
        if path in self._file:
            raise RuntimeError(
                f"IV data already exists at {path} in {self._path}. "
                "This would overwrite existing data."
            )
        return self._file.require_group(path)

    def _pulse_group(self, sipm_id: int, temperature_K: float, illuminated: bool):
        path = f"{sipm_id}/{self._temp_key(temperature_K)}/{self._cond_key(illuminated)}/pulse"
        if path in self._file:
            raise RuntimeError(
                f"Pulse data already exists at {path} in {self._path}."
            )
        return self._file.require_group(path)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _write_meta(self):
        meta = self._file.require_group("meta")
        if self._config is not None:
            try:
                import dataclasses
                cfg_dict = {k: v for k, v in dataclasses.asdict(self._config).items()
                            if not k.startswith("_")}
                meta.create_dataset("config", data=json.dumps(cfg_dict))
            except Exception as e:
                log.warning("Could not serialise config to HDF5: %s", e)

            if self._config._sipms:
                map_list = [
                    {"sipm_id": e.sipm_id, "mux_channel": e.mux_channel,
                     "x_mm": e.x_mm, "y_mm": e.y_mm}
                    for e in self._config._sipms
                ]
                meta.create_dataset("channel_map", data=json.dumps(map_list))

        meta.attrs["created"] = datetime.now().isoformat()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
