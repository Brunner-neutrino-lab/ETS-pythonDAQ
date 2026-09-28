"""
daq/rails.py

Which NGE100 channel powers which bench device, at what voltage and current
limit, and switching a device's rails together.

A mapping is {channel: {"device": key or "", "voltage": V, "current": A}}.
Lab defaults live in config.nge100_rails; edits from the web UI are saved to
<repo>/.nge100_rails.json and loaded at startup.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEVICES = {"cremat": "CSP/Shaper", "ivmux": "IV MUX"}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_REPO_ROOT, ".nge100_rails.json")


def load(default: dict) -> dict:
    """The saved mapping, or `default` if none has been saved."""
    try:
        with open(_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s (%s); using config defaults", _PATH, e)
        return default
    return {int(ch): rail for ch, rail in data.items()}


def save(rails: dict) -> None:
    with open(_PATH, "w") as f:
        json.dump({str(ch): rail for ch, rail in sorted(rails.items())}, f, indent=2)


def validate(rails: dict) -> None:
    """Raise ValueError unless every rail is within the NGE100's range."""
    from nge100.driver import IMAX, IMIN, VMAX, VMIN
    for ch, rail in sorted(rails.items()):
        if rail["device"] and rail["device"] not in DEVICES:
            raise ValueError(f"ch{ch}: unknown device {rail['device']!r}")
        if not VMIN <= rail["voltage"] <= VMAX:
            raise ValueError(f"ch{ch}: voltage must be {VMIN:g}-{VMAX:g} V")
        if not IMIN <= rail["current"] <= IMAX:
            raise ValueError(f"ch{ch}: current limit must be "
                             f"{IMIN * 1000:g}-{IMAX * 1000:g} mA")


def channels_for(rails: dict, device: str) -> list[int]:
    return sorted(ch for ch, rail in rails.items() if rail["device"] == device)


def power_on(psu, rails: dict, device: str) -> list[int]:
    """Set each of the device's rails to its voltage and current limit, then
    switch them on. The rails go on together or not at all: if any output
    fails to switch, every rail of the device is switched off again."""
    chans = channels_for(rails, device)
    if not chans:
        raise ValueError(f"no NGE100 channel is mapped to {DEVICES[device]}")
    for ch in chans:
        if not psu.apply(ch, rails[ch]["voltage"], rails[ch]["current"]):
            raise RuntimeError(f"ch{ch}: PSU did not accept the V/I setting")
    try:
        for ch in chans:
            if not psu.output_on(ch):
                raise RuntimeError(f"ch{ch}: PSU did not switch the output on")
    except Exception:
        for ch in chans:
            psu.output_off(ch)
        raise
    return chans


def power_off(psu, rails: dict, device: str) -> list[int]:
    chans = channels_for(rails, device)
    if not chans:
        raise ValueError(f"no NGE100 channel is mapped to {DEVICES[device]}")
    failed = [ch for ch in chans if not psu.output_off(ch)]
    if failed:
        raise RuntimeError("PSU did not switch off "
                           + ", ".join(f"ch{ch}" for ch in failed))
    return chans
