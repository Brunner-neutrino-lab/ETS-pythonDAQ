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
