"""
scripts/bench_test.py

End-to-end bench-setup smoke test for a single SiPM in the dark at RT,
no MUX, no temperature control, no stage.

Setup assumed (per user description):
  - Keysight B2987A: HV bias source on the cathode through a bias-T
  - Keithley 6485: low-side photocurrent monitor
  - Keysight 33510B ch 1: LED pulse driver (fiber-coupled to SiPM)
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
import json
import logging
import argparse
import datetime
from pathlib import Path

import numpy as np
import h5py

# Make sibling submodules importable
_ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("keysight2987b-python", "keithley6485-python",
             "vx2740-python", "keysight33500b-python"):
    p = _ROOT / _pkg
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
sys.path.insert(0, str(_ROOT))

from b2987b           import B2987BController
from keithley6485     import K6485Driver
from vx2740.controller import VX2740Controller
from ks33500b         import KS33500BController
from daq              import h5io


# ---------------------------------------------------------------------------
# Configuration — defaults for this bench. Override on the command line.
# ---------------------------------------------------------------------------

DEFAULT_CFG = dict(
    # Addresses — must match what the user has on the lab subnet.
    # Use SOCKET (raw TCP on port 5025) instead of VXI-11 (INSTR) for the
    # B2987 — VXI-11 leaks session slots on every abnormal exit and the
    # listener locks up after a few crashes, requiring a power-cycle.
    # SOCKET is stateless on the instrument side.
    b2987_visa      = "TCPIP::172.16.0.11::5025::SOCKET",
    k6485_port      = "/dev/ttyUSB0",
    k6485_baud      = 9600,
    k6485_read_term = "\r",
    k6485_wr_term   = "\r",
    vx2740_address  = "172.16.0.51",
    wfg_visa        = "TCPIP0::172.16.0.46::5025::SOCKET",

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

    # Coarse-then-fine IV: do a coarse pass (large step) across the full
    # range to find the approximate breakdown, then a fine pass (small step)
    # in a window around it.  Cuts run-time roughly in half versus the
    # uniform sweep and gives the same resolution at the knee.
    iv_use_coarse_fine   = True,
    iv_coarse_step       = 2.0,    # V
    iv_fine_window_half  = 2.0,    # V — ±2 V around the coarse V_BD estimate
    iv_fine_step         = 0.1,    # V
    iv_measure_photodiode= False,  # skip pd read at every point (≈2× faster)

    # K6485 baseline read at TWO biases: well below V_BD (no SPAD gain — diagnostic)
    # and just above V_BD (gain on; LED on/off should now be visible).
    bias_for_k6485       = 30.0,    # below V_BD — control
    bias_for_k6485_above = 1.0,     # add to V_BD; e.g. V_BD + 1 V
    k6485_n_samples = 20,
    k6485_delay_s   = 0.05,

    # WFG (Keysight 33510B) LED pulse train — defaults per bench operator (2026-05-26):
    #   1 kHz, 0.04% duty cycle (= 400 ns pulse), amplitude ~3 V into a high-Z
    #   line driving the LED. The shaper saturates around 1-2 V at the CAEN
    #   input (which is after a 10 dB attenuator), so back at the WFG that
    #   means signals beyond ~3-6 V can clip the chain.
    led_frequency_hz = 1_000.0,
    led_amplitude_v  = 3.0,
    led_offset_v     = 1.5,
    led_pulse_width  = 4e-7,    # 400 ns pulse (0.04% duty @ 1 kHz)
    led_load         = "INF",

    # LED amplitude sweep at low over-voltage — finds where the chain
    # saturates so the user knows the safe operating window.
    led_amp_sweep_v       = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
    led_amp_sweep_ov      = 1.0,   # V above V_BD — safe gain, far below saturation
    led_amp_sweep_n_wfs   = 500,

    # VX2740 self-trigger threshold scan at fixed OV.  Plateaus in
    # rate vs threshold (log-log) mark the SPE / 2pe / 3pe peaks.  At
    # OV=+3 the gain is largest; we walk the threshold up across the
    # SPE peak (≈250-1000 ADC at +3 V from the OV scan results).
    thresh_scan_ov        = 3.0,
    thresh_scan_adc       = [20, 40, 80, 150, 250, 400, 600, 900, 1400, 2000, 3000],
    thresh_scan_n_wfs     = 1000,
    thresh_scan_timeout_s = 8.0,    # per-threshold budget; high thr → DCR-limited

    # Dark threshold scan: LED off, otherwise identical.  Quantifies the
    # dark count rate vs threshold (intrinsic DCR + correlated noise).
    dark_thresh_scan_n_wfs     = 200,
    dark_thresh_scan_timeout_s = 12.0,

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

    # Clean OV scan — LED off so the chain doesn't saturate.  Stores full
    # waveforms so we can extract pulse shape (rise time, decay τ) and a
    # clean SPE peak from the amplitude histogram at each OV.
    ov_scan_clean_volts       = [1.0, 2.0, 3.0, 4.0, 5.0],
    ov_scan_clean_n_wfs       = 300,
    ov_scan_clean_thresh_adc  = 40,         # well above noise, below SPE
    ov_scan_clean_timeout_s   = 30.0,       # per OV — DCR-limited at low OV

    # DCR vs OV — LED off, fixed threshold above noise floor, rate per OV.
    dcr_vs_ov_volts           = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
    dcr_vs_ov_thresh_adc      = 200,        # ≥1 PE-ish (varies with OV but acceptable)
    dcr_vs_ov_n_wfs           = 500,
    dcr_vs_ov_timeout_s       = 8.0,

    # Crosstalk / afterpulse — LED off, big record window, find secondary
    # pulses in the tail.  Threshold = SPE-ish so each capture is a primary
    # pulse + (possibly) AP/CT pulses afterwards.
    ctap_over_voltage         = 3.0,
    ctap_thresh_adc           = 300,        # ≈ SPE @ OV+3
    ctap_pre_us               = 2.0,
    ctap_post_us              = 50.0,       # 5× the normal window
    ctap_n_wfs                = 300,
    ctap_timeout_s            = 30.0,
    ctap_peak_height_frac     = 0.5,        # secondary peak ≥ 0.5× primary
    ctap_peak_min_dt_us       = 0.5,        # ignore peaks <0.5 µs after primary

    # LED pulse-width sweep — at fixed amp & frequency, vary the WFG pulse
    # width to characterise the LED + shaper time response.
    led_width_sweep_widths_s  = [1e-7, 2e-7, 4e-7, 8e-7, 1.6e-6, 3.2e-6],
    led_width_sweep_amp_v     = 3.0,
    led_width_sweep_ov        = 1.0,        # safe — below saturation
    led_width_sweep_n_wfs     = 500,

    # VX2740 noise floor — bias OFF, LED OFF.  Sweep the self-trigger
    # threshold across the digitizer's own noise band so we can see the
    # rate the chain produces with NO signal.  This sets the floor that
    # every real DCR / SPE measurement has to live above.
    nf_thr_adc                = [5, 8, 12, 18, 25, 35, 50, 75, 100, 150, 250],
    nf_n_wfs                  = 500,
    nf_timeout_s              = 4.0,

    # K6485 noise floor — at no bias / no LED, read in different ranges
    # to characterise the picoammeter's intrinsic noise.
    k6485_nf_ranges           = [("AUTO", "AUTO"),
                                  (2e-9,   "2 nA"),
                                  (2e-8,   "20 nA"),
                                  (2e-7,   "200 nA")],
    k6485_nf_n_samples        = 50,
    k6485_nf_delay_s          = 0.05,
)


# ---------------------------------------------------------------------------
# V_BD cache — persist the last good breakdown estimate so --skip-iv works
# ---------------------------------------------------------------------------

_VBD_CACHE = Path(__file__).resolve().parents[1] / "data" / "last_vbd.json"


def _save_vbd(vbd: float, source_h5: str | None = None) -> None:
    """Persist V_BD to last_vbd.json so subsequent --skip-iv runs can read it."""
    _VBD_CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "v_bd_v":      float(vbd),
        "timestamp":   time.time(),
        "iso":         datetime.datetime.now().isoformat(timespec="seconds"),
        "source_h5":   source_h5,
    }
    _VBD_CACHE.write_text(json.dumps(payload, indent=2))


def _load_vbd() -> float | None:
    """Return cached V_BD or None if unavailable / malformed."""
    if not _VBD_CACHE.is_file():
        return None
    try:
        d = json.loads(_VBD_CACHE.read_text())
        v = float(d.get("v_bd_v"))
        return v if v > 0 else None
    except Exception:
        return None


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

    with step(results, "WFG connect"):
        d = KS33500BController(visa=cfg["wfg_visa"], mode="hardware")
        d.connect()
        log.info("  WFG IDN: %s", d.identify())
        instr["wfg"] = d

    return instr


def test_dark_iv(cfg: dict, h5: h5py.File, instr: dict, results: StepResult) -> float | None:
    """Dark IV sweep — B2987 ramps bias, K6485 reads SiPM current at each V.

    NOTE on architecture: the B2987's *built-in* ammeter is on its own input
    terminal which (in this bench setup) is connected to the XUV photodiode,
    NOT to the SiPM. So we cannot rely on `B2987Controller.sweep()` to give
    us the SiPM IV — that returns the photodiode current vs bias voltage,
    which is essentially constant. Instead, we step the B2987 source manually
    and average a few K6485 reads at each voltage. The B2987's own current
    reading is captured in parallel as a diagnostic but is not the IV curve.
    """
    e  = instr.get("b2987")
    k  = instr.get("k6485")
    dg = instr.get("wfg")
    if e is None or k is None:
        results.add("dark IV", False, "B2987 or K6485 not connected"); return None

    v_bd = None
    with step(results, "ensure LED off for dark IV"):
        if dg is not None:
            dg.output_off(1)
            dg.output_off(2)

    def _sweep_pass(voltages: np.ndarray, pass_label: str
                    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list, list]:
        """One pass over the given voltages.  Returns (mean_I, std_I, pd_I,
        raw_I_per_sample, raw_V_per_sample).  Photodiode reads are skipped
        unless cfg['iv_measure_photodiode'] is true."""
        measure_pd = bool(cfg.get("iv_measure_photodiode", False))
        n = len(voltages)
        mean_i = np.zeros(n, dtype=np.float64)
        std_i  = np.zeros(n, dtype=np.float64)
        pd_i   = np.full(n, np.nan, dtype=np.float64)
        raw_i: list = []
        raw_v: list = []
        log.info("  [%s] %d points  %.2f → %.2f V", pass_label,
                 n, voltages[0], voltages[-1])
        t_start = time.time()
        for ix, v in enumerate(voltages):
            e.set_bias(float(v), settle_s=cfg["iv_delay_s"])
            arr, _ts = k.read_n(int(cfg["iv_pts_per_v"]),
                                 float(cfg["iv_delay_s"]))
            mean_i[ix] = float(np.mean(arr))
            std_i[ix]  = (float(np.std(arr, ddof=1))
                          if len(arr) > 1 else 0.0)
            raw_i.extend(arr.tolist())
            raw_v.extend([float(v)] * len(arr))
            if measure_pd:
                try:
                    pd_i[ix] = float(e.measure_current())
                except Exception as exc:
                    log.debug("  pd reading at %.2f V failed: %s", v, exc)
            if n >= 10 and ix % max(1, n // 5) == 0:
                log.info("    [%s] %5.2f V  I_sipm = %+.3e A", pass_label,
                         v, mean_i[ix])
        log.info("  [%s] done in %.1f s", pass_label, time.time() - t_start)
        return mean_i, std_i, pd_i, raw_i, raw_v

    def _estimate_vbd(voltages: np.ndarray, mean_i: np.ndarray) -> float | None:
        """Return V_BD ≈ argmax of d(log|I|)/dV over points above 30 V, or
        None if the curve never gets large enough."""
        absi = np.abs(mean_i)
        if absi.max() < 1e-10:
            return None
        log_i = np.log10(np.clip(absi, 1e-15, None))
        dlogi = np.diff(log_i)
        v_mid = 0.5 * (voltages[:-1] + voltages[1:])
        mask = v_mid > 30.0
        if not mask.any():
            return None
        idx = int(np.argmax(dlogi[mask]))
        return float(v_mid[mask][idx])

    with step(results, "dark IV sweep"):
        if bool(cfg.get("iv_use_coarse_fine", True)):
            # Coarse pass across the full range
            coarse_v = np.arange(cfg["iv_v_start"],
                                  cfg["iv_v_stop"] + cfg["iv_coarse_step"] * 0.5,
                                  cfg["iv_coarse_step"])
            c_mean, c_std, c_pd, c_ri, c_rv = _sweep_pass(coarse_v, "coarse")
            coarse_vbd = _estimate_vbd(coarse_v, c_mean)

            # Fine pass in a window around the coarse estimate
            if coarse_vbd is not None:
                half = float(cfg["iv_fine_window_half"])
                fine_lo = max(cfg["iv_v_start"], coarse_vbd - half)
                fine_hi = min(cfg["iv_v_stop"],  coarse_vbd + half)
                fine_v  = np.arange(fine_lo,
                                     fine_hi + cfg["iv_fine_step"] * 0.5,
                                     cfg["iv_fine_step"])
                log.info("  coarse V_BD ≈ %.2f V → fine sweep %.2f → %.2f V",
                         coarse_vbd, fine_lo, fine_hi)

                # The coarse pass ends well above V_BD where the SiPM draws
                # μA of avalanche current.  Jumping straight down to fine_lo
                # (below V_BD) produces a discharge transient that the K6485
                # picks up as a spurious large reading at the first fine
                # point, fooling the V_BD estimator into a low value.  Ramp
                # to fine_lo with a long settle, then discard one K6485
                # sample to flush any residual transient, before measuring.
                e.set_bias(float(fine_lo), settle_s=2.0)
                try:
                    k.read_n(1, 0.1)
                except Exception:
                    pass

                f_mean, f_std, f_pd, f_ri, f_rv = _sweep_pass(fine_v, "fine")
            else:
                log.warning("  coarse pass did not find a V_BD; skipping fine pass")
                fine_v = np.array([], dtype=np.float64)
                f_mean = np.array([], dtype=np.float64)
                f_std  = np.array([], dtype=np.float64)
                f_pd   = np.array([], dtype=np.float64)
                f_ri, f_rv = [], []

            # Merge passes, sorted by voltage (so plotting/derivatives behave)
            voltages = np.concatenate([coarse_v, fine_v])
            sipm_i_mean = np.concatenate([c_mean, f_mean])
            sipm_i_std  = np.concatenate([c_std,  f_std])
            pd_i        = np.concatenate([c_pd,   f_pd])
            order = np.argsort(voltages, kind="stable")
            voltages    = voltages[order]
            sipm_i_mean = sipm_i_mean[order]
            sipm_i_std  = sipm_i_std[order]
            pd_i        = pd_i[order]
            raw_k_i = c_ri + f_ri
            raw_k_v = c_rv + f_rv
        else:
            voltages = np.arange(cfg["iv_v_start"],
                                  cfg["iv_v_stop"] + cfg["iv_v_step"] * 0.5,
                                  cfg["iv_v_step"])
            sipm_i_mean, sipm_i_std, pd_i, raw_k_i, raw_k_v = \
                _sweep_pass(voltages, "uniform")

        v_bd = _estimate_vbd(voltages, sipm_i_mean)
        if v_bd is not None:
            log.info("  estimated V_BD ≈ %.2f V (max d log|I_SiPM|/dV)", v_bd)
        else:
            log.warning("  no V_BD estimate (current never exceeded 1 nA or "
                        "no points above 30 V)")

        g = h5.create_group("iv")
        iv_attrs = {
            "v_start":           float(cfg["iv_v_start"]),
            "v_stop":            float(cfg["iv_v_stop"]),
            "v_step":            float(cfg["iv_v_step"]),
            "pts_per_v":         int(cfg["iv_pts_per_v"]),
            "iv_delay_s":        float(cfg["iv_delay_s"]),
            "use_coarse_fine":   bool(cfg.get("iv_use_coarse_fine", True)),
            "iv_coarse_step":    float(cfg.get("iv_coarse_step", 0.0)),
            "iv_fine_step":      float(cfg.get("iv_fine_step", 0.0)),
            "iv_fine_window_half": float(cfg.get("iv_fine_window_half", 0.0)),
            "iv_measure_photodiode": bool(cfg.get("iv_measure_photodiode", False)),
            "current_source":    "k6485",
            "photodiode_source": "b2987_ammeter" if cfg.get("iv_measure_photodiode", False) else "skipped",
        }
        if v_bd is not None:
            iv_attrs["v_bd_estimate"] = float(v_bd)
        h5io.write_iv(g,
                      source_v             = voltages,
                      current_a            = sipm_i_mean,
                      err_current          = sipm_i_std,
                      photodiode_current_a = pd_i,
                      raw_source_v         = raw_k_v,
                      raw_current_a        = raw_k_i,
                      attrs                = iv_attrs)

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
    dg = instr.get("wfg")
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
            h5io.write_current_samples(ng, current_a=arr, timestamp_s=ts)
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
            h5io.write_current_samples(ng, current_a=arr, timestamp_s=ts, attrs={
                "led_freq":          float(cfg["led_frequency_hz"]),
                "led_pulse_width_s": float(cfg["led_pulse_width"]),
                "led_amp_v":         float(cfg["led_amplitude_v"]),
            })
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
    dg = instr.get("wfg")
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
            h5io.write_pulse(sg,
                             waveforms=wf_arr.astype(np.uint16),
                             attrs={
                                 "n":             int(len(wf)),
                                 "baseline_mean": float(base.mean()),
                                 "max_pos":       float(wf_bl.max()),
                                 "min_neg":       float(wf_bl.min()),
                             })
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
            wf = result.waveforms.get(ch)
            h5io.write_pulse(g,
                             channel        = ch,
                             amplitudes_adc = result.amplitudes[ch],
                             timestamps_s   = result.timestamps[ch],
                             waveforms      = wf if wf is not None else None)

    with step(results, "bias off post-VX2740"):
        e.bias_off()


def test_vx2740_overvoltage_scan(cfg: dict, h5: h5py.File, instr: dict,
                                  v_bd: float | None, results: StepResult) -> None:
    """For each over-voltage in cfg['ov_scan_volts'], acquire N waveforms,
    record per-bias amplitude spectrum + summary stats. Lets the downstream
    plot show mean amplitude vs over-voltage (gain curve)."""
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
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
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             timestamps_s   = ts,
                             attrs={
                                 "bias_v":      bias,
                                 "over_voltage":float(ov),
                                 "n_pulses":    int(len(amps)),
                                 "mean_amp":    mean,
                                 "std_amp":     std,
                                 "n_waveforms": int(r.n_waveforms),
                             })

    with step(results, "bias off post-OV-scan"):
        e.bias_off()


def test_led_amp_sweep(cfg: dict, h5: h5py.File, instr: dict,
                        v_bd: float | None, results: StepResult) -> None:
    """At low over-voltage, sweep the WFG LED amplitude and record VX2740
    pulse statistics at each step.  Finds where the shaper saturates so we
    know the safe operating window for amplitude calibrations later.

    Bias is held at V_BD + led_amp_sweep_ov (default +1 V — gain on, far below
    the OV regime where the chain would clip).
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("led amp sweep", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("led amp sweep", False, "V_BD unknown"); return

    bias = float(v_bd) + float(cfg["led_amp_sweep_ov"])

    with step(results, f"set bias = V_BD + {cfg['led_amp_sweep_ov']} = {bias:.2f} V "
                       f"for LED amp sweep"):
        e.set_bias(bias, settle_s=0.5)

    grp = h5.create_group("vx2740_led_amp_sweep")
    grp.attrs["v_bd_used"]       = float(v_bd)
    grp.attrs["bias_v"]          = float(bias)
    grp.attrs["over_voltage"]    = float(cfg["led_amp_sweep_ov"])
    grp.attrs["pre_us"]          = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]         = float(cfg["vx2740_post_us"])
    grp.attrs["self_thresh_adc"] = int(cfg["vx2740_self_thresh"])
    grp.attrs["n_waveforms"]     = int(cfg["led_amp_sweep_n_wfs"])
    grp.attrs["led_freq_hz"]     = float(cfg["led_frequency_hz"])
    grp.attrs["led_pulse_width_s"] = float(cfg["led_pulse_width"])
    grp.attrs["led_offset_v"]    = float(cfg["led_offset_v"])
    grp.attrs["amp_steps"]       = np.asarray(cfg["led_amp_sweep_v"], dtype=np.float32)
    grp.attrs["timestamp"]       = time.time()

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_channels(
        sipm_channels    = [0],
        thresholds       = {0: int(cfg["vx2740_self_thresh"])},
        threshold_mode   = "per_channel",
        include_pmt      = False,
    )
    v.configure_trigger(mode="self")

    # Set up the LED once with the first amplitude; per-iteration we only
    # change amplitude (cheap) rather than re-applying the full pulse spec.
    dg.apply_pulse(frequency=cfg["led_frequency_hz"],
                   amplitude=float(cfg["led_amp_sweep_v"][0]),
                   offset   =cfg["led_offset_v"],
                   channel  =1)
    dg.configure_pulse(period_s=1.0 / cfg["led_frequency_hz"],
                       width_s =cfg["led_pulse_width"],
                       channel =1)
    dg.set_load(cfg["led_load"], channel=1)
    dg.output_on(1)
    time.sleep(0.2)

    for amp_v in cfg["led_amp_sweep_v"]:
        amp_v = float(amp_v)
        with step(results, f"LED amp sweep: {amp_v:.2f} Vpp "
                           f"({cfg['led_amp_sweep_n_wfs']} wfs)"):
            dg.set_amplitude(amp_v, channel=1)
            time.sleep(0.15)
            try:
                r = v.run(n_waveforms=int(cfg["led_amp_sweep_n_wfs"]),
                           batch_size=500,
                           store_waveforms=False,
                           timeout_s=15.0)
            except TimeoutError as te:
                log.warning("  timed out at amp=%.2f Vpp: %s", amp_v, te)
                pg = grp.create_group(f"amp_{amp_v:.2f}V".replace(".", "p"))
                pg.attrs["led_amp_v"]   = amp_v
                pg.attrs["n_pulses"]    = 0
                pg.attrs["mean_amp"]    = float("nan")
                pg.attrs["std_amp"]     = float("nan")
                pg.attrs["timed_out"]   = True
                continue

            amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
            ts   = np.asarray(r.timestamps.get(0, np.array([])), dtype=np.float64)
            mean = float(np.mean(amps)) if len(amps) else float("nan")
            std  = float(np.std(amps, ddof=1)) if len(amps) > 1 else float("nan")
            log.info("  LED %.2f Vpp → %d pulses, mean=%.1f ADC ± %.1f",
                     amp_v, len(amps), mean, std)
            pg = grp.create_group(f"amp_{amp_v:.2f}V".replace(".", "p"))
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             timestamps_s   = ts,
                             attrs={
                                 "led_amp_v":   amp_v,
                                 "n_pulses":    int(len(amps)),
                                 "mean_amp":    mean,
                                 "std_amp":     std,
                                 "n_waveforms": int(r.n_waveforms),
                             })

    with step(results, "bias off post-LED-amp-sweep"):
        dg.output_off(1)
        e.bias_off()


def _threshold_scan(cfg: dict, h5: h5py.File, instr: dict,
                     v_bd: float, results: StepResult, *,
                     led_on: bool, h5_group: str, n_wfs: int,
                     timeout_s: float, skip_set_bias: bool = False) -> None:
    """Internal: sweep VX2740 self-trigger threshold and record rate at each
    point.  Saves rate / mean-amplitude / amplitudes histogram per threshold.

    `skip_set_bias` lets the caller chain dark+light scans at the same OV
    without ramping the bias back to 0 between them.
    """
    e, v, dg = instr["b2987"], instr["vx2740"], instr["wfg"]
    bias = float(v_bd) + float(cfg["thresh_scan_ov"])

    label = "light" if led_on else "dark"
    if not skip_set_bias:
        with step(results, f"set bias = V_BD + {cfg['thresh_scan_ov']} = {bias:.2f} V "
                           f"for thresh scan [{label}]"):
            e.set_bias(bias, settle_s=0.3)
    else:
        log.info("  (bias already set to %.2f V — skipping ramp)", bias)

    if led_on:
        dg.set_amplitude(float(cfg["led_amplitude_v"]), channel=1)
        dg.output_on(1)
    else:
        dg.output_off(1)
    time.sleep(0.15)

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_trigger(mode="self")

    grp = h5.create_group(h5_group)
    grp.attrs["bias_v"]        = bias
    grp.attrs["over_voltage"]  = float(cfg["thresh_scan_ov"])
    grp.attrs["v_bd_used"]     = float(v_bd)
    grp.attrs["led_on"]        = bool(led_on)
    grp.attrs["pre_us"]        = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]       = float(cfg["vx2740_post_us"])
    grp.attrs["thresholds_adc"]= np.asarray(cfg["thresh_scan_adc"], dtype=np.int32)
    grp.attrs["n_wfs"]         = int(n_wfs)
    grp.attrs["timeout_s"]     = float(timeout_s)
    if led_on:
        grp.attrs["led_amp_v"]         = float(cfg["led_amplitude_v"])
        grp.attrs["led_freq_hz"]       = float(cfg["led_frequency_hz"])
        grp.attrs["led_pulse_width_s"] = float(cfg["led_pulse_width"])
    grp.attrs["timestamp"]     = time.time()

    for thr in cfg["thresh_scan_adc"]:
        thr = int(thr)
        v.configure_channels(
            sipm_channels  = [0],
            thresholds     = {0: thr},
            threshold_mode = "per_channel",
            include_pmt    = False,
        )
        with step(results, f"thresh [{label}]: {thr} ADC (max {n_wfs} wfs, "
                           f"timeout {timeout_s:.1f}s)"):
            t0 = time.time()
            try:
                r = v.run(n_waveforms=int(n_wfs),
                           batch_size=min(500, n_wfs),
                           store_waveforms=False,
                           timeout_s=float(timeout_s))
                dt   = time.time() - t0
                amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
                n_pulses = int(len(amps))
                rate_hz  = (n_pulses / dt) if dt > 0 else float("nan")
                timed_out= False
            except TimeoutError:
                dt        = time.time() - t0
                amps      = np.array([], dtype=np.float32)
                n_pulses  = 0
                rate_hz   = 0.0
                timed_out = True
                log.warning("    thr=%d: timed out after %.1f s", thr, dt)

            mean = float(np.mean(amps)) if n_pulses else float("nan")
            log.info("  thr=%5d → %5d pulses in %.2fs = %8.1f Hz  (mean amp %s)",
                     thr, n_pulses, dt, rate_hz,
                     f"{mean:.1f}" if n_pulses else "—")

            pg = grp.create_group(f"thr_{thr:05d}")
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             attrs={
                                 "threshold_adc": thr,
                                 "n_pulses":      n_pulses,
                                 "wall_time_s":   float(dt),
                                 "rate_hz":       float(rate_hz),
                                 "mean_amp":      mean,
                                 "timed_out":     timed_out,
                             })


def test_threshold_scan_light(cfg: dict, h5: h5py.File, instr: dict,
                               v_bd: float | None, results: StepResult,
                               *, keep_bias_on: bool = False) -> None:
    """LED-on threshold scan at fixed OV — locates the SPE peak via the
    rate-vs-threshold plateau structure.

    `keep_bias_on` skips the trailing bias_off so a dark scan can run
    immediately after at the same bias.
    """
    if not all((instr.get("b2987"), instr.get("vx2740"), instr.get("wfg"))):
        results.add("thresh scan light", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("thresh scan light", False, "V_BD unknown"); return
    _threshold_scan(cfg, h5, instr, v_bd, results,
                     led_on=True,
                     h5_group="vx2740_thresh_scan_light",
                     n_wfs=int(cfg["thresh_scan_n_wfs"]),
                     timeout_s=float(cfg["thresh_scan_timeout_s"]))
    if keep_bias_on:
        # Turn off the LED but leave bias on for the next scan
        instr["wfg"].output_off(1)
    else:
        with step(results, "bias off post-thresh-scan-light"):
            instr["wfg"].output_off(1)
            instr["b2987"].bias_off()


def test_threshold_scan_dark(cfg: dict, h5: h5py.File, instr: dict,
                              v_bd: float | None, results: StepResult,
                              *, bias_already_set: bool = False) -> None:
    """LED-off threshold scan at the same OV — gives the dark count rate
    vs threshold curve.  Low-rate points will time out (rate=0 reported).

    `bias_already_set` skips the initial set_bias when chaining after a
    light scan that left bias on.
    """
    if not all((instr.get("b2987"), instr.get("vx2740"), instr.get("wfg"))):
        results.add("thresh scan dark", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("thresh scan dark", False, "V_BD unknown"); return
    _threshold_scan(cfg, h5, instr, v_bd, results,
                     led_on=False,
                     h5_group="vx2740_thresh_scan_dark",
                     n_wfs=int(cfg["dark_thresh_scan_n_wfs"]),
                     timeout_s=float(cfg["dark_thresh_scan_timeout_s"]),
                     skip_set_bias=bias_already_set)
    with step(results, "bias off post-thresh-scan-dark"):
        instr["b2987"].bias_off()


def test_ov_scan_clean(cfg: dict, h5: h5py.File, instr: dict,
                        v_bd: float | None, results: StepResult) -> None:
    """LED-OFF over-voltage scan with full waveforms stored at each OV.

    Without LED, the only triggered events are dark counts.  The amplitude
    distribution at each OV then shows a clean SPE peak (and 2pe, 3pe at
    high OV), uncontaminated by the saturated LED-driven response.  Use
    this for gain extraction: SPE peak position vs OV is linear and the
    slope (ADC/V) gives the SPAD gain.
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("ov scan clean", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("ov scan clean", False, "V_BD unknown"); return

    dg.output_off(1); time.sleep(0.1)

    grp = h5.create_group("vx2740_ov_scan_clean")
    grp.attrs["v_bd_used"]    = float(v_bd)
    grp.attrs["led_on"]       = False
    grp.attrs["pre_us"]       = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]      = float(cfg["vx2740_post_us"])
    grp.attrs["thresh_adc"]   = int(cfg["ov_scan_clean_thresh_adc"])
    grp.attrs["n_wfs_target"] = int(cfg["ov_scan_clean_n_wfs"])
    grp.attrs["ov_steps"]     = np.asarray(cfg["ov_scan_clean_volts"], dtype=np.float32)
    grp.attrs["timestamp"]    = time.time()

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_channels(
        sipm_channels  = [0],
        thresholds     = {0: int(cfg["ov_scan_clean_thresh_adc"])},
        threshold_mode = "per_channel",
        include_pmt    = False,
    )
    v.configure_trigger(mode="self")

    for ov in cfg["ov_scan_clean_volts"]:
        bias = float(v_bd) + float(ov)
        with step(results, f"OV-clean: V_BD + {ov:.1f} V = {bias:.2f} V "
                           f"({cfg['ov_scan_clean_n_wfs']} wfs, LED off)"):
            e.set_bias(bias, settle_s=0.3)
            t0 = time.time()
            try:
                r = v.run(n_waveforms=int(cfg["ov_scan_clean_n_wfs"]),
                           batch_size=min(200, int(cfg["ov_scan_clean_n_wfs"])),
                           store_waveforms=True,
                           timeout_s=float(cfg["ov_scan_clean_timeout_s"]))
                timed_out = False
            except TimeoutError as te:
                log.warning("  OV=%.1f V timed out: %s", ov, te)
                pg = grp.create_group(f"ov_{ov:+.1f}V".replace("+", "p").replace("-", "m"))
                pg.attrs["bias_v"]      = bias
                pg.attrs["over_voltage"]= float(ov)
                pg.attrs["n_pulses"]    = 0
                pg.attrs["mean_amp"]    = float("nan")
                pg.attrs["timed_out"]   = True
                continue
            dt = time.time() - t0

            amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
            ts   = np.asarray(r.timestamps.get(0, np.array([])), dtype=np.float64)
            wfs  = r.waveforms.get(0)
            mean = float(np.mean(amps)) if len(amps) else float("nan")
            std  = float(np.std(amps, ddof=1)) if len(amps) > 1 else float("nan")
            rate = (len(amps) / dt) if dt > 0 else float("nan")
            log.info("  OV=%.1f V  bias=%.2f V  →  %d pulses in %.1fs  "
                     "rate=%.1f Hz  mean=%.1f ADC ± %.1f",
                     ov, bias, len(amps), dt, rate, mean, std)

            pg = grp.create_group(f"ov_{ov:+.1f}V".replace("+", "p").replace("-", "m"))
            wf_arr = (np.asarray(wfs).astype(np.uint16)
                      if wfs is not None and len(wfs) else None)
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             timestamps_s   = ts,
                             waveforms      = wf_arr,
                             attrs={
                                 "bias_v":       bias,
                                 "over_voltage": float(ov),
                                 "n_pulses":     int(len(amps)),
                                 "wall_time_s":  float(dt),
                                 "rate_hz":      float(rate),
                                 "mean_amp":     mean,
                                 "std_amp":      std,
                                 "timed_out":    timed_out,
                             })

    with step(results, "bias off post-OV-clean"):
        e.bias_off()


def test_dcr_vs_ov(cfg: dict, h5: h5py.File, instr: dict,
                    v_bd: float | None, results: StepResult) -> None:
    """LED-OFF rate vs over-voltage at a single fixed threshold.

    Output: rate (Hz) per OV → standard DCR vs OV curve.  At very low OV the
    rate is dominated by noise hitting threshold; at high OV it's true
    DCR + correlated noise (cross-talk).  Slope on log-Y gives the DCR
    activation energy / breakdown probability slope.
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("dcr vs ov", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("dcr vs ov", False, "V_BD unknown"); return

    dg.output_off(1); time.sleep(0.1)
    thr = int(cfg["dcr_vs_ov_thresh_adc"])

    grp = h5.create_group("vx2740_dcr_vs_ov")
    grp.attrs["v_bd_used"]   = float(v_bd)
    grp.attrs["led_on"]      = False
    grp.attrs["thresh_adc"]  = thr
    grp.attrs["pre_us"]      = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]     = float(cfg["vx2740_post_us"])
    grp.attrs["n_wfs_target"]= int(cfg["dcr_vs_ov_n_wfs"])
    grp.attrs["ov_steps"]    = np.asarray(cfg["dcr_vs_ov_volts"], dtype=np.float32)
    grp.attrs["timestamp"]   = time.time()

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_channels(
        sipm_channels  = [0],
        thresholds     = {0: thr},
        threshold_mode = "per_channel",
        include_pmt    = False,
    )
    v.configure_trigger(mode="self")

    for ov in cfg["dcr_vs_ov_volts"]:
        bias = float(v_bd) + float(ov)
        with step(results, f"DCR vs OV: V_BD + {ov:.1f} V = {bias:.2f} V "
                           f"@ thr={thr}"):
            e.set_bias(bias, settle_s=0.3)
            t0 = time.time()
            try:
                r = v.run(n_waveforms=int(cfg["dcr_vs_ov_n_wfs"]),
                           batch_size=min(200, int(cfg["dcr_vs_ov_n_wfs"])),
                           store_waveforms=False,
                           timeout_s=float(cfg["dcr_vs_ov_timeout_s"]))
                dt   = time.time() - t0
                amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
                n    = int(len(amps))
                rate = n / dt if dt > 0 else float("nan")
                timed_out = False
            except TimeoutError:
                dt   = time.time() - t0
                amps = np.array([], dtype=np.float32)
                n    = 0
                rate = 0.0
                timed_out = True

            log.info("  OV=%.2f V  →  %5d pulses / %.2fs = %8.1f Hz",
                     ov, n, dt, rate)
            pg = grp.create_group(f"ov_{ov:+.2f}V".replace("+", "p").replace("-", "m"))
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             attrs={
                                 "bias_v":       bias,
                                 "over_voltage": float(ov),
                                 "n_pulses":     n,
                                 "wall_time_s":  float(dt),
                                 "rate_hz":      float(rate),
                                 "mean_amp":     float(np.mean(amps)) if n else float("nan"),
                                 "timed_out":    timed_out,
                             })

    with step(results, "bias off post-DCR-vs-OV"):
        e.bias_off()


def test_crosstalk_afterpulse(cfg: dict, h5: h5py.File, instr: dict,
                               v_bd: float | None, results: StepResult) -> None:
    """LED-OFF capture with a long record window, then offline find_peaks
    on every stored waveform to extract:
      - number of pulses per trigger window (cross-talk / 2pe / 3pe rate)
      - time-of-secondary relative to primary (afterpulse time distribution)

    Saves raw waveforms + per-waveform peak lists so analysis can be re-run.
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("crosstalk/AP", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("crosstalk/AP", False, "V_BD unknown"); return

    from scipy.signal import find_peaks

    bias = float(v_bd) + float(cfg["ctap_over_voltage"])
    pre_us  = float(cfg["ctap_pre_us"])
    post_us = float(cfg["ctap_post_us"])
    thr     = int(cfg["ctap_thresh_adc"])
    n_wfs   = int(cfg["ctap_n_wfs"])

    dg.output_off(1); time.sleep(0.1)
    with step(results, f"set bias = V_BD + {cfg['ctap_over_voltage']} = {bias:.2f} V "
                       f"for crosstalk/AP"):
        e.set_bias(bias, settle_s=0.3)

    v.configure_record_window(pre_us=pre_us, post_us=post_us)
    v.configure_channels(
        sipm_channels  = [0],
        thresholds     = {0: thr},
        threshold_mode = "per_channel",
        include_pmt    = False,
    )
    v.configure_trigger(mode="self")

    grp = h5.create_group("vx2740_crosstalk_ap")
    grp.attrs["bias_v"]       = bias
    grp.attrs["over_voltage"] = float(cfg["ctap_over_voltage"])
    grp.attrs["v_bd_used"]    = float(v_bd)
    grp.attrs["led_on"]       = False
    grp.attrs["pre_us"]       = pre_us
    grp.attrs["post_us"]      = post_us
    grp.attrs["thresh_adc"]   = thr
    grp.attrs["n_wfs_target"] = n_wfs
    grp.attrs["height_frac"]  = float(cfg["ctap_peak_height_frac"])
    grp.attrs["min_dt_us"]    = float(cfg["ctap_peak_min_dt_us"])
    grp.attrs["timestamp"]    = time.time()

    with step(results, f"acquire {n_wfs} long-window dark waveforms "
                       f"(post={post_us:.0f} µs)"):
        t0 = time.time()
        try:
            r = v.run(n_waveforms=n_wfs,
                       batch_size=min(50, n_wfs),
                       store_waveforms=True,
                       timeout_s=float(cfg["ctap_timeout_s"]))
            dt = time.time() - t0
        except TimeoutError as te:
            log.warning("  crosstalk/AP timed out: %s", te)
            grp.attrs["timed_out"] = True
            with step(results, "bias off post-crosstalk/AP"):
                e.bias_off()
            return
        wfs = r.waveforms.get(0)
        if wfs is None or len(wfs) == 0:
            log.warning("  no waveforms captured")
            grp.attrs["n_waveforms"] = 0
            with step(results, "bias off post-crosstalk/AP"):
                e.bias_off()
            return

        # 125 MS/s → 8 ns / sample
        SAMPLE_NS = 1000.0 / 125.0   # =8.0
        pre_samples = int(round(pre_us * 1000.0 / SAMPLE_NS))
        min_dist_samples = max(1, int(round(cfg["ctap_peak_min_dt_us"]
                                             * 1000.0 / SAMPLE_NS)))

        wfs_arr = np.asarray(wfs, dtype=np.float32)
        baselines = wfs_arr[:, :pre_samples].mean(axis=1, keepdims=True)
        wfs_bl    = wfs_arr - baselines

        grp.create_dataset("waveforms",
                            data=wfs_arr.astype(np.uint16),
                            compression="gzip")
        grp.attrs["n_waveforms"] = int(len(wfs))
        grp.attrs["wall_time_s"] = float(dt)
        grp.attrs["rate_hz"]     = float(len(wfs) / dt) if dt > 0 else float("nan")

        # Per-waveform offline find_peaks: count pulses, record amplitudes & dt
        height_frac = float(cfg["ctap_peak_height_frac"])
        n_peaks_per_wf: list[int] = []
        all_amps: list[float]     = []     # all primary amplitudes
        secondary_dts_us: list[float] = []
        secondary_amps: list[float]   = []
        primary_amps: list[float]     = []

        for i in range(len(wfs_arr)):
            w_post = wfs_bl[i, pre_samples:]
            if w_post.size == 0: continue
            # First find the primary peak (largest in post window above thr)
            max_val = float(w_post.max())
            if max_val < thr:
                n_peaks_per_wf.append(0)
                continue
            # find_peaks with height = primary fraction
            peaks, props = find_peaks(w_post,
                                       height=max_val * height_frac,
                                       distance=min_dist_samples)
            if len(peaks) == 0:
                n_peaks_per_wf.append(0)
                continue
            n_peaks_per_wf.append(int(len(peaks)))
            heights = props["peak_heights"]
            # Define primary as the first peak (in time), secondaries follow
            primary_idx  = int(peaks[0])
            primary_amp  = float(heights[0])
            primary_amps.append(primary_amp)
            all_amps.extend([float(h) for h in heights])
            for j in range(1, len(peaks)):
                dt_samples = int(peaks[j] - primary_idx)
                dt_us      = dt_samples * SAMPLE_NS / 1000.0
                secondary_dts_us.append(dt_us)
                secondary_amps.append(float(heights[j]))

        n_peaks_per_wf = np.asarray(n_peaks_per_wf, dtype=np.int32)
        grp.create_dataset("n_peaks_per_wf", data=n_peaks_per_wf, compression="gzip")
        grp.create_dataset("all_peak_amps_adc",
                            data=np.asarray(all_amps, dtype=np.float32),
                            compression="gzip")
        grp.create_dataset("primary_amps_adc",
                            data=np.asarray(primary_amps, dtype=np.float32),
                            compression="gzip")
        grp.create_dataset("secondary_dt_us",
                            data=np.asarray(secondary_dts_us, dtype=np.float32),
                            compression="gzip")
        grp.create_dataset("secondary_amps_adc",
                            data=np.asarray(secondary_amps, dtype=np.float32),
                            compression="gzip")
        n_single = int(np.sum(n_peaks_per_wf == 1))
        n_multi  = int(np.sum(n_peaks_per_wf >  1))
        n_zero   = int(np.sum(n_peaks_per_wf == 0))
        ct_frac  = (n_multi / (n_single + n_multi)) if (n_single + n_multi) > 0 else 0.0
        grp.attrs["n_zero_peak_wfs"]   = n_zero
        grp.attrs["n_single_peak_wfs"] = n_single
        grp.attrs["n_multi_peak_wfs"]  = n_multi
        grp.attrs["crosstalk_fraction"]= float(ct_frac)
        log.info("  %d wfs: %d single, %d multi-peak, %d zero  →  CT ≈ %.1f%%",
                 len(wfs), n_single, n_multi, n_zero, 100.0 * ct_frac)
        if len(secondary_dts_us):
            log.info("  %d secondary peaks; median dt = %.2f µs",
                     len(secondary_dts_us), float(np.median(secondary_dts_us)))

    with step(results, "bias off post-crosstalk/AP"):
        e.bias_off()


def test_led_width_sweep(cfg: dict, h5: h5py.File, instr: dict,
                          v_bd: float | None, results: StepResult) -> None:
    """At fixed bias and LED amplitude, sweep the WFG pulse width.

    Mean VX2740 pulse amplitude vs LED pulse width characterises the LED
    response time and the shaper integration window.  Narrow pulses below
    the LED rise-time produce small signals; once wide enough the signal
    plateaus.
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("LED width sweep", False, "missing instrument(s)"); return
    if v_bd is None:
        results.add("LED width sweep", False, "V_BD unknown"); return

    bias = float(v_bd) + float(cfg["led_width_sweep_ov"])
    with step(results, f"set bias = V_BD + {cfg['led_width_sweep_ov']} = "
                       f"{bias:.2f} V for LED width sweep"):
        e.set_bias(bias, settle_s=0.4)

    grp = h5.create_group("vx2740_led_width_sweep")
    grp.attrs["v_bd_used"]    = float(v_bd)
    grp.attrs["bias_v"]       = bias
    grp.attrs["over_voltage"] = float(cfg["led_width_sweep_ov"])
    grp.attrs["led_amp_v"]    = float(cfg["led_width_sweep_amp_v"])
    grp.attrs["led_freq_hz"]  = float(cfg["led_frequency_hz"])
    grp.attrs["pre_us"]       = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]      = float(cfg["vx2740_post_us"])
    grp.attrs["self_thresh_adc"] = int(cfg["vx2740_self_thresh"])
    grp.attrs["n_wfs"]        = int(cfg["led_width_sweep_n_wfs"])
    grp.attrs["widths_s"]     = np.asarray(cfg["led_width_sweep_widths_s"], dtype=np.float32)
    grp.attrs["timestamp"]    = time.time()

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_channels(
        sipm_channels  = [0],
        thresholds     = {0: int(cfg["vx2740_self_thresh"])},
        threshold_mode = "per_channel",
        include_pmt    = False,
    )
    v.configure_trigger(mode="self")

    # Configure WFG once with the LED amplitude; sweep only the width below
    dg.apply_pulse(frequency=cfg["led_frequency_hz"],
                   amplitude=float(cfg["led_width_sweep_amp_v"]),
                   offset   =cfg["led_offset_v"],
                   channel  =1)
    dg.set_load(cfg["led_load"], channel=1)
    dg.output_on(1)
    time.sleep(0.2)

    for w_s in cfg["led_width_sweep_widths_s"]:
        w_s = float(w_s)
        with step(results, f"LED width sweep: {w_s*1e9:.0f} ns "
                           f"({cfg['led_width_sweep_n_wfs']} wfs)"):
            dg.configure_pulse(period_s=1.0 / cfg["led_frequency_hz"],
                                width_s =w_s,
                                channel =1)
            time.sleep(0.15)
            t0 = time.time()
            try:
                r = v.run(n_waveforms=int(cfg["led_width_sweep_n_wfs"]),
                           batch_size=200,
                           store_waveforms=False,
                           timeout_s=10.0)
                timed_out = False
                dt = time.time() - t0
                amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
            except TimeoutError as te:
                log.warning("  width=%.0f ns timed out: %s", w_s*1e9, te)
                timed_out = True
                dt = time.time() - t0
                amps = np.array([], dtype=np.float32)

            mean = float(np.mean(amps)) if len(amps) else float("nan")
            std  = float(np.std(amps, ddof=1)) if len(amps) > 1 else float("nan")
            log.info("  width=%5.0f ns  →  %d pulses, mean=%.1f ADC ± %.1f",
                     w_s*1e9, len(amps), mean, std)
            pg = grp.create_group(f"w_{int(round(w_s*1e9)):07d}ns")
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             attrs={
                                 "width_s":     w_s,
                                 "n_pulses":    int(len(amps)),
                                 "wall_time_s": float(dt),
                                 "mean_amp":    mean,
                                 "std_amp":     std,
                                 "timed_out":   timed_out,
                             })

    with step(results, "bias off post-LED-width-sweep"):
        dg.output_off(1)
        e.bias_off()


def test_vx2740_noise_floor(cfg: dict, h5: h5py.File, instr: dict,
                              v_bd: float | None, results: StepResult) -> None:
    """Digitizer false-trigger rate vs threshold with bias OFF and LED OFF.

    The B2987 output is disabled so no signal reaches the SiPM → shaper →
    digitizer chain.  Any triggers are purely the digitizer's own electronics.
    The rate at low threshold (≲20 ADC) sets the noise floor for every real
    measurement; the elbow of the curve is the lowest useful threshold.
    """
    e  = instr.get("b2987")
    v  = instr.get("vx2740")
    dg = instr.get("wfg")
    if not all((e, v, dg)):
        results.add("vx2740 noise floor", False, "missing instrument(s)"); return

    with step(results, "bias OFF for noise-floor scan"):
        e.bias_off()
        dg.output_off(1)
        time.sleep(0.3)

    v.configure_record_window(pre_us=cfg["vx2740_pre_us"],
                               post_us=cfg["vx2740_post_us"])
    v.configure_trigger(mode="self")

    grp = h5.create_group("vx2740_noise_floor")
    grp.attrs["bias_v"]       = 0.0
    grp.attrs["led_on"]       = False
    grp.attrs["pre_us"]       = float(cfg["vx2740_pre_us"])
    grp.attrs["post_us"]      = float(cfg["vx2740_post_us"])
    grp.attrs["thresholds_adc"] = np.asarray(cfg["nf_thr_adc"], dtype=np.int32)
    grp.attrs["n_wfs"]        = int(cfg["nf_n_wfs"])
    grp.attrs["timeout_s"]    = float(cfg["nf_timeout_s"])
    grp.attrs["timestamp"]    = time.time()

    for thr in cfg["nf_thr_adc"]:
        thr = int(thr)
        v.configure_channels(
            sipm_channels  = [0],
            thresholds     = {0: thr},
            threshold_mode = "per_channel",
            include_pmt    = False,
        )
        with step(results, f"noise floor: {thr} ADC"):
            t0 = time.time()
            try:
                r = v.run(n_waveforms=int(cfg["nf_n_wfs"]),
                           batch_size=min(200, int(cfg["nf_n_wfs"])),
                           store_waveforms=False,
                           timeout_s=float(cfg["nf_timeout_s"]))
                dt   = time.time() - t0
                amps = np.asarray(r.amplitudes.get(0, np.array([])), dtype=np.float32)
                n    = int(len(amps))
                rate = (n / dt) if dt > 0 else float("nan")
                timed_out = False
            except TimeoutError:
                dt   = time.time() - t0
                amps = np.array([], dtype=np.float32)
                n    = 0
                rate = 0.0
                timed_out = True

            log.info("  thr=%4d → %4d pulses in %.2fs = %8.1f Hz (noise)",
                     thr, n, dt, rate)
            pg = grp.create_group(f"thr_{thr:05d}")
            h5io.write_pulse(pg,
                             amplitudes_adc = amps,
                             attrs={
                                 "threshold_adc": thr,
                                 "n_pulses":      n,
                                 "wall_time_s":   float(dt),
                                 "rate_hz":       float(rate),
                                 "mean_amp":      float(np.mean(amps)) if n else float("nan"),
                                 "timed_out":     timed_out,
                             })


def test_k6485_noise_floor(cfg: dict, h5: h5py.File, instr: dict,
                             v_bd: float | None, results: StepResult) -> None:
    """K6485 RMS noise vs configured range at zero bias, LED off.

    AUTO is the default the IV uses; fixed ranges are useful when the
    expected current is known a priori.  Smaller-range modes have lower
    quantization noise but overflow above their full scale.
    """
    e  = instr.get("b2987")
    k  = instr.get("k6485")
    dg = instr.get("wfg")
    if not all((e, k, dg)):
        results.add("k6485 noise floor", False, "missing instrument(s)"); return

    with step(results, "bias OFF + LED OFF for K6485 noise floor"):
        try: e.bias_off()
        except Exception: pass
        dg.output_off(1)
        time.sleep(0.3)

    grp = h5.create_group("k6485_noise_floor")
    grp.attrs["bias_v"]    = 0.0
    grp.attrs["led_on"]    = False
    grp.attrs["n_samples"] = int(cfg["k6485_nf_n_samples"])
    grp.attrs["delay_s"]   = float(cfg["k6485_nf_delay_s"])
    grp.attrs["timestamp"] = time.time()

    for rng_val, rng_label in cfg["k6485_nf_ranges"]:
        with step(results, f"K6485 @ range {rng_label}"):
            k.set_range(rng_val)
            time.sleep(0.2)
            arr, ts = k.read_n(int(cfg["k6485_nf_n_samples"]),
                                float(cfg["k6485_nf_delay_s"]))
            mean = float(np.mean(arr))
            std  = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
            log.info("  range %-6s  μ=%+.3e A  σ=%.3e A  (n=%d)",
                     rng_label, mean, std, len(arr))
            pg = grp.create_group(f"range_{rng_label.replace(' ', '_')}")
            h5io.write_current_samples(pg, current_a=arr, timestamp_s=ts, attrs={
                "range_value": (str(rng_val) if isinstance(rng_val, str) else float(rng_val)),
                "range_label": rng_label,
            })

    # Restore AUTO range so downstream tests see a sensible default
    try: k.set_range("AUTO")
    except Exception: pass


def test_disconnect_all(instr: dict, results: StepResult) -> None:
    for name, c in instr.items():
        try:
            if name == "wfg":
                c.output_off(1); c.output_off(2)
            c.disconnect()
            results.add(f"{name} disconnect", True)
        except Exception as exc:
            results.add(f"{name} disconnect", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test registry — keys are usable in --only and drive the dispatch in main()
# ---------------------------------------------------------------------------

ALL_TESTS: list[str] = [
    "iv",
    "k6485",
    "pulses",
    "ov_scan",
    "led_amp",
    "ov_scan_clean",
    "dcr_vs_ov",
    "crosstalk",
    "led_width",
    "threshold",
    "vx_noise_floor",
    "k6485_noise_floor",
]

# Map test key → plot key(s) in daq.plotting.PLOTS that consume its data
_PLOT_MAP: dict[str, list[str]] = {
    "iv":            ["iv", "iv_leakage"],
    "k6485":         ["k6485_bars"],
    "pulses":        ["waveform", "mean_waveform", "spectrum"],
    "ov_scan":       ["ov_scan", "ov_spectra"],
    "led_amp":       ["led_amp_sweep"],
    "ov_scan_clean": ["ov_scan_clean", "ov_scan_clean_gain", "pulse_area_scatter"],
    "dcr_vs_ov":     ["dcr_vs_ov"],
    "crosstalk":     ["crosstalk_ap"],
    "led_width":     ["led_width_sweep"],
    "threshold":     ["threshold_scan"],
    "vx_noise_floor":   ["vx2740_noise_floor"],
    "k6485_noise_floor":["k6485_noise_floor"],
}


def _auto_plot(h5_path: Path, which_plots: list[str]) -> None:
    """Render every requested plot type from h5_path into plots/ as PNGs."""
    try:
        sys.path.insert(0, str(_ROOT))
        from daq import plotting as plib
    except Exception as e:
        log.warning("auto-plot disabled (could not import daq.plotting): %s", e)
        return

    out = _ROOT / "plots"
    out.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rendered: list[str] = []
    for key in which_plots:
        if key not in plib.PLOTS:
            log.warning("auto-plot: unknown plot key %r — skipping", key); continue
        try:
            ax = plib.PLOTS[key]["fn"](str(h5_path))
            # Some plots (e.g. crosstalk_ap with no ax) make their own figure;
            # find the parent figure off the returned Axes.
            fig = ax.figure
            fig.tight_layout()
            png = out / f"{key}_{ts}.png"
            fig.savefig(png, facecolor=fig.get_facecolor(), dpi=110)
            plib.plt.close(fig)
            rendered.append(str(png))
        except Exception as exc:
            log.warning("auto-plot %r failed: %s: %s", key, type(exc).__name__, exc)
    if rendered:
        log.info("wrote %d plot(s):", len(rendered))
        for p in rendered:
            log.info("  %s", p)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(_ROOT / "data"),
                    help="directory for the HDF5 output (default: ./data)")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of tests to run. "
                         f"Choices: {','.join(ALL_TESTS)}. "
                         f"Default: run all.")
    ap.add_argument("--skip-iv", action="store_true",
                    help="skip the dark IV sweep; read V_BD from "
                         "data/last_vbd.json instead.")
    ap.add_argument("--vbd", type=float, default=None,
                    help="override V_BD (V); skips IV and the cache.")
    ap.add_argument("--no-plot", action="store_true",
                    help="don't auto-write PNGs into plots/ at the end.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-12s %(levelname)-7s %(message)s")

    # ------------------------------------------------------------------
    # Resolve which tests to run
    # ------------------------------------------------------------------
    if args.only.strip():
        requested = [t.strip() for t in args.only.split(",") if t.strip()]
        unknown   = [t for t in requested if t not in ALL_TESTS]
        if unknown:
            log.error("unknown test(s) in --only: %s. Known: %s",
                      unknown, ALL_TESTS)
            return 2
        run_tests = requested
    else:
        run_tests = list(ALL_TESTS)

    # If user wants to skip IV, IV must not appear in run_tests; also they
    # must provide a V_BD source (--vbd or cached).
    skip_iv = bool(args.skip_iv) or (args.vbd is not None)
    if skip_iv and "iv" in run_tests:
        run_tests = [t for t in run_tests if t != "iv"]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    h5_path = out_dir / f"bench_{ts}.h5"
    log.info("writing %s", h5_path)
    log.info("tests to run: %s", ",".join(run_tests))

    cfg = dict(DEFAULT_CFG)
    results = StepResult()

    with h5py.File(h5_path, "w") as h5:
        h5io.write_top_attrs(h5, measurement_type="bench")
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
            "Keysight 33510B ch1 drives the LED. No MUX/temp/stage."
        )
        meta.attrs["run_tests"]    = ",".join(run_tests)
        meta.attrs["skip_iv"]      = bool(skip_iv)

        instr = test_connect_all(cfg, results)

        # ------------------------------------------------------------------
        # V_BD: derive from IV, --vbd override, or last_vbd.json
        # ------------------------------------------------------------------
        v_bd: float | None = None
        v_bd_source = "unknown"
        if "iv" in run_tests:
            v_bd = test_dark_iv(cfg, h5, instr, results)
            v_bd_source = "iv_this_run"
            if v_bd is not None:
                _save_vbd(v_bd, source_h5=str(h5_path))
        elif args.vbd is not None:
            v_bd = float(args.vbd)
            v_bd_source = f"cli_override (--vbd {v_bd:.2f})"
            log.info("using V_BD = %.2f V from --vbd", v_bd)
            results.add(f"V_BD from CLI (--vbd {v_bd:.2f} V)", True)
        else:
            cached = _load_vbd()
            if cached is not None:
                v_bd = cached
                v_bd_source = f"cache ({_VBD_CACHE.name})"
                log.info("using V_BD = %.2f V from %s", v_bd, _VBD_CACHE)
                results.add(f"V_BD from cache ({v_bd:.2f} V)", True)
            else:
                log.warning("no IV and no cached V_BD — downstream tests will "
                            "skip themselves.")
                results.add("V_BD source", False,
                             "no IV, no --vbd, and no cached value")
        meta.attrs["v_bd_source"] = v_bd_source

        # ------------------------------------------------------------------
        # Dispatch — each test gated by --only membership
        # ------------------------------------------------------------------
        def maybe(name: str) -> bool:
            return name in run_tests

        if maybe("k6485"):     test_k6485_baseline(cfg, h5, instr, v_bd, results)
        if maybe("pulses"):    test_vx2740_pulses(cfg, h5, instr, v_bd, results)
        if maybe("ov_scan"):   test_vx2740_overvoltage_scan(cfg, h5, instr, v_bd, results)
        if maybe("led_amp"):   test_led_amp_sweep(cfg, h5, instr, v_bd, results)
        if maybe("ov_scan_clean"): test_ov_scan_clean(cfg, h5, instr, v_bd, results)
        if maybe("dcr_vs_ov"): test_dcr_vs_ov(cfg, h5, instr, v_bd, results)
        if maybe("crosstalk"): test_crosstalk_afterpulse(cfg, h5, instr, v_bd, results)
        if maybe("led_width"): test_led_width_sweep(cfg, h5, instr, v_bd, results)
        if maybe("threshold"):
            # Chain light → dark holding bias on between them
            test_threshold_scan_light(cfg, h5, instr, v_bd, results,
                                       keep_bias_on=True)
            test_threshold_scan_dark(cfg, h5, instr, v_bd, results,
                                      bias_already_set=True)
        if maybe("vx_noise_floor"):    test_vx2740_noise_floor(cfg, h5, instr, v_bd, results)
        if maybe("k6485_noise_floor"): test_k6485_noise_floor(cfg, h5, instr, v_bd, results)

        test_disconnect_all(instr, results)

        meta.attrs["v_bd_estimate"] = float(v_bd) if v_bd is not None else float("nan")

    print(results.summary())
    print(f"\nHDF5 output: {h5_path}")

    # ------------------------------------------------------------------
    # Auto-plot — render PNGs for every test that produced data
    # ------------------------------------------------------------------
    if cfg.get("auto_plot", True) and not args.no_plot:
        which_plots: list[str] = []
        for t in run_tests:
            which_plots.extend(_PLOT_MAP.get(t, []))
        # de-dup while preserving order
        seen = set()
        which_plots = [p for p in which_plots if not (p in seen or seen.add(p))]
        if which_plots:
            _auto_plot(h5_path, which_plots)

    return 0


if __name__ == "__main__":
    sys.exit(main())
