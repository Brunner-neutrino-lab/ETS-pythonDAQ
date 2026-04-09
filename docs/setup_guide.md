# ETS-pythonDAQ — Setup and Testing Guide

This guide walks through cloning the repository, installing dependencies,
testing each instrument in simulation, verifying hardware connections, and
running the DAQ GUI.  It is intended for on-site users who are setting up the
system for the first time.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone the repository](#2-clone-the-repository)
3. [Create the Python environment](#3-create-the-python-environment)
4. [Test each instrument in simulation](#4-test-each-instrument-in-simulation)
5. [Hardware setup and connection testing](#5-hardware-setup-and-connection-testing)
6. [Configure the DAQ](#6-configure-the-daq)
7. [Run the GUI](#7-run-the-gui)
8. [Run a test measurement](#8-run-a-test-measurement)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Git** ≥ 2.20 | Needed for submodule support |
| **Anaconda or Miniconda** | Manages the Python environment |
| **Windows 10/11** | Tested on Windows; Linux/macOS should work with minor path changes |
| **Phidget22 drivers** | Required for XY stage — install before connecting hardware |
| **CAEN FELib** | Required only for VX2740 digitizer — see §5.5 |
| **NI-VISA or pyvisa-py** | For VISA instruments (B2987b, Keithley 6485) |

### Install Phidget22 drivers (stage)

Download and run the Phidget22 installer from:
https://www.phidgets.com/docs/OS_-_Windows

### Install NI-VISA (recommended for USB instruments)

Download NI-VISA from:
https://www.ni.com/en/support/downloads/drivers/download.ni-visa.html

Alternatively, `pyvisa-py` (pure Python, no NI-VISA needed) is already
included in the environment — it works for most instruments but may have
issues with some USB backends.

---

## 2. Clone the repository

The instrument packages are git submodules.  Always clone with `--recurse-submodules`:

```bash
git clone --recurse-submodules https://github.com/Brunner-neutrino-lab/ETS-pythonDAQ.git
cd ETS-pythonDAQ
```

If you already cloned without the flag, initialise the submodules manually:

```bash
git submodule update --init --recursive
```

Verify the submodules are populated — each directory should contain files,
not be empty:

```bash
ls RTO2024-python/
ls keysight2987b-python/
ls keithley6485-python/
ls phidget-stage-python/
ls pulse-mux-python/
ls vx2740-python/
```

---

## 3. Create the Python environment

```bash
conda env create -f environment.yml
conda activate ets-daq
```

Install any remaining instrument package dependencies:

```bash
pip install -r RTO2024-python/requirements.txt
pip install -r keysight2987b-python/requirements.txt
pip install -r keithley6485-python/requirements.txt
pip install -r phidget-stage-python/requirements.txt
pip install -r pulse-mux-python/requirements.txt
pip install -r vx2740-python/requirements.txt
pip install pyyaml influxdb-client PyQt5
```

Verify the environment:

```bash
python -c "import numpy, h5py, PyQt5, pyvisa, serial, Phidget22; print('All core imports OK')"
```

---

## 4. Test each instrument in simulation

Every instrument package has a simulation mode that runs without any hardware.
This verifies the software is installed correctly before touching any equipment.

Run all simulation tests from the repo root:

```bash
conda activate ets-daq
```

### 4.1 Keysight B2987B Electrometer

```bash
python keysight2987b-python/examples/basic_usage.py
```

Expected output: a simulated IV sweep printed to the terminal with no errors.

### 4.2 Keithley 6485 Picoammeter

```bash
python keithley6485-python/examples/basic_usage.py
```

Expected output: simulated current readings printed, no errors.

### 4.3 Phidget XY Stage

```bash
python phidget-stage-python/examples/basic_usage.py
```

Expected output: simulated moves and position readback, no errors.

### 4.4 96-channel MUX

```bash
python pulse-mux-python/examples/basic_usage.py
```

Expected output: simulated channel selections, no errors.

### 4.5 RTO2024 Digitizer

```bash
python RTO2024-python/examples/basic_acquisition.py
```

Expected output: simulated waveform acquisition, no errors.

### 4.6 VX2740 Digitizer

```bash
python vx2740-python/examples/basic_acquisition.py
```

Expected output: simulated waveform acquisition, no errors.

### 4.7 DAQ layer smoke test

```bash
python -c "
import sys; sys.path += [
    'keysight2987b-python', 'keithley6485-python',
    'phidget-stage-python', 'pulse-mux-python',
    'RTO2024-python', 'vx2740-python'
]
from daq.config import ExperimentConfig
from daq.estimate_time import estimate
cfg = ExperimentConfig()
print('Config OK')
est = estimate(cfg)
print(f'Estimated run time: {est.total_s/3600:.1f} h')
"
```

If all six simulation tests and the DAQ smoke test pass, the software is
installed correctly.

---

## 5. Hardware setup and connection testing

Work through each instrument in the order below.  The DAQ will function with
any subset of instruments connected — missing instruments are logged as
warnings and skipped.

### 5.1 Keysight B2987B Electrometer (VISA USB)

1. Connect the B2987B to the PC via USB
2. Power on the instrument
3. Find the VISA address:
   ```bash
   python -c "import pyvisa; rm = pyvisa.ResourceManager(); print(rm.list_resources())"
   ```
   Look for a resource like `USB0::2391::37912::MY54321112::0::INSTR`
4. Test the connection:
   ```python
   # run in Python
   import sys; sys.path.insert(0, 'keysight2987b-python')
   from b2987b import B2987BController
   ctrl = B2987BController(visa="USB0::2391::37912::MY54321112::0::INSTR", mode="hardware")
   ctrl.connect()
   print(ctrl.identify())
   ctrl.disconnect()
   ```
5. Record the VISA address — you will enter it in the Config tab of the GUI.

### 5.2 Keithley 6485 Picoammeter (Serial VISA)

1. Connect via RS-232 or USB-serial adapter
2. Find the port:
   - Windows: Device Manager → Ports (COM & LPT) — note the COM number
   - Or: `python -c "import serial.tools.list_ports; print([p.device for p in serial.tools.list_ports.comports()])"`
3. Test the connection:
   ```python
   import sys; sys.path.insert(0, 'keithley6485-python')
   from keithley6485 import K6485Driver
   drv = K6485Driver(visa="COM5", mode="hardware")   # replace COM5
   drv.connect()
   drv.reset()
   drv.zero_check_off()
   print(drv.read_current())
   drv.disconnect()
   ```

### 5.3 96-channel MUX (Serial, Arduino)

1. Connect the MUX motherboard USB to the PC
2. Find the COM port (same method as §5.2)
3. The MUX communicates at **9600 baud**
4. Test the connection:
   ```python
   import sys; sys.path.insert(0, 'pulse-mux-python')
   from pulse_mux import MuxController
   mux = MuxController(port="COM6", mode="hardware")   # replace COM6
   mux.connect()
   mux.select(1)   # select channel 1
   print("MUX OK — channel 1 selected")
   mux.zero()
   mux.disconnect()
   ```

### 5.4 Phidget XY Stage (USB)

> **Phidget22 drivers must be installed before plugging in the stage** (§1).

1. Connect both stepper controllers and the limit switch hub via USB
2. Open the Phidget Control Panel (installed with the drivers) and note the
   serial numbers of the two stepper controllers and the hub
3. Test the connection:
   ```python
   import sys; sys.path.insert(0, 'phidget-stage-python')
   from phidget_stage import StageController
   stage = StageController(
       serial_x=523267,    # replace with your serial numbers
       serial_y=523253,
       serial_limit=527475,
       mode="hardware"
   )
   stage.connect()
   pos = stage.position()
   print(f"Stage position: {pos} mm")
   stage.disconnect()
   ```
4. Record the three serial numbers for the Config tab.

### 5.5 Digitizer

**RTO2024 (Rohde & Schwarz):**
1. Connect via Ethernet; set a static IP on the instrument (e.g. `192.168.0.2`)
2. Ensure the PC is on the same subnet
3. Test:
   ```python
   import sys; sys.path.insert(0, 'RTO2024-python')
   from rto2024 import RTO2024Controller
   ctrl = RTO2024Controller(address="192.168.0.2", mode="hardware")
   ctrl.connect()
   print(ctrl.identify())
   ctrl.disconnect()
   ```

**VX2740 (CAEN):**
1. Install the CAEN FELib C library from the CAEN website
2. Install the Python binding: `pip install caen-felib`
3. Connect via Ethernet; note the IP address
4. Test:
   ```python
   import sys; sys.path.insert(0, 'vx2740-python')
   from vx2740 import VX2740Controller
   ctrl = VX2740Controller(address="192.168.0.1", mode="hardware")
   ctrl.connect()
   print(ctrl.identify())
   ctrl.disconnect()
   ```

### 5.6 Slow Control (InfluxDB)

The slow control reads temperature from an InfluxDB instance on a Raspberry Pi.

1. Ensure the PC can reach the InfluxDB server (ping the hostname or IP)
2. Obtain the InfluxDB token from the person responsible for the slow control
   system, or set it as an environment variable:
   ```bash
   set DAQ_INFLUX_TOKEN=your_token_here   # Windows
   ```
3. Test:
   ```python
   import sys; sys.path.insert(0, '.')
   from daq.config import ExperimentConfig
   from daq.slowcontrol import SlowControl
   cfg = ExperimentConfig()
   cfg.influxdb_url   = "http://192.168.1.10:8086"   # replace
   cfg.influxdb_org   = "your-org"
   cfg.influxdb_bucket = "Cryostat"
   cfg.influxdb_rtd_field = "RTD2_C"
   sc = SlowControl(cfg)
   sc.connect()
   print(f"Temperature: {sc.temperature_K():.2f} K")
   sc.disconnect()
   ```

---

## 6. Configure the DAQ

The DAQ is configured via a YAML file.  Copy the example and edit it:

```bash
cp docs/example_config.yaml run_config.yaml
```

Or generate one from inside Python:

```python
import sys; sys.path.insert(0, '.')
from daq.config import ExperimentConfig
cfg = ExperimentConfig()
cfg.to_yaml("run_config.yaml")
print("run_config.yaml written")
```

Open `run_config.yaml` and fill in:

| Field | Description | Example |
|-------|-------------|---------|
| `b2987b_visa` | VISA address of B2987B | `USB0::2391::37912::MY54321112::0::INSTR` |
| `digitizer_type` | `rto2024` or `vx2740` | `rto2024` |
| `digitizer_address` | IP address of digitizer | `192.168.0.2` |
| `mux_port` | COM port of MUX | `COM6` |
| `k6485_port` | COM port or VISA of K6485 | `COM5` |
| `stage_serial_x` | Phidget serial, X axis | `523267` |
| `stage_serial_y` | Phidget serial, Y axis | `523253` |
| `stage_serial_limit` | Phidget serial, limit hub | `527475` |
| `influxdb_url` | InfluxDB server URL | `http://192.168.1.10:8086` |
| `influxdb_org` | InfluxDB organisation | `your-org` |
| `influxdb_bucket` | InfluxDB bucket | `Cryostat` |
| `influxdb_rtd_field` | RTD field name (in °C) | `RTD2_C` |
| `channel_map_file` | Path to channel map CSV | `channel_map.csv` |
| `temperatures_K` | Temperature schedule | `[233.0, 215.0, 165.0]` |
| `data_dir` | Where HDF5 files are saved | `data` |

### Channel map

The channel map CSV maps each MUX channel to a SiPM ID and stage position.
See `daq/config.py` for the format.  Generate a template:

```python
import sys; sys.path.insert(0, '.')
from daq.config import write_example_channel_map
write_example_channel_map("channel_map.csv", n_sipms=96, pitch_mm=10.0)
print("channel_map.csv written — edit x_mm/y_mm for your tile layout")
```

The `dark`, `lamp`, and `photodiode` rows at the bottom define special stage
positions.  Measure these by hand and enter them in mm.

---

## 7. Run the GUI

```bash
conda activate ets-daq
cd ETS-pythonDAQ
python -m daq.app
```

The GUI opens with tabs for each level of operation:

| Tab | Purpose |
|-----|---------|
| **Connections** | Connect/disconnect each instrument individually; shows status |
| **Config** | Edit all parameters; load/save YAML; estimate run time |
| **Level 1 — Primitives** | Manual single-instrument operations (move stage, set bias, etc.) |
| **Level 2 — Single SiPM** | Run an IV sweep or pulse acquisition on one SiPM |
| **Level 3 — Tile Sweep** | IV or pulse run across the whole tile at one temperature |
| **Level 4 — Temp Point** | Full measurement sequence at one temperature |
| **Level 5 — Full Run** | Complete experiment with resume support |
| **Raster Scan** | 2D position scans for beam profiling and device mapping |
| **Alignment** | Re-zero stage coordinates after physical adjustments |

### Recommended startup sequence

1. Open **Config** tab → Load YAML → click **Apply to Hub**
2. Open **Connections** tab → connect instruments one by one
3. Check each status label turns green
4. Open **Level 1** tab → read temperature, read position — verify responses
5. Proceed to the measurement level you need

---

## 8. Run a test measurement

Before running a full experiment, verify the system end-to-end with a
single-SiPM test.

1. In the **Config** tab, set a short IV sweep (e.g. 40–45 V, 1 V step) and
   few waveforms (e.g. 100)
2. Open the **Level 2 — Single SiPM** tab
3. Select a SiPM from the dropdown
4. Click **Run IV Sweep** — watch the results appear in the log
5. Click **Run Pulse** — verify waveforms are acquired

If both succeed, the full stack (stage → MUX → electrometer/digitizer → HDF5)
is working.

### Run a time estimate

In the **Config** tab click **Estimate Run Time** with your full channel map
and temperature schedule loaded.  This prints a breakdown by phase and a total
estimated duration.  Adjust parameters (e.g. waveforms per SiPM) if needed
before committing to a full run.

---

## 9. Troubleshooting

### "No module named X"

Ensure the conda environment is activated and all `requirements.txt` files
were installed:
```bash
conda activate ets-daq
pip install -r keysight2987b-python/requirements.txt   # etc.
```

### VISA instrument not found

- Run `python -c "import pyvisa; print(pyvisa.ResourceManager().list_resources())"` to list visible resources
- Try unplugging and replugging the USB cable
- On Windows, check Device Manager for driver errors
- If using pyvisa-py instead of NI-VISA, ensure `pyusb` or `pyserial` backends are installed:
  ```bash
  pip install pyusb pyserial
  ```

### Phidget stage not found

- Confirm Phidget22 drivers are installed (not just the Python library)
- Open the Phidget Control Panel — the device should appear there before the
  Python code can see it
- Check the serial numbers in the Config tab match what the Control Panel shows

### MUX not responding

- Verify the COM port in Device Manager
- Confirm baud rate is 9600 — the Arduino firmware is fixed at this rate
- Try the connection using a serial terminal (e.g. PuTTY) to confirm basic comms

### InfluxDB temperature reads fail

- Ping the InfluxDB server from the DAQ PC
- Confirm the token is correct (try `set DAQ_INFLUX_TOKEN=...` before launching)
- Confirm the bucket and field names match the slow control system configuration
- The DAQ can run without slow control — temperature waits are skipped with a warning

### GUI freezes

All hardware calls run in background threads.  If the GUI appears frozen,
check the log area at the bottom of the active tab — it may be waiting for
a hardware response.  Long operations (temperature stabilisation, full tile
sweeps) are expected to take minutes to hours.

### Resume a partial run

If a run was interrupted, relaunch the GUI, load the same YAML, and in the
**Level 5 — Full Run** tab set the same run directory and run ID, ensure
**Resume** is checked, and click **Start Run**.  Completed steps are skipped
automatically.
