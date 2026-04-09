# ETS DAQ — Software Architecture

**Document version:** 0.1
**Last updated:** 2026-04-08

---

## 1. Design Philosophy

The architecture is derived from three reference projects (`usphere-DAQ`,
`GWINSTEKAFG2225_controller`, `usphere-charge`) and adapted for this system.
The core principles are:

1. **Separation of concerns** — hardware communication, instrument logic, GUI, and
   experiment orchestration are distinct layers that never reach across each other
2. **Independent modules** — every instrument module runs standalone with its own
   GUI and can be tested without the rest of the system
3. **Headless-first API** — all control logic is implemented in plain Python classes;
   the GUI is a skin on top, not the logic itself
4. **Plugin interface** — modules expose a standard interface so the top-level DAQ
   can discover and call them without knowing their internals
5. **Session persistence** — config is saved automatically; no explicit save button

---

## 2. Layer Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Experiment Layer                      │
│         daq/experiments/  (setup.py, characterize.py)   │
│  Orchestrates full measurement sequences; calls modules  │
└───────────────────────────┬─────────────────────────────┘
                            │ imports
┌───────────────────────────▼─────────────────────────────┐
│                      DAQ Layer                          │
│              daq/  (daq_core.py, daq_runner.py)         │
│  Initializes all instruments; routes data; saves files   │
└───────────┬───────────────┬───────────────┬─────────────┘
            │               │               │
    ┌───────▼──────┐ ┌──────▼──────┐ ┌─────▼────────┐
    │  Instrument  │ │  Instrument  │ │  Instrument  │
    │   Module A   │ │   Module B   │ │   Module C   │
    │  (B2987b/)   │ │ (VX2740/)   │ │  (mux/ etc.) │
    │  driver.py   │ │  driver.py  │ │  driver.py   │
    │  controller.py│ │ controller.py│ │ controller.py│
    │  gui.py      │ │  gui.py     │ │  gui.py      │
    └──────────────┘ └─────────────┘ └──────────────┘
```

---

## 3. Instrument Module Structure

Every instrument is its own git submodule (standalone repo). Internally each
follows the same three-file pattern:

```
instrument-name/
├── instrument_name/
│   ├── __init__.py          ← exports Controller class and plugin interface
│   ├── driver.py            ← low-level communication only (VISA/serial/ethernet)
│   ├── controller.py        ← high-level API; stateful; context manager
│   └── gui.py               ← PyQt5 standalone window; imports controller only
├── examples/
│   └── basic_usage.py
├── tests/
│   └── test_controller.py
├── README.md
└── requirements.txt
```

### 3.1 Driver (`driver.py`)

Handles only bytes-on-the-wire: open connection, send command, read response,
close. No state, no logic. Example pattern:

```python
class VX2740Driver:
    def __init__(self, address: str):
        self._address = address   # e.g. "dig2://192.168.1.100"
        self._device = None

    def connect(self): ...
    def disconnect(self): ...
    def write(self, endpoint: str, value): ...
    def read(self, endpoint: str): ...
    def __enter__(self): self.connect(); return self
    def __exit__(self, *_): self.disconnect()
```

### 3.2 Controller (`controller.py`)

Stateful high-level API that the DAQ and experiments call. Wraps the driver
with instrument-specific logic. Never imports Qt.

```python
class VX2740Controller:
    def __init__(self, address: str): ...
    def connect(self): ...
    def disconnect(self): ...

    # Instrument-specific API
    def configure_acquisition(self, n_samples: int, channels: list[int]): ...
    def start(self): ...
    def stop(self): ...
    def read_waveforms(self) -> dict[int, np.ndarray]: ...

    # Plugin interface (consumed by DAQ)
    MODULE_NAME  = "VX2740"
    DEVICE_NAME  = "CAEN VX2740 Digitizer"
    CONFIG_FIELDS = [
        {"key": "address", "label": "IP Address", "type": "str",
         "default": "192.168.0.1"},
        {"key": "n_samples", "label": "Samples per waveform", "type": "int",
         "default": 1024},
    ]
    DEFAULTS = {"address": "192.168.0.1", "n_samples": 1024}

    @staticmethod
    def test(config: dict) -> tuple[bool, str]: ...

    @staticmethod
    def read(config: dict) -> dict: ...

    def __enter__(self): ...
    def __exit__(self, *_): ...
```

### 3.3 GUI (`gui.py`)

Standalone PyQt5 window. Can be launched directly (`python gui.py`) for
testing without the rest of the DAQ. Communicates with the controller only
through its public API. All hardware calls run in worker threads; GUI updates
via Qt signals.

```python
class VX2740Window(QMainWindow):
    # Connection panel (address, connect/disconnect)
    # Configuration panel (channels, samples, trigger)
    # Acquisition panel (start/stop, live status)
    # Plot panel (live waveform preview)
    # Status log
```

### 3.4 Plugin Interface

Every controller exposes these four attributes so the DAQ can auto-discover
and configure modules:

| Attribute | Type | Purpose |
|-----------|------|---------|
| `MODULE_NAME` | `str` | Short key used in config files and data files |
| `DEVICE_NAME` | `str` | Human-readable name for GUI labels |
| `CONFIG_FIELDS` | `list[dict]` | Field definitions for auto-built config panels |
| `DEFAULTS` | `dict` | Fallback values if device unavailable |
| `test(config)` | `staticmethod` | Returns `(bool, str)` — used by GUI test button |
| `read(config)` | `staticmethod` | Returns `dict` of current state — used by DAQ |

---

## 4. DAQ Layer

`daq/` is the integration layer. It is **not** a standalone git repo — it lives
in the main `ETS-pythonDAQ` repo.

```
daq/
├── daq_core.py          ← DAQConfig dataclass + DAQRunner class
├── daq_runner.py        ← Measurement loop orchestration
├── daq_h5.py            ← HDF5 write/read (fixed schema)
├── daq_gui.py           ← Top-level GUI (tab per module + run control)
├── slow_control.py      ← InfluxDB query interface (temperature, etc.)
├── estimate_time.py     ← Time budget calculator (prints estimates)
├── config/
│   └── default_config.yaml   ← Default instrument addresses and run params
├── experiments/
│   ├── setup.py         ← Setup phase measurement sequence
│   └── characterize.py  ← Characterization phase measurement sequence
└── session_log.jsonl    ← Rolling config/session history (auto-written)
```

### 4.1 DAQConfig

A dataclass holding all run parameters. Serializes to/from dict (for JSONL
session log). Example fields:

```python
@dataclass
class DAQConfig:
    # Instrument addresses
    b2987b_visa: str = "USB0::2391::37912::MY54321112::0::INSTR"
    vx2740_address: str = "192.168.0.1"
    mux_port: str = "COM5"
    stage_serial_x: int = 527475
    stage_serial_y: int = 527476
    keithley6485_visa: str = "ASRL6::INSTR"

    # Run parameters
    output_dir: str = "data"
    n_waveforms_illuminated: int = 100_000
    n_waveforms_dark: int = 10_000
    voltage_sweep_iv: list[float] = field(default_factory=list)
    voltage_sweep_pulse: list[float] = field(default_factory=list)
    temperatures_k: list[float] = field(default_factory=lambda: [233.0, 215.0, 165.0])

    # Module configs (passed to plugin read/test)
    module_configs: dict = field(default_factory=dict)

    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "DAQConfig": ...
```

### 4.2 DAQRunner

Runs in a daemon thread. Orchestrates the measurement loop. Calls module
controllers directly — no SCPI in this file.

```python
class DAQRunner:
    def __init__(self, config: DAQConfig, instruments: dict, callbacks): ...
    def start(self): ...   # spawn daemon thread
    def stop(self): ...    # set stop event
    def _run(self): ...    # measurement loop (calls experiment scripts)
```

### 4.3 Slow Control Interface

```python
# slow_control.py
def query_influxdb(measurement: str, field: str,
                   host: str = "raspberrypi.local",
                   db: str = "slow_control") -> float:
    """Return most recent value of field from InfluxDB."""
    ...

def get_temperature(channel: str = "A") -> float:
    """Return current temperature in Kelvin from slow control DB."""
    ...
```

---

## 5. Experiment Layer

Experiment scripts are plain Python functions (no Qt) that accept initialized
instrument controllers and a config, then execute a measurement sequence.
They emit progress via callbacks so both GUI and headless runs see the same output.

```python
# experiments/setup.py
def run_warm_iv_survey(b2987b, mux, config, on_progress=None, on_data=None):
    """Dark IV sweep for all 96 channels at warm temperature."""
    for ch in range(1, 97):
        mux.select_channel(ch)
        data = b2987b.sweep(config.voltage_sweep_iv)
        if on_data: on_data(ch, data)
        if on_progress: on_progress(ch, 96)

def run_device_position_map(stage, b2987b, mux, config, ...):
    """Raster scan to locate device centroids."""
    ...

# experiments/characterize.py
def run_dark_iv(b2987b, mux, config, ...): ...
def run_dark_pulse(digitizer, mux, b2987b, config, ...): ...
def run_illuminated_iv(b2987b, mux, stage, config, ...): ...
def run_illuminated_pulse(digitizer, mux, b2987b, stage, keithley, config, ...): ...
```

---

## 6. Data Format

### 6.1 File Structure

```
data/
└── run_YYYYMMDD_HHMMSS/
    ├── run_config.json          ← Full DAQConfig snapshot for this run
    ├── dark_iv/
    │   └── ch{N}_T{K}.h5       ← IV data per channel per temperature
    ├── dark_pulse/
    │   └── ch{N}_V{mV}_T{K}.h5 ← Waveform/pulse data
    ├── illuminated_iv/
    │   └── ch{N}_T{K}.h5
    ├── illuminated_pulse/
    │   └── ch{N}_V{mV}_T{K}.h5
    └── flux_checks/
        └── flux_log.csv         ← Keithley 6485 readings with timestamps
```

### 6.2 HDF5 Schema (per file)

```
/ (root)
├── attrs:
│   ├── schema_version = 1
│   ├── channel_id = 5
│   ├── bias_voltage_V = 48.5
│   ├── temperature_K = 165.0
│   ├── measurement_type = "dark_pulse"
│   └── run_start_utc = 1712345678.0
│
├── waveforms [shape (N_waveforms, N_samples), dtype float32]
│   └── attrs: sample_rate_Hz, trigger_type
│
├── pulse_amplitudes [shape (N_pulses,), dtype float32]
│   └── attrs: units = "V"
│
└── pulse_timestamps [shape (N_pulses,), dtype float64]
    └── attrs: units = "s", reference = "run_start"
```

### 6.3 Session Log

`daq/session_log.jsonl` — one JSON object per line, written automatically on
every run start. Loads last entry on startup to restore previous config.

---

## 7. Threading Model

| Thread | What runs there | Communication |
|--------|----------------|---------------|
| Main (Qt) | GUI event loop | — |
| DAQ worker | DAQRunner._run() | Signals → GUI |
| Instrument workers | Long hardware calls (connect, sweep) | QThread + signals |
| Waveform processor | Online pulse finding | Thread + queue |

**Rule:** No hardware calls on the main thread. All results relayed to GUI
via `pyqtSignal` — Qt handles thread marshalling.

---

## 8. GUI Structure (top-level DAQ GUI)

```
DAQ Main Window (QMainWindow)
├── Instrument panels (one per module, auto-built from CONFIG_FIELDS)
│   ├── Connection status indicator
│   ├── Config fields (address, port, etc.)
│   └── Test Connection button
│
├── Tabs:
│   ├── Run Control
│   │   ├── Temperature sequence selector
│   │   ├── Measurement type checkboxes
│   │   ├── Output directory + run name
│   │   ├── Start / Stop / Abort buttons
│   │   └── Progress bar + ETA
│   │
│   ├── Live Data
│   │   ├── Current channel / voltage / temperature
│   │   ├── Waveform preview (last N pulses)
│   │   └── IV curve preview (current sweep)
│   │
│   ├── Setup
│   │   ├── Corner device coordinate entry
│   │   ├── Grid preview (computed device positions)
│   │   └── Run setup phase button
│   │
│   └── [Per-instrument tabs — passthrough to module GUIs]
│
└── Status log (scrolling)
```

Each instrument module can also be launched as a **standalone window** for
independent testing:

```bash
python -m vx2740.gui          # digitizer standalone
python -m b2987b.gui          # electrometer standalone
python -m mux.gui             # multiplexer standalone
python -m stage_controller.gui  # XY stage standalone
```

---

## 9. Module Summary Table

| Module | Repo | Status | Interface |
|--------|------|--------|-----------|
| `b2987b` | B2987b-Control-Program | Improve | VISA USB |
| `vx2740` | vx2740-python (new) | New | Ethernet (caen-dig2) |
| `mux` | ETS-96-channel-IV-pulse-mux | Add Python driver | Serial (Arduino) |
| `stage_controller` | phidget-stage-controller (new) | New (from scanIV) | USB (Phidget22) |
| `keithley6485` | keithley6485 (new) | New | Serial VISA |
| `rto2024` | RTO2024-python | New (optional) | Ethernet VISA |

---

## 10. Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 0.1 | 2026-04-08 | AI | Initial draft from reference repo analysis |
