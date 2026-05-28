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


def plot_led_amp_sweep(src: GroupLike, ax=None, label: Optional[str] = None,
                        **opts) -> plt.Axes:
    """Mean VX2740 pulse amplitude vs DG1022 LED amplitude.

    The curve should be roughly linear at low LED amplitude, then bend over
    and saturate where the Cremat shaper clips (around 1-2 V at the CAEN
    input, which corresponds to ~3-6 V at the WFG given the 10 dB pad).
    """
    f = _open(src)
    if "vx2740_led_amp_sweep" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_led_amp_sweep group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_led_amp_sweep"]
    amps, means, stds, ns = [], [], [], []
    for name in g.keys():
        sub = g[name]
        if not hasattr(sub, "attrs") or "led_amp_v" not in sub.attrs: continue
        amps.append(float(sub.attrs["led_amp_v"]))
        means.append(float(sub.attrs.get("mean_amp", float("nan"))))
        stds.append(float(sub.attrs.get("std_amp",  float("nan"))))
        ns.append(int(sub.attrs.get("n_pulses", 0)))
    if not amps:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-amplitude entries in /vx2740_led_amp_sweep",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    order = np.argsort(amps)
    amps  = np.array(amps)[order]
    means = np.array(means)[order]
    stds  = np.array(stds)[order]

    ax = ax if ax is not None else _new_ax()
    ax.errorbar(amps, means, yerr=stds, fmt="o-", ms=5, lw=1.5,
                capsize=3, color=ACCENT, ecolor=MUT,
                label=label or "mean amp")
    ov   = float(g.attrs.get("over_voltage", float("nan")))
    bias = float(g.attrs.get("bias_v", float("nan")))
    ax.set_xlabel("DG1022 LED amplitude (V$_{pp}$)")
    ax.set_ylabel("mean pulse amplitude (ADC counts)")
    ax.set_title(f"LED amplitude sweep @ V$_{{BD}}$+{ov:.1f}={bias:.2f} V"
                 if label is None else label)
    return ax


def plot_threshold_scan(src: GroupLike, ax=None, label: Optional[str] = None,
                         which: str = "both", log_y: bool = True,
                         **opts) -> plt.Axes:
    """Rate vs threshold on log-log axes.

    `which` selects which curve(s) to draw:
      - "light" → /vx2740_thresh_scan_light only
      - "dark"  → /vx2740_thresh_scan_dark only
      - "both"  → overlay (default)

    Plateaus in the light curve mark integer photo-electron peaks (the
    horizontal sections correspond to threshold being between two PE
    levels).  The dark curve sets the DCR floor.
    """
    f = _open(src)
    ax = ax if ax is not None else _new_ax()

    def _draw(group_name: str, color, marker, lab_suffix: str):
        if group_name not in f:
            return False
        g = f[group_name]
        thrs, rates, n_pulses = [], [], []
        for name in g.keys():
            sub = g[name]
            if not hasattr(sub, "attrs") or "threshold_adc" not in sub.attrs: continue
            thrs.append(int(sub.attrs["threshold_adc"]))
            rates.append(float(sub.attrs.get("rate_hz", float("nan"))))
            n_pulses.append(int(sub.attrs.get("n_pulses", 0)))
        if not thrs:
            return False
        order = np.argsort(thrs)
        thrs  = np.array(thrs)[order]
        rates = np.array(rates)[order]
        # log-Y can't show zeros; clip 0-rate (timeouts) to 0.1 Hz floor
        rates_plot = np.where(rates > 0, rates, 0.1)
        lab = (label + " " + lab_suffix).strip() if label else lab_suffix
        ax.plot(thrs, rates_plot, f"{marker}-", ms=5, lw=1.5,
                color=color, label=lab)
        return True

    drew = False
    if which in ("light", "both"):
        drew |= _draw("vx2740_thresh_scan_light", ACCENT, "o", "LED on")
    if which in ("dark",  "both"):
        drew |= _draw("vx2740_thresh_scan_dark",  MUT,    "s", "LED off (DCR)")
    if not drew:
        ax.text(0.5, 0.5, "no /vx2740_thresh_scan_* group(s)",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax

    ax.set_xscale("log")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("self-trigger threshold (ADC counts)")
    ax.set_ylabel("rate (Hz)")
    # Title shows OV from whichever group has it
    title = "threshold scan: rate vs trigger threshold"
    for gname in ("vx2740_thresh_scan_light", "vx2740_thresh_scan_dark"):
        if gname in f and "over_voltage" in f[gname].attrs:
            ov = float(f[gname].attrs["over_voltage"])
            bias = float(f[gname].attrs.get("bias_v", float("nan")))
            title = f"threshold scan @ V$_{{BD}}$+{ov:.1f}={bias:.2f} V"
            break
    ax.set_title(title)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_ov_scan_clean(src: GroupLike, ax=None, label: Optional[str] = None,
                        bins: int = 80, log_y: bool = False, **opts) -> plt.Axes:
    """Amplitude-spectrum family at each OV (LED off).

    Clean SPE peak should be visible at every OV; its position grows
    linearly with OV.  Below the SPE peak, baseline noise + sub-threshold
    pile-up.  At high OV, 2pe / 3pe peaks appear from cross-talk.
    """
    f = _open(src)
    if "vx2740_ov_scan_clean" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_ov_scan_clean group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    ax = ax if ax is not None else _new_ax()
    g = f["vx2740_ov_scan_clean"]
    entries = []
    for name in g.keys():
        sub = g[name]
        if "amplitudes_adc" not in sub or "over_voltage" not in sub.attrs: continue
        entries.append((float(sub.attrs["over_voltage"]), name))
    entries.sort()
    cmap = plt.get_cmap("viridis")
    for k, (ov, name) in enumerate(entries):
        sub  = g[name]
        amps = sub["amplitudes_adc"][:]
        if not len(amps): continue
        c = cmap(k / max(1, len(entries) - 1))
        ax.hist(amps, bins=int(bins), histtype="step", lw=1.3, color=c,
                label=f"OV +{ov:.1f}  (N={len(amps)})")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("amplitude (ADC counts)")
    ax.set_ylabel("counts")
    ax.set_title("LED-off OV scan: SPE spectra vs over-voltage"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_ov_scan_clean_gain(src: GroupLike, ax=None, label: Optional[str] = None,
                              **opts) -> plt.Axes:
    """Mean amplitude vs OV from the LED-off OV scan.

    Linear fit slope (ADC/V) gives the SiPM gain in raw digitizer units
    per volt of over-voltage.  The intercept on the V axis is V_BD.
    """
    f = _open(src)
    if "vx2740_ov_scan_clean" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_ov_scan_clean group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_ov_scan_clean"]
    ovs, means, stds = [], [], []
    for name in g.keys():
        sub = g[name]
        if "over_voltage" not in sub.attrs: continue
        ovs.append(float(sub.attrs["over_voltage"]))
        means.append(float(sub.attrs.get("mean_amp", float("nan"))))
        stds.append(float(sub.attrs.get("std_amp",  float("nan"))))
    if not ovs:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-OV entries",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    order = np.argsort(ovs)
    ovs   = np.array(ovs)[order]
    means = np.array(means)[order]
    stds  = np.array(stds)[order]

    ax = ax if ax is not None else _new_ax()
    ax.errorbar(ovs, means, yerr=stds, fmt="o-", ms=5, lw=1.5, capsize=3,
                color=ACCENT, ecolor=MUT, label=label or "mean amp (LED off)")

    # Linear fit for the gain slope.  The shaper saturates above OV+3 on
    # this bench, so means(OV) bends over (and the std blows up).  Fit
    # only the longest monotonically-increasing prefix — that's the
    # unsaturated linear regime and gives the true gain slope.
    valid = np.isfinite(means)
    if valid.sum() >= 2:
        # Find longest prefix where mean is strictly increasing
        cut = 1
        while (cut < len(means)
               and valid[cut]
               and means[cut] > means[cut - 1]):
            cut += 1
        n_lin = max(2, cut)   # at least the first two points
        n_lin = min(n_lin, valid.sum())
        ovs_lin   = ovs[valid][:n_lin]
        means_lin = means[valid][:n_lin]
        coef = np.polyfit(ovs_lin, means_lin, 1)
        slope, intercept = float(coef[0]), float(coef[1])
        xfit = np.linspace(ovs_lin.min(), ovs_lin.max(), 50)
        ax.plot(xfit, slope * xfit + intercept, ":", color=WARN, lw=1.3,
                label=f"linear fit (first {n_lin} pts): {slope:.0f} ADC/V")
        n_sat = valid.sum() - n_lin
        if n_sat > 0:
            ax.axvspan(ovs_lin.max() + 1e-3, ovs.max(),
                       facecolor=BAD, alpha=0.10)
            ax.text(0.985, 0.05, f"saturated ({n_sat} pts excluded)",
                    transform=ax.transAxes, ha="right", va="bottom",
                    color=BAD, fontsize=9)
    ax.set_xlabel("over-voltage V$_{BIAS}$ − V$_{BD}$ (V)")
    ax.set_ylabel("mean pulse amplitude (ADC counts)")
    ax.set_title("LED-off gain curve (clean OV scan)" if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_dcr_vs_ov(src: GroupLike, ax=None, label: Optional[str] = None,
                    log_y: bool = True, **opts) -> plt.Axes:
    """Dark count rate (Hz) vs over-voltage at a single fixed threshold."""
    f = _open(src)
    if "vx2740_dcr_vs_ov" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_dcr_vs_ov group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_dcr_vs_ov"]
    ovs, rates = [], []
    for name in g.keys():
        sub = g[name]
        if "over_voltage" not in sub.attrs: continue
        ovs.append(float(sub.attrs["over_voltage"]))
        rates.append(float(sub.attrs.get("rate_hz", 0.0)))
    if not ovs:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-OV entries",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    order = np.argsort(ovs)
    ovs   = np.array(ovs)[order]
    rates = np.array(rates)[order]
    rates_plot = np.where(rates > 0, rates, 0.1)

    thr = int(g.attrs.get("thresh_adc", -1))
    ax = ax if ax is not None else _new_ax()
    ax.plot(ovs, rates_plot, "s-", ms=5, lw=1.5, color=ACCENT,
            label=label or f"DCR @ thr={thr} ADC")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("over-voltage V$_{BIAS}$ − V$_{BD}$ (V)")
    ax.set_ylabel("rate (Hz)")
    ax.set_title("DCR vs over-voltage" if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_crosstalk_ap(src: GroupLike, ax=None, label: Optional[str] = None,
                       bins: int = 50, log_y: bool = True, **opts) -> plt.Axes:
    """Two-panel: (top) n-peaks-per-window histogram for cross-talk,
    (bottom) secondary-pulse time-after-primary for afterpulses.

    Implementation note: we hand back the first Axes, but draw both into a
    fresh figure so the layout is right; standalone use only — overlays
    don't make sense for a 2-panel plot.
    """
    f = _open(src)
    if "vx2740_crosstalk_ap" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_crosstalk_ap group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_crosstalk_ap"]

    npeaks = g.get("n_peaks_per_wf")
    dts    = g.get("secondary_dt_us")
    if npeaks is None or dts is None:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "crosstalk/AP data missing",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    npeaks = np.asarray(npeaks[:], dtype=np.int32)
    dts    = np.asarray(dts[:],    dtype=np.float32)

    # If we got a host-supplied Axes, draw into it (single-panel summary).
    # Otherwise produce the 2-panel figure.
    ct = float(g.attrs.get("crosstalk_fraction", float("nan")))
    if ax is not None:
        # Single-panel: n_peaks histogram only (host owns the layout)
        max_k = int(npeaks.max()) if len(npeaks) else 0
        ax.hist(npeaks, bins=np.arange(-0.5, max_k + 1.5, 1.0),
                color=ACCENT, alpha=0.85, edgecolor=PANEL, linewidth=0.5,
                label=label or f"CT fraction ≈ {100*ct:.1f}%")
        if log_y: ax.set_yscale("log")
        ax.set_xlabel("peaks per trigger window")
        ax.set_ylabel("waveforms")
        ax.set_title(f"crosstalk distribution  (N={len(npeaks)})"
                     if label is None else label)
        ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
        return ax

    fig, axs = plt.subplots(2, 1, figsize=(9, 6))
    apply_dark_style(fig, axs[0])
    apply_dark_style(fig, axs[1])

    max_k = int(npeaks.max()) if len(npeaks) else 0
    axs[0].hist(npeaks, bins=np.arange(-0.5, max_k + 1.5, 1.0),
                color=ACCENT, alpha=0.85, edgecolor=PANEL, linewidth=0.5)
    if log_y: axs[0].set_yscale("log")
    axs[0].set_xlabel("peaks per trigger window")
    axs[0].set_ylabel("waveforms")
    axs[0].set_title(f"crosstalk distribution  (N={len(npeaks)}, "
                     f"CT ≈ {100*ct:.1f}%)")

    if len(dts):
        axs[1].hist(dts, bins=int(bins), color=OK, alpha=0.85,
                    edgecolor=PANEL, linewidth=0.5)
    else:
        axs[1].text(0.5, 0.5, "no secondary pulses observed",
                    ha="center", va="center", color=MUT,
                    transform=axs[1].transAxes)
    if log_y: axs[1].set_yscale("log")
    axs[1].set_xlabel("Δt from primary (µs)")
    axs[1].set_ylabel("count")
    axs[1].set_title(f"afterpulse Δt distribution  (N={len(dts)})")

    fig.tight_layout()
    return axs[0]


def plot_pulse_area_scatter(src: GroupLike, ax=None, label: Optional[str] = None,
                              ov: Optional[float] = None,
                              max_points: int = 10000, log_y: bool = False,
                              **opts) -> plt.Axes:
    """Scatter of pulse area vs peak amplitude across stored waveforms.

    Reads /vx2740_ov_scan_clean/ov_*/waveforms.  For each waveform:
      - subtract baseline (mean of pre-trigger samples)
      - amplitude = max(post-trigger samples)
      - area      = Σ(post-trigger samples) × 8 ns

    A clean SPE chain gives a tight linear cluster (constant pulse shape,
    so area ∝ amplitude).  Scatter or outliers above the line are pile-up;
    a flat / broken tail is shaper saturation.

    `ov` selects a single over-voltage subgroup; default: use the lowest
    OV (cleanest, no saturation).
    """
    f = _open(src)
    if "vx2740_ov_scan_clean" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_ov_scan_clean group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_ov_scan_clean"]
    entries = []
    for name in g.keys():
        sub = g[name]
        if "waveforms" not in sub or "over_voltage" not in sub.attrs: continue
        entries.append((float(sub.attrs["over_voltage"]), name))
    if not entries:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no stored waveforms in /vx2740_ov_scan_clean",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    entries.sort()
    # Pick OV
    if ov is None:
        chosen_ov, chosen_name = entries[0]
    else:
        idx = int(np.argmin([abs(e[0] - ov) for e in entries]))
        chosen_ov, chosen_name = entries[idx]
    sub = g[chosen_name]
    wfs = np.asarray(sub["waveforms"][:], dtype=np.float32)
    pre_us  = float(g.attrs.get("pre_us",  2.0))
    post_us = float(g.attrs.get("post_us", 10.0))
    pre_samples  = int(round(pre_us * 125.0))   # 125 MS/s
    post_samples = int(round(post_us * 125.0))
    baselines = wfs[:, :pre_samples].mean(axis=1, keepdims=True)
    wfs_bl    = wfs - baselines
    post = wfs_bl[:, pre_samples:pre_samples + post_samples]
    amps = post.max(axis=1).astype(np.float32)
    area = post.sum(axis=1).astype(np.float64) * (1.0 / 125.0e6 * 1e9)   # ADC·ns

    if len(amps) > max_points:
        idx = np.random.default_rng(0).choice(len(amps), size=max_points, replace=False)
        amps_p, area_p = amps[idx], area[idx]
    else:
        amps_p, area_p = amps, area

    ax = ax if ax is not None else _new_ax(figsize=(8, 4.5))
    ax.plot(amps_p, area_p, ".", color=ACCENT, ms=2.5, alpha=0.5,
            label=label or f"OV +{chosen_ov:.1f} V  (N={len(amps)})")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("peak amplitude (ADC counts)")
    ax.set_ylabel("pulse area (ADC·ns)")
    bias_v = float(sub.attrs.get("bias_v", float("nan")))
    ax.set_title(f"area vs amplitude  @ OV +{chosen_ov:.1f} V "
                 f"(bias {bias_v:.2f} V)"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_iv_leakage(src: GroupLike, ax=None, label: Optional[str] = None,
                     v_max: Optional[float] = None, **opts) -> plt.Axes:
    """Sub-V_BD region of the dark IV with a 1/R linear fit.

    For V well below V_BD, |I| should be roughly linear in V (Ohmic
    leakage through the bias filter + cable + diode reverse saturation).
    The slope gives leakage resistance.  `v_max` defaults to V_BD−2 V
    if /iv has the estimate, else 45 V.
    """
    f = _open(src)
    if "iv" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /iv group", ha="center", va="center",
                color=MUT, transform=ax.transAxes)
        return ax
    g = f["iv"]
    v  = g["source_v"][:]
    i  = np.abs(g["current_a"][:])

    if v_max is None:
        v_max = float(g.attrs["v_bd_estimate"] - 2.0) if "v_bd_estimate" in g.attrs else 45.0

    mask = (v > 0) & (v <= v_max)
    if mask.sum() < 3:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, f"not enough points below {v_max:.1f} V",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    vv, ii = v[mask], i[mask]

    ax = ax if ax is not None else _new_ax()
    ax.plot(vv, ii, "o", ms=4, color=ACCENT, label=label or "|I| below V$_{BD}$")

    # Linear fit through 0 (one-parameter): I = V / R  →  R = V·V / (V·I) sum form
    # Use ordinary linear-regression with intercept allowed, but report
    # both slope and intercept so the reader sees the offset.
    coef = np.polyfit(vv, ii, 1)
    slope, intercept = float(coef[0]), float(coef[1])
    vfit = np.linspace(0, v_max, 50)
    ax.plot(vfit, slope * vfit + intercept, ":", color=WARN, lw=1.2,
            label=f"fit: R≈{1/abs(slope):.2e} Ω  (offset {intercept:+.2e} A)"
                  if abs(slope) > 0 else "fit: slope ~ 0")
    ax.set_xlabel("bias (V)"); ax.set_ylabel("|leakage current| (A)")
    ax.set_title(f"leakage IV  (V ≤ {v_max:.1f} V)"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_vx2740_noise_floor(src: GroupLike, ax=None,
                              label: Optional[str] = None,
                              log_y: bool = True, **opts) -> plt.Axes:
    """VX2740 false-trigger rate vs threshold with bias OFF + LED OFF.

    This is the digitizer noise contribution to any rate measurement —
    set every real threshold scan well above the elbow of this curve.
    """
    f = _open(src)
    if "vx2740_noise_floor" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_noise_floor group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_noise_floor"]
    thrs, rates = [], []
    for name in g.keys():
        sub = g[name]
        if "threshold_adc" not in sub.attrs: continue
        thrs.append(int(sub.attrs["threshold_adc"]))
        rates.append(float(sub.attrs.get("rate_hz", 0.0)))
    if not thrs:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-threshold entries",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    order = np.argsort(thrs)
    thrs  = np.array(thrs)[order]
    rates = np.array(rates)[order]
    rates_plot = np.where(rates > 0, rates, 0.05)

    ax = ax if ax is not None else _new_ax()
    ax.plot(thrs, rates_plot, "o-", ms=5, lw=1.5, color=BAD,
            label=label or "noise floor (bias off, LED off)")
    ax.set_xscale("log")
    if log_y: ax.set_yscale("log")
    ax.set_xlabel("self-trigger threshold (ADC counts)")
    ax.set_ylabel("rate (Hz)")
    ax.set_title("VX2740 noise floor (bias off, LED off)"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
    return ax


def plot_k6485_noise_floor(src: GroupLike, ax=None,
                             label: Optional[str] = None, **opts) -> plt.Axes:
    """K6485 RMS noise vs configured range at zero bias / no LED.

    Lower-range modes integrate more samples internally (smaller RMS but
    slower); AUTO picks per-read.  Use this curve to pick an appropriate
    range for the bench's expected current.
    """
    f = _open(src)
    if "k6485_noise_floor" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /k6485_noise_floor group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["k6485_noise_floor"]
    names, means, stds = [], [], []
    for name in g.keys():
        sub = g[name]
        if not hasattr(sub, "attrs") or "range_label" not in sub.attrs: continue
        names.append(str(sub.attrs["range_label"]))
        means.append(float(sub.attrs.get("mean_a", float("nan"))))
        stds.append(float(sub.attrs.get("std_a",  float("nan"))))
    if not names:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-range entries",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax

    ax = ax if ax is not None else _new_ax()
    xpos = np.arange(len(names))
    ax.bar(xpos, np.abs(means), yerr=stds, capsize=6,
           color=ACCENT, edgecolor=LINE)
    ax.set_xticks(xpos); ax.set_xticklabels(names, color="#dde3ee")
    ax.set_ylabel("|mean current| ± RMS (A)")
    ax.set_yscale("log")
    ax.set_title("K6485 noise floor (no bias, no LED)" if label is None else label)
    for x, m, s in zip(xpos, means, stds):
        ax.text(x, abs(m), f"  μ={m:+.1e}\n  σ={s:.1e}",
                ha="left", va="center", color="#dde3ee", fontsize=8)
    return ax


def plot_led_width_sweep(src: GroupLike, ax=None, label: Optional[str] = None,
                          **opts) -> plt.Axes:
    """Mean amplitude vs LED pulse width at fixed amp + OV."""
    f = _open(src)
    if "vx2740_led_width_sweep" not in f:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no /vx2740_led_width_sweep group",
                ha="center", va="center", color=MUT, transform=ax.transAxes)
        return ax
    g = f["vx2740_led_width_sweep"]
    ws, means, stds = [], [], []
    for name in g.keys():
        sub = g[name]
        if "width_s" not in sub.attrs: continue
        ws.append(float(sub.attrs["width_s"]))
        means.append(float(sub.attrs.get("mean_amp", float("nan"))))
        stds.append(float(sub.attrs.get("std_amp",  float("nan"))))
    if not ws:
        ax = ax if ax is not None else _new_ax()
        ax.text(0.5, 0.5, "no per-width entries", ha="center", va="center",
                color=MUT, transform=ax.transAxes)
        return ax
    order = np.argsort(ws)
    ws    = np.array(ws)[order]
    means = np.array(means)[order]
    stds  = np.array(stds)[order]

    ax = ax if ax is not None else _new_ax()
    ax.errorbar(ws * 1e9, means, yerr=stds, fmt="o-", ms=5, lw=1.5,
                capsize=3, color=ACCENT, ecolor=MUT,
                label=label or "mean amp")
    ax.set_xscale("log")
    ov   = float(g.attrs.get("over_voltage", float("nan")))
    amp  = float(g.attrs.get("led_amp_v",  float("nan")))
    ax.set_xlabel("LED pulse width (ns)")
    ax.set_ylabel("mean pulse amplitude (ADC counts)")
    ax.set_title(f"LED width sweep @ OV+{ov:.1f} V, {amp:.1f} V$_{{pp}}$"
                 if label is None else label)
    ax.legend(facecolor=PANEL, edgecolor=LINE, labelcolor="#dde3ee", fontsize=9)
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
    "led_amp_sweep": {
        "fn":   plot_led_amp_sweep,
        "desc": "Mean VX2740 amplitude vs DG1022 LED Vpp at fixed over-voltage",
    },
    "threshold_scan": {
        "fn":   plot_threshold_scan,
        "desc": "Rate vs self-trigger threshold (LED on + DCR), log-log",
    },
    "ov_scan_clean": {
        "fn":   plot_ov_scan_clean,
        "desc": "Clean LED-off OV scan: SPE-spectrum family overlay",
    },
    "ov_scan_clean_gain": {
        "fn":   plot_ov_scan_clean_gain,
        "desc": "Clean OV scan: mean amplitude vs OV with linear gain fit",
    },
    "dcr_vs_ov": {
        "fn":   plot_dcr_vs_ov,
        "desc": "Dark count rate vs over-voltage at fixed threshold",
    },
    "crosstalk_ap": {
        "fn":   plot_crosstalk_ap,
        "desc": "Crosstalk: peaks-per-window + afterpulse Δt (two panels)",
    },
    "led_width_sweep": {
        "fn":   plot_led_width_sweep,
        "desc": "Mean VX2740 amplitude vs LED pulse width (log X)",
    },
    "pulse_area_scatter": {
        "fn":   plot_pulse_area_scatter,
        "desc": "Pulse area vs peak amplitude scatter (from stored waveforms)",
    },
    "iv_leakage": {
        "fn":   plot_iv_leakage,
        "desc": "Sub-V_BD IV with 1/R linear fit",
    },
    "vx2740_noise_floor": {
        "fn":   plot_vx2740_noise_floor,
        "desc": "Digitizer false-trigger rate vs threshold (bias off, LED off)",
    },
    "k6485_noise_floor": {
        "fn":   plot_k6485_noise_floor,
        "desc": "K6485 RMS noise across measurement ranges at zero bias",
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
