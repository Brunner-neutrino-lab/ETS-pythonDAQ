"""
daq/characterization.py

Tile characterization: the per-channel table (MUX channel -> digitizer
channel, V_BD, A0) that seeds the coarse sweep, and the initial
characterization that fills it.

Initial characterization, per MUX channel: N self-triggered waveforms at
each of two (or more) bias voltages V = V_BD,est + OV. The median pulse
amplitude at a voltage is taken as the SPE amplitude there, and the
straight line A = A0 * (V - V_BD) through those points gives A0 (ADC counts
per volt of overvoltage) and V_BD (where it crosses zero). Each run writes
an HDF5 file under data/characterization/<table>/, the fitted values into
the table, and (from the web UI) one lab book entry with a plot per channel.

A table is one JSON file, data/characterization/<name>.json, for one
cabling of the MUX at one temperature (e.g. one quad of a tile).

No NiceGUI imports; the web UI is shell._build_char_panel.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("daq.characterization")

_REPO_ROOT = Path(__file__).resolve().parents[1]
CHAR_DIR = _REPO_ROOT / "data" / "characterization"

SAMPLE_RATE_HZ = 125e6        # VX2740

# MUX channels with fitted connectors on the IV MUX: 17-22 are unpopulated
# on the second board, so a quad of 24 SiPMs goes on 1-16 and 23-30.
DEFAULT_MUX_CHANNELS = "1-16,23-30"

# Keep this many baseline-subtracted waveforms per bias point in memory
# for the live view and the lab book plot (all N go to the HDF5).
N_SAMPLE_WAVEFORMS = 10

# A trigger threshold outside this range of the measured SPE amplitude
# biases the median: too low and noise triggers pull it down, too high and
# the SPE peak is cut so it moves up.
GOOD_THRESHOLD_PE = (0.3, 0.7)

# V_BD of SiPMs on one tile scatter by ~0.2 V; a fit this far from the
# estimate usually means the wrong channel or a bad estimate.
VBD_WARN_V = 1.5


# ---------------------------------------------------------------------------
# Channel lists
# ---------------------------------------------------------------------------

def parse_channels(text: str) -> list[int]:
    """'1-16, 23-30' -> [1..16, 23..30], in the order given, no repeats."""
    out: list[int] = []
    for tok in re.split(r"[,\s]+", str(text or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            step = 1 if b >= a else -1
            vals = range(a, b + step, step)
        elif tok.isdigit():
            vals = [int(tok)]
        else:
            raise ValueError(f"bad channel token {tok!r}")
        for v in vals:
            if v not in out:
                out.append(v)
    return out


def parse_floats(text: str) -> list[float]:
    return [float(t) for t in re.split(r"[,\s]+", str(text or "").strip()) if t]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def amplitude_summary(amps) -> dict:
    """Median amplitude and its uncertainty.

    sigma is the MAD-based width (robust against the crosstalk and
    afterpulse tail); the median's standard error for a near-Gaussian peak
    is sqrt(pi/2) * sigma / sqrt(n).
    """
    a = np.asarray(amps, dtype=float)
    a = a[np.isfinite(a)]
    n = int(a.size)
    if n == 0:
        return {"n": 0, "median": math.nan, "sigma": math.nan,
                "median_err": math.nan}
    med = float(np.median(a))
    sigma = float(1.4826 * np.median(np.abs(a - med)))
    err = 1.2533 * sigma / math.sqrt(n) if n > 1 else math.nan
    return {"n": n, "median": med, "sigma": sigma, "median_err": float(err)}


def fit_gain(bias_v, amp, amp_err=None) -> dict:
    """Weighted straight-line fit A = A0 * (V - V_BD).

    Returns a0 (ADC/V), vbd (V) and their 1-sigma errors propagated from
    amp_err; with two points the line goes through both exactly and the
    errors are plain propagation. ok is False when the fit is unusable
    (fewer than two points, or the amplitude does not rise with bias).
    """
    v = np.asarray(bias_v, dtype=float)
    y = np.asarray(amp, dtype=float)
    good = np.isfinite(v) & np.isfinite(y)
    if amp_err is not None:
        e = np.asarray(amp_err, dtype=float)
        good &= np.isfinite(e) & (e > 0)
    v, y = v[good], y[good]
    w = 1.0 / e[good] ** 2 if amp_err is not None else np.ones_like(v)
    out = {"ok": False, "n_points": int(v.size), "a0": math.nan,
           "vbd": math.nan, "a0_err": math.nan, "vbd_err": math.nan}
    if v.size < 2 or np.ptp(v) == 0:
        return out
    s, sx, sy = w.sum(), (w * v).sum(), (w * y).sum()
    sxx, sxy = (w * v * v).sum(), (w * v * y).sum()
    delta = s * sxx - sx * sx
    b1 = (s * sxy - sx * sy) / delta
    b0 = (sxx * sy - sx * sxy) / delta
    out["a0"], out["vbd"] = float(b1), float(-b0 / b1) if b1 != 0 else math.nan
    if b1 <= 0:
        return out
    out["ok"] = True
    if amp_err is not None:
        v00, v11, c01 = sxx / delta, s / delta, -sx / delta
        out["a0_err"] = float(math.sqrt(v11))
        var_vbd = (v00 / b1 ** 2 + b0 ** 2 * v11 / b1 ** 4
                   - 2 * b0 * c01 / b1 ** 3)
        out["vbd_err"] = float(math.sqrt(max(var_vbd, 0.0)))
    return out


def threshold_adc(a0: float, vbd: float, bias_v: float, pe: float = 0.5) -> int:
    """Trigger threshold at `pe` SPE for bias_v, from the fitted gain."""
    return int(round(pe * a0 * (float(bias_v) - vbd)))


def channel_flags(points: list[dict], fit: dict, plan: "InitialPlan") -> list[str]:
    """Plain-language reasons to look at this channel again."""
    flags = []
    for p in points:
        if p["n"] == 0:
            flags.append(f"no pulses at {p['bias_v']:.2f} V")
        elif p["n_waveforms"] < plan.n_waveforms:
            flags.append(f"only {p['n_waveforms']}/{plan.n_waveforms} "
                         f"waveforms at {p['bias_v']:.2f} V (timeout)")
    first = next((p for p in points if p["n"] > 0), None)
    if first is not None and first["median"] > 0:
        ratio = first["threshold_adc"] / first["median"]
        lo, hi = GOOD_THRESHOLD_PE
        if not lo <= ratio <= hi:
            flags.append(
                f"threshold {first['threshold_adc']} ADC is {ratio:.2f} SPE at "
                f"{first['bias_v']:.2f} V; re-run with ~"
                f"{int(round(first['median'] / 2))} ADC")
    if not fit["ok"]:
        flags.append("amplitude does not rise with bias: no fit")
    elif abs(fit["vbd"] - plan.vbd_est_v) > VBD_WARN_V:
        flags.append(f"V_BD {fit['vbd']:.2f} V is "
                     f"{fit['vbd'] - plan.vbd_est_v:+.2f} V from the estimate")
    return flags


# ---------------------------------------------------------------------------
# Channel tables
# ---------------------------------------------------------------------------

_TABLE_LOCK = threading.RLock()
_revision = 0

EDITABLE_FIELDS = ("dig_ch", "sipm", "feedthrough", "vbd_v",
                   "a0_adc_per_v", "notes")

CSV_COLUMNS = ["mux_ch", "dig_ch", "sipm", "feedthrough", "vbd_v",
               "vbd_err_v", "a0_adc_per_v", "a0_err", "spe_ov3_adc",
               "thr_half_spe_ov3_adc", "source", "updated", "notes",
               "initial_threshold_adc", "bias1_v", "median1_adc", "n1",
               "bias2_v", "median2_adc", "n2", "initial_h5", "check"]


def revision() -> int:
    return _revision


def _bump() -> None:
    global _revision
    _revision += 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip()).strip("._")
    if not name:
        raise ValueError("empty table name")
    return name


def _path(name: str) -> Path:
    return CHAR_DIR / f"{safe_name(name)}.json"


def list_tables() -> list[str]:
    if not CHAR_DIR.is_dir():
        return []
    files = sorted(CHAR_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return [p.stem for p in files]


def _blank_row(mux_ch: int, dig_ch: Optional[int] = None) -> dict:
    return {"mux_ch": int(mux_ch),
            "dig_ch": int(mux_ch if dig_ch is None else dig_ch),
            "sipm": None, "feedthrough": "",
            "vbd_v": None, "vbd_err_v": None,
            "a0_adc_per_v": None, "a0_err": None,
            "source": "", "updated": "", "notes": "",
            "initial": None, "history": []}


def new_table(name: str, *, tile: str = "", quad: str = "",
              temperature_K: Optional[float] = None,
              mux_channels=()) -> dict:
    with _TABLE_LOCK:
        path = _path(name)
        if path.exists():
            raise FileExistsError(f"table {path.stem} already exists")
        table = {"name": path.stem, "tile": tile, "quad": quad,
                 "temperature_K": temperature_K, "created": _now(),
                 "updated": _now(),
                 "rows": [_blank_row(ch) for ch in mux_channels]}
        save_table(table)
        return table


def load_table(name: str) -> dict:
    with open(_path(name)) as f:
        return json.load(f)


def save_table(table: dict) -> None:
    with _TABLE_LOCK:
        CHAR_DIR.mkdir(parents=True, exist_ok=True)
        table["rows"].sort(key=lambda r: r["mux_ch"])
        table["updated"] = _now()
        path = _path(table["name"])
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(table, f, indent=1)
        os.replace(tmp, path)
        _bump()


def get_row(table: dict, mux_ch: int, create: bool = False) -> Optional[dict]:
    for r in table["rows"]:
        if r["mux_ch"] == int(mux_ch):
            return r
    if not create:
        return None
    r = _blank_row(mux_ch)
    table["rows"].append(r)
    return r


def _push_history(row: dict) -> None:
    if row.get("vbd_v") is None and row.get("a0_adc_per_v") is None:
        return
    row.setdefault("history", []).append({
        "vbd_v": row.get("vbd_v"), "vbd_err_v": row.get("vbd_err_v"),
        "a0_adc_per_v": row.get("a0_adc_per_v"), "a0_err": row.get("a0_err"),
        "source": row.get("source"), "updated": row.get("updated"),
        "replaced": _now()})


def _clean(field_name: str, value):
    if field_name in ("dig_ch", "sipm"):
        if value in (None, ""):
            return None
        return int(float(value))
    if field_name in ("vbd_v", "a0_adc_per_v"):
        if value in (None, ""):
            return None
        return float(value)
    return "" if value is None else str(value)


def edit_row(name: str, mux_ch: int, user: str, **fields) -> dict:
    """Hand edit of one row. A changed V_BD or A0 keeps the previous pair in
    the row's history and marks the row as entered by hand."""
    with _TABLE_LOCK:
        table = load_table(name)
        row = get_row(table, mux_ch, create=True)
        clean = {k: _clean(k, v) for k, v in fields.items()
                 if k in EDITABLE_FIELDS}
        gain_changed = any(k in ("vbd_v", "a0_adc_per_v")
                           and clean[k] != row.get(k) for k in clean)
        if gain_changed:
            _push_history(row)
            row["vbd_err_v"] = row["a0_err"] = None
            row["source"] = f"entered by {user or 'anonymous'}"
            row["updated"] = _now()
        row.update(clean)
        save_table(table)
        return table


def add_rows(name: str, mux_channels) -> dict:
    with _TABLE_LOCK:
        table = load_table(name)
        for ch in mux_channels:
            get_row(table, ch, create=True)
        save_table(table)
        return table


def delete_rows(name: str, mux_channels) -> dict:
    with _TABLE_LOCK:
        table = load_table(name)
        drop = {int(c) for c in mux_channels}
        table["rows"] = [r for r in table["rows"] if r["mux_ch"] not in drop]
        save_table(table)
        return table


def record_fit(name: str, mux_ch: int, dig_ch: int, fit: dict,
               initial: dict, store: bool = True) -> None:
    """Store an initial-characterization result in the table. Without
    `store` (failed or implausible fit) the row keeps its V_BD and A0 and
    only the attempt is recorded."""
    with _TABLE_LOCK:
        table = load_table(name)
        row = get_row(table, mux_ch, create=True)
        row["dig_ch"] = int(dig_ch)
        row["initial"] = initial
        if store and fit["ok"]:
            _push_history(row)
            row.update(vbd_v=round(fit["vbd"], 4),
                       vbd_err_v=_round_or_none(fit["vbd_err"], 4),
                       a0_adc_per_v=round(fit["a0"], 2),
                       a0_err=_round_or_none(fit["a0_err"], 2),
                       source=f"initial char {initial['ts']}",
                       updated=_now())
        save_table(table)


def _round_or_none(x, nd):
    return None if x is None or not math.isfinite(x) else round(x, nd)


def derived(row: dict, ov: float = 3.0) -> dict:
    """SPE amplitude and the 0.5 SPE threshold at `ov` volts overvoltage."""
    a0 = row.get("a0_adc_per_v")
    if a0 is None:
        return {"spe": None, "thr": None}
    return {"spe": round(a0 * ov), "thr": round(0.5 * a0 * ov)}


def table_to_csv(table: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in table["rows"]:
        d = derived(r)
        vals = {**r, "spe_ov3_adc": d["spe"], "thr_half_spe_ov3_adc": d["thr"]}
        ini = r.get("initial") or {}
        vals["initial_threshold_adc"] = ini.get("threshold_adc")
        vals["initial_h5"] = ini.get("h5")
        vals["check"] = "; ".join(ini.get("flags") or [])
        for i, pt in enumerate((ini.get("points") or [])[:2], start=1):
            vals[f"bias{i}_v"], vals[f"median{i}_adc"], _, vals[f"n{i}"] = pt
        w.writerow(["" if vals.get(c) is None else vals.get(c)
                    for c in CSV_COLUMNS])
    return buf.getvalue()


def merge_csv(name: str, text: str, user: str) -> int:
    """Apply a CSV with a mux_ch column (as exported, or typed in Excel) to
    the table. Only editable columns are read; blank cells are left alone.
    Returns the number of rows touched."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "mux_ch" not in reader.fieldnames:
        raise ValueError("CSV needs a mux_ch column")
    n = 0
    for rec in reader:
        if not (rec.get("mux_ch") or "").strip():
            continue
        fields = {k: rec[k] for k in EDITABLE_FIELDS
                  if k in rec and (rec[k] or "").strip() != ""}
        edit_row(name, int(float(rec["mux_ch"])), user, **fields)
        n += 1
    return n


# ---------------------------------------------------------------------------
# Initial characterization run
# ---------------------------------------------------------------------------

@dataclass
class InitialPlan:
    table: str
    channels: list                 # [(mux_ch, dig_ch), ...]
    vbd_est_v: float
    ov_v: list = field(default_factory=lambda: [3.0, 4.0])
    n_waveforms: int = 100
    threshold_adc: int = 300
    pre_us: float = 2.0
    post_us: float = 10.0
    settle_s: float = 1.0
    timeout_s: float = 30.0
    switch_mux: bool = True
    temperature_K: Optional[float] = None
    user: str = ""

    def bias_points(self) -> list[float]:
        return [round(self.vbd_est_v + ov, 4) for ov in self.ov_v]


def _check_plan(plan: InitialPlan) -> None:
    if not plan.channels:
        raise ValueError("no channels to characterize")
    if len(plan.ov_v) < 2:
        raise ValueError("need at least two overvoltages for a line")
    if plan.n_waveforms < 2:
        raise ValueError("need at least two waveforms per point")
    if min(plan.bias_points()) <= 0:
        raise ValueError("bias points must be positive")


def _sample_waveforms(waves, pre_samples: int, k: int) -> np.ndarray:
    """First k waveforms with each one's pre-trigger mean subtracted."""
    if waves is None or len(waves) == 0:
        return np.zeros((0, 0), dtype=np.float32)
    w = np.asarray(waves[:k], dtype=np.float32)
    pre = max(1, min(pre_samples, w.shape[1]))
    return w - w[:, :pre].mean(axis=1, keepdims=True)


def _trigger_rate(ts) -> float:
    t = np.asarray(ts, dtype=float)
    if t.size < 2 or t[-1] <= t[0]:
        return math.nan
    return float((t.size - 1) / (t[-1] - t[0]))


def run_initial(plan: InitialPlan, instruments: dict, *,
                h5_path: Optional[str] = None,
                on_point: Optional[Callable] = None,
                on_channel: Optional[Callable] = None,
                on_log: Optional[Callable] = None,
                on_acquisition: Optional[Callable] = None,
                abort: Optional[dict] = None) -> list[dict]:
    """Acquire, analyse and record every channel of `plan`.

    on_point(channel_result, point), on_channel(channel_result) and
    on_log(text) report progress; on_acquisition(AcquisitionResult,
    n_requested) hands each raw acquisition on (the web UI's waveform
    viewer). abort["flag"] stops before the next point. The bias is
    switched off between channels and at the end.
    """
    from . import h5io
    from . import primitives as P
    from b2987b.driver import check_bias_lock

    _check_plan(plan)
    say = on_log or (lambda _t: None)
    elec = instruments.get("elec")
    dig = instruments.get("digitizer")
    ctrl = getattr(dig, "_ctrl", None) if dig is not None else None
    ivmux = instruments.get("ivmux")
    if elec is None:
        raise RuntimeError("electrometer (B2987) not connected")
    if ctrl is None:
        raise RuntimeError("VX2740 digitizer not connected")
    if plan.switch_mux and ivmux is None:
        raise RuntimeError("IV MUX not connected (untick 'switch IV MUX' "
                           "to run on whatever channel is selected)")
    biases = plan.bias_points()
    check_bias_lock(max(biases))
    pre_samples = int(round(plan.pre_us * SAMPLE_RATE_HZ * 1e-6))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    h5 = None
    if h5_path:
        import h5py
        Path(h5_path).parent.mkdir(parents=True, exist_ok=True)
        h5 = h5py.File(h5_path, "w")
        h5io.write_top_attrs(h5, measurement_type="initial_characterization",
                             temperature_K=plan.temperature_K,
                             illuminated=False,
                             extra={"table": plan.table,
                                    "plan": json.dumps(asdict(plan))})
    results: list[dict] = []
    try:
        for ci, (mux_ch, dig_ch) in enumerate(plan.channels):
            if abort and abort.get("flag"):
                break
            res = {"mux_ch": int(mux_ch), "dig_ch": int(dig_ch),
                   "points": [], "fit": None, "flags": [], "index": ci}
            results.append(res)
            say(f"MUX ch {mux_ch} (digitizer ch {dig_ch}): "
                f"{ci + 1}/{len(plan.channels)}")
            if plan.switch_mux:
                P.bias_off(elec)
                P.select_channel(ivmux, int(mux_ch))
            ctrl.configure_record_window(pre_us=plan.pre_us,
                                         post_us=plan.post_us)
            ctrl.configure_channels(sipm_channels=[int(dig_ch)],
                                    thresholds={int(dig_ch): int(plan.threshold_adc)},
                                    threshold_mode="per_channel",
                                    include_pmt=False)
            ctrl.configure_trigger(mode="self")
            for ov, bias in zip(plan.ov_v, biases):
                if abort and abort.get("flag"):
                    break
                P.set_bias(elec, bias, settle_s=plan.settle_s)
                t0 = time.time()
                ctrl.arm()
                try:
                    acq = ctrl.acquire(int(plan.n_waveforms),
                                       min(1000, int(plan.n_waveforms)),
                                       True, float(plan.timeout_s))
                finally:
                    ctrl.disarm()
                if on_acquisition is not None:
                    on_acquisition(acq, int(plan.n_waveforms))
                amps = np.asarray(acq.amplitudes.get(int(dig_ch), []),
                                  dtype=np.float32)
                ts = np.asarray(acq.timestamps.get(int(dig_ch), []))
                waves = acq.waveforms.get(int(dig_ch))
                summ = amplitude_summary(amps)
                point = {"ov_v": float(ov), "bias_v": float(bias),
                         "threshold_adc": int(plan.threshold_adc),
                         "n_waveforms": int(acq.n_waveforms),
                         "amplitudes": amps,
                         "samples": _sample_waveforms(waves, pre_samples,
                                                      N_SAMPLE_WAVEFORMS),
                         "rate_hz": _trigger_rate(ts),
                         "elapsed_s": time.time() - t0, **summ}
                res["points"].append(point)
                say(f"  {bias:.2f} V (OV {ov:+.2f}): median "
                    f"{summ['median']:.0f} ± {summ['median_err']:.0f} ADC, "
                    f"{summ['n']} pulses in {acq.n_waveforms} waveforms")
                if h5 is not None:
                    g = h5.require_group(f"mux{int(mux_ch):02d}/"
                                         f"{int(round(bias * 1000))}mV")
                    h5io.write_pulse(
                        g, amplitudes_adc=amps, timestamps_s=ts,
                        waveforms=(waves if waves is not None and len(waves)
                                   else None),
                        channel=int(dig_ch),
                        attrs={"bias_v": bias, "ov_v": ov,
                               "threshold_adc": int(plan.threshold_adc),
                               "n_waveforms": int(acq.n_waveforms),
                               "median_adc": summ["median"],
                               "median_err_adc": summ["median_err"],
                               "sigma_adc": summ["sigma"],
                               "rate_hz": point["rate_hz"]})
                    h5.flush()
                if on_point is not None:
                    on_point(res, point)
            if plan.switch_mux:
                P.bias_off(elec)
            pts = res["points"]
            if (abort and abort.get("flag")) and (
                    len(pts) < len(biases)
                    or pts[-1]["n_waveforms"] < plan.n_waveforms):
                res["flags"].append("stopped by the operator before the "
                                    "channel was complete: not recorded")
                say(f"  ! {res['flags'][-1]}")
                break
            fit = fit_gain([p["bias_v"] for p in pts],
                           [p["median"] for p in pts],
                           [p["median_err"] for p in pts])
            res["fit"] = fit
            res["flags"] = channel_flags(pts, fit, plan)
            # The coarse sweep biases at V_BD + OV straight from the table,
            # so a fit far from the estimate must not land there.
            usable = fit["ok"] and abs(fit["vbd"] - plan.vbd_est_v) <= VBD_WARN_V
            if fit["ok"] and not usable:
                res["flags"].append("not stored in the table")
            if fit["ok"]:
                say(f"  -> V_BD {fit['vbd']:.3f} ± {fit['vbd_err']:.3f} V, "
                    f"A0 {fit['a0']:.1f} ± {fit['a0_err']:.1f} ADC/V")
            for fl in res["flags"]:
                say(f"  ! {fl}")
            if h5 is not None:
                g = h5.require_group(f"mux{int(mux_ch):02d}")
                g.attrs.update({"mux_ch": int(mux_ch), "dig_ch": int(dig_ch),
                                "a0_adc_per_v": fit["a0"], "vbd_v": fit["vbd"],
                                "a0_err": fit["a0_err"], "vbd_err_v": fit["vbd_err"],
                                "fit_ok": int(fit["ok"]),
                                "flags": json.dumps(res["flags"])})
                h5.flush()
            record_fit(plan.table, mux_ch, dig_ch, fit, {
                "ts": stamp,
                "h5": _rel_data_path(h5_path),
                "threshold_adc": int(plan.threshold_adc),
                "points": [[p["bias_v"], round(p["median"], 1),
                            _round_or_none(p["median_err"], 1), p["n"]]
                           for p in pts],
                "flags": res["flags"]}, store=usable)
            if on_channel is not None:
                on_channel(res)
    finally:
        try:
            P.bias_off(elec)
        except Exception:
            log.exception("bias off after initial characterization failed")
        if h5 is not None:
            h5.close()
    return results


def _rel_data_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def h5_path_for(table: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(CHAR_DIR / safe_name(table) / f"initial_{stamp}.h5")


# ---------------------------------------------------------------------------
# Background job (one at a time, independent of any browser)
# ---------------------------------------------------------------------------

class InitialCharJob:

    def __init__(self, max_log_lines: int = 300):
        self._lock = threading.RLock()
        self._running = False
        self._abort = {"flag": False}
        self.generation = 0
        self.version = 0
        self.status = "idle"
        self.plan: Optional[InitialPlan] = None
        self.results: list[dict] = []
        self.h5_path: Optional[str] = None
        self.labbook_id: Optional[str] = None
        self.error: Optional[str] = None
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.point_idx = 0
        self.logs: deque = deque(maxlen=max_log_lines)
        self._log_seq = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def claim(self, plan: InitialPlan) -> bool:
        _check_plan(plan)
        load_table(plan.table)          # results are recorded there
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._abort["flag"] = False
            self.generation += 1
            self.status = "running"
            self.plan = plan
            self.results = []
            self.h5_path = h5_path_for(plan.table)
            self.labbook_id = None
            self.error = None
            self.started, self.finished = time.time(), None
            self.point_idx = 0
            self.logs.clear()
            self._touch()
            return True

    def request_abort(self) -> None:
        with self._lock:
            if self._running:
                self._abort["flag"] = True
                self.log("stop requested: finishing the current acquisition")

    def log(self, text: str) -> None:
        with self._lock:
            self._log_seq += 1
            self.logs.append((self._log_seq,
                              f"[{time.strftime('%H:%M:%S')}] {text}"))
        log.info("initial char: %s", text)

    def _touch(self) -> None:
        self.version += 1

    def _on_point(self, res, point) -> None:
        with self._lock:
            if not self.results or self.results[-1] is not res:
                self.results.append(res)
            self.point_idx += 1
            self._touch()

    def _on_channel(self, res) -> None:
        with self._lock:
            self._touch()

    def progress(self) -> float:
        with self._lock:
            if self.plan is None:
                return 0.0
            total = len(self.plan.channels) * len(self.plan.ov_v)
            return min(1.0, self.point_idx / total) if total else 0.0

    def snapshot(self) -> dict:
        """Consistent copy for a UI timer (the worker appends meanwhile)."""
        with self._lock:
            return {"generation": self.generation, "version": self.version,
                    "status": self.status, "running": self._running,
                    "progress": self.progress(), "plan": self.plan,
                    "results": list(self.results), "logs": tuple(self.logs),
                    "h5_path": self.h5_path, "labbook_id": self.labbook_id,
                    "error": self.error, "started": self.started,
                    "finished": self.finished}

    def run_claimed(self, instruments: dict, *, on_acquisition=None,
                    on_finish: Optional[Callable] = None) -> list[dict]:
        plan = self.plan
        try:
            self.log(f"{len(plan.channels)} channel(s) · bias "
                     + ", ".join(f"{v:.2f}" for v in plan.bias_points())
                     + f" V · {plan.n_waveforms} waveforms per point · "
                       f"threshold {plan.threshold_adc} ADC")
            results = run_initial(plan, instruments, h5_path=self.h5_path,
                                  on_point=self._on_point,
                                  on_channel=self._on_channel,
                                  on_log=self.log,
                                  on_acquisition=on_acquisition,
                                  abort=self._abort)
            with self._lock:
                self.results = results
                self.status = "stopped" if self._abort["flag"] else "done"
            return results
        except Exception as exc:
            with self._lock:
                self.status = "failed"
                self.error = f"{type(exc).__name__}: {exc}"
            self.log(f"FAIL: {self.error}")
            log.exception("initial characterization failed")
            raise
        finally:
            if on_finish is not None:
                try:
                    on_finish(self)
                except Exception as exc:
                    self.log(f"lab book entry failed: {type(exc).__name__}: {exc}")
                    log.exception("initial characterization lab book post failed")
            with self._lock:
                self.finished = time.time()
                self._running = False
                self._touch()


# ---------------------------------------------------------------------------
# Plots and the lab book entry
# ---------------------------------------------------------------------------

_POINT_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
                 "#8c564b"]


def render_channel_png(res: dict, plan: InitialPlan) -> bytes:
    """Sample waveforms, amplitude histograms and the A(V) line for one
    channel, as a PNG for the lab book."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=(13, 3.8), dpi=90)
    FigureCanvasAgg(fig)
    ax_w, ax_h, ax_f = fig.subplots(1, 3)
    pts = res["points"]
    dt_us = 1e6 / SAMPLE_RATE_HZ
    for i, p in enumerate(pts):
        c = _POINT_COLORS[i % len(_POINT_COLORS)]
        s = p["samples"]
        if s.size:
            t = np.arange(s.shape[1]) * dt_us
            for j, w in enumerate(s):
                ax_w.plot(t, w, color=c, lw=0.6, alpha=0.7,
                          label=f"{p['bias_v']:.2f} V" if j == 0 else None)
    ax_w.axhline(plan.threshold_adc, color="k", ls=":", lw=0.8,
                 label=f"threshold {plan.threshold_adc}")
    ax_w.set_xlabel("time (µs)")
    ax_w.set_ylabel("ADC counts above baseline")
    ax_w.set_title(f"{N_SAMPLE_WAVEFORMS} sample waveforms per bias", fontsize=9)
    ax_w.legend(fontsize=7, loc="upper right")

    all_amps = [p["amplitudes"] for p in pts if p["n"]]
    if all_amps:
        hi = max(float(np.percentile(a, 99.5)) for a in all_amps) * 1.1
        edges = np.linspace(0, max(hi, plan.threshold_adc * 2), 61)
        for i, p in enumerate(pts):
            if not p["n"]:
                continue
            c = _POINT_COLORS[i % len(_POINT_COLORS)]
            ax_h.hist(p["amplitudes"], bins=edges, histtype="step", color=c,
                      label=f"{p['bias_v']:.2f} V: median {p['median']:.0f}")
            ax_h.axvline(p["median"], color=c, ls="--", lw=0.8)
    ax_h.axvline(plan.threshold_adc, color="k", ls=":", lw=0.8)
    ax_h.set_xlabel("pulse amplitude (ADC)")
    ax_h.set_ylabel("pulses")
    ax_h.set_title("amplitude spectrum", fontsize=9)
    ax_h.legend(fontsize=7)

    fit = res.get("fit") or {}
    good = [p for p in pts if p["n"]]
    ax_f.errorbar([p["bias_v"] for p in good], [p["median"] for p in good],
                  yerr=[p["median_err"] for p in good], fmt="o", color="k",
                  ms=4, capsize=3)
    if fit.get("ok"):
        vmax = max(p["bias_v"] for p in good) + 0.5
        vv = np.array([fit["vbd"], vmax])
        ax_f.plot(vv, fit["a0"] * (vv - fit["vbd"]), color="#d62728", lw=1)
        ax_f.axvline(fit["vbd"], color="#d62728", ls=":", lw=0.8)
        ax_f.set_title(f"V_BD {fit['vbd']:.3f} ± {fit['vbd_err']:.3f} V · "
                       f"A0 {fit['a0']:.1f} ± {fit['a0_err']:.1f} ADC/V",
                       fontsize=9)
    else:
        ax_f.set_title("no fit", fontsize=9)
    ax_f.axhline(0, color="0.6", lw=0.6)
    ax_f.set_xlabel("bias (V)")
    ax_f.set_ylabel("median amplitude (ADC)")
    for ax in (ax_w, ax_h, ax_f):
        ax.grid(alpha=0.3)
        ax.tick_params(labelsize=8)
    fig.suptitle(f"{plan.table} · MUX ch {res['mux_ch']} · digitizer ch "
                 f"{res['dig_ch']}", fontsize=10)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def render_summary_png(results: list[dict], plan: InitialPlan) -> bytes:
    """V_BD and A0 for every fitted channel of a run. The axis range and
    the mean follow the unflagged channels; flagged ones are drawn in
    orange (at the edge, with their MUX number, when off scale)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fitted = [r for r in results if r.get("fit") and r["fit"]["ok"]]
    clean = [r for r in fitted if not r["flags"]]
    fig = Figure(figsize=(11, 3.6), dpi=90)
    FigureCanvasAgg(fig)
    ax_v, ax_a = fig.subplots(1, 2)
    for ax, key, ekey, lab in ((ax_v, "vbd", "vbd_err", "V_BD (V)"),
                               (ax_a, "a0", "a0_err", "A0 (ADC/V)")):
        y = np.array([r["fit"][key] for r in clean])
        ax.errorbar([r["mux_ch"] for r in clean], y,
                    yerr=[r["fit"][ekey] for r in clean], fmt="o", color="k",
                    ms=4, capsize=2)
        if y.size:
            sd = float(np.std(y, ddof=1)) if y.size > 1 else 0.0
            ax.axhline(float(np.mean(y)), color="#d62728", lw=0.8,
                       label=f"mean {np.mean(y):.4g}, sd {sd:.2g} "
                             f"({y.size} unflagged)")
            pad = max(3 * sd, 0.05 * abs(float(np.mean(y))), 1e-3)
            lo, hi = float(y.min()) - pad, float(y.max()) + pad
            ax.set_ylim(lo, hi)
            for r in fitted:
                if not r["flags"]:
                    continue
                v = r["fit"][key]
                vy = min(max(v, lo), hi)
                ax.plot([r["mux_ch"]], [vy], marker="v" if v < lo else
                        "^" if v > hi else "o", color="#ff7f0e", ms=6)
                if vy != v:
                    ax.annotate(f"MUX {r['mux_ch']}: {v:.3g}", (r["mux_ch"], vy),
                                fontsize=7, color="#ff7f0e",
                                xytext=(4, -10 if v < lo else 4),
                                textcoords="offset points")
            ax.legend(fontsize=8)
        ax.set_xlabel("MUX channel")
        ax.set_ylabel(lab)
        ax.grid(alpha=0.3)
    n_flag = sum(1 for r in results if r["flags"])
    fig.suptitle(f"{plan.table}: initial characterization, "
                 f"{len(fitted)}/{len(results)} channels fitted, "
                 f"{n_flag} flagged (orange)", fontsize=10)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


def labbook_text(job: InitialCharJob) -> tuple[str, str]:
    plan, results = job.plan, job.results
    t = f"{plan.temperature_K:g} K" if plan.temperature_K is not None else "T not set"
    chans = ",".join(str(r["mux_ch"]) for r in results) or "none"
    subject = (f"Initial characterization · {plan.table} · {t} · "
               f"MUX ch {chans}")
    lines = [
        f"Automatic entry from the digitizer tab (started by {plan.user or 'anonymous'}); "
        f"status: {job.status}.",
        f"V_BD estimate {plan.vbd_est_v:.2f} V; overvoltages "
        + ", ".join(f"{ov:g}" for ov in plan.ov_v) + " V -> bias "
        + ", ".join(f"{v:.2f}" for v in plan.bias_points()) + " V.",
        f"{plan.n_waveforms} waveforms per point, self trigger at "
        f"{plan.threshold_adc} ADC, window -{plan.pre_us:g}/+{plan.post_us:g} µs, "
        f"settle {plan.settle_s:g} s.",
        f"A = median pulse amplitude; fit A = A0 (V - V_BD). Table: {plan.table}.",
        f"HDF5: {_rel_data_path(job.h5_path)}",
        "",
    ]
    for r in results:
        head = f"MUX {r['mux_ch']} (dig {r['dig_ch']}): "
        pts = "; ".join(f"{p['bias_v']:.2f} V: A {p['median']:.0f} ± "
                        f"{p['median_err']:.0f} (n {p['n']}, 0.5 SPE "
                        f"{p['median'] / 2:.0f})" for p in r["points"])
        fit = r.get("fit")
        if fit and fit["ok"]:
            res = (f"V_BD {fit['vbd']:.3f} ± {fit['vbd_err']:.3f} V, "
                   f"A0 {fit['a0']:.1f} ± {fit['a0_err']:.1f} ADC/V, "
                   f"SPE at OV+3 {3 * fit['a0']:.0f} ADC")
        else:
            res = "no fit"
        lines.append(head + res)
        lines.append("    " + pts)
        for fl in r["flags"]:
            lines.append("    CHECK: " + fl)
    clean = [r["fit"]["vbd"] for r in results
             if r.get("fit") and r["fit"]["ok"] and not r["flags"]]
    n_flag = sum(1 for r in results if r["flags"])
    if len(clean) > 1:
        lines += ["", f"V_BD over {len(clean)} unflagged channels: mean "
                      f"{np.mean(clean):.3f} V, sd {np.std(clean, ddof=1):.3f} V"]
    if n_flag:
        lines.append(f"{n_flag} channel(s) flagged CHECK above.")
    if job.error:
        lines += ["", f"Run ended with an error: {job.error}"]
    return subject, "\n".join(lines)


def post_labbook(job: InitialCharJob, slowcontrol=None) -> Optional[str]:
    """One lab book entry for the run: text table plus a plot per channel
    (and a V_BD/A0 summary when there are several). Returns the entry id,
    or None when no channel produced data."""
    from . import labbook

    plan, results = job.plan, [r for r in job.results if r["points"]]
    if not results:
        return None
    attachments = []
    if len(results) > 1:
        attachments.append(labbook.save_attachment(
            f"initial_{safe_name(plan.table)}_summary.png",
            render_summary_png(results, plan)))
    for r in results:
        attachments.append(labbook.save_attachment(
            f"initial_{safe_name(plan.table)}_mux{r['mux_ch']:02d}.png",
            render_channel_png(r, plan)))
    subject, body = labbook_text(job)
    entry, _ = labbook.append(plan.user or "anonymous", subject, body,
                              attachments, slowcontrol=slowcontrol)
    return entry["id"]


# ---------------------------------------------------------------------------
# Coarse sweep (Level 3 specs from a table)
# ---------------------------------------------------------------------------

def coarse_specs(table: dict, *, ov_v, n_waveforms: int = 1000,
                 threshold_pe: float = 0.5, pre_us: float = 2.0,
                 post_us: float = 10.0, store_raw: bool = True,
                 mux_channels=None) -> tuple[list, list[str]]:
    """One L3 pulse spec per table row that has V_BD and A0: bias points at
    V_BD + each OV, trigger at threshold_pe SPE at every point.

    Returns (specs, skipped): skipped is [(mux_ch, reason), ...] for the
    rows left out.
    """
    from .sequence import MeasurementSpec

    want = None if mux_channels is None else {int(c) for c in mux_channels}
    specs, skipped = [], []
    for r in table["rows"]:
        if want is not None and r["mux_ch"] not in want:
            continue
        if r.get("vbd_v") is None or r.get("a0_adc_per_v") is None:
            skipped.append((r["mux_ch"], "no V_BD/A0"))
            continue
        vbd, a0 = float(r["vbd_v"]), float(r["a0_adc_per_v"])
        if vbd <= 0 or a0 <= 0:
            skipped.append((r["mux_ch"], "V_BD or A0 not positive"))
            continue
        bias = [round(vbd + float(ov), 3) for ov in ov_v]
        sipm = r.get("sipm")
        specs.append(MeasurementSpec(
            sipm_id=int(sipm) if sipm is not None else int(r["mux_ch"]),
            mux_channel=int(r["mux_ch"]),
            temperature_K=float(table.get("temperature_K") or 298.0),
            label=f"{table['name']} coarse mux{r['mux_ch']}",
            dark=True, illuminated=False,
            do_iv=False, do_pulse=True, do_scan=False,
            pulse_bias_v=bias,
            pulse_capture_ch=int(r["dig_ch"]),
            pulse_threshold_adc=max(1, threshold_adc(a0, vbd, bias[0],
                                                     threshold_pe)),
            pulse_pre_us=float(pre_us), pulse_post_us=float(post_us),
            pulse_store_waveforms=bool(store_raw),
            pulse_batch_size=min(1000, int(n_waveforms)),
            n_waveforms_dark=int(n_waveforms), n_waveforms_illum=0,
            vbd_v=vbd, spe_adc_per_v=a0, pulse_threshold_pe=float(threshold_pe),
        ))
    return specs, skipped
