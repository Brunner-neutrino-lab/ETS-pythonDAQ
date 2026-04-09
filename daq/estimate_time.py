"""
daq/estimate_time.py

Standalone time estimation script for the nEXO SiPM tile characterisation run.

Usage
-----
    python -m daq.estimate_time                      # uses default config
    python -m daq.estimate_time run_config.yaml      # uses YAML config
    python -m daq.estimate_time run_config.yaml --verbose

Prints a breakdown by phase and a total estimate.
"""

from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Per-operation timing constants  (seconds, can be overridden)
# ---------------------------------------------------------------------------

# IV sweep
T_IV_MEASURE_PER_POINT   = 0.15   # s per single current measurement (electrometer)
T_IV_SETTLE              = 0.1    # s settle delay between voltage points (default)
T_IV_OVERHEAD            = 2.0    # s fixed overhead per SiPM (configure, source on/off)

# Pulse acquisition
T_PULSE_TRIGGER_RATE_HZ  = 2200.0  # Hz — PMT-triggered at ~2.2 kHz
T_PULSE_OVERHEAD         = 1.0     # s fixed overhead per SiPM

# Stage motion
T_STAGE_MOVE_PER_MM      = 0.5    # s/mm (conservative; depends on velocity setting)
T_STAGE_SIPM_PITCH_MM    = 10.0   # mm between adjacent SiPMs (default)
T_STAGE_OVERHEAD         = 0.5    # s re-energize + settle per move
T_LAMP_MOVE              = 3.0    # s to move lamp between dark/lamp positions

# MUX
T_MUX_SWITCH             = 0.05   # s relay settle

# Flux check
T_FLUX_CHECK             = 8.0    # s (move to photodiode + read + return)

# Temperature stabilisation
T_TEMP_RAMP_PER_KELVIN   = 120.0  # s/K (conservative ramp rate, cryostat dependent)
T_TEMP_STABLE_S          = 60.0   # s stability hold (overridden by config)
T_TEMP_OVERHEAD          = 300.0  # s fixed overhead per temperature point


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

class TimeEstimate:
    def __init__(self):
        self.rows: list[tuple[str, float]] = []

    def add(self, label: str, seconds: float):
        self.rows.append((label, seconds))

    @property
    def total_s(self) -> float:
        return sum(v for _, v in self.rows)

    def print_table(self, verbose: bool = False):
        col = 52
        print()
        print("=" * (col + 14))
        print(f"  {'Phase':<{col}}  {'hh:mm':>8}  {'hours':>6}")
        print("=" * (col + 14))
        for label, s in self.rows:
            if s < 1 and not verbose:
                continue
            h, rem = divmod(int(s), 3600)
            m      = rem // 60
            print(f"  {label:<{col}}  {h:3d}h{m:02d}m  {s/3600:6.2f}")
        print("-" * (col + 14))
        tot = self.total_s
        h, rem = divmod(int(tot), 3600)
        m = rem // 60
        print(f"  {'TOTAL':<{col}}  {h:3d}h{m:02d}m  {tot/3600:6.2f}")
        print("=" * (col + 14))
        print()


def estimate(config, verbose: bool = False) -> TimeEstimate:
    """
    Compute a time estimate for the full experiment given config.

    Parameters
    ----------
    config  : ExperimentConfig
    verbose : Print all sub-second line items too.

    Returns
    -------
    TimeEstimate
    """
    est = TimeEstimate()

    n_sipms       = len(config.sipm_list())
    temperatures  = config.temperatures_K
    n_temps       = len(temperatures)
    do_illum_set  = set(config.illuminated_temperatures_K)
    flux_interval = config.flux_check_interval

    voltages      = list(config.iv_voltages())
    n_voltages    = len(voltages)
    n_per_pt      = config.iv_n_per_point
    delay_s       = getattr(config, "iv_delay_s", T_IV_SETTLE)
    n_waveforms   = config.pulse_n_waveforms

    # ------------------------------------------------------------------
    # Per-SiPM IV time
    # ------------------------------------------------------------------
    t_iv_per_sipm = (
        T_IV_OVERHEAD
        + n_voltages * (n_per_pt * T_IV_MEASURE_PER_POINT + delay_s)
    )

    # ------------------------------------------------------------------
    # Per-SiPM pulse time
    # ------------------------------------------------------------------
    t_pulse_per_sipm = (
        T_PULSE_OVERHEAD
        + n_waveforms / T_PULSE_TRIGGER_RATE_HZ
    )

    # ------------------------------------------------------------------
    # Per-SiPM stage move
    # ------------------------------------------------------------------
    t_move_per_sipm = (
        T_STAGE_OVERHEAD
        + T_STAGE_SIPM_PITCH_MM * T_STAGE_MOVE_PER_MM
    )

    # ------------------------------------------------------------------
    # Flux checks per temperature
    # ------------------------------------------------------------------
    n_flux_per_temp  = n_sipms // flux_interval
    t_flux_per_temp  = n_flux_per_temp * T_FLUX_CHECK

    # ------------------------------------------------------------------
    # Loop over temperatures
    # ------------------------------------------------------------------
    for T_K in temperatures:
        do_illum = T_K in do_illum_set
        tag      = f"{T_K:.0f}K"

        # Temperature stabilisation
        prev_T = temperatures[temperatures.index(T_K) - 1] if temperatures.index(T_K) > 0 else T_K
        ramp_s = abs(T_K - prev_T) * T_TEMP_RAMP_PER_KELVIN if temperatures.index(T_K) > 0 else 0.0
        stable_s = getattr(config, "temp_stable_s", T_TEMP_STABLE_S)
        est.add(f"  [{tag}] temperature ramp + stabilisation",
                T_TEMP_OVERHEAD + ramp_s + stable_s)

        # Dark IV
        t = n_sipms * (t_iv_per_sipm + t_move_per_sipm + T_MUX_SWITCH)
        est.add(f"  [{tag}] dark IV  ({n_sipms} SiPMs × {n_voltages} V × {n_per_pt} pts)", t)

        # Dark pulse
        t = n_sipms * (t_pulse_per_sipm + t_move_per_sipm + T_MUX_SWITCH)
        est.add(f"  [{tag}] dark pulse  ({n_sipms} SiPMs × {n_waveforms} wfms)", t)

        # Flux checks (shared between dark and illuminated)
        est.add(f"  [{tag}] flux checks  ({n_flux_per_temp} × {T_FLUX_CHECK:.0f} s)", t_flux_per_temp)

        if do_illum:
            # Lamp moves for illuminated IV
            t_lamp = 2 * T_LAMP_MOVE * n_sipms   # dark→lamp + lamp→dark per SiPM
            # Illuminated IV
            t = n_sipms * (t_iv_per_sipm + t_move_per_sipm + T_MUX_SWITCH) + t_lamp
            est.add(f"  [{tag}] illuminated IV", t)

            # Illuminated pulse
            t = n_sipms * (t_pulse_per_sipm + t_move_per_sipm + T_MUX_SWITCH) + t_lamp
            est.add(f"  [{tag}] illuminated pulse", t)

            est.add(f"  [{tag}] flux checks (illum)  ({n_flux_per_temp} checks)", t_flux_per_temp)

    return est


# ---------------------------------------------------------------------------
# Summary line
# ---------------------------------------------------------------------------

def print_assumptions(config):
    voltages = list(config.iv_voltages())
    print("\nAssumptions / config values used:")
    print(f"  SiPMs                : {len(config.sipm_list())}")
    print(f"  Temperatures         : {config.temperatures_K}")
    print(f"  Illuminated temps    : {config.illuminated_temperatures_K}")
    print(f"  IV voltages          : {len(voltages)}  ({voltages[0]:.1f}–{voltages[-1]:.1f} V, step {config.iv_voltage_step:.2f} V)")
    print(f"  IV pts/voltage       : {config.iv_n_per_point}")
    print(f"  IV settle delay      : {getattr(config, 'iv_delay_s', T_IV_SETTLE):.2f} s")
    print(f"  Pulse n_waveforms    : {config.pulse_n_waveforms}")
    print(f"  Pulse trigger rate   : {T_PULSE_TRIGGER_RATE_HZ:.0f} Hz  (PMT)")
    print(f"  Flux check interval  : every {config.flux_check_interval} SiPMs")
    print(f"  Stage move / SiPM    : {T_STAGE_SIPM_PITCH_MM:.0f} mm")
    print(f"  Temp stable hold     : {getattr(config, 'temp_stable_s', T_TEMP_STABLE_S):.0f} s")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Estimate total run time for the ETS SiPM characterisation experiment."
    )
    parser.add_argument("config", nargs="?", help="Path to run_config.yaml (optional)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show all line items including sub-second entries")
    parser.add_argument("--n-sipms", type=int, default=None,
                        help="Override number of SiPMs (if no channel map loaded)")
    args = parser.parse_args()

    from daq.config import ExperimentConfig, write_example_channel_map
    import tempfile

    if args.config:
        cfg = ExperimentConfig.from_yaml(args.config)
    else:
        # Default config with example channel map
        cfg = ExperimentConfig()
        if not os.path.exists(cfg.channel_map_file):
            tmp = tempfile.mktemp(suffix=".csv")
            write_example_channel_map(tmp, n_sipms=args.n_sipms or 96)
            cfg.load_channel_map(tmp)
        else:
            cfg.load_channel_map()

    if args.n_sipms and not args.config:
        import tempfile as _tmp
        p = _tmp.mktemp(suffix=".csv")
        write_example_channel_map(p, n_sipms=args.n_sipms)
        cfg.load_channel_map(p)

    print_assumptions(cfg)
    est = estimate(cfg, verbose=args.verbose)
    est.print_table(verbose=args.verbose)


if __name__ == "__main__":
    main()
