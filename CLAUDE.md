# Project: ETS-pythonDAQ

nEXO SiPM tile-characterization DAQ for the Brunner neutrino lab (Yale).
The repo lives on the `etsdaq` machine and drives a stack of GPIB/USB/LXI
instruments. Six instrument drivers are vendored as git submodules under
`<repo>/*-python/`.

User: **Lucas Darroch** (lucas.darroch@yale.edu).


## Session kickoff

At the start of each session, do this in order:

1. **Read `docs/session_log.md`** — the latest entry is at the top.
   Past decisions, open threads, and "what we tried that didn't work"
   live there. Skim the most recent 1–2 sessions before doing anything
   non-trivial.
2. **Quick health check** (only when the user mentions hardware/bench/webapp):
   - `systemctl --user is-active daq-webapp` — should print `active`
   - `ls -t data/bench_*.h5 | head -3` — most recent bench runs
   - `cat data/last_vbd.json` — cached V_BD from the last successful IV
3. **Update `docs/session_log.md` as you go.** Add a new entry at the top
   with the current ISO date. Capture *changes made*, *decisions* (with
   the reason), and *open threads* for the next session. Don't just narrate
   the conversation — record what's useful to know months from now.
4. **Commit decisions belong in commits, not the log.** When you commit
   code, the commit message is the canonical record; the log is for
   work-in-progress, exploration, and tribal knowledge.


## What this codebase is

A layered DAQ that controls instruments, runs measurements, persists data
to HDF5, and serves a NiceGUI control web app. Layers (matching the
sweep granularity):

```
primitives (instrument-level)
   → measurement (single SiPM, single sweep)
      → tile      (loop over SiPMs at one temperature)
         → temppoint (full sequence at one temperature)
            → run    (loop over the temperature schedule)
```

Most actual benchwork right now is **single SiPM, no MUX, no stage, no
temp control**, exercised through `scripts/bench_test.py` (the closed-loop
sweep harness) and the web app's per-instrument panels.

Key files:

| Path | What it is |
|---|---|
| `daq/webgui/shell.py` | NiceGUI shell — top-level tabs, status pills, BIAS OFF button |
| `daq/webgui/webcam.py` | C525 webcam capture singleton + `/webcam.mjpeg` route |
| `daq/webapp.py` | Entry point for the web app (`python -m daq.webapp`) |
| `daq/gui/hub.py` | `InstrumentHub` — shared controller objects + status |
| `daq/config.py` | `ExperimentConfig` dataclass with lab defaults |
| `daq/plotting.py` | `PLOTS` registry — every plot the GUI/bench can render |
| `scripts/bench_test.py` | Closed-loop sweep harness (CLI: `--skip-iv`, `--only`, `--vbd`, `--no-plot`) |
| `data/last_vbd.json` | V_BD cache written by every successful IV |
| `data/bench_*.h5` | Per-run HDF5 outputs |
| `plots/*.png` | Auto-rendered plots (one per registered plot type per run) |
| `~/.config/systemd/user/daq-webapp.service` | Systemd user service for the webapp |


## Hardware (current bench)

The default config in `daq/config.py` matches the lab. Verify with the
user when something looks off — physical setup changes faster than this
file.

| Instrument | Address | Notes |
|---|---|---|
| B2987B electrometer | `TCPIP::172.16.0.11::5025::SOCKET` | **NEVER use `::INSTR` (VXI-11)** — leaks session slots, requires power-cycle. SOCKET is stateless. See "Lessons" below. |
| K6485 picoammeter | `/dev/ttyUSB0`, 9600 baud, `\r`/`\r` term | Reads SiPM low-side current. NOT the B2987's built-in ammeter — that's wired to the (unbiased) photodiode. |
| CAEN VX2740 digitizer | `172.16.0.51` | 125 MS/s, **64 channels** (full set exposed in the GUI). |
| Keysight 33510B (WFG) | `TCPIP0::172.16.0.46::5025::SOCKET` | **The visible WFG in the shell.** Agilent 33510B, S/N MY57200344. SOCKET, not `::INSTR` — same VXI-11-avoidance reasoning as the B2987. |
| Rigol DG1022 (WFG, hidden) | `/dev/usbtmc0` | Still in the codebase (bench scripts use `HUB.wfg`). Hidden from the header pills + "connect all" via `hidden: True` on its `_INSTRUMENT_SPECS` entry. Reachable through the Settings menu → "wfg (dg1022, hidden)". |
| R&S NGE100 PSU | `TCPIP0::172.16.0.19::INSTR` | Powers the Cremat CSP+shaper. |
| Pulse MUX | `/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_ec8db4c99972ef11ae387a4f8fcc3fa0-if00-port0` | CP2102N USB-serial, 9600 baud. by-id symlink is stable across replug. |
| Phidget stage | three serial numbers in config | Not always plugged in. |
| Logitech C525 webcam | `/dev/video0` | Lab-view feed, streamed at `http://<host>:8765/webcam.mjpeg`. |

Typical results on this bench, room temperature: **V_BD ≈ 52.25 V**,
**SPE ≈ 600–900 ADC at OV+3**, **DCR ≈ 400 Hz** at SPE-cut threshold.
The Cremat shaper **saturates above OV+3** — use OV+1 for clean SPE
measurements.


## Bench workflow

```bash
# Full sweep (~4 min): IV → K6485 baseline → VX2740 pulses → OV scan →
# LED amp sweep → clean OV scan → DCR vs OV → crosstalk/AP → LED width
# sweep → threshold scan → noise floors. Then auto-plot everything.
python scripts/bench_test.py

# Fast iteration loop (uses cached V_BD, no IV, ~30 s):
python scripts/bench_test.py --skip-iv --only ov_scan_clean,dcr_vs_ov

# Override V_BD without an IV at all:
python scripts/bench_test.py --vbd 52.25 --only threshold

# Skip the auto-plot:
python scripts/bench_test.py --no-plot
```

Test keys for `--only`: `iv`, `k6485`, `pulses`, `ov_scan`, `led_amp`,
`ov_scan_clean`, `dcr_vs_ov`, `crosstalk`, `led_width`, `threshold`,
`vx_noise_floor`, `k6485_noise_floor`.


## Web app

A systemd **user** service, lingering enabled (survives logout, starts
at boot). Default port 8765, bind on `0.0.0.0`.

```bash
systemctl --user status daq-webapp        # health
systemctl --user restart daq-webapp       # pick up code changes
systemctl --user stop daq-webapp          # take it down
journalctl --user -u daq-webapp -f        # live log
```

URLs:
- Local:       <http://localhost:8765/>
- Lab subnet:  <http://172.16.0.216:8765/>

**Login.** Shared password gate. Login page at `/login`; visiting `/`
unauthenticated redirects there. Routes also gated: `/webcam.mjpeg`,
`/webcam.jpg`, `/labbook-paste` all return 401 without a logged-in
session. Default password is in code; override at runtime with
`DAQ_PASSWORD` in `.env`. Users still self-declare a display name on
the login form (kept in the session for the who's-connected pill in
the header).

**Conflict with bench scripts:** every instrument allows only one
concurrent session. The "⏏ release all instruments" button at the top
of the Connections tab disconnects everything in the webapp so a
scripted bench run can claim the same instruments.


## Conventions & lessons

- **Don't talk to the B2987 over VXI-11.** Use SOCKET on port 5025. The
  VXI-11 listener leaks session slots on every abnormal exit (Ctrl-C,
  exception during shutdown, pyvisa-py bug), and once it's full the only
  recovery is a power-cycle. SOCKET is stateless on the instrument side
  — no session table.
- **Coarse-then-fine IV needs a settle between passes.** Going from
  high bias (avalanche, ~µA) to low bias (just below V_BD) produces a
  discharge transient the K6485 sees as a spurious large reading. We
  pre-settle 2 s at `fine_lo` and discard one K6485 sample before the
  fine pass. Without this, V_BD comes out ~1.5 V low.
- **VX2740 has 64 channels, not 5.** The legacy "SiPM ch0–3 + PMT ch4"
  convention is a default-on set, not a hardware limit. The GUI exposes
  all 64 in an 8×8 grid with quick-action helpers (`all`, `none`,
  `invert`, range parser like `"0,4,8-15"`).
- **OV scans saturate above OV+3 on this bench.** Cremat shaper clips
  around 1–2 V at the CAEN input (after the 10 dB pad). For *clean*
  gain extraction use the LED-off OV scan at OV+1..+3 only;
  `plot_ov_scan_clean_gain` automatically excludes saturated points
  from the fit.
- **Photodiode current ≠ SiPM IV.** B2987's built-in ammeter is on its
  own input, wired (on this bench) to the unbiased photodiode. The SiPM
  IV is measured by K6485 on the low side. `iv_measure_photodiode=False`
  is the default; flipping it on just adds a photodiode diagnostic
  trace.
- **Don't poll bench progress in tight loops.** The bench writes a
  final `=== summary: N/N PASS ===` line and an `HDF5 output:` line —
  wait for those, not for intermediate logging. Most events are noisy
  per-step lines that aren't worth surfacing.
- **No emojis in code/files** unless the user asks for them.
- **Avoid commenting WHAT the code does** — explain *why* when the
  reason is non-obvious. References to the current task, fix, or
  callers belong in commit messages, not source comments.
- **Don't pile on defensive error handling** for cases that can't
  happen. Trust the framework / internal callers. Validate at system
  boundaries only.


## Known issues (not blockers)

- `daq-webapp.service` SIGKILLs on stop after the 20 s grace when an
  MJPEG client is connected — uvicorn doesn't cancel the streaming
  response. Fix: add an `app.on_shutdown` hook that signals the webcam
  grabber thread and closes active streams.
- `daq/webapp.py` shutdown handler raises `'NoneType' object has no
  attribute 'reset_input_buffer'` when the MUX was never connected.
  One-line None-guard fix.
- `scripts/bench_test.py` is ~1700 lines and ripe for splitting into
  `daq/sweeps.py` + thin CLI wrapper.
- No tests. Each instrument module has `mode="simulation"`; we could
  exercise the dispatch in CI with sim instruments.


## Useful one-liners

```bash
# Latest bench HDF5 and what test groups it contains
h5ls -r "$(ls -t data/bench_*.h5 | head -1)" | head -30

# Reproduce the most recent plot for one test type
python scripts/plot_bench.py threshold_scan --live --log-y

# Sudo password is sometimes needed for udev / loginctl / setfacl;
# the user has explicitly shared it earlier in conversation history —
# if you need it again, ASK rather than assume.
```
