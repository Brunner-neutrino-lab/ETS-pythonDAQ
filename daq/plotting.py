"""
daq/plotting.py

Plot library for bench-test HDF5 files.

Design goals
------------
The same plot functions must serve three callers:

    1. The `scripts/plot_bench.py` CLI — picks a plot type, writes PNG to
       `plots/`.
    2. A future NiceGUI plot page — calls the same function into a
       `ui.matplotlib()` figure.
    3. Ad-hoc exploration in a Jupyter notebook.

So each plot function follows one signature:

    plot_NAME(group_or_file, ax=None, label=None, **opts) -> matplotlib.axes.Axes

  - First positional arg can be an h5py.Group OR an h5py.File OR a path.
    `_resolve_group(...)` does the right thing.
  - `ax` is provided by the caller; if None, a new figure is created.
    The CLI then saves the parent figure; NiceGUI gets back the Axes
    that already lives inside its `ui.matplotlib()` figure.
  - `label` becomes the legend entry — used for overlay across files.
  - Returns the Axes so callers can do further customisation.

Overlay
-------
`overlay_plots(plot_fn, sources, ax=None, **opts)` calls the same plot
function for each `(label, source)` in `sources` and shows the legend.
Sources can be HDF5 paths or `(path, attrs_override)` tuples.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

import h5py
import numpy as np
import matplotlib
matplotlib.use("Agg")   # safe default for headless CLI; NiceGUI uses its own backend
import matplotlib.pyplot as plt

# Accept any of: h5py.Group, h5py.File, str path, Path
GroupLike = Union[h5py.Group, h5py.File, str, Path]


# ---------------------------------------------------------------------------
# Theme — match the xsphere/DAQ "register" palette so embedded plots look right
# ---------------------------------------------------------------------------

ACCENT = "#58a6ff"
MUT    = "#8a93a6"
OK     = "#3fb950"
WARN   = "#d29922"
BAD    = "#f85149"
PANEL  = "#1b2230"
BG     = "#11151c"
LINE   = "#2d3648"


def apply_dark_style(fig, ax):
    """Apply the dark 'register' style to a single (fig, ax). Idempotent."""
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    ax.tick_params(colors="#dde3ee")
    for spine in ax.spines.values():
        spine.set_color(LINE)
    ax.title.set_color("#dde3ee")
    ax.xaxis.label.set_color("#dde3ee")
    ax.yaxis.label.set_color("#dde3ee")
    ax.grid(True, color=LINE, alpha=0.5)


def _new_ax(figsize=(8, 4)):
    fig, ax = plt.subplots(figsize=figsize)
    apply_dark_style(fig, ax)
    return ax


def _resolve_group(src: GroupLike, sub: Optional[str] = None) -> h5py.Group:
    """Take a Group, File, or path; optionally descend into a named subgroup."""
    if isinstance(src, (str, Path)):
        f = h5py.File(str(src), "r")
        # Caller is responsible for closing; for CLI use, lifetime is fine
        # because plt is the only consumer and we plot eagerly.
        src = f
    if sub is not None:
        return src[sub]
    return src


# ===========================================================================
# Individual plot functions
# Each: plot_X(src, ax=None, label=None, **opts) -> Axes
# ===========================================================================

def plot_iv(src: GroupLike, ax=None, label: Optional[str] = None,
            log_y: bool = True, mark_vbd: bool = True, **opts) -> plt.Axes:
    """Dark IV curve from /iv/. Log-Y, |I| vs V. Marks V_BD if available."""
    if isinstance(src, h5py.Group) and src.name.rstrip("/").endswith("/iv"):
        g = src
    else:
        f = _open(src)
        if "iv" not in f:
            ax = ax if ax is not None else _new_ax()
            ax.text(0.5, 0.5, "no /iv group", ha="center", va="center",
                    color=MUT, transform=ax.transAxes)
            return ax
        g = f["iv"]

    v = g["source_v"][:]
    i = g["current_a"][:]
    valid = np.abs(i) < 1e-3
    v, i = v[valid], i[valid]

    ax = ax if ax is not None else _new_ax()
    ax.plot(v, np.abs(i), "o-", ms=3, lw=1, color=ACCENT, label=label or "dark IV")
    if log_y:
        ax.set_yscale("log")
    if mark_vbd and "v_bd_estimate" in g.attrs:
        vbd = float(g.attrs["v_bd_estimate"])
        ax.axvline(vbd, color=WARN, ls="--", lw=1, alpha=0.8,
                   label=f"V$_{{BD}}$ ≈ {vbd:.1f} V" if label is None else None)
    ax.set_xlabel("bias (V)"); ax.set_ylabel("|current| (A)")
    ax.set_title("dark IV")
    if label or mark_vbd: ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee")
    return ax


def plot_k6485_bars(src: GroupLike, ax=None, label: Optional[str] = None,
                    bias_group: str = "above_vbd", **opts) -> plt.Axes:
    """Bar plot of K6485 dark vs light mean current at one bias.

    bias_group: 'above_vbd' (default — SPAD gain on) or 'below_vbd' (control).
    """
    f = _open(src)
    if "k6485" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /k6485 group", ha="center", va="center",
                color=MUT, transform=ax.transAxes)
        return ax
    # Backwards compat: an older HDF5 layout had /k6485/dark and /k6485/light
    # directly. New layout has /k6485/<bias_group>/dark and .../light.
    if bias_group in f["k6485"]:
        sub = f["k6485"][bias_group]
    else:
        sub = f["k6485"]    # old layout

    if "dark" not in sub or "light" not in sub:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no dark/light pair in /k6485/{bias_group}",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax

    ax = ax if ax is not None else _new_ax(figsize=(6, 4))
    dark  = sub["dark/current_a"][:]
    light = sub["light/current_a"][:]
    means = [float(np.mean(dark)),  float(np.mean(light))]
    errs  = [float(np.std(dark, ddof=1))  if len(dark)  > 1 else 0.0,
             float(np.std(light, ddof=1)) if len(light) > 1 else 0.0]
    xpos = [0, 1]
    colors = [MUT, ACCENT]
    bars = ax.bar(xpos, means, yerr=errs, capsize=6,
                  color=colors, edgecolor=LINE)
    ax.set_xticks(xpos); ax.set_xticklabels(["LED off", "LED on"], color="#dde3ee")
    ax.set_ylabel("mean current (A)")
    bias_v = float(sub.attrs.get("bias_v", 0.0))
    delta  = means[1] - means[0]
    title = f"K6485 @ {bias_v:.2f} V  (Δ = {delta:+.2e} A)"
    if label: title = f"{label}: {title}"
    ax.set_title(title)
    # Annotate values
    for x, m, e in zip(xpos, means, errs):
        ax.text(x, m, f"  {m:.2e}", ha="left", va="center",
                color="#dde3ee", fontsize=9)
    return ax


def plot_k6485_timeseries(src: GroupLike, ax=None, label: Optional[str] = None,
                          bias_group: str = "above_vbd", **opts) -> plt.Axes:
    """Per-sample K6485 readings vs time for dark and light at one bias."""
    f = _open(src)
    if "k6485" not in f or bias_group not in f["k6485"]:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no /k6485/{bias_group}", ha="center", va="center",
                color=MUT, transform=ax.transAxes)
        return ax
    sub = f["k6485"][bias_group]
    ax = ax if ax is not None else _new_ax()

    if "dark" in sub:
        cur = sub["dark/current_a"][:]
        ts  = sub["dark/timestamp_s"][:]
        t0  = ts[0] if len(ts) else 0.0
        ax.plot(ts - t0, cur, "o-", ms=4, lw=1, color=MUT,
                label=f"{label+' ' if label else ''}LED off")
    if "light" in sub:
        cur = sub["light/current_a"][:]
        ts  = sub["light/timestamp_s"][:]
        t0  = ts[0] if len(ts) else 0.0
        ax.plot(ts - t0, cur, "s-", ms=4, lw=1, color=ACCENT,
                label=f"{label+' ' if label else ''}LED on")
    bias_v = float(sub.attrs.get("bias_v", 0.0))
    ax.set_xlabel("time within block (s)")
    ax.set_ylabel("current (A)")
    ax.set_title(f"K6485 samples @ {bias_v:.2f} V")
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee")
    return ax


def plot_waveform(src: GroupLike, ax=None, label: Optional[str] = None,
                  channel: int = 0, index: int = 0,
                  baseline_subtract: bool = True,
                  group: str = "vx2740", **opts) -> plt.Axes:
    """One VX2740 waveform from /<group>/ch<N>/waveforms[index]."""
    f = _open(src)
    if group not in f or f"ch{channel}" not in f[group]:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no /{group}/ch{channel}",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    chg = f[group][f"ch{channel}"]
    if "waveforms" not in chg:
        # Try swtrig_probe fallback
        if "swtrig_probe" in f.get(group, {}):
            chg = f[group]["swtrig_probe"]
        else:
            ax = ax if ax is not None else _new_ax()
            ax.text(0.5, 0.5, f"no waveforms stored in /{group}/ch{channel}",
                    ha="center", va="center", color=MUT, transform=ax.transAxes)
            return ax

    wfs = chg["waveforms"]
    n = wfs.shape[0]
    idx = max(0, min(int(index), n - 1))
    w   = np.asarray(wfs[idx], dtype=np.float64)
    if baseline_subtract:
        base = w[: len(w) // 4].mean()
        w    = w - base

    # 125 MS/s → 8 ns / sample
    t_us = np.arange(len(w)) / 125.0e6 * 1e6
    ax = ax if ax is not None else _new_ax(figsize=(9, 3.5))
    ax.plot(t_us, w, lw=1, color=ACCENT,
            label=label or f"ch{channel} #{idx}")
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("ADC counts (baseline-subtracted)" if baseline_subtract else "ADC counts")
    title = f"waveform ch{channel} #{idx}"
    if label: title = f"{label}: {title}"
    ax.set_title(title)
    return ax


def plot_mean_waveform(src: GroupLike, ax=None, label: Optional[str] = None,
                       channel: int = 0,
                       baseline_subtract: bool = True,
                       group: str = "vx2740", **opts) -> plt.Axes:
    """Average waveform across all stored waveforms of a channel."""
    f = _open(src)
    if group not in f or f"ch{channel}" not in f[group]:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no /{group}/ch{channel}",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    wfs = f[group][f"ch{channel}"].get("waveforms")
    if wfs is None or wfs.shape[0] == 0:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no stored waveforms in /{group}/ch{channel}",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax

    w = np.asarray(wfs[:], dtype=np.float64).mean(axis=0)
    if baseline_subtract:
        base = w[: len(w) // 4].mean()
        w   -= base

    t_us = np.arange(len(w)) / 125.0e6 * 1e6
    ax = ax if ax is not None else _new_ax(figsize=(9, 3.5))
    ax.plot(t_us, w, lw=1.5, color=OK,
            label=label or f"ch{channel} mean")
    ax.set_xlabel("time (µs)")
    ax.set_ylabel("mean ADC counts (baseline-subtracted)" if baseline_subtract else "mean ADC counts")
    ax.set_title(f"mean waveform ch{channel}  (N={wfs.shape[0]})"
                 if not label else f"{label}: mean ch{channel}")
    return ax


def plot_spectrum(src: GroupLike, ax=None, label: Optional[str] = None,
                  channel: int = 0, bins: int = 100,
                  group: str = "vx2740", log_y: bool = False, **opts) -> plt.Axes:
    """Pulse-amplitude histogram for a channel."""
    f = _open(src)
    if group not in f or f"ch{channel}" not in f[group]:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"no /{group}/ch{channel}",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    amps = f[group][f"ch{channel}"]["amplitudes_adc"][:]
    ax = ax if ax is not None else _new_ax()
    ax.hist(amps, bins=int(bins), color=ACCENT, alpha=0.85,
            edgecolor=PANEL, linewidth=0.5,
            label=label or f"ch{channel}")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("amplitude (ADC counts)")
    ax.set_ylabel("counts")
    title = f"spectrum ch{channel}  (N={amps.size})"
    if label: title = f"{label}: {title}"
    ax.set_title(title)
    return ax


def plot_overvoltage_scan(src: GroupLike, ax=None, label: Optional[str] = None,
                          **opts) -> plt.Axes:
    """Mean pulse amplitude vs over-voltage from /vx2740_ov_scan/."""
    f = _open(src)
    if "vx2740_ov_scan" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_ov_scan group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_ov_scan"]
    ovs, means, stds, biases = [], [], [], []
    for name in g.keys():
        sub = g[name]
        if not hasattr(sub, "attrs"): continue
        if "over_voltage" not in sub.attrs: continue
        ovs.append(float(sub.attrs["over_voltage"]))
        means.append(float(sub.attrs.get("mean_amp", float("nan"))))
        stds.append(float(sub.attrs.get("std_amp",  float("nan"))))
        biases.append(float(sub.attrs.get("bias_v", float("nan"))))
    order = np.argsort(ovs)
    ovs   = np.array(ovs)[order]
    means = np.array(means)[order]
    stds  = np.array(stds)[order]

    ax = ax if ax is not None else _new_ax()
    ax.errorbar(ovs, means, yerr=stds, fmt="o-", ms=5, lw=1.5,
                capsize=3, color=ACCENT, ecolor=MUT,
                label=label or "mean amp")
    ax.set_xlabel("over-voltage V$_{BIAS}$ − V$_{BD}$ (V)")
    ax.set_ylabel("mean pulse amplitude (ADC counts)")
    ax.set_title("VX2740 over-voltage scan: gain curve" if label is None else label)
    return ax


def plot_overvoltage_spectra(src: GroupLike, ax=None, label: Optional[str] = None,
                              bins: int = 80, log_y: bool = False, **opts) -> plt.Axes:
    """Overlay of amplitude spectra at each over-voltage point."""
    f = _open(src)
    if "vx2740_ov_scan" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_ov_scan group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    ax = ax if ax is not None else _new_ax()
    g  = f["vx2740_ov_scan"]
    # Sort by OV so colours go cool → warm
    entries = []
    for name in g.keys():
        sub = g[name]
        if "amplitudes_adc" not in sub: continue
        entries.append((float(sub.attrs["over_voltage"]), name))
    entries.sort()
    cmap = plt.get_cmap("viridis")
    for k, (ov, name) in enumerate(entries):
        sub = g[name]
        amps = sub["amplitudes_adc"][:]
        if not len(amps): continue
        c = cmap(k / max(1, len(entries) - 1))
        ax.hist(amps, bins=int(bins), histtype="step", lw=1.2, color=c,
                label=f"OV {ov:+.1f} V  (N={len(amps)})")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("amplitude (ADC counts)")
    ax.set_ylabel("counts")
    ax.set_title("VX2740 amplitude spectra vs over-voltage"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee",
              fontsize=9, ncol=1)
    return ax


# ---------------------------------------------------------------------------
# Overlay helper
# ---------------------------------------------------------------------------

def overlay_plots(plot_fn: Callable, sources: Iterable[tuple[str, GroupLike]],
                   ax=None, **opts) -> plt.Axes:
    """Call `plot_fn` for each (label, source) pair on a single Axes.

    plot_fn must accept (src, ax=ax, label=label, **opts).
    """
    ax = ax if ax is not None else _new_ax()
    for label, src in sources:
        plot_fn(src, ax=ax, label=label, **opts)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


# ---------------------------------------------------------------------------
# Registry — used by the CLI and the future web page to enumerate plot types
# ---------------------------------------------------------------------------

PLOTS: dict[str, dict[str, Any]] = {
    "iv": {
        "fn":   plot_iv,
        "desc": "Dark IV curve (log |I| vs V), with V_BD marker",
    },
    "k6485_bars": {
        "fn":   plot_k6485_bars,
        "desc": "K6485 dark vs light bar chart; bias_group=below_vbd|above_vbd",
    },
    "k6485_ts": {
        "fn":   plot_k6485_timeseries,
        "desc": "K6485 per-sample time series for dark+light at one bias",
    },
    "waveform": {
        "fn":   plot_waveform,
        "desc": "Single VX2740 waveform (channel, index)",
    },
    "mean_waveform": {
        "fn":   plot_mean_waveform,
        "desc": "Average VX2740 waveform across all stored captures",
    },
    "spectrum": {
        "fn":   plot_spectrum,
        "desc": "VX2740 pulse-amplitude histogram for a channel",
    },
    "ov_scan": {
        "fn":   plot_overvoltage_scan,
        "desc": "Over-voltage scan: mean pulse amplitude vs V−V_BD",
    },
    "ov_spectra": {
        "fn":   plot_overvoltage_spectra,
        "desc": "Over-voltage scan: amplitude-spectrum family overlay",
    },
}


# ---------------------------------------------------------------------------
# File-handle helper — keep file alive while plot uses it
# ---------------------------------------------------------------------------

_OPEN_FILES: dict[str, h5py.File] = {}

def _open(src: GroupLike) -> h5py.File:
    """Return an h5py.File, opening if needed. Caches by abs path so plots
    on the same file share the handle (cleanup happens at process exit)."""
    if isinstance(src, h5py.File):
        return src
    if isinstance(src, h5py.Group):
        return src.file
    p = str(Path(src).resolve())
    if p in _OPEN_FILES:
        return _OPEN_FILES[p]
    f = h5py.File(p, "r")
    _OPEN_FILES[p] = f
    return f


def find_latest(data_dir: str | Path = None) -> Optional[Path]:
    """Return the newest bench_*.h5 in data_dir. Used by the 'live' button."""
    data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parents[1] / "data"
    if not data_dir.is_dir(): return None
    files = sorted(data_dir.glob("bench_*.h5"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None
