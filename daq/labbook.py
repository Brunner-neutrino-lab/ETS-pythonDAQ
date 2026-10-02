"""
daq/labbook.py

Lab book entries — JSONL is the source of truth; InfluxDB is an
optional mirror so notes can be overlaid against temperature/levels
data in Grafana.

Storage:
  <repo>/labbook_entries.jsonl    — one entry per line, oldest first
  <repo>/labbook_history.jsonl    — the previous version of every edited
                                    or deleted entry, one line per change
  <repo>/labbook_attachments/<f>  — uploaded files (images, plots)

Editing or deleting never removes anything for good: the old entry goes
to the history file and attachment files are left on disk, so either can
be undone by hand.

InfluxDB schema (when mirror enabled):
  measurement: labbook
  tag:         user
  fields:      subject (str), body (str), attachments_csv (str),
               n_attachments (int)

An edit keeps the entry's `ts` and `user`, so its mirror write lands on
the same point (same measurement, tag and timestamp) and overwrites it.

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
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_REPO_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENTRIES_PATH = os.path.join(_REPO_ROOT, "labbook_entries.jsonl")
_HISTORY_PATH = os.path.join(_REPO_ROOT, "labbook_history.jsonl")
_ATTACH_DIR   = os.path.join(_REPO_ROOT, "labbook_attachments")
os.makedirs(_ATTACH_DIR, exist_ok=True)

# Counts changes to the entries file. Each open lab book tab compares it
# with the value it last rendered, to notice another browser's changes.
_revision = 0


def revision() -> int:
    return _revision


def _changed() -> None:
    global _revision
    _revision += 1


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

def _write_jsonl(entry: dict) -> None:
    with open(_ENTRIES_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _replace_line(entry_id: str, replacement: dict | None) -> None:
    """Rewrite the entries file with one entry replaced, or dropped when
    `replacement` is None.

    Every other line is carried over untouched, including lines that do
    not parse, so a rewrite cannot lose what list_all() merely skips.
    """
    out: list[str] = []
    with open(_ENTRIES_PATH, "r") as f:
        for line in f:
            try:
                is_target = json.loads(line).get("id") == entry_id
            except json.JSONDecodeError:
                is_target = False
            if not is_target:
                out.append(line)
            elif replacement is not None:
                out.append(json.dumps(replacement) + "\n")
    tmp = _ENTRIES_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(out)
    os.replace(tmp, _ENTRIES_PATH)


def _write_history(action: str, by: str, entry: dict) -> None:
    with open(_HISTORY_PATH, "a") as f:
        f.write(json.dumps({"action": action, "at": time.time(),
                            "by": by or "anonymous", "entry": entry}) + "\n")


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


def get(entry_id: str) -> dict | None:
    for entry in list_all():
        if entry.get("id") == entry_id:
            return entry
    return None


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


def _delete_from_influx(entry: dict, slowcontrol) -> bool:
    """Delete one entry's point from the slowcontrol bucket. Returns True
    on success; failures are logged and swallowed like the mirror write.

    The delete API takes a time range, not a point: the 2 ms window around
    the entry's timestamp cannot hold a second hand-written entry, and the
    predicate keeps every other measurement in the bucket out of it.
    """
    if slowcontrol is None or getattr(slowcontrol, "_client", None) is None:
        return False
    try:
        at = datetime.fromtimestamp(entry["ts"], timezone.utc)
        slowcontrol._client.delete_api().delete(
            start=at - timedelta(milliseconds=1),
            stop=at + timedelta(milliseconds=1),
            predicate='_measurement="labbook"',
            bucket=slowcontrol._cfg.influxdb_bucket,
            org=slowcontrol._cfg.influxdb_org,
        )
        return True
    except Exception as e:
        log.warning("InfluxDB labbook delete failed: %s", e)
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
    _changed()
    mirrored = _mirror_to_influx(entry, slowcontrol)
    return entry, mirrored


def update(entry_id: str, subject: str, body: str, attachments: list[str],
           edited_by: str, slowcontrol=None) -> tuple[dict | None, bool]:
    """Replace the subject, body and attachments of a posted entry.

    Returns (entry_dict, mirrored_to_influx); entry_dict is None when no
    entry has that id (someone else deleted it meanwhile).
    """
    old = get(entry_id)
    if old is None:
        return None, False
    entry = dict(old)
    entry.update(
        subject=subject or "",
        body=body or "",
        attachments=list(attachments or []),
        edited_ts=time.time(),
        edited_by=edited_by or "anonymous",
    )
    _write_history("edit", edited_by, old)
    _replace_line(entry_id, entry)
    _changed()
    mirrored = _mirror_to_influx(entry, slowcontrol)
    return entry, mirrored


def delete(entry_id: str, deleted_by: str,
           slowcontrol=None) -> tuple[dict | None, bool]:
    """Remove a posted entry.

    Returns (deleted_entry, removed_from_influx); deleted_entry is None
    when no entry has that id.
    """
    old = get(entry_id)
    if old is None:
        return None, False
    _write_history("delete", deleted_by, old)
    _replace_line(entry_id, None)
    _changed()
    removed = _delete_from_influx(old, slowcontrol)
    return old, removed


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
# POSTs the blob to /labbook-paste in webapp.py, together with the id of
# the NiceGUI client (the browser page) it was pasted into. That endpoint
# saves the file via save_attachment() and queues the filename under that
# client id. Each page's polling timer drains its own queue every ~0.5 s
# and adds the filenames to its pending-attachments list.
#
# One queue per client, because every open page polls: from a single
# shared queue a paste goes to whichever page polls first, which with
# two people logged in is often the other person's.
# ---------------------------------------------------------------------------

_paste_queues: dict[str, list[str]] = {}


def queue_pasted(client_id: str, filename: str) -> None:
    _paste_queues.setdefault(client_id, []).append(filename)


def pop_pasted(client_id: str) -> list[str]:
    return _paste_queues.pop(client_id, [])
