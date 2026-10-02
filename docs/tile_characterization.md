# Tile characterization with the DAQ web app

How to run the initial and coarse characterization of a 96-SiPM tile with
the web app, and how it fits the workbook
`docs/tile_characterization_workbook.xlsx` (copy it per tile; its
"Start here" sheet has the full procedure, step by step).

Work one quad at a time: 24 SiPMs on IV MUX channels 1-16 and 23-30
(17-22 are not populated). Each quad at each temperature gets its own
**channel table**.

## Before you start

- Connect the B2987, the VX2740 and the IV MUX (header "connect all" or
  their tabs). A bench script holding an instrument blocks the web app:
  "release all instruments" on the Connections tab is the reverse.
- The B2987 bias lock (55 V at the time of writing) refuses any run whose
  bias points go above it, before anything moves.
- Fill in the "Connections" sheet of the workbook as you cable the quad;
  a second person checks it.

## Initial characterization

Digitizer tab > **characterization**.

1. **new**: tile, quad, temperature. The table name is suggested
   (`tile1_Q1_165K`); its rows are MUX 1-16 and 23-30 with digitizer
   channel = MUX channel. Change a row's digitizer channel in the table if
   it is cabled differently.
2. Enter the **V_BD estimate** for this temperature, **overvoltages**
   `3, 4`, **waveforms / point** (100), and a **trigger threshold** estimate
   in ADC counts above baseline.
3. **run**. For each MUX channel it switches the MUX, sets V_BD,est + 3 V,
   takes the waveforms, sets V_BD,est + 4 V with the same threshold, takes
   them again, and switches the bias off before the next channel.
   A quad takes a few minutes.
4. Watch **Latest run**: sample waveforms (with the threshold line), the
   amplitude spectrum at each bias, and median amplitude vs bias with the
   line A = A0 (V - V_BD). Every acquisition is also in the **waveforms**
   and **spectrum** sub-tabs, for looking at waveforms one by one.
5. At the end one lab book entry is posted: settings, V_BD and A0 per
   channel, the median and 0.5 SPE at each bias, a plot per channel and
   a V_BD/A0 summary plot.

**stop** finishes the acquisition in progress. A channel cut short is not
written to the table, and the lab book entry still covers the channels
that finished.

### The "check" column

A run flags a channel instead of quietly accepting it:

| Flag | Meaning, what to do |
|---|---|
| threshold X ADC is Y SPE ... re-run with ~Z ADC | The threshold is outside 0.3-0.7 SPE, so the median is biased (noise pulls it down, a cut SPE peak pushes it up). Re-run that channel with the suggested threshold. |
| only k/N waveforms (timeout) | Fewer triggers than asked within the timeout: dead channel, threshold far too high, or bias not reaching the SiPM. |
| no pulses at ... V | Nothing above threshold. |
| V_BD ... is ... V from the estimate / not stored in the table | The fit is more than 1.5 V off the estimate. It is **not** stored, because the coarse sweep biases straight from the table. Check the cabling and waveforms. |
| amplitude does not rise with bias: no fit | Usually noise triggers or the wrong channel. |

To re-run some channels, list just those in "MUX channels" (e.g. `9,24`).

### Editing the table by hand

Click a cell (digitizer ch, tile SiPM, feedthrough, V_BD, A0, notes) to
edit it. The change saves at once and the row's source becomes "entered
by <name>". The previous V_BD/A0 stays in the table file's history.
**export CSV** gives every column, including the two measured points
(`bias1_v`, `median1_adc`, ...) for the workbook's "Initial char" sheet.
**import CSV** reads a CSV with a `mux_ch` column back in and fills only
non-blank editable cells.

## Coarse characterization

L3 tab > **coarse sweep from a characterization table**.

1. Pick the table. Leave the MUX channels blank for all rows, or list some.
   The defaults follow the plan: overvoltages `2, 3, 4, 5, 6`, 1000
   waveforms per step, threshold 0.5 SPE, store raw on.
2. **append to list**: one dark pulse entry per row that has V_BD and A0,
   with bias points at V_BD + each OV. The trigger threshold follows the
   SPE at every bias point (0.5 x A0 x OV). Rows without V_BD/A0 are listed
   as skipped. The run id becomes `<table>_coarse`.
3. **run sequence** as for any L3 run. Each bias point's group in the run
   file records the threshold used and the V_BD/A0 it came from.

The entries can be edited like any other. "thr follows SPE" with the
V_BD and A0 fields is the same mechanism in the spec builder.

## Looking at the data afterwards

Data tab: click any `amplitudes_v` or `amplitudes_adc` dataset to get its
amplitude histogram (the charge spectrum) in ADC counts. Bins, range and
log counts are adjustable. By default the range stops at the 99.9th
percentile so a few large pulses do not squeeze the 1-3 PE peaks.

## Files

| What | Where |
|---|---|
| Channel table | `data/characterization/<table>.json` |
| Initial characterization data | `data/characterization/<table>/initial_<date>_<time>.h5`: per `muxNN/<bias>mV/ch<dig>/`, amplitudes, timestamps and every waveform; fit results as attributes of `muxNN/` |
| Coarse sweep | `data/<table>_coarse_<date>_<time>.h5` (L3 layout) |
| Plots in the lab book | `labbook_attachments/` |
