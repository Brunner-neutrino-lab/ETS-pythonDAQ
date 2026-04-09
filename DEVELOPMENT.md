# ETS DAQ — Development Tracker

> Living document. Updated as the project progresses.
> Status legend: `[ ]` Not started · `[~]` In progress · `[x]` Complete

---

## 1. Software Modules

Each module is a standalone git repository loaded into the DAQ as a submodule.
Columns: **Origin** = New | Improve existing · **Status** = overall readiness.

### 1.1 B2987b Electrometer Control
**Directory:** `B2987b-Control-Program/`
**Origin:** Improve existing
**Status:** `[~]` In progress

| Task | Status |
|------|--------|
| README with usage instructions | `[~]` Draft exists |
| API defined (function signatures, return types) | `[ ]` |
| Core driver (`electrometer.py`) working | `[x]` |
| Mux interface (`multiplexer.py`) working | `[x]` |
| Configuration file format documented | `[~]` CSV format exists, undocumented |
| Data analysis utilities (`DataAnalysis.py`) | `[~]` Exists, needs review |
| Unit/integration tests or example scripts | `[ ]` |
| Clean up and remove dead code | `[ ]` |

---

### 1.2 Keithley 6485 Picoammeter (XUV Flux Monitor)
**Directory:** `Keithley6487/` *(rename/fork to `keithley6485/`)*
**Origin:** Improve existing (Keithley6487 repo has a similar driver)
**Status:** `[ ]` Not started

| Task | Status |
|------|--------|
| README with usage instructions | `[ ]` |
| API defined | `[ ]` |
| Serial VISA connection and initialization | `[ ]` |
| Single current reading | `[ ]` |
| Averaged reading (mean + std over N samples) | `[ ]` |
| Example scripts | `[ ]` |

---

### 1.3 RTO2024 Oscilloscope Control
**Directory:** `RTO2024-python/`
**Origin:** New (empty repo)
**Status:** `[ ]` Not started

| Task | Status |
|------|--------|
| README with usage instructions | `[ ]` |
| API defined | `[ ]` |
| VISA/LXI Ethernet connection and initialization | `[ ]` |
| Single waveform acquisition | `[ ]` |
| Averaging / statistics modes | `[ ]` |
| Trigger configuration (external, edge, etc.) | `[ ]` |
| Data export (raw trace → numpy / CSV) | `[ ]` |
| Example scripts | `[ ]` |

---

### 1.3 96-Channel IV-Pulse Multiplexer
**Directory:** `ETS-96-channel-IV-pulse-mux/`
**Origin:** Improve existing (hardware complete; firmware complete)
**Status:** `[~]` In progress

| Task | Status |
|------|--------|
| README with usage instructions | `[x]` |
| Hardware design files (KiCad) | `[x]` |
| Arduino firmware (`iv-mux.ino`) | `[x]` |
| Firmware serial command protocol documented | `[x]` |
| Python serial driver (channel switching, status queries) | `[ ]` |
| API defined for Python driver | `[ ]` |
| Known hardware errata documented | `[x]` (in README) |
| Integration test (Python → serial → Arduino) | `[ ]` |

---

### 1.4 Linear Rail / Stage Controller (Phidget)
**Directory:** `phidget-rail-controller/` *(to be created from `scanIV/ETSStageController.py`)*
**Origin:** New repo (extracted from `scanIV`)
**Status:** `[ ]` Not started

| Task | Status |
|------|--------|
| README with usage instructions | `[ ]` |
| API defined (move_to, home, get_position, etc.) | `[ ]` |
| 1D rail adaptation of ETSStageController | `[ ]` |
| Homing / limit switch handling | `[ ]` |
| Position calibration (steps → mm) | `[ ]` |
| Example scripts | `[ ]` |

---

### 1.5 scanIV (Legacy / Reference)
**Directory:** `scanIV/`
**Origin:** Existing — kept as reference; superseded by DAQ integration
**Status:** `[~]` Reference only

| Task | Status |
|------|--------|
| Confirm which parts are superseded | `[ ]` |
| Archive or deprecate cleanly | `[ ]` |

---

## 2. Hardware Documentation

Each hardware component without its own software module gets a documentation
directory under `hardware/`. Each should have a description, connection/wiring
notes, and relevant datasheets.

**Top-level directory:** `hardware/`

| Component | Directory | Description | Wiring/Connection | Datasheet(s) | Status |
|-----------|-----------|-------------|-------------------|--------------|--------|
| Cremat CR112 CSP | `hardware/cremat-CR112/` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| Cremat CR200-1µs Shaping Amp | `hardware/cremat-CR200/` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| VUV Light Source | `hardware/vuv-light-source/` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| Linear Rail System | `hardware/linear-rail/` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |
| SiPM Device Under Test | `hardware/sipm-dut/` | `[ ]` | `[ ]` | `[ ]` | `[ ]` |

---

## 3. Main DAQ

**Directory:** `daq/` *(to be created)*

### 3.1 Protocol and Architecture Documentation

| Document | Status |
|----------|--------|
| System architecture overview (block diagram, signal flow) | `[ ]` |
| Measurement protocol (step-by-step procedure) | `[ ]` |
| Instrument connection map (VISA addresses, ports, IPs) | `[ ]` |
| Data format specification | `[ ]` |

### 3.2 DAQ Software

| Task | Status |
|------|--------|
| Top-level configuration (YAML/JSON — instrument addresses, run params) | `[ ]` |
| Instrument manager (loads/initializes all modules) | `[ ]` |
| IV measurement sequence (dark + bright, per channel) | `[ ]` |
| Pulse measurement sequence (dark + bright, per channel) | `[ ]` |
| Channel loop orchestration (mux + all measurements) | `[ ]` |
| VUV source position control integration | `[ ]` |
| Data storage layer (HDF5 or structured CSV) | `[ ]` |
| Run logging (instrument state, errors, timestamps) | `[ ]` |
| Live monitoring / progress display | `[ ]` |
| Emergency stop / safe shutdown | `[ ]` |
| End-to-end integration test (all instruments, single channel) | `[ ]` |
| Full run test (all channels, single temperature) | `[ ]` |

### 3.3 Analysis Pipeline

| Task | Status |
|------|--------|
| IV parameter extraction (V_BD, DCR, CAs) | `[ ]` |
| Pulse analysis (SPE resolution, gain) | `[ ]` |
| PDE extraction (bright vs dark) | `[ ]` |
| Per-channel summary plots | `[ ]` |
| Batch analysis across tiles / temperatures | `[ ]` |

---

## 4. Overall Milestones

| ID | Milestone | Status |
|----|-----------|--------|
| M-01 | Protocol document written and reviewed | `[ ]` |
| M-02 | All module APIs defined | `[ ]` |
| M-03 | B2987b module clean and tested | `[ ]` |
| M-04 | RTO2024 module working (single waveform) | `[ ]` |
| M-05 | Mux Python driver working | `[ ]` |
| M-06 | Rail controller working | `[ ]` |
| M-07 | Hardware docs complete | `[ ]` |
| M-08 | DAQ single-channel end-to-end test passing | `[ ]` |
| M-09 | DAQ full 96-channel unattended run | `[ ]` |
| M-10 | Analysis pipeline validated | `[ ]` |
