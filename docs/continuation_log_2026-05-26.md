# Continuation log — 2026-05-26 bench testing

A handoff note for the next session (or a context-fresh me) to pick up
the bench-testing thread without re-reading the whole conversation. Keep
this file updated as tasks are knocked off.

## Where things stand

### Hardware on the bench
Single SiPM in the dark at RT (no MUX, no temperature control, no
linear actuator on the light source). All instruments confirmed and on
the lab subnet:

| Role | Instrument | Address |
|---|---|---|
| HV bias to cathode through bias-T | Keysight B2987A | `TCPIP::172.16.0.11::INSTR` |
| Low-side photocurrent monitor | Keithley 6485 | `/dev/ttyUSB0` (PL2303 USB-serial, 9600/CR/CR) |
| Pulse acquisition (after Cremat CSP+shaper, 10 dB attenuator) | CAEN VX2740 ch 0 | `dig2://172.16.0.51` |
| LED driver (ch 1 → fiber) | Rigol DG1022 | `/dev/usbtmc0` (kernel USBTMC) |
| Cremat CSP+shaper bias | R&S NGE103 PSU | `TCPIP::172.16.0.33::INSTR` (manual; not in DAQ stack yet) |
| MUX rail power (user setting reference) | R&S NGE103 PSU | `TCPIP::172.16.0.19::INSTR` (**TODO: add as submodule** — repo: https://github.com/Brunner-neutrino-lab/r-snge100-python) |

### The bench test script: `scripts/bench_test.py`

Runs four end-to-end tests against the live hardware, writes one HDF5
file to `data/bench_<timestamp>.h5`. Decoupled per-step
exception handling so one failure doesn't abort the run; final
PASS/FAIL summary printed.

Tests, in order:

1. **Connect all four instruments** (B2987, K6485, VX2740, DG1022) and
   `configure_sweep` on the B2987 with the rich measurement params.
2. **Dark IV sweep** (B2987, LED off). Auto-range, 20 ms aperture,
   0–55 V step 0.5 V × 3 reps. Estimates V_BD from `argmax(d log|I|/dV)`
   above 30 V, masking out the `9.91e+37` overrange sentinel.
3. **K6485 averaged photocurrent**, dark vs LED-on, at **two biases**:
   - `below_vbd` (30 V, well below V_BD — control, no SPAD gain)
   - `above_vbd` (V_BD + 1 V — SPAD gain on, light should be visible)
4. **VX2740 pulse acquisition** at V_BD + 3 V:
   - 5-event SW-trigger probe first (sanity: are events arriving at all?)
   - Then 1000-event self-trigger acquisition via ITLA, threshold 50 ADC
5. **VX2740 over-voltage scan** at V_BD + [1, 2, 3, 4, 5] V, 500
   self-triggered waveforms per point. Saves per-point amplitudes +
   summary attrs (mean/std/n_pulses) to `/vx2740_ov_scan/ov_<X>V/`.

The script currently passes 21/21 on the simpler version (before
steps 3.above_vbd and 5 were added). The extended version (steps 3
two-bias + 5 over-voltage scan) is implemented and **needs one more
run** — the latest attempt died at step 1 because the B2987's VXI-11
listener hung again (see "Known issues" below).

### The plot library: `daq/plotting.py` + `scripts/plot_bench.py`

Designed up-front for **three callers**: CLI, future NiceGUI plots
tab, ad-hoc notebook. Every plot follows one signature:

```python
plot_X(src, ax=None, label=None, **opts) -> matplotlib.axes.Axes
```

`src` accepts an `h5py.Group | h5py.File | path`. `ax` is supplied by
the caller (CLI uses `plt.subplots()`, future NiceGUI page uses
`ui.matplotlib()`). `apply_dark_style(fig, ax)` matches the xsphere
palette. `find_latest()` powers the "live" button.

Registered plot types (see `PLOTS` dict):

```
iv             dark IV with V_BD marker
k6485_bars     dark vs light bar chart, --bias-group below_vbd|above_vbd
k6485_ts       per-sample current vs time, dark+light pair
waveform       single VX2740 waveform (--channel, --index)
mean_waveform  average over all stored captures (best diagnostic)
spectrum       pulse-amplitude histogram (--bins, --log-y)
ov_scan        mean amplitude vs over-voltage (gain curve)
ov_spectra     amplitude spectra family overlay vs over-voltage
```

CLI examples:
```bash
python scripts/plot_bench.py --list
python scripts/plot_bench.py iv --live
python scripts/plot_bench.py spectrum data/bench_*.h5 --log-y --bins 80
python scripts/plot_bench.py mean_waveform fileA.h5 fileB.h5 --label "SiPM A" --label "SiPM B"
```

### What the existing plots tell us (from `bench_20260526_113743.h5`)

- **IV plot**: ~2.566 µA constant baseline current across 0–55 V — the
  bias filter has a DC return path that swamps SiPM dark current. Real
  dark variation is in the ~3 nA range on top. **V_BD = 54.75 V** but
  only one point above V_BD in the data — sweep needs to extend to
  60–65 V to make the breakdown knee visible.
- **Mean waveform**: clean Cremat-shaper response, ~1 µs rise, 2-3 µs
  fall, ringing tail. Peak ~38 ADC at t ≈ 2.5 µs after trigger. Recording
  chain is healthy.
- **Single waveforms**: noise-dominated; need averaging to see structure.
- **Spectrum**: peak ~75 ADC, tail to 400. With threshold = 50 ADC,
  the histogram likely catches some baseline noise too — 79% trigger
  efficiency suggests cutting it tighter.

## What's blocking right now

**B2987 VXI-11 listener is hung.** Same dance we've done several times.
Symptoms:
- `ping 172.16.0.11` → up
- raw socket on `:5025` → `*IDN?` returns instantly
- VXI-11 `TCPIP::172.16.0.11::INSTR` open succeeds, but the first
  `*IDN?` query times out with `VI_ERROR_TMO`
- Recovery via raw-socket `*CLS` doesn't unstick VXI-11

**Fix is a manual power-cycle of the B2987.** Once it's back, the
script's `try/except` around `device_clear()` (committed earlier)
prevents the connect step from also hanging.

**Longer-term fix (parked)**: the SOCKET-mode refactor for b2987b/
driver.py. The driver should be made to work over
`TCPIP::172.16.0.11::5025::SOCKET` so VXI-11 lockups can be sidestepped
entirely. First attempt at this revealed that `_run_sweep_hardware`'s
`:STAT:OPER:COND?` poll uses a brittle `resp[2] == '7'` check that
false-positives over SOCKET. A proper rewrite uses `*OPC?` for sweep
completion. See conversation `2026-05-25` ~16:00 for the
diagnostic transcript.

## Pick-up plan (the moment the B2987 is back)

Run each item in order. Do not skip steps — each catches a specific
class of failure.

### Step A — verify the B2987 is alive again
```bash
# Stop the webapp first so it doesn't claim the VISA session
pkill -f "daq.webapp"; sleep 2

conda run -n ets-daq --no-capture-output python -c "
import sys; sys.path.insert(0, '/home/ets/ETS-pythonDAQ/keysight2987b-python')
from b2987b import B2987BController
with B2987BController(visa='TCPIP::172.16.0.11::INSTR', mode='hardware') as e:
    print('  IDN:', e.identify())
    print('  I(0V) =', e.measure_current(0.0), 'A')
"
```
Expected: `Keysight B2987B [hardware] @ TCPIP::172.16.0.11::INSTR` and
a current ≲ 1 nA at 0 V.

### Step B — re-run the extended bench test
```bash
conda run -n ets-daq --no-capture-output python /home/ets/ETS-pythonDAQ/scripts/bench_test.py
```
Expected: ~25/25 PASS (was 21/21 before adding the K6485-above-V_BD
pair + over-voltage scan). Writes `data/bench_<timestamp>.h5` with
groups `/iv`, `/k6485/below_vbd/{dark,light}`,
`/k6485/above_vbd/{dark,light}`, `/vx2740/swtrig_probe/`,
`/vx2740/ch0/{waveforms,amplitudes_adc,timestamps_s}`,
`/vx2740_ov_scan/ov_+1.0V/...` through `ov_+5.0V/...`.

If the K6485-above-V_BD step shows similar dark vs light (e.g., both
~0.9 nA), check that the LED was actually firing — DG1022 amplitude
might be below the LED threshold. Watch the scope while the script
runs to confirm.

If the over-voltage scan shows a flat or noisy gain curve, the
self-trigger threshold (currently 50 ADC) may be catching mostly
noise — bump to 80–100 and re-run that one step manually.

### Step C — generate the new plots
```bash
for p in iv k6485_bars k6485_ts ov_scan ov_spectra; do
    python scripts/plot_bench.py "$p" --live
done
# Then per-channel
python scripts/plot_bench.py mean_waveform --live --channel 0
python scripts/plot_bench.py spectrum --live --channel 0 --bins 80
python scripts/plot_bench.py spectrum --live --channel 0 --bins 80 --log-y
# K6485 above V_BD specifically (the interesting one)
python scripts/plot_bench.py k6485_bars --live --bias-group above_vbd
python scripts/plot_bench.py k6485_ts   --live --bias-group above_vbd
```
PNGs land in `plots/`. The key plot to inspect is **`ov_scan`**:
mean amplitude should rise roughly linearly with over-voltage (∝ gain
× over-voltage) if the SPE is being captured correctly.

### Step D — diagnose any failures
- **No pulses captured in the OV scan at low over-voltages**: SPE
  amplitude is below the trigger threshold. Lower
  `cfg["vx2740_self_thresh"]` to 30 or use a different trigger mode.
- **K6485 dark ≈ light at above-V_BD**: LED isn't actually injecting
  light into the SiPM. Check the DG1022 output is on, the fiber is
  coupled, and the amplitude is past the LED's forward-voltage
  threshold.
- **Dark IV shows a constant DC baseline**: known — the bias filter's
  return path. Could subtract per-V baseline before estimating V_BD,
  or extend the sweep to 65 V to make the knee unambiguous.

## Other open work (separate, can be tackled independently)

### NGE100 power supply integration ← in progress now

Repo: `https://github.com/Brunner-neutrino-lab/r-snge100-python`

- Add as a git submodule at `r-snge100-python/`.
- Inspect the API. If it follows the same `controller.py` /
  `driver.py` pattern as the other instruments, build a NiceGUI panel
  using the standalone-or-embedded `build_page(get_controller,
  show_connection)` pattern (see `b2987b/gui.py` or `vx2740/gui.py`
  for the template).
- Wire into `ExperimentConfig` (add `nge100_visa` field),
  `InstrumentHub` (`connect_nge100` / `disconnect_nge100` /
  `instruments['nge100']`), `daq/webgui/shell.py` (add to
  `_INSTRUMENT_SPECS` + new `_build_nge100_tab`).
- IP is `172.16.0.19` (this is the **MUX rail PSU**; the Cremat PSU
  at `.33` is separate).

### Plots tab in the DAQ web GUI

The plot library is already designed for it. Add to
`daq/webgui/shell.py`:
- New tab `t_plots = ui.tab("plots")`.
- New `_build_plots_tab()` function with:
  - Plot-type dropdown populated from `daq.plotting.PLOTS.keys()`.
  - Live toggle that picks `daq.plotting.find_latest()` as the source.
  - When not live: text input or file selector for HDF5 path
    (`/home/ets/ETS-pythonDAQ/data/` listing).
  - Optional second file picker for **overlay** mode + per-file label
    inputs.
  - Optional knobs (channel/index/bins/bias_group/log_y) — pass
    through to the plot function.
  - Render into `ui.matplotlib()`. After each change, the function
    grabs the figure's axes, clears it, re-applies `apply_dark_style`,
    calls the plot fn, calls `.update()`.

### Future improvements (defer until bench tests work)

1. SOCKET-mode refactor for `b2987b/driver.py` (sweep response parser
   needs to use `*OPC?` for completion detection).
2. Subtract bias-filter DC baseline from the IV before V_BD estimation.
3. Extend dark IV to 65 V default so the breakdown knee is unambiguous.
4. Per-channel self-trigger threshold knob in the bench script.
5. Add a Cremat-PSU NGE100 too (`.33`) once the NGE driver is added.

## Files touched today

- `scripts/bench_test.py` (new) — the end-to-end bench-test driver
- `daq/plotting.py` (new) — shared plot library
- `scripts/plot_bench.py` (new) — CLI driver for the plot library
- `plots/*.png` (new) — first batch of plots for inspection
- `docs/continuation_log_2026-05-26.md` (this file)

## How to run

```bash
# After power-cycle, with the webapp stopped:
python scripts/bench_test.py
python scripts/plot_bench.py --list
python scripts/plot_bench.py iv --live
```
