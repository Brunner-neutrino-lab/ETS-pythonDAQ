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
    │       │       ├── ch{N}/
    │       │       │   ├── amplitudes_v float32 (n_pulses,)
    │       │       │   └── timestamps_s float64 (n_pulses,)
    │       │       └── attrs: n_waveforms, bias_v, source, timestamp, ...
    │       └── illuminated/
    │           ├── iv/     (same layout)
    │           └── pulse/  (same layout)
    │
    └── flux/
        └── <timestamp_s>    float64 scalar (one dataset per flux reading)

L3 sequence runs use a parallel `/seq/<entry_index>/...` layout so that the
same SiPM can appear more than once in a list (repeats) without colliding:

    /seq/<idx>/<sipm_id>/<temperature_K>K/<dark|illuminated>/iv/
    /seq/<idx>/<sipm_id>/<temperature_K>K/<dark|illuminated>/pulse/<bias_mV>/ch{N}/
    /seq/<idx>/<sipm_id>/<temperature_K>K/scan/<axis>/

The entry index disambiguates repeats; the pulse bias-sweep gets one subgroup
per bias point (keyed by integer millivolts). The serialized sequence is stored
at /meta/sequence. The tile-mode write_iv/write_pulse layout above is unchanged.

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
import time
from datetime import datetime
from typing import Optional

import numpy as np

from daq import h5io
from daq.h5io import run_filename  # re-export for existing callers

log = logging.getLogger(__name__)

try:
    import h5py
    _H5PY_AVAILABLE = True
except ImportError:
    _H5PY_AVAILABLE = False


def _require_h5py():
    if not _H5PY_AVAILABLE:
        raise ImportError("pip install h5py")


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
        merged = {
            "temperature_K": float(temperature_K),
            "illuminated":   int(illuminated),
            "sipm_id":       int(sipm_id),
        }
        if attrs:
            merged.update(attrs)
        h5io.write_iv(grp,
                      source_v    = source_v,
                      current_a   = current_a,
                      err_current = err_current,
                      attrs       = merged)
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
        merged = {
            "temperature_K": float(temperature_K),
            "illuminated":   int(illuminated),
            "sipm_id":       int(sipm_id),
        }
        if attrs:
            merged.update(attrs)

        if channel is not None:
            amps = result.amplitudes_v.get(channel)
            ts   = result.timestamps.get(channel)
            h5io.write_pulse(grp,
                             amplitudes_v = amps,
                             timestamps_s = ts,
                             channel      = channel)
            grp.attrs["timestamp"]   = time.time()
            grp.attrs["n_waveforms"] = int(result.n_waveforms)
            grp.attrs["source"]      = str(result.source)
            for k, v in merged.items():
                grp.attrs[k] = v
        else:
            h5io.write_pulse_multichannel(grp, result, attrs=merged)

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
    # L3 sequence data  (/seq/<idx>/... — repeat-safe)
    # ------------------------------------------------------------------

    def write_iv_seq(self, idx, sipm_id, temperature_K, illuminated,
                     result, attrs: Optional[dict] = None):
        """Write an IV SweepResult for sequence entry `idx`."""
        grp = self._seq_iv_group(idx, sipm_id, temperature_K, illuminated)
        h5io.write_sweep_result(grp, result,
                                attrs=self._seq_attrs(idx, sipm_id, temperature_K,
                                                      illuminated, attrs))
        self._file.flush()

    def write_pulse_seq(self, idx, sipm_id, temperature_K, illuminated,
                        bias_v, result, attrs: Optional[dict] = None):
        """Write a pulse DigitizerResult for one bias point of entry `idx`."""
        grp = self._seq_pulse_group(idx, sipm_id, temperature_K, illuminated, bias_v)
        merged = self._seq_attrs(idx, sipm_id, temperature_K, illuminated, attrs)
        merged["bias_v"] = float(bias_v)
        h5io.write_pulse_multichannel(grp, result, attrs=merged)
        self._file.flush()

    def write_scan_seq(self, idx, sipm_id, temperature_K, axis,
                       positions_mm, mean_current_a, std_current_a=None,
                       raw_current_a=None, attrs: Optional[dict] = None):
        """Write a 1-D scan for sequence entry `idx`."""
        grp = self._seq_scan_group(idx, sipm_id, temperature_K, axis)
        merged = self._seq_attrs(idx, sipm_id, temperature_K, None, attrs)
        merged["axis"] = str(axis)
        h5io.write_scan(grp,
                        positions_mm   = positions_mm,
                        mean_current_a = mean_current_a,
                        std_current_a  = std_current_a,
                        raw_current_a  = raw_current_a,
                        attrs          = merged)
        self._file.flush()

    def write_sequence_meta(self, seq_dict: dict):
        """Store the serialized sequence at /meta/sequence (self-describing file)."""
        meta = self._file.require_group("meta")
        if "sequence" in meta:
            del meta["sequence"]
        meta.create_dataset("sequence", data=json.dumps(seq_dict))
        self._file.flush()

    # ------------------------------------------------------------------
    # Group helpers
    # ------------------------------------------------------------------

    def _temp_key(self, temperature_K: float) -> str:
        return f"{temperature_K:.1f}K"

    def _cond_key(self, illuminated: bool) -> str:
        return "illuminated" if illuminated else "dark"

    def _bias_key(self, bias_v: float) -> str:
        return f"{int(round(bias_v * 1000))}mV"

    def _seq_attrs(self, idx, sipm_id, temperature_K, illuminated, attrs):
        merged = {
            "seq_index":     int(idx),
            "sipm_id":       int(sipm_id),
            "temperature_K": float(temperature_K),
        }
        if illuminated is not None:
            merged["illuminated"] = int(bool(illuminated))
        if attrs:
            merged.update(attrs)
        return merged

    def _seq_require(self, path: str):
        if path in self._file:
            raise RuntimeError(
                f"Sequence data already exists at {path} in {self._path}. "
                "This would overwrite existing data."
            )
        return self._file.require_group(path)

    def _seq_iv_group(self, idx, sipm_id, temperature_K, illuminated):
        return self._seq_require(
            f"seq/{int(idx)}/{sipm_id}/{self._temp_key(temperature_K)}/"
            f"{self._cond_key(illuminated)}/iv")

    def _seq_pulse_group(self, idx, sipm_id, temperature_K, illuminated, bias_v):
        return self._seq_require(
            f"seq/{int(idx)}/{sipm_id}/{self._temp_key(temperature_K)}/"
            f"{self._cond_key(illuminated)}/pulse/{self._bias_key(bias_v)}")

    def _seq_scan_group(self, idx, sipm_id, temperature_K, axis):
        return self._seq_require(
            f"seq/{int(idx)}/{sipm_id}/{self._temp_key(temperature_K)}/scan/{axis}")

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
        h5io.write_top_attrs(self._file, measurement_type="run")
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
