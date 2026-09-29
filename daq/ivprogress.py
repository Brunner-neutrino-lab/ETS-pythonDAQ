"""Thread-neutral aggregation for live Level-3 IV progress."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class IVSampleProgress:
    entry_idx: int
    n_entries: int
    illuminated: bool
    voltage: float
    current_a: float
    sample_index: int
    sample_total: int
    voltage_sample_index: int
    n_per_voltage: int


@dataclass(frozen=True)
class IVPointSummary:
    entry_idx: int
    illuminated: bool
    voltage: float
    mean_a: float
    std_a: float
    n: int


class IVLiveAccumulator:
    """Turn worker-thread chunks into one latest sample and point summaries."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._active_key = None
        self._sample_index = 0
        self._point_values = {}
        self._completed = set()

    def add(self, entry_idx, n_entries, illuminated, voltage, currents,
            n_per_voltage, n_voltages):
        key = (int(entry_idx), bool(illuminated))
        if key != self._active_key:
            self.reset()
            self._active_key = key

        voltage = float(voltage)
        values = self._point_values.setdefault(voltage, [])
        latest = None
        for current in currents:
            values.append(float(current))
            self._sample_index += 1
            latest = IVSampleProgress(
                entry_idx=int(entry_idx),
                n_entries=int(n_entries),
                illuminated=bool(illuminated),
                voltage=voltage,
                current_a=float(current),
                sample_index=self._sample_index,
                sample_total=int(n_per_voltage) * int(n_voltages),
                voltage_sample_index=len(values),
                n_per_voltage=int(n_per_voltage),
            )

        summary = None
        if len(values) >= int(n_per_voltage) and voltage not in self._completed:
            self._completed.add(voltage)
            summary = IVPointSummary(
                entry_idx=int(entry_idx),
                illuminated=bool(illuminated),
                voltage=voltage,
                mean_a=statistics.fmean(values),
                std_a=statistics.pstdev(values) if len(values) > 1 else 0.0,
                n=len(values),
            )
        return latest, summary


def format_sample_status(sample: IVSampleProgress) -> str:
    condition = "bright" if sample.illuminated else "dark"
    return (
        f"entry {sample.entry_idx + 1}/{sample.n_entries} · {condition} IV · "
        f"sample {sample.sample_index}/{sample.sample_total} · "
        f"V={sample.voltage:.3f} V · I={sample.current_a:.3e} A"
    )


def format_point_summary(summary: IVPointSummary) -> str:
    condition = "bright" if summary.illuminated else "dark"
    return (
        f"entry {summary.entry_idx + 1} · {condition} · "
        f"V={summary.voltage:.3f} V · mean={summary.mean_a:.3e} A · "
        f"std={summary.std_a:.3e} A · n={summary.n}"
    )


def parse_iv_timeout(value) -> float:
    try:
        timeout_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("IV max time must be a positive number") from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("IV max time must be greater than zero")
    return timeout_s
