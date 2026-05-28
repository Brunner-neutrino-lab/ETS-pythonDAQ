"""
daq/connection_state.py

Persist the most recently used connection address for each instrument so
the webapp can pre-populate the Connections tab on startup and the
"Connect all" button can bring the whole rig back up in one click.

Storage format (JSON at <repo>/.last_connections.json):

    {
      "elec":   {"address": "TCPIP::172.16.0.11::INSTR",
                 "last_connected": 1716745200.0},
      "mux":    {"address": "/dev/ttyUSB0",
                 "last_connected": 1716745205.0},
      ...
    }

`record_connect()` is called after every successful HUB.connect_*(),
`load_into_config()` is called once at webapp startup before the
Connections tab is built.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)

# Repo root, one level up from daq/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_PATH = os.path.join(_REPO_ROOT, ".last_connections.json")

# Instrument key → ExperimentConfig attribute name holding the address.
# The 'stage' has no single string address (it has three serials), so it
# is excluded from address persistence; connect-all will still try it.
_ADDR_ATTRS: dict[str, str] = {
    "elec":     "b2987b_visa",
    "dig":      "digitizer_address",
    "mux":      "mux_port",
    "k6485":    "k6485_port",
    "wfg":      "wfg_visa",
    "ks33500b": "ks33500b_visa",
    "nge100":   "nge100_resource",
    "sc":       "influxdb_url",
}


def _read(path: str = _DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read %s: %s", path, e)
        return {}


def _write(data: dict, path: str = _DEFAULT_PATH) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


def record_connect(instrument_key: str, address: str | None,
                   path: str = _DEFAULT_PATH) -> None:
    """Record a successful connection. Address may be None for instruments
    without a single string address (e.g. stage)."""
    data = _read(path)
    data[instrument_key] = {
        "address": address,
        "last_connected": time.time(),
    }
    _write(data, path)


def load_into_config(config, path: str = _DEFAULT_PATH) -> dict[str, str]:
    """Populate `config.<addr_attr>` for each persisted instrument.

    Returns a dict of {instrument_key: applied_address} for logging.
    """
    data = _read(path)
    applied: dict[str, str] = {}
    for key, info in data.items():
        attr = _ADDR_ATTRS.get(key)
        addr = info.get("address") if isinstance(info, dict) else None
        if attr and addr:
            setattr(config, attr, addr)
            applied[key] = addr
    return applied


def last_seen() -> dict[str, dict]:
    """Return the raw persisted dict (for UI display)."""
    return _read()
