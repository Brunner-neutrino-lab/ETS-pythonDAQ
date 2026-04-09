# ETS Python DAQ

Python-based data acquisition system for nEXO SiPM tile characterization.

## Overview

This repository integrates all software modules, hardware documentation, and
measurement protocols for the ETS (Electrometer Test Stand) DAQ system. The
system performs automated IV and pulse measurements across 96 SiPM channels
using a multiplexed bias/sense circuit, a VUV light source on a motorized rail,
and a charge-sensitive preamplifier chain.

See [DEVELOPMENT.md](DEVELOPMENT.md) for current development status.

## Repository Structure

```
ETS-pythonDAQ/
│
├── daq/                          ← Top-level DAQ integration (main entry point)
│
├── docs/                         ← Protocol, architecture, and connection documents
│
├── hardware/                     ← Hardware documentation and datasheets
│   ├── cremat-CR112/             ← Charge-sensitive preamplifier
│   ├── cremat-CR200/             ← Shaping amplifier (1 µs)
│   ├── vuv-light-source/         ← VUV illumination source
│   ├── linear-rail/              ← Linear positioning stage
│   └── sipm-dut/                 ← SiPM device under test
│
├── B2987b-Control-Program/       ← [submodule] Keysight B2987b electrometer driver
├── ETS-96-channel-IV-pulse-mux/  ← [submodule] 96-ch IV-pulse MUX (hardware + firmware)
├── RTO2024-python/               ← [submodule] R&S RTO2024 oscilloscope driver
├── phidget-rail-controller/      ← [submodule] Phidget stepper rail controller (TBD)
├── scanIV/                       ← [submodule] Legacy scan DAQ (reference)
│
├── reference/                    ← Non-active repos kept for reference
│
├── DEVELOPMENT.md                ← Development tracker and checklist
└── environment.yml               ← Conda environment for the full system
```

## Instruments

| Instrument | Role | Module |
|-----------|------|--------|
| Keysight B2987b | HV bias source + IV measurement | `B2987b-Control-Program/` |
| Keithley 6485 | XUV photodiode picoammeter (flux monitor) | `keithley6485/` |
| R&S RTO2024 / CAEN VX2740 | Pulse waveform acquisition (TBD) | `RTO2024-python/` |
| Custom 96-ch MUX | Channel switching (Arduino) | `ETS-96-channel-IV-pulse-mux/` |
| Phidget XY stage | VUV source 2D positioning | `phidget-stage-controller/` |
| VUV lamp (passive + beam) | SiPM illumination; PMT trigger source | `hardware/vuv-light-source/` |
| Cremat CR112 + CR200 | Analog pulse shaping ×4 (passive) | `hardware/` |
| Slow control (InfluxDB) | Temperature + environmental monitoring | read-only from DAQ |

## Quickstart

> Full instructions TBD after protocol document is complete.

```bash
conda env create -f environment.yml
conda activate ets-daq
```

## Status

See [DEVELOPMENT.md](DEVELOPMENT.md) for a full task-by-task breakdown.
