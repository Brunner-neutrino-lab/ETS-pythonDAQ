# ETS DAQ — Measurement Protocol

**Document version:** 0.2
**Last updated:** 2026-04-08

---

## 1. Overview

This document describes the full measurement protocol for characterizing SiPM tiles
on the ETS (Electrometer Test Stand). Measurements are organized in four layers of
analysis, from raw waveforms up to temperature-dependent parameter extraction:

| Layer | Input | Output |
|-------|-------|--------|
| **Waveform** | Raw digitized traces | Pulse amplitude and timing per avalanche |
| **Spectral** | Waveform analysis results | Charge and time distributions at a given voltage |
| **Voltage** | Spectral analysis results | Fit parameters vs. bias (V_BD, gain, DCR, CAs) |
| **Temperature** | Voltage analysis results | Temperature dependence of fit parameters |

The campaign divides into two phases:
- **Setup Phase** — system commissioning, device location, and parameter estimation at warm temperature
- **Characterization Phase** — full measurement campaign stepping from warm to cold

---

## 2. System Overview

### 2.1 Instruments

| Instrument | Role | Interface |
|-----------|------|-----------|
| Keysight B2987b | HV bias source + IV measurement | USB (VISA) |
| Keithley 6485 | XUV photodiode picoammeter (flux monitor) | Serial (VISA) |
| 96-ch IV-Pulse MUX | Channel switching (bias + pulse routing) | Serial (Arduino, 9600 baud) |
| XY Stage (Phidget) | VUV source positioning (2-axis) | USB (Phidget22) |
| VUV light source | SiPM illumination (passive or beam mode) | Mounted on XY stage; HV supply ~2 kV (beam mode) |
| PMT | External trigger source; mounted on lamp | HV supply ~800 V; BNC → digitizer trigger input |
| XUV photodiode | Calibrated VUV flux monitor; inside cryostat near tile | → Keithley 6485 |
| Cremat CR112 CSP | Charge-sensitive preamplification (×4) | Analog (passive) |
| Cremat CR200-1µs | Gaussian pulse shaping (×4) | Analog (passive) |
| Digitizer (TBD) | Pulse waveform acquisition | Ethernet (TBD: RTO2024 ×2 or VX2740) |
| Slow control system | Temperature, pressure, other sensors | InfluxDB on Raspberry Pi (read-only from DAQ) |

### 2.2 VUV Light Source Modes

The VUV lamp operates in two distinct modes:

| Mode | Mechanism | HV | Beam | Use |
|------|-----------|-----|------|-----|
| **Passive** | Natural scintillation | Off | Diffuse, illuminates whole tile | Setup, warm temperature characterization |
| **Beam** | Electroluminescence (EL) | ~2 kV | Collimated, ~few mm Gaussian | Cold temperature scanning (device-by-device) |

The PMT is physically mounted to the lamp in both modes and provides the external
trigger signal. The lamp flashes at approximately **2.2 kHz** in passive mode.

### 2.3 VUV Flux Monitoring

A calibrated XUV photodiode is mounted inside the cryostat near the tile. Its
photocurrent is read by the Keithley 6485 picoammeter. This provides an absolute
flux reference. During illuminated measurements, the DAQ periodically moves the
light source to the photodiode position (every 4–8 SiPMs) to verify flux stability.
The PMT provides a pulse-by-pulse relative flux monitor throughout.

### 2.4 MUX Architecture

The 96-channel MUX consists of:
- **1 motherboard** — distributes bias voltage and coordinates all daughterboards
- **4 daughterboards** — each handles 24 channels

Signal routing per measurement type:

| Measurement | Bias | Sense / Pulse |
|-------------|------|---------------|
| IV (dark or illuminated) | MUX routes one channel at a time | Common current sense node → B2987b |
| Pulse counting (dark) | MUX routes one channel per board simultaneously | 4 independent pulse nodes → 4 amplifier chains → digitizer |
| Pulse counting (illuminated) | MUX routes one channel at a time | Active board's pulse node → amplifier chain → digitizer |

### 2.5 Amplifier Chain (×4, one per daughterboard)

```
SiPM pulse output
      ↓
  Cremat CR112 (CSP)
      ↓
  Cremat CR200-1µs (shaping amplifier)
      ↓
  Digitizer input channel
```

### 2.6 Digitizer Configuration (TBD)

Two options under evaluation:

**Option A — R&S RTO2024 (×2 units)**
- 2 channels per unit (ch1, ch3), 4 channels total
- History buffer mode: acquire N waveforms, bulk readout, Python processes offline
- Dark: self-trigger (threshold on shaped pulse)
- Illuminated: external trigger from PMT sync

**Option B — CAEN VX2740**
- All 4 channels into one instrument
- Waveforms streamed to PC; Python pulse-finding script processes in real time and clears buffer
- Dark: self-trigger; Illuminated: external PMT trigger

In both cases, Python reads raw numpy waveforms and an existing pulse-finding
script extracts amplitude and timestamp per avalanche event.

### 2.7 Slow Control

Temperature, pressure, and other environmental parameters are monitored by an
independent slow control system (Lakeshore 350 + other sensors), logged to an
**InfluxDB** database on a Raspberry Pi via Node-RED. The DAQ reads temperature
and other parameters from InfluxDB at key moments (e.g. before each measurement
block) and stores them in the run data structure. The DAQ does not directly
control or query the Lakeshore.

---

## 3. Measurement Geometry

- The **tile is fixed** inside the cryostat
- The **XY stage moves the VUV light source** over the tile
- SiPM devices are 1×1 cm² on a regular rectangular grid
- VUV beam diameter: Gaussian, ~few mm (beam mode); diffuse (passive mode)
- During beam-mode scanning, each device is scanned over a **1.5×1.5 cm² grid**
  to ensure full coverage despite mechanical tolerances

---

## 4. Setup Phase

The setup phase commissions the system and establishes parameters needed to run
the characterization campaign efficiently. It is organized into two sub-phases:
warm commissioning (at the warmest temperature) and cold device mapping (at the
lowest temperature).

### 4.1 Warm Commissioning (warmest temperature)

Performed with the lamp in **passive mode**, positioned approximately at the
center of the tile. The lamp is not moved during this sub-phase.

#### 4.1.1 Dark IV Survey

Goal: identify functioning channels and estimate V_BD for all 96 devices.

1. For each channel 1–96 (MUX stepping, one at a time):
   - Run a sparse dark IV sweep (B2987b)
   - Range: pre-breakdown to electrical runaway
   - Resolution: ~500 mV steps, ~10 measurements per point
   - Save: voltage, current, timestamp
2. Extract approximate V_BD per channel from IV curve inflection

#### 4.1.2 Warm Pulse Counting Survey

Goal: estimate SPAD capacitance and refine V_BD estimate per channel.

1. Set lamp to passive mode (no HV, diffuse illumination)
2. For each voltage in {V_BD+2, V_BD+3, V_BD+4, V_BD+5} V (per-channel V_BD from 4.1.1):
   - Set bias voltage (B2987b)
   - Acquire **10,000 waveforms** per channel (4 channels in parallel via MUX)
   - Trigger: external PMT (lamp flashes at ~2.2 kHz → ~4.5 s per 10k triggers)
   - Process waveforms → amplitude + timestamp per pulse
   - Save results
3. From charge spectra: extract SPE amplitude vs. voltage → fit C, V_BD

### 4.2 Device Position Mapping (lowest temperature)

Performed after reaching the lowest temperature, before beam-mode scanning.

1. Manually position the XY stage near the expected location of a corner device
2. Raster scan a small region (~2×2 cm²) to find the device centroid from pulse rate map
3. Record precise (x, y) coordinates for corner device 1
4. Repeat for the diagonally opposite corner device
5. Use the two corner coordinates to define the rectangular grid and compute
   nominal positions for all 96 devices
6. Store position map; scanning begins from these coordinates

---

## 5. Characterization Phase

The characterization phase steps from warmest to coldest temperature. At each
temperature, the measurement sequence is:

```
1. Dark IV (all 96 channels)
2. Dark pulse counting (all 96 channels, 4 in parallel)
3. [At lowest temperature only] Switch lamp to beam mode → begin scanning
   3a. Illuminated IV + pulse counting (device by device, with flux checks)
```

Temperatures are measured at the start of each measurement block by querying
the slow control InfluxDB.

### 5.1 Temperature Sequence

| Step | Temperature | Lamp mode | Notes |
|------|-------------|-----------|-------|
| 1 | Warmest (~RT or ~233 K) | Passive (setup) → Dark | Setup phase completes here first |
| 2 | ~215 K | Dark only | IV + dark pulse counting |
| 3 | 165 K | Dark → Beam | IV + dark pulse counting + full illuminated scan |

Additional intermediate temperatures may be added. Temperature stabilization
criterion: ΔT < [TBD] K over [TBD] minutes before starting a measurement block.

### 5.2 Dark IV (per temperature)

For each channel 1–96, one at a time via MUX:

1. Select channel (MUX serial command)
2. Run B2987b list sweep
   - Range: pre-breakdown → second divergence (set from setup V_BD estimate)
   - Step: ~50 mV
   - Integration: ~10 measurements per point (fast)
3. Save: source voltage array, current array, UTC timestamps

### 5.3 Dark Pulse Counting (per temperature)

4 channels measured in parallel (one per daughterboard).

For each voltage step in the dark pulse sweep:
1. Set bias voltage (B2987b)
2. For each group of 4 channels (channels {1,25,49,73}, {2,26,50,74}, …):
   - Select one channel per daughterboard via MUX
   - Acquire waveforms (digitizer, self-trigger)
   - Number of waveforms: reduced relative to illuminated case (see §5.5)
   - Process → amplitude + timestamp per pulse
   - Save results
3. Repeat until all 96 channels complete

Voltage sweep: V_BD+2 V → V_BD+8 V, ~1 V steps (per-channel V_BD from dark IV).

### 5.4 Illuminated Measurements (lowest temperature only, beam mode)

The lamp switches to beam mode (EL, HV ~2 kV applied). The XY stage moves the
beam to each device position in sequence.

For each SiPM (positions from device map, §4.2):

1. Move XY stage to device position
2. Select channel via MUX
3. **Illuminated IV:**
   - Run B2987b list sweep (same range/step as dark IV, §5.2)
   - Save: voltage, current, timestamp
4. **Illuminated pulse counting:**
   - For each voltage step:
     - Set bias voltage
     - Acquire **100,000 waveforms** (PMT external trigger, ~2.2 kHz → ~45 s)
     - Process → amplitude + timestamp per pulse
     - Save results
5. **Flux check** (every 4–8 SiPMs):
   - Move XY stage to XUV photodiode position
   - Record Keithley 6485 photocurrent (several readings, average)
   - Log flux value with timestamp
   - Return to next SiPM position

Voltage sweep: same as dark pulse counting (V_BD+2 V → V_BD+8 V, ~1 V steps).

### 5.5 Waveform Acquisition Parameters

| Measurement | Waveforms | Trigger | Rate | Duration per voltage |
|-------------|-----------|---------|------|----------------------|
| Warm pulse survey (setup) | 10,000 | PMT external | ~2.2 kHz | ~4.5 s |
| Dark pulse counting (cold) | TBD (fewer) | Self | <100 Hz at 165 K | TBD |
| Illuminated pulse counting | 100,000 | PMT external | ~2.2 kHz | ~45 s |

> **Note on dark counting at 165 K:** DCR is expected to be <100 Hz. Total counts
> will be set to balance statistical requirements against acquisition time. A
> time-based acquisition (fixed duration rather than fixed count) may be more
> appropriate at the lowest temperature.

### 5.6 Data Saved Per Measurement

| Measurement | Saved fields |
|-------------|-------------|
| IV | Channel ID, voltage array, current array, UTC timestamps, temperature (from DB) |
| Pulse counting | Channel ID, bias voltage, pulse amplitude array, relative timestamps, temperature |
| Flux check | Timestamp, Keithley 6485 current (mean + std), XY position |
| Position scan | (x, y) grid, pulse rate or current map, channel ID |
| Run metadata | Instrument settings, VISA addresses, software version, run start time |

---

## 6. Analysis Pipeline

### 6.1 Waveform Analysis (per acquisition, online)
- Input: raw waveform (numpy array)
- Find pulses above threshold
- Extract: peak amplitude, pulse integral, arrival time relative to trigger
- Output: (amplitude, timestamp) arrays → saved to disk, buffer cleared

### 6.2 Spectral Analysis (per channel per voltage, offline)
- Input: amplitude arrays from waveform analysis
- Build charge spectrum histogram
- Identify SPE peak and multi-PE structure
- Extract: mean SPE charge, peak positions, DCR rate

### 6.3 Voltage Analysis (per channel per temperature, offline)
- Input: spectral fit parameters vs. voltage
- Fit gain vs. V: `Q = C × (V − V_BD)` → extract C, V_BD
- Fit DCR(V), CAs(V) (cross-talk + afterpulsing)
- Fit PDE from illuminated vs. dark IV

### 6.4 Temperature Analysis (per channel, offline)
- Input: voltage analysis parameters vs. temperature
- Fit V_BD(T): linear, slope ~−51.5 mV/K (HPK VUV4 reference value)
- Fit DCR(T), gain(T), CAs(T), PDE(T)

---

## 7. Time Budget

A Python script (`daq/estimate_time.py`) will compute estimated run time from
configurable parameters. Reference estimates below.

### 7.1 Illuminated Measurements (per temperature, beam mode)
- 96 devices × 6 voltage steps × 45 s = ~7.2 hours
- Flux checks (every 6 devices): ~16 checks × ~1 min = ~16 min
- Stage moves + settling: ~96 × ~1 min = ~1.6 hours
- **Total illuminated: ~9–10 hours per temperature**

### 7.2 Dark IV (per temperature)
- 96 channels × [sweep points × integration time] = TBD (fast, <1 hour estimated)

### 7.3 Dark Pulse Counting (per temperature)
- At 165 K, DCR <100 Hz: acquisition time per channel TBD (time-limited)
- At warmer temperatures: faster, ~minutes per channel

### 7.4 Overall Campaign
- Setup phase (warm): ~0.5 day
- Per temperature (characterization): ~1–2 days
- Temperature changes + stabilization: ~0.5–1 day each
- **Estimated total: ~1 week for 3 temperatures**

---

## 8. Open Questions

| ID | Question | Blocking |
|----|----------|---------|
| Q-01 | Digitizer choice: RTO2024 ×2 vs. VX2740 | Pulse module design |
| Q-02 | Waveform count for dark pulse at 165 K | Time budget |
| Q-03 | VUV beam diameter at operating distance | Raster scan step size |
| Q-04 | Temperature stabilization criterion (ΔT, duration) | Measurement sequencing |
| Q-05 | ~~HV supply for lamp and PMT — manually controlled or DAQ-controlled?~~ **Resolved: manually switched** | — |
| Q-06 | Flux check frequency (every 4 vs. 8 SiPMs) | Illuminated sequence |
| Q-07 | Time-based vs. count-based dark acquisition at 165 K | Dark pulse module |
| Q-08 | InfluxDB query interface — example scripts to be provided | Slow control integration |

---

## 9. Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 0.1 | 2026-04-07 | AI | Initial draft |
| 0.2 | 2026-04-08 | AI | Added VUV lamp modes, PMT/HV details, Keithley 6485, slow control via InfluxDB, corrected setup phase (warm IV survey + passive pulse counting; cold = device mapping only), illuminated sequence (IV+pulse per device, flux checks), updated time budget, waveform counts |
