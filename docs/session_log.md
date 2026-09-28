# Session log

Reverse-chronological. **Latest entry at the top.** Each entry covers
one working session (≈ one Claude Code conversation): what changed,
why, and what's still open. Commits are the authoritative record of
code; this log captures the *exploration*, *decisions*, and *tribal
knowledge* that don't survive in `git log`.

> If you are reviewing this in a fresh session, also read **`CLAUDE.md`**
> at the repo root — it's the session-kickoff procedure and the long-lived
> hardware/architecture cheat sheet.

---

## 2026-09-28 — Digitizer tab: recent-acquisitions list in the waveform / spectrum viewers

### Where raw waveforms go (user asked; answer recorded here)
- **Digitizer tab** ("Store raw waveforms", default off): nothing is ever
  written to disk. The switch only keeps the traces in memory so the
  waveform viewer has something to show; pulse finding (amplitudes and
  timestamps for the spectrum) runs either way. The label now says so.
- **L2 pulse** ("store raw", default on): traces saved to the L2 HDF5
  (`MSTORE.save_l2_pulse_run` → `h5io.write_pulse_multichannel`).
- **L2 pulse sweep**: never saves traces, only per-bias amplitudes,
  timestamps and rates.
- **L3 sequence** ("store raw", default off): traces saved to run.h5 only
  when on.

### Changed (shell.py)
- Module-level `DIG_HISTORY` (newest first, 20 entries), shared by every
  browser, so a reload or second viewer sees the same list. Raw waveforms
  are capped at `DIG_RAW_BUDGET_BYTES` (256 MB). Once a run doesn't fit, it
  and every older run lose their traces (the raw column says "dropped") but
  keep amplitudes. The newest run always keeps its own.
- Waveform and spectrum sub-tabs: each has a list beside the plot (#, time,
  wfs [got / requested if stopped early], channels, pulses, raw, trigger)
  with "follow latest" (default on). Clicking a row plots it.
  - **redraw** re-plots the selection.
  - **overlay** freezes the plotted trace(s), so the next pick draws on top
    (up to 8).
  - **clear plot** empties the plot.
  - Channel, waveform # and bins act on the live trace. Picking a run jumps
    to a channel that has data and clamps waveform #.
- Spectrum: step lines, not bars. Overlaid spectra share bin edges, and
  there is a lin/log counts toggle (zero bins are dropped on log).
- The viewer says why a trace is missing: store raw was off, raw dropped
  for memory, no waveforms on that channel, or index past the end.

### Testing
- Isolated harness on :8896 with a simulated VX2740 (dark rate raised to
  2e5 Hz for pulses). `HUB.connect_dig` was patched to sim so nothing could
  reach 172.16.0.51. Driven with headless Chrome: follow, next/prev,
  overlay, a UI-run acquisition, clear, redraw, row picks, no-raw run,
  index clamp, lin/log, rebin, budget-drop order, second browser. No JS
  exceptions. Scripts are in the session scratchpad `digview/`, not kept.

### Open threads
- Nothing on the digitizer tab saves to disk. If a quick-look acquisition
  turns out to be worth keeping, a "save .h5" button on a list row would
  reuse `h5io.write_pulse_multichannel`. Not built; the user hasn't asked.

---

## 2026-09-27 (midnight) — Data tab: tick-box multi-file download

### Changed (shell.py `_build_data_tab`, h5browse.py)
- Each file row has a tick box. Above the list is an "all" box that
  applies to the rows the name filter currently shows (it goes
  indeterminate when only some are ticked), plus an "N selected" count and
  a download button. Ticked files stay ticked when the filter changes.
  One ticked file downloads as is; several are zipped first
  (`h5browse.zip_files`: folder layout kept relative to data/,
  `_safe_under_root` checked, level-1 deflate for speed).
- Zips are written to `$TMPDIR/daq_downloads/` and served single-use.
  Zips older than 1 h are deleted each time a new zip is built.
- The existing per-file "download" button in the structure card is unchanged.

### Testing note
- NiceGUI's `user_simulation` works outside pytest only with
  `PYTEST_CURRENT_TEST` set and a `main_file` app (the `root=` form falls
  into script mode). Programmatic event names are camelCase
  (`update:modelValue`). Script: session scratchpad `sim_data_tab.py`, not
  kept in the repo.

---

## 2026-09-27 (late night, 3) — B2987 plot: lin/log current axis

- Toggle lin | log next to averaged | timestream. Log plots |I| (axis
  "|I| (A)"), drops exact zeros, hides the planned-voltage dots (at I = 0) and
  any sigma line that would cross zero. Tick labels now use exponent notation
  via an ECharts `":formatter"` (NiceGUI evaluates `:`-prefixed option keys as
  JS). Switching view or scale during a live run redraws the live data at the
  next tick instead of flashing the previous run.
- Tested on the fake B2987 with mixed-sign currents; no console errors.
- Held back at 23:52 while Lucas's 605-reading sweep ran; deployed after he
  asked (B2987 idle, output off, released on restart).

---

## 2026-09-27 (late night, 2) — live plotting for B2987 sweeps and ammeter runs

### Changed
- Controller `sweep_live(voltages, n, delay, on_point)`: software-stepped
  sweep (checks all voltages against lock + HV interlock up front, output on
  at the first voltage, `write_level` per step, readings in ~1 s chunks via
  `acquire_current`, output off in `finally`). The hardware list sweep
  (`sweep()`) can only be fetched once finished, so it can't show progress;
  `sweep()` is unchanged for L2/L3/bench callers. `ammeter(n, interval,
  on_chunk)` now acquires in ~1 s chunks too.
- Driver split: `configure_current_sense()`, `acquire_current()`,
  `write_level()`; stop flag cleared once per run (`reset_stop()`), checked
  between chunks (`check_stop()`); completion poll every 20 ms.
- Tab: 0.5 s live redraw of the running data in the chosen view (averaged
  running means per voltage, or timestream), stats line "live k/N", plot
  cleared at run start, markers only when <= 200 points. Stop keeps the
  partial data on screen and saves it (h5 attr `stopped=True`).

### Verified
Fake B2987 in headless Chrome: live counts 16/32/50/66/82 of 100, markers
off at 232 points, partial kept + saved on Stop. Hardware (40/50 V x50):
chunks of 8 every ~1.2 s; 5.6 s gap after the 40->50 V step (auto-range
settling on pA ranges); the timestream shows the settling transient
(-32 fA decaying to ~0 over ~6 s). Ammeter 100: chunks of 60 + 40.

### Incident
My hardware test connected while Lucas's webapp session held the B2987
(idle, output off, after he stopped a sweep). `connect_elec` sends *RST, so
his session's instrument settings were reset. Abort such tests when the
webapp holds the instrument; don't just print the socket count.

---

## 2026-09-27 (late night) — sweep "stall" root cause; persistent 55 V bias lock

### Root cause of the stalled 605-point sweep (verified on hardware)
Not a stall: **auto-range settling**. With auto-range allowed down to the
2 pA range and a dark photodiode (~ -15 fA), every bias step makes the
ammeter re-range and settle for ~4.6 s; readings at constant voltage take
0.118 s (0.1 s delay + 1 PLC). 121 steps x 4.6 s + 605 x 0.12 s ~ 10.5 min >
the 600 s sweep timeout. With the floor at 2 nA the step gap is 0.13 s (but
SEM ~20 fA instead of ~2 fA). Afternoon sweeps were fast because the
photodiode was lit (nA-uA).

### B2987 facts learned
- `:STAT:OPER:COND?` answers mid-acquisition: 1152 busy, 1170 idle
  (bits 1 and 4 = transient/acquire idle).
- `*OPC` then `*ESR?`: the instrument holds the query until the acquisition
  ends (so it can't be used to poll or stop). Don't use it.
- Errors at INIT (e.g. -222 source range after *RST's 20 V default) end the
  acquisition immediately with 1 point; `+231 "Invalid reference data"`
  appears when the zero-reference acquisition fails (seen with fixed range).
- FETC after ABOR does not report the partial count reliably.

### Changed (driver submodule + shell.py + sequence.py)
- Completion: poll STAT:OPER:COND? for bits 0x12; afterwards drain the error
  queue: any error except +231 -> RuntimeError "B2987 reported: ...";
  +231 -> `driver.last_warnings` (shown in the tab log). Stop/timeout abort.
- **Bias lock** (driver): `~/.config/b2987b/bias_lock.json` (env
  `B2987_BIAS_LOCK_FILE` overrides), re-read before every set_voltage, list
  sweep and output_on (which queries the instrument's actual level first);
  `BiasLockExceeded`, nothing sent; unreadable file fails closed. Hardware
  mode only. Set to **55.00 V** tonight. Raw SCPI or the front panel bypass
  it.
- Web app: `_bias_lock_refuses()` pre-check (message, run doesn't start) at
  L1 set/toggle bias, L2 IV / pulse / pulse sweep / scan (before
  `_prep_position` moves anything), B2987 output toggle / source apply /
  sweep; L3 `run_sequence` checks all planned voltages first. Lock control in
  the B2987 Source block (raising asks for confirmation; logged with the
  user's name). Measure block: "Auto-range floor" (default 2 pA) with hint.
- Hardware verified: 40/50 V x50 at both floors, Stop mid-sweep (output and
  input off), ammeter 50 samples output off, set_bias(56) refused. UI
  verified against a fake B2987 answering STAT like the real one.

### Open threads
- Default auto-range floor stays 2 pA (sensitivity); fast IV sweeps need
  2 nA+. Lucas to decide the default.
- keysight2987b-python changes (lock, series, stop, completion, partial
  configure_sweep) still need commit + push + bump.

---

## 2026-09-27 (night) — B2987 sweep "doesn't change the bias"; Stop button

### Diagnosis (read-only queries while Lucas's sweep was loaded)
- The Source-output card's "source level" is the driver's cached last
  commanded voltage (`_source_voltage`, 50 V from the Source block); a list
  sweep never updates it, so it always read 50 V during sweeps.
- `_run_sweep_hardware` turned the output on in LIST mode before triggering:
  the output sits at the immediate level (the Source block's 50 V) until the
  first trigger.
- Completion was detected with `STAT:OPER:COND?` and `resp[2] == "7"`. Verified
  values: 1152 while TRAN+ACQ are busy, 1170 when idle.
- Tonight's sweep (40->52 V, 0.1 V step, x5 = 605 list points, fixed 16.7 ms
  aperture, auto-range, delay 0.1 s) **stalled**: status stayed 1152 for many
  minutes, `FETC` blocked, error queue empty, ARM/TRIG all AINT, then the
  600 s timeout aborted it (no h5 written). Sweeps that completed today: 15:58
  (13 voltages, 1 V step, 65 pts, AUTO aperture) and 17:0x (1 voltage, up to
  1000 pts, FIXED aperture). **Root cause of the stall is open**; not the
  point count alone.

### Changed (keysight2987b-python submodule, still uncommitted, + shell.py)
- Driver: `_wait_acquisition()` starts INIT, sends `*OPC`, polls `*ESR?`
  (bit 0 done; bits 2-5 -> read `SYST:ERR?`, abort, raise "B2987 reported:
  ..."), honours `request_stop()` (thread-safe Event; the polling thread sends
  `ABOR`), times out with ", N points acquired" appended. Used by sweeps and
  the ammeter series. Sweep sets the immediate level to the first list point
  before OUTP ON, and always turns ammeter + output off in `finally` (a VISA
  error mid-sweep used to leave the output on). `AcquisitionStopped`
  exception; controller `request_stop()`.
- Tab: "Stop" button (enabled only while running); status pill shows elapsed
  vs estimated time; output card shows "sweeping A -> B V" while running;
  tab log lines also go to the journal (logger `webapp.elec`); header BIAS OFF
  also requests a stop.
- Verified against a fake B2987 over TCP (list sweeps with real timing,
  *OPC/*ESR?, ABOR, error queue): completion, first-point start, Stop mid
  605-point sweep, BIAS OFF mid-sweep, injected -222 error surfaced; output
  and ammeter off afterwards in every case. Not yet on hardware.

### Open threads
- Reproduce the stall on hardware with the new code: the Stop/timeout message
  now says how many points were acquired (0 = never started; k = stalled
  mid-way). Candidate differences to bisect: 0.1 V vs 1 V step, 121 vs 13
  distinct voltages, FIXED 16.7 ms vs AUTO aperture, output already on at 50 V
  before INIT.
- keysight2987b-python changes need commit + push + submodule bump.

---

## 2026-09-27 (afternoon, 4) — B2987 tab: ammeter mode, averaged/timestream views, reads with output off

### Root cause (verified on the instrument, output off)
Driver reads used `:TRIG1:ALL` + `:INIT:ALL`, which also starts the source's
transient action: with the output off the B2987 returns 9.91e+37 and
`+212,"Output relay must be on"`. `:INIT:ACQ` reads the photodiode fine
(-0.45 pA, no error). Line frequency reported: 60 Hz.

### Changed — keysight2987b-python submodule (UNCOMMITTED; needs commit + push
there and a bump here)
- `driver.measure_current` / `measure_voltage`: `:TRIG1:ACQ` + `:INIT:ACQ`.
- `driver.measure_current_series(n, interval_s, ...)`: applies range and
  aperture, zero-reference OFF (absolute readings), TIM-paced or back-to-back,
  `*OPC?` with a sized timeout, returns raw currents + UTC timestamps.
- `controller.ammeter(n, interval_s)` -> SweepResult (raw + mean/SEM), so
  plotting and `h5io.write_sweep_result` are shared with sweeps.
- `controller.configure_sweep`: source_range / n_per_voltage / delay_s /
  measure_voltage / current_limit are now truly partial (default None). Before,
  any partial call (e.g. the tab's Measure or Timing apply) silently reset the
  source range to 1000 V and the current-limit resistor to off and wrote both
  to the instrument, even with the output on. All existing callers pass these
  explicitly or rely on values equal to the controller's initial state.

### Changed — electrometer tab (shell.py)
- Mode toggle Sweep (IV) | Ammeter (samples + interval, 0 = back to back);
  view toggle averaged | timestream (IV: I(V) vs all raw samples vs t;
  ammeter: mean +/-1 sigma vs all samples vs t). Stats line: mean, sigma, SEM,
  n, duration, source state, over-range count. Ammeter runs saved as
  `data/elec_ammeter_*.h5` (raw + averaged).
- "read I" = `ammeter(1)`: works with the output off, uses the on-screen
  range/aperture.
- Runs and reads apply the Measure and Timing blocks first (what you see is
  what runs); Source is left alone.
- NPLC: hint with ms per reading and guidance, presets 0.1 / 1 / 10, estimated
  run time in the header. Delay renamed "Settling delay (sweep)" with
  guidance. Removed "Trigger source" and "Timer interval": never applied.
- After a sweep the Status bias state records the real output state (the
  driver turns the output off at the end of a sweep).

### Verified
Harness (simulated B2987): read I with output off, 100-sample ammeter
(stats, averaged = mean +/- sigma lines, timestream = 100 points), IV sweep
(averaged 5 pts, timestream 25 raw), NPLC presets/hint, h5 files, source range
20 V + resistor survive the pre-run apply. 0 JS / 0 server errors. Hardware:
the acquire-only read path (see above).

### Incidents
- A first B2987 probe ran while Lucas's webapp session held the instrument
  with the output on; it sent only `*IDN?`, `:OUTP1?`, `:INP1?`, `*CLS` before
  failing. Always check the webapp's sockets and OUTP1 first and abort.
- The requested restart (16:4x) also interrupted Will's and Lei's sessions;
  release disconnected elec (output off), stage and slow control.

---

## 2026-09-27 (afternoon, 3) — NGE100 device rails: mapping, power buttons, cremat tab

### Changed
- `daq/rails.py`: mapping {ch: {device, voltage V, current A}}, load/save/
  validate, `power_on`/`power_off` per device. Power-on applies V/I (APPL) to
  every mapped channel first, then OUTP ON each; if any output fails, all of
  the device's channels are switched off again (rails go on together or not
  at all).
- Defaults in `config.nge100_rails` (config.py + template): ch1, ch2 ->
  cremat (CSP/Shaper) 6 V / 600 mA; ch3 -> ivmux 12 V / 200 mA. UI edits are
  saved to `<repo>/.nge100_rails.json` (gitignored) and loaded at startup in
  `daq/webapp.py`.
- nge100 tab: "Devices" card with a power row per device and the mapping
  editor (device select, V, mA) + "update mapping". Saving never touches the
  PSU; settings apply on the next power-on. Warns if a channel that is on was
  moved to another device (its old device's power-off no longer covers it).
  Channel cards' V/I fields default to the mapping.
- `_build_rail_power(device)` (shared widget: state on/off/partly on/no
  response, setpoints, live V/I, buttons disabled with the reason when the
  NGE100 isn't connected or nothing is mapped) on the nge100 tab, the iv-mux
  tab (side column) and a new cremat tab (settings menu -> "cremat
  (CSP/shaper)"). Commands go through `_NGE_CMD_LOCK`; readback shares the
  single background NGE poll (`_read_nge100_state`, now module level).

### Verified (isolated harness, simulated NGE100 behind `_SerializedNGE100`)
IV MUX on from its tab -> exactly apply(3, 12, 0.2), output_on(3); CSP on/off
from the cremat tab -> ch1/ch2 at 6 V / 0.6 A, ch3 untouched; mapping edit
(ch3 11 V) saved and used on next power-on; 40 V rejected, file unchanged;
ch2 output_on failure -> ch1 and ch2 switched off; NGE disconnected ->
buttons disabled with reason. 0 JS / 0 server errors. Not yet exercised on
the real PSU.

### Open threads
- Mapping edits in one browser don't refresh the editor fields in another
  open browser until reload (power cards do update live).
- `_release_instruments` still doesn't include nge100, so a webapp stop leaves
  the rails as they are (intended for amplifier supplies? confirm with Lucas).

---

## 2026-09-27 (afternoon, 2) — IV MUX is 30 channels; serial monitor on the iv-mux tab

### Changed
- **30 channels, not 90.** The firmware addresses 6 boards x 15 (1-90) and
  always dumps six words; this bench has the first two boards. New
  `config.ivmux_channels = 30` (config.py + template). New `daq/ivmux.py`
  `IVMux(MuxController)` refuses channels outside 1-n in `select`, `sweep`,
  `sequence` and raw commands, before anything reaches the firmware (a relay on
  an absent board would "succeed" with nothing connected). Hub and `run.py`
  build the IV MUX through it. UI limits/labels (iv-mux tab, L1 card, L2
  input) read the config. The submodule's 90 is the firmware protocol and is
  left alone.
- **Serial monitor** (iv-mux tab): `_SerialTap` wraps the driver's pyserial
  port (`_drv._ser`) and records every line into `IVM.TRAFFIC` (deque 2000,
  sequence-numbered), tagged "poll" for the UI's periodic `d` dumps
  (`poll_active_channel`), "user" for raw sends, "" for everything else incl.
  measurements. Log view (hide-polls toggle, clear), raw command input
  (Enter/send) via `IVMux.send_raw` under the driver lock; `t` is stopped with
  `x` after ~3 s; `q` gets a dwell-scaled timeout.

### Verified
Fake-firmware pty: out-of-range select/sweep/sequence/raw all refused with 0
bytes sent; raw replies incl. "Error in command"; `t` stream stopped and the
next command works; tags correct. Headless Chrome on an isolated app: "30-channel"
strip, `max=30` on every channel input, live log, `a 12` + Enter -> reply +
active ch12, `a 45` -> fitted-range error, toggle/clear OK, 0 JS/server errors.
Real MUX (read-only: `d` only, closed without `z`): same traffic in the monitor.

### Open threads
- `IVMux` reaches into `MuxController._drv._ser` / `_drv._lock`; an
  `on_traffic` hook and `n_channels` parameter upstream in ivmux-python would
  remove that.
- Channel -> physical relay mapping for 1-30 assumes the fitted boards are
  firmware boards 0 and 1 (chain order); not checked on hardware.

---

## 2026-09-27 (afternoon) — IV MUX is on the CP2102 at /dev/ttyUSB0; ports pinned by-id

### Correction to 2026-09-26
The CP2102 (USB serial `0001`) at `/dev/ttyUSB0` is the **IV MUX's** adapter
(Arduino Serial1 over USB-UART, 9600 8N1), not the K6485's. The K6485 uses a
Prolific PL2303 ("USB-Serial Controller D", no serial; last seen May 27), so
`/dev/ttyUSB0` only meant the K6485 when the PL2303 enumerated first. Last
night's `ivmux: /dev/ttyUSB0` was the right port; the hang came from the MUX
not answering at that time (firmware wedged or board unpowered — unknown), and
the K6485's timeouts were its connects opening the MUX's adapter.

### Verified (14:3x, read-only)
Raw probe at 9600 8N1: `d` -> `Command: <d>`, `Dump`, six `0` words,
`Command complete` in 60 ms. `InstrumentHub.connect_ivmux()` on the by-id path:
OK in 2.1 s (2 s settle + reply), `active_channel()` None in 61 ms, closed
without sending `z` (relays untouched, all off).

### Changed
- `config.py` and `config.example.py`: `ivmux_port` = CP2102 by-id path
  (replaces the `/dev/ttyACM0` placeholder); `k6485_port` = PL2303 by-id path
  `usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0`, inferred
  from the May kernel log, **unverified until the PL2303 is plugged in**.
- `scripts/bench_test.py`: same K6485 by-id default.
- `.last_connections.json`: dropped `k6485: /dev/ttyUSB0` (it overrides the
  config at startup). Backup in the session scratchpad.
- CLAUDE.md hardware table: K6485 row fixed, IV MUX row added.
- Service restarted (clean stop in 0.19 s); IV MUX not yet connected in the
  webapp — connect it from the iv-mux tab or "connect all".

### Open threads
- The round-2 adversarial verification workflow (wf_310c6100-534) was cut off
  when the previous session ended; round-2 fixes are deployed but only
  smoke-tested.
- If a second stock CP2102 (serial 0001) is ever added, switch the IV MUX to
  its `/dev/serial/by-path/` name.

---

## 2026-09-27 — remote browser "always loading": webcam thread leak + socket never connecting

### Symptoms
Lucas, remote (10.127.184.205, Yale VPN path via 172.16.0.1): page "always
loading". The InfluxDB UI on this machine's :8086 works from the same browser.

### Findings (5 reproduced hypotheses + adversarial verifiers; scratch
`wf/` artifacts in the session scratchpad)
- **Webcam stream (everyone, confirmed).** With /dev/video0 absent the status
  tab's `<img src=/webcam.mjpeg>` never got even headers (GZipMiddleware waits
  for the first body chunk), so the load event never fired: that is the
  literal endless tab spinner. The old *sync* `_mjpeg_iter` looped forever
  without yielding, so each request pinned an anyio worker thread (Starlette
  cannot cancel it). The pool has 40 tokens and also serves StaticFiles, so at
  40 leaks NiceGUI's JS/CSS stop loading: blank page for any uncached browser.
  PID 1407 logged exactly 40 grabber starts, the 40th at 21:46:04; PID 10549
  had reached 28/40 before the redeploy. It also made every service stop hang
  until SIGKILL, so `on_shutdown(_release_instruments)` never ran.
- **Remote socket.io handshake never completing (remote-only, confirmed; root
  cause open).** 19 of 30 remote page builds after 23:57: websocket upgrade got
  101 + engine.io OPEN (ACKed), the browser's `40` CONNECT never arrived,
  server closed at +6 s, the client never answered the FIN (FIN-WAIT-2 60 s),
  no reconnects until a manual reload. The server-side pattern matches a
  browser main-thread stall right after `new WebSocket()` (reproduced exactly)
  better than a websocket-breaking middlebox with live JS (which would retry
  within 1-9 s or reload every 20 s). Client-side interception (AV web shield,
  VPN web-security module, extension) is the other candidate. Needs a check in
  the remote browser's DevTools, InPrivate/extensions-off, or another browser.
- NiceGUI defaults are LAN-tuned: reconnect_timeout 3 s -> ping 4 s /
  timeout 2 s, and a client down > 3 s is deleted, so its reconnect reloads
  the page. Reproduced; a secondary effect on the remote session.
- Refuted as causes: page weight (heavy: 872 KB HTML / 127 KB gz, 2779
  elements, ~0.85 s synchronous build, but only slow), auth/storage loss on
  SIGKILL (auth persisted; Lucas authenticated on every build).
- Log caveat: "session connect" is logged when '/' is *built*, not when the
  socket connects. The new `socket connect/disconnect/delete` lines are the
  real signal; a `delete` without `connect` = a page that never went live.
- Polling-only transports survive every network-fault model but fail the same
  way under the page-stall model. `['polling','websocket']` is worse:
  python-engineio 4.13.2 freezes a polling session whose upgrade probe opens
  but carries no frames. Not deployed; an experiment if the remote DevTools
  check shows frames dropped on a live page.

### What changed (deployed 01:58)
- `webcam.py`: async routes; `_first_frame` awaits up to 3 s and gives up at
  once when the grabber thread exits (no camera): 503 in ~50 ms instead of
  3 s; async `_mjpeg_iter` sends each new frame once; `Content-Encoding:
  identity` so gzip passes the stream through.
- `webapp.py`: `reconnect_timeout=30.0`, `timeout_graceful_shutdown=5`,
  socket lifecycle log lines.
- `shell.py`: unauthenticated '/' -> HTTP 303 to /login (was a socket
  message: blank page when the socket fails); SESSIONS unregister on delete
  instead of disconnect; lab-book paste drained only by tabs with a live socket.
- Smoke-tested in an isolated harness: 6 authenticated loads, load event in
  ~1 s, handshake over websocket, 0 leaked threads, static JS 9 ms, SIGTERM
  exits in 0.6 s with the release hook running. After the redeploy the remote
  page's socket connected in 1.7 s.

### Open threads
- `_release_instruments` now runs on every stop (it never did before, because
  stops always SIGKILLed). Its list omits ivmux / ks33500b / nge100.
- Status, L1 and stage tabs stream MJPEG on every page view; with a working
  camera that's ~17 Mbit/s per landing-page viewer (hurts remote users).
  Consider a snapshot + slow refresh or streaming only on the webcam tab.
- Lazy tab-panel building would cut the '/' build from ~830 ms to ~20 ms.

---

## 2026-09-26 — webapp hung by a mis-addressed IV MUX; timers moved off the event loop

### Symptoms (reported by Lucas)
DAQ very slow, devices dropping, web UI won't load.

### Root cause (confirmed)
- `.last_connections.json` had `"ivmux": "/dev/ttyUSB0"` (saved 20:48:51) —
  someone retyped the IV MUX address to the **K6485's CP2102** (WRONG — see
  the 2026-09-27 afternoon correction: that CP2102 is the IV MUX's own adapter,
  so the address was right and the MUX simply didn't answer). The IV MUX
  `connect()` only opens the port, so it reported ✓. From then on the Status
  tab (1 s) and IV MUX tab (1.5 s) timers called `active_channel()` ->
  `dump_state()` -> a serial command with a **10 s** timeout, synchronously on
  the event loop, once per open browser. Evidence: `curl /login` took
  3 ms / 4 s / 6 s / >15 s; main thread in `do_select` 142/150 wchan samples
  (idle asyncio sits in `ep_poll`). Browsers dropped their websockets and
  reconnected every ~40 s; the ~1150 "parent slot of the element has been
  deleted" tracebacks are dead sessions' timers — symptom, not cause.
- A hung MUX couldn't be released from the UI either: `MuxController.disconnect()`
  zeroes first and raises on the timeout *before* closing the port, so
  `disconnect_ivmux` left `HUB.ivmux` set. Only a restart cleared it.
- The pulse MUX had also been saved as `/dev/ttyUSB0` (20:41). Note
  `connection_state._ADDR_ATTRS` has no `ivmux` key, so the IV MUX address is
  *not* restored at startup; `mux` is.

### Other findings
- **CAEN FELib segfaults on two overlapping opens** (reproduced in a scratch
  process with the VX2740 unreachable: 4 sequential failed opens are clean,
  2 concurrent ones -> exit 139). Explains tonight's crashes at 20:12 / 20:16
  (two threads died at once). Sep 18 had a similar crash in `libphidget22.so`
  on Phidget hot-unplug — not addressed.
- **NGE100 tab master switch turned on every rail** when one channel was on:
  the refresh set `master_sw.value = True`, and NiceGUI fires
  `on_value_change` for programmatic sets too (the old comment claiming
  otherwise was wrong) -> `all_outputs_on()`. Reproduced against HEAD with a
  simulated PSU: ch1 on -> ch1/2/3 on within 2.5 s. A failed read
  (`is_on=False`) likewise re-sent `output_off`.
- Hardware: VX2740 (.51) and 33510B (.46) fail ARP (off / unplugged / dead
  switch port); IV MUX Arduino never enumerated today; C525 webcam unplugged
  20:32:58; K6485 timed out on every connect today. Phidget hub, webcam and
  CP2102 were moved between USB ports 20:10–20:33; PC rebooted 4x 20:40–20:47.
  NIC at **100 Mbps** since the 16:21 boot (1000 Mbps on Sep 16–18).
- The webcam is not a factor: opening the missing `/dev/video0` fails in ~1 ms.

### What changed (uncommitted at time of writing)
- `shell.py`: `_reading(key, fn)` / `READINGS` — timers render cached values;
  the query runs in a worker thread, at most one in flight per key however
  many clients ask. Used by Status (temperature, pulse MUX, IV MUX), the MUX
  tabs, and the NGE100 tab (now `_read_nge100` + `_render` + `_refresh_now`).
  MUX tabs show `err` instead of `none` when the query fails.
- NGE100 switches remember the value last shown (`_show_switch`); handlers
  ignore echoes of it. Verified: ch1 on -> only ch1 on.
- `hub.py`: MUX connects require a firmware reply (`_require_mux_reply`) —
  wrong port now fails in ~12 s with `ConnectionError`; MUX disconnects always
  close the port and drop the reference (`_close_mux`); `connect_dig` /
  `disconnect_dig` serialized by `_dig_lock` (verified: 3 rounds of 2
  concurrent connects, no crash); K6485 and digitizer close on a failed
  post-connect setup.
- Verified with headless Chrome against the real tab builders: 3 browsers on a
  never-answering IV MUX -> `/ping` 1–11 ms, one `d` command in flight at a
  time, tab shows the TimeoutError. All four modified tabs build and tick with
  zero tracebacks.
- Removed the bogus `mux`/`ivmux` entries from `.last_connections.json`
  (backup in the session scratchpad); restarted the service 23:57.

### Open threads
- `_close_mux` reaches into `MuxController._drv`; the real fix is upstream in
  `ivmux-python` / `pulse-mux-python`: make `disconnect()` close the port in a
  `finally`. Then drop the reach-in.
- Other connects (`ivmux`, `k6485`, ...) can still race each other from two
  browsers; only the digitizer is locked because only FELib crashes on it.
- Service stop still SIGKILLs after the 20 s grace even when idle (existing
  known issue in CLAUDE.md).
- Chrome headless on this box needs `--no-sandbox --disable-dev-shm-usage
  --password-store=basic --use-mock-keychain` or it hangs at startup.

---

## 2026-06-02 — config.py untracked; template + first-run bootstrap; stop buttons

### What changed
- **`daq/config.py` is no longer tracked** (gitignored as a per-machine user
  setting — instrument addresses + measurement defaults change too often to
  live in version control). The local file (with this bench's LED tweak,
  1.8 Vpp / 80 ns) stays on disk.
- **`daq/config.example.py`** (tracked) is the canonical template — seeded from
  the last committed `config.py` (370c89f), so it carries the shipped lab
  defaults (3 Vpp / 400 ns LED, `ivmux_port`, etc.), **not** the local tweak.
- **First-run bootstrap** at the top of `daq/__init__.py`: if `config.py` is
  absent (fresh checkout), copy `config.example.py` -> `config.py` before any
  `.config` import. Existing `config.py` is left untouched. Must stay above the
  `from .digitizer/.config` imports (digitizer pulls in config transitively).
- Committed the L2 IV/pulse + L3 run/stop control buttons that were WIP in the
  tree (separate commit, not mine).

### Decisions (user)
config.py = local user setting -> untrack + gitignore + template/bootstrap so
fresh machines still start. Keep the run/stop buttons.

### Verified (simulation)
Moved config.py aside -> `import daq` recreated it from the template (led=3.0);
restored local (led=1.8 preserved). Template + __init__ compile.

### Open threads
- If the **template's** defaults drift from what new machines should start with
  (e.g. someone changes addresses on the bench), remember to update
  `config.example.py` deliberately — it no longer tracks `config.py`.

---

## 2026-06-02 — IV MUX added; channel switching moved off the pulse MUX

### What changed

Replaced the pulse MUX with the **IV MUX** as the bench's channel selector.
New submodule `ivmux-python/` (Brunner-neutrino-lab/ivmux-python) — package
`iv_mux`, class `MuxController`. API mirrors `pulse_mux` (select/zero/sweep/
read_temperature/active_channel) **but**: 90 channels (not 96), no bypass relay,
plus a firmware-side `sequence()`. Arduino Nano Every over USB-UART, 9600 baud.

- `daq/config.py`: new `ivmux_port` (default `/dev/ttyACM0` — placeholder; the
  Nano Every wasn't plugged in at the time, so no stable by-id path yet — swap
  one in once it's on the bench).
- `daq/gui/hub.py`: `self.ivmux`, status key `ivmux`, `instruments["ivmux"]`,
  `connect_ivmux`/`disconnect_ivmux` (mode="hardware", same build-then-assign
  pattern as the rest).
- `daq/webgui/shell.py`: `_INSTRUMENT_SPECS` entry `ivmux` (name "iv-mux"),
  **not hidden** — so it gets a header pill and is part of "connect all"
  (the pulse `mux` stays `hidden: True`). New `_build_ivmux_tab()` mirrors
  `_build_mux_tab` and **reuses the `.mux-panel` CSS scope** (no CSS dup);
  bypass card dropped, channel range 1-90. Registered tab `t_ivmux`, panel,
  and `_pill_tabs["ivmux"]`. Status tab gained an "iv-mux channel" card.
- **sys.path**: added `ivmux-python` to all three submodule-path blocks
  (`daq/app.py`, `daq/run.py`, `daq/webgui/shell.py`).

### Channel switching repointed to the IV MUX (per user: "for now we are not
using the pulse mux")
- Automation: `daq/measurement.py` `_move_to_sipm`, `daq/sequence.py`
  `_move_for_condition`/`_exec_scan` now read `instruments.get("ivmux")`.
- `daq/run.py`: added an IV MUX connect block (`instruments["ivmux"]`); kept
  the pulse-MUX block (relabeled legacy).
- Webapp tabs: L1 "MUX channel" card -> "IV-MUX channel" (1-90, HUB.ivmux);
  L2 SiPM/MUX go-to + IV/pulse `_prep_position` + scan handler; raster + 
  alignment `run_all`/`run_scan` now pass `HUB.ivmux` to `raster_scan`/
  `multi_raster`. The pulse-MUX **instrument tab** and its status card still
  target `HUB.mux` on purpose.

### Decisions
- IV MUX visible in header + connect-all; pulse MUX stays hidden (still
  reachable from its own tab). Both controllers coexist in the hub.
- `select_channel` primitive is generic (any controller with `.select()`),
  so repointing was just swapping which controller object is passed.

### Verified (simulation)
py_compile of all touched files; `iv_mux` sim select/zero round-trip;
`daq.gui.hub` + `daq.webgui.shell` import with ivmux in specs/instruments/
visible; `daq.run`/`measurement`/`sequence`/`raster` import clean.

### Open threads
- **`ivmux_port` is a guess** (`/dev/ttyACM0`). Confirm the real device path
  on the bench and capture a stable `/dev/serial/by-id/...` symlink.
- Not yet exercised on a live webapp/browser or real hardware.
- The IV MUX firmware `sequence()` (hardware-timed walk) isn't surfaced in the
  GUI — only the software `sweep` is, matching the pulse-MUX panel.
- `daq/gui/level1_tab.py` (legacy PyQt) still references `HUB.mux`; left as-is
  since the webapp doesn't use it.

---

## 2026-05-29 — L3 rebuilt as a per-SiPM measurement sequence

### What changed

Replaced the old "L3 — tile sweep" (one global setting across the channel
map) with **L3 — sequence**: a list of per-SiPM specs run in order, scripting
the L2 measurements across a whole tile. Adding the same SiPM twice repeats it.

New module **`daq/sequence.py`**:
- `MeasurementSpec` (one list entry = one SiPM) + `SequenceFile`; YAML
  `save_sequence`/`load_sequence` (mirrors config.py style, filters unknown keys).
- Three sample counts per the user's ask: `n_iv_samples_{dark,illum}`,
  `n_waveforms_{dark,illum}`, single `n_scan_samples`. Pulse is a **bias sweep**
  (`pulse_bias_v` list). Conditions: dark / bright / both.
- Spec-driven executors `_exec_iv/_exec_pulse/_exec_scan` call the L1 primitives
  + the digitizer `_ctrl` + the AWG directly (mirroring the L2 run handlers but
  driven by the spec). **L2 / measurement.py untouched.**
- `run_sequence(...)`: entries → leaves (iv / pulse-per-bias / scan), resume via
  `manifest.is_done(step_id)`, two-level `on_progress`, cooperative `abort` dict.
  `build_sequence_steps` gives the manifest/progress total.

Storage (single run.h5, repeat-safe index):
- `daq/storage.py`: `write_iv_seq/write_pulse_seq/write_scan_seq` under
  **`/seq/{idx}/{sipm}/{T}K/{dark|illuminated}/...`** (pulse → `pulse/{bias_mV}/`,
  scan → `scan/{axis}/`) + `/meta/sequence` JSON. Old write_iv/write_pulse/
  write_flux unchanged → tile/temppoint/run still work.
- `daq/h5io.py`: new `write_scan`. `daq/resume.py`: `Step` gained optional
  `seq_index`/`bias_v`/`axis` (`.get` on load → backward compatible) + `set_steps()`.

GUI: `daq/gui/hub.py` adds `ks33500b` to the `instruments` dict (runner needs the
AWG for bright/scan). `daq/webgui/shell.py` `_build_level3_tab` fully rebuilt:
checkbox-gated spec builder (fields from L2 defaults), editable list
(add/duplicate/edit/up/down/delete), save/load YAML, run + ⛔ abort with
entry+step progress. Tab relabeled "L3 — sequence".

HV interplay: `run_sequence` trusts an already-armed confirmer (GUI dialog →
interactive prompt for >60 V); headless + none + >threshold → fail fast before
moving hardware; an explicit confirmer is cleared in `finally`. Added `hv_confirm`
getter to the b2987 driver/controller.

### Decisions (user)
Replace old L3 · per-entry checkboxes {IV,pulse,scan} · GUI builder + YAML ·
single run.h5, per-entry index groups · three sample counts.

### Verified (simulation)
Compile + import; YAML round-trip; `/seq/0`+`/seq/1` (repeat of SiPM 1); pulse
bias subgroups `48000mV/49000mV`; resume skips all; scan datasets+attrs; HV
(armed/explicit/headless) correct; L3 tab builds headless.

### Open threads
- Not yet run on a live webapp/browser or real hardware. Pulse leg needs the
  digitizer `_ctrl` (sim has it; guard raises cleanly if absent).
- Resume keys on list index — editing the YAML shifts indices; a spec-list hash
  is in `/meta/sequence` but drift-warning isn't wired into the GUI yet.
- NOTE: the working tree changed under this session (shell.py line offsets,
  `measurement_store.py`/`h5browse.py` gained unrelated WIP, and an earlier
  HV-interlock log entry I wrote was replaced) — looks like a concurrent edit or
  checkout. My L3 + HV code is present and compiles, but reconcile before commit.

---

## 2026-05-29 — L2 measurements page: 420 px procedure + stretched plots (v3)

### What changed

Tuned the L2 layout per the v3 mockup:

- **Layout columns** are now `420px minmax(0,1fr)` with
  `align-items:stretch`. The procedure card is narrow + tall; the
  plots column gets all remaining width. On a 1800 px monitor each
  plot widens from ~400 to ~640 px.
- **Procedure card stretches** to the plots' height. `.card-l2` is a
  flex column (`min-height:0`); `.mpanel.is-active` is also
  `display:flex; flex-direction:column; flex:1`; `.runrow` has
  `margin-top:auto` — so the run button is pinned to the bottom of
  whichever tab is active.
- **Plot column matches.** `.l2-plots` got `grid-auto-rows:1fr;
  height:100%` and `.l2-plots > .card-l2` flexes; `.plotbox-l2`
  switched from `height:240px` to `flex:1 1 auto; min-height:260px`.
- **Param grids trimmed for the narrow column.** IV switched from
  `g4` to `g2`. Scan AWG subgroup went from `g4` to `g2`. Scan
  source pills shortened (`VUV (ch1)` → `VUV`). Dropped the scan
  intro paragraph.
- **Run button** bumped to 42 px; standalone status pills hidden
  (`set_visibility(False)`) so the inline holder next to the run
  button is the only visible one.

### Files touched

- `daq/webgui/shell.py` — `.l2-panel` CSS scope: `.l2-layout`,
  `.card-l2`, `.mpanel`, `.runrow`, `.l2-plots`, `.plotbox-l2`;
  IV/Scan grid swaps; scan intro removed and source pills shortened;
  three `*_status` HTMLs hidden.

---

## 2026-05-29 — L2 measurements page: horizontal context strip + 1:1 split

### What changed

Reorganized the L2 measurements tab around the *measurement procedure*
card as the hero, per the second-pass mockup
(`measurements_page_target.html` v2):

- **SiPM context flattened into a horizontal strip** above the layout
  (no more tall left card). One row: SiPM id, MUX ch, Bright (x, y),
  Dark (x, y), T (+ read T button + manual/slowctrl source indicator),
  and a live "Save folder · next file" preview on the right.
- **Per-field include-toggles are now compact inline switches**
  (`.inc-toggle` CSS class shrinks the Quasar QToggle to 13 px) inside
  the field's eyebrow label. The corresponding cfld dims via
  `is-off` (opacity 0.4) when unchecked.
- **Save destination promoted into the strip.** Folder override is a
  plain `ui.input` (placeholder "auto (data/...)"). The live preview
  shows `data/<auto>/<colored sub>/<unix_ms>.h5` and refreshes on
  every relevant change (sipm id, sipm toggle, T, folder text).
- **Layout is now 1:1** (`grid-template-columns:1fr 1fr`) — procedure
  card on the left, 2×2 plot grid on the right. Output card removed
  (its inputs moved into the strip). `_out_kwargs()` now always passes
  `basename=None` since the basename input is gone.
- **Run button enlarged** to 40 px / 14 px / weight 500 via a scoped
  override on `.l2-panel .runrow .q-btn` so the most-clicked control
  is unmissable.

### Decisions

- **Save-folder semantics preserved.** The strip's input maps to the
  existing `folder` kwarg of `MSTORE.save_l2_*`: empty → auto
  `sipm{N}_T{K}/` subfolder; filled → custom subfolder under `data/`.
  The strip's preview text is purely visual — it doesn't change save
  routing.
- **T is always included in the file.** The strip omits a toggle for
  T (matching the v2 HTML — the user wants T as a load-bearing key
  in every file). The path preview always includes a `T<K>` part.
- The "go to SiPM" button stays in the strip next to the locations,
  using `mux_in`/`cx_in`/`cy_in` closures from the same scope.

### Files touched

- `daq/webgui/shell.py` — added `.l2-ctx` CSS scope (~60 lines),
  replaced the SiPM context card + Output card with the strip
  (~200 lines), changed `.l2-layout` to `1fr 1fr`, dropped
  `out_base`. The measurement procedure card and `_build_l2_plots`
  callbacks (IV/Scan/Charge/Waveform browser) are unchanged.

### Open threads

- Strip currently dims fields via opacity only; inputs are still
  interactive when their include-switch is off (their values just
  won't reach the file). Acceptable for now; could disable inputs
  via `bind_enabled_from` if it confuses users.
- The "always" indicator for T was dropped from the strip lbl to
  save space — the path preview makes its always-included status
  obvious. Revisit if users ask.

---

## 2026-05-29 — L2 pulse sweep (vs bias) + sweep plot

### What changed

The L2 pulse-counting panel can now **sweep bias** instead of only
acquiring at a single bias — the GUI form of the bench `ov_scan`.

- **Mode toggle** (`single bias` / `bias sweep`) in the pulse panel.
  `single` shows the existing Bias field; `sweep` swaps it for
  start/stop/step (absolute V, same semantics as the IV sweep).
  Capture ch + self-trig thr + aux trigger + pre/post/N/store are
  shared by both modes (no field duplication).
- **`run_pulse_sweep()`** ([daq/webgui/shell.py](../daq/webgui/shell.py)):
  configures the digitizer once, then at each bias `set_bias` →
  `ctrl.run(N)` → reduce `amplitudes[ch]` to mean/std/count and a
  trigger rate (`n_pulses / elapsed`, wall-clock measured around the
  acquire). Streams per-bias into the plots and logs a per-point line.
- The single ▶ button dispatches on mode and relabels
  (`run pulse` / `run pulse sweep`).

**New 5th plot** in `_build_l2_plots` — full-width, dual y-axis:
mean amplitude (left, ADC) and trigger rate (right, Hz) vs bias, with
the same clickable legend-dot / clear affordances as the other cards.
`psweep_begin()` / `psweep_point(bias, mean_amp, rate_hz)` registered
in `PLOTS`. NaN means (zero-pulse bias) are dropped from the amp trace
but the rate point is kept. Per-bias charge spectrum + (if `store`)
waveforms also refresh live in their existing views.

**New saver** `save_l2_pulse_sweep()`
([daq/measurement_store.py](../daq/measurement_store.py)),
`measurement_type="pulse_sweep"`: summary arrays (bias_v, mean_amp_adc,
std_amp_adc, n_pulses, rate_hz, n_waveforms) as datasets on the sweep
group, plus per-bias `point_NNN/chN/` amplitude+timestamp subgroups
(via `h5io.write_pulse`) so the spectra stay recoverable. Honors the
operator folder/basename + optional-identifier conventions.

### Decisions

- **Toggle in the pulse panel, not a 4th sub-tab** (user choice) —
  reuses the channel/trigger/window config; the panel does double duty.
- **Absolute bias V** (user choice), mirroring the IV sweep — no
  dependency on a cached/valid V_BD. OV is derivable offline.
- **Plot both mean amplitude and rate** (user choice) on one dual-axis
  card rather than two cards — the gain curve and DCR/light-response
  curve share the bias x-axis.
- **Rate = n_pulses / wall-clock elapsed** around `ctrl.run`, not from
  `result.timestamps` — robust even if a result lacks timestamps (they
  are still saved per-bias when present, for offline rate refinement).

### Verification

Headless build of the whole L2 tab + the new callbacks (incl. the
NaN-gap path) and a `save_l2_pulse_sweep` HDF5 round-trip all pass.
Deployed: `systemctl --user restart daq-webapp`, serving clean (no
tracebacks; a client loaded the page). **Not yet exercised on live
instruments** — confirm a real bias sweep on the bench (watch for the
HV interlock prompt if a sweep crosses the 60 V threshold).

### Open threads

- In sweep mode the charge/waveform views flip through each bias as it
  runs and settle on the last one — intended as live feedback, but
  there's no "pick a bias to inspect" control afterward (the data is in
  the HDF5 `point_NNN/` groups; the Data tab can open it).
- `store=on` during a long sweep keeps every bias's waveforms in the
  result objects transiently; fine for typical N, heavy for large N ×
  many biases. No cap enforced.

---

## 2026-05-29 — L2 output: operator-chosen folder + basename

### What changed

The L2 page can now direct where each measurement file goes instead of
always `data/sipm{N}_T{K}K/<unix_ms>.h5`.

- **`daq/measurement_store.py`**: the four L2 savers (`save_l2_iv_sweep`,
  `save_l2_current_measure`, `save_l2_pulse_run`, `save_l2_scan`) gained
  `folder=None, basename=None` kwargs, resolved by a new `_l2_path` helper:
  - `folder` blank -> the existing per-(sipm, T) auto folder; otherwise an
    operator subfolder under `data/` (created if missing).
  - `basename` blank -> the unix-ms stamp; otherwise `<basename>.h5`, with
    the ms stamp appended **only on collision** so a run never silently
    overwrites another.
  - Both inputs are sanitized: `_safe_subdir` rejects anything that escapes
    the data root (`../..`), `_safe_stem` strips directory parts + a
    trailing `.h5` from a typed basename.
- **L2 tab** (`_build_level2_tab`): new **output (optional)** card (CARD 1b)
  with a typeable folder combobox (`ui.select` `with_input` +
  `new_value_mode="add-unique"`, options from `h5browse.list_folders()`), a
  refresh button, a basename input, and a **create folder** button
  (`h5browse.make_folder`, so it appears immediately in the data tab).
  `_out_kwargs()` packs `{folder, basename}` (None when blank) and is
  splatted into all three save calls next to `_opt_kwargs()`.

### Decisions

- **Blank == default, not data-root.** The folder combobox drops the ""
  root entry — "no folder" means the auto per-(sipm, T) dir, which is a
  distinct intent from "write into data/ directly".
- **Collision -> append ms, don't refuse.** A chosen basename is a label,
  not a uniqueness contract; appending the stamp keeps both files. The
  HDF5-internal path still carries the measurement type, so same-basename
  files of different types still h5repack-merge cleanly.
- **Typed-but-not-created folder is fine** — the saver mkdirs it at write
  time; "create folder" is just for making it show up in the data tab
  ahead of the run.

### Verification

- `_l2_path` unit cases: default, anon (no sipm), custom nested folder +
  basename, collision suffix, `.h5`/path-part stripping, and the escape
  guard (`../../etc` rejected).
- Full `save_l2_iv_sweep` against a minimal SweepResult: default path,
  custom `campaignX/wafer3/iv_dark.h5`, and the collision ->
  `iv_dark_<ms>.h5` all land correctly.
- Isolated L2 page build: 200, output card + folder/basename/create-folder
  controls present, no server errors.
- `daq-webapp` restarted, `active`.

### Open threads

- Folder/basename apply per save independently of the sipm/T identifier
  switches — an operator could set a custom folder *and* sipm_id; the
  custom folder wins for location, sipm_id still tags the file attrs.
  Intentional, but worth a sentence in any user-facing doc.
- No live preview of the resolved path before clicking run. Could echo
  "will write: <path>" under the card. Minor.
- Live websocket click-through (type a new folder, run an IV, confirm the
  file lands) still wants a human pass on the running app.

---

## 2026-05-29 — Data tab: file management + click-to-plot

### What changed

Extended the `data` explorer (added earlier today) with the two things the
user asked for:

1. **File management in the browser** — `daq/h5browse.py` gained
   `list_folders` / `make_folder` / `move_file` / `delete_file`, all routed
   through `_safe_under_root` (resolve + reject anything that escapes the
   data root — the rel paths come from the browser). The left file card now
   has a **new-folder** button (header) and per-row **move** / **delete**
   icon buttons; move opens a destination-folder dropdown, delete a confirm
   dialog. Dialogs are built once and reused. If the file being viewed is
   moved/deleted, `_reset_active_if` clears the structure + detail panes so
   we don't dangle on a stale path.
2. **Click a node -> analysis plot** — clicking e.g. `/iv` now renders the
   real IV curve, not just attributes. New `GROUP_PLOTS` map in `h5browse`
   ties each top-level HDF5 group to the applicable `daq.plotting` PLOTS
   keys (iv->iv/iv_leakage, vx2740->mean_waveform/spectrum/waveform,
   vx2740_ov_scan->ov_scan/ov_spectra, etc.). `_domain_plot` shows a
   plot-type select + only the knobs that plot's fn actually accepts
   (introspected via `inspect.signature`), and `context_hints` seeds
   channel / bias_group / dark-light from *where* the user clicked
   (e.g. clicking under `/vx2740/ch3/...` -> channel 3,
   `/k6485/below_vbd/...` -> below_vbd, `vx2740_thresh_scan_dark` -> dark).
   The raw per-dataset value plot is kept below, for datasets.

### Decisions

- **Reuse `daq.plotting`** for the analysis plots rather than re-deriving:
  those fns already accept a file path and locate their own group, so
  dispatch is just "group name -> PLOTS key(s) -> fn(path, ax, **opts)".
  Extra opts are harmless (every fn takes `**opts`).
- **Knob visibility by signature introspection** instead of a hand-kept
  per-plot knob table — self-maintaining as plots are added/changed.
- **Delete is confirm-gated, move is not** — a move is reversible (move it
  back); a delete isn't.

### Verification

- `h5browse`: domain/hints mapping, folder/move/delete round-trip on a
  throwaway `_zztest.h5`, and the path-escape guard (`../../etc/passwd`
  rejected) all pass.
- Every mapped group's plot fn called the way the tab calls it, against a
  real bench file — all 15 (iv, iv_leakage, k6485_bars/ts, mean_waveform,
  spectrum, waveform, ov_scan, ov_spectra, dcr_vs_ov, led_amp_sweep,
  crosstalk_ap, threshold_scan) render with data artists, none raise.
- Isolated page-build render: 200, 14 file rows each with a move button,
  new-folder button present, no server errors.
- `daq-webapp` restarted, came back `active`.

### Open threads

- Still no end-to-end click-through on the live websocket app — backend +
  page-build verified, but a human should click iv/spectrum/move/delete
  once on the running app.
- No multi-select / bulk move. One file at a time; fine for now.
- `_domain_plot` rebuilds a matplotlib figure per node click (same as the
  raw plot). Fine on local disk; revisit if it feels heavy.

---

## 2026-05-29 — Electrometer high-voltage interlock

### What changed

A safety interlock on the B2987B voltage source: any commanded `|V|`
above a threshold (default **60 V**) is denied unless an operator
confirms.

Enforced at the **driver** (`keysight2987b-python/b2987b/driver.py`),
which is the single sink every voltage command funnels through:
- `set_voltage()` and `configure_list_sweep()` both call a new
  `_guard_hv()`. Point-sets *and* hardware list-sweeps are covered.
- `_hv_confirm` callback (`set_hv_confirm()`), `hv_threshold` property,
  `HighVoltageInterlock` exception, `HV_DEFAULT_THRESHOLD = 60.0`.
- **Deny-by-default**: no confirmer → raise. So no unattended path
  (scripts, Claude, forgotten code) can push HV silently.
- **Simulation mode is exempt** (`mode != "hardware"` short-circuits),
  so sim/CI/Claude test runs never trip the prompt.

Controller (`controller.py`) exposes `hv_threshold` + `set_hv_confirm()`
passthroughs so callers never reach into `_driver`.

GUI (`daq/webgui/shell.py`): registered one confirmer at elec-connect
(`_arm_hv_guard`, also armed defensively in the bias handlers /
apply_source). The confirmer bridges the driver's *synchronous* guard —
which runs inside the worker thread of `await asyncio.to_thread(...)` —
back onto the event loop via `run_coroutine_threadsafe` to raise a modal
(`_hv_dialog`), broadcast to connected clients, first answer wins.
Closing the dialog / timeout (120 s) / no client all deny. Added an
"HV interlock threshold" number field (default 60) to the Source block;
edits apply live.

CLI (`scripts/bench_test.py`): `--allow-high-voltage` auto-approves
(logged loud); else prompt on a TTY; non-interactive denies. Note the
default bench IV sweep stops at **55 V**, under the threshold — routine
runs never see the prompt.

### Decisions

- **Driver-level, not GUI-level.** GUI-level couldn't be "written once":
  bench scripts, primitives, measurement/raster, and the bypassing
  Electrometer panel all skip GUI code. The driver is the only universal
  chokepoint. (User explicitly weighed both; chose driver + deny default
  + confirm-every-time.)
- **Threshold lives in the driver (default 60), selectable from the GUI.**

### Open threads

- End-to-end GUI dialog needs a human click to fully verify (logic +
  fail-safe paths unit-tested; webapp imports clean). Not yet restarted
  the live service — do it when the bench is free.
- Multi-client broadcast cancels the *other* clients' pending dialogs but
  leaves them visually open until reload; fine for 1-2 operators.

---

## 2026-05-29 — L2 live-plot column (iv / scan / charge / waveform)

### What changed

The L2 tab in [daq/webgui/shell.py](../daq/webgui/shell.py) gained a
right-hand live-plot column.  The page is now a left/right split: the
existing iv/pulse/scan/sipm control cards on the left, four stacked
echart views on the right that update **as data is recorded**.

Four views (`_build_l2_plots()`, a new module-level builder):
1. **iv** — current vs bias, *dark* + *bright* overlaid.  Streams
   per-point: the K6485 path feeds the existing `progress_cb`; the
   B2987 batch path fills the trace from the returned block.
2. **scan** — current vs stage position, *X* + *Y* overlaid.  Streams
   per-point from the (main-thread) scan loop.
3. **charge** — amplitude histogram (the VX2740 amplitudes are already
   baseline-subtracted, so this is the "amplitude − baseline" spectrum
   the user asked for), *dark* + *bright* overlaid, step-line, bin
   count adjustable.  Drawn once per pulse run.
4. **waveform** — a single stored frame from the capture channel with
   prev / ◀ / ▶ scroll through the acquisition; baseline-subtracts
   using the pre-trigger region and aligns t=0 to the trigger.

Overlay rule (per the user's spec) is **replace same slot, keep the
other**: a new dark IV run overwrites only the dark trace, etc.  Each
view has a clear button that wipes both slots.

### How it's wired

- A `PLOTS` dict + best-effort `_plot(name, *args)` wrapper live in
  `_build_level2_tab`.  The plot column (built *after* the control
  cards) fills `PLOTS` with streaming callbacks; the run handlers
  (defined above it) call them by key, so build order doesn't matter —
  lookups happen at click-time.
- `_plot()` swallows + logs any chart error: a plotting glitch must
  never abort a measurement run.
- **Thread safety:** the IV sweep runs in a worker thread, so its
  `progress_cb` only *buffers* points + sets `_plot_dirty["iv"]`; a
  `ui.timer(0.3)` in the plot column redraws on the UI loop (echart
  can't be touched off-loop).  Scan / charge / waveform callbacks all
  fire on the main thread (after their `await _run_in_thread(...)`
  returns) and redraw directly.

### Decisions

- **Right-side panel** (not a sub-tab) so a plot updates live while the
  operator watches the controls — chosen by the user.
- **Live point-by-point** for IV/scan; charge + waveform are inherently
  post-acquisition (need the full amplitude/waveform arrays).
- **echart, not matplotlib** — matches every other live plot in the
  webapp (digitizer waveform/spectrum, L1 single waveform) and updates
  incrementally without re-rendering a PNG.

### Verification

Headless build test (manual `nicegui.Client`, no request) renders the
whole L2 tab and `_build_l2_plots` without error; all seven callbacks
register and run against simulated IV/scan/pulse data (histogram +
waveform redraw paths included).  **Not yet deployed** — needs
`systemctl --user restart daq-webapp` to pick up the change, then a
real run on the bench to confirm against live instruments.

### Open threads

- IV/scan y-axes are linear; reverse-bias currents span decades, so a
  log-y toggle might help.  Left off for now (echart auto-scales and
  some currents are negative).
- The charge spectrum reads `result.amplitudes` directly (counts).  If
  a future bench uses the RTO2024 (amplitudes already in volts) the
  axis label "ADC" would be wrong — but L2 only drives the VX2740.
- `no-wrap` on the split means very narrow windows scroll horizontally
  rather than stacking; fine for lab monitors, noted in case a laptop
  user complains.

---

## 2026-05-29 — Data tab: HDF5 explorer for all recorded runs

### What changed

New **`data`** tab in the web shell — a browser for every `.h5` under
`./data`, not just the `bench_*.h5` the plots tab already knew about.

- **`daq/h5browse.py`** (new, pure data layer, no NiceGUI deps so it's
  unit-testable on its own):
  - `list_data_files()` — `rglob("*.h5")` so it catches L1/L2 measurements
    in their per-SiPM/per-T subfolders (`sipm{N}_T{K}K/`, `L1/`,
    `T{K}K_anon/`) as well as top-level bench/elec runs. Newest first.
  - `build_tree(path)` — HDF5 hierarchy as a `ui.tree` node list; node
    `id` is the internal HDF5 path so the detail/read helpers re-open by it.
  - `node_detail(path, h5path)` — attrs (numpy scalars/arrays formatted),
    plus for datasets: shape/dtype, a bounded value preview, numeric stats.
    Stats sample is capped (`_sample`, 2e6 elems via a leading axis-0 slice)
    so a 1000x1500 waveform set doesn't get fully materialized for a hover.
  - `read_dataset(path, h5path, row=None)` — full read, or one row of a 2D
    dataset (so we plot a single waveform, not 1.5M points).
- **`_build_data_tab()`** in `shell.py`: left = filterable file list (one
  clickable `.data-file-row` per file, size + mtime); right = `ui.tree` of
  the selected file + a detail card (attribute table, preview, stats, and a
  quick matplotlib plot for 1D / per-row 2D numeric datasets). Download
  button uses `ui.download.file`.
- Registered the tab + a header **`🗂 data`** button next to `📊 plots`
  (the two "look at recorded data" destinations sit together). New
  `.data-file-row` CSS next to the `.daq-card` rules.

### Verification

- `h5browse` exercised directly against real files: tree walk, group
  detail, 1D (`/iv/current_a`) and 2D (`/vx2740/ch0/waveforms`) dataset
  detail, row slicing — all correct.
- Mounted `_build_data_tab` on a throwaway unauthenticated page in a
  separate app instance and HTTP-fetched it: 200, 14 file rows rendered,
  no server errors. (Login is websocket-driven, so this side-channel was
  easier than scripting the auth flow.)
- `ui.tree.expand` / on_select `e.value` / `ui.download.file` signatures
  confirmed against the installed NiceGUI 3.12.1.
- Main `daq-webapp` service restarted, came back `active`. (The `stop`
  side logged the known MJPEG-client SIGKILL-on-timeout; the new process
  started clean.)

### Open threads

- Interactive paths (click file -> tree, click node -> detail/plot) build
  over the websocket and weren't driven end-to-end here — the backend and
  page-build are verified, but a human click-through on the live app is the
  last mile.
- The detail pane re-opens the file per node click. Fine for local disk;
  if `data/` ever moves to a slow mount, consider caching the open handle
  per selected file.
- 2D plot is one row at a time. An overlay (first N rows) or a heatmap
  would be a natural follow-up for waveform inspection.

---

## 2026-05-28 — L2 identifiers go optional; pulse gains aux trigger

### What changed

Per the user's spec, every SiPM-identification field on the L2 page
is now opt-in:

- **`sipm + position (optional)` card**: each of `SiPM id`, `MUX ch`,
  and `Location` is gated by its own switch.  Off ⇒ the field is
  omitted from the measurement file AND the corresponding action is
  skipped at run-time (no MUX `select`, no stage move).  Only `T (K)`
  remains mandatory (it's used in the per-T folder name).
- **`go to sipm` button** acts on whatever's enabled.  If neither MUX
  nor Location are on, it's a no-op with an explanatory log line.
- **`_prep_position()` helper** (used by IV, Pulse, and Scan) now
  conditionally selects the MUX and conditionally moves the stage,
  rather than requiring both.  The MUX can be disconnected when
  unused.

**`MSTORE` save signatures** widened to accept
`sipm_id=None, mux_channel=None, center_x_mm=None, center_y_mm=None`.
Folder convention:
- sipm_id present → `data/sipm{N}_T{K:.1f}K/<ms>.h5`
- sipm_id absent  → `data/T{K:.1f}K_anon/<ms>.h5`

New `_write_optional_attrs(group, **kw)` helper skips any None values
so on-disk attrs only contain what the operator entered.

### Pulse card — aux-trigger channel

New optional `trigger on another ch` switch reveals two extra fields:
`aux ch` and `aux thr (ADC)`.  When enabled, the auxiliary channel is
added to the VX2740's `sipm_channels` + `thresholds` dict, so the
digitizer self-triggers when **either** the capture channel or the
aux channel crosses its per-channel threshold.  Both channels are
read out (the result's `channel_ids` includes both), so analysis can
correlate them.  Saved as `/pulse/<dark|illuminated>/<ms>/` attrs
(`capture_ch`, `capture_thr_adc`, `aux_trigger_ch`,
`aux_trigger_thr_adc`) so the trigger chain is unambiguous downstream.

### Scan card — auto-range from center

Added a small `use ±0.75 cm from center` button.  When the Location
switch is on, it fills `start`/`stop` with `center − 7.5 mm` →
`center + 7.5 mm` along the currently-selected axis.  Per the user's
spec: "If I enter a location, then scan x and scan y should be
centered on that location, running from -0.75 cm to 0.75."  Operator
can still edit start/stop manually for smaller or asymmetric windows.

When Location is off, the scan still runs along the selected axis at
the entered absolute coords; the "other axis" defaults to 0 mm
rather than refusing to run.

### Other touches

- New fields on `ExperimentConfig`: `led_frequency_hz`,
  `led_amplitude_v`, `led_offset_v`, `led_pulse_width`.  These were
  duplicated in `scripts/bench_test.py:DEFAULT_CFG` until now; the
  L2 bright wrappers (`_awg_pulse_on(1, freq, amp, offset, width)`)
  now read them from config.

### Decisions

- **Per-field switches, not "leave the box empty"** — NiceGUI's
  `ui.number` doesn't have clean empty-state semantics, and 0 is a
  meaningful value for center_x / center_y.  An explicit on/off
  switch removes ambiguity.
- **Save-file folder picks `_anon` suffix when sipm_id is absent**
  rather than collapsing all anonymous saves into a single folder
  — keeps per-T separation either way.
- **Aux trigger via `sipm_channels`** (per-channel threshold, both
  channels read out) rather than a separate "trigger only" channel.
  The VX2740 firmware natively does ITLA-OR across enabled channels
  for self-trigger; this is the cleanest mapping.

### Open threads

- L2 scan UX is getting busy.  If a "dark scan" light mode is added
  later, the toggle becomes a three-way segmented control; not a
  problem, just noting.
- `bench_test.py:DEFAULT_CFG` still has its own LED defaults — the
  config.py copies should be the single source of truth.  Small
  follow-up.
- Pulse aux-trigger thresholds use `per_channel`; if both should
  share a value, the operator enters it twice.  Could expose a
  `threshold_mode: global / per_ch` toggle but the UX cost > value.

---

## 2026-05-28 — L2 single-SiPM tab rebuilt: IV / pulse / scan

### What changed

`_build_level2_tab` in [daq/webgui/shell.py](../daq/webgui/shell.py)
fully replaced with a four-card layout matching the user's mental
model of single-SiPM workflow:

1. **sipm + position** — direct user-inputs (no channel-map lookup):
   sipm id (file tag), MUX channel, center (x, y) in mm, T (K) with
   the read-from-slowcontrol button.  "go to sipm" button moves the
   stage + selects MUX without running any measurement (sanity check).
2. **iv sweep** — dark/bright toggle + meter selector (k6485 / b2987)
   + start / stop / step / N-per-V.  Bright wraps the sweep in
   `_awg_pulse_on(ks_ch=1, ...)` / `_awg_off(1)` using the existing
   `config.led_*` defaults.
3. **pulse counting** — dark/bright toggle + bias + VX2740 channel +
   threshold (ADC) + pre/post (µs) + N waveforms + store-raw switch.
   Bright same AWG channel + LED defaults as IV.  Runs directly
   against `HUB.dig._ctrl.run(...)` (bypasses `M.pulse_run` because
   that path goes through the legacy lamp_stage abstraction that
   doesn't apply on this bench).
4. **scan** — axis toggle (X / Y) + bias + start / stop / step / N-per-pt +
   settle + meter selector + light-mode toggle (VUV beam → AWG ch1,
   Laser → AWG ch2) + AWG params (freq, amp, offset, width) that
   default to the existing `led_*` config.  Loop:
     - select MUX channel once
     - turn on AWG with chosen channel + pulse params
     - set bias
     - for each position: move stage (deenergize_after=True) → settle
       → take N samples on chosen meter → next move (auto-energizes)
     - turn AWG off + bias off

All four cards share two helpers defined at the top of the tab body:
- `_awg_pulse_on(ks_ch, freq, amp, offset, width)` — set load=INF,
  apply_pulse, configure_pulse, output_on
- `_awg_off(ks_ch)` — output_off (silent on failure)

The previous `current_measure` card was removed (functionality is
covered by IV with start=stop=bias or by L1's current-samples card).

### Decisions

- **No channel-map lookup.** User supplies sipm id, MUX ch, and
  (cx, cy) directly per measurement.  This matches how the bench
  is actually used right now (no global channel-map CSV in play).
- **"Bright" means AWG pulse, not lamp-stage move.** The old
  `M.iv_sweep(illuminated=True)` etc. assumed a separately-moving
  lamp_stage that doesn't exist on this bench.  Instead, "bright"
  here just enables the AWG pulse on ch1 with the existing
  `config.led_*` defaults before the measurement and disables it
  after.  Result still saves to MSTORE with `illuminated=True`.
- **Scan illumination is two distinct AWG channels** (ch1 = VUV
  beam, ch2 = Laser) with per-mode pulse parameters editable from
  the card.  No way to express "dark scan" in the current card —
  if needed, set amp=0 or add a dark toggle later.
- **Scan motion follows the user's spec exactly**: move → de-energize
  → record → re-energize (implicit via the next move) → next.  Uses
  `P.move_stage(deenergize_after=True)` which leaves the stage
  de-energized at each measurement instant.
- **New `MSTORE.save_l2_scan`** persists positions + means + stds +
  raw samples to `data/sipm{N}_T{K}/<ms>.h5` under
  `/scan/<x|y>/<unix_ms>/` with all relevant attrs (axis, bias,
  meter, light_mode, light_*, center_*, mux_channel, n_per_point,
  settle_s).  Matches the existing per-(sipm, T) folder convention.

### Open threads

- **Dark scan not exposed.** The card always enables the AWG.  If a
  user wants a "dark" position scan (e.g., to map leakage vs
  position), add a third light-mode option `"off"` that skips the
  `_awg_pulse_on` step and saves with light_mode="dark".
- **No live plot of the scan in the page.** Status line shows the
  current point + final summary.  A small matplotlib plot like the
  L1 single-waveform card would be a nice add.
- **`M.iv_sweep` / `M.current_measure` / `M.pulse_run` no longer
  called from L2.** They're still used by L3/L4/L5 (tile sweep,
  temp point, full run).  The lamp_stage assumption is fine there
  — when those bench setups exist, the abstraction makes sense.
- **AWG load is hard-coded "INF".** If the lab adds a 50 Ω target,
  expose the load as a per-scan field.

---

## 2026-05-28 — WFG (33500B) amplitude change rejected by instrument

### What changed

Fixed a bug where changing amplitude (or offset) for the Keysight
33500B from the GUI did nothing and threw an error on the instrument's
front panel.

- Root cause: `KS33500BDriver.apply()` built the `APPLy:<func>` SCPI
  command with a 4th parameter (`phase`) for SIN/SQU/RAMP/PULS. The
  33500-series `APPLy` command accepts at most `freq,amplitude,offset`
  — no phase. The extra value raised **-108 "Parameter not allowed"**
  and the instrument discarded the *whole* command, so amplitude never
  updated. Every GUI Apply hits this path (`apply_sine` etc.).
- Fix (in submodule `keysight33500b-python`): `APPLy` now sends only
  freq/amp/offset; phase is written separately via `:SOURce<n>:PHASe`.
- Verified against the vendored user's guide
  (`keysight33500b-python/9018-03290.pdf`, Agilent 33500 Series User's
  Guide, converted with `pdftotext -layout`). The guide repeatedly
  documents APPLy as setting "function, frequency, amplitude, and
  offset" — never phase — and confirms `VOLTage {<amplitude>}` is the
  amplitude command (matches `set_amplitude`). Cross-checked every
  SCPI command the module emits against the guide; all names check out
  (DATA:VOLatile:CATalog is the only one not in this guide — it's a
  query covered by the separate Programmer's Reference).
- **Refinement after reading the guide:** the guide lists a phase
  reference only for sine/square/ramp/arb ("0 degrees is the point at
  which the waveform crosses zero..."). Pulse/noise/DC have none. So
  the separate `:PHASe` write is restricted to SIN/SQU/RAMP/ARB —
  otherwise pulse would have traded the old -108 for a settings error.
- Commits: submodule `4fae808` (drop phase from APPLy) + `95abfe4`
  (restrict :PHASe); parent pointer bumps `4ee7540` + `e16804a`.
  Webapp restarted.

### Lessons / tribal knowledge

- The 33500 `APPLy:<func>` is freq/amp/offset only. Phase, duty,
  symmetry are all separate commands. Don't append phase to APPLy.
- The continuous-phase `:PHASe` command only applies to sine/square/
  ramp/arb. Pulse/noise/DC have no phase reference.
- The 33500 user's guide is vendored at
  `keysight33500b-python/9018-03290.pdf`; `pdftotext -layout` gives a
  readable dump. It's the *User's Guide*, not the *Programmer's
  Reference* — exact SCPI bracket syntax (memory/DATA commands etc.)
  lives in the latter, which is not vendored.

### Open threads

- ~~Confirm fix on real hardware~~ — CONFIRMED working on the bench
  (Lucas, 2026-05-29): amplitude changes apply with no front-panel
  error.
- Submodule changes were committed on the submodule's `main`; not
  pushed to its remote. Push when convenient so the pointer bumps
  resolve for other clones.

---

## 2026-05-28 — Per-measurement HDF5 persistence (L1 + L2 tabs)

### What changed

Every measurement clicked from the webapp's L1 or L2 tab now writes
its own HDF5 file. New module `daq/measurement_store.py` is the only
place that writes these files; both tabs call it via thin wrappers in
the click handlers.

- **File-per-click**, named `<unix_time_ms>.h5` (millisecond precision
  to avoid collisions when clicking quickly).
- **L2 layout** (`data/sipm{N}_T{K:.1f}K/<ms>.h5`):
  - top-level attrs: `sipm_id`, `temperature_K`, `run_start_utc`,
    `measurement_type`, `illuminated`, `schema_version=1`
  - measurement payload at `/<type>/<dark|illuminated>/<unix_ms>/`,
    with type one of `iv`, `current_measure`, `pulse`. The ms-named
    leaf group is the merge-key: if L2 files for the same (sipm, T)
    are concatenated later (h5repack or a script), all the payloads
    live at unique paths and don't collide.
- **L1 layout** (`data/L1/<ms>.h5`): flat — `measurement_type` attr +
  datasets at root, no sipm/T/illuminated wrapping (L1 has no such
  context). Currently saved: VX2740 single-waveform captures and the
  K6485/B2987 N-sample current sweeps. Single-shot `read I` /
  `read flux` / `read T` buttons are NOT saved (they are dashboard
  pokes, not measurements).
- **L2 SiPM-selection card** got a temperature widget: a `T (K)`
  number input and a "read T" button that pulls from slowcontrol; a
  small "manual / slowcontrol" label tracks the value's provenance.
  Manual edits flip the label back to "manual". The value at click
  time goes into the file's attrs and the folder name.

Three L2 click handlers and two L1 click handlers wrapped their
post-measurement block in `try: MSTORE.save_*(...) except ...:
log SAVE FAIL`. A save failure is reported but does not propagate
— the measurement itself is still considered complete.

### Why this shape

User answered three focused questions:
- **File scope:** "one file per click". So no per-(sipm, T)
  session-file logic — every click writes its own file. The folder
  groups by (sipm, T) and the inside-the-file path uses
  `/<type>/<dark|illuminated>/<unix_ms>/` so merging files later
  with h5repack yields a "single file with all data for one (sipm, T)"
  by construction.
- **L1 vs L2:** "L1 saves to data/L1/<unix_ms>.h5" with flat layout —
  L1 primitives have no SiPM context, so they get a separate
  unstructured file format.
- **Single-shot reads:** the L1 spot-check buttons (`read I`,
  `read flux`, `read T`) intentionally don't save. They're
  diagnostic pokes, not measurements. Easy to flip if the user
  wants them saved.

### Decisions

- **Writer is its own module** (`daq/measurement_store.py`),
  separate from the existing `daq/storage.py`. `storage.py` is the
  Level-3+ run-file writer (one file per *run*, owned by
  `RunFile`); the new writer is one-shot, owned by the click
  handler. Mixing the two would conflate "long-running run file
  context" with "one-shot per-click file" and force the L1/L2
  tabs to carry RunFile lifecycles they don't need.
- **L1 flat layout has the same dataset *names*** as the L2
  hierarchical layout (`current_a`, `timestamp_s`, `waveforms/ch{N}`,
  etc.) so analysis code can be shared with minor path differences.
- **ms-precision filename** rather than seconds. The user's wording
  was "unix time", and `int(time.time()*1000)` still reads as a
  unix time (just ×1000). Avoids the file-collision edge case
  without needing a "_1, _2" suffix mechanism.
- **Save failure does not fail the measurement.** Two `try:` blocks
  in each handler: one around the measurement, one around the save.
  This way a wedged disk or full filesystem doesn't lose the
  in-memory result the user is staring at on the page.
- **No automatic merge tool yet.** If the user wants a single
  per-(sipm, T) file, they run h5repack or a small script. Keeping
  the writer dumb keeps the contract simple.

### Open threads

- The L2 tab intro line no longer says "results not saved". The L3
  intro still talks about Level-3 HDF5 — that wording is now
  ambiguous since L2 also writes HDF5 (different schema, different
  scope). Worth a re-word.
- The L2 SiPM-selection card asks the user to manually read T on
  every measurement they care about. Could optionally auto-read T
  at click time when slowcontrol is connected (with the manual
  value as a fallback). Current implementation is "snapshot
  whatever's in the number box" — explicit but a bit clunky.
- L1 single-shot buttons (`read I`, `read flux`, `read T`)
  currently don't save. If the user later decides they want a
  full data audit trail, wire them through
  `MSTORE.save_l1_current_samples` with N=1 or add a new
  `save_l1_scalar(value, instrument, kind, ...)` helper.
- Output dir is hard-coded to `data` (relative to cwd). Should
  honor a config knob (`config.output_dir`) eventually, but the
  current default matches `scripts/bench_test.py` and the
  systemd service's `WorkingDirectory`, so no immediate breakage.
- The Qt desktop L2 tab (`daq/gui/level2_tab.py`) was not updated;
  it was already broken before today (wrong SweepResult attrs).
  Same for any Qt tabs that might write to disk later.

---

## 2026-05-28 — Level 2 meter selection; flux_reading → current_measure

### What changed

`daq/measurement.py` (Level 2) restructured so the picoammeter is a
first-class current-meter option, not just a side instrument:

- `iv_sweep(...)` gains `meter: str = "b2987" | "k6485"`. B2987B is always
  the bias source; the meter argument picks which instrument reads
  current. `meter="b2987"` uses the electrometer's instrument-side list
  sweep (existing path, fast); `meter="k6485"` dispatches to a new
  Level 1 primitive that steps bias on the B2987 and reads N samples on
  the picoammeter per voltage (slow, but gives the actual SiPM IV on
  this bench since the B2987 ammeter is wired to the photodiode).
- `flux_reading(instruments, config) -> float` renamed to
  `current_measure(sipm_id, instruments, config, meter=..., illuminated,
  n_samples, delay_s) -> SweepResult`. Same setup as `iv_sweep` (move
  stage to SiPM, select MUX channel, position lamp) but bias remains
  off. Returns a single-point `SweepResult` (mean ± stderr in
  `avg_current_a` / `err_current_a`). Replaces the old
  "move-to-photodiode and average K6485" flow — that was a leftover
  from the XUV-photodiode flux-monitor design that no longer matches
  the current bench wiring.

New Level 1 primitive: `daq/primitives.py:iv_sweep_external_meter(elec,
meter, voltages, n_per_voltage, delay_s, first_point_settle_s)`. Lifts
the manual `_sweep_pass` pattern out of `scripts/bench_test.py` —
includes the V_BD-discharge-transient guard (extra settle + discard one
sample on the first point) that was learned the hard way last quarter.

Tile caller (`daq/tile.py:_do_flux_check`) updated to call
`M.current_measure(last_sipm_id, ..., meter="k6485")` and extract the
float from `result.avg_current_a[0]`. Tile-level HDF5 layout under
`/flux/` and the `flux_check_interval` config knob were left unchanged
— renaming those is a wider refactor.

### Why this shape

Asked the user to pick `current_measure`'s shape; they chose "N samples,
no sweep, returns single-point SweepResult" and the meter arg as
`"b2987" | "k6485"` (model names, not bundle keys like `"elec"`).
Symmetry with `iv_sweep` was the goal — same sipm_id, illuminated,
n/delay defaults from `config.iv_n_per_point` / `config.iv_delay_s`.

### Decisions

- **`meter` uses model names, instrument bundle stays keyed `"elec"`.**
  Internal `_check_meter` maps the public arg to the right hub key.
  Model names read cleanly at the call site; bundle keys are an
  implementation detail.
- **`P.iv_sweep_external_meter` lives in primitives** even though it
  touches two instruments. Justified: it's a single physical operation
  ("source on A, measure on B") with no config knowledge — same shape
  as `P.iv_sweep`. Putting it at Level 2 would break the "REPL-callable
  with raw instrument objects" property of Level 1.
- **`bias_off` at the start of `current_measure`** is explicit rather
  than assumed. On the simulator the readback doesn't actually go to
  zero (sim carries last setpoint), but on hardware the B2987 source
  enable goes low — either way, the contract is observable.
- **Old `read_flux` primitive in `daq/primitives.py` left as-is.** It's
  exported from `daq/__init__.py`, used elsewhere, and the user's
  rename was scoped to the Level 2 `flux_reading`. Per the K6485-naming
  feedback memory, broader symbol renames need an explicit ask.

### Open threads

- `daq/gui/level2_tab.py:_show` (Level 2 GUI tab) reads
  `result.voltages` / `result.currents` / `result.current_errs` —
  attributes that don't exist on `SweepResult` (it uses
  `avg_source_v` / `avg_current_a` / `err_current_a`). This was already
  broken before today; the meter change didn't touch it. The tab will
  raise when it tries to render.
- The Level 2 GUI tab doesn't expose the new `meter` argument. If we
  want users to drive the picoammeter IV path from the GUI, add a
  radio/dropdown.
- `iv_sweep_external_meter`'s `first_point_settle_s` default (0.5 s) is
  generous but shorter than the 2.0 s `bench_test.py` uses after a
  coarse/fine V_BD pass. The bench script's larger setting is for the
  worst case (jumping down from avalanche current to below V_BD).
  Single-pass callers don't need it. If a future caller stacks Level 2
  sweeps the way `bench_test.py` does, expose a knob.
- `tile.py` `flux_interval` and HDF5 `/flux/` group are still
  flux-named. They now feed off `current_measure` results. A future
  pass should rename `flux_interval → current_check_interval` and the
  HDF5 group, but that touches `resume.py` and `storage.py`.

---

## 2026-05-28 — Keysight 33500B submodule + waveform preview

### What changed

- **New git submodule** `keysight33500b-python` (Brunner-neutrino-lab
  upstream) added under `keysight33500b-python/`. The repo shipped with a
  PyQt5 GUI; replaced `ks33500b/gui.py` with a NiceGUI panel that mirrors
  the Rigol DG1022 layout (connection / ch1 / ch2 / burst / sweep /
  arbitrary). Same precedent as when the Rigol GUI was first converted.
- **Pulse parameters are 33500B-native**: period / width / rise / fall
  (seconds), with separate expansions for square-duty and ramp-symmetry.
  Rigol stays period + width only because that's what its
  `configure_pulse` takes.
- **Live waveform preview** on every channel card in **both** panels.
  Pure-Python `generate_preview(fn, freq, amp, offset, ...)` in
  `dg1022/gui.py` and `ks33500b/gui.py`. The preview matplotlib plot
  regenerates on every parameter change so the operator sees the shape
  they're about to send before pressing apply. X-axis auto-scales between
  ns / µs / ms / s based on the total preview duration.
- **Rigol DG1022 hidden from the visible WFG slot**, Keysight 33500B
  promoted. Implemented via a `"hidden": True` flag on the Rigol's
  `_INSTRUMENT_SPECS` entry plus a new `_visible_specs()` helper that
  every header / connect-all loop now goes through
  ([daq/webgui/shell.py](../daq/webgui/shell.py)). The Rigol's
  Connections-tab card still renders (the user wanted to keep manual
  access), and a "wfg (dg1022, hidden)" link in the Settings menu opens
  its panel.
- **`HUB.ks33500b`** added alongside `HUB.wfg` in `daq/gui/hub.py`.
  Bench scripts using `HUB.wfg` (the DG1022 driving the LED) keep
  working unchanged.

### Decisions

- **Keep `HUB.wfg` pointing at the DG1022, not the Keysight.** All
  the bench-test code references `HUB.wfg` to drive the LED. Renaming
  the field would have rippled into every bench step. Adding a separate
  `HUB.ks33500b` is the lower-risk move.
- **`hidden: True` flag, not a separate `_HIDDEN_SPECS` list.** The
  full instrument list stays in one place; visibility is one attribute
  per spec.
- **Connections tab still renders cards for hidden specs** so the
  hidden instrument is reachable manually. The header + connect-all
  skip them. That's what "I dont want it deleted because I may want it
  back" maps to most cleanly.
- **Replaced upstream `ks33500b/gui.py`** rather than adding a parallel
  `gui_nicegui.py`. Same precedent as the Rigol's conversion; cleaner
  imports.
- **Preview is pure NumPy, not a controller round-trip.** Hits no
  instrument; updates instantly. It doesn't model rise/fall edges
  (they'd be sub-pixel on the typical preview scale) — square / pulse
  show ideal edges.

### Open threads

- ~~`daq/config.py:ks33500b_visa` is a placeholder.~~ Updated to
  `TCPIP0::172.16.0.46::5025::SOCKET` — confirmed against the real
  instrument (Agilent 33510B, S/N MY57200344). SOCKET, not VXI-11,
  for the same reason as the B2987: stateless on the instrument
  side, no session-leak risk on abnormal exit.
- Submodule has uncommitted local changes (NiceGUI gui.py replacing
  PyQt5 gui.py). Commit upstream when stable.
- `_visible_specs()` covers most loops but is deliberately bypassed at
  `_quick_connect` (explicit-key lookup) and at the Connections-tab
  card-render loop (Rigol still reachable from there). Worth keeping
  this exception list in mind if a future feature adds another loop.

---

## 2026-05-28 — webapp wedge during connect-all: guarded post-loop notifies

### What happened

User reported the webapp was laggy. Probe pattern matched the 2026-05-27
recovery entry exactly: `systemctl is-active` → `active`, but HTTP `/`
timed out at 5 s, listener had multiple connections with `Recv-Q` of
hundreds-to-low-thousands and no owning PID (half-accepted, never
serviced). Log showed `RuntimeError: The parent element this slot
belongs to has been deleted.` in `_do_connect_all_header`
([shell.py](../daq/webgui/shell.py), search for `_do_connect_all_header`)
at the trailing `ui.notify(msg, ...)`. Also a fresh `AssertionError:
user storage for ... should be created before accessing it` at
`index()` reading `app.storage.user` — same root family.

User narrowed the trigger to: **click "connect all" → mux is slow to
probe → browser reloads or navigates during the wait → post-loop
`ui.notify` runs without a live client → exception → NiceGUI's own
handler re-enters `context.client` and re-raises → event loop wedged**.

### What changed

Added a `try / except RuntimeError: pass` guard around the post-loop
`ui.notify(...)` at three sites in [daq/webgui/shell.py](../daq/webgui/shell.py):

- `do_connect_all` (Connections tab, "connect all" button)
- `do_release_all` (Connections tab, "release all" button — same pattern)
- `_do_connect_all_header` (header "⚡ connect all" button)

Also reordered each so `log.info(msg)` runs **before** the notify (and
added a missing `log.info` to the header version), so the result still
hits the journal even if the toast can never render.

### Decisions

- **Inline `try/except` over a `_safe_notify` helper.** Three sites,
  one-liner each — abstraction wasn't warranted (per CLAUDE.md's
  no-premature-abstraction rule).
- **Only guarded post-loop notifies, not the "starting…" toasts at the
  top of each handler.** Those run synchronously off the click event,
  before any `await`, so the client is guaranteed to still exist.
- **Did not touch the underlying NiceGUI bug** (where
  `app.handle_exception` itself re-enters `context.client`). It's
  library code, and the guard prevents us from ever feeding it the
  exception that triggers the re-entry.
- **Did not change mux probe timing.** "Mux is slow to connect" is
  expected (serial autodetect on the CP2102N); the wedge is what
  needed fixing, not the latency.

### Open threads

- **Other long async callbacks may have the same shape.** A quick grep
  shows ~66 `ui.notify` call sites in `shell.py`. Most are immediately
  after a short `await _run_in_thread(...)` for a single instrument
  command, which should be safe enough (sub-second). But the per-tab
  "connect" buttons (electrometer 2767, mux 3320, k6485 3617) all
  follow the same `await _quick_connect → ui.notify` pattern — if
  any of those reproduces the wedge, give them the same guard.
- **`/webcam.mjpeg` and `/webcam.jpg` return 404 instead of the
  expected 401.** The module is imported at startup
  ([webapp.py:35](../daq/webapp.py#L35)) and `register_routes()` runs
  at import ([webcam.py:207](../daq/webgui/webcam.py#L207)), so the
  routes should be there. Not investigated yet. Side observation only;
  unrelated to the wedge.

---

## 2026-05-28 — password gate on the webapp

### What changed

Shared-password login on the NiceGUI webapp. Visiting `/` without a
session redirects to `/login`; the login form asks for a display name
and a password and writes both into `app.storage.user`. Files touched:

- `daq/webgui/shell.py` — added `_PASSWORD` constant
  (env override: `DAQ_PASSWORD`), `_is_authenticated()` helper, and
  an `@ui.page("/login")` route rendering a centred card. Modified the
  existing `@ui.page("/")` route to early-return `ui.navigate.to("/login")`
  when the session isn't authenticated.
- `daq/webgui/webcam.py` — wrapped the `/webcam.mjpeg` and `/webcam.jpg`
  handlers with an `_authed()` check returning 401 if not logged in.
- `daq/webapp.py` — gated `/labbook-paste` with an `HTTPException(401)`
  guard. Also added a top-level `from daq.webgui import webcam as _webcam`
  so the webcam routes are registered at server startup regardless of
  whether anyone has visited the webcam tab yet.

### Why the extra import was needed

`webcam.py` registers its FastAPI routes at module import time (via
`register_routes()` at the bottom of the file). Until today, the only
import path that pulled it in was `_build_webcam_tab()` inside
`shell.py`, which runs when an authenticated user has the index page
rendered. With the auth gate, an unauthenticated visitor never reaches
that build path → `webcam.py` never imported → `/webcam.jpg` returns
404 instead of 401. Manifested as: log in works fine in the browser,
but the unauthenticated 401 verification probe fails open. Fix is the
explicit top-level `from daq.webgui import webcam as _webcam` in
`webapp.py`. Lesson: don't let auth state affect which FastAPI routes
exist — routes should always be registered; per-route handlers do the
auth check.

### Decisions

- **Single shared password, not per-user accounts.** This is a
  lab-internal app on a trusted subnet. The casual gate is to prevent
  accidental clicks from someone who got the URL, not real attackers.
  If the app is ever exposed outside the lab the right move is to
  swap this for SSO/OAuth, not bolt on per-user creds here.
- **Display name still required.** The header's "who's connected"
  pill is load-bearing for coordination in a multi-operator lab; the
  login form sets `display_name` alongside `authenticated`.
- **Auth state lives in `app.storage.user`** (cookie-keyed,
  server-side dict). Already configured via `storage_secret` in
  `webapp.py`. No new dependency.
- **Storage cookie expires after 14 days** (NiceGUI default for
  `app.storage.user`). Browsers re-prompt on a fresh session.

### Open threads

- No logout button. To force a re-login, clear the `session` cookie in
  the browser or restart the webapp (which clears all sessions).
- Curl-based health probes that GET `/` will keep registering
  anonymous sessions until they fall off via NiceGUI's reconnect
  timeout. For probes use `/login` (cheap public page) or a HEAD
  request on `/webcam.jpg` (returns 401 quickly).

---

## 2026-05-27 (recovery) — webapp wedged on NiceGUI slot-deleted error

### What happened

User reported "connection issues" to the webapp despite the network being
fine. Diagnosis:

- `systemctl --user status daq-webapp` → `active (running)`
- `ss -tlnp | grep 8765` → listener present, **`Recv-Q: 1`** (connections
  arriving but not being accepted at the application layer)
- `curl 127.0.0.1:8765` → timeout at 5 s, `ttfb=0`
- Log: stack trace ending in
  `RuntimeError: The parent element this slot belongs to has been deleted.`
  inside `nicegui/slot.py:parent`.

The worker hadn't crashed but its event loop was stuck — accepted TCP
connections never got HTTP responses. Browser kept reconnecting (visible
as one `session connect` line per ~minute, same client IP).

### Recovery

`pkill -f daq\.webapp` + `systemctl --user reset-failed daq-webapp` +
`systemctl --user restart daq-webapp`. Back to HTTP 200 in ~0.5 s,
webcam grabber re-started cleanly, browser auto-reconnected.

### Open thread

- The "parent element deleted" error is a NiceGUI footgun: a callback
  or background coroutine tries to update a UI element after its parent
  (usually a page) has been garbage-collected. The trigger today wasn't
  identified — most likely a matplotlib `update()` from one of the
  recent async callbacks (single-waveform card, plot library) running
  after a browser tab reload. If this recurs:
  - Wrap `.update()` calls in a `try/except RuntimeError: pass` guard,
    OR
  - Check `element.client.has_socket_connection` before updating, OR
  - Replace the long-lived matplotlib axes with lazily-created ones
    on each render.
- Pattern to recognise: webapp appears "active" in systemd but HTTP
  hangs with zero response. Restart, then look for the slot-deleted
  trace in the log.

---

## 2026-05-27 (even later) — L1 stage: jog buttons + move program

### What changed

Two additions inside the L1 tab in [daq/webgui/shell.py](../daq/webgui/shell.py):

- **Jog block** in the existing stage card: a step-size input (default
  1 mm) plus four buttons (`− X`, `+ X`, `− Y`, `+ Y`). Each click calls
  `stage.move_by(dx, dy)` with the signed step. After the jog the
  absolute-move inputs (`x`, `y`) update to the new position so
  follow-up "move" clicks aren't a surprise jump.
- **Stage move program** as a new card next to the stage card. Build a
  list of move steps one at a time:
  - Inputs per row: `x`, `y`, an `X` toggle, a `Y` toggle, a `settle`
    time, and a single global "de-energize after each step" toggle.
  - If `X` is off, the step's `x` is stored as `None`; same for `Y`.
    That flows straight to `primitives.move_stage(stage, x_mm=None, ...)`
    which already supports per-axis skipping at the controller level.
  - List shows enumerated `x=±0.000 · y=— · settle 0.05s · de-en`
    style rows. Buttons: `add step`, `remove last`, `clear`, `▶ run all`.
  - During run, the status label and the operator log narrate each step.

### Decisions

- **Use `phidget_stage.StageController.move_by` directly for jog** rather
  than going through `primitives.move_stage` (which is absolute only).
  The driver enforces limit switches in either direction, so there's
  no software guard to add — the hardware stops motion when a switch
  trips.
- **List is in-memory only.** No persistence across page reloads — these
  are throwaway ad-hoc sequences for alignment / inspection. If someone
  wants saved programs, that's a follow-up: serialise to a small JSON in
  `data/move_programs/`.
- **Per-step axis-include uses two toggles, not a tri-state select.**
  Mapping to `None` vs concrete value at the API boundary is exactly what
  the user described and matches how `move_to` is parameterised. A
  "both / X-only / Y-only" select would have been the same information
  through one widget; two toggles are more direct.
- **Python 3.11 nested-f-string footgun:** the initial cut had
  `f"...{f'{step[\"x\"]:.3f}'}..."` — illegal in 3.11 (PEP 701 only
  landed in 3.12). Flattened to two lookups + a plain outer f-string.

### Open threads (next session)

- Add a "save program" / "load program" pair so useful sequences
  (e.g., the SiPM grid for alignment) survive across reloads. Small
  JSON file under `data/move_programs/<name>.json`.
- The jog block lives in the stage card; the move-program is a separate
  card next to it. With the prior L1 additions (vx2740 single-waveform,
  current-samples) the L1 tab now has 7 cards in one flex row — at some
  point this wants either a grid layout or splitting into sub-tabs.

---

## 2026-05-27 (later) — L1 primitives: single-waveform + current-samples

### What changed

Added two new cards to the **Manual (L1)** tab in [daq/webgui/shell.py](../daq/webgui/shell.py):

- **VX2740 single waveform** — pick any channel 0–63, set a self-trigger
  threshold (ADC counts), pre/post window (µs), and timeout. Single-shot
  capture via `ctrl.run(n_waveforms=1, store_waveforms=True)`. Renders the
  baseline-subtracted trace inline as a small dark matplotlib panel, marks
  the trigger time (t=0) and the threshold level, and logs peak/baseline.
- **Current samples** — N samples from either the K6485 or the B2987.
  For K6485, a range selector exposes AUTO + 2 nA … 20 mA (mapped to the
  driver's float A or `"AUTO"`). B2987 inherits whatever range is already
  configured (the controller has no high-level range API; range selector
  hides itself when B2987 is chosen). Reports μ ± σ.

### Decisions

- **Self-trigger, not software trigger, for the L1 waveform card.** The
  user specified "threshold" as a knob, which only matters with self-trigger.
  Software trigger would ignore it and capture noise.
- **The L1 capture reconfigures the controller** (channel + window + trigger
  mode). It does not save state — anything the Digitizer tab had configured
  needs a fresh "apply config" afterwards. Documented inline in the card
  comments; not silently restoring because the user usually *wants* the L1
  state to persist for follow-up captures.
- **PMT channel (ch 4) routed via `include_pmt=True`** rather than
  `sipm_channels=[4]`. The VX2740 controller still has the legacy
  PMT-vs-SiPM distinction in its API — channel 4 only ends up in the
  read-out set if include_pmt is true.
- **`np` imported inside each async function**, not at module top, matching
  the existing pattern elsewhere in `shell.py`. Module is huge; lazy imports
  keep startup time bounded if something fails.

### Footgun encountered

Restarting the systemd service didn't work the first time — a stale
python process (PID 16843) from an earlier SIGKILL'd shutdown was still
holding port 8765. `systemctl --user restart` doesn't kill orphans
that escaped the unit's cgroup. Workaround: `kill <pid>` then
`systemctl --user reset-failed daq-webapp && systemctl --user restart`.
This is a manifestation of the existing "uvicorn doesn't cancel MJPEG
streams on shutdown" issue noted in the prior session — the webcam
grabber thread keeps the worker alive past systemd's grace, systemd
KILLs the parent, the worker (orphaned) keeps running and holds the
port. Still on the open-threads list.

---

## 2026-05-27 — webapp service, webcam, 64-channel digitizer

### What changed

**Web app is now a real service** (`~/.config/systemd/user/daq-webapp.service`).
Lingering enabled via `loginctl enable-linger ets` so it survives logout
and starts at boot. Common commands: `systemctl --user {status,restart,stop}
daq-webapp`, `journalctl --user -u daq-webapp -f`. Default port 8765,
binds `0.0.0.0`.

**Webcam tab added.** Logitech C525 on `/dev/video0`. New
`daq/webgui/webcam.py` runs a single background frame-grabber thread shared
across all browser viewers; exposes `/webcam.mjpeg` (multipart stream) and
`/webcam.jpg` (snapshot). Permissions fixed by adding the `ets` user to the
`video` group + a per-device ACL (`setfacl -m u:ets:rw /dev/video0`). Both
took sudo. 1280×720 @ 15 fps target, JPEG quality 80.

**MUX serial port identified and config defaults updated.** A Silicon Labs
CP2102N USB-UART showed up as `/dev/ttyUSB1` (Prolific is `ttyUSB0` for the
K6485). Replaced the Windows-era `COM6` default in `daq/config.py` and
`.last_connections.json` with the by-id symlink
`/dev/serial/by-id/usb-Silicon_Labs_CP2102N_..._ec8db4c9..._-if00-port0` —
stable across replug and reboot (embeds the chip's hardware serial).

**Release-all-instruments button.** Renamed `disconnect all` →
`⏏ release all instruments` in the Connections tab, with better
notify messaging that lists which instruments were released. Reason:
every instrument allows one concurrent session, so a bench script needs
the webapp to let go first.

**VX2740 GUI exposes all 64 channels.** Previous GUI capped at 5
(ch 0–3 SiPM + ch 4 PMT) but the controller / driver always supported
arbitrary indices. New layout:
- 8×8 grid of enable checkboxes
- Quick-action row: `all`, `none`, `invert`, range parser (`"0,4,8-15"`)
- Per-channel threshold inputs in a separate scrollable strip, visible
  only for currently-enabled channels
- Defaults preserve the old "0–4 on" behaviour
- Waveform / spectrum tabs widened from 4 choices to 64

**Webapp shell: P0 + P5 from prior UI review landed.**
- P0: persistent red `⛔ BIAS OFF` button in the sticky header
  (`_emergency_bias_off` in `daq/webgui/shell.py`).
- P5: config tab fields use `bind_value(HUB.config, attr, forward=int|float)`
  for live two-way sync. Removed the "apply to hub" footgun; only the
  comma-separated temperature lists still need an explicit Apply.

### Decisions

- **B2987 transport: switch from VXI-11 (`::INSTR`) to raw SOCKET
  (`TCPIP::host::5025::SOCKET`).** *Why:* VXI-11 sessions leak slots
  on every abnormal exit. After ~5 leaks the instrument refuses new
  connections and needs a power-cycle. SOCKET is stateless from the
  instrument's perspective — no session table — so this class of bug is
  gone permanently. Caveat: the B2987's built-in `sweep()` response
  parser is parked over SOCKET, but the bench script doesn't use it
  (we use K6485 for the SiPM IV; the B2987 just sources voltage).
  Touched: `keysight2987b-python/b2987b/driver.py` (SOCKET termination
  + skip `device_clear`), `scripts/bench_test.py` (default
  `b2987_visa`).
- **Coarse-then-fine IV sweep, with explicit settle between passes.**
  *Why:* uniform 0.5 V step took 263 s (B2987 set_bias dominates per-point
  cost). Coarse 2 V step (~60 s) followed by fine 0.1 V step in
  V_BD±2 V window (~55 s) gives the same V_BD precision in ~half the
  time. Critical detail: jumping from end-of-coarse (~54 V, well above
  V_BD, drawing µA) straight down to start-of-fine (~51 V, below V_BD)
  produces a discharge transient the K6485 reads as a huge spurious
  current at the first fine point. Fix: pre-settle 2 s at `fine_lo`
  and discard one K6485 sample.
- **Photodiode read in IV is now off by default.** `iv_measure_photodiode
  = False` in `DEFAULT_CFG`. The photodiode is unbiased on this bench
  so its current is constant and uninformative; reading it doubled the
  per-point time.

### New physics tests (all wired into `--only`)

- `ov_scan_clean` — LED-off OV scan with full waveforms stored. Gives a
  *clean* SPE peak (the LED-on `ov_scan` saturates above OV+3 because
  the Cremat shaper clips). Plots: `ov_scan_clean` (spectrum family),
  `ov_scan_clean_gain` (mean amplitude vs OV with linear fit; the fit
  excludes saturated points automatically).
- `dcr_vs_ov` — DCR vs over-voltage at fixed threshold (200 ADC).
  Classic SiPM characterization.
- `crosstalk` — long-window (50 µs post) capture at OV+3, LED off, then
  offline `scipy.signal.find_peaks` per waveform extracts secondary-pulse
  delays and amplitudes. Per-waveform peak count → cross-talk fraction;
  Δt distribution → afterpulse time spectrum.
- `led_width` — sweep the DG1022 pulse width at fixed amplitude. Maps
  LED + shaper time response.
- `vx_noise_floor` — bias OFF, LED OFF, sweep the VX2740 self-trigger
  threshold. Establishes the digitizer's own false-trigger floor.
- `k6485_noise_floor` — at zero bias, read K6485 at AUTO + 2 nA + 20 nA
  + 200 nA ranges. Calibrates the lowest detectable current.

### Bench harness CLI flags

`--skip-iv` (uses cached V_BD from `data/last_vbd.json`), `--vbd VAL`
(override), `--only KEYS` (comma-separated subset), `--no-plot`. The
V_BD cache is written by every successful IV; iteration loops can now
run in ~30 s instead of ~4 min.

### Open threads (next session)

- `daq-webapp.service` SIGKILLs on stop after 20 s grace when an MJPEG
  client is still connected — uvicorn doesn't cancel the streaming
  response. Need `app.on_shutdown` hook that stops the webcam grabber
  thread and closes active streams.
- `daq/webapp.py` shutdown handler raises `'NoneType' object has no
  attribute 'reset_input_buffer'` when MUX was never connected. One-line
  guard needed.
- `scripts/bench_test.py` is ~1700 lines. Plausible split:
  `daq/sweeps.py` (the `test_*` functions), `daq/vbd_cache.py` (cache
  helpers), `scripts/bench_test.py` (thin CLI + dispatch).
- No tests at all. `mode="simulation"` exists on every instrument
  module — could exercise the dispatch end-to-end without hardware.

### Verified results

- V_BD = 52.25 V, room temp, reproducible across runs after the IV fix.
- SPE peak at OV+3: 600–900 ADC (visible in the `threshold_scan` dark
  curve as the rate-drop knee).
- DCR at SPE-cut threshold: ~400 Hz at OV+3.
- Gain slope (clean OV scan, OV+1..+3): ~270 ADC/V.
- Crosstalk: ~1–2 % at OV+3 from the long-window analysis.

---

## 2026-05-26 — bench harness + plot library + first physics

(Reconstructed from `docs/continuation_log_2026-05-26.md` and from
references to "the previous session" in this conversation.)

### What changed

- Built **`scripts/bench_test.py`** — the closed-loop bench sweep
  harness. Initial test set: connect-all, dark IV, K6485 baseline
  (dark/light at two biases), VX2740 SW-trigger probe, VX2740 self-trigger
  acquire, OV scan (mean amplitude vs OV).
- Built **`daq/plotting.py`** with a `PLOTS` registry of plot functions.
  Initial registrations: `iv`, `k6485_bars`, `k6485_ts`, `waveform`,
  `mean_waveform`, `spectrum`, `ov_scan`, `ov_spectra`.
- Built **`scripts/plot_bench.py`** as the CLI for the plot library
  (with `--live` to plot the newest `bench_*.h5`).
- Added the **R&S NGE100 submodule** (`r-snge100-python`) and a NiceGUI
  panel for it; wired into `daq/gui/hub.py` and `daq/webgui/shell.py`
  as the `nge100` instrument.
- Added a **Plots tab** to the webapp shell that renders any registered
  plot from any `bench_*.h5` (single or overlay).
- Added a **LED amplitude sweep** and a **threshold scan (light + dark)**
  to the bench harness.

### Decisions

- **B2987's built-in ammeter reads the photodiode, not the SiPM**
  (the photodiode is on the B2987's separate input on this bench, and
  it's unbiased). The SiPM IV must come from the K6485 on the low side.
  The IV sweep was rewritten to step the B2987 source manually and
  average K6485 reads at each voltage; the B2987 current is recorded as
  a (constant) photodiode diagnostic.
- **K6485 driver accepts `/dev/tty*` paths** (not only VISA strings),
  and the lab default was set to `/dev/ttyUSB0`, 9600 baud, `\r`/`\r`.
- **VX2740 software trigger** required two fixes shipped through the
  submodule: (1) `/endpoint/par/activeendpoint` must be set to `Scope`
  (default is `Raw` and silently captures nothing), and (2) WAVEFORM
  schema needs 64 channel rows regardless of how many are enabled. Two
  submodule PRs landed.

### Verified results

- V_BD = 52.2 V identified. Visible SPE pulses on the VX2740 with LED on,
  threshold 50 ADC at OV+3, ~915 pulses per 1000 capture windows.

### Open threads (resolved in 2026-05-27)

- B2987 hangs after a few aborted runs (VXI-11 leak). → fixed via SOCKET.
- OV scan saturates at OV ≥ +3 with LED. → understood and worked around
  via `ov_scan_clean` (LED off).
- Bench harness re-runs the full IV every invocation. → fixed via
  `--skip-iv` + `last_vbd.json`.

---

<!--
TEMPLATE for new entries — copy above existing entries.

## YYYY-MM-DD — short title

### What changed
- Bullet list of concrete changes (files / behaviour).

### Decisions
- Decision *with the reason*. The reason is the load-bearing part.

### Open threads (next session)
- Bullet list, each phrased as "thing to do next time".

### Verified results
- Numbers / measurements / artefacts that future-me will want.
-->
