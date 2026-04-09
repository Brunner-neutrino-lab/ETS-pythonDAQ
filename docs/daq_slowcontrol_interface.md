# DAQ ↔ Slow Control Interface

This document defines the interface the nEXO SiPM DAQ requires from the slow
control system.  It is intended to be read by Claude (or a human developer)
working on the slow control project so that the integration can be completed
without modifying the DAQ.

---

## Context

The DAQ (`daq/`) orchestrates SiPM characterisation measurements across a
temperature schedule (typically 3–5 set-points between ~100 K and ~300 K).
At each temperature the DAQ:

1. Commands the target temperature via `SlowControl.set_setpoint(T_K)`.
2. Blocks until the temperature is stable via `SlowControl.wait_for_stable(...)`,
   polling `SlowControl.temperature_K()` every few seconds.
3. Runs dark IV sweeps, dark pulse acquisitions, and (at selected temperatures)
   illuminated versions of the same.
4. Reads the measured temperature at each SiPM measurement and stores it as
   `temperature_K_measured` in the HDF5 output file alongside the commanded
   setpoint (`temperature_K_setpoint`).

The DAQ does **not** talk to the LakeShore 350 directly.  All temperature
interaction goes through the `SlowControl` class in `daq/slowcontrol.py`.

---

## The SlowControl class

File: `daq/slowcontrol.py`

The DAQ instantiates `SlowControl(config)` where `config` is an
`ExperimentConfig` (a dataclass).  Relevant config fields:

```python
influxdb_url       : str   # e.g. "http://192.168.1.10:8086"
influxdb_org       : str
influxdb_token     : str   # can also be set via DAQ_INFLUX_TOKEN env var
influxdb_bucket    : str   # default "cryostat"
influxdb_rtd_field : str   # InfluxDB field name for the control RTD, e.g. "RTD1_C"
```

The class currently implements:

| Method | Status | Description |
|--------|--------|-------------|
| `connect()` | Implemented | Opens InfluxDB client |
| `disconnect()` | Implemented | Closes client |
| `temperature_K() -> float` | Implemented | Queries last 30 s, returns newest value in K |
| `all_rtds_K() -> dict` | Implemented | Returns all RTD fields in K |
| `wait_for_stable(target_K, tolerance_K, stable_s, ...)` | Implemented | Polls until T is stable |
| `set_setpoint(T_K)` | **Stub — raises NotImplementedError** | See below |

---

## What the slow control system must implement

### `set_setpoint(T_K: float)`

The slow control system must provide a working implementation of this method.
The recommended approach is to **subclass** `SlowControl` in the slow control
project and override `set_setpoint`:

```python
# In the slow control project:
from daq.slowcontrol import SlowControl

class LakeShoreSlowControl(SlowControl):
    def __init__(self, config, lakeshore_ip: str, lakeshore_port: int = 7777):
        super().__init__(config)
        self._ls_ip   = lakeshore_ip
        self._ls_port = lakeshore_port
        self._ls      = None   # LakeShore 350 connection

    def connect(self):
        super().connect()          # InfluxDB
        # ... connect to LakeShore 350 ...
        self._ls = ...

    def disconnect(self):
        super().disconnect()
        # ... disconnect LakeShore ...

    def set_setpoint(self, T_K: float):
        """
        Send setpoint to LakeShore 350 loop 1.
        T_K is in Kelvin; convert to whatever unit the LakeShore is configured for.
        """
        # Example using lakeshore-py or direct SCPI:
        T_C = T_K - 273.15
        self._ls.command(f"SETP 1,{T_C:.3f}")   # adjust command to actual API
```

Then in `daq/gui/hub.py`, instantiate `LakeShoreSlowControl` instead of
`SlowControl`:

```python
def connect_sc(self):
    from slow_control import LakeShoreSlowControl   # slow control project
    self.sc = LakeShoreSlowControl(self.config,
                                   lakeshore_ip=self.config.lakeshore_ip)
    self.sc.connect()
    T = self.sc.temperature_K()
    self.status["sc"] = f"OK — {T:.2f} K"
```

A config field `lakeshore_ip` (and optionally `lakeshore_port`) should be
added to `ExperimentConfig` (in `daq/config.py`) when ready.

---

## Behaviour when `set_setpoint` is not implemented

The DAQ handles `NotImplementedError` gracefully in `daq/temppoint.py`:

```
WARNING  daq.temppoint: set_setpoint() not implemented — assuming temperature
         is set manually. Waiting for stability at 165.0 K.
```

The stability wait still runs normally — the DAQ will simply block until the
temperature arrives at the target (however it gets there).  This means the
DAQ works correctly today with manual temperature changes and will work with
automated control once `set_setpoint` is implemented.

---

## HDF5 output

Every IV sweep and pulse acquisition group in the HDF5 file has two
temperature attributes:

| Attribute | Value |
|-----------|-------|
| `temperature_K_setpoint` | The commanded temperature from the schedule |
| `temperature_K_measured` | The RTD reading at the time of measurement (from `temperature_K()`) |

`temperature_K_measured` is omitted if `slowcontrol` is `None` or the query
fails.  It is always the value from InfluxDB — i.e., whatever the slow control
system is writing there.

The DAQ therefore assumes that the slow control system **writes LakeShore RTD
readings to InfluxDB** continuously (the existing behaviour).  The
`temperature_K_measured` attribute reflects the most recent RTD value at the
time each SiPM measurement completes.

---

## Temperature stability criteria

`wait_for_stable` polls `temperature_K()` every `poll_s` seconds (default 5 s)
and considers the temperature stable when it has been within
`target_K ± tolerance_K` continuously for `stable_s` seconds.

These are all configurable via `ExperimentConfig`:

```python
temp_tolerance_K : float  # default 0.5 K
temp_stable_s    : float  # default 60 s
```

The timeout is 7200 s (2 hours) by default.

---

## Summary checklist for slow control integration

- [ ] Subclass `SlowControl`, override `set_setpoint(T_K)`
- [ ] LakeShore 350 connection managed in `connect()` / `disconnect()`
- [ ] RTD readings continue to be written to InfluxDB (existing behaviour)
- [ ] Add `lakeshore_ip` (and port if needed) to `ExperimentConfig`
- [ ] Update `hub.py` `connect_sc()` to instantiate the subclass
- [ ] Test with `skip_wait=True` in Level 4 tab before running a full sequence
