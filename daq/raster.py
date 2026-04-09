"""
daq/raster.py

Raster scan — Levels 1 through 3.

Level 1 — scan_point()
    Move stage to (x, y), select MUX channel, set bias, measure N current
    readings.  Returns a PointResult.

Level 2 — raster_scan()
    Sweep a rectangular grid of (x, y) positions for one RasterSpec.
    Grid is boustrophedon (snake order) to minimise stage travel.
    A line scan is a grid with num_y=1 (or num_x=1).
    A single point is num_x=1, num_y=1.
    Returns a RasterResult.

Level 3 — multi_raster()
    Run a list of RasterSpecs in sequence.  Specs can mix different channels,
    different grid shapes, or even the same channel with different patterns
    (e.g. a horizontal line + a box for the same device).
    Returns list[RasterResult].

Helpers
-------
tile_raster_specs()
    Build one RasterSpec per SiPM from the channel map, centred on each
    SiPM's position with a given x/y half-width.  Useful as a starting
    point; the caller can append, remove, or customise individual specs
    before passing to multi_raster().
"""

from __future__ import annotations

import csv
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np

log = logging.getLogger("daq.raster")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RasterSpec:
    """
    Defines one raster (or line, or point) scan.

    Parameters
    ----------
    channel      : MUX channel (1-based)
    bias_v       : Electrometer output voltage during scan
    x_mm         : 1-D array of X stage positions [mm]
    y_mm         : 1-D array of Y stage positions [mm]
    n_per_point  : Number of current readings averaged at each position
    settle_s     : Seconds to wait after each move before measuring
    deenergize_between : De-energize motor coils while measuring
    label        : Optional human-readable label (used in saved files)
    """
    channel:            int
    bias_v:             float
    x_mm:               np.ndarray
    y_mm:               np.ndarray
    n_per_point:        int   = 1
    settle_s:           float = 0.05
    deenergize_between: bool  = True
    label:              str   = ""

    @classmethod
    def linspace(
        cls,
        channel: int,
        bias_v: float,
        x_start: float, x_stop: float, num_x: int,
        y_start: float, y_stop: float, num_y: int,
        n_per_point: int = 1,
        settle_s: float  = 0.05,
        deenergize_between: bool = True,
        label: str = "",
    ) -> "RasterSpec":
        """
        Convenience constructor matching the scanXYIV interface style.

        For a line scan along X: set y_start == y_stop  (or num_y = 1).
        For a line scan along Y: set x_start == x_stop  (or num_x = 1).
        For a single point: num_x = 1, num_y = 1.
        """
        return cls(
            channel  = channel,
            bias_v   = bias_v,
            x_mm     = np.linspace(x_start, x_stop, max(1, num_x)),
            y_mm     = np.linspace(y_start, y_stop, max(1, num_y)),
            n_per_point        = n_per_point,
            settle_s           = settle_s,
            deenergize_between = deenergize_between,
            label              = label,
        )

    @property
    def n_points(self) -> int:
        return len(self.x_mm) * len(self.y_mm)

    @property
    def shape(self) -> tuple:
        return (len(self.x_mm), len(self.y_mm))

    def summary(self) -> str:
        lbl = f" [{self.label}]" if self.label else ""
        return (
            f"ch{self.channel} @ {self.bias_v:.2f}V"
            f"  x[{self.x_mm[0]:.2f}->{self.x_mm[-1]:.2f}, N={len(self.x_mm)}]"
            f"  y[{self.y_mm[0]:.2f}->{self.y_mm[-1]:.2f}, N={len(self.y_mm)}]"
            f"  {self.n_per_point}pt/pos  {self.n_points} total"
            + lbl
        )


@dataclass
class PointResult:
    """Measurement at a single (x, y) position."""
    x_mm:        float
    y_mm:        float
    current_a:   float          # mean of n_per_point readings
    current_std: float          # std of n_per_point readings
    timestamp_s: float
    raw:         np.ndarray     # individual readings


@dataclass
class RasterResult:
    """Outcome of one raster_scan() call."""
    spec:   RasterSpec
    points: List[PointResult] = field(default_factory=list)

    # ---- convenience arrays ------------------------------------------------

    @property
    def x_mm(self) -> np.ndarray:
        return np.array([p.x_mm for p in self.points])

    @property
    def y_mm(self) -> np.ndarray:
        return np.array([p.y_mm for p in self.points])

    @property
    def current_a(self) -> np.ndarray:
        return np.array([p.current_a for p in self.points])

    @property
    def current_std(self) -> np.ndarray:
        return np.array([p.current_std for p in self.points])

    def as_grid(self) -> tuple:
        """
        Return (X, Y, I, I_std) as 2-D arrays shaped (num_x, num_y).

        Only well-defined if points were collected on a rectangular grid
        (no missing points).  Raises ValueError otherwise.
        """
        nx, ny = self.spec.shape
        if len(self.points) != nx * ny:
            raise ValueError(
                f"Expected {nx*ny} points, got {len(self.points)}. "
                "Grid is incomplete — cannot reshape."
            )
        X   = self.x_mm.reshape(nx, ny)
        Y   = self.y_mm.reshape(nx, ny)
        I   = self.current_a.reshape(nx, ny)
        Ist = self.current_std.reshape(nx, ny)
        return X, Y, I, Ist

    def to_csv(self, path: str):
        """Save results to CSV with a header block."""
        with open(path, "w", newline="") as f:
            f.write(
                f"# label={self.spec.label}, channel={self.spec.channel}, "
                f"bias_v={self.spec.bias_v}, n_per_point={self.spec.n_per_point}\n"
            )
            writer = csv.writer(f)
            writer.writerow(["x_mm", "y_mm", "current_a", "current_std", "timestamp_s"])
            for p in self.points:
                writer.writerow([p.x_mm, p.y_mm, p.current_a, p.current_std, p.timestamp_s])


# ---------------------------------------------------------------------------
# Centroid helpers
# ---------------------------------------------------------------------------

def centroid_1d(result: "RasterResult") -> tuple[float, float]:
    """
    Compute the weighted centroid of a line scan.

    Uses |current| as the weight at each position.  For a Gaussian beam
    profile the centroid of the integrated response (error function) is the
    beam centre, so this gives the stage position of the SiPM.

    Parameters
    ----------
    result : RasterResult from a line scan (num_x=1 or num_y=1).

    Returns
    -------
    (centroid_x_mm, centroid_y_mm)
        One of the two values will be constant (the fixed axis of the line).
    """
    if not result.points:
        raise ValueError("RasterResult has no points")

    weights = np.abs(result.current_a)
    w_sum   = weights.sum()
    if w_sum == 0:
        raise ValueError("All currents are zero — cannot compute centroid")

    cx = float(np.sum(weights * result.x_mm) / w_sum)
    cy = float(np.sum(weights * result.y_mm) / w_sum)
    return cx, cy


def centroid_2d(result: "RasterResult") -> tuple[float, float]:
    """
    Compute the 2-D weighted centroid of a box scan.

    Uses |current| as the weight.  Works for any rectangular grid.

    Returns
    -------
    (centroid_x_mm, centroid_y_mm)
    """
    return centroid_1d(result)   # same formula works in 2D


# ---------------------------------------------------------------------------
# Level 1 — single point
# ---------------------------------------------------------------------------

def scan_point(
    stage,
    mux,
    elec,
    x_mm: float,
    y_mm: float,
    channel: int,
    bias_v: float,
    n_per_point: int = 1,
    settle_s: float  = 0.05,
    deenergize_after: bool = True,
) -> PointResult:
    """
    Move to (x_mm, y_mm), select MUX channel, set bias, measure current.

    Parameters
    ----------
    stage, mux, elec  : connected instrument objects
    deenergize_after  : de-energize motor coils before measuring
    """
    from daq.primitives import (
        move_stage, select_channel, set_bias, measure_current,
        deenergize_stage, energize_stage,
    )

    # Move
    move_stage(stage, x_mm, y_mm, deenergize_after=False)

    # Channel + bias
    select_channel(mux, channel, settle_s=0.0)
    set_bias(elec, bias_v, settle_s=settle_s)

    # De-energize if requested
    if deenergize_after and stage is not None:
        deenergize_stage(stage)

    # Measure
    readings = np.array([measure_current(elec) for _ in range(n_per_point)])

    # Re-energize so stage can move next time
    if deenergize_after and stage is not None:
        energize_stage(stage)

    return PointResult(
        x_mm        = x_mm,
        y_mm        = y_mm,
        current_a   = float(np.mean(readings)),
        current_std = float(np.std(readings, ddof=min(1, len(readings) - 1))),
        timestamp_s = time.time(),
        raw         = readings,
    )


# ---------------------------------------------------------------------------
# Level 2 — single raster
# ---------------------------------------------------------------------------

def raster_scan(
    stage,
    mux,
    elec,
    spec: RasterSpec,
    on_progress: Optional[Callable] = None,
) -> RasterResult:
    """
    Sweep a grid of (x, y) positions in boustrophedon (snake) order.

    Boustrophedon pattern: scan Y forward for x[0], Y reversed for x[1], etc.
    This matches the original scanXYIV.py pattern and minimises Y-travel.

    Parameters
    ----------
    on_progress : callable(done, total, point_result) called after each point
    """
    from daq.primitives import (
        move_stage, select_channel, set_bias, measure_current,
        deenergize_stage, energize_stage,
    )

    result = RasterResult(spec=spec)
    nx, ny = len(spec.x_mm), len(spec.y_mm)
    total  = nx * ny
    done   = 0

    log.info("raster_scan: ch%d @ %.2fV  shape=(%d, %d)  label=%r",
             spec.channel, spec.bias_v, nx, ny, spec.label)

    # Select channel once at the start (stays selected throughout scan)
    select_channel(mux, spec.channel, settle_s=0.0)
    set_bias(elec, spec.bias_v, settle_s=0.05)

    for ix, x in enumerate(spec.x_mm):
        # Alternate Y direction each row (boustrophedon)
        y_row = spec.y_mm if ix % 2 == 0 else spec.y_mm[::-1]

        for y in y_row:
            move_stage(stage, x, y, deenergize_after=False)

            if spec.deenergize_between and stage is not None:
                deenergize_stage(stage)

            if spec.settle_s > 0:
                time.sleep(spec.settle_s)

            readings = np.array([measure_current(elec)
                                 for _ in range(spec.n_per_point)])

            if spec.deenergize_between and stage is not None:
                energize_stage(stage)

            pt = PointResult(
                x_mm        = x,
                y_mm        = y,
                current_a   = float(np.mean(readings)),
                current_std = float(np.std(readings, ddof=min(1, len(readings) - 1))),
                timestamp_s = time.time(),
                raw         = readings,
            )
            result.points.append(pt)
            done += 1

            if on_progress:
                on_progress(done, total, pt)

    log.info("raster_scan done: %d points", done)
    return result


# ---------------------------------------------------------------------------
# Level 3 — list of rasters
# ---------------------------------------------------------------------------

def multi_raster(
    stage,
    mux,
    elec,
    specs: List[RasterSpec],
    on_progress: Optional[Callable] = None,
) -> List[RasterResult]:
    """
    Run a list of RasterSpecs in sequence.

    Specs may have different channels, different shapes, or be mixed
    patterns for the same channel (e.g. a line scan + a box scan).

    Parameters
    ----------
    on_progress : callable(spec_idx, n_specs, done, total, point_result)
    """
    results = []
    for i, spec in enumerate(specs):
        log.info("multi_raster: spec %d/%d  %s", i + 1, len(specs), spec.summary())

        def _prog(done, total, pt, _i=i):
            if on_progress:
                on_progress(_i, len(specs), done, total, pt)

        results.append(raster_scan(stage, mux, elec, spec, on_progress=_prog))

    return results


# ---------------------------------------------------------------------------
# Helper — build specs from tile channel map
# ---------------------------------------------------------------------------

def tile_raster_specs(
    config,
    x_width_mm: float,
    y_width_mm: float,
    num_x: int,
    num_y: int,
    bias_v: float,
    n_per_point: int = 1,
    settle_s: float  = 0.05,
    deenergize_between: bool = True,
) -> List[RasterSpec]:
    """
    Build one RasterSpec per SiPM centred on its channel-map position.

    The scan for SiPM at (cx, cy) runs from
        x: [cx - x_width/2, cx + x_width/2]  with num_x points
        y: [cy - y_width/2, cy + y_width/2]  with num_y points

    For a line scan along X set y_width_mm = 0 (or num_y = 1).
    For a line scan along Y set x_width_mm = 0 (or num_x = 1).

    Returns a list that can be passed directly to multi_raster() or
    customised further before running.
    """
    specs = []
    for sipm in config.sipm_list():
        specs.append(RasterSpec.linspace(
            channel  = sipm.mux_channel,
            bias_v   = bias_v,
            x_start  = sipm.x_mm - x_width_mm / 2,
            x_stop   = sipm.x_mm + x_width_mm / 2,
            num_x    = max(1, num_x),
            y_start  = sipm.y_mm - y_width_mm / 2,
            y_stop   = sipm.y_mm + y_width_mm / 2,
            num_y    = max(1, num_y),
            n_per_point        = n_per_point,
            settle_s           = settle_s,
            deenergize_between = deenergize_between,
            label              = f"SiPM_{sipm.sipm_id}",
        ))
    return specs
