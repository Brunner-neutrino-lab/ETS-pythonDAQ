"""
daq/labbook.py

Lab book entries — JSONL is the source of truth; InfluxDB is an
optional mirror so notes can be overlaid against temperature/levels
data in Grafana.

Storage:
  <repo>/labbook_entries.jsonl    — newest entry appended per line
  <repo>/labbook_attachments/<f>  — uploaded files (images, plots)

InfluxDB schema (when mirror enabled):
  measurement: labbook
  tag:         user
  fields:      subject (str), body (str), attachments_csv (str),
               n_attachments (int)

The InfluxDB mirror reuses HUB.sc's open client; if slowcontrol is
not connected, the mirror is silently skipped and the JSONL file is
still the complete record.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

log = logging.getLogger(__name__)

_REPO_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENTRIES_PATH = os.path.join(_REPO_ROOT, "labbook_entries.jsonl")
_ATTACH_DIR   = os.path.join(_REPO_ROOT, "labbook_attachments")
os.makedirs(_ATTACH_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

def _write_jsonl(entry: dict) -> None:
    with open(_ENTRIES_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def list_all() -> list[dict]:
    """Return all entries, newest first."""
    if not os.path.exists(_ENTRIES_PATH):
        return []
    out: list[dict] = []
    with open(_ENTRIES_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(out))


# ---------------------------------------------------------------------------
# InfluxDB mirror
# ---------------------------------------------------------------------------

def _mirror_to_influx(entry: dict, slowcontrol) -> bool:
    """Mirror one entry into the slowcontrol bucket. Returns True on success.

    Failures are logged and swallowed — JSONL is the source of truth.
    `slowcontrol` is a SlowControl instance with `._client` open
    (i.e. HUB.sc after a successful connect_sc()).
    """
    if slowcontrol is None or getattr(slowcontrol, "_client", None) is None:
        return False
    try:
        from influxdb_client import Point
        from influxdb_client.client.write_api import SYNCHRONOUS

        bucket = slowcontrol._cfg.influxdb_bucket
        org    = slowcontrol._cfg.influxdb_org

        p = (Point("labbook")
             .tag("user", entry.get("user") or "anonymous")
             .field("subject", entry.get("subject") or "")
             .field("body", entry.get("body") or "")
             .field("attachments_csv",
                    ",".join(entry.get("attachments") or []))
             .field("n_attachments", len(entry.get("attachments") or []))
             .time(int(entry["ts"] * 1e9)))

        write_api = slowcontrol._client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=bucket, org=org, record=p)
        return True
    except Exception as e:
        log.warning("InfluxDB labbook mirror failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append(user: str, subject: str, body: str,
           attachments: list[str], slowcontrol=None) -> tuple[dict, bool]:
    """Append a new entry. Returns (entry_dict, mirrored_to_influx)."""
    entry = {
        "id":          uuid.uuid4().hex,
        "ts":          time.time(),
        "user":        user or "anonymous",
        "subject":     subject or "",
        "body":        body or "",
        "attachments": list(attachments or []),
    }
    _write_jsonl(entry)
    mirrored = _mirror_to_influx(entry, slowcontrol)
    return entry, mirrored


def save_attachment(name: str, content: bytes) -> str:
    """Save an attachment under labbook_attachments/; return the basename."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{uuid.uuid4().hex[:6]}_{safe}"
    with open(os.path.join(_ATTACH_DIR, fname), "wb") as f:
        f.write(content)
    return fname


def attachments_dir() -> str:
    return _ATTACH_DIR


# ---------------------------------------------------------------------------
# Clipboard-paste queue
#
# When the user pastes an image while the lab book tab is open, a JS handler
# POSTs the blob to /labbook-paste in webapp.py. That endpoint saves the
# file via save_attachment() and pushes the filename onto _paste_queue.
# The lab book tab's polling timer drains the queue every ~0.5 s and adds
# new filenames to the pending-attachments list. A module-level queue is
# fine because the DAQ is single-user in practice; if two browser tabs are
# open, both will pick up the same paste — harmless.
# ---------------------------------------------------------------------------

_paste_queue: list[str] = []


def queue_pasted(filename: str) -> None:
    _paste_queue.append(filename)


def pop_pasted() -> list[str]:
    out = list(_paste_queue)
    _paste_queue.clear()
    return out
