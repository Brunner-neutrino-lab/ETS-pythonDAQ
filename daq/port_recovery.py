"""
daq/port_recovery.py

Free a stuck serial port by killing same-user processes that have it open.

Used by the Connections-tab "connect" handler: when a serial.SerialException
with "Device or resource busy" / Errno 16 surfaces, we call
`free_serial_port("/dev/ttyUSB0")`, which runs `fuser` to find PIDs holding
the device and sends SIGTERM to those owned by the current UID. Processes
owned by other users are skipped — no sudo, no surprise reaping.

Only acts on paths under /dev/ — refuses to touch anything else.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

log = logging.getLogger(__name__)


def _is_busy_error(exc: BaseException) -> bool:
    """True if the exception looks like 'serial port is held by something else'."""
    s = str(exc).lower()
    if "device or resource busy" in s:
        return True
    if "[errno 16]" in s:
        return True
    if "busy" in s and "errno" in s:
        return True
    return False


def _pids_holding(path: str) -> list[int]:
    """Return PIDs holding `path` open, via `fuser`. Empty list on failure."""
    try:
        result = subprocess.run(
            ["fuser", path],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("fuser failed for %s: %s", path, e)
        return []
    pids: list[int] = []
    # fuser prints PIDs to stdout (space-separated) on Linux.
    for token in result.stdout.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def free_serial_port(path: str) -> tuple[bool, str]:
    """Try to free a busy serial port. Returns (success, human-readable message).

    "success" means at least one same-user PID was killed (and given a
    moment to die). Refuses to touch paths outside /dev/.
    """
    if not path or not path.startswith("/dev/"):
        return False, f"refusing to act on non-/dev path {path!r}"

    pids = _pids_holding(path)
    if not pids:
        return False, f"no process found holding {path}"

    my_uid = os.getuid()
    killed: list[int] = []
    skipped_other_user: list[int] = []
    for pid in pids:
        try:
            pid_uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        if pid_uid != my_uid:
            skipped_other_user.append(pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue

    if not killed:
        if skipped_other_user:
            return False, (f"{path} held by PIDs owned by other users "
                           f"({skipped_other_user}); refusing to kill")
        return False, f"could not signal any holder of {path}"

    # Brief grace period so the OS releases the file descriptor.
    time.sleep(0.6)

    msg = f"sent SIGTERM to PIDs {killed} holding {path}"
    if skipped_other_user:
        msg += f" (skipped other-user PIDs {skipped_other_user})"
    return True, msg
