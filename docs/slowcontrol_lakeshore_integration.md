# Slow Control ↔ DAQ: LakeShore 350 Integration Guide

This document explains how ETS-pythonDAQ interfaces with the new slow control
system to read RTD temperatures and command the LakeShore 350 setpoint.

---

## How it works

The slow control service runs on the Raspberry Pi and owns the LakeShore 350
connection.  The DAQ never talks to the LakeShore directly.  Instead:

```
ETS-pythonDAQ                     Slow Control Service
─────────────                     ────────────────────
                MQTT
set_setpoint() ──────────────→  ets/commands/lakeshore/setpoint
                                   │
                                   ▼
                                LakeShore350Driver
                                sends SCPI: SETP 1,<T_K>
                                   │
                                   ▼
                                LakeShore 350 hardware
                                   │
                                   ▼
                                Reads RTDs (KRDG?)
                                   │
                                   ▼  publishes
                                ets/sensors/lakeshore/rtd/{A,B,C,D}
                                   │
                                   ▼  Telegraf
                                InfluxDB ("slowcontrol" bucket)
                                   │
temperature_K() ◄────────────  Flux query on influxdb_rtd_field
```

**Reads** use the existing InfluxDB path (`SlowControl.temperature_K()`).
Nothing changes for reads — the slow control service publishes RTD values to
MQTT, Telegraf writes them to InfluxDB, and the DAQ queries InfluxDB exactly
as before.

**Writes** (setpoint commands) go over MQTT instead of requiring a direct
LakeShore connection from the DAQ machine.

---

## Implementation: override `set_setpoint()`

The DAQ's `SlowControl` base class in `daq/slowcontrol.py` has a stub
`set_setpoint()` that raises `NotImplementedError`.  To enable automated
temperature control, subclass it and publish an MQTT message:

### 1. Create `daq/lakeshore_slowcontrol.py`

```python
"""SlowControl subclass that commands the LakeShore via MQTT."""

import json
import logging

import paho.mqtt.publish as publish

from daq.slowcontrol import SlowControl

log = logging.getLogger(__name__)


class LakeShoreSlowControl(SlowControl):
    """Extends SlowControl with MQTT-based setpoint control.

    The slow control service subscribes to
    ``ets/commands/lakeshore/setpoint`` and forwards the command to the
    LakeShore 350 via SCPI.
    """

    def __init__(self, config):
        super().__init__(config)
        self._mqtt_broker = getattr(config, "mqtt_broker", "localhost")
        self._mqtt_port = getattr(config, "mqtt_port", 1883)

    def set_setpoint(self, T_K: float):
        """Publish a setpoint command to the slow control service.

        Parameters
        ----------
        T_K : float
            Target temperature in Kelvin.  The slow control LakeShore
            driver sends ``SETP 1,<T_K>`` directly — the LakeShore 350
            is configured to read in Kelvin.
        """
        payload = json.dumps({"value": T_K, "loop": 1})
        publish.single(
            topic="ets/commands/lakeshore/setpoint",
            payload=payload,
            hostname=self._mqtt_broker,
            port=self._mqtt_port,
            qos=1,
        )
        log.info("Setpoint command sent: %.4f K", T_K)
```

### 2. Add config fields to `daq/config.py`

Add two fields to `ExperimentConfig`:

```python
mqtt_broker: str = "localhost"
mqtt_port: int = 1883
```

These default to localhost, which works when the DAQ runs on the same Pi.
When running from a different machine, point them at the Pi's IP.

### 3. Wire it up in `daq/gui/hub.py`

Replace the `SlowControl` instantiation:

```python
def connect_sc(self):
    from daq.lakeshore_slowcontrol import LakeShoreSlowControl

    self.sc = LakeShoreSlowControl(self.config)
    self.sc.connect()
    T = self.sc.temperature_K()
    self.status["sc"] = f"OK — {T:.2f} K"
```

That's it.  The rest of the DAQ (stability wait, temperature storage in HDF5)
works unchanged.

---

## What the DAQ does at each temperature point

For reference, here is the sequence that Level 4 / `temppoint.py` executes:

1. **`sc.set_setpoint(T_K)`** — publishes `{"value": T_K, "loop": 1}` to
   `ets/commands/lakeshore/setpoint`.  The slow control service receives
   this and sends `SETP 1,<T_K>` to the LakeShore 350.

2. **`sc.wait_for_stable(target_K, tolerance_K, stable_s)`** — polls
   `sc.temperature_K()` (InfluxDB query) until the measured temperature is
   within `tolerance_K` of the target for `stable_s` consecutive seconds.
   Configurable timeout prevents infinite blocking.

3. **Measurements run** — IV sweeps, pulse acquisitions, etc.

4. **`sc.temperature_K()`** — called once per SiPM measurement.  The result
   is stored as `temperature_K_measured` in the HDF5 dataset alongside
   `temperature_K_setpoint`.

5. Repeat from step 1 for the next temperature point.

---

## InfluxDB field mapping

The slow control service publishes LakeShore readings to these MQTT topics:

| Topic | Content |
|-------|---------|
| `ets/sensors/lakeshore/rtd/A` | `{"value": <T_K>, "ts": ...}` |
| `ets/sensors/lakeshore/rtd/B` | `{"value": <T_K>, "ts": ...}` |
| `ets/sensors/lakeshore/rtd/C` | `{"value": <T_K>, "ts": ...}` |
| `ets/sensors/lakeshore/rtd/D` | `{"value": <T_K>, "ts": ...}` |
| `ets/sensors/lakeshore/heater/1` | `{"value": <percent>, "ts": ...}` |
| `ets/sensors/lakeshore/setpoint/1` | `{"value": <T_K>, "ts": ...}` |

Telegraf writes these to the `slowcontrol` InfluxDB bucket.  The field used
by the DAQ is controlled by `influxdb_rtd_field` in `ExperimentConfig`.

Typical config values for the DAQ:

```yaml
influxdb_url: "http://192.168.1.10:8086"
influxdb_org: "ets"
influxdb_bucket: "slowcontrol"
influxdb_rtd_field: "rtd/A"      # match the MQTT channel for the control RTD
mqtt_broker: "192.168.1.10"       # Pi address
mqtt_port: 1883
```

> **Note:** The `influxdb_rtd_field` value must match the Telegraf tag/field
> name as it appears in InfluxDB after ingestion.  Run a Flux query to
> confirm:
> ```flux
> from(bucket: "slowcontrol")
>   |> range(start: -5m)
>   |> filter(fn: (r) => r["driver"] == "lakeshore")
> ```

---

## Testing without hardware

The DAQ handles `NotImplementedError` gracefully.  If you keep the base
`SlowControl` class (without the MQTT subclass), the DAQ logs a warning and
waits for manual temperature adjustment.  This lets you test the DAQ
independently of the slow control system.

To test the MQTT path without a LakeShore, use `mosquitto_sub` on the Pi:

```bash
# Watch for setpoint commands
mosquitto_sub -t "ets/commands/lakeshore/setpoint" -v
```

Then trigger a setpoint from the DAQ side (or Python shell):

```python
from daq.lakeshore_slowcontrol import LakeShoreSlowControl
from daq.config import ExperimentConfig

cfg = ExperimentConfig(mqtt_broker="192.168.1.10")
sc = LakeShoreSlowControl(cfg)
sc.set_setpoint(165.0)
# You should see the message appear in mosquitto_sub
```

---

## Summary of changes required in ETS-pythonDAQ

| File | Change |
|------|--------|
| `daq/lakeshore_slowcontrol.py` | **New file** — `LakeShoreSlowControl` subclass (see above) |
| `daq/config.py` | Add `mqtt_broker` and `mqtt_port` fields to `ExperimentConfig` |
| `daq/gui/hub.py` | Import and instantiate `LakeShoreSlowControl` instead of `SlowControl` |
| `requirements.txt` / `environment.yml` | Add `paho-mqtt>=2.0` |

No changes to `daq/slowcontrol.py`, `daq/temppoint.py`, or any measurement code.
