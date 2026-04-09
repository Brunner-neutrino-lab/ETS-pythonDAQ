"""
daq/slowcontrol.py

InfluxDB slow-control reader for cryostat temperature.

Reads RTD temperatures from the Raspberry Pi InfluxDB instance
(same DB used by Node-RED and query_db.py).  The DAQ uses this to:
  - Check the current temperature
  - Wait until a target temperature is stable before starting a measurement block

Temperature fields are in Celsius in the DB; all values returned here are
in Kelvin.

Usage
-----
    from daq.slowcontrol import SlowControl
    from daq.config import ExperimentConfig

    cfg = ExperimentConfig()
    sc  = SlowControl(cfg)

    # Single reading
    T_K = sc.temperature_K()
    print(f"Current temperature: {T_K:.2f} K")

    # Wait for 165 K ± 0.5 K, stable for 60 s
    sc.wait_for_stable(target_K=165.0, tolerance_K=0.5, stable_s=60.0)
"""

import time
import logging

log = logging.getLogger(__name__)

_CELSIUS_TO_KELVIN = 273.15


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

    def temperature_K(self) -> float:
        """
        Return the most recent RTD reading in Kelvin.

        Queries the last 30 seconds of data and returns the newest value.
        Raises RuntimeError if no data is returned.
        """
        field  = self._cfg.influxdb_rtd_field
        bucket = self._cfg.influxdb_bucket
        org    = self._cfg.influxdb_org

        flux = f"""
from(bucket: "{bucket}")
  |> range(start: -30s)
  |> filter(fn: (r) => r["_field"] == "{field}")
  |> last()
"""
        tables = self._api.query(flux, org=org)
        for table in tables:
            for record in table.records:
                val_c = record.get_value()
                if val_c is not None:
                    return float(val_c) + _CELSIUS_TO_KELVIN

        raise RuntimeError(
            f"No temperature data returned for field '{field}' "
            f"in bucket '{bucket}' (last 30 s). "
            f"Check InfluxDB connection and field name."
        )

    def all_rtds_K(self) -> dict[str, float]:
        """
        Return the most recent value for all RTD fields (RTD1_C – RTD4_C)
        as a dict of field_name → temperature_K.
        """
        bucket = self._cfg.influxdb_bucket
        org    = self._cfg.influxdb_org

        flux = f"""
from(bucket: "{bucket}")
  |> range(start: -30s)
  |> filter(fn: (r) => r["_measurement"] == "RTD")
  |> last()
"""
        result = {}
        tables = self._api.query(flux, org=org)
        for table in tables:
            for record in table.records:
                fname = record.get_field()
                val   = record.get_value()
                if val is not None:
                    result[fname] = float(val) + _CELSIUS_TO_KELVIN
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
