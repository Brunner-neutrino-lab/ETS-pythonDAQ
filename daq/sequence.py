"""
daq/sequence.py  —  Level 3 (sequence)

Scripted L2: a list of per-SiPM measurement specs run sequentially. Each
``MeasurementSpec`` is one list entry and carries every parameter that defines
a SiPM's measurements (placement, conditions, and the IV / pulse / scan
parameters). The same SiPM may appear more than once — that is a deliberate
repeat — so steps and HDF5 groups are keyed by the entry's list index.

The runner reuses the Level-1 primitives (``daq.primitives``), the digitizer
controller, and the AWG, driven entirely by the spec rather than by
``ExperimentConfig``. It writes a single consolidated ``run.h5`` via
``daq.storage.RunFile`` under a repeat-safe ``/seq/<idx>/...`` layout and tracks
resume through ``daq.resume.RunManifest``.

L2 (``daq.measurement``) is intentionally untouched: its functions look up
placement from config and cannot configure the digitizer window/thresholds or
drive the AWG, so the spec-driven glue lives here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import primitives as P
from .resume import Step

log = logging.getLogger("daq.sequence")

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MeasurementSpec:
    """One list entry — all measurements for a single SiPM placement."""

    # --- identity / placement (explicit, not looked up from config) ---
    sipm_id:        int   = 1
    mux_channel:    int   = 1
    x_mm:           float = 0.0          # bright / illuminated location
    y_mm:           float = 0.0
    dark_x_mm:      Optional[float] = None   # falls back to (x_mm, y_mm) if None
    dark_y_mm:      Optional[float] = None
    temperature_K:  float = 298.0
    label:          str   = ""

    # --- conditions (both True -> dark then illuminated) ---
    dark:           bool = True
    illuminated:    bool = False

    # --- which measurements to run ---
    do_iv:          bool = True
    do_pulse:       bool = False
    do_scan:        bool = False

    # --- IV sweep ---
    iv_voltages:        list = field(default_factory=list)   # explicit voltage list
    iv_meter:           str   = "k6485"      # "k6485" | "b2987"
    iv_delay_s:         float = 0.1
    n_iv_samples_dark:  int   = 5            # readings per voltage, dark
    n_iv_samples_illum: int   = 5            # readings per voltage, illuminated

    # --- pulse acquisition (bias sweep) ---
    pulse_bias_v:        list = field(default_factory=list)   # one acquisition per point
    pulse_capture_ch:    int  = 0            # digitizer channel (0-63)
    pulse_threshold_adc: int  = 50
    pulse_aux_ch:        Optional[int] = None
    pulse_aux_thr_adc:   Optional[int] = None
    pulse_pre_us:        float = 2.0
    pulse_post_us:       float = 10.0
    pulse_store_waveforms: bool = False
    pulse_batch_size:    int   = 1000
    n_waveforms_dark:    int   = 10000
    n_waveforms_illum:   int   = 10000

    # --- 1-D scan (runs once, in its own condition) ---
    scan_axis:        str   = "x"            # "x" | "y"
    scan_light:       str   = "vuv"          # "vuv" (ch1) | "laser" (ch2)
    scan_meter:       str   = "k6485"
    scan_bias_v:      float = 49.0
    scan_start_mm:    float = -7.5
    scan_stop_mm:     float = 7.5
    scan_step_mm:     float = 0.5
    n_scan_samples:   int   = 5              # readings per position
    scan_settle_s:    float = 0.1
    scan_illuminated: bool  = True
    scan_freq_hz:     float = 1000.0
    scan_amp_v:       float = 1.0
    scan_offset_v:    float = 0.0
    scan_width_s:     float = 1e-6


@dataclass
class SequenceFile:
    """A named, ordered list of measurement specs (the unit saved to YAML)."""
    entries:        list = field(default_factory=list)   # list[MeasurementSpec]
    name:           str  = ""
    description:    str  = ""
    created:        str  = ""
    schema_version: int  = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# YAML serialization
# ---------------------------------------------------------------------------

def spec_to_dict(spec: MeasurementSpec) -> dict:
    return dataclasses.asdict(spec)


def spec_from_dict(d: dict) -> MeasurementSpec:
    """Build a MeasurementSpec, ignoring unknown keys (forward/backward compat)."""
    fields = MeasurementSpec.__dataclass_fields__
    clean = {k: v for k, v in d.items() if k in fields}
    return MeasurementSpec(**clean)


def save_sequence(seq: SequenceFile, path: str) -> None:
    import time
    import yaml
    if not seq.created:
        seq.created = time.strftime("%Y-%m-%dT%H:%M:%S")
    data = {
        "schema_version": seq.schema_version,
        "name":           seq.name,
        "description":    seq.description,
        "created":        seq.created,
        "entries":        [spec_to_dict(s) for s in seq.entries],
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    log.info("sequence saved: %s (%d entries)", path, len(seq.entries))


def load_sequence(path: str) -> SequenceFile:
    import yaml
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    entries = [spec_from_dict(d) for d in data.get("entries", [])]
    return SequenceFile(
        entries        = entries,
        name           = data.get("name", ""),
        description    = data.get("description", ""),
        created        = data.get("created", ""),
        schema_version = data.get("schema_version", SCHEMA_VERSION),
    )


def sequence_hash(specs: list) -> str:
    """Stable hash of a spec list — used to detect YAML drift across resumes."""
    blob = json.dumps([spec_to_dict(s) for s in specs], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Leaf / step enumeration
# ---------------------------------------------------------------------------

def _cond_key(illuminated: bool) -> str:
    return "illuminated" if illuminated else "dark"


def _bias_mv(bias_v: float) -> int:
    return int(round(float(bias_v) * 1000))


def _spec_leaves(spec: MeasurementSpec) -> list:
    """Ordered list of leaf measurements for one spec.

    Each leaf is a dict: kind, illuminated, bias_v, axis, step_name.
    """
    leaves: list = []
    conditions = []
    if spec.dark:
        conditions.append(False)
    if spec.illuminated:
        conditions.append(True)

    for illum in conditions:
        cond = _cond_key(illum)
        if spec.do_iv:
            leaves.append({"kind": "iv", "illuminated": illum, "bias_v": None,
                           "axis": None, "step_name": f"iv {cond}"})
        if spec.do_pulse:
            for bias_v in spec.pulse_bias_v:
                leaves.append({"kind": "pulse", "illuminated": illum,
                               "bias_v": float(bias_v), "axis": None,
                               "step_name": f"pulse {cond} {bias_v:.2f}V"})

    if spec.do_scan:
        leaves.append({"kind": "scan", "illuminated": spec.scan_illuminated,
                       "bias_v": None, "axis": spec.scan_axis,
                       "step_name": f"scan {spec.scan_axis}"})
    return leaves


def _step_id(idx: int, spec: MeasurementSpec, leaf: dict) -> str:
    cond = _cond_key(leaf["illuminated"])
    if leaf["kind"] == "iv":
        return f"seq{idx}_s{spec.sipm_id}_iv_{cond}"
    if leaf["kind"] == "pulse":
        return f"seq{idx}_s{spec.sipm_id}_pulse_{cond}_b{_bias_mv(leaf['bias_v'])}"
    return f"seq{idx}_s{spec.sipm_id}_scan_{leaf['axis']}"


def build_sequence_steps(specs: list) -> list:
    """Generate resume Steps for the whole spec list, in execution order."""
    steps = []
    for idx, spec in enumerate(specs):
        for leaf in _spec_leaves(spec):
            steps.append(Step(
                step_id       = _step_id(idx, spec, leaf),
                kind          = leaf["kind"],
                illuminated   = bool(leaf["illuminated"]),
                sipm_id       = spec.sipm_id,
                temperature_K = spec.temperature_K,
                mux_channel   = spec.mux_channel,
                seq_index     = idx,
                bias_v        = leaf["bias_v"],
                axis          = leaf["axis"],
            ))
    return steps


# ---------------------------------------------------------------------------
# Spec-driven executors (mirror the L2 run handlers, parameterized by spec)
# ---------------------------------------------------------------------------

def _awg_pulse_on(awg, ks_ch, freq, amp, offset, width_s):
    if awg is None:
        raise RuntimeError("Keysight 33500B not connected — required for "
                           "illuminated / scan illumination.")
    awg.set_load("INF", channel=ks_ch)
    awg.apply_pulse(float(freq), float(amp), float(offset), 0.0, channel=ks_ch)
    awg.configure_pulse(period_s=1.0 / max(float(freq), 1e-9),
                        width_s=float(width_s), channel=ks_ch)
    awg.output_on(ks_ch)


def _awg_off(awg, ks_ch):
    if awg is None:
        return
    try:
        awg.output_off(ks_ch)
    except Exception:
        pass


def _move_for_condition(instruments, spec: MeasurementSpec, illuminated: bool,
                        config):
    stage = instruments.get("stage")
    mux   = instruments.get("mux")
    if illuminated:
        x, y = spec.x_mm, spec.y_mm
    else:
        x = spec.dark_x_mm if spec.dark_x_mm is not None else spec.x_mm
        y = spec.dark_y_mm if spec.dark_y_mm is not None else spec.y_mm
    if stage is not None:
        P.move_stage(stage, float(x), float(y),
                     deenergize_after=getattr(config, "stage_deenergize", True))
    if mux is not None:
        P.select_channel(mux, int(spec.mux_channel))


def _exec_iv(spec, instruments, config, illuminated):
    elec = instruments.get("elec")
    if elec is None:
        raise RuntimeError("electrometer not connected")
    if not spec.iv_voltages:
        raise RuntimeError("iv_voltages is empty")
    n = spec.n_iv_samples_illum if illuminated else spec.n_iv_samples_dark
    awg = instruments.get("ks33500b")
    _move_for_condition(instruments, spec, illuminated, config)
    try:
        if illuminated:
            _awg_pulse_on(awg, 1, config.led_frequency_hz, config.led_amplitude_v,
                          config.led_offset_v, config.led_pulse_width)
        if spec.iv_meter == "k6485":
            meter = instruments.get("k6485")
            if meter is None:
                raise RuntimeError("K6485 not connected (iv_meter=k6485)")
            return P.iv_sweep_external_meter(elec, meter, spec.iv_voltages,
                                             n_per_voltage=n, delay_s=spec.iv_delay_s)
        return P.iv_sweep(elec, spec.iv_voltages, n_per_voltage=n,
                          delay_s=spec.iv_delay_s)
    finally:
        if illuminated:
            _awg_off(awg, 1)
        try:
            P.bias_off(elec)
        except Exception:
            pass


def _exec_pulse(spec, instruments, config, illuminated, bias_v):
    elec = instruments.get("elec")
    dig  = instruments.get("digitizer")
    if elec is None or dig is None:
        raise RuntimeError("electrometer or digitizer not connected")
    ctrl = getattr(dig, "_ctrl", None)
    if ctrl is None:
        raise RuntimeError("digitizer backend has no controller (_ctrl)")
    n = spec.n_waveforms_illum if illuminated else spec.n_waveforms_dark
    ch  = max(0, min(63, int(spec.pulse_capture_ch)))
    thr = int(spec.pulse_threshold_adc)
    sipm_chs = [ch]
    thresholds = {ch: thr}
    if spec.pulse_aux_ch is not None and int(spec.pulse_aux_ch) != ch:
        aux = int(spec.pulse_aux_ch)
        sipm_chs.append(aux)
        thresholds[aux] = int(spec.pulse_aux_thr_adc
                              if spec.pulse_aux_thr_adc is not None else thr)
    awg = instruments.get("ks33500b")
    _move_for_condition(instruments, spec, illuminated, config)
    try:
        if illuminated:
            _awg_pulse_on(awg, 1, config.led_frequency_hz, config.led_amplitude_v,
                          config.led_offset_v, config.led_pulse_width)
        P.set_bias(elec, float(bias_v), settle_s=0.3)
        ctrl.configure_record_window(pre_us=float(spec.pulse_pre_us),
                                     post_us=float(spec.pulse_post_us))
        ctrl.configure_channels(sipm_channels=sipm_chs, thresholds=thresholds,
                                threshold_mode="per_channel", include_pmt=False)
        ctrl.configure_trigger(mode="self")
        return ctrl.run(n_waveforms=n,
                        batch_size=min(int(spec.pulse_batch_size), n),
                        store_waveforms=bool(spec.pulse_store_waveforms),
                        timeout_s=120.0)
    finally:
        if illuminated:
            _awg_off(awg, 1)
        try:
            P.bias_off(elec)
        except Exception:
            pass


def _exec_scan(spec, instruments, config):
    """Mirror L2 run_scan: AWG on, set bias, step positions, read N per point."""
    elec  = instruments.get("elec")
    stage = instruments.get("stage")
    mux   = instruments.get("mux")
    if elec is None or stage is None:
        raise RuntimeError("electrometer or stage not connected")
    meter_name = spec.scan_meter or "k6485"
    k6485 = instruments.get("k6485")
    if meter_name == "k6485" and k6485 is None:
        raise RuntimeError("K6485 not connected (scan_meter=k6485)")
    awg = instruments.get("ks33500b")
    ks_ch = 1 if str(spec.scan_light) == "vuv" else 2

    positions = np.arange(
        float(spec.scan_start_mm),
        float(spec.scan_stop_mm) + float(spec.scan_step_mm) * 0.5,
        float(spec.scan_step_mm),
    ).astype(float)

    cx, cy = float(spec.x_mm), float(spec.y_mm)
    n = max(1, int(spec.n_scan_samples))
    means, stds, raws = [], [], []

    if mux is not None:
        P.select_channel(mux, int(spec.mux_channel))
    try:
        _awg_pulse_on(awg, ks_ch, spec.scan_freq_hz, spec.scan_amp_v,
                      spec.scan_offset_v, spec.scan_width_s)
        P.set_bias(elec, float(spec.scan_bias_v), settle_s=0.3)
        for pos in positions:
            x_t, y_t = (float(pos), cy) if spec.scan_axis == "x" else (cx, float(pos))
            P.move_stage(stage, x_t, y_t,
                         deenergize_after=getattr(config, "stage_deenergize", True))
            if spec.scan_settle_s > 0:
                import time
                time.sleep(spec.scan_settle_s)
            if meter_name == "k6485":
                arr, _ts = k6485.read_n(n, 0.0)
                arr = np.asarray(arr, dtype=np.float64)
            else:
                arr = np.array([elec.measure_current() for _ in range(n)],
                               dtype=np.float64)
            means.append(float(np.mean(arr)))
            stds.append(float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0)
            raws.extend(arr.tolist())
    finally:
        _awg_off(awg, ks_ch)
        try:
            P.bias_off(elec)
        except Exception:
            pass

    return (positions,
            np.asarray(means, dtype=np.float64),
            np.asarray(stds, dtype=np.float64),
            np.asarray(raws, dtype=np.float64))


# ---------------------------------------------------------------------------
# High-voltage interlock
# ---------------------------------------------------------------------------

def _max_commanded_voltage(specs: list) -> float:
    vmax = 0.0
    for s in specs:
        for v in list(s.iv_voltages) + list(s.pulse_bias_v) + [s.scan_bias_v]:
            try:
                vmax = max(vmax, abs(float(v)))
            except (TypeError, ValueError):
                pass
    return vmax


def _ensure_hv_confirmer(instruments, specs, hv_confirmer) -> bool:
    """Honor the electrometer interlock before any hardware moves.

    Returns True if we registered a confirmer here (so the runner restores
    deny-by-default afterwards).

    - explicit confirmer given  -> register it (returns True).
    - confirmer already armed    -> trust it (e.g. the GUI dialog bridge); the
      interlock prompts interactively for >threshold commands (returns False).
    - none, headless             -> pre-scan and fail fast if any voltage
      exceeds the threshold, rather than a silent always-allow (returns False).
    """
    elec = instruments.get("elec")
    if elec is None:
        return False
    if hv_confirmer is not None:
        elec.set_hv_confirm(hv_confirmer)
        return True
    if getattr(elec, "hv_confirm", None) is not None:
        return False
    threshold = getattr(elec, "hv_threshold", 60.0)
    vmax = _max_commanded_voltage(specs)
    if vmax > threshold:
        raise RuntimeError(
            f"sequence commands {vmax:.1f} V > {threshold:.1f} V interlock; "
            "pass an explicit hv_confirmer to run_sequence or lower the voltages.")
    return False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_sequence(specs, instruments, config,
                 run_file=None, manifest=None,
                 on_progress=None, abort=None, hv_confirmer=None) -> dict:
    """Run a list of MeasurementSpecs sequentially.

    Parameters
    ----------
    specs        : list[MeasurementSpec]
    instruments  : dict with keys elec, digitizer, mux, stage, k6485, ks33500b.
    config       : ExperimentConfig (for LED params + stage_deenergize).
    run_file     : daq.storage.RunFile (open) or None.
    manifest     : daq.resume.RunManifest or None (resume + completion log).
    on_progress  : callable(entry_idx, n_entries, step_name, done, total).
    abort        : mutable dict; runner stops cleanly when abort["flag"] is set.
    hv_confirmer : callable(vmax)->bool for >threshold voltages, or None.

    Returns
    -------
    dict summary {n_entries, n_done, n_skipped, aborted}.
    """
    elec = instruments.get("elec")
    registered_hv = _ensure_hv_confirmer(instruments, specs, hv_confirmer)

    n_entries = len(specs)
    n_done = n_skipped = 0
    aborted = False
    path = getattr(run_file, "_path", None)

    try:
        if run_file is not None:
            try:
                run_file.write_sequence_meta({
                    "schema_version": SCHEMA_VERSION,
                    "hash":           sequence_hash(specs),
                    "entries":        [spec_to_dict(s) for s in specs],
                })
            except Exception as e:
                log.warning("could not write /meta/sequence: %s", e)

        for idx, spec in enumerate(specs):
            leaves = _spec_leaves(spec)
            total  = len(leaves)
            for li, leaf in enumerate(leaves):
                if abort is not None and abort.get("flag"):
                    log.info("sequence aborted before entry %d leaf %d", idx, li)
                    aborted = True
                    return {"n_entries": n_entries, "n_done": n_done,
                            "n_skipped": n_skipped, "aborted": True}

                sid = _step_id(idx, spec, leaf)
                if manifest is not None and manifest.is_done(sid):
                    n_skipped += 1
                    if on_progress:
                        on_progress(idx, n_entries, leaf["step_name"], li + 1, total)
                    continue

                illum = leaf["illuminated"]
                if leaf["kind"] == "iv":
                    result = _exec_iv(spec, instruments, config, illum)
                    if run_file is not None:
                        run_file.write_iv_seq(
                            idx, spec.sipm_id, spec.temperature_K, illum, result,
                            attrs={"mux_channel": spec.mux_channel,
                                   "x_mm": spec.x_mm, "y_mm": spec.y_mm,
                                   "label": spec.label, "meter": spec.iv_meter})
                    grp = (f"/seq/{idx}/{spec.sipm_id}/{spec.temperature_K:.1f}K/"
                           f"{_cond_key(illum)}/iv")
                elif leaf["kind"] == "pulse":
                    bias_v = leaf["bias_v"]
                    result = _exec_pulse(spec, instruments, config, illum, bias_v)
                    if run_file is not None:
                        run_file.write_pulse_seq(
                            idx, spec.sipm_id, spec.temperature_K, illum, bias_v,
                            result,
                            attrs={"mux_channel": spec.mux_channel,
                                   "x_mm": spec.x_mm, "y_mm": spec.y_mm,
                                   "label": spec.label,
                                   "capture_ch": spec.pulse_capture_ch,
                                   "threshold_adc": spec.pulse_threshold_adc,
                                   "aux_ch": spec.pulse_aux_ch,
                                   "aux_thr_adc": spec.pulse_aux_thr_adc})
                    grp = (f"/seq/{idx}/{spec.sipm_id}/{spec.temperature_K:.1f}K/"
                           f"{_cond_key(illum)}/pulse/{_bias_mv(bias_v)}mV")
                else:  # scan
                    pos, means, stds, raws = _exec_scan(spec, instruments, config)
                    if run_file is not None:
                        run_file.write_scan_seq(
                            idx, spec.sipm_id, spec.temperature_K, spec.scan_axis,
                            pos, means, stds, raws,
                            attrs={"mux_channel": spec.mux_channel,
                                   "bias_v": spec.scan_bias_v,
                                   "meter": spec.scan_meter,
                                   "light": spec.scan_light,
                                   "label": spec.label})
                    grp = (f"/seq/{idx}/{spec.sipm_id}/{spec.temperature_K:.1f}K/"
                           f"scan/{spec.scan_axis}")

                if manifest is not None:
                    manifest.mark_done(sid, hdf5_path=path, hdf5_group=grp)
                n_done += 1
                if on_progress:
                    on_progress(idx, n_entries, leaf["step_name"], li + 1, total)

        return {"n_entries": n_entries, "n_done": n_done,
                "n_skipped": n_skipped, "aborted": aborted}
    finally:
        # Restore deny-by-default only if we registered the confirmer here, so
        # we never disarm a pre-existing one (e.g. the GUI dialog bridge).
        if elec is not None and registered_hv:
            try:
                elec.set_hv_confirm(None)
            except Exception:
                pass
        if elec is not None:
            try:
                P.bias_off(elec)
            except Exception:
                pass
