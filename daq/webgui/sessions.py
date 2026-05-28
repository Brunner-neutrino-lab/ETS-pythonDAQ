"""
daq/webgui/sessions.py

Lightweight session registry for the web DAQ. Tracks who currently has the
control window open so a glance at the header shows "lucas (lab-pc-3), alice".

Self-declared display name only — stored in app.storage.browser (cookie-keyed
per browser). No authentication; this is a lab-internal app on a trusted
subnet. If the app is ever exposed beyond the lab, swap this out for real auth.
"""

from __future__ import annotations

import time

# Keyed by NiceGUI client.id (one entry per browser tab).
# Value: {"name": str, "ip": str, "since": float}
_SESSIONS: dict[str, dict] = {}


def register(client_id: str, name: str, ip: str) -> None:
    _SESSIONS[client_id] = {
        "name": (name or "").strip() or "anonymous",
        "ip": ip or "?",
        "since": time.time(),
    }


def unregister(client_id: str) -> None:
    _SESSIONS.pop(client_id, None)


def set_name(client_id: str, name: str) -> None:
    s = _SESSIONS.get(client_id)
    if s is not None:
        s["name"] = (name or "").strip() or "anonymous"


def active() -> list[dict]:
    """Snapshot of currently connected sessions (one per browser tab)."""
    return list(_SESSIONS.values())


def unique_users() -> list[tuple[str, str]]:
    """Distinct (name, ip) pairs across all open tabs, sorted by name."""
    seen = {(s["name"], s["ip"]) for s in _SESSIONS.values()}
    return sorted(seen)
