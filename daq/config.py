"""
daq/config.py

Experiment configuration: all magic numbers, instrument addresses,
and the device/position map.

Channel map CSV format
----------------------
One row per SiPM plus special-purpose rows for named positions:

    type,id,mux_channel,x_mm,y_mm
    sipm,1,1,0.0,0.0
    sipm,2,2,10.0,0.0
    sipm,3,3,20.0,0.0
    dark,D,,−50.0,0.0
    lamp,L,,-25.0,50.0
    photodiode,P,,-30.0,0.0

  type        : "sipm", "dark", "lamp", or "photodiode"
  id          : integer SiPM ID, or D / L / P for special positions
  mux_channel : MUX channel 1-96 (blank/empty for special positions)
  x_mm, y_mm  : stage coordinates in mm

Usage
-----
    from daq.config import ExperimentConfig

    cfg = ExperimentConfig.from_yaml("run_config.yaml")
    # or default (simulation) config:
    cfg = ExperimentConfig()

    # Access device map
    pos = cfg.sipm_position(sipm_id=3)   # (x_mm, y_mm)
    ch  = cfg.sipm_channel(sipm_id=3)    # MUX channel

    # IV voltage array
    import numpy as np
    voltages = cfg.iv_voltages()          # np.ndarray

YAML example
------------
    # run_config.yaml
    channel_map_file: channel_map.csv

    b2987b_visa: "USB0::2391::37912::MY54321112::0::INSTR"
    digitizer_type: rto2024
    digitizer_address: "192.168.0.2"
    mux_port: COM6
    k6485_port: COM5

    influxdb_url: "http://gl-sft1200.stdusr.yale.internal:2504"
    influxdb_org: xbox-server
    influxdb_token: ""          # override from env DAQ_INFLUX_TOKEN
    influxdb_bucket: Cryostat
    influxdb_rtd_field: RTD2_C  # field name in Celsius

    iv_voltage_start: 40.0
    iv_voltage_stop:  52.0
    iv_voltage_step:  0.1
    iv_n_per_point:   5

    pulse_bias_v:       49.0
    pulse_n_waveforms:  10000
    pulse_pre_us:       2.0
    pulse_post_us:      10.0
    pulse_threshold_v:  0.010

    temperatures_K: [233.0, 215.0, 165.0]
    illuminated_temperatures_K: [165.0]
    temp_tolerance_K: 0.5
    temp_stable_s:    60.0

    flux_check_interval: 8
    data_dir: data
    log_dir:  logs
    stage_log_max_mb: 8.0
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Channel map entries
# ---------------------------------------------------------------------------

@dataclass
class SiPMEntry:
    sipm_id:     int
    mux_channel: int
    x_mm:        float
    y_mm:        float


@dataclass
class PositionEntry:
    name:  str    # "dark", "lamp", "photodiode"
    x_mm:  float
    y_mm:  float


# ---------------------------------------------------------------------------
# Main config dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExperimentConfig:

    # --- Instrument connections -------------------------------------------
    b2987b_visa:        str = "USB0::2391::37912::MY54321112::0::INSTR"
    digitizer_type:     str = "rto2024"          # "rto2024" or "vx2740"
    digitizer_address:  str = "192.168.0.2"
    mux_port:           str = "COM6"
    k6485_port:         str = "COM5"
    stage_serial_x:     int = 523267
    stage_serial_y:     int = 523253
    stage_serial_limit: int = 527475

    # --- Slow control (InfluxDB) ------------------------------------------
    influxdb_url:       str = "http://gl-sft1200.stdusr.yale.internal:2504"
    influxdb_org:       str = "xbox-server"
    influxdb_token:     str = ""     # override with env var DAQ_INFLUX_TOKEN
    influxdb_bucket:    str = "Cryostat"
    influxdb_rtd_field: str = "RTD2_C"   # field name, values in °C

    # --- Stage magic numbers ----------------------------------------------
    stage_steps_per_mm_x: float = 800.0
    stage_steps_per_mm_y: float = 1600.0
    stage_velocity_x:     float = 2000.0
    stage_velocity_y:     float = 1000.0
    stage_deenergize:     bool  = True   # de-energize coils during measurements

    # --- IV sweep ---------------------------------------------------------
    iv_voltage_start: float = 40.0   # V
    iv_voltage_stop:  float = 52.0   # V
    iv_voltage_step:  float = 0.1    # V
    iv_n_per_point:   int   = 5

    # --- Pulse acquisition ------------------------------------------------
    pulse_bias_v:       float = 49.0    # V (same for whole tile)
    pulse_n_waveforms:  int   = 10000
    pulse_pre_us:       float = 2.0
    pulse_post_us:      float = 10.0
    pulse_threshold_v:  float = 0.010   # V

    # --- Temperature schedule --------------------------------------------
    temperatures_K:              list = field(default_factory=lambda: [233.0, 215.0, 165.0])
    illuminated_temperatures_K:  list = field(default_factory=lambda: [165.0])
    temp_tolerance_K:            float = 0.5
    temp_stable_s:               float = 60.0

    # --- Scan control ----------------------------------------------------
    flux_check_interval: int = 8    # flux check every N SiPMs

    # --- Data & logging ---------------------------------------------------
    data_dir:          str   = "data"
    log_dir:           str   = "logs"
    stage_log_max_mb:  float = 8.0

    # --- Channel map file ------------------------------------------------
    channel_map_file: str = "channel_map.csv"

    # --- Coordinate offset -----------------------------------------------
    # Applied to every position lookup (sipm_position, named_position).
    # Set via set_origin() after scanning a reference SiPM to re-zero
    # coordinates when the light source or cryostat is physically adjusted.
    # Units: mm.  Default (0, 0) = channel map coordinates == stage coordinates.
    position_offset_x_mm: float = 0.0
    position_offset_y_mm: float = 0.0

    # --- Loaded channel map (populated by load_channel_map()) ------------
    _sipms:      list = field(default_factory=list, repr=False)
    _positions:  dict = field(default_factory=dict, repr=False)  # name -> PositionEntry

    # ------------------------------------------------------------------
    # IV voltage array
    # ------------------------------------------------------------------

    def iv_voltages(self) -> np.ndarray:
        """Return voltage array for IV sweep (equivalent to np.arange)."""
        return np.arange(self.iv_voltage_start,
                         self.iv_voltage_stop + self.iv_voltage_step * 0.5,
                         self.iv_voltage_step)

    # ------------------------------------------------------------------
    # Channel map access
    # ------------------------------------------------------------------

    def load_channel_map(self, path: Optional[str] = None):
        """
        Parse the channel map CSV and populate internal SiPM and position tables.

        Parameters
        ----------
        path : str, optional
            Path to CSV file. Defaults to self.channel_map_file.
        """
        path = path or self.channel_map_file
        self._sipms     = []
        self._positions = {}

        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rtype = row["type"].strip().lower()
                x = float(row["x_mm"])
                y = float(row["y_mm"])

                if rtype == "sipm":
                    self._sipms.append(SiPMEntry(
                        sipm_id     = int(row["id"]),
                        mux_channel = int(row["mux_channel"]),
                        x_mm        = x,
                        y_mm        = y,
                    ))
                elif rtype in ("dark", "lamp", "photodiode"):
                    self._positions[rtype] = PositionEntry(name=rtype, x_mm=x, y_mm=y)

    def sipm_list(self) -> list[SiPMEntry]:
        """Return list of all SiPM entries in map order."""
        return list(self._sipms)

    def sipm_by_id(self, sipm_id: int) -> SiPMEntry:
        for e in self._sipms:
            if e.sipm_id == sipm_id:
                return e
        raise KeyError(f"SiPM id {sipm_id} not found in channel map")

    def sipm_position(self, sipm_id: int) -> tuple[float, float]:
        """Return stage coordinates (x_mm, y_mm) including current offset."""
        e = self.sipm_by_id(sipm_id)
        return (e.x_mm + self.position_offset_x_mm,
                e.y_mm + self.position_offset_y_mm)

    def sipm_position_raw(self, sipm_id: int) -> tuple[float, float]:
        """Return channel-map coordinates without the offset applied."""
        e = self.sipm_by_id(sipm_id)
        return e.x_mm, e.y_mm

    def sipm_channel(self, sipm_id: int) -> int:
        return self.sipm_by_id(sipm_id).mux_channel

    def named_position(self, name: str) -> tuple[float, float]:
        """Return stage coordinates (x_mm, y_mm) for 'dark', 'lamp', or 'photodiode'."""
        name = name.lower()
        if name not in self._positions:
            raise KeyError(
                f"Position '{name}' not in channel map. "
                f"Available: {list(self._positions)}"
            )
        e = self._positions[name]
        return (e.x_mm + self.position_offset_x_mm,
                e.y_mm + self.position_offset_y_mm)

    # ------------------------------------------------------------------
    # Coordinate re-zeroing
    # ------------------------------------------------------------------

    def set_origin(self, reference_sipm_id: int,
                   actual_x_mm: float, actual_y_mm: float):
        """
        Set the coordinate offset so that reference_sipm_id maps to
        (actual_x_mm, actual_y_mm) in stage coordinates.

        All other SiPM and named positions shift by the same offset,
        preserving their relative layout.

        Parameters
        ----------
        reference_sipm_id : int
            The SiPM used as the alignment reference.
        actual_x_mm, actual_y_mm : float
            The actual stage position of that SiPM (e.g. found by centroid fit).
        """
        nom_x, nom_y = self.sipm_position_raw(reference_sipm_id)
        self.position_offset_x_mm = actual_x_mm - nom_x
        self.position_offset_y_mm = actual_y_mm - nom_y

    def clear_offset(self):
        """Reset coordinate offset to zero (channel map == stage coordinates)."""
        self.position_offset_x_mm = 0.0
        self.position_offset_y_mm = 0.0

    # ------------------------------------------------------------------
    # InfluxDB token resolution
    # ------------------------------------------------------------------

    def resolved_influx_token(self) -> str:
        """Return token from config or DAQ_INFLUX_TOKEN environment variable."""
        return os.environ.get("DAQ_INFLUX_TOKEN", self.influxdb_token)

    # ------------------------------------------------------------------
    # YAML serialisation
    # ------------------------------------------------------------------

    def to_yaml(self, path: str):
        """Save config to YAML file (excludes loaded channel map data)."""
        try:
            import yaml
        except ImportError:
            raise ImportError("pip install pyyaml")

        data = {k: v for k, v in self.__dict__.items()
                if not k.startswith("_")}
        # Convert numpy arrays / lists for YAML serialisation
        for key in ("temperatures_K", "illuminated_temperatures_K"):
            if isinstance(data.get(key), np.ndarray):
                data[key] = data[key].tolist()

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    @classmethod
    def from_yaml(cls, path: str) -> "ExperimentConfig":
        """Load config from YAML file."""
        try:
            import yaml
        except ImportError:
            raise ImportError("pip install pyyaml")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        # Strip private / unknown keys
        valid = {k for k in cls.__dataclass_fields__ if not k.startswith("_")}
        filtered = {k: v for k, v in data.items() if k in valid}
        cfg = cls(**filtered)

        # Auto-load channel map if file exists next to YAML
        map_path = filtered.get("channel_map_file", cfg.channel_map_file)
        if not os.path.isabs(map_path):
            map_path = os.path.join(os.path.dirname(os.path.abspath(path)), map_path)
        if os.path.exists(map_path):
            cfg.load_channel_map(map_path)

        return cfg


# ---------------------------------------------------------------------------
# Example channel map generator (for testing without hardware)
# ---------------------------------------------------------------------------

def write_example_channel_map(path: str, n_sipms: int = 96, pitch_mm: float = 10.0):
    """
    Write an example channel_map.csv with n_sipms devices on a square grid
    plus dark, lamp, and photodiode positions.
    """
    cols = int(np.ceil(np.sqrt(n_sipms)))
    rows = int(np.ceil(n_sipms / cols))

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["type", "id", "mux_channel", "x_mm", "y_mm"])

        sipm_id = 1
        for row in range(rows):
            for col in range(cols):
                if sipm_id > n_sipms:
                    break
                writer.writerow([
                    "sipm", sipm_id, sipm_id,
                    round(col * pitch_mm, 3),
                    round(row * pitch_mm, 3),
                ])
                sipm_id += 1

        # Special positions outside the device grid
        x_max = (cols - 1) * pitch_mm
        writer.writerow(["dark",       "D", "", round(-3 * pitch_mm, 3), 0.0])
        writer.writerow(["lamp",       "L", "", round(x_max / 2, 3),    round(-3 * pitch_mm, 3)])
        writer.writerow(["photodiode", "P", "", round(-2 * pitch_mm, 3), 0.0])
