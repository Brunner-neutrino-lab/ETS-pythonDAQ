"""
daq/slowcontrol.py

InfluxDB slow-control reader for cryostat temperature.

Reads RTD temperatures from the Raspberry Pi InfluxDB instance
(same DB used by Node-RED and query_db.py).  The DAQ uses this to:
  - Check the current temperature
  - Wait until a target temperature is stable before starting a measurement block

Stored RTD values may be in Celsius or Kelvin depending on the driver;
controlled by config.influxdb_rtd_in_kelvin. All values returned here are
in Kelvin.

Usage
-----
    from daq.slowcontrol import SlowControl
    from daq.config import ExperimentConfig

    cfg = ExperimentConfig()
    sc  = SlowControl(cfg)

    # Single reading of the LakeShore loop's PV
    ch, T_K = sc.pv_reading()
    print(f"Current temperature: {T_K:.2f} K (ch {ch})")

    # Wait for 165 K ± 0.5 K, stable for 60 s
    sc.wait_for_stable(target_K=165.0, tolerance_K=0.5, stable_s=60.0)
"""

import time
import logging

log = logging.getLogger(__name__)

_CELSIUS_TO_KELVIN = 273.15

# The slow control publishes each loop's OUTMODE input here, tag channel =
# loop number.
_PV_INPUT_MEASUREMENT = "outinput"
# LakeShore 336/350 OUTMODE input codes. 0 is "none"; 5-8 are the 3062
# scanner inputs D2-D5, which the slow control has no RTD series for.
_LAKESHORE_INPUTS = {1: "A", 2: "B", 3: "C", 4: "D"}


class SlowControl:
    """
    Cryostat temperature reader via InfluxDB.

    Parameters
    ----------
    config : ExperimentConfig
        Provides InfluxDB connection details and the RTD field name.
    """

    def __init__(self, config):
        self._cfg    = config
        self._client = None
        self._api    = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self):
        try:
            from influxdb_client import InfluxDBClient
        except ImportError:
            raise ImportError("pip install influxdb-client")

        token = self._cfg.resolved_influx_token()
        self._client = InfluxDBClient(
            url   = self._cfg.influxdb_url,
            token = token,
            org   = self._cfg.influxdb_org,
            timeout = 15_000,
        )
        self._api = self._client.query_api()
        log.info("SlowControl connected to %s", self._cfg.influxdb_url)

    def disconnect(self):
        if self._client is not None:
            self._client.close()
            self._client = None
            self._api    = None

    # ------------------------------------------------------------------
    # Setpoint control
    # ------------------------------------------------------------------

    def set_setpoint(self, T_K: float):
        """
        Command the temperature controller to move to T_K.

        This is a stub.  Implement it in the slow control system by
        subclassing SlowControl or monkey-patching this method.

        The DAQ calls this at the start of each temperature point
        (before wait_for_stable).  If the slow control system does not
        support setpoint control, raise NotImplementedError and the DAQ
        will log a warning and continue (assuming the operator sets the
        temperature manually).

        Parameters
        ----------
        T_K : float
            Target temperature in Kelvin.
        """
        raise NotImplementedError(
            "set_setpoint() is not implemented. "
            "Implement this method in the slow control system to enable "
            "automated temperature control. "
            "See docs/daq_slowcontrol_interface.md for the required interface."
        )

    # ------------------------------------------------------------------
    # Single temperature query
    # ------------------------------------------------------------------

    def _to_kelvin(self, value: float) -> float:
        if self._cfg.influxdb_rtd_in_kelvin:
            return float(value)
        return float(value) + _CELSIUS_TO_KELVIN

    def pv_reading(self) -> tuple[str, float]:
        """
        Return (channel, temperature_K) for the process variable of LakeShore
        control loop `influxdb_pv_loop`.

        The PV channel is not configured here: it is the loop's control input
        as published by the slow control, so changing the control sensor
        there moves the DAQ's temperature to the new sensor. Both the input
        and the RTD values come from the last 30 s in one query.
        """
        bucket      = self._cfg.influxdb_bucket
        org         = self._cfg.influxdb_org
        measurement = self._cfg.influxdb_rtd_measurement
        field       = self._cfg.influxdb_rtd_field
        loop        = self._cfg.influxdb_pv_loop

        flux = f"""
from(bucket: "{bucket}")
  |> range(start: -30s)
  |> filter(fn: (r) =>
       (r["_measurement"] == "{measurement}" and r["_field"] == "{field}") or
       (r["_measurement"] == "{_PV_INPUT_MEASUREMENT}" and r["_field"] == "value"
        and r["channel"] == "{loop}"))
  |> last()
"""
        code = None
        rtds: dict[str, float] = {}
        for table in self._api.query(flux, org=org):
            for record in table.records:
                val = record.get_value()
                if val is None:
                    continue
                if record.get_measurement() == _PV_INPUT_MEASUREMENT:
                    code = int(val)
                else:
                    rtds[str(record.values.get("channel"))] = val

        if code is None:
            raise RuntimeError(
                f"No '{_PV_INPUT_MEASUREMENT}' for LakeShore loop {loop} in "
                f"bucket '{bucket}' (last 30 s), so the PV channel is unknown. "
                f"Check that the slow control is running."
            )
        channel = _LAKESHORE_INPUTS.get(code)
        if channel is None:
            raise RuntimeError(
                f"LakeShore loop {loop} control input is {code} "
                f"({'none' if code == 0 else 'not one of A-D'}): no RTD PV to read."
            )
        if channel not in rtds:
            raise RuntimeError(
                f"No temperature data for PV channel {channel} "
                f"(_measurement='{measurement}', _field='{field}') in bucket "
                f"'{bucket}' (last 30 s)."
            )
        return channel, self._to_kelvin(rtds[channel])

    def temperature_K(self) -> float:
        """Return the PV temperature in Kelvin (see pv_reading)."""
        return self.pv_reading()[1]

    def all_rtds_K(self) -> dict[str, float]:
        """
        Return the most recent value for every channel of the configured RTD
        measurement, as a dict of channel-tag → temperature_K.
        """
        bucket      = self._cfg.influxdb_bucket
        org         = self._cfg.influxdb_org
        measurement = self._cfg.influxdb_rtd_measurement
        field       = self._cfg.influxdb_rtd_field

        flux = f"""
from(bucket: "{bucket}")
  |> range(start: -30s)
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
  |> filter(fn: (r) => r["_field"] == "{field}")
  |> last()
"""
        result: dict[str, float] = {}
        tables = self._api.query(flux, org=org)
        for table in tables:
            for record in table.records:
                ch  = record.values.get("channel")
                val = record.get_value()
                if ch is not None and val is not None:
                    result[str(ch)] = self._to_kelvin(val)
        return result

    # ------------------------------------------------------------------
    # Stability wait
    # ------------------------------------------------------------------

    def wait_for_stable(self,
                        target_K:    float,
                        tolerance_K: float = 0.5,
                        stable_s:    float = 60.0,
                        poll_s:      float = 5.0,
                        timeout_s:   float = 7200.0,
                        on_update=None):
        """
        Block until the temperature has been within target_K ± tolerance_K
        continuously for stable_s seconds.

        Parameters
        ----------
        target_K : float
            Target temperature in Kelvin.
        tolerance_K : float
            Acceptable deviation from target (K). Default 0.5 K.
        stable_s : float
            How long the temperature must remain within tolerance (s).
        poll_s : float
            Query interval (s). Default 5 s.
        timeout_s : float
            Maximum total wait time (s). Raises TimeoutError if exceeded.
        on_update : callable, optional
            Called every poll cycle as on_update(current_K, stable_elapsed_s).
            Use for GUI progress updates.
        """
        log.info(
            "Waiting for %.1f K ± %.2f K, stable for %.0f s",
            target_K, tolerance_K, stable_s,
        )
        deadline     = time.monotonic() + timeout_s
        stable_since = None

        while time.monotonic() < deadline:
            try:
                T = self.temperature_K()
            except Exception as e:
                log.warning("Temperature query failed: %s — retrying", e)
                time.sleep(poll_s)
                continue

            in_band = abs(T - target_K) <= tolerance_K

            if in_band:
                if stable_since is None:
                    stable_since = time.monotonic()
                    log.info("Temperature %.2f K entered band — starting stability timer", T)
                elapsed = time.monotonic() - stable_since
            else:
                if stable_since is not None:
                    log.info(
                        "Temperature %.2f K left band (target %.1f K ± %.2f K) — resetting timer",
                        T, target_K, tolerance_K,
                    )
                stable_since = None
                elapsed = 0.0

            log.debug("T=%.3f K  stable_elapsed=%.0f / %.0f s", T, elapsed, stable_s)

            if on_update is not None:
                on_update(T, elapsed)

            if in_band and elapsed >= stable_s:
                log.info("Temperature stable at %.2f K — proceeding", T)
                return

            time.sleep(poll_s)

        raise TimeoutError(
            f"Timed out waiting for temperature to stabilise at "
            f"{target_K:.1f} K ± {tolerance_K:.2f} K "
            f"for {stable_s:.0f} s (timeout={timeout_s:.0f} s)"
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
