"""
scripts/bench_test.py

End-to-end bench-setup smoke test for a single SiPM in the dark at RT,
no MUX, no temperature control, no stage.

Setup assumed (per user description):
  - Keysight B2987A: HV bias source on the cathode through a bias-T
  - Keithley 6485: low-side photocurrent monitor
  - Rigol DG1022 ch 1: LED pulse driver (fiber-coupled to SiPM)
  - CAEN VX2740 ch 0: receives the amplified pulse signal through a
    10 dB attenuator, after Cremat CSP + shaper
  - (RTO2024 also tapped on the same signal at 1 MΩ, but not used here)

Tests executed, all results saved to one HDF5 file under /<group>/:
  /meta            run-level attributes
  /iv              dark IV sweep (B2987)
  /k6485/dark      averaged photocurrent at modest bias, LED off
  /k6485/light     averaged photocurrent at modest bias, LED on
  /vx2740          pulse waveforms + amplitudes + per-channel spectra
                   acquired with LED on, bias = V_BD + over-V

Closed-loop usage:
    python scripts/bench_test.py
The script catches per-step exceptions, logs them, and continues so a
single failure doesn't abort the whole run.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import argparse
import datetime
from pathlib import Path

import numpy as np
import h5py

# Make sibling submodules importable
_ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("keysight2987b-python", "keithley6485-python",
             "vx2740-python", "rigoldg1022-python"):
    p = _ROOT / _pkg
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
sys.path.insert(0, str(_ROOT))

from b2987b           import B2987BController
from keithley6485     import K6485Driver
from vx2740.controller import VX2740Controller
from dg1022           import DG1022Controller


# ---------------------------------------------------------------------------
# Configuration — defaults for this bench. Override on the command line.
# ---------------------------------------------------------------------------

DEFAULT_CFG = dict(
    # Addresses — must match what the user has on the lab subnet
    b2987_visa      = "TCPIP::172.16.0.11::INSTR",
    k6485_port      = "/dev/ttyUSB0",
    k6485_baud      = 9600,
    k6485_read_term = "\r",
    k6485_wr_term   = "\r",
    vx2740_address  = "172.16.0.51",
    wfg_visa        = "/dev/usbtmc0",

    # B2987 sweep range — broad enough to capture any plausible V_BD
    iv_v_start      = 0.0,
    iv_v_stop       = 55.0,
    iv_v_step       = 0.5,
    iv_pts_per_v    = 3,
    iv_delay_s      = 0.05,
    iv_current_range_auto = True,   # auto-range; fixed range overflows at V_BD
    iv_current_range_lower_a = 2e-12,
    iv_current_range_upper_a = 2e-3,
    iv_current_aperture  = 0.02,    # 20 ms integration per point

    # K6485 baseline read at TWO biases: well below V_BD (no SPAD gain — diagnostic)
    # and just above V_BD (gain on; LED on/off should now be visible).
    bias_for_k6485       = 30.0,    # below V_BD — control
    bias_for_k6485_above = 1.0,     # add to V_BD; e.g. V_BD + 1 V
    k6485_n_samples = 20,
    k6485_delay_s   = 0.05,

    # DG1022 LED pulse train
    led_frequency_hz = 1_000.0,
    led_amplitude_v  = 5.0,
    led_offset_v     = 2.5,
    led_pulse_width  = 1e-7,    # 100 ns pulse
    led_load         = "INF",

    # VX2740 acquisition
    vx2740_pre_us         = 2.0,
    vx2740_post_us        = 10.0,
    vx2740_n_waveforms    = 1000,
    vx2740_self_thresh    = 50,     # ADC counts (Relative) — lowered from 200
    vx2740_over_voltage   = 3.0,    # V above estimated V_BD (single-point check)
    vx2740_swtrig_probe_n = 5,      # SW-triggered probe before self-trigger
    # Over-voltage scan: list of (V_BD + offset) values. The script enforces
    # a small minimum so it doesn't sweep below the gain region.
    ov_scan_volts         = [1.0, 2.0, 3.0, 4.0, 5.0],
    ov_scan_n_waveforms   = 500,    # per point — keep run-time reasonable
)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log = logging.getLogger("bench_test")


# ---------------------------------------------------------------------------
# Step helper — runs fn, catches exceptions, records pass/fail
# ---------------------------------------------------------------------------

class StepResult:
    def __init__(self):
        self.steps: list[dict] = []

    def add(self, name: str, ok: bool, msg: str = ""):
        self.steps.append({"name": name, "ok": ok, "msg": msg})
        marker = "PASS" if ok else "FAIL"
        log.info("[%s] %s%s", marker, name, f" — {msg}" if msg else "")

    def summary(self) -> str:
        ok_n  = sum(1 for s in self.steps if s["ok"])
        n     = len(self.steps)
        lines = [f"  {'PASS' if s['ok'] else 'FAIL'}  {s['name']}"
                 + (f" — {s['msg']}" if s["msg"] else "") for s in self.steps]
        return f"\n=== summary: {ok_n}/{n} PASS ===\n" + "\n".join(lines)


def step(results: StepResult, name: str):
    """Decorator-ish context manager that records pass/fail per step."""
    class _Ctx:
        def __enter__(self_):
            log.info("--- %s ---", name)
            self_.t0 = time.time()
            return self_
        def __exit__(self_, exc_type, exc, tb):
            dt = time.time() - self_.t0
            if exc is None:
                results.add(name, True, f"({dt:.1f}s)")
            else:
                results.add(name, False, f"{exc_type.__name__}: {exc} ({dt:.1f}s)")
                log.exception("step %s raised:", name)
                return True   # suppress so next step can run
    return _Ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_connect_all(cfg: dict, results: StepResult) -> dict:
    """Open all four instruments. Returns the live objects (or None on fail)."""
    instr: dict = {}

    with step(results, "B2987 connect"):
        e = B2987BController(visa=cfg["b2987_visa"], mode="hardware")
        e.connect()
        log.info("  B2987 IDN: %s", e.identify())
        e.configure_sweep(
            source_range          = 1000,
            n_per_voltage         = cfg["iv_pts_per_v"],
            delay_s               = cfg["iv_delay_s"],
            measure_voltage       = False,
            current_limit         = False,
            current_range_auto    = cfg["iv_current_range_auto"],
            current_range_lower_a = cfg["iv_current_range_lower_a"],
            current_range_upper_a = cfg["iv_current_range_upper_a"],
            current_aperture_mode = "FIXED",
            current_aperture_s    = cfg["iv_current_aperture"],
            zero_reference        = True,
        )
        instr["b2987"] = e

    with step(results, "K6485 connect"):
        k = K6485Driver(visa=cfg["k6485_port"], mode="hardware",
                        baud_rate=cfg["k6485_baud"],
                        read_termination=cfg["k6485_read_term"],
                        write_termination=cfg["k6485_wr_term"])
        k.connect()
        k.reset()
        k.zero_check_off()
        k.set_range("AUTO")
        instr["k6485"] = k

    with step(results, "VX2740 connect"):
        v = VX2740Controller(address=cfg["vx2740_address"], mode="hardware")
        v.connect()
        log.info("  VX2740 IDN: %s", v.identify())
        instr["vx2740"] = v

    with step(results, "DG1022 connect"):
        d = DG1022Controller(visa=cfg["wfg_visa"], mode="hardware")
        d.connect()
        log.info("  DG1022 IDN: %s", d.identify())
        instr["dg1022"] = d

    return instr


def test_dark_iv(cfg: dict, h5: h5py.File, instr: dict, results: StepResult) -> float | None:
    """Run the dark IV sweep. Returns an estimated V_BD or None."""
    e = instr.get("b2987")
    dg = instr.get("dg1022")
    if e is None:
        results.add("dark IV", False, "B2987 not connected"); return None

    v_bd = None
    with step(results, "ensure LED off for dark IV"):
        if dg is not None:
            dg.output_off(1)
            dg.output_off(2)

    with step(results, "dark IV sweep"):
        voltages = np.arange(cfg["iv_v_start"],
                             cfg["iv_v_stop"] + cfg["iv_v_step"] * 0.5,
                             cfg["iv_v_step"])
        log.info("  sweeping %s V → %s V in %d steps × %d reps",
                 voltages[0], voltages[-1], len(voltages), cfg["iv_pts_per_v"])
        r = e.sweep(voltages.tolist())
        log.info("  swept %d unique V, |I|_max=%.3e A",
                 len(r.avg_source_v), float(np.abs(r.avg_current_a).max()))

        # Estimate V_BD from |dI/dV| max in the upper half of the sweep.
        # Keysight returns 9.91e+37 when a measurement is over-range / invalid;
        # mask those out before any derivative work.
        v_raw = np.asarray(r.avg_source_v)
        i_raw = np.asarray(r.avg_current_a)
        valid = np.abs(i_raw) < 1e-3   # 1 mA is more than any sane SiPM dark IV
        v = v_raw[valid]; i = i_raw[valid]
        n_overrange = int((~valid).sum())
        if n_overrange:
            log.warning("  %d of %d sweep points overrange (9.91e+37); "
                        "auto-range may not have followed the runaway. "
                        "Estimating V_BD from valid points only.", n_overrange, len(v_raw))
        absi = np.abs(i)
        if len(v) > 5 and absi.max() > 1e-9:
            log_i = np.log10(np.clip(absi, 1e-15, None))
            dlogi = np.diff(log_i)
            v_mid = 0.5 * (v[:-1] + v[1:])
            # Avoid the low-bias leakage region; V_BD is the steepest rise.
            mask = v_mid > 30.0
            if mask.any():
                idx = int(np.argmax(dlogi[mask]))
                v_bd = float(v_mid[mask][idx])
                log.info("  estimated V_BD ≈ %.1f V (max d log|I|/dV)", v_bd)
            else:
                log.warning("  no valid points above 30V; cannot estimate V_BD")
        else:
            log.warning("  current never exceeded 1 nA; sweep too low or detector disconnected")

        # Save to HDF5
        g = h5.create_group("iv")
        g.create_dataset("source_v",     data=np.asarray(r.avg_source_v,    dtype=np.float64), compression="gzip")
        g.create_dataset("current_a",    data=np.asarray(r.avg_current_a,   dtype=np.float64), compression="gzip")
        g.create_dataset("err_current",  data=np.asarray(r.err_current_a,   dtype=np.float64), compression="gzip")
        g.create_dataset("raw_source_v", data=np.asarray(r.source_v,        dtype=np.float64), compression="gzip")
        g.create_dataset("raw_current_a",data=np.asarray(r.current_a,       dtype=np.float64), compression="gzip")
        g.attrs["v_start"]       = float(cfg["iv_v_start"])
        g.attrs["v_stop"]        = float(cfg["iv_v_stop"])
        g.attrs["v_step"]        = float(cfg["iv_v_step"])
        g.attrs["pts_per_v"]     = int(cfg["iv_pts_per_v"])
        g.attrs["current_range_auto"]    = bool(cfg["iv_current_range_auto"])
        g.attrs["current_range_lower_a"] = float(cfg["iv_current_range_lower_a"])
        g.attrs["current_range_upper_a"] = float(cfg["iv_current_range_upper_a"])
        g.attrs["current_aperture_s"]    = float(cfg["iv_current_aperture"])
        g.attrs["n_overrange_points"]    = int(n_overrange)
        if v_bd is not None:
            g.attrs["v_bd_estimate"] = float(v_bd)
        g.attrs["timestamp"]     = time.time()

    # Leave the B2987 with bias off after the sweep
    with step(results, "bias off post-IV"):
        e.bias_off()

    return v_bd


def test_k6485_baseline(cfg: dict, h5: h5py.File, instr: dict,
                         v_bd: float | None, results: StepResult) -> None:
    """K6485 averaged photocurrent (LED off vs on) at two biases:

      1. `bias_for_k6485` — well below V_BD; SPAD has no gain, LED current is
         indistinguishable from background. Control point.
      2. `V_BD + bias_for_k6485_above` — above breakdown; SPAD gain ON, the
         LED-injected light should now produce a measurable photocurrent.

    Each pair (off, on) is captured as a sub-group, so plots can show the
    contrast clearly.
    """
    e  = instr.get("b2987")
    k  = instr.get("k6485")
    dg = instr.get("dg1022")
    if not all((e, k, dg)):
        results.add("k6485 baseline", False, "missing instrument(s)"); return

    grp = h5.create_group("k6485")

    def _read_pair(bias_v: float, label: str):
        """Set bias, take (LED off, LED on) average reads, save to /k6485/<label>/."""
        sub = grp.create_group(label)
        sub.attrs["bias_v"] = float(bias_v)

        with step(results, f"set bias = {bias_v:.2f} V for K6485 [{label}]"):
            e.set_bias(bias_v, settle_s=0.5)

        with step(results, f"K6485 dark @ {bias_v:.2f} V [{label}]"):
            dg.output_off(1)
            time.sleep(0.5)
            arr, ts = k.read_n(cfg["k6485_n_samples"], cfg["k6485_delay_s"])
            ng = sub.create_group("dark")
            ng.create_dataset("current_a",   data=np.asarray(arr, dtype=np.float64), compression="gzip")
            ng.create_dataset("timestamp_s", data=np.asarray(ts,  dtype=np.float64), compression="gzip")
            ng.attrs["mean_a"] = float(np.mean(arr))
            ng.attrs["std_a"]  = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
            ng.attrs["n"]      = int(len(arr))
            log.info("  [%s] dark  mean I = %.3e A  ± %.2e (n=%d)", label,
                     np.mean(arr),
                     np.std(arr, ddof=1) if len(arr) > 1 else 0.0, len(arr))

        with step(results, f"K6485 light @ {bias_v:.2f} V [{label}]"):
            dg.apply_pulse(frequency=cfg["led_frequency_hz"],
                           amplitude=cfg["led_amplitude_v"],
                           offset   =cfg["led_offset_v"],
                           channel  =1)
            dg.configure_pulse(period_s=1.0 / cfg["led_frequency_hz"],
                               width_s =cfg["led_pulse_width"],
                               channel =1)
            dg.set_load(cfg["led_load"], channel=1)
            dg.output_on(1)
            time.sleep(0.5)
            arr, ts = k.read_n(cfg["k6485_n_samples"], cfg["k6485_delay_s"])
            ng = sub.create_group("light")
            ng.create_dataset("current_a",   data=np.asarray(arr, dtype=np.float64), compression="gzip")
            ng.create_dataset("timestamp_s", data=np.asarray(ts,  dtype=np.float64), compression="gzip")
            ng.attrs["mean_a"] = float(np.mean(arr))
            ng.attrs["std_a"]  = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
            ng.attrs["n"]      = int(len(arr))
            ng.attrs["led_freq"] = float(cfg["led_frequency_hz"])
            ng.attrs["led_pulse_width_s"] = float(cfg["led_pulse_width"])
            ng.attrs["led_amp_v"]         = float(cfg["led_amplitude_v"])
            dark_mean  = float(sub["dark"].attrs["mean_a"])
            light_mean = float(np.mean(arr))
            log.info("  [%s] light mean I = %.3e A  (Δ vs dark = %.3e A)",
                     label, light_mean, light_mean - dark_mean)
            sub.attrs["light_minus_dark_a"] = light_mean - dark_mean

    # Probe 1: below V_BD (control — SPAD gain off)
    _read_pair(cfg["bias_for_k6485"], "below_vbd")

    # Probe 2: just above V_BD (gain on — LED on/off should be visible)
    if v_bd is not None:
        v_above = v_bd + cfg["bias_for_k6485_above"]
        _read_pair(v_above, "above_vbd")
    else:
        log.warning("V_BD unknown; skipping the above-V_BD K6485 probe")
        results.add("K6485 above V_BD", False, "V_BD unknown")

    with step(results, "bias off post-K6485"):
        e.bias_off()


def test_vx2740_pulses(cfg: dict, h5: h5py.File, instr: dict,
                        v_bd: float | None, results: StepResult) -> None:
    """Acquire N waveforms from VX2740 ch 0 with LED on, self-trigger."""
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("dg1022")
    if not all((e, v, dg)):
        results.add("vx2740 acquisition", False, "missing instrument(s)"); return

    if v_bd is None:
        log.warning("V_BD unknown; using 45 V as fallback")
        v_bd = 45.0
    bias = v_bd + cfg["vx2740_over_voltage"]

    with step(results, f"set bias = V_BD + {cfg['vx2740_over_voltage']} = {bias:.2f} V"):
        e.set_bias(bias, settle_s=0.5)

    with step(results, "configure record window"):
        v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                                   post_us=cfg["vx2740_post_us"])

    with step(results, "ensure LED on"):
        dg.output_on(1)
        time.sleep(0.2)

    g = h5.create_group("vx2740")
    g.attrs["bias_v"]       = float(bias)
    g.attrs["v_bd_used"]    = float(v_bd)
    g.attrs["over_voltage"] = float(cfg["vx2740_over_voltage"])
    g.attrs["pre_us"]       = float(cfg["vx2740_pre_us"])
    g.attrs["post_us"]      = float(cfg["vx2740_post_us"])

    # --- Sanity probe: software-trigger N waveforms ---
    # Captures whatever is on ch 0 with no level discrimination, so we can
    # see if the signal is actually reaching the digitizer + understand
    # baseline/polarity before we trust self-trigger.
    with step(results, f"SW-trigger probe ({cfg['vx2740_swtrig_probe_n']} waveforms)"):
        v.configure_channels(sipm_channels=[0],
                              threshold_mode="global",
                              global_threshold=0,
                              include_pmt=False)
        v.configure_trigger(mode="software")
        v.arm()
        try:
            # Fire triggers in a background thread while acquire reads
            import threading
            n_probe = int(cfg["vx2740_swtrig_probe_n"])
            stop_evt = threading.Event()
            def _fire():
                time.sleep(0.05)
                for _ in range(n_probe):
                    if stop_evt.is_set(): break
                    try: v.send_software_trigger()
                    except Exception: pass
                    time.sleep(0.005)
            th = threading.Thread(target=_fire, daemon=True); th.start()
            probe = v.acquire(n_waveforms=n_probe, batch_size=n_probe,
                               store_waveforms=True, timeout_s=10.0)
        finally:
            stop_evt.set()
            v.disarm()
        wf = probe.waveforms.get(0)
        if wf is not None and len(wf):
            wf_arr = np.asarray(wf, dtype=np.float64)
            base = wf_arr[:, :wf_arr.shape[1] // 4].mean(axis=1, keepdims=True)
            wf_bl = wf_arr - base
            log.info("  probe ch0 raw min/max  = %d / %d",
                     int(wf_arr.min()), int(wf_arr.max()))
            log.info("  probe ch0 base mean    = %.1f", float(base.mean()))
            log.info("  probe ch0 (after BL): pos max = %+.1f, neg min = %+.1f, "
                     "abs max = %.1f",
                     float(wf_bl.max()), float(wf_bl.min()), float(np.abs(wf_bl).max()))
            sg = g.create_group("swtrig_probe")
            sg.create_dataset("waveforms",
                              data=wf_arr.astype(np.uint16),
                              compression="gzip")
            sg.attrs["n"]            = int(len(wf))
            sg.attrs["baseline_mean"]= float(base.mean())
            sg.attrs["max_pos"]      = float(wf_bl.max())
            sg.attrs["min_neg"]      = float(wf_bl.min())
        else:
            log.warning("  SW-trigger probe captured no waveforms — VX2740 not "
                        "delivering events at all")

    # --- Self-trigger acquisition ---
    with step(results, f"self-trigger acquire {cfg['vx2740_n_waveforms']} waveforms "
                       f"(thresh={cfg['vx2740_self_thresh']} ADC, RISE)"):
        v.configure_channels(
            sipm_channels    = [0],
            thresholds       = {0: int(cfg["vx2740_self_thresh"])},
            threshold_mode   = "per_channel",
            include_pmt      = False,
        )
        v.configure_trigger(mode="self")
        result = v.run(n_waveforms=int(cfg["vx2740_n_waveforms"]),
                        batch_size=1000,
                        store_waveforms=True,
                        timeout_s=30.0)
        log.info("  collected %d waveforms", result.n_waveforms)
        for ch in result.channel_ids:
            amps = result.amplitudes.get(ch, np.array([]))
            log.info("    ch%d: %d pulses, mean amp = %.1f ADC counts",
                     ch, len(amps), float(np.mean(amps)) if len(amps) else 0.0)

        g.attrs["self_thresh_adc"] = int(cfg["vx2740_self_thresh"])
        g.attrs["n_waveforms"]  = int(result.n_waveforms)
        g.attrs["timestamp"]    = time.time()
        for ch in result.channel_ids:
            cg = g.create_group(f"ch{ch}")
            if result.waveforms.get(ch) is not None:
                cg.create_dataset("waveforms",
                                  data=np.asarray(result.waveforms[ch]),
                                  compression="gzip")
            cg.create_dataset("amplitudes_adc",
                              data=np.asarray(result.amplitudes[ch], dtype=np.float32),
                              compression="gzip")
            cg.create_dataset("timestamps_s",
                              data=np.asarray(result.timestamps[ch], dtype=np.float64),
                              compression="gzip")

    with step(results, "bias off post-VX2740"):
        e.bias_off()


def test_vx2740_overvoltage_scan(cfg: dict, h5: h5py.File, instr: dict,
                                  v_bd: float | None, results: StepResult) -> None:
    """For each over-voltage in cfg['ov_scan_volts'], acquire N waveforms,
    record per-bias amplitude spectrum + summary stats. Lets the downstream
    plot show mean amplitude vs over-voltage (gain curve)."""
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("dg1022")
    if not all((e, v, dg)):
        results.add("ov scan", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("ov scan", False, "V_BD unknown"); return

    grp = h5.create_group("vx2740_ov_scan")
    grp.attrs["v_bd_used"]   = float(v_bd)
    grp.attrs["pre_us"]      = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]     = float(cfg["vx2740_post_us"])
    grp.attrs["self_thresh_adc"] = int(cfg["vx2740_self_thresh"])
    grp.attrs["n_waveforms"] = int(cfg["ov_scan_n_waveforms"])
    grp.attrs["ov_steps"]    = np.asarray(cfg["ov_scan_volts"], dtype=np.float32)
    grp.attrs["timestamp"]   = time.time()

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_channels(
        sipm_channels    = [0],
        thresholds       = {0: int(cfg["vx2740_self_thresh"])},
        threshold_mode   = "per_channel",
        include_pmt      = False,
    )
    v.configure_trigger(mode="self")
    dg.output_on(1)
    time.sleep(0.2)

    for ov in cfg["ov_scan_volts"]:
        bias = float(v_bd) + float(ov)
        with step(results, f"OV scan: V_BD + {ov:.1f} V = {bias:.2f} V "
                           f"({cfg['ov_scan_n_waveforms']} wfs)"):
            e.set_bias(bias, settle_s=0.3)
            try:
                r = v.run(n_waveforms=int(cfg["ov_scan_n_waveforms"]),
                           batch_size=1000,
                           store_waveforms=False,    # just amplitudes for the scan
                           timeout_s=15.0)
            except TimeoutError as te:
                log.warning("  timed out at OV=%.1f: %s", ov, te)
                # Save zero-pulse marker
                pg = grp.create_group(f"ov_{ov:+.1f}V".replace("+", "p").replace("-", "m"))
                pg.attrs["bias_v"]      = bias
                pg.attrs["over_voltage"]= float(ov)
                pg.attrs["n_pulses"]    = 0
                pg.attrs["mean_amp"]    = float("nan")
                pg.attrs["timed_out"]   = True
                continue

            amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
            ts   = np.asarray(r.timestamps.get(0, np.array([])), dtype=np.float64)
            mean = float(np.mean(amps)) if len(amps) else float("nan")
            std  = float(np.std(amps, ddof=1)) if len(amps) > 1 else float("nan")
            log.info("  OV=%.1f V  bias=%.2f V  → %d pulses, mean=%.1f ADC ± %.1f",
                     ov, bias, len(amps), mean, std)
            pg = grp.create_group(f"ov_{ov:+.1f}V".replace("+", "p").replace("-", "m"))
            pg.create_dataset("amplitudes_adc", data=amps, compression="gzip")
            pg.create_dataset("timestamps_s",   data=ts,   compression="gzip")
            pg.attrs["bias_v"]      = bias
            pg.attrs["over_voltage"]= float(ov)
            pg.attrs["n_pulses"]    = int(len(amps))
            pg.attrs["mean_amp"]    = mean
            pg.attrs["std_amp"]     = std
            pg.attrs["n_waveforms"] = int(r.n_waveforms)

    with step(results, "bias off post-OV-scan"):
        e.bias_off()


def test_disconnect_all(instr: dict, results: StepResult) -> None:
    for name, c in instr.items():
        try:
            if name == "dg1022":
                c.output_off(1); c.output_off(2)
            c.disconnect()
            results.add(f"{name} disconnect", True)
        except Exception as exc:
            results.add(f"{name} disconnect", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(_ROOT / "data"),
                    help="directory for the HDF5 output (default: ./data)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-12s %(levelname)-7s %(message)s")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    h5_path = out_dir / f"bench_{ts}.h5"
    log.info("writing %s", h5_path)

    cfg = dict(DEFAULT_CFG)
    results = StepResult()

    with h5py.File(h5_path, "w") as h5:
        meta = h5.create_group("meta")
        for k, v in cfg.items():
            try:
                meta.attrs[k] = v
            except TypeError:
                meta.attrs[k] = str(v)
        meta.attrs["created_iso"] = ts
        meta.attrs["bench_setup"] = (
            "Single SiPM, RT, dark. B2987 bias via bias-T; K6485 on low-side; "
            "Cremat CSP+shaper feeding VX2740 ch0 through 10 dB attenuator; "
            "DG1022 ch1 drives the LED. No MUX/temp/stage."
        )

        instr = test_connect_all(cfg, results)

        v_bd = test_dark_iv(cfg, h5, instr, results)
        test_k6485_baseline(cfg, h5, instr, v_bd, results)
        test_vx2740_pulses(cfg, h5, instr, v_bd, results)
        test_vx2740_overvoltage_scan(cfg, h5, instr, v_bd, results)

        test_disconnect_all(instr, results)

        meta.attrs["v_bd_estimate"] = float(v_bd) if v_bd is not None else float("nan")

    print(results.summary())
    print(f"\nHDF5 output: {h5_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
