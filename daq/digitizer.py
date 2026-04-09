"""
daq/digitizer.py

Common digitizer interface for the ETS DAQ.

Both the R&S RTO2024 oscilloscope and the CAEN VX2740 digitizer are supported
as drop-in interchangeable backends.  Downstream code only imports this module
and never calls instrument-specific APIs directly.

Usage
-----
    from daq.digitizer import make_digitizer

    dig = make_digitizer("rto2024", address="192.168.0.2", mode="simulation")
    # or
    dig = make_digitizer("vx2740",  address="192.168.0.1", mode="simulation")

    with dig:
        dig.setup(channels=[0], pre_us=2.0, post_us=10.0, threshold_v=0.010)
        result = dig.run(n_waveforms=1000)
        # result.amplitudes_v[ch] -> np.ndarray of pulse amplitudes in VOLTS
        # result.timestamps[ch]   -> np.ndarray of arrival times in seconds

Unit normalisation
------------------
VX2740 amplitudes are in ADC counts.  This module converts them to volts using
the instrument's full-scale range (2 V full-scale, 14-bit ADC → 1 LSB ≈ 122 µV).
RTO2024 amplitudes are already in volts.  After normalisation `amplitudes_v`
is always in volts regardless of backend.

The raw `amplitudes` field in the underlying AcquisitionResult is preserved
unchanged (counts for VX2740, volts for RTO2024); `amplitudes_v` is the
normalised field to use in analysis.
"""

import sys
import os
import numpy as np
from dataclasses import dataclass, field
import time

# ---------------------------------------------------------------------------
# VX2740 hardware constants (used for ADC→V conversion)
# ---------------------------------------------------------------------------

VX2740_FULL_SCALE_V   = 2.0      # ± 1 V input range (2 V full-scale)
VX2740_ADC_BITS       = 14       # 14-bit ADC
VX2740_ADC_COUNTS     = 2 ** VX2740_ADC_BITS   # 16384
VX2740_COUNTS_PER_V   = VX2740_ADC_COUNTS / VX2740_FULL_SCALE_V   # 8192 counts/V


# ---------------------------------------------------------------------------
# Normalised result container
# ---------------------------------------------------------------------------

@dataclass
class DigitizerResult:
    """
    Normalised result from either digitizer backend.

    All amplitude arrays are in **volts**, regardless of which instrument
    was used.

    Fields
    ------
    amplitudes_v : dict[ch -> np.ndarray float32]
        Pulse amplitudes in volts.
    timestamps   : dict[ch -> np.ndarray float64]
        Pulse arrival times in seconds (relative to run start).
    waveforms_v  : dict[ch -> np.ndarray float32], shape (N, n_samples)
        Raw waveforms in volts (empty if not stored).
    time_axis    : np.ndarray float32 | None
        Time axis for waveforms in seconds (None if not stored).
    n_waveforms  : int
    source       : str      — "rto_measure", "rto_waveform", "rto_simulation",
                              "vx2740", "vx2740_simulation"
    run_timestamp : float   — Unix time at acquisition start
    bias_voltage_V : float  — set externally before saving
    temperature_K  : float  — set externally before saving
    channel_ids    : list[int]
    """
    amplitudes_v:   dict  = field(default_factory=dict)
    timestamps:     dict  = field(default_factory=dict)
    waveforms_v:    dict  = field(default_factory=dict)
    time_axis:      object = None
    n_waveforms:    int    = 0
    source:         str    = "unknown"
    run_timestamp:  float  = field(default_factory=time.time)
    bias_voltage_V: float  = 0.0
    temperature_K:  float  = 0.0
    channel_ids:    list   = field(default_factory=list)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _counts_to_volts(arr: np.ndarray) -> np.ndarray:
    """Convert VX2740 ADC counts (already baseline-subtracted) to volts."""
    return arr.astype(np.float32) / VX2740_COUNTS_PER_V


def _rto_result_to_normalised(raw) -> DigitizerResult:
    """Wrap an RTO2024 AcquisitionResult into a DigitizerResult."""
    result = DigitizerResult(
        n_waveforms   = raw.n_waveforms,
        source        = raw.source,
        run_timestamp = raw.run_timestamp,
        channel_ids   = list(raw.channel_ids),
        time_axis     = raw.time_axis,
    )
    for ch in raw.channel_ids:
        result.amplitudes_v[ch] = raw.amplitudes.get(ch, np.array([], dtype=np.float32))
        result.timestamps[ch]   = raw.timestamps.get(ch, np.array([], dtype=np.float64))
        if ch in raw.waveforms:
            result.waveforms_v[ch] = raw.waveforms[ch]   # already in volts
    return result


def _vx_result_to_normalised(raw) -> DigitizerResult:
    """Wrap a VX2740 AcquisitionResult into a DigitizerResult."""
    src = "vx2740_simulation" if "sim" in getattr(raw, "source", "") else "vx2740"
    result = DigitizerResult(
        n_waveforms   = raw.n_waveforms,
        source        = src,
        run_timestamp = raw.run_timestamp,
        channel_ids   = list(raw.channel_ids),
        time_axis     = getattr(raw, "time_axis", None),
    )
    for ch in raw.channel_ids:
        amp_counts = raw.amplitudes.get(ch, np.array([], dtype=np.float32))
        result.amplitudes_v[ch] = _counts_to_volts(amp_counts)
        result.timestamps[ch]   = raw.timestamps.get(ch, np.array([], dtype=np.float64))
        if ch in getattr(raw, "waveforms", {}):
            result.waveforms_v[ch] = _counts_to_volts(raw.waveforms[ch])
    return result


# ---------------------------------------------------------------------------
# Backend wrappers
# ---------------------------------------------------------------------------

class _RTO2024Backend:
    """
    Thin wrapper around RTO2024Controller that presents the common interface.
    """

    def __init__(self, address: str, mode: str):
        # Import lazily so this file doesn't hard-require rto2024 to be installed
        _add_path("RTO2024-python")
        from rto2024.controller import RTO2024Controller
        self._ctrl = RTO2024Controller(address=address, mode=mode)
        self._channels = [1]

    def connect(self):    self._ctrl.connect()
    def disconnect(self): self._ctrl.disconnect()
    def identify(self) -> str: return self._ctrl.identify()

    def setup(self, channels: list[int],
              pre_us: float   = 2.0,
              post_us: float  = 10.0,
              threshold_v: float = 0.010,
              scale_v: float  = 0.05,
              acquisition_mode: str = "measure"):
        self._channels = channels
        for ch in channels:
            self._ctrl.configure_channel(ch, scale_v=scale_v)
        self._ctrl.configure_record_window(pre_us=pre_us, post_us=post_us)
        self._ctrl.configure_trigger(
            source=channels[0], level_v=threshold_v, slope="POS"
        )
        self._ctrl.configure_acquisition_mode(acquisition_mode)
        self._ctrl.configure_pulse_finding(threshold_v=threshold_v)

    def run(self, n_waveforms: int, timeout_s: float = 120.0) -> DigitizerResult:
        raw = self._ctrl.run(n_waveforms=n_waveforms, timeout_s=timeout_s)
        return _rto_result_to_normalised(raw)

    def __enter__(self):  self.connect();    return self
    def __exit__(self, *_): self.disconnect()


class _VX2740Backend:
    """
    Thin wrapper around VX2740Controller that presents the common interface.
    """

    def __init__(self, address: str, mode: str):
        _add_path("vx2740-python")
        from vx2740.controller import VX2740Controller
        self._ctrl = VX2740Controller(address=address, mode=mode)
        self._channels = [0]

    def connect(self):    self._ctrl.connect()
    def disconnect(self): self._ctrl.disconnect()
    def identify(self) -> str: return self._ctrl.identify()

    def setup(self, channels: list[int],
              pre_us: float   = 2.0,
              post_us: float  = 10.0,
              threshold_v: float = 0.010,
              scale_v: float  = 0.05,       # ignored for VX2740 (fixed range)
              acquisition_mode: str = "measure"):  # ignored for VX2740
        self._channels = channels
        threshold_counts = int(threshold_v * VX2740_COUNTS_PER_V)
        self._ctrl.configure_record_window(pre_us=pre_us, post_us=post_us)
        self._ctrl.configure_channels(
            sipm_channels   = channels,
            threshold_mode  = "global",
            global_threshold = threshold_counts,
            include_pmt     = False,
        )

    def run(self, n_waveforms: int, timeout_s: float = 120.0) -> DigitizerResult:
        raw = self._ctrl.run(n_waveforms=n_waveforms, timeout_s=timeout_s)
        return _vx_result_to_normalised(raw)

    def __enter__(self):  self.connect();    return self
    def __exit__(self, *_): self.disconnect()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_digitizer(instrument: str,
                   address: str = "",
                   mode: str = "simulation") -> "_RTO2024Backend | _VX2740Backend":
    """
    Create a digitizer backend.

    Parameters
    ----------
    instrument : str
        "rto2024"  — R&S RTO2024 oscilloscope
        "vx2740"   — CAEN VX2740 digitizer
    address : str
        IP address of the instrument.
    mode : str
        "hardware" or "simulation".

    Returns
    -------
    Backend object with the common interface:
      .connect() / .disconnect()
      .identify() -> str
      .setup(channels, pre_us, post_us, threshold_v, ...)
      .run(n_waveforms) -> DigitizerResult

    Example
    -------
        dig = make_digitizer("rto2024", address="192.168.0.2", mode="simulation")
        with dig:
            dig.setup(channels=[1], pre_us=2.0, post_us=10.0, threshold_v=0.010)
            result = dig.run(1000)
            # result.amplitudes_v[1] -> np.ndarray, volts
    """
    key = instrument.lower().replace("-", "").replace("_", "")
    if key == "rto2024":
        return _RTO2024Backend(address=address, mode=mode)
    elif key == "vx2740":
        return _VX2740Backend(address=address, mode=mode)
    else:
        raise ValueError(
            f"Unknown instrument {instrument!r}. Choose 'rto2024' or 'vx2740'."
        )


# ---------------------------------------------------------------------------
# Path helper (adds sibling package directories to sys.path)
# ---------------------------------------------------------------------------

def _add_path(subdir: str):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg  = os.path.join(root, subdir)
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
