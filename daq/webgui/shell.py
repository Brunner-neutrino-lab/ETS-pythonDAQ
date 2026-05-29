"""
daq/webgui/shell.py

NiceGUI shell for the nEXO SiPM DAQ. MVP scope: Connections, Config,
Level 1 (primitives), and Level 2 (single-SiPM).

Styled to match _legacy/xsphere-slow-control/webcontrol — dark "register"
theme, status pills, accent-blue panel headers, compact layout.
The layered API in daq/ (primitives → measurement → tile → temppoint →
run) is shared with the PyQt5 GUI in daq/gui/.

Entry point: `python -m daq.webapp` (see daq/webapp.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Callable

from nicegui import app, ui

# Make instrument submodules importable when running from the repo root
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _pkg in ("keysight2987b-python", "keithley6485-python", "phidget-stage-python",
             "pulse-mux-python", "RTO2024-python", "vx2740-python",
             "rigoldg1022-python", "r-snge100-python", "keysight33500b-python"):
    _p = os.path.join(_REPO, _pkg)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from daq.config import ExperimentConfig
from daq.gui.hub import InstrumentHub
from daq import primitives as P
from daq import measurement as M
from daq import measurement_store as MSTORE
from daq import h5io
from daq.webgui import sessions as SESSIONS
from daq import connection_state
from daq import labbook
from daq import port_recovery

log = logging.getLogger("daq.webgui")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

HUB = InstrumentHub()


# Current measurement state for the status page. Set by measurement runners
# (L1–L5, raster, alignment) at the start of a run and cleared when finished;
# the status tab reads this every ~1 s to show "what is the experiment doing
# right now?". `detail` is a short free-text line shown beneath the name.
ACTIVITY: dict = {"name": None, "started": None, "detail": ""}


def set_activity(name: str, detail: str = "") -> None:
    ACTIVITY["name"] = name
    ACTIVITY["started"] = time.time()
    ACTIVITY["detail"] = detail


def clear_activity() -> None:
    ACTIVITY["name"] = None
    ACTIVITY["started"] = None
    ACTIVITY["detail"] = ""


# Last known bias setpoint (V) + measured current (A) — measurement runners
# poke these whenever they touch the B2987 so the status page can display
# the most recent values without re-querying the instrument (which would
# interfere with a running sweep).
BIAS_STATE: dict = {"v_set": None, "i_meas": None, "output_on": None, "ts": None}


def note_bias(v_set: float | None = None,
              i_meas: float | None = None,
              output_on: bool | None = None) -> None:
    if v_set    is not None: BIAS_STATE["v_set"]     = v_set
    if i_meas   is not None: BIAS_STATE["i_meas"]    = i_meas
    if output_on is not None: BIAS_STATE["output_on"] = output_on
    BIAS_STATE["ts"] = time.time()


# Ring buffer of recent log records so the status page can show a tail of
# what's happening across the app. A custom logging.Handler appended once
# at import time captures records from all daq.* loggers.
_LOG_RING: list[tuple[float, str, str, str]] = []  # (ts, level, name, msg)
_LOG_RING_MAX = 200


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_RING.append((record.created, record.levelname,
                              record.name, record.getMessage()))
            if len(_LOG_RING) > _LOG_RING_MAX:
                del _LOG_RING[: len(_LOG_RING) - _LOG_RING_MAX]
        except Exception:
            pass


_ring_handler = _RingHandler(level=logging.INFO)
logging.getLogger("daq").addHandler(_ring_handler)


_INSTRUMENT_SPECS = [
    {"key": "elec",  "name": "b2987",     "addr_attr": "b2987b_visa",
     "addr_label": "VISA",  "connect": "connect_elec",  "disconnect": "disconnect_elec"},
    {"key": "dig",   "name": "digitizer", "addr_attr": "digitizer_address",
     "addr_label": "addr", "connect": "connect_dig",   "disconnect": "disconnect_dig"},
    # Mux temporarily out of "connect all": the Arduino occasionally wedges
    # after a hard webapp kill (port opens fine but no command response),
    # which silently turns the pill green and then hangs the first real
    # operation for 10 s. Connect from the Connections tab when needed.
    {"key": "mux",   "name": "mux",       "addr_attr": "mux_port",
     "addr_label": "port", "connect": "connect_mux",   "disconnect": "disconnect_mux",
     "hidden": True},
    {"key": "k6485", "name": "k6485",     "addr_attr": "k6485_port",
     "addr_label": "VISA", "connect": "connect_k6485", "disconnect": "disconnect_k6485"},
    # Keysight 33500B = the visible WFG in the header / connect-all.
    {"key": "ks33500b", "name": "wfg (33500b)", "addr_attr": "ks33500b_visa",
     "addr_label": "VISA / device", "connect": "connect_ks33500b",
     "disconnect": "disconnect_ks33500b"},
    # Rigol DG1022 stays in the codebase (bench scripts still use HUB.wfg)
    # but is hidden from the header status pills and the "connect all"
    # flow.  Its Connections-tab card is still rendered so it can be
    # connected on demand.  Flip "hidden" to False to bring it back.
    {"key": "wfg",   "name": "wfg (dg1022)", "addr_attr": "wfg_visa",
     "addr_label": "VISA / device", "connect": "connect_wfg",
     "disconnect": "disconnect_wfg", "hidden": True},
    {"key": "nge100","name": "nge100 (mux PSU)", "addr_attr": "nge100_resource",
     "addr_label": "VISA / device", "connect": "connect_nge100", "disconnect": "disconnect_nge100"},
    {"key": "stage", "name": "stage",     "addr_attr": None,
     "addr_label": "serials","connect": "connect_stage", "disconnect": "disconnect_stage"},
    {"key": "sc",    "name": "slow ctrl", "addr_attr": "influxdb_url",
     "addr_label": "URL",  "connect": "connect_sc",    "disconnect": "disconnect_sc"},
]


def _visible_specs() -> list[dict]:
    """Specs that should appear in the header / connect-all flow.  An entry
    marked `"hidden": True` is excluded — the Connections tab still renders
    its card (so the instrument is reachable manually) but it doesn't clutter
    the top bar and isn't connected by the bulk "connect all" buttons."""
    return [s for s in _INSTRUMENT_SPECS if not s.get("hidden", False)]


# ---------------------------------------------------------------------------
# Style — port of _legacy/xsphere-slow-control/webcontrol theme
# ---------------------------------------------------------------------------

_XSPHERE_CSS = """
:root {
  --bg:#11151c; --panel:#1b2230; --panel2:#232c3d;
  --fg:#dde3ee; --mut:#8a93a6;
  --ok:#3fb950; --warn:#d29922; --bad:#f85149; --acc:#58a6ff;
  --line:#2d3648;
  /* Quasar overrides */
  --q-primary:   #58a6ff;
  --q-secondary: #232c3d;
  --q-dark:      #1b2230;
  --q-dark-page: #11151c;
}
* { box-sizing:border-box; }
html, body, .nicegui-content { background:var(--bg) !important; color:var(--fg);
  font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; }

/* Sticky header strip with title + status pills.
   Hugs the content vertically — no extra padding, button/pills constrained
   so nothing pushes the row taller than the text height. */
.daq-header {
  display:flex; align-items:center; gap:.5rem; flex-wrap:wrap;
  padding:2px 12px !important; margin:0;
  background:var(--panel); border-bottom:1px solid var(--line);
  position:sticky; top:0; z-index:5; line-height:1;
}
.daq-header > * { line-height:1.15; }
.daq-header h1 { font-size:.9rem; margin:0 .3rem 0 0; font-weight:600;
  letter-spacing:.3px; color:var(--fg); line-height:1.15; }
.daq-header .sub { color:var(--mut); font-size:.74rem; }
.daq-header .q-btn.estop {
  padding:0 .55rem !important; min-height:20px !important; height:20px !important;
  font-size:.7rem !important; letter-spacing:.3px;
  box-shadow:0 0 0 1px rgba(248,81,73,.25) !important;
}
.daq-header .q-btn.estop .q-btn__content { min-height:20px !important; padding:0 !important; }
.daq-header .pill { padding:1px .5rem; }

/* Footer strip — page-level notes and links */
.daq-footer {
  display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
  padding:.45rem 1rem; background:var(--panel); border-top:1px solid var(--line);
  margin-top:1rem; color:var(--mut); font-size:.76rem;
}
.daq-footer a { color:var(--acc); text-decoration:none; }
.daq-footer a:hover { text-decoration:underline; }
.daq-footer .spacer { flex:1; }
.daq-footer code { color:var(--fg); background:var(--panel2); padding:0 .25rem;
                   border-radius:3px; font-size:.72rem; }
.pill { padding:.15rem .55rem; border-radius:999px; font-size:.78rem;
  font-weight:600; white-space:nowrap; display:inline-flex; align-items:center; gap:.3rem; }
.pill.pill-clickable { transition: filter .12s ease, transform .12s ease; }
.pill.pill-clickable:hover { filter: brightness(1.35); transform: translateY(-1px); }
.pill.ok   { background:rgba(63,185,80,.18);  color:var(--ok); }
.pill.bad  { background:rgba(248,81,73,.18);  color:var(--bad); }
.pill.warn { background:rgba(210,153,34,.18); color:var(--warn); }
.pill.mut  { background:rgba(138,147,166,.15);color:var(--mut); }
.dot { width:.55rem; height:.55rem; border-radius:50%; background:currentColor; display:inline-block; }

/* Tabs (Quasar overrides) — strip is hidden in favor of the menu bar
   below; we still need q-tab-panel styling for the panel content. */
.hidden-tabs { display: none !important; }
.q-tab-panel { background:var(--bg) !important; padding:.6rem !important; }

/* Header-inline dropdown buttons (settings / instruments / measurements).
   Sit between the title and the BIAS OFF / status pills. */
.daq-header .menu-btn {
  background: transparent !important; border: none !important;
  color: var(--fg) !important; padding: 0 .55rem !important;
  min-height: 20px !important; height: 20px !important;
  font-size: .72rem !important; letter-spacing: .6px;
  text-transform: uppercase !important; font-weight: 600 !important;
  box-shadow: none !important; border-radius: 4px !important;
}
.daq-header .menu-btn:hover {
  background: var(--panel2) !important; color: var(--acc) !important;
}
.daq-header .menu-btn .q-btn__content { min-height: 20px !important; padding: 0 !important; }
.daq-header .hdr-plots-btn {
  color: var(--acc) !important; font-weight: 700 !important;
}
.daq-header .hdr-plots-btn:hover {
  background: rgba(88,166,255,.12) !important;
}
.daq-header .hdr-connect-all {
  min-height: 20px !important; height: 20px !important;
  padding: 0 .55rem !important; font-size: .7rem !important;
  letter-spacing: .3px;
}
.daq-header .hdr-connect-all .q-btn__content { min-height: 20px !important; padding: 0 !important; }
.q-menu {
  background: var(--panel) !important; border: 1px solid var(--line);
  box-shadow: 0 4px 12px rgba(0,0,0,.45) !important; border-radius: 4px;
  min-width: 180px;
}
.q-menu .q-item {
  color: var(--fg) !important; min-height: 28px !important;
  padding: 0 .9rem !important; font-size: .8rem !important;
}
.q-menu .q-item:hover {
  background: var(--panel2) !important; color: var(--acc) !important;
}

/* (Old multi-row tab-group / tab-row-break styles removed — replaced by
   the .menu-bar dropdown navigation above.) */

/* Status page widget tweaks */
.status-grid { display: grid; gap: .6rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.status-top { padding: .15rem .65rem !important; line-height: 1.2; }
.status-top .nicegui-row, .status-top > .row {
  padding: 0 !important; margin: 0 !important;
  gap: .35rem !important; flex-wrap: nowrap !important;
}
.status-top .big { font-size: .9rem; font-weight: 700; color: var(--fg); }
.status-top .num { font-variant-numeric: tabular-nums; font-size: .85rem; color: var(--fg); }
.status-top .lbl { color: var(--mut); font-size: .65rem; letter-spacing: .6px;
                   text-transform: uppercase; }
.status-top .sep { color: var(--line); margin: 0 .1rem; }
.conn-card { padding: .3rem .55rem !important; gap: .15rem; }
.conn-card .conn-name { font-size: .82rem; font-weight: 600; color: var(--acc); }
.conn-card .conn-status { color: var(--mut); white-space: nowrap; overflow: hidden;
                          text-overflow: ellipsis; max-width: 60%; }
.conn-card .q-field--dense .q-field__control { min-height: 26px !important; height: 26px; }
.conn-card .q-field--dense .q-field__native { padding: 0 !important; font-size: .78rem; }
.conn-card .q-btn.conn-btn { min-height: 24px !important; padding: 0 .45rem !important;
                             font-size: .72rem !important; }
.status-card .big { font-size: 1.3rem; font-weight: 700; color: var(--fg); }
.status-card .num { font-variant-numeric: tabular-nums; }
.status-card .lbl { color: var(--mut); font-size: .78rem; letter-spacing: .3px; text-transform: uppercase; }
.thumb-strip { display: flex; gap: .4rem; flex-wrap: wrap; }
.thumb-strip a img { height: 88px; border:1px solid var(--line); border-radius: 4px;
                     background:#000; display:block; }
.thumb-strip a img:hover { border-color: var(--acc); }

/* Stage tab — controls on the left, live webcam on the right so the
   operator can watch the rig while jogging the stage. Stacks under
   ~900 px so the webcam doesn't crowd the controls on narrow screens. */
.stage-with-cam { display:grid; gap:.5rem; grid-template-columns:1fr; }
@media (min-width: 900px) {
  .stage-with-cam { grid-template-columns: 3fr 2fr; }
}
.stage-with-cam .stage-cam-card { padding:.4rem .55rem !important; }
.stage-with-cam .stage-cam-card .lbl {
  font-size:.7rem; letter-spacing:.5px; color:var(--mut);
  text-transform:uppercase; display:block; margin-bottom:.3rem;
}

/* Embedded per-instrument GUIs: the external packages each render an
   internal `ui.log(...)` widget with `h-32` (~128 px) at the top of their
   page. When the user hasn't done anything yet that's just a tile of dead
   space — collapse it here so the actual controls sit right under the
   page intro. The log still works; it just starts small and can scroll. */
.instr-embed .q-log, .instr-embed .nicegui-log {
  height: 4rem !important; min-height: 0 !important;
}
/* Sibling-package GUIs use big <h2>/<h3> titles that look out of place
   inside the DAQ shell. Match the rest of the app's compact register feel. */
.instr-embed h1, .instr-embed h2, .instr-embed h3 {
  font-size: .85rem !important; font-weight: 600 !important;
  margin: .1rem 0 .25rem !important; color: var(--acc) !important;
  letter-spacing: .3px;
}
.instr-embed .q-card { padding: .35rem .55rem !important; }
.instr-embed .q-tab { min-height: 24px !important; font-size: .78rem !important; }
.instr-embed .q-field--filled .q-field__control { min-height: 28px !important; }

/* Custom DAQ-side control panel — dense, no-scroll, plot-at-top.
   The whole electrometer page is meant to fit a single viewport so
   the operator can see everything at a glance. */
.ctrl-grid { display: grid; gap: .4rem;
             grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
.ctrl-card { padding: .25rem .5rem .35rem !important; }
.ctrl-card h3 { font-size: .65rem !important; letter-spacing: 1px;
                text-transform: uppercase; color: var(--acc) !important;
                font-weight: 700; margin: 0 0 .2rem !important; }
.ctrl-field { display: flex; flex-direction: column; gap: 0;
              margin-bottom: .15rem; }
.ctrl-field .badge { align-self: flex-start; margin: -1px 0 0 .25rem; }
.ctrl-card .nicegui-input, .ctrl-card .nicegui-number,
.ctrl-card .nicegui-select { width: 100%; }
.ctrl-card .q-field--filled .q-field__control { min-height: 26px !important; height: 26px; }
.ctrl-card .q-field__native, .ctrl-card .q-field__input {
  padding: 0 !important; font-size: .78rem;
}
.ctrl-card .q-field__label { font-size: .72rem !important; }
.ctrl-card .q-btn { min-height: 22px !important; padding: 0 .55rem !important;
                    font-size: .72rem !important; }
.ctrl-card .q-toggle, .ctrl-card .q-toggle__label { font-size: .78rem !important; }
.ctrl-row-btns { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .15rem; }
.ctrl-readout { font-variant-numeric: tabular-nums; font-size: .85rem;
                color: var(--fg); display: block; margin: 0 0 .15rem; }
.ctrl-readout .lbl { color: var(--mut); font-size: .65rem;
                     letter-spacing: .5px; text-transform: uppercase; }
.badge { color: var(--ok); font-variant-numeric: tabular-nums;
         font-size: .68rem; padding: 0 .35rem; background: var(--panel2);
         border-radius: 3px; border: 1px solid var(--line); white-space: nowrap; }
.badge.mut { color: var(--mut); }
.idn-strip { padding: .12rem .5rem; background: var(--panel2);
             border: 1px solid var(--line); border-radius: 4px;
             font-size: .72rem; color: var(--mut); margin-bottom: .25rem;
             display: flex; align-items: center; gap: .5rem; }
.idn-strip .num { color: var(--fg); }
.iv-plot { width: 100%; height: 230px; background: var(--panel);
           border: 1px solid var(--line); border-radius: 4px; }
.sweep-controls { display: flex; align-items: end; gap: .35rem;
                  flex-wrap: nowrap; width: 100%; margin-top: .15rem; }
.sweep-controls .nicegui-number { flex: 0 0 70px; }

/* =====================================================================
   Electrometer v2 — label-above, unit-suffix, monospace, no floating
   labels, staged-vs-applied dirty marker. Used only by
   _build_electrometer_tab(); other panels still use the older ctrl-*.
   ===================================================================== */

/* Slim connection bar */
.idn-bar { display:flex; align-items:center; gap:.5rem;
           padding:.2rem .6rem; background:var(--panel2);
           border:1px solid var(--line); border-radius:4px;
           font-size:.74rem; color:var(--mut); margin-bottom:.4rem; }
.idn-bar .num { color:var(--fg); font-family:ui-monospace,Menlo,Consolas,monospace; }

/* Top zone: hero readout + output toggle (side by side on wide screens) */
.elec-top-row { display:grid; gap:.5rem; grid-template-columns:1fr;
                margin-bottom:.5rem; }
@media (min-width: 900px) {
  .elec-top-row { grid-template-columns: 2fr 1fr; }
}

/* Hero readout — compact, I and V side-by-side. */
.hero-card { padding:.3rem .55rem !important; }
.hero-card .lbl { font-size:.62rem; letter-spacing:.6px;
                  text-transform:uppercase; color:var(--mut);
                  display:block; }
.hero-row-2 { display:grid; grid-template-columns:1fr 1fr;
              gap:.6rem; margin-bottom:.25rem; }
.hero-val { font-family:ui-monospace,Menlo,Consolas,monospace;
            font-size:1.55rem; font-weight:600; color:var(--fg);
            line-height:1.05; letter-spacing:.3px;
            font-variant-numeric: tabular-nums; display:block; }
.hero-val.dim { color:var(--mut); font-size:1.2rem; }
.hero-unit { color:var(--mut); margin-left:.25rem;
             font-family:ui-monospace,Menlo,Consolas,monospace;
             font-size: .58em; }

/* Output card — tight: state pill on top, level field + actions below. */
.output-card { padding:.3rem .55rem !important;
               display:flex; flex-direction:column; gap:.3rem; }
.output-card .lbl { font-size:.62rem; letter-spacing:.6px;
                    text-transform:uppercase; color:var(--mut);
                    display:block; }
.output-pill {
  display:flex; align-items:center; justify-content:center;
  padding:.5rem .8rem; font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:1rem; font-weight:700; letter-spacing:1.3px;
  border:2px solid var(--mut); border-radius:6px;
  background:var(--panel2); color:var(--mut);
  user-select:none;
}
.output-pill.is-on {
  background:var(--bad); color:#fff; border-color:#ff8a82;
  box-shadow:0 0 0 2px rgba(248,81,73,.25);
}
.output-pill.is-disabled { opacity:.5; }
.output-row { display:flex; align-items:center; gap:.35rem; }
.output-row > .v2-field { flex:1 1 auto; margin-bottom:0; }
.output-row .q-btn { padding:0 .55rem !important; min-height:30px !important;
                     font-size:.72rem !important; }

/* Merged SOURCE+OUTPUT and MEASURE+READOUT cards laid out as
   "short rectangles": a 2-col internal grid (.tile-cols) with the
   heading spanning full width. Compact font sizes everywhere. */
.source-merged, .measure-merged {
  padding:.25rem .55rem .3rem !important;
}
.source-merged h3, .measure-merged h3 {
  margin: 0 0 .2rem !important;
}
.tile-cols {
  display:grid; gap:.35rem .8rem;
  grid-template-columns:1fr; align-items:start;
}
@media (min-width: 600px) {
  .tile-cols { grid-template-columns:1fr 1fr; }
}
.tile-col { display:flex; flex-direction:column; gap:.05rem; }
.source-merged .lbl, .measure-merged .lbl {
  font-size:.58rem; letter-spacing:.5px;
  text-transform:uppercase; color:var(--mut); display:block;
  line-height:1.1;
}
.source-merged .v2-field, .measure-merged .v2-field { margin-bottom:.1rem; }
.source-merged .output-pill {
  padding:.18rem .5rem; font-size:.74rem; letter-spacing:1px;
  border-width:1px; border-radius:4px;
  margin-bottom:.2rem;
}
.source-merged .output-row { margin:.1rem 0; }
.source-merged .apply-btn, .measure-merged .apply-btn {
  margin-top:.2rem; align-self:flex-start;
}
.measure-merged .hero-val { font-size:1rem; line-height:1.05; }
.measure-merged .hero-val.dim { font-size:.85rem; }
.measure-merged .hero-unit { font-size:.55em; }
.measure-merged .v2-field .q-field--filled .q-field__control,
.source-merged   .v2-field .q-field--filled .q-field__control {
  min-height:26px !important; height:26px;
}

/* ============================================================
   Electrometer panel — Quick-I/V target layout. Scoped to .elec-panel
   so it doesn't leak into other instrument pages.
   ============================================================ */
.elec-panel { display:flex; flex-direction:column; gap:14px; }

.elec-panel .statusbar {
  display:flex; align-items:center; gap:12px;
  background:var(--panel2); border:1px solid var(--line);
  border-radius:8px; padding:9px 16px; font-size:13px;
}
.elec-panel .statusbar .dot {
  width:8px; height:8px; border-radius:50%; flex:none; background:var(--bad);
}
.elec-panel .statusbar.is-connected .dot { background:var(--ok); }
.elec-panel .statusbar .model { font-weight:500; color:var(--fg); }
.elec-panel .statusbar .addr {
  color:var(--mut); font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px;
}
.elec-panel .statusbar .right-status {
  margin-left:auto; color:var(--mut); font-size:12px;
}

/* Top region: sweep+plot wide, readout+output narrow */
.elec-panel .ep-top {
  display:grid; grid-template-columns:minmax(0,2.3fr) minmax(300px,1fr);
  gap:14px;
}
@media (max-width:1050px) { .elec-panel .ep-top { grid-template-columns:1fr; } }

.elec-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 12px; font-weight:500;
}
.elec-panel .ep-card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}

.elec-panel .plot-head {
  display:flex; align-items:center; gap:14px; margin-bottom:10px;
}
.elec-panel .plot-head .eyebrow { margin:0; }
.elec-panel .plot-head .spacer { flex:1; }
.elec-panel .derived {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px; color:var(--mut); opacity:.85;
}
.elec-panel .statuspill {
  font-size:12px; color:var(--mut); min-width:54px; text-align:right;
}
.elec-panel .plotbox {
  width:100%; height:330px; border:1px solid var(--line);
  border-radius:8px; background:var(--panel2);
}
.elec-panel .sweep-fields {
  display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:12px;
}
@media (max-width:600px) {
  .elec-panel .sweep-fields { grid-template-columns:repeat(2,1fr); }
}

.elec-panel .readout-stack { display:flex; flex-direction:column; gap:14px; }
.elec-panel .readout .i-line {
  display:flex; align-items:baseline; gap:8px;
}
.elec-panel .readout .i-val {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:38px; font-weight:500; letter-spacing:-.02em;
  color:var(--fg); line-height:1.05;
}
.elec-panel .readout .i-unit {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:18px; color:var(--mut);
}
.elec-panel .readout .v-line {
  display:flex; align-items:baseline; gap:8px; margin-top:6px;
}
.elec-panel .readout .v-val {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:22px; font-weight:500; color:var(--mut); line-height:1.05;
}
.elec-panel .readout .v-unit {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:14px; color:var(--mut); opacity:.7;
}
.elec-panel .readout .no-data { color:var(--warn); }
.elec-panel .read-btns { display:flex; gap:8px; margin-top:14px; }
.elec-panel .read-btns .q-btn { flex:1; }

.elec-panel .output-card { display:flex; flex-direction:column; gap:12px; }
.elec-panel .output-btn {
  height:62px; border-radius:8px; border:1.5px solid var(--line);
  background:var(--panel2); color:var(--fg);
  font-family:system-ui,-apple-system,sans-serif;
  font-size:17px; font-weight:500; cursor:pointer; letter-spacing:.01em;
  display:flex; align-items:center; justify-content:center; gap:10px;
  transition:background .12s, border-color .12s, color .12s;
}
.elec-panel .output-btn:hover { filter:brightness(1.1); }
.elec-panel .output-btn .ic { font-size:20px; line-height:1; }
.elec-panel .output-btn[data-on="true"] {
  background:rgba(248,81,73,.13); border-color:var(--bad); color:#ffb4b4;
}
.elec-panel .output-card.is-on { border-color:var(--bad); }
.elec-panel .output-card .readback { font-size:12px; color:var(--mut); }
.elec-panel .output-card .readback .v {
  font-family:ui-monospace,Menlo,Consolas,monospace; color:var(--fg);
}

/* 3-col blocks below the top region */
.elec-panel .blocks {
  display:grid; grid-template-columns:repeat(3,1fr); gap:14px;
}
@media (max-width:1050px) { .elec-panel .blocks { grid-template-columns:1fr 1fr; } }
@media (max-width:720px) { .elec-panel .blocks { grid-template-columns:1fr; } }
.elec-panel .block-head {
  display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;
}
.elec-panel .block-head .eyebrow { margin:0; }
.elec-panel .fields { display:flex; flex-direction:column; gap:12px; }

/* Field: label above + Quasar dense filled input below, unit as static suffix. */
.elec-panel .fld { display:flex; flex-direction:column; gap:5px; }
.elec-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.elec-panel .fld .nicegui-input,
.elec-panel .fld .nicegui-number,
.elec-panel .fld .nicegui-select { width:100%; }
.elec-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
  transition:border-color .12s, box-shadow .12s;
}
.elec-panel .fld .q-field--filled .q-field__control::before,
.elec-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.elec-panel .fld .q-field__native,
.elec-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.elec-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}
.elec-panel .fld.is-dirty .q-field--filled .q-field__control {
  border-color:rgba(88,166,255,.55) !important;
  box-shadow:0 0 0 2px rgba(88,166,255,.14) !important;
}
.elec-panel .fld .applied-note {
  font-size:11px; color:var(--mut); opacity:.7;
  font-family:ui-monospace,Menlo,Consolas,monospace;
  margin-top:1px; min-height:14px;
}
.elec-panel .fld.is-dirty .applied-note { color:var(--warn); opacity:1; }

/* Toggle row inside a block */
.elec-panel .tgl-row {
  display:flex; align-items:center; justify-content:space-between;
  padding:2px 0;
}
.elec-panel .tgl-row .tgl-lbl { font-size:13px; color:var(--fg); }
.elec-panel .tgl-row .q-toggle { padding:0 !important; }

/* Apply button — accent when dirty */
.elec-panel .apply-btn .q-btn,
.elec-panel .apply-btn {
  height:28px; padding:0 14px; font-size:12px;
  border-radius:8px;
}
.elec-panel .block.is-dirty .apply-btn {
  background:var(--acc) !important; color:#08111f !important;
  border-color:var(--acc) !important;
}

/* Footer */
.elec-panel .efooter {
  margin-top:4px; padding:12px 16px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
  color:var(--mut); font-size:12px;
  display:flex; align-items:center; gap:14px; flex-wrap:wrap;
}
.elec-panel .efooter code {
  font-family:ui-monospace,Menlo,Consolas,monospace; background:var(--bg);
  padding:2px 7px; border-radius:5px; color:var(--mut);
}
.elec-panel .efooter a { color:var(--acc); text-decoration:none; }
.elec-panel .efooter .spacer { flex:1; }

/* ============================================================
   Digitizer panel — CAEN VX2740 layout target (companion to
   .elec-panel). Scoped to .dig-panel so it doesn't leak into
   other instruments. Sub-tabs are inside this panel.
   ============================================================ */
.dig-panel { display:flex; flex-direction:column; gap:14px; }

.dig-panel .subtabs {
  display:flex; gap:4px; justify-content:center;
  padding:8px 0 14px; border-bottom:1px solid var(--line);
  margin-bottom:14px;
}
.dig-panel .subtab {
  background:none !important; border:none !important;
  color:var(--mut) !important; font-size:.78rem !important;
  font-weight:500 !important; letter-spacing:.05em;
  text-transform:uppercase; padding:.4rem .9rem !important;
  min-height:0 !important;
  border-bottom:2px solid transparent !important; margin-bottom:-15px;
  border-radius:0 !important;
}
.dig-panel .subtab:hover { color:var(--fg) !important; }
.dig-panel .subtab.is-active {
  color:var(--fg) !important;
  border-bottom-color:var(--acc) !important;
}

.dig-panel .dpanel { display:none; }
.dig-panel .dpanel.is-active { display:block; }

/* Slim connect strip at the top of the panel (matches mux/k6485/psu) */
.dig-panel .connstrip {
  display:flex; align-items:center; gap:12px;
  padding:10px 14px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
}
.dig-panel .connstrip .dot {
  width:8px; height:8px; border-radius:50%;
  background:var(--mut); flex:none;
}
.dig-panel .connstrip.is-connected .dot { background:var(--ok); }
.dig-panel .connstrip .lbl { font-size:13px; color:var(--mut); }
.dig-panel .connstrip.is-connected .lbl { color:var(--fg); }

.dig-panel .card-dig {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}
.dig-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 12px; font-weight:500;
}

/* Channel grid — 8×8 cells, each is a clickable channel-name label
   (like the header menu buttons) + a compact threshold input. Click
   the label to toggle the channel; accent border + accent text when
   enabled, neutral when off. */
.dig-panel .ch-grid {
  display:grid; grid-template-columns:repeat(8, 1fr);
  gap:6px; width:100%;
}
@media (max-width: 1100px) { .dig-panel .ch-grid { grid-template-columns:repeat(4, 1fr); } }
@media (max-width: 600px)  { .dig-panel .ch-grid { grid-template-columns:repeat(2, 1fr); } }
.dig-panel .ch-cell {
  display:flex; align-items:center; gap:4px;
}
.dig-panel .ch-toggle {
  flex:0 0 auto; min-width:42px; text-align:center;
  padding:3px 6px; border:1px solid var(--line); border-radius:4px;
  background:var(--panel2); color:var(--mut);
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
  cursor:pointer; user-select:none;
  transition:background .12s, color .12s, border-color .12s;
}
.dig-panel .ch-toggle:hover { color:var(--fg); border-color:var(--mut); }
.dig-panel .ch-toggle.is-on {
  background:rgba(88,166,255,.14); color:var(--acc);
  border-color:var(--acc);
}
.dig-panel .ch-toggle.is-pmt.is-on {
  background:rgba(63,185,80,.14); color:var(--ok);
  border-color:var(--ok);
}
.dig-panel .ch-thresh { flex:1 1 auto; min-width:0; }
.dig-panel .ch-thresh .q-field--filled .q-field__control {
  height:26px !important; min-height:26px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:4px !important; padding:0 6px !important;
}
.dig-panel .ch-thresh .q-field__native {
  font-family:ui-monospace,Menlo,Consolas,monospace !important;
  font-size:11px !important; padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.dig-panel .ch-thresh.is-off .q-field__control,
.dig-panel .ch-thresh.is-off .q-field__native { opacity:.4; }
.dig-panel .ch-grid-header {
  font-size:11px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--mut); font-weight:500; margin-bottom:8px;
}

/* Fields inside digitizer panel (reuse the same look as elec-panel) */
.dig-panel .fld { display:flex; flex-direction:column; gap:5px; }
.dig-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.dig-panel .fld .nicegui-input,
.dig-panel .fld .nicegui-number,
.dig-panel .fld .nicegui-select { width:100%; }
.dig-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.dig-panel .fld .q-field--filled .q-field__control::before,
.dig-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.dig-panel .fld .q-field__native, .dig-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.dig-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}
.dig-panel .derived {
  font-size:12px; color:var(--mut); opacity:.85;
  font-family:ui-monospace,Menlo,Consolas,monospace; margin-top:4px;
}

/* Apply button — accent-blue when dirty */
.dig-panel .apply-btn {
  height:28px !important; padding:0 14px !important;
  font-size:12px !important; border-radius:8px !important;
}
.dig-panel .apply-btn.is-dirty {
  background:var(--acc) !important; color:#fff !important;
  border-color:var(--acc) !important;
}

/* Plot box */
.dig-panel .plotbox-dig {
  width:100%; height:340px; border:1px solid var(--line);
  border-radius:8px; background:var(--panel2);
}

/* Status pill */
.dig-panel .statuspill {
  font-size:12px; color:var(--mut);
}

/* ============================================================
   MUX panel — 96-channel IV-Pulse MUX layout. Same tokens as
   .elec-panel and .dig-panel. Scoped to .mux-panel.
   ============================================================ */
.mux-panel { display:flex; flex-direction:column; gap:14px; }
.mux-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 14px; font-weight:500;
}
.mux-panel .card-mux {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}

/* Slim connect strip at the top of the panel */
.mux-panel .connstrip {
  display:flex; align-items:center; gap:12px;
  padding:10px 14px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
}
.mux-panel .connstrip .dot {
  width:8px; height:8px; border-radius:50%;
  background:var(--mut); flex:none;
}
.mux-panel .connstrip.is-connected .dot { background:var(--ok); }
.mux-panel .connstrip .lbl { font-size:13px; color:var(--mut); }
.mux-panel .connstrip.is-connected .lbl { color:var(--fg); }

/* Top grid: channel-select (left) + side column (right) */
.mux-panel .top-grid {
  display:grid; grid-template-columns:1.5fr 1fr; gap:14px;
}
@media (max-width: 880px) { .mux-panel .top-grid { grid-template-columns:1fr; } }
.mux-panel .side-col {
  display:grid; grid-template-columns:1fr; gap:14px;
}

/* Hero readout — large monospace "active channel" */
.mux-panel .hero {
  display:flex; align-items:baseline; gap:10px; margin-bottom:16px;
}
.mux-panel .hero .k {
  font-size:12px; letter-spacing:.05em;
  text-transform:uppercase; color:var(--mut);
}
.mux-panel .hero .v {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:34px; font-weight:500; letter-spacing:-.01em;
  color:var(--fg); line-height:1;
}

/* Field block (label above, dense filled input, unit suffix inside) */
.mux-panel .fld { display:flex; flex-direction:column; gap:5px; }
.mux-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.mux-panel .fld .nicegui-input,
.mux-panel .fld .nicegui-number,
.mux-panel .fld .nicegui-select { width:100%; }
.mux-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.mux-panel .fld .q-field--filled .q-field__control::before,
.mux-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.mux-panel .fld .q-field__native, .mux-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.mux-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}

/* Bypass toggle row (single stateful toggle, not two buttons) */
.mux-panel .tgl-row {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
}
.mux-panel .tgl-row .lbl { font-size:13px; color:var(--fg); }
.mux-panel .tgl-row .state {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; color:var(--mut);
}

/* Temperature readout line */
.mux-panel .readline {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:18px; color:var(--fg); margin:0;
}
.mux-panel .readline .muted { color:var(--mut); }

/* Status pill in the sweep card */
.mux-panel .statuspill { font-size:12px; color:var(--mut); }

/* Description line under sweep eyebrow */
.mux-panel .desc {
  font-size:13px; color:var(--mut); margin:0 0 14px; max-width:720px;
}

/* ============================================================
   K6485 panel — picoammeter layout, sibling of .elec-panel. Hero
   current readout + strip chart + range/integration + read settings.
   ============================================================ */
.k6485-panel { display:flex; flex-direction:column; gap:14px; }
.k6485-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 14px; font-weight:500;
}
.k6485-panel .card-k {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}
.k6485-panel .connstrip {
  display:flex; align-items:center; gap:12px;
  padding:10px 14px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
}
.k6485-panel .connstrip .dot {
  width:8px; height:8px; border-radius:50%;
  background:var(--mut); flex:none;
}
.k6485-panel .connstrip.is-connected .dot { background:var(--ok); }
.k6485-panel .connstrip .lbl { font-size:13px; color:var(--mut); }
.k6485-panel .connstrip.is-connected .lbl { color:var(--fg); }

.k6485-panel .top-grid {
  display:grid; grid-template-columns:1.6fr 1fr; gap:14px;
}
@media (max-width: 900px) { .k6485-panel .top-grid { grid-template-columns:1fr; } }
.k6485-panel .side-col {
  display:grid; grid-template-columns:1fr; gap:14px;
}

/* Hero current readout */
.k6485-panel .hero {
  display:flex; align-items:baseline; gap:10px; margin-bottom:6px;
}
.k6485-panel .hero .k {
  font-size:13px; letter-spacing:.05em;
  text-transform:uppercase; color:var(--mut);
}
.k6485-panel .hero .v {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:40px; font-weight:500; letter-spacing:-.02em;
  color:var(--fg); line-height:1;
}
.k6485-panel .hero .v.no-data { color:var(--warn); }
.k6485-panel .hero .u {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:18px; color:var(--mut);
}
.k6485-panel .read-meta {
  font-size:12px; color:var(--mut); min-height:16px;
  margin-bottom:10px; font-family:ui-monospace,Menlo,Consolas,monospace;
}

/* Strip-chart plot */
.k6485-panel .plotbox-k {
  position:relative; width:100%; height:300px;
  border:1px solid var(--line); border-radius:8px;
  background:var(--panel2);
}
.k6485-panel .plot-empty {
  position:absolute; inset:0; display:flex;
  align-items:center; justify-content:center;
  color:var(--mut); font-size:13px; pointer-events:none;
}

/* Fields (label-above, suffix-inside) — same look as the elec / mux panels */
.k6485-panel .fld { display:flex; flex-direction:column; gap:5px; }
.k6485-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.k6485-panel .fld .nicegui-input,
.k6485-panel .fld .nicegui-number,
.k6485-panel .fld .nicegui-select { width:100%; }
.k6485-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.k6485-panel .fld .q-field--filled .q-field__control::before,
.k6485-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.k6485-panel .fld .q-field__native, .k6485-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.k6485-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}
.k6485-panel .fld .hint { font-size:11px; color:var(--mut); opacity:.7; margin-top:-1px; }

/* Stateful zero-check toggle row */
.k6485-panel .tgl-row {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
}
.k6485-panel .tgl-row .lbl { font-size:13px; color:var(--fg); }
.k6485-panel .tgl-row .state {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; color:var(--mut);
}

/* Apply button — accent when dirty */
.k6485-panel .apply-btn {
  height:28px !important; padding:0 14px !important;
  font-size:12px !important; border-radius:8px !important;
}
.k6485-panel .apply-btn.is-dirty {
  background:var(--acc) !important; color:#fff !important;
  border-color:var(--acc) !important;
}

/* ============================================================
   NGE103 PSU panel — multi-channel power supply layout (MUX rail).
   Same tokens; scoped to .psu-panel.
   ============================================================ */
.psu-panel { display:flex; flex-direction:column; gap:14px; }
.psu-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0; font-weight:500;
}
.psu-panel .card-psu {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}
.psu-panel .connstrip {
  display:flex; align-items:center; gap:12px;
  padding:10px 14px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
}
.psu-panel .connstrip .dot {
  width:8px; height:8px; border-radius:50%;
  background:var(--mut); flex:none;
}
.psu-panel .connstrip.is-connected .dot { background:var(--ok); }
.psu-panel .connstrip .lbl { font-size:13px; color:var(--mut); }
.psu-panel .connstrip.is-connected .lbl { color:var(--fg); }

/* Master strip */
.psu-panel .master {
  display:flex; align-items:center; gap:18px; flex-wrap:wrap;
}
.psu-panel .master .info {
  font-size:13px; color:var(--mut);
  font-family:ui-monospace,Menlo,Consolas,monospace;
}
.psu-panel .master .right {
  margin-left:auto; display:flex; align-items:center; gap:14px;
}

/* Channel grid — responsive auto-fit row */
.psu-panel .chgrid {
  display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));
  gap:14px;
}

.psu-panel .ch-head {
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:12px;
}
.psu-panel .ch-head .title {
  font-size:14px; font-weight:500; color:var(--acc);
}

/* Readback box — bordered, turns red when output is energised */
.psu-panel .readback {
  background:var(--panel2); border:1px solid var(--line);
  border-radius:8px; padding:10px 12px; margin-bottom:14px;
}
.psu-panel .readback.is-live { border-color:rgba(248,81,73,.55); }
.psu-panel .readback .row {
  display:flex; align-items:baseline; justify-content:space-between;
}
.psu-panel .readback .row + .row { margin-top:4px; }
.psu-panel .readback .lab {
  font-size:11px; letter-spacing:.04em;
  text-transform:uppercase; color:var(--mut);
}
.psu-panel .readback .val {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:22px; font-weight:500; color:var(--fg);
}
.psu-panel .readback .val.small {
  font-size:16px; color:var(--mut);
}
.psu-panel .readback .u {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px; color:var(--mut); margin-left:3px;
}

/* Output toggle row — red when ON */
.psu-panel .out-row {
  display:flex; align-items:center; gap:10px;
}
.psu-panel .out-row .state {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; color:var(--mut);
}
.psu-panel .out-row .state.is-on { color:#ffb4b4; }
.psu-panel .tgl-danger .q-toggle__inner--truthy {
  color:var(--bad) !important;
}

/* Field block (label above, suffix inside) */
.psu-panel .fld { display:flex; flex-direction:column; gap:5px; }
.psu-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.psu-panel .fld .nicegui-input,
.psu-panel .fld .nicegui-number,
.psu-panel .fld .nicegui-select { width:100%; }
.psu-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.psu-panel .fld .q-field--filled .q-field__control::before,
.psu-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.psu-panel .fld .q-field__native, .psu-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.psu-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}

/* Per-channel Apply (lights up accent when dirty) */
.psu-panel .ch-foot {
  display:flex; align-items:center; justify-content:flex-end; margin-top:12px;
}
.psu-panel .apply-btn {
  height:28px !important; padding:0 14px !important;
  font-size:12px !important; border-radius:8px !important;
}
.psu-panel .apply-btn.is-dirty {
  background:var(--acc) !important; color:#fff !important;
  border-color:var(--acc) !important;
}

/* ============================================================
   L1 primitives — uniform dashboard of single-instrument cards.
   ============================================================ */
.l1-panel .intro { font-size:13px; color:var(--mut); margin:0 0 12px; }
.l1-panel .dash {
  display:grid; grid-template-columns:repeat(auto-fill, minmax(290px, 1fr));
  gap:14px; grid-auto-flow:dense;
}
.l1-panel .span2 { grid-column: span 2; }
@media (max-width: 760px) { .l1-panel .span2 { grid-column: span 1; } }
.l1-panel .l1-card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:14px 16px;
  display:flex; flex-direction:column;
}
.l1-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 12px; font-weight:500;
}
.l1-panel .fld { display:flex; flex-direction:column; gap:4px; }
.l1-panel .fld > label.fld-lbl { font-size:11px; color:var(--mut); }
.l1-panel .fld .nicegui-input,
.l1-panel .fld .nicegui-number,
.l1-panel .fld .nicegui-select { width:100%; }
.l1-panel .fld .q-field--filled .q-field__control {
  height:34px !important; min-height:34px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.l1-panel .fld .q-field--filled .q-field__control::before,
.l1-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.l1-panel .fld .q-field__native, .l1-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.l1-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:11px;
}
.l1-panel .frow { display:grid; gap:8px; margin-bottom:12px; }
.l1-panel .frow.cols-2 { grid-template-columns:1fr 1fr; }
.l1-panel .btnrow { display:flex; gap:8px; flex-wrap:wrap; }
.l1-panel .l1-card .q-btn {
  min-height:32px !important; padding:0 14px !important;
  font-size:13px !important;
}
.l1-panel .result {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px;
  color:var(--mut); margin-top:12px;
}
.l1-panel .result .muted { color:var(--mut); opacity:.6; }
.l1-panel .result b { color:var(--fg); font-weight:500; }
.l1-panel .cam {
  position:relative; aspect-ratio:16/10; border-radius:8px;
  overflow:hidden;
  background:linear-gradient(135deg,#1a2230,#0d131b);
  border:1px solid var(--line);
}
.l1-panel .cam img {
  width:100%; height:100%; object-fit:cover; display:block; background:#000;
}
.l1-panel .cam .live-pill {
  position:absolute; top:10px; left:10px;
  display:flex; align-items:center; gap:6px;
  font-size:11px; color:#ffb4b4;
  background:rgba(0,0,0,.45); padding:3px 9px; border-radius:20px;
}
.l1-panel .cam .live-pill .d {
  width:7px; height:7px; border-radius:50%;
  background:var(--bad); animation:l1-pulse 1.4s infinite;
}
@keyframes l1-pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
.l1-panel .jog {
  display:grid; grid-template-columns:repeat(4, 1fr); gap:6px;
}
.l1-panel .jog .q-btn {
  min-height:32px !important; padding:0 !important;
  font-size:13px !important; min-width:42px !important;
}
.l1-panel .steps {
  background:var(--panel2); border:1px solid var(--line);
  border-radius:8px; padding:8px 10px; min-height:54px;
  margin:0 0 12px; font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px; color:var(--mut);
}
.l1-panel .steps .empty { color:var(--mut); opacity:.6; font-style:italic; }
.l1-panel .steps .step {
  padding:2px 0; border-top:1px solid var(--line);
}
.l1-panel .steps .step:first-child { border-top:0; }
.l1-panel .plotbox-l1 {
  position:relative; width:100%; height:200px;
  border:1px solid var(--line); border-radius:8px;
  background:var(--panel2); margin-bottom:12px;
}
.l1-panel .plot-empty {
  position:absolute; inset:0; display:flex;
  align-items:center; justify-content:center;
  color:var(--mut); font-size:12px; pointer-events:none;
}
.l1-panel .tgl-inline {
  display:inline-flex; align-items:center; gap:7px; font-size:13px;
}

/* ============================================================
   Phidget XY stage panel — position hero + jog pad + moves + coils.
   ============================================================ */
.stage-panel { display:flex; flex-direction:column; gap:14px; }
.stage-panel .eyebrow {
  font-size:11px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--mut); margin:0 0 14px; font-weight:500;
}
.stage-panel .card-s {
  background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:16px 18px;
}
.stage-panel .connstrip {
  display:flex; align-items:center; gap:12px;
  padding:10px 14px; background:var(--panel2);
  border:1px solid var(--line); border-radius:8px;
}
.stage-panel .connstrip .dot {
  width:8px; height:8px; border-radius:50%; background:var(--mut); flex:none;
}
.stage-panel .connstrip.is-connected .dot { background:var(--ok); }
.stage-panel .connstrip .lbl { font-size:13px; color:var(--mut); }
.stage-panel .connstrip.is-connected .lbl { color:var(--fg); }

/* Layout: left controls column + right webcam */
.stage-panel .layout {
  display:grid; grid-template-columns:minmax(0,1fr) minmax(420px, 640px);
  gap:14px; align-items:start;
}
@media (max-width:1080px) {
  .stage-panel .layout { grid-template-columns:1fr; }
}
.stage-panel .stack {
  display:flex; flex-direction:column; gap:14px;
}

/* Position hero readback */
.stage-panel .pos-hero {
  display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;
}
.stage-panel .pos-hero .v {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:30px; font-weight:500; letter-spacing:-.01em;
  color:var(--fg); line-height:1.05;
}
.stage-panel .pos-hero .v .muted { color:var(--mut); }
.stage-panel .pos-hero .u {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:15px; color:var(--mut);
}
.stage-panel .coil-line {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:13px; color:var(--mut); margin-top:8px;
}
.stage-panel .coil-line .on { color:#ffb4b4; }

/* JOG PAD — 5×5 grid */
.stage-panel .jogwrap {
  display:flex; gap:24px; align-items:center; flex-wrap:wrap;
  margin-top:18px;
}
.stage-panel .jogpad {
  display:grid;
  grid-template-columns:repeat(5, 46px);
  grid-template-rows:repeat(5, 46px);
  gap:6px;
}
.stage-panel .jb {
  display:flex; align-items:center; justify-content:center;
  border-radius:8px; cursor:pointer;
  border:1px solid var(--line); background:var(--panel2);
  color:var(--fg); transition:background .12s, border-color .12s;
  font-variant-numeric:tabular-nums; padding:0;
}
.stage-panel .jb:hover {
  background:#1b2430; border-color:rgba(88,166,255,.55);
}
.stage-panel .jb:active { background:rgba(88,166,255,.14); }
.stage-panel .jb.fine { font-size:18px; }
.stage-panel .jb.coarse { font-size:13px; color:var(--mut); }
.stage-panel .jb.center {
  cursor:pointer; border-style:dashed; border-color:var(--line);
  background:transparent; color:var(--mut);
  font-size:10px; letter-spacing:.04em; text-transform:uppercase;
}
.stage-panel .jb.center:hover { background:rgba(88,166,255,.06); }
/* 5×5 cell placement */
.stage-panel .p-yc1 { grid-column:3; grid-row:1; }
.stage-panel .p-yf1 { grid-column:3; grid-row:2; }
.stage-panel .p-xc0 { grid-column:1; grid-row:3; }
.stage-panel .p-xf0 { grid-column:2; grid-row:3; }
.stage-panel .p-ctr { grid-column:3; grid-row:3; }
.stage-panel .p-xf1 { grid-column:4; grid-row:3; }
.stage-panel .p-xc1 { grid-column:5; grid-row:3; }
.stage-panel .p-yf0 { grid-column:3; grid-row:4; }
.stage-panel .p-yc0 { grid-column:3; grid-row:5; }

.stage-panel .jog-steps {
  display:flex; flex-direction:column; gap:12px; min-width:140px;
}
.stage-panel .swatch {
  display:inline-flex; align-items:center; gap:7px;
  font-size:12px; color:var(--mut);
}
.stage-panel .swatch .box {
  width:12px; height:12px; border-radius:3px;
  border:1px solid var(--line); background:var(--panel2);
}
.stage-panel .swatch .box.coarse { background:#1b2430; }

/* Camera-view axis-direction diagram (next to the jog pad).
   Visual reference so the operator can see at a glance which axis
   maps to which screen direction. */
/* Global (also reused on the L1 primitives Stage card). */
.axis-diagram {
  background:var(--panel2); border:1px solid var(--line);
  border-radius:8px; padding:6px 8px 4px;
  display:inline-flex; flex-direction:column; align-items:center; gap:2px;
}
.axis-cap {
  font-size:10px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--mut);
}

.stage-panel .hint { font-size:11px; color:var(--mut); opacity:.7; }

/* Fields */
.stage-panel .fld { display:flex; flex-direction:column; gap:5px; }
.stage-panel .fld > label.fld-lbl { font-size:12px; color:var(--mut); }
.stage-panel .fld > label.fld-lbl-rich { font-size:12px; color:var(--mut); }
.stage-panel .fld .nicegui-input,
.stage-panel .fld .nicegui-number,
.stage-panel .fld .nicegui-select { width:100%; }
.stage-panel .fld .q-field--filled .q-field__control {
  height:36px !important; min-height:36px !important;
  background:var(--panel2) !important; border:1px solid var(--line) !important;
  border-radius:8px !important; padding:0 12px !important;
}
.stage-panel .fld .q-field--filled .q-field__control::before,
.stage-panel .fld .q-field--filled .q-field__control::after { display:none !important; }
.stage-panel .fld .q-field__native, .stage-panel .fld .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:14px;
  padding:0 !important; color:var(--fg) !important;
  font-variant-numeric:tabular-nums;
}
.stage-panel .fld .q-field__suffix {
  color:var(--mut); opacity:.75;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
}

/* Move sub-blocks (Absolute | Relative side-by-side) */
.stage-panel .moves {
  display:grid; grid-template-columns:1fr 1fr; gap:18px;
}
@media (max-width:560px) { .stage-panel .moves { grid-template-columns:1fr; } }
.stage-panel .subhead {
  font-size:12px; color:var(--mut); margin:0 0 10px; font-weight:500;
}
.stage-panel .btnrow {
  display:flex; gap:8px; flex-wrap:wrap; align-items:center;
}
.stage-panel .tgl-inline {
  display:flex; align-items:center; gap:9px; font-size:13px;
}

/* Webcam card */
.stage-panel .cam {
  position:relative; aspect-ratio:16/10; border-radius:8px;
  overflow:hidden;
  background:linear-gradient(135deg,#1a2230,#0d131b);
  border:1px solid var(--line);
}
.stage-panel .cam img {
  width:100%; height:100%; object-fit:cover; display:block; background:#000;
}
.stage-panel .cam .live-pill {
  position:absolute; top:10px; left:10px;
  display:flex; align-items:center; gap:6px;
  font-size:11px; color:#ffb4b4;
  background:rgba(0,0,0,.45); padding:3px 9px; border-radius:20px;
}
.stage-panel .cam .live-pill .d {
  width:7px; height:7px; border-radius:50%;
  background:var(--bad); animation:stage-pulse 1.4s infinite;
}
/* Axis overlay on the camera frame — top right corner. */
.stage-panel .cam .cam-axis {
  position:absolute; top:8px; right:8px;
  background:rgba(10,15,22,.55); border:1px solid var(--line);
  border-radius:6px; padding:2px 4px 0;
  display:flex; align-items:center; justify-content:center;
}
@keyframes stage-pulse { 0%,100%{opacity:1} 50%{opacity:.35} }

/* Mode toggle for the acquisition card (single | sweep). */
.acq-mode { margin-bottom:.4rem; }
.acq-mode .q-btn-toggle { background:var(--panel2) !important;
                          border:1px solid var(--line) !important; }
.acq-mode .q-btn-toggle .q-btn {
  min-height:24px !important; font-size:.72rem !important;
  padding:0 .8rem !important;
}

/* Plot card — sensible default height */
.plot-card-v2 { padding:.3rem .5rem !important; margin-bottom:.5rem; }
.plot-card-v2 .lbl { font-size:.7rem; letter-spacing:.7px;
                     text-transform:uppercase; color:var(--mut);
                     display:block; margin-bottom:.15rem; }
.iv-plot-v2 { width:100%; height:280px;
              background:var(--panel); border:1px solid var(--line);
              border-radius:4px; }

/* 2x2 setting blocks grid (collapses to single column under 900px) */
.elec-grid-v2 { display:grid; gap:.5rem; grid-template-columns:1fr; }
@media (min-width: 900px) {
  .elec-grid-v2 { grid-template-columns: 1fr 1fr; }
}

/* Block card — consistent typography, dense */
.block-card { padding:.4rem .7rem .5rem !important; }
.block-card h3 { font-size:.7rem !important; letter-spacing:1px;
                 text-transform:uppercase; color:var(--acc) !important;
                 font-weight:700; margin:0 0 .4rem !important;
                 display:flex; align-items:baseline; gap:.5rem; }
.block-card h3 .h-sub { font-size:.65rem; color:var(--mut);
                        font-weight:400; letter-spacing:.3px;
                        text-transform:none; }
.block-card .apply-btn {
  margin-top:.4rem; padding:0 .9rem !important;
  min-height:26px !important; font-size:.78rem !important;
  font-weight:600 !important;
}
.block-card .apply-btn.is-clean { opacity:.45; }
.block-card .apply-btn.is-dirty { /* normal opacity, accent stroke handled by props */ }

/* Stacked field: label row above, input below */
.v2-field { display:flex; flex-direction:column; gap:.05rem;
            margin-bottom:.35rem; }
.v2-field.inline { flex-direction:row; align-items:center;
                   gap:.5rem; margin-bottom:.25rem; }
.v2-label-row { display:flex; align-items:baseline;
                justify-content:space-between; gap:.5rem;
                line-height:1.1; }
.v2-lbl { font-size:.72rem; color:var(--fg); letter-spacing:.2px; }
.v2-applied { font-size:.68rem; color:var(--mut);
              font-family:ui-monospace,Menlo,Consolas,monospace;
              font-variant-numeric:tabular-nums; }

/* Make numeric inputs monospace and uniform height. Kill the floating
   label by simply not passing a `label` prop in Python — Quasar then
   doesn't reserve label space at all. */
.v2-field .nicegui-input, .v2-field .nicegui-number,
.v2-field .nicegui-select { width:100%; }
.v2-field .q-field--filled .q-field__control {
  min-height:32px !important; height:32px;
  background:var(--panel2) !important;
  border:1px solid var(--line) !important;
  border-radius:4px !important;
}
.v2-field .q-field--filled .q-field__control::before,
.v2-field .q-field--filled .q-field__control::after { display:none !important; }
.v2-field .q-field__native, .v2-field .q-field__input {
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:.86rem; padding:0 !important;
  color:var(--fg) !important;
  font-variant-numeric: tabular-nums;
}
.v2-field .q-field__suffix, .v2-field .q-field__prefix {
  color:var(--mut) !important; font-size:.78rem;
  font-family:ui-monospace,Menlo,Consolas,monospace;
}
.v2-field .q-field__marginal { padding:0 .35rem !important; }

/* Dirty marker — visible accent stroke when staged ≠ applied */
.v2-field.is-dirty .q-field__control {
  border-color: var(--acc) !important;
  box-shadow: inset 0 0 0 1px var(--acc) !important;
}
.v2-field.is-dirty .v2-applied {
  color: var(--warn);
}

/* Toggle field (inline label + switch, no overlap) */
.v2-toggle-row { display:flex; align-items:center;
                 justify-content:space-between; gap:.5rem;
                 padding:.15rem 0; }
.v2-toggle-row .v2-lbl { font-size:.78rem; }
.v2-toggle-row.is-dirty .q-toggle__thumb {
  box-shadow: 0 0 0 2px var(--acc);
}

/* Hero secondary action buttons (read I / read V) — subdued */
.subordinate-btn {
  padding:0 .6rem !important; min-height:24px !important;
  font-size:.72rem !important;
  background:transparent !important; color:var(--mut) !important;
  border:1px solid var(--line) !important;
}
.subordinate-btn:hover { color:var(--fg) !important; border-color:var(--acc) !important; }

/* Sweep run button — the primary action. Distinct from Apply. */
.run-btn {
  width:100%; padding:.5rem 1rem !important;
  min-height:34px !important;
  font-size:.85rem !important; font-weight:700 !important;
  letter-spacing:.8px; text-transform:uppercase;
}

/* Cards = dark panels */
.q-card, .daq-card {
  background:var(--panel) !important; color:var(--fg) !important;
  border:1px solid var(--line); border-radius:10px;
  box-shadow:none !important; padding:.55rem .85rem .7rem !important;
}
.daq-card h2 { font-size:.92rem; margin:.05rem 0 .45rem; color:var(--acc);
  font-weight:600; letter-spacing:.3px; }

/* Data explorer file list — one clickable row per .h5 file */
.data-file-row { padding:.3rem .5rem; border:1px solid var(--line);
  border-radius:6px; background:var(--panel2); cursor:pointer;
  transition:border-color .12s ease, background .12s ease; }
.data-file-row:hover { border-color:var(--acc); background:var(--panel); }
.data-file-row .df-name { font-size:.8rem; color:var(--fg);
  word-break:break-all; }
.data-file-row .df-meta { font-size:.7rem; color:var(--mut, #9aa); }

/* Inputs & buttons */
.q-field__control, .q-field--filled .q-field__control { background:var(--panel2) !important;
  border:1px solid var(--line) !important; border-radius:6px !important; min-height:32px !important;
  color:var(--fg) !important; }
.q-field__label, .q-field__native, .q-field input, .q-field textarea {
  color:var(--fg) !important; }
.q-field__label { color:var(--mut) !important; }
.q-field--filled .q-field__control:before { display:none !important; }
.q-field--filled .q-field__control:after  { display:none !important; }

.q-btn { background:var(--panel2) !important; color:var(--fg) !important;
  border:1px solid var(--line) !important; border-radius:6px !important; box-shadow:none !important;
  padding:.18rem .65rem !important; min-height:32px !important; text-transform:none !important; }
.q-btn:hover { border-color:var(--acc) !important; }
.q-btn.q-btn--standard.text-white { color:var(--fg) !important; }   /* override colored-text */
.q-btn[data-q-color="primary"], .q-btn.bg-primary {
  background:var(--acc) !important; color:#08111f !important; border-color:var(--acc) !important;
  font-weight:600 !important; }
.q-btn[data-q-color="negative"], .q-btn.bg-negative {
  background:transparent !important; color:var(--bad) !important; border-color:var(--bad) !important; }
.q-btn[data-q-color="warning"], .q-btn.bg-warning {
  background:transparent !important; color:var(--warn) !important; border-color:var(--warn) !important; }
.q-btn[data-q-color="secondary"], .q-btn.bg-secondary {
  background:transparent !important; color:var(--acc) !important; border-color:var(--acc) !important; }

/* Log widget */
.q-log, .nicegui-log {
  background:var(--panel2) !important; color:var(--fg) !important;
  border:1px solid var(--line); border-radius:6px;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.82rem;
}

.num { font-variant-numeric:tabular-nums; }
small.hint { color:var(--mut); display:block; margin-top:.3rem; font-size:.78rem; }

/* Emergency BIAS OFF — visible on every tab, hard to miss */
.q-btn.estop {
  background: var(--bad) !important; color:#1a0606 !important;
  border:1px solid #ff8a82 !important;
  font-weight:700 !important; letter-spacing:.5px;
  padding:.32rem 1rem !important; min-height:38px !important;
  box-shadow:0 0 0 2px rgba(248,81,73,.25) !important;
}
.q-btn.estop:hover { background:#ff6a60 !important; color:#0a0202 !important; }
.q-btn.estop:active{ transform:translateY(1px); }
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_in_thread(fn: Callable, *args, **kwargs):
    """Run a blocking instrument call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _quick_connect(instrument_key: str) -> bool:
    """Connect one instrument by key using whatever address is currently in
    HUB.config (populated at startup from .last_connections.json).

    Used by the per-instrument "connect" button on each panel and by the
    header's "⚡ connect all" button. Persists the address on success.

    Auto port-recovery: if connect raises with "Device or resource busy"
    on a /dev/tty* path, we run fuser, kill same-user PIDs holding the
    port, and retry once.
    """
    spec = next((s for s in _INSTRUMENT_SPECS if s["key"] == instrument_key), None)
    if spec is None:
        return False
    addr = (getattr(HUB.config, spec["addr_attr"], None)
            if spec["addr_attr"] else None)
    # Skip instruments whose address is still the dataclass placeholder.
    # Concretely: VISA strings with "MY00000000" — the Keysight serial-number
    # template. pyvisa-py retries USB::INSTR three times on a missing device
    # (~20 s total) before raising, which blocks the connect-all loop long
    # enough to drop the browser's websocket. Per-instrument connect can
    # still be attempted from the instrument's own tab.
    if isinstance(addr, str) and "MY00000000" in addr:
        HUB.status[instrument_key] = "skipped (placeholder address — set in its tab)"
        return False
    HUB.status[instrument_key] = "connecting…"

    async def _attempt() -> Exception | None:
        try:
            await _run_in_thread(getattr(HUB, spec["connect"]))
            return None
        except Exception as e:
            return e

    err = await _attempt()
    if err is not None and port_recovery._is_busy_error(err) and isinstance(addr, str):
        ok, msg = port_recovery.free_serial_port(addr)
        log.warning("port recovery on %s: %s", addr, msg)
        if ok:
            HUB.status[instrument_key] = f"connecting (after freeing {addr})…"
            err = await _attempt()

    if err is None:
        connection_state.record_connect(
            instrument_key, str(addr) if addr else None
        )
        return True

    HUB.status[instrument_key] = f"FAIL: {type(err).__name__}: {err}"
    log.exception("connect %s failed: %s", instrument_key, err,
                  exc_info=(type(err), err, err.__traceback__))
    return False


def _classify(text: str) -> str:
    """Return 'ok' | 'bad' | 'warn' | 'mut' for a hub status string."""
    if text.startswith("OK"):
        return "ok"
    if text in ("disconnected", ""):
        return "mut"
    if text.startswith("connecting"):
        return "warn"
    return "bad"


def _short_status(text: str) -> str:
    """Two-word summary for the header pill (full text goes in the Connections tab)."""
    if text.startswith("OK"):
        return "ok"
    if text == "disconnected":
        return "—"
    if text.startswith("connecting"):
        return "..."
    return "down"


def _fmt_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


# ===========================================================================
# Status tab (landing page)
# ===========================================================================

def _build_status_tab():
    """Real-time overview of what the experiment is doing. Landing page.

    Reads shared state (ACTIVITY, BIAS_STATE, SESSIONS) plus light-weight
    queries against connected instruments. Refreshes every ~1 s via timer.
    Heavy instrument I/O (e.g. forcing a B2987 current measurement) is
    explicitly avoided to not interfere with running measurements.
    """
    from pathlib import Path

    # --- Top: what's running right now (single-line status bar) -----------
    with ui.card().classes("daq-card status-card status-top w-full"):
        with ui.row().classes("items-baseline gap-2 no-wrap w-full"):
            ui.html('<span class="lbl">running</span>')
            act_name = ui.html('<span class="big">idle</span>')
            ui.html('<span class="sep">·</span>')
            ui.html('<span class="lbl">elapsed</span>')
            act_elapsed = ui.html('<span class="num">—</span>')
            ui.html('<span class="sep">·</span>')
            ui.html('<span class="lbl">bias V</span>')
            act_bias = ui.html('<span class="num">—</span>')
            ui.html('<span class="sep">·</span>')
            ui.html('<span class="lbl">bias I</span>')
            act_iread = ui.html('<span class="num">—</span>')
            act_detail = ui.html('<span class="sub" style="color:var(--mut); margin-left:auto"></span>')

    # --- Middle: small per-subsystem cards --------------------------------
    with ui.element("div").classes("status-grid w-full"):
        with ui.card().classes("daq-card status-card"):
            ui.html('<span class="lbl">temperature</span>')
            temp_val = ui.html('<span class="num big">—</span>')
            temp_sub = ui.html('<span class="sub" style="color:var(--mut)">connect slow control</span>')

        with ui.card().classes("daq-card status-card"):
            ui.html('<span class="lbl">stage position</span>')
            stage_val = ui.html('<span class="num big">—</span>')
            stage_sub = ui.html('<span class="sub" style="color:var(--mut)">stage not connected</span>')

        with ui.card().classes("daq-card status-card"):
            ui.html('<span class="lbl">mux channel</span>')
            mux_val = ui.html('<span class="num big">—</span>')
            mux_sub = ui.html('<span class="sub" style="color:var(--mut)">mux not connected</span>')

        with ui.card().classes("daq-card status-card"):
            ui.html('<span class="lbl">connected users</span>')
            users_val = ui.html('<span class="big">0</span>')
            users_sub = ui.html('<span class="sub" style="color:var(--mut)">—</span>')

    # --- Webcam + instrument-health pills ---------------------------------
    with ui.row().classes("w-full gap-3 items-stretch flex-wrap"):
        with ui.card().classes("daq-card status-card").style("flex:2 1 480px"):
            ui.html("<h2>webcam</h2>")
            ui.html(
                '<img id="status-webcam" src="/webcam.mjpeg" '
                'style="max-width:100%; height:auto; border:1px solid var(--line); '
                'border-radius:6px; background:#000;" alt="webcam stream" />'
            )

        with ui.card().classes("daq-card status-card").style("flex:1 1 320px"):
            ui.html("<h2>instrument health</h2>")
            health_pills: dict[str, ui.html] = {}
            with ui.row().classes("flex-wrap gap-1"):
                for spec in _visible_specs():
                    cls = _classify(HUB.status.get(spec["key"], "disconnected"))
                    txt = HUB.status.get(spec["key"], "disconnected")
                    health_pills[spec["key"]] = ui.html(
                        f'<span class="pill {cls}" title="{txt}">'
                        f'{spec["name"]}: {_short_status(txt)}</span>'
                    )

    # --- Recent plots (newest first) --------------------------------------
    plots_dir = Path(__file__).resolve().parents[2] / "plots"
    with ui.card().classes("daq-card status-card w-full"):
        ui.html("<h2>recent plots</h2>")
        thumbs = ui.html('<span class="sub" style="color:var(--mut)">no plots yet</span>') \
            .classes("thumb-strip w-full")

    # --- Log tail ---------------------------------------------------------
    with ui.card().classes("daq-card status-card w-full"):
        ui.html("<h2>activity log</h2>")
        log_box = ui.log(max_lines=200).classes("h-48 w-full")
    _log_last_idx = {"n": 0}

    # --- Periodic refresh -------------------------------------------------
    def _refresh():
        # Current measurement
        if ACTIVITY["name"]:
            act_name.set_content(f'<span class="big" style="color:var(--acc)">'
                                 f'{ACTIVITY["name"]}</span>')
            act_elapsed.set_content(
                f'<span class="num">{_fmt_elapsed(time.time() - ACTIVITY["started"])}</span>'
            )
            act_detail.set_content(
                f'<span class="sub" style="color:var(--mut); margin-left:auto">{ACTIVITY["detail"] or ""}</span>'
            )
        else:
            act_name.set_content('<span class="big">idle</span>')
            act_elapsed.set_content('<span class="num">—</span>')
            act_detail.set_content('')

        # Bias
        v = BIAS_STATE["v_set"]; i = BIAS_STATE["i_meas"]
        act_bias.set_content(
            f'<span class="num">{v:.2f} V</span>' if v is not None
            else '<span class="num">—</span>'
        )
        act_iread.set_content(
            f'<span class="num">{i:.2e} A</span>' if i is not None
            else '<span class="num">—</span>'
        )

        # Temperature
        if HUB.sc is not None:
            try:
                T = HUB.sc.temperature_K()
                temp_val.set_content(f'<span class="num big">{T:.2f} K</span>')
                temp_sub.set_content('<span class="sub" style="color:var(--mut)">slow-control InfluxDB</span>')
            except Exception as e:
                temp_val.set_content('<span class="num big">err</span>')
                temp_sub.set_content(f'<span class="sub" style="color:var(--bad)">{type(e).__name__}</span>')
        else:
            temp_val.set_content('<span class="num big">—</span>')
            temp_sub.set_content('<span class="sub" style="color:var(--mut)">connect slow control</span>')

        # Stage
        if HUB.stage is not None:
            try:
                x, y = HUB.stage.position()
                stage_val.set_content(f'<span class="num big">{x:.2f} / {y:.2f} mm</span>')
                stage_sub.set_content('<span class="sub" style="color:var(--mut)">x / y</span>')
            except Exception as e:
                stage_val.set_content('<span class="num big">err</span>')
                stage_sub.set_content(f'<span class="sub" style="color:var(--bad)">{type(e).__name__}</span>')
        else:
            stage_val.set_content('<span class="num big">—</span>')
            stage_sub.set_content('<span class="sub" style="color:var(--mut)">stage not connected</span>')

        # Mux
        if HUB.mux is not None:
            try:
                ch = HUB.mux.active_channel()
                stxt = f"ch {ch}" if ch is not None else "none"
                mux_val.set_content(f'<span class="num big">{stxt}</span>')
                mux_sub.set_content('<span class="sub" style="color:var(--mut)">active channel</span>')
            except Exception as e:
                mux_val.set_content('<span class="num big">err</span>')
                mux_sub.set_content(f'<span class="sub" style="color:var(--bad)">{type(e).__name__}</span>')
        else:
            mux_val.set_content('<span class="num big">—</span>')
            mux_sub.set_content('<span class="sub" style="color:var(--mut)">mux not connected</span>')

        # Users
        users = SESSIONS.unique_users()
        users_val.set_content(f'<span class="big">{len(users)}</span>')
        users_sub.set_content(
            '<span class="sub" style="color:var(--mut)">'
            + (", ".join(f"{n} ({ip})" for n, ip in users) or "—")
            + "</span>"
        )

        # Instrument health pills
        for spec in _visible_specs():
            txt = HUB.status.get(spec["key"], "disconnected")
            cls = _classify(txt)
            pill = health_pills.get(spec["key"])
            if pill is not None:
                pill.set_content(
                    f'<span class="pill {cls}" title="{txt}">'
                    f'{spec["name"]}: {_short_status(txt)}</span>'
                )

        # Recent plots
        if plots_dir.is_dir():
            files = sorted(plots_dir.glob("*.png"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:8]
            if files:
                html = "".join(
                    f'<a href="/plots-img/{p.name}" target="_blank" title="{p.name}">'
                    f'<img src="/plots-img/{p.name}"/></a>'
                    for p in files
                )
                thumbs.set_content(html)
            else:
                thumbs.set_content('<span class="sub" style="color:var(--mut)">no plots yet</span>')

        # Log tail
        new = _LOG_RING[_log_last_idx["n"]:]
        for ts, level, name, msg in new:
            log_box.push(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  "
                         f"{level:<5} {name:<22} {msg}")
        _log_last_idx["n"] = len(_LOG_RING)

    _refresh()
    ui.timer(1.0, _refresh)


# ===========================================================================
# Connections tab
# ===========================================================================

def _build_connections_tab(header_pills: dict[str, ui.html]):
    """`header_pills` is a dict of instrument-key → ui.html element in the
    sticky header that this tab updates as connections change."""

    ui.label(
        "Connect each instrument independently, or use 'connect all' to bring "
        "everything up using the last-known addresses. Addresses persist to "
        ".last_connections.json across restarts."
    ).classes("text-gray-400 text-sm")

    status_labels: dict[str, ui.label] = {}
    addr_inputs:   dict[str, ui.input] = {}

    def refresh(key: str):
        cls = _classify(HUB.status.get(key, "disconnected"))
        text = HUB.status.get(key, "disconnected")
        if key in status_labels:
            status_labels[key].text = text
            status_labels[key].classes(replace=f"pill {cls} num")
        if key in header_pills:
            spec = next(s for s in _INSTRUMENT_SPECS if s["key"] == key)
            header_pills[key].content = (
                f'<span class="pill pill-clickable {cls}" '
                f'title="open {spec["name"]} panel">'
                f'{spec["name"]}: {_short_status(text)}</span>'
            )

    async def _connect_one(spec) -> bool:
        """Push address to HUB.config and run the connect method. Returns
        True on success, False on failure. Persists the address on success.

        Auto port-recovery: on "Device or resource busy" with a /dev/tty*
        address, frees the port via fuser and retries once."""
        k = spec["key"]
        if k in addr_inputs and spec["addr_attr"] is not None:
            setattr(HUB.config, spec["addr_attr"], addr_inputs[k].value)
        addr = addr_inputs[k].value if k in addr_inputs else None
        HUB.status[k] = "connecting…"
        refresh(k)

        async def _attempt() -> Exception | None:
            try:
                await _run_in_thread(getattr(HUB, spec["connect"]))
                return None
            except Exception as e:
                return e

        err = await _attempt()
        if err is not None and port_recovery._is_busy_error(err) and isinstance(addr, str):
            ok, msg = port_recovery.free_serial_port(addr)
            log.warning("port recovery on %s: %s", addr, msg)
            if ok:
                HUB.status[k] = f"connecting (after freeing {addr})…"
                refresh(k)
                err = await _attempt()

        if err is None:
            connection_state.record_connect(k, str(addr) if addr else None)
            refresh(k)
            return True

        HUB.status[k] = f"FAIL: {type(err).__name__}: {err}"
        refresh(k)
        return False

    async def _disconnect_one(spec) -> None:
        k = spec["key"]
        try:
            await _run_in_thread(getattr(HUB, spec["disconnect"]))
        except Exception as e:
            HUB.status[k] = f"disconnect failed: {e}"
        refresh(k)

    # Bulk action buttons up top
    async def do_connect_all():
        ui.notify("Connect all: starting…", type="info", position="top",
                  timeout=2000)
        ok, fail = [], []
        for spec in _visible_specs():
            if HUB.status.get(spec["key"], "").startswith("OK"):
                ok.append(spec["key"])
                continue
            if await _connect_one(spec):
                ok.append(spec["key"])
            else:
                fail.append(spec["key"])
        msg = f"connect all: ✓ {len(ok)} ({', '.join(ok)})"
        if fail:
            msg += f"  ·  ✗ {len(fail)} ({', '.join(fail)})"
        log.info(msg)
        # Browser may have navigated/reloaded during the multi-second connect
        # loop. ui.notify raises RuntimeError("parent element ... deleted") and
        # NiceGUI's own handler then re-raises while logging it, which wedges
        # the worker event loop. Swallow it — the log line above is the record.
        try:
            ui.notify(msg, type="positive" if not fail else "warning",
                      position="top", timeout=6000)
        except RuntimeError:
            pass

    async def do_release_all():
        """One-click disconnect for every currently-connected instrument.

        Each instrument only accepts one concurrent session, so the webapp
        must let go before a scripted bench job (scripts/bench_test.py and
        friends) can connect.  Skips anything that isn't currently OK and
        reports which ones were released.  Hidden specs (e.g. the Rigol
        DG1022) are skipped here too — manually disconnect from the
        instrument's own card on the Connections tab if needed."""
        connected = [s for s in _visible_specs()
                     if (HUB.status.get(s["key"], "") or "").startswith("OK")]
        if not connected:
            ui.notify("nothing to release — no instruments are connected",
                      type="info", position="top", timeout=3000)
            return
        names = ", ".join(s["name"] for s in connected)
        ui.notify(f"releasing {len(connected)}: {names}…",
                  type="warning", position="top", timeout=2500)
        released, failed = [], []
        for spec in connected:
            before = HUB.status.get(spec["key"], "")
            await _disconnect_one(spec)
            after = HUB.status.get(spec["key"], "")
            if after.startswith("disconnect failed"):
                failed.append(f"{spec['name']} ({after.split(':',1)[-1].strip()})")
            else:
                released.append(spec["name"])
        log.info("release-all from Connections tab: released=%s failed=%s",
                 released, failed)
        try:
            if failed:
                ui.notify(f"released {len(released)}, FAILED {len(failed)}: "
                          f"{'; '.join(failed)}",
                          type="negative", position="top", timeout=8000)
            else:
                ui.notify(f"released {len(released)} instrument(s) — bench "
                          f"scripts can now claim them",
                          type="positive", position="top", timeout=4000)
        except RuntimeError:
            pass  # see do_connect_all: page may be gone after long disconnect loop

    with ui.row().classes("w-full gap-2 items-center"):
        ui.button("⚡ connect all", on_click=do_connect_all) \
            .props("color=primary")
        ui.button("⏏ release all instruments", on_click=do_release_all) \
            .props("color=negative flat") \
            .tooltip("Disconnect every instrument the webapp is holding so "
                     "a scripted bench run can claim them. No-op for "
                     "anything not currently connected.")
        ui.html('<span class="sub" style="color:var(--mut);margin-left:.5rem">'
                'uses last-known addresses below · saved automatically on '
                'successful connect</span>')

    with ui.element("div").classes("status-grid w-full"):
        for spec in _INSTRUMENT_SPECS:
            key = spec["key"]
            with ui.card().classes("daq-card conn-card"):
                # Row 1: name + live status label
                with ui.row().classes("items-center w-full gap-2 no-wrap"):
                    ui.html(f'<span class="conn-name">{spec["name"]}</span>')
                    ui.html('<span style="flex:1"></span>')
                    status_labels[key] = ui.label(
                        HUB.status.get(key, "disconnected")
                    ).classes("text-xs num conn-status")

                # Row 2: address + connect/disconnect
                with ui.row().classes("items-center w-full gap-1 no-wrap"):
                    if spec["addr_attr"] is not None:
                        inp = ui.input(
                            placeholder=spec["addr_label"],
                            value=getattr(HUB.config, spec["addr_attr"]),
                        ).props("dense").classes("flex-1 conn-input")
                        # Keep HUB.config in sync with the input as the user
                        # types. Without this, "connect all" (which only
                        # reads HUB.config) uses stale addresses.
                        inp.bind_value(HUB.config, spec["addr_attr"])
                        addr_inputs[key] = inp
                    else:
                        ui.label(
                            f'x={HUB.config.stage_serial_x} '
                            f'y={HUB.config.stage_serial_y} '
                            f'hub={HUB.config.stage_serial_limit}'
                        ).classes("flex-1 text-gray-400 num text-xs")

                    ui.button("conn",
                              on_click=lambda s=spec: _connect_one(s)) \
                        .props("color=primary dense").classes("conn-btn")
                    ui.button("✕",
                              on_click=lambda s=spec: _disconnect_one(s)) \
                        .props("color=negative flat dense").classes("conn-btn")
                refresh(key)


# ===========================================================================
# Config tab
# ===========================================================================

def _build_config_tab():
    ui.label("Edit run parameters. Numeric fields update the hub live; lists "
             "(temperatures) apply on Enter or via the button below.") \
        .classes("text-gray-400 text-sm")

    yaml_path = ui.input(label="YAML path",
                         value=os.path.join(_REPO, "run_config.yaml")).classes("w-full")
    msg_log = ui.log(max_lines=10).classes("h-32 w-full")
    def log_msg(s: str): msg_log.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    # Each numeric field auto-syncs into HUB.config via bind_value.  load_yaml
    # mutates HUB.config in place (rather than replacing it) so the bindings
    # remain valid after reloading.
    inputs: dict[str, ui.number] = {}
    def field(label: str, attr: str, **kw):
        current = getattr(HUB.config, attr)
        is_int  = isinstance(current, int) and not isinstance(current, bool)
        fwd     = int if is_int else float
        w = ui.number(label=label, value=current, **kw).classes("w-40 num")
        w.bind_value(HUB.config, attr, forward=fwd)
        inputs[attr] = w
        return w

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>iv sweep</h2>")
            field("start (V)", "iv_voltage_start", step=0.1)
            field("stop (V)",  "iv_voltage_stop",  step=0.1)
            field("step (V)",  "iv_voltage_step",  step=0.01)
            field("pts / V",   "iv_n_per_point",   step=1)

        with ui.card().classes("daq-card"):
            ui.html("<h2>pulse</h2>")
            field("bias (V)",     "pulse_bias_v",       step=0.1)
            field("waveforms",    "pulse_n_waveforms",  step=100)
            field("pre (µs)",     "pulse_pre_us",       step=0.5)
            field("post (µs)",    "pulse_post_us",      step=0.5)
            field("threshold (V)","pulse_threshold_v",  step=0.001)

        with ui.card().classes("daq-card"):
            ui.html("<h2>temperature</h2>")
            temps_in = ui.input(label="schedule (K, comma-sep)",
                value=", ".join(f"{t:.1f}" for t in HUB.config.temperatures_K)).classes("w-64 num")
            illum_in = ui.input(label="illuminated (K)",
                value=", ".join(f"{t:.1f}" for t in HUB.config.illuminated_temperatures_K)).classes("w-64 num")
            field("tolerance (K)", "temp_tolerance_K", step=0.1)
            field("stable for (s)","temp_stable_s",    step=10)

    def apply_lists():
        """Parse and apply only the comma-separated temperature lists.
        Numeric fields are already bound live to HUB.config."""
        try:
            HUB.config.temperatures_K = [
                float(x.strip()) for x in temps_in.value.split(",") if x.strip()
            ]
            HUB.config.illuminated_temperatures_K = [
                float(x.strip()) for x in illum_in.value.split(",") if x.strip()
            ]
            log_msg(f"applied temperatures ({len(HUB.config.temperatures_K)} pts, "
                    f"{len(HUB.config.illuminated_temperatures_K)} illum)")
        except ValueError as e:
            log_msg(f"temp parse error: {e}")

    # Apply the lists whenever the user leaves the text input (blur event)
    for w in (temps_in, illum_in):
        w.on("blur", lambda _e: apply_lists())

    def load_yaml():
        try:
            new_cfg = ExperimentConfig.from_yaml(yaml_path.value)
            # Mutate the existing HUB.config in place so bound widgets keep
            # tracking the same object reference.
            for fname in HUB.config.__dataclass_fields__:
                if fname.startswith("_"): continue
                setattr(HUB.config, fname, getattr(new_cfg, fname))
            # Push refreshed values to the widgets (bindings will then write
            # any further edits straight back into HUB.config).
            for attr, w in inputs.items():
                w.value = getattr(HUB.config, attr)
            temps_in.value = ", ".join(f"{t:.1f}" for t in HUB.config.temperatures_K)
            illum_in.value = ", ".join(f"{t:.1f}" for t in HUB.config.illuminated_temperatures_K)
            log_msg(f"loaded {yaml_path.value} "
                    f"({len(HUB.config.sipm_list())} SiPMs in map)")
        except Exception as e:
            log_msg(f"load failed: {type(e).__name__}: {e}")

    def save_yaml():
        try:
            apply_lists()   # numeric fields are already in HUB.config
            HUB.config.to_yaml(yaml_path.value)
            log_msg(f"saved → {yaml_path.value}")
        except Exception as e:
            log_msg(f"save failed: {type(e).__name__}: {e}")

    with ui.row().classes("mt-2"):
        ui.button("load yaml",        on_click=load_yaml).props("color=primary")
        ui.button("save yaml",        on_click=save_yaml)
        ui.button("apply temperatures", on_click=apply_lists).props("color=secondary")


# ===========================================================================
# Lab book
# ===========================================================================

def _build_labbook_tab():
    """Free-text lab notes with optional image attachments.

    Entries land in <repo>/labbook_entries.jsonl (source of truth) and
    are also mirrored into the slowcontrol InfluxDB (measurement
    `labbook`) when HUB.sc is connected, so notes can be overlaid
    against temperature in Grafana.
    """
    # ---- compose new entry --------------------------------------------
    with ui.card().classes("daq-card w-full"):
        ui.html("<h2>new entry</h2>")

        subject_in = ui.input(
            label="subject (optional)",
        ).classes("w-full").props("dense filled")

        body_in = ui.textarea(
            label="body",
            placeholder="What's the experiment doing? Anything weird?",
        ).classes("w-full").props("dense filled autogrow")

        # Pending attachments collected before the user clicks 'post'.
        pending: list[str] = []
        with ui.row().classes("items-center gap-2 w-full"):
            attach_lbl = ui.label("no attachments").classes("text-xs") \
                .style("color:var(--mut)")

            def on_upload(e):
                # e.content is a BinaryIO-like stream
                data = e.content.read()
                fname = labbook.save_attachment(e.name, data)
                pending.append(fname)
                attach_lbl.text = (
                    f"{len(pending)} attached: {', '.join(pending)}"
                )

            ui.upload(
                on_upload=on_upload,
                multiple=True, auto_upload=True,
                label="attach plots / images",
            ).props("flat color=primary").classes("w-64")

            ui.html('<span class="sub" style="color:var(--mut)">'
                    '· or just Ctrl/Cmd+V to paste a screenshot</span>')

        # Install a document-level paste listener that uploads any image
        # found on the clipboard to /labbook-paste. The endpoint queues
        # the filename and our 0.5 s timer below picks it up. Guarded by
        # a window flag so multi-mount (e.g. reload) doesn't stack
        # listeners.
        ui.run_javascript("""
            if (!window._etsLabbookPasteInstalled) {
                window._etsLabbookPasteInstalled = true;
                document.addEventListener('paste', async (e) => {
                    if (!e.clipboardData) return;
                    for (const item of e.clipboardData.items) {
                        if (item.type && item.type.startsWith('image/')) {
                            const blob = item.getAsFile();
                            if (!blob) continue;
                            const ts = new Date().toISOString().replace(/[:.]/g, '-');
                            const ext = (item.type.split('/')[1] || 'png');
                            const fd = new FormData();
                            fd.append('file', blob, 'pasted_' + ts + '.' + ext);
                            try {
                                await fetch('/labbook-paste',
                                            { method: 'POST', body: fd });
                            } catch (err) {
                                console.error('labbook paste upload failed:', err);
                            }
                        }
                    }
                });
            }
        """)

        def _drain_pasted():
            new = labbook.pop_pasted()
            if not new:
                return
            for fname in new:
                pending.append(fname)
            attach_lbl.text = f"{len(pending)} attached: {', '.join(pending)}"
            ui.notify(f"pasted screenshot attached ({len(new)})",
                      type="positive", position="top", timeout=2000)

        ui.timer(0.5, _drain_pasted)

        with ui.row().classes("gap-2 mt-1"):
            mirror_lbl = ui.html('<span class="sub">'
                                 'InfluxDB mirror: off (sc not connected)</span>')

            def post():
                user  = app.storage.user.get("display_name", "anonymous")
                subj  = (subject_in.value or "").strip()
                body  = (body_in.value or "").strip()
                attached = list(pending)
                if not subj and not body and not attached:
                    ui.notify("nothing to post", type="warning",
                              position="top", timeout=3000)
                    return
                entry, mirrored = labbook.append(
                    user, subj, body, attached, slowcontrol=HUB.sc,
                )
                msg = f"posted by {user}"
                if mirrored:
                    msg += " (also mirrored to InfluxDB)"
                ui.notify(msg, type="positive", position="top", timeout=3500)
                log.info("labbook entry %s by %s (subject=%r, "
                         "n_attach=%d, influx=%s)",
                         entry["id"][:8], user, subj, len(attached), mirrored)
                subject_in.value = ""
                body_in.value = ""
                pending.clear()
                attach_lbl.text = "no attachments"
                entries_panel.refresh()

            ui.button("post entry", on_click=post).props("color=primary")
            ui.button("clear", on_click=lambda: (
                setattr(subject_in, "value", ""),
                setattr(body_in, "value", ""),
                pending.clear(),
                setattr(attach_lbl, "text", "no attachments"),
            )).props("flat")

        def _refresh_mirror_status():
            if HUB.sc is not None and getattr(HUB.sc, "_client", None) is not None:
                mirror_lbl.set_content(
                    '<span class="sub" style="color:var(--ok)">'
                    'InfluxDB mirror: on (entries also written to slowcontrol bucket)'
                    '</span>'
                )
            else:
                mirror_lbl.set_content(
                    '<span class="sub" style="color:var(--mut)">'
                    'InfluxDB mirror: off · connect slow control to also write entries to Influx'
                    '</span>'
                )
        _refresh_mirror_status()
        ui.timer(2.0, _refresh_mirror_status)

    # ---- past entries -------------------------------------------------
    @ui.refreshable
    def entries_panel():
        entries = labbook.list_all()
        if not entries:
            ui.html('<span class="sub" style="color:var(--mut)">'
                    'no entries yet — post your first above'
                    '</span>')
            return
        ui.html(f'<span class="sub" style="color:var(--mut)">'
                f'{len(entries)} entries · newest first</span>')
        for entry in entries:
            with ui.card().classes("daq-card w-full"):
                ts = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(entry["ts"]))
                # header line: timestamp · user · subject
                header_html = (
                    f'<span class="sub" style="color:var(--mut)">{ts}</span>'
                    f' · <strong style="color:var(--acc)">{entry["user"]}</strong>'
                )
                if entry.get("subject"):
                    header_html += (
                        f' · <span style="color:var(--fg)">'
                        f'{entry["subject"]}</span>'
                    )
                ui.html(header_html)

                if entry.get("body"):
                    # Preserve line breaks but escape HTML angle brackets.
                    body_safe = (entry["body"]
                                 .replace("&", "&amp;")
                                 .replace("<", "&lt;")
                                 .replace(">", "&gt;")
                                 .replace("\n", "<br/>"))
                    ui.html(f'<div style="white-space:pre-wrap; '
                            f'margin-top:.3rem; font-size:.88rem">'
                            f'{body_safe}</div>')

                if entry.get("attachments"):
                    with ui.row().classes("gap-2 flex-wrap mt-2"):
                        for a in entry["attachments"]:
                            ui.html(
                                f'<a href="/labbook-img/{a}" target="_blank" '
                                f'title="{a}">'
                                f'<img src="/labbook-img/{a}" '
                                f'style="max-height:180px; max-width:280px; '
                                f'border:1px solid var(--line); '
                                f'border-radius:4px; background:#000"/></a>'
                            )

    entries_panel()


# ===========================================================================
# Level 1 — primitives
# ===========================================================================

def _build_level1_tab():
    """Dashboard of single-instrument primitives.

    Uniform card grid (auto-fill, 290 px min). Wide cards (webcam, stage
    move program, vx2740 waveform) span two columns. Label-above fields
    with unit-suffix-inside; one primary action per card; red reserved
    for destructive / energised states.
    """
    import numpy as _np
    program: list[dict] = []

    with ui.element("div").classes("l1-panel w-full"):
        ui.html('<p class="intro">Manual single-instrument operations. '
                'Connect first on the Connections tab.</p>')

        with ui.element("div").classes("dash w-full"):

            # =========== WEBCAM (span 2) ===========
            with ui.element("div").classes("l1-card span2"):
                ui.html('<p class="eyebrow">Webcam</p>')
                ui.html(
                    '<div class="cam">'
                    '<span class="live-pill"><span class="d"></span>live</span>'
                    '<img src="/webcam.mjpeg" alt="webcam stream"/>'
                    '</div>'
                )

            # =========== STAGE ===========
            with ui.element("div").classes("l1-card"):
                ui.html('<p class="eyebrow">Stage</p>')
                with ui.element("div").classes("frow cols-2"):
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">X</label>')
                        x_in = ui.number(value=0.0, step=0.1, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="mm"')
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Y</label>')
                        y_in = ui.number(value=0.0, step=0.1, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="mm"')

                stage_pos = ui.html('<span class="result">pos: '
                                    '<b><span class="muted">—</span></b></span>')

                async def stage_move():
                    if HUB.stage is None:
                        log_msg("stage not connected"); return
                    log_msg(f"move → ({x_in.value:.2f}, {y_in.value:.2f}) mm")
                    try:
                        await _run_in_thread(
                            P.move_stage, HUB.stage,
                            float(x_in.value), float(y_in.value),
                            HUB.config.stage_deenergize,
                        )
                        x, y = await _run_in_thread(P.stage_position, HUB.stage)
                        log_msg(f"  arrived ({x:.3f}, {y:.3f}) mm")
                        stage_pos.set_content(
                            f'<span class="result">pos: '
                            f'<b>X {x:+.3f} mm · Y {y:+.3f} mm</b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  move FAIL: {type(e).__name__}: {e}")

                async def stage_read():
                    if HUB.stage is None:
                        log_msg("stage not connected"); return
                    try:
                        x, y = await _run_in_thread(P.stage_position, HUB.stage)
                        log_msg(f"position: ({x:.3f}, {y:.3f}) mm")
                        stage_pos.set_content(
                            f'<span class="result">pos: '
                            f'<b>X {x:+.3f} mm · Y {y:+.3f} mm</b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  read FAIL: {type(e).__name__}: {e}")

                async def stage_home():
                    if HUB.stage is None:
                        log_msg("stage not connected"); return
                    log_msg("homing stage…")
                    try:
                        await _run_in_thread(P.home_stage, HUB.stage)
                        log_msg("  homed")
                        stage_pos.set_content(
                            '<span class="result">pos: <b>X 0.000 mm · Y 0.000 mm</b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  home FAIL: {type(e).__name__}: {e}")

                with ui.element("div").classes("btnrow") \
                        .style("margin-bottom:12px"):
                    ui.button("move", on_click=stage_move).props("color=primary")
                    ui.button("read pos", on_click=stage_read).props("flat")
                    ui.button("home", on_click=stage_home).props("flat")

                with ui.element("div").classes("fld") \
                        .style("max-width:140px; margin-bottom:8px"):
                    ui.html('<label class="fld-lbl">Jog step</label>')
                    jog_step = ui.number(value=1.0, step=0.1, min=0.0,
                                         format="%.2f") \
                        .props('dense filled hide-bottom-space suffix="mm"')

                async def _jog(dx_sign: float, dy_sign: float):
                    if HUB.stage is None:
                        log_msg("stage not connected"); return
                    step = float(jog_step.value or 0)
                    dx, dy = dx_sign * step, dy_sign * step
                    log_msg(f"jog Δ=({dx:+.3f}, {dy:+.3f}) mm")
                    try:
                        await _run_in_thread(
                            HUB.stage.move_by, dx, dy,
                            HUB.config.stage_deenergize,
                        )
                        x, y = await _run_in_thread(P.stage_position, HUB.stage)
                        x_in.value, y_in.value = float(x), float(y)
                        stage_pos.set_content(
                            f'<span class="result">pos: '
                            f'<b>X {x:+.3f} mm · Y {y:+.3f} mm</b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  jog FAIL: {type(e).__name__}: {e}")

                with ui.element("div").classes("jog"):
                    ui.button("− X", on_click=lambda: _jog(-1, 0)).props("flat")
                    ui.button("+ X", on_click=lambda: _jog(+1, 0)).props("flat")
                    ui.button("− Y", on_click=lambda: _jog(0, -1)).props("flat")
                    ui.button("+ Y", on_click=lambda: _jog(0, +1)).props("flat")

                # Camera-view axis indicator — same diagram used on the
                # dedicated stage tab. Shows where X+ and Y+ point as
                # seen through the webcam.
                ui.html(
                    '<div class="axis-diagram" '
                    'title="frame of reference seen on the webcam" '
                    'style="margin-top:10px; align-self:flex-start">'
                    '<div class="axis-cap">camera view · X+ ↓ · Y+ ←</div>'
                    '<svg viewBox="0 0 110 90" '
                    'width="100%" style="max-width:130px">'
                    '<defs>'
                    '<marker id="ah-l1" viewBox="0 0 10 10" '
                    'refX="9" refY="5" markerWidth="6" markerHeight="6" '
                    'orient="auto">'
                    '<path d="M0 0 L10 5 L0 10 z" fill="#58a6ff"/>'
                    '</marker>'
                    '</defs>'
                    # origin
                    '<circle cx="60" cy="40" r="2.5" fill="#8a93a6"/>'
                    # +Y → LEFT
                    '<line x1="60" y1="40" x2="14" y2="40" '
                    'stroke="#58a6ff" stroke-width="1.8" '
                    'marker-end="url(#ah-l1)"/>'
                    '<text x="6" y="36" fill="#58a6ff" '
                    'font-size="11" font-family="ui-monospace,Menlo,monospace">'
                    'Y+</text>'
                    # +X → DOWN
                    '<line x1="60" y1="40" x2="60" y2="82" '
                    'stroke="#58a6ff" stroke-width="1.8" '
                    'marker-end="url(#ah-l1)"/>'
                    '<text x="66" y="80" fill="#58a6ff" '
                    'font-size="11" font-family="ui-monospace,Menlo,monospace">'
                    'X+</text>'
                    # negative ticks (faint)
                    '<line x1="60" y1="40" x2="100" y2="40" '
                    'stroke="#5c6775" stroke-width="1" '
                    'stroke-dasharray="2 2"/>'
                    '<text x="90" y="36" fill="#5c6775" '
                    'font-size="10" font-family="ui-monospace,Menlo,monospace">'
                    'Y−</text>'
                    '<line x1="60" y1="40" x2="60" y2="6" '
                    'stroke="#5c6775" stroke-width="1" '
                    'stroke-dasharray="2 2"/>'
                    '<text x="66" y="14" fill="#5c6775" '
                    'font-size="10" font-family="ui-monospace,Menlo,monospace">'
                    'X−</text>'
                    '</svg>'
                    '</div>'
                )

            # =========== STAGE MOVE PROGRAM (span 2) ===========
            with ui.element("div").classes("l1-card span2"):
                ui.html('<p class="eyebrow">Stage move program</p>')
                ui.html('<p style="font-size:12px; color:var(--mut); '
                        'opacity:.85; margin:0 0 12px">'
                        'Build a list of move steps; each can include X, Y, '
                        'or both. Unchecked axes stay put. Run the whole '
                        'list sequentially.</p>')

                with ui.row().classes("items-end w-full") \
                        .style("gap:12px; flex-wrap:wrap; margin-bottom:12px"):
                    with ui.element("div").classes("fld").style("width:110px"):
                        ui.html('<label class="fld-lbl">X</label>')
                        prog_x = ui.number(value=0.0, step=0.1, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="mm"')
                    with ui.element("div").classes("fld").style("width:110px"):
                        ui.html('<label class="fld-lbl">Y</label>')
                        prog_y = ui.number(value=0.0, step=0.1, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="mm"')
                    with ui.row().classes("tgl-inline items-center"):
                        prog_xen = ui.switch(value=True).props("dense")
                        ui.html('<span>X</span>')
                    with ui.row().classes("tgl-inline items-center"):
                        prog_yen = ui.switch(value=True).props("dense")
                        ui.html('<span>Y</span>')
                    with ui.element("div").classes("fld").style("width:110px"):
                        ui.html('<label class="fld-lbl">Settle</label>')
                        prog_settle = ui.number(value=0.05, step=0.01,
                                                min=0.0, format="%.2f") \
                            .props('dense filled hide-bottom-space suffix="s"')
                    with ui.row().classes("tgl-inline items-center"):
                        prog_deen = ui.switch(value=True).props("dense")
                        ui.html('<span>de-energize after each step</span>')

                steps_el = ui.html('<div class="steps">'
                                   '<span class="empty">empty</span></div>')
                status_lbl = ui.html('<span class="result">ready</span>')

                def _render_list():
                    if not program:
                        steps_el.set_content('<div class="steps">'
                                             '<span class="empty">empty</span></div>')
                        return
                    rows = []
                    for i, step in enumerate(program):
                        # Each step has a 'kind' — "move" (default) or "home".
                        # Home steps re-zero the coordinate origin via the
                        # phidget stage's home() routine.
                        kind = step.get("kind", "move")
                        if kind == "home":
                            label = "HOME both (drive to limit switches, "
                            "re-zero origin)"
                            rows.append(
                                f'<div class="step">{i+1}.  '
                                f'<strong>HOME both</strong>  ·  '
                                f'drives to limit switches, re-zeros (0, 0)'
                                f'</div>'
                            )
                            continue
                        parts = []
                        if step["x"] is not None:
                            parts.append(f"x={step['x']:+.3f}")
                        else:
                            parts.append("x=—")
                        if step["y"] is not None:
                            parts.append(f"y={step['y']:+.3f}")
                        else:
                            parts.append("y=—")
                        parts.append(f"settle {step['settle_s']:.2f}s")
                        if step["deenergize"]:
                            parts.append("de-en")
                        rows.append(
                            f'<div class="step">{i+1}.  '
                            f'{"  ·  ".join(parts)}</div>'
                        )
                    steps_el.set_content(
                        '<div class="steps">' + "".join(rows) + '</div>'
                    )

                def add_step():
                    if not prog_xen.value and not prog_yen.value:
                        status_lbl.set_content('<span class="result">'
                                               'neither X nor Y selected</span>')
                        return
                    program.append({
                        "kind":       "move",
                        "x":          float(prog_x.value) if prog_xen.value else None,
                        "y":          float(prog_y.value) if prog_yen.value else None,
                        "settle_s":   float(prog_settle.value),
                        "deenergize": bool(prog_deen.value),
                    })
                    _render_list()
                    status_lbl.set_content(
                        f'<span class="result">{len(program)} step(s) queued</span>'
                    )

                def add_home_step():
                    """Append a 'HOME both' step.  At run-time this calls
                    P.home_stage(stage), which drives each axis into its
                    home limit switch and resets position to (0, 0).
                    Useful as the first step of a program to guarantee a
                    known reference before any absolute moves."""
                    program.append({"kind": "home"})
                    _render_list()
                    status_lbl.set_content(
                        f'<span class="result">{len(program)} step(s) queued</span>'
                    )

                def remove_last():
                    if program:
                        program.pop()
                        _render_list()
                        status_lbl.set_content(
                            f'<span class="result">{len(program)} step(s) queued</span>'
                        )

                def clear_all():
                    program.clear()
                    _render_list()
                    status_lbl.set_content('<span class="result">cleared</span>')

                async def run_all():
                    if HUB.stage is None:
                        status_lbl.set_content('<span class="result">'
                                               'stage not connected</span>'); return
                    if not program:
                        status_lbl.set_content('<span class="result">'
                                               'list is empty</span>'); return
                    log_msg(f"running {len(program)} step program…")
                    for i, step in enumerate(program):
                        kind = step.get("kind", "move")
                        if kind == "home":
                            desc = f"step {i+1}/{len(program)}: HOME both"
                        else:
                            xs = "—" if step["x"] is None else f"{step['x']:.3f}"
                            ys = "—" if step["y"] is None else f"{step['y']:.3f}"
                            desc = f"step {i+1}/{len(program)}: x={xs}, y={ys}"
                        status_lbl.set_content(f'<span class="result">{desc}</span>')
                        log_msg(f"  {desc}")
                        try:
                            if kind == "home":
                                await _run_in_thread(P.home_stage, HUB.stage)
                            else:
                                await _run_in_thread(
                                    P.move_stage, HUB.stage,
                                    step["x"], step["y"],
                                    step["deenergize"], step["settle_s"],
                                )
                            x, y = await _run_in_thread(P.stage_position, HUB.stage)
                            log_msg(f"    at ({x:.3f}, {y:.3f}) mm")
                        except Exception as e:
                            status_lbl.set_content(
                                f'<span class="result">'
                                f'FAIL at step {i+1}: {type(e).__name__}</span>'
                            )
                            log_msg(f"    FAIL: {type(e).__name__}: {e}")
                            return
                    status_lbl.set_content(
                        f'<span class="result">'
                        f'done — {len(program)} step(s) executed</span>'
                    )
                    log_msg("program done")

                with ui.element("div").classes("btnrow"):
                    ui.button("add step", on_click=add_step) \
                        .props("color=primary")
                    ui.button("+ HOME both", on_click=add_home_step) \
                        .props("color=warning flat") \
                        .tooltip("Append a HOME step. Run-time: drives "
                                 "both axes into their home limit switches "
                                 "and resets position to (0, 0).")
                    ui.button("remove last", on_click=remove_last) \
                        .props("flat")
                    ui.button("clear", on_click=clear_all) \
                        .props("color=negative flat")
                    ui.html('<span style="flex:1"></span>')
                    ui.button("▶ run all", on_click=run_all) \
                        .props("color=primary")

            # =========== MUX CHANNEL ===========
            with ui.element("div").classes("l1-card"):
                ui.html('<p class="eyebrow">MUX channel</p>')
                with ui.element("div").classes("fld") \
                        .style("margin-bottom:12px"):
                    ui.html('<label class="fld-lbl">Channel (1–96)</label>')
                    mux_ch_in = ui.number(value=1, step=1,
                                          min=1, max=96, format="%d") \
                        .props('dense filled hide-bottom-space')

                mux_active = ui.html('<span class="result">active: '
                                     '<b><span class="muted">—</span></b></span>')

                async def select_ch():
                    if HUB.mux is None:
                        log_msg("mux not connected"); return
                    ch = int(mux_ch_in.value or 1)
                    log_msg(f"mux → ch {ch}")
                    try:
                        await _run_in_thread(P.select_channel, HUB.mux, ch)
                        log_msg(f"  ch {ch} active")
                        mux_active.set_content(
                            f'<span class="result">active: <b>ch{ch}</b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  select FAIL: {type(e).__name__}: {e}")

                async def zero_mux():
                    if HUB.mux is None:
                        log_msg("mux not connected"); return
                    try:
                        await _run_in_thread(P.zero_channels, HUB.mux)
                        log_msg("mux zeroed")
                        mux_active.set_content(
                            '<span class="result">active: <b><span class="muted">none</span></b></span>'
                        )
                    except Exception as e:
                        log_msg(f"  zero FAIL: {type(e).__name__}: {e}")

                with ui.element("div").classes("btnrow"):
                    ui.button("select", on_click=select_ch) \
                        .props("color=primary")
                    ui.button("zero", on_click=zero_mux).props("flat")

            # =========== BIAS · b2987 ===========
            with ui.element("div").classes("l1-card"):
                ui.html('<p class="eyebrow">Bias · b2987</p>')
                with ui.element("div").classes("fld") \
                        .style("margin-bottom:12px"):
                    ui.html('<label class="fld-lbl">Voltage setpoint</label>')
                    bias_v_in = ui.number(value=0.0, step=0.5, format="%.3f") \
                        .props('dense filled hide-bottom-space suffix="V"')

                with ui.row().classes("items-center w-full") \
                        .style("gap:10px; margin-bottom:12px"):
                    ui.html('<span style="font-size:13px">Bias output</span>')
                    bias_state = ui.html(
                        '<span class="result" style="margin:0">off</span>'
                    )
                    bias_sw = ui.switch(value=False) \
                        .props("dense color=negative") \
                        .style("margin-left:auto")

                bias_i_html = ui.html(
                    '<span class="result">I = '
                    '<b><span class="muted">—</span></b> A</span>'
                )

                async def set_bias():
                    if HUB.elec is None:
                        log_msg("electrometer not connected"); return
                    v = float(bias_v_in.value or 0)
                    log_msg(f"set_bias {v:.3f} V")
                    try:
                        await _run_in_thread(P.set_bias, HUB.elec, v, 0.2)
                        note_bias(v_set=v, output_on=True)
                        bias_sw.value = True
                        bias_state.set_content(
                            '<span class="result" style="margin:0; color:#ffb4b4">on</span>'
                        )
                        log_msg("  bias on")
                    except Exception as e:
                        log_msg(f"  set_bias FAIL: {type(e).__name__}: {e}")

                async def _on_bias_toggle(_e):
                    if HUB.elec is None:
                        log_msg("electrometer not connected")
                        bias_sw.value = False
                        return
                    on = bool(_e.value)
                    try:
                        if on:
                            v = float(bias_v_in.value or 0)
                            await _run_in_thread(P.set_bias, HUB.elec, v, 0.2)
                            note_bias(v_set=v, output_on=True)
                            bias_state.set_content(
                                '<span class="result" style="margin:0; color:#ffb4b4">on</span>'
                            )
                            log_msg(f"bias on @ {v:.3f} V")
                        else:
                            await _run_in_thread(P.bias_off, HUB.elec)
                            note_bias(v_set=0.0, output_on=False)
                            bias_state.set_content(
                                '<span class="result" style="margin:0">off</span>'
                            )
                            log_msg("bias off")
                    except Exception as e:
                        log_msg(f"  bias toggle FAIL: {type(e).__name__}: {e}")
                        bias_sw.value = not on
                bias_sw.on_value_change(_on_bias_toggle)

                async def read_bias_i():
                    if HUB.elec is None:
                        log_msg("electrometer not connected"); return
                    try:
                        i = await _run_in_thread(P.measure_current, HUB.elec)
                        note_bias(i_meas=i)
                        log_msg(f"I = {i:.3e} A")
                        bias_i_html.set_content(
                            f'<span class="result">I = <b>{i:.3e}</b> A</span>'
                        )
                    except Exception as e:
                        log_msg(f"  measure FAIL: {type(e).__name__}: {e}")

                with ui.element("div").classes("btnrow"):
                    ui.button("set bias", on_click=set_bias) \
                        .props("color=primary")
                    ui.button("read I", on_click=read_bias_i).props("flat")

            # =========== TEMPERATURE ===========
            with ui.element("div").classes("l1-card"):
                ui.html('<p class="eyebrow">Temperature</p>')

                temp_html = ui.html(
                    '<span class="result">T = '
                    '<b><span class="muted">—</span></b> K</span>'
                )

                async def read_temp():
                    if HUB.sc is None:
                        log_msg("slow control not connected"); return
                    try:
                        T = await _run_in_thread(P.read_temperature, HUB.sc)
                        log_msg(f"T = {T:.3f} K")
                        temp_html.set_content(
                            f'<span class="result">'
                            f'T = <b>{T:.3f}</b> K</span>'
                        )
                    except Exception as e:
                        log_msg(f"  temp FAIL: {type(e).__name__}: {e}")

                with ui.element("div").classes("btnrow"):
                    ui.button("read T", on_click=read_temp) \
                        .props("color=primary")

            # =========== VX2740 SINGLE WAVEFORM (span 2) ===========
            with ui.element("div").classes("l1-card span2"):
                ui.html('<p class="eyebrow">vx2740 · single waveform</p>')
                with ui.row().classes("items-end w-full") \
                        .style("gap:8px; flex-wrap:wrap; margin-bottom:12px"):
                    with ui.element("div").classes("fld").style("width:120px"):
                        ui.html('<label class="fld-lbl">Channel (0–63)</label>')
                        wf_ch_in = ui.number(value=0, step=1, min=0, max=63,
                                             format="%d") \
                            .props('dense filled hide-bottom-space')
                    with ui.element("div").classes("fld").style("width:130px"):
                        ui.html('<label class="fld-lbl">Threshold</label>')
                        wf_thr_in = ui.number(value=50, step=10, format="%d") \
                            .props('dense filled hide-bottom-space suffix="ADC"')
                    with ui.element("div").classes("fld").style("width:100px"):
                        ui.html('<label class="fld-lbl">Pre</label>')
                        wf_pre_in = ui.number(value=2.0, step=0.5, format="%.2f") \
                            .props('dense filled hide-bottom-space suffix="µs"')
                    with ui.element("div").classes("fld").style("width:100px"):
                        ui.html('<label class="fld-lbl">Post</label>')
                        wf_post_in = ui.number(value=10.0, step=1.0, format="%.2f") \
                            .props('dense filled hide-bottom-space suffix="µs"')
                    with ui.element("div").classes("fld").style("width:100px"):
                        ui.html('<label class="fld-lbl">Timeout</label>')
                        wf_to_in = ui.number(value=10.0, step=1.0, format="%.1f") \
                            .props('dense filled hide-bottom-space suffix="s"')

                wf_chart_opts = {
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 56, "right": 14, "top": 12, "bottom": 32},
                    "backgroundColor": "transparent",
                    "textStyle": {"color": "#dde3ee"},
                    "xAxis": {
                        "type": "value", "name": "time from trigger (µs)",
                        "nameLocation": "middle", "nameGap": 20, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "yAxis": {
                        "type": "value", "name": "ADC (bl-sub)",
                        "nameLocation": "middle", "nameGap": 42, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "series": [{
                        "type": "line", "showSymbol": False,
                        "data": [],
                        "lineStyle": {"width": 1.4, "color": "#3b82f6"},
                    }],
                }
                with ui.element("div").classes("plotbox-l1") as wf_plotbox:
                    wf_chart = ui.echart(wf_chart_opts) \
                        .classes("w-full").style("height:100%")
                    wf_empty = ui.html(
                        '<div class="plot-empty">no capture yet</div>'
                    )

                wf_summary = ui.html(
                    '<span class="result"><span class="muted">no capture yet</span></span>'
                )

                async def capture_wf():
                    if HUB.dig is None:
                        log_msg("digitizer not connected"); return
                    ctrl = getattr(HUB.dig, "_ctrl", None)
                    if ctrl is None:
                        log_msg("digitizer backend has no controller"); return
                    ch = max(0, min(63, int(wf_ch_in.value)))
                    thr = int(wf_thr_in.value)
                    pre = float(wf_pre_in.value)
                    post = float(wf_post_in.value)
                    to = float(wf_to_in.value)
                    log_msg(f"L1 waveform: ch{ch}, thr={thr} ADC, "
                            f"pre={pre} µs, post={post} µs")

                    def _grab():
                        ctrl.configure_record_window(pre_us=pre, post_us=post)
                        # Same lesson as the digitizer page: routing ch 4
                        # through include_pmt makes the controller add it
                        # to the trigger set but DROP its data from the
                        # AcquisitionResult (the post-loop iterates over
                        # `_sipm_channels` only).  Pass every captured
                        # channel — including ch 4 — in sipm_channels so
                        # the waveform lands in result.waveforms[ch].
                        ctrl.configure_channels(
                            sipm_channels=[ch],
                            thresholds={ch: thr},
                            threshold_mode="per_channel",
                            include_pmt=False,
                        )
                        ctrl.configure_trigger(mode="self")
                        ctrl.arm()
                        try:
                            return ctrl.acquire(
                                n_waveforms=1, batch_size=1,
                                store_waveforms=True, timeout_s=to,
                            )
                        finally:
                            ctrl.disarm()

                    try:
                        result = await _run_in_thread(_grab)
                    except Exception as e:
                        log_msg(f"  capture FAIL: {type(e).__name__}: {e}")
                        return

                    waves = result.waveforms.get(ch) if hasattr(result, "waveforms") else None
                    # `waves` is None when the channel isn't in the result
                    # (e.g. PMT routed via include_pmt rather than
                    # sipm_channels), or a numpy 2-D array of shape
                    # (n_wfs, n_samples) when captured.  Don't use `not
                    # waves` — that triggers numpy's truth-value-ambiguity
                    # error on a multi-element array.
                    if waves is None or len(waves) == 0:
                        log_msg("  capture returned no waveform"); return
                    w = _np.asarray(waves[0], dtype=_np.float64)
                    n_pre = max(1, int(round(pre * 125.0)))
                    baseline = float(w[:n_pre].mean())
                    w_bl = w - baseline
                    peak = float(w_bl.max())
                    t_us = _np.arange(len(w)) / 125.0 - pre  # µs from trigger

                    wf_chart.options["series"][0]["data"] = list(zip(
                        [float(x) for x in t_us],
                        [float(y) for y in w_bl],
                    ))
                    wf_chart.update()
                    wf_empty.set_visibility(False)
                    wf_summary.set_content(
                        f'<span class="result">'
                        f'ch{ch}: peak <b>{peak:+.0f}</b> ADC, '
                        f'baseline <b>{baseline:.0f}</b>, '
                        f'N=<b>{len(w)}</b> samples</span>'
                    )
                    log_msg(f"  ch{ch} peak={peak:+.0f} ADC  "
                            f"baseline={baseline:.0f}  ({len(w)} samples)")

                with ui.element("div").classes("btnrow"):
                    ui.button("capture", on_click=capture_wf) \
                        .props("color=primary")

            # =========== CURRENT SAMPLES ===========
            with ui.element("div").classes("l1-card"):
                ui.html('<p class="eyebrow">Current samples</p>')

                _K_RANGES = {
                    "AUTO":   "AUTO",
                    "2 nA":   2e-9,  "20 nA":  2e-8, "200 nA": 2e-7,
                    "2 µA":   2e-6,  "20 µA":  2e-5, "200 µA": 2e-4,
                    "2 mA":   2e-3,  "20 mA":  2e-2,
                }
                with ui.element("div").classes("frow cols-2"):
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Instrument</label>')
                        ci_inst = ui.select(["K6485", "B2987"], value="K6485") \
                            .props("dense filled hide-bottom-space")
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Range (K6485)</label>')
                        ci_range = ui.select(list(_K_RANGES.keys()),
                                             value="AUTO") \
                            .props("dense filled hide-bottom-space")
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">N samples</label>')
                        ci_n = ui.number(value=50, step=10, min=1, format="%d") \
                            .props('dense filled hide-bottom-space suffix="#"')
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Delay</label>')
                        ci_delay = ui.number(value=0.05, step=0.01,
                                             min=0.0, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="s"')

                ci_result = ui.html(
                    '<span class="result">I = '
                    '<b><span class="muted">—</span></b></span>'
                )

                async def read_current_samples():
                    inst = ci_inst.value
                    n = max(1, int(ci_n.value))
                    d = max(0.0, float(ci_delay.value))
                    if inst == "K6485":
                        if HUB.k6485 is None:
                            log_msg("k6485 not connected"); return
                        rng_label = ci_range.value
                        rng_val = _K_RANGES.get(rng_label, "AUTO")
                        def _read():
                            HUB.k6485.set_range(rng_val)
                            return HUB.k6485.read_n(n, d)
                        try:
                            arr, _ts = await _run_in_thread(_read)
                        except Exception as e:
                            log_msg(f"  K6485 read FAIL: "
                                    f"{type(e).__name__}: {e}"); return
                        where = f"K6485 @ {rng_label}"
                    else:  # B2987
                        if HUB.elec is None:
                            log_msg("electrometer not connected"); return
                        def _read():
                            xs = _np.empty(n, dtype=_np.float64)
                            for i in range(n):
                                xs[i] = HUB.elec.measure_current()
                                if d > 0 and i + 1 < n:
                                    time.sleep(d)
                            return xs
                        try:
                            arr = await _run_in_thread(_read)
                        except Exception as e:
                            log_msg(f"  B2987 read FAIL: "
                                    f"{type(e).__name__}: {e}"); return
                        where = "B2987 (current range)"
                    mean = float(_np.mean(arr))
                    std = (float(_np.std(arr, ddof=1))
                           if len(arr) > 1 else 0.0)
                    ci_result.set_content(
                        f'<span class="result">'
                        f'I = <b>{mean:+.4e}</b> ± {std:.3e} A · N=<b>{len(arr)}</b></span>'
                    )
                    log_msg(f"{where}  N={n}  μ={mean:+.4e} A  σ={std:.3e} A")

                with ui.element("div").classes("btnrow"):
                    ui.button("read", on_click=read_current_samples) \
                        .props("color=primary")

    # Tiny activity log under the dashboard, for diagnostics.
    log_lbl = ui.log(max_lines=18).classes("h-32 w-full") \
        .style("margin-top:14px")
    def log_msg(s: str):
        log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")


# ===========================================================================
# Level 2 — single SiPM
# ===========================================================================

def _build_l2_plots(PLOTS: dict, dirty: dict) -> None:
    """Build the L2 right-hand live-plot column and register streaming
    callbacks in PLOTS.

    Four stacked echart views:
      iv      — current vs bias, dark + bright overlaid (live, per-point)
      scan    — current vs stage position, X + Y overlaid (live, per-point)
      charge  — amplitude histogram (baseline-subtracted, from the
                digitizer), dark + bright overlaid (per pulse run)
      wave    — a single stored waveform with prev/next scroll

    Overlay rule is "replace same slot, keep the other": a new dark IV
    run overwrites the dark trace but leaves bright untouched (and
    symmetrically for scan X/Y and charge dark/bright).  Each view has a
    clear button that wipes both slots.

    Registered callbacks (all best-effort, called via the run handlers'
    `_plot(...)` wrapper):
      iv_begin(label)            reset the dark|bright IV slot
      iv_point(label, v, i)      append a point (worker-thread safe)
      iv_redraw()                force redraw (b2987 batch path)
      scan_begin(axis)           reset the x|y scan slot
      scan_point(axis, pos, i)   append a point (main thread)
      charge_set(label, result, ch)   set the dark|bright histogram
      wave_set(result, ch, label, pre_us)   load waveforms for scrolling
    """
    import numpy as _np

    _SAMPLES_PER_US = 125.0       # VX2740 runs at 125 MS/s
    _DARK_COLOR   = "#3b82f6"
    _BRIGHT_COLOR = "#f59e0b"
    _X_COLOR      = "#3b82f6"
    _Y_COLOR      = "#22c55e"

    def _axis(name, gap):
        return {
            "type": "value", "name": name,
            "nameLocation": "middle", "nameGap": gap, "scale": True,
            "axisLine":  {"lineStyle": {"color": "#5c6775"}},
            "axisLabel": {"color": "#8a93a6", "fontSize": 10},
            "splitLine": {"lineStyle": {"color": "#1d2733"}},
        }

    def _base(xname, yname, ygap=56):
        return {
            "tooltip": {"trigger": "axis"},
            "legend": {"textStyle": {"color": "#dde3ee"}, "top": 2,
                       "right": 8, "data": []},
            "grid": {"left": 66, "right": 16, "top": 30, "bottom": 36},
            "backgroundColor": "transparent",
            "textStyle": {"color": "#dde3ee"},
            "xAxis": _axis(xname, 22),
            "yAxis": _axis(yname, ygap),
            "series": [],
        }

    def _plotbox(opts):
        with ui.element("div").style(
                "position:relative;width:100%;height:200px;"
                "border:1px solid var(--line);border-radius:8px;"
                "background:var(--panel2)"):
            return ui.echart(opts).classes("w-full").style("height:100%")

    def _two_trace(opts, names, colors, kind="line"):
        opts["legend"]["data"] = list(names)
        for nm, col in zip(names, colors):
            s = {"name": nm, "type": kind, "showSymbol": False, "data": [],
                 "lineStyle": {"width": 1.6, "color": col},
                 "itemStyle": {"color": col}}
            opts["series"].append(s)

    # ----------------------------- IV --------------------------------
    with ui.card().classes("daq-card w-full"):
        with ui.row().classes("w-full items-center"):
            ui.html("<h2>iv — current vs bias</h2>")
            ui.element("div").style("flex:1")
            ui.button("clear", on_click=lambda: iv_clear()).props("flat dense")
        iv_opts = _base("bias (V)", "current (A)")
        _two_trace(iv_opts, ["dark", "bright"], [_DARK_COLOR, _BRIGHT_COLOR])
        iv_chart = _plotbox(iv_opts)
        iv_store = {"dark": [], "bright": []}

        def _iv_redraw():
            iv_chart.options["series"][0]["data"] = list(iv_store["dark"])
            iv_chart.options["series"][1]["data"] = list(iv_store["bright"])
            iv_chart.update()

        def iv_begin(label):
            iv_store[label] = []
            dirty["iv"] = True

        def iv_point(label, v, i):
            iv_store[label].append([float(v), float(i)])
            dirty["iv"] = True       # pumped onto the UI by the timer below

        def iv_clear():
            iv_store["dark"] = []; iv_store["bright"] = []
            _iv_redraw()

        PLOTS.update(iv_begin=iv_begin, iv_point=iv_point,
                     iv_redraw=_iv_redraw)

    # ---------------------------- scan -------------------------------
    with ui.card().classes("daq-card w-full"):
        with ui.row().classes("w-full items-center"):
            ui.html("<h2>scan — current vs position</h2>")
            ui.element("div").style("flex:1")
            ui.button("clear", on_click=lambda: scan_clear()).props("flat dense")
        scan_opts = _base("position (mm)", "current (A)")
        _two_trace(scan_opts, ["X", "Y"], [_X_COLOR, _Y_COLOR])
        scan_chart = _plotbox(scan_opts)
        scan_store = {"x": [], "y": []}

        def _scan_redraw():
            scan_chart.options["series"][0]["data"] = list(scan_store["x"])
            scan_chart.options["series"][1]["data"] = list(scan_store["y"])
            scan_chart.update()

        def scan_begin(axis):
            scan_store[axis] = []
            _scan_redraw()

        def scan_point(axis, pos, i):
            scan_store[axis].append([float(pos), float(i)])
            _scan_redraw()           # run_scan loop is on the main thread

        def scan_clear():
            scan_store["x"] = []; scan_store["y"] = []
            _scan_redraw()

        PLOTS.update(scan_begin=scan_begin, scan_point=scan_point)

    # --------------------------- charge ------------------------------
    with ui.card().classes("daq-card w-full"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.html("<h2>charge spectrum</h2>")
            ui.element("div").style("flex:1")
            chg_bins = ui.number(label="bins", value=100, min=10, step=10) \
                .classes("w-24 num").props("dense")
            ui.button("clear", on_click=lambda: charge_clear()).props("flat dense")
        chg_opts = _base("amplitude (ADC, baseline-sub)", "counts", ygap=46)
        _two_trace(chg_opts, ["dark", "bright"], [_DARK_COLOR, _BRIGHT_COLOR])
        for s in chg_opts["series"]:
            s["step"] = "middle"     # step lines overlay more legibly
        chg_chart = _plotbox(chg_opts)
        chg_store = {"dark": None, "bright": None}   # raw amplitude arrays

        def _chg_redraw():
            bins = max(10, int(chg_bins.value or 100))
            for idx, label in enumerate(("dark", "bright")):
                amps = chg_store[label]
                if amps is None or len(amps) == 0:
                    chg_chart.options["series"][idx]["data"] = []
                    continue
                arr = _np.asarray(amps, dtype=float)
                hist, edges = _np.histogram(arr, bins=bins)
                chg_chart.options["series"][idx]["data"] = [
                    [float((edges[i] + edges[i + 1]) / 2), int(h)]
                    for i, h in enumerate(hist)]
            chg_chart.update()

        def charge_set(label, result, ch):
            try:
                amps = result.amplitudes.get(ch)
            except Exception:
                amps = None
            chg_store[label] = list(amps) if amps is not None else []
            _chg_redraw()

        def charge_clear():
            chg_store["dark"] = None; chg_store["bright"] = None
            _chg_redraw()

        chg_bins.on_value_change(lambda _e: _chg_redraw())
        PLOTS.update(charge_set=charge_set)

    # -------------------------- waveform -----------------------------
    with ui.card().classes("daq-card w-full"):
        with ui.row().classes("w-full items-center gap-2"):
            ui.html("<h2>waveform</h2>")
            ui.element("div").style("flex:1")
            wv_prev = ui.button("◀").props("flat dense")
            wv_idx  = ui.number(value=0, min=0, step=1, format="%d") \
                .classes("w-20 num").props("dense")
            wv_next = ui.button("▶").props("flat dense")
        wv_info = ui.label("no waveforms yet").classes("text-xs text-gray-400")
        wv_opts = _base("time from trigger (µs)", "ADC (baseline-sub)", 46)
        wv_opts["series"].append({
            "name": "wfm", "type": "line", "showSymbol": False, "data": [],
            "lineStyle": {"width": 1.4, "color": _BRIGHT_COLOR}})
        wv_chart = _plotbox(wv_opts)
        wv_state = {"wfs": None, "pre_us": 0.0, "label": ""}

        def _wv_redraw():
            wfs = wv_state["wfs"]
            if wfs is None or len(wfs) == 0:
                wv_chart.options["series"][0]["data"] = []
                wv_chart.update()
                wv_info.text = ("no waveforms — enable 'store raw waveforms' "
                                "on the pulse card")
                return
            n = len(wfs)
            i = max(0, min(int(wv_idx.value or 0), n - 1))
            if i != int(wv_idx.value or 0):
                wv_idx.value = i
            w = _np.asarray(wfs[i], dtype=float)
            n_pre = max(1, int(round(wv_state["pre_us"] * _SAMPLES_PER_US)))
            baseline = (float(w[:n_pre].mean()) if len(w) >= n_pre
                        else float(w.mean()))
            w_bl = w - baseline
            t = _np.arange(len(w)) / _SAMPLES_PER_US - wv_state["pre_us"]
            wv_chart.options["series"][0]["data"] = [
                [float(t[k]), float(w_bl[k])] for k in range(len(w))]
            wv_chart.update()
            lbl = f"{wv_state['label']} · " if wv_state["label"] else ""
            wv_info.text = f"{lbl}waveform {i + 1} / {n}"

        def wave_set(result, ch, label="", pre_us=0.0):
            try:
                wfs = result.waveforms.get(ch)
            except Exception:
                wfs = None
            wv_state.update(wfs=wfs, pre_us=float(pre_us), label=str(label))
            wv_idx.value = 0
            _wv_redraw()

        wv_idx.on_value_change(lambda _e: _wv_redraw())
        wv_prev.on_click(lambda: (
            wv_idx.set_value(max(0, int(wv_idx.value or 0) - 1)), _wv_redraw()))
        wv_next.on_click(lambda: (
            wv_idx.set_value(int(wv_idx.value or 0) + 1), _wv_redraw()))
        PLOTS.update(wave_set=wave_set)

    # IV points stream in from the sweep worker thread; redraw on the UI
    # loop only when something changed (echart can't be touched off-loop).
    def _pump_iv():
        if dirty.get("iv"):
            dirty["iv"] = False
            _iv_redraw()
    ui.timer(0.3, _pump_iv)


def _build_level2_tab():
    """Single-SiPM measurements: IV, pulse counting, and scan.

    Operator provides the SiPM identity directly (no channel-map lookup):
    sipm id (label), MUX channel, and the (x, y) center position in mm.
    All measurements:
      - select that MUX channel
      - move the stage to (center_x, center_y)
      - run the measurement (dark or bright; bright = AWG ch1 pulse)
      - save to data/sipm{N}_T{K}/<unix_ms>.h5

    The scan card sweeps one axis (X or Y) around the center, takes
    N current samples per position, and supports two illumination modes:
    VUV beam (AWG ch1) and Laser (AWG ch2), each with its own pulse
    parameters.
    """
    ui.label(
        "Single-SiPM measurements: IV (dark/bright), pulse counting "
        "(dark/bright), and 1-D scan (X or Y, dark/VUV/laser). "
        "Each click writes data/sipm{N}_T{K}/<unix_ms>.h5."
    ).classes("text-gray-400 text-sm")

    log_lbl = ui.log(max_lines=24).classes("h-64 w-full")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    # ------------------------------------------------------------------
    # Live-plot registry.  The right-hand plot column (built after the
    # control cards, below) fills this with streaming callbacks; the run
    # handlers stream points/traces into the views as data is recorded.
    # Defined here so the handlers can close over `PLOTS` regardless of
    # build order — lookups happen at click-time, after the column is up.
    # `_plot_dirty["iv"]` is set from the IV worker thread and pumped onto
    # the UI by a ui.timer inside the plot column (echart updates must run
    # on the main loop, not the worker thread).
    # ------------------------------------------------------------------
    PLOTS: dict = {}
    _plot_dirty = {"iv": False}

    def _plot(_name, *args):
        """Best-effort live-plot update — a chart glitch must never abort
        a measurement run, so swallow (and log) any failure."""
        fn = PLOTS.get(_name)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            try: log_msg(f"  plot {_name} FAIL: {type(e).__name__}: {e}")
            except Exception: pass

    # ==================================================================
    # Shared helpers used by every measurement card.
    # ==================================================================
    def _awg_pulse_on(ks_ch: int, freq: float, amp: float, offset: float,
                       width_s: float) -> None:
        """Configure + enable a Keysight 33500B channel with a pulse train.

        Called inside a worker thread (no await).  Raises on missing AWG.
        """
        awg = HUB.ks33500b
        if awg is None:
            raise RuntimeError(
                "Keysight 33500B not connected — required for bright / scan "
                "illumination.  Connect it on the Connections tab.")
        awg.set_load("INF", channel=ks_ch)
        awg.apply_pulse(float(freq), float(amp), float(offset), 0.0,
                         channel=ks_ch)
        awg.configure_pulse(period_s=1.0 / max(float(freq), 1e-9),
                             width_s=float(width_s),
                             channel=ks_ch)
        awg.output_on(ks_ch)

    def _awg_off(ks_ch: int) -> None:
        if HUB.ks33500b is None: return
        try: HUB.ks33500b.output_off(ks_ch)
        except Exception: pass

    # Controls on the left, the live-plot column on the right.  no-wrap
    # keeps them side by side on a wide monitor; each side stays
    # scrollable on its own when the window is narrow.
    _split = ui.row().classes("w-full gap-4 items-start no-wrap")
    with _split:
        _left  = ui.column().style("flex:2 1 640px; min-width:0; gap:12px")
        _right = ui.column().style("flex:1 1 440px; min-width:380px; gap:14px")

    with _left, ui.row().classes("w-full gap-3 items-start"):

        # ==============================================================
        # CARD 1 — SiPM identity + position (all but T are optional)
        # ==============================================================
        with ui.card().classes("daq-card"):
            ui.html("<h2>sipm + position (optional)</h2>")
            ui.label(
                "Toggle the switch next to a field to include it.  "
                "Off ⇒ field is omitted from the measurement file AND "
                "the corresponding action is skipped at run-time (no MUX "
                "select, no stage move).  T (K) is always required for "
                "the per-T folder name."
            ).classes("text-xs text-gray-400")

            # ---- SiPM id (optional, label-only — file tag) ----
            with ui.row().classes("gap-2 items-end"):
                sipm_use = ui.switch("SiPM id", value=False) \
                    .props("dense")
                sipm_in = ui.number(value=1, step=1).classes("w-24 num")
                sipm_in.bind_enabled_from(sipm_use, "value")

            # ---- MUX channel (optional — gates whether MUX is touched) ----
            with ui.row().classes("gap-2 items-end"):
                mux_use = ui.switch("MUX ch", value=False) \
                    .props("dense")
                mux_in  = ui.number(value=1, step=1, min=1, max=96) \
                    .classes("w-24 num")
                mux_in.bind_enabled_from(mux_use, "value")

            # ---- Bright-measurement location ----
            # When on, the stage moves the LIGHT SOURCE to (x, y) for any
            # bright measurement (IV bright, pulse bright, scan center).
            # Dark measurements use the dark-location row below instead.
            with ui.row().classes("gap-2 items-end"):
                loc_use = ui.switch("Bright location", value=False) \
                    .props("dense")
                cx_in = ui.number(label="x (mm)", value=0.0,
                                   step=0.1, format="%.3f") \
                    .classes("w-24 num")
                cy_in = ui.number(label="y (mm)", value=0.0,
                                   step=0.1, format="%.3f") \
                    .classes("w-24 num")
                cx_in.bind_enabled_from(loc_use, "value")
                cy_in.bind_enabled_from(loc_use, "value")

            # ---- Dark-measurement location ----
            # When on, the stage moves the LIGHT SOURCE to (dx, dy) for
            # any dark measurement (typically a spot well away from the
            # SiPM so the LED's residual light can't bias the result).
            # When off, the stage doesn't move for dark measurements.
            with ui.row().classes("gap-2 items-end"):
                dark_loc_use = ui.switch("Dark location", value=False) \
                    .props("dense")
                dx_in = ui.number(label="x (mm)", value=0.0,
                                   step=0.1, format="%.3f") \
                    .classes("w-24 num")
                dy_in = ui.number(label="y (mm)", value=0.0,
                                   step=0.1, format="%.3f") \
                    .classes("w-24 num")
                dx_in.bind_enabled_from(dark_loc_use, "value")
                dy_in.bind_enabled_from(dark_loc_use, "value")

            # ---- Temperature (always required) ----
            with ui.row().classes("gap-2 items-end"):
                temp_in = ui.number(label="T (K)", value=298.0, step=0.1,
                                     format="%.2f").classes("w-32 num")
                temp_src = ui.label("manual").classes("text-xs text-gray-500")
                async def read_T_from_sc():
                    if HUB.sc is None:
                        log_msg("slow control not connected — keep manual T")
                        temp_src.text = "manual"; return
                    try:
                        T = await _run_in_thread(P.read_temperature, HUB.sc)
                        temp_in.value = float(T)
                        temp_src.text = "slowcontrol"
                        log_msg(f"T = {T:.3f} K (from slowcontrol)")
                    except Exception as e:
                        log_msg(f"  T read FAIL: {type(e).__name__}: {e}")
                        temp_src.text = "manual"
                ui.button("read T", on_click=read_T_from_sc).props("dense")
                temp_in.on_value_change(lambda _: setattr(temp_src, "text",
                                                           "manual"))

            async def _go_to_sipm():
                """Apply whatever the operator entered: select MUX channel
                (if enabled) + move stage (if enabled).  No measurement.
                Logs a no-op if neither toggle is on."""
                if not (mux_use.value or loc_use.value):
                    log_msg("go-to: neither MUX nor Location enabled — "
                            "nothing to do"); return
                if mux_use.value:
                    if HUB.mux is None:
                        log_msg("MUX enabled but mux not connected"); return
                    try:
                        await _run_in_thread(P.select_channel, HUB.mux,
                                              int(mux_in.value))
                        log_msg(f"  MUX → ch {int(mux_in.value)}")
                    except Exception as e:
                        log_msg(f"  MUX FAIL: {e}"); return
                if loc_use.value:
                    if HUB.stage is None:
                        log_msg("Location enabled but stage not connected")
                        return
                    try:
                        await _run_in_thread(
                            P.move_stage, HUB.stage,
                            float(cx_in.value), float(cy_in.value),
                            bool(HUB.config.stage_deenergize),
                        )
                        log_msg(f"  stage → ({cx_in.value:.3f}, "
                                f"{cy_in.value:.3f}) mm")
                    except Exception as e:
                        log_msg(f"  stage FAIL: {e}")

            ui.button("go to sipm", on_click=_go_to_sipm) \
                .props("dense color=secondary")

        # ---- helpers shared by all measurement cards ----
        def _opt_kwargs() -> dict:
            """Pack the optional identifier fields (None when their switch
            is off).  Pass directly to MSTORE.save_l2_* via **_opt_kwargs().

            Both bright (`center_x_mm`/`center_y_mm`) and dark
            (`dark_x_mm`/`dark_y_mm`) coordinates are included
            whenever their respective switches are on, regardless of
            which one the current measurement actually used — the
            file then records the full position setup for both.
            """
            return {
                "sipm_id":     int(sipm_in.value) if sipm_use.value else None,
                "mux_channel": int(mux_in.value)  if mux_use.value  else None,
                "center_x_mm": float(cx_in.value) if loc_use.value  else None,
                "center_y_mm": float(cy_in.value) if loc_use.value  else None,
                "dark_x_mm":   float(dx_in.value) if dark_loc_use.value else None,
                "dark_y_mm":   float(dy_in.value) if dark_loc_use.value else None,
            }

        async def _prep_position(bright: bool = False) -> bool:
            """Apply the entered identifiers ahead of a measurement.

            MUX:  selected whenever the MUX switch is on (wiring,
                  independent of light state).
            Stage: bright measurement → moves to (cx, cy) if the
                  Bright-location switch is on.
                  dark measurement   → moves to (dx, dy) if the
                  Dark-location switch is on.  Otherwise no move.
                  The stage carries the light source, so:
                    - bright + bright-loc on  → light goes over SiPM
                    - dark   + dark-loc   on  → light parks away
                    - either + corresponding switch off → no move

            Returns True if everything requested succeeded (True also
            when nothing was requested).  Logs + returns False on any
            failure.
            """
            if mux_use.value:
                if HUB.mux is None:
                    log_msg("MUX enabled but mux not connected — abort")
                    return False
                try:
                    await _run_in_thread(P.select_channel, HUB.mux,
                                          int(mux_in.value))
                except Exception as e:
                    log_msg(f"  MUX FAIL: {type(e).__name__}: {e}")
                    return False
            # Choose which coord pair (if any) to move to based on
            # bright vs dark + which Location switch is on.
            if bright and loc_use.value:
                target_x, target_y = float(cx_in.value), float(cy_in.value)
                tag = "bright"
            elif (not bright) and dark_loc_use.value:
                target_x, target_y = float(dx_in.value), float(dy_in.value)
                tag = "dark"
            else:
                return True   # no stage move requested
            if HUB.stage is None:
                log_msg(f"{tag.title()} location enabled but stage not "
                        "connected — abort")
                return False
            try:
                await _run_in_thread(
                    P.move_stage, HUB.stage,
                    target_x, target_y,
                    bool(HUB.config.stage_deenergize),
                )
            except Exception as e:
                log_msg(f"  stage FAIL: {type(e).__name__}: {e}")
                return False
            return True

        # ==============================================================
        # CARD 2 — IV (dark / bright × b2987 / k6485)
        # ==============================================================
        with ui.card().classes("daq-card"):
            ui.html("<h2>iv sweep</h2>")
            iv_illum = ui.toggle({"dark": "dark", "bright": "bright"},
                                  value="dark").props("dense")
            iv_meter = ui.select(
                {"k6485": "K6485 picoammeter",
                 "b2987": "B2987 (built-in ammeter)"},
                value="k6485", label="current meter").classes("w-56")
            with ui.row().classes("gap-2 items-end"):
                iv_start = ui.number(label="start (V)",
                                      value=HUB.config.iv_voltage_start,
                                      step=0.1).classes("w-24 num")
                iv_stop  = ui.number(label="stop (V)",
                                      value=HUB.config.iv_voltage_stop,
                                      step=0.1).classes("w-24 num")
                iv_step  = ui.number(label="step (V)",
                                      value=HUB.config.iv_voltage_step,
                                      step=0.01).classes("w-24 num")
                iv_npt   = ui.number(label="N / V",
                                      value=HUB.config.iv_n_per_point,
                                      step=1).classes("w-20 num")

            async def run_iv():
                if HUB.elec is None: log_msg("b2987 not connected"); return
                meter = str(iv_meter.value or "k6485")
                if meter == "k6485" and HUB.k6485 is None:
                    log_msg("K6485 not connected"); return
                bright = (iv_illum.value == "bright")
                if bright and HUB.ks33500b is None:
                    log_msg("Keysight 33500B not connected — bright needs AWG")
                    return
                if not await _prep_position(bright=bright): return

                import numpy as np
                voltages = np.arange(
                    float(iv_start.value),
                    float(iv_stop.value) + float(iv_step.value) * 0.5,
                    float(iv_step.value),
                ).tolist()
                log_msg(f"IV sipm={int(sipm_in.value)} {iv_illum.value} "
                        f"{meter} {len(voltages)} V, N={int(iv_npt.value)}/V")
                set_activity("L2 IV",
                             f"sipm {int(sipm_in.value)} · "
                             f"{iv_illum.value} · {meter} · {len(voltages)} V")
                iv_slot = "bright" if bright else "dark"
                _plot("iv_begin", iv_slot)   # replace this slot, keep the other
                try:
                    if bright:
                        await _run_in_thread(
                            _awg_pulse_on, 1,
                            HUB.config.led_frequency_hz,
                            HUB.config.led_amplitude_v,
                            HUB.config.led_offset_v,
                            HUB.config.led_pulse_width,
                        )
                    delay = float(getattr(HUB.config, "iv_delay_s", 0.05))
                    # Per-voltage progress feedback.  The cb fires inside
                    # the worker thread, so it goes to BOTH the page log
                    # (NiceGUI queues UI updates from any thread) and
                    # stdout (guaranteed real-time in the systemd journal
                    # via `journalctl --user -fu daq-webapp -f`).  The
                    # journal path is the reliable one — the page log
                    # may batch.
                    n_total = len(voltages)
                    def _iv_prog(i, n, v, mean_i, std_i):
                        line = (f"  [{i}/{n}] v={v:+.3f} V  "
                                f"I={mean_i:+.3e} A ± {std_i:.2e}")
                        try: log_msg(line)
                        except Exception: pass
                        try: print(f"[L2 IV] {line}", flush=True)
                        except Exception: pass
                        # Worker thread: iv_point only buffers + flags;
                        # the plot column's timer redraws on the UI loop.
                        _plot("iv_point", iv_slot, v, mean_i)
                    if meter == "k6485":
                        result = await _run_in_thread(
                            lambda: P.iv_sweep_external_meter(
                                HUB.elec, HUB.k6485, voltages,
                                n_per_voltage=int(iv_npt.value),
                                delay_s=delay,
                                progress_cb=_iv_prog,
                            )
                        )
                    else:
                        # The B2987's on-instrument sweep doesn't support
                        # progress callbacks — it returns one block.
                        # Heartbeat once at the start so the user sees
                        # something.
                        print(f"[L2 IV] b2987 sweep starting "
                              f"({n_total} voltages, "
                              f"{int(iv_npt.value)} pts/V) — "
                              "no per-point progress on this meter",
                              flush=True)
                        log_msg(f"  b2987 sweep starting "
                                f"({n_total} V, no per-pt updates)")
                        result = await _run_in_thread(
                            P.iv_sweep, HUB.elec, voltages,
                            int(iv_npt.value), delay,
                        )
                        # No per-point callback on the b2987 — fill the
                        # trace from the returned block.
                        for _v, _i in zip(result.avg_source_v,
                                          result.avg_current_a):
                            _plot("iv_point", iv_slot, _v, _i)
                        _plot("iv_redraw")
                    log_msg(f"  done: I({result.avg_source_v[-1]:.2f} V) = "
                            f"{result.avg_current_a[-1]:.3e} A")
                    note_bias(v_set=result.avg_source_v[-1],
                              i_meas=result.avg_current_a[-1],
                              output_on=False)
                    try:
                        p = MSTORE.save_l2_iv_sweep(
                            result,
                            temperature_K = float(temp_in.value),
                            illuminated   = bright,
                            meter         = meter,
                            **_opt_kwargs(),
                        )
                        log_msg(f"  saved: {p}")
                    except Exception as e:
                        log_msg(f"  SAVE FAIL: {type(e).__name__}: {e}")
                except Exception as e:
                    log_msg(f"  IV FAIL: {type(e).__name__}: {e}")
                finally:
                    if bright:
                        await _run_in_thread(_awg_off, 1)
                    try: await _run_in_thread(P.bias_off, HUB.elec)
                    except Exception: pass
                    clear_activity()
            ui.button("run iv", on_click=run_iv).props("color=primary")

        # ==============================================================
        # CARD 3 — Pulse counting (dark / bright)
        # ==============================================================
        with ui.card().classes("daq-card"):
            ui.html("<h2>pulse counting</h2>")
            pc_illum = ui.toggle({"dark": "dark", "bright": "bright"},
                                  value="dark").props("dense")
            with ui.row().classes("gap-2 items-end"):
                pc_bias = ui.number(label="bias (V)",
                                     value=HUB.config.pulse_bias_v,
                                     step=0.1).classes("w-24 num")
                pc_ch   = ui.number(label="capture ch",
                                     value=0, step=1, min=0, max=63) \
                    .classes("w-24 num")
                pc_thr  = ui.number(label="self-trig thr (ADC)",
                                     value=50, step=10).classes("w-32 num")
            # Optional aux trigger: enable a SECOND channel + threshold so
            # the digitizer can self-trigger on EITHER channel (any ITLA-OR
            # channel above its per-channel threshold).  Capture channel
            # always gets its own threshold.
            with ui.row().classes("gap-2 items-end"):
                pc_aux_use = ui.switch("trigger on another ch", value=False) \
                    .props("dense")
                pc_aux_ch  = ui.number(label="aux ch",
                                        value=4, step=1, min=0, max=63) \
                    .classes("w-24 num")
                pc_aux_thr = ui.number(label="aux thr (ADC)",
                                        value=50, step=10).classes("w-28 num")
                pc_aux_ch.bind_enabled_from(pc_aux_use, "value")
                pc_aux_thr.bind_enabled_from(pc_aux_use, "value")
            with ui.row().classes("gap-2 items-end"):
                pc_pre  = ui.number(label="pre (µs)",
                                     value=HUB.config.pulse_pre_us,
                                     step=0.5).classes("w-24 num")
                pc_post = ui.number(label="post (µs)",
                                     value=HUB.config.pulse_post_us,
                                     step=0.5).classes("w-24 num")
                pc_n    = ui.number(label="N waveforms",
                                     value=HUB.config.pulse_n_waveforms,
                                     step=100).classes("w-28 num")
            pc_store = ui.switch("store raw waveforms", value=True)

            async def run_pulse():
                if HUB.elec is None or HUB.dig is None:
                    log_msg("b2987 or digitizer not connected"); return
                ctrl = getattr(HUB.dig, "_ctrl", None)
                if ctrl is None:
                    log_msg("digitizer backend has no controller"); return
                bright = (pc_illum.value == "bright")
                if bright and HUB.ks33500b is None:
                    log_msg("Keysight 33500B not connected — bright needs AWG")
                    return
                if not await _prep_position(bright=bright): return

                ch  = max(0, min(63, int(pc_ch.value)))
                thr = int(pc_thr.value)
                n   = int(pc_n.value)
                store = bool(pc_store.value)
                bias = float(pc_bias.value)
                # Resolve the trigger set.  The aux channel (if enabled)
                # is added so the VX2740 fires on ANY of the listed
                # channels crossing its per-channel threshold; both
                # channels are read out so the analyst can correlate.
                aux_ch  = int(pc_aux_ch.value)  if pc_aux_use.value else None
                aux_thr = int(pc_aux_thr.value) if pc_aux_use.value else None
                sipm_chs = [ch]
                thresholds = {ch: thr}
                if aux_ch is not None and aux_ch != ch:
                    sipm_chs.append(aux_ch)
                    thresholds[aux_ch] = aux_thr

                sipm_label = (int(sipm_in.value) if sipm_use.value
                              else "anon")
                aux_desc = (f" + trig ch{aux_ch}@{aux_thr}"
                            if aux_ch is not None else "")
                log_msg(f"PULSE sipm={sipm_label} {pc_illum.value} "
                        f"bias={bias:.2f} ch{ch} thr={thr}{aux_desc} "
                        f"N={n} store={store}")
                set_activity("L2 pulse",
                             f"sipm {sipm_label} · {pc_illum.value} "
                             f"· ch{ch}{aux_desc} · N={n}")

                def _run_acq():
                    if bright:
                        _awg_pulse_on(1,
                                       HUB.config.led_frequency_hz,
                                       HUB.config.led_amplitude_v,
                                       HUB.config.led_offset_v,
                                       HUB.config.led_pulse_width)
                    try:
                        P.set_bias(HUB.elec, bias, settle_s=0.3)
                        ctrl.configure_record_window(pre_us=float(pc_pre.value),
                                                     post_us=float(pc_post.value))
                        ctrl.configure_channels(
                            sipm_channels=sipm_chs,
                            thresholds=thresholds,
                            threshold_mode="per_channel",
                            include_pmt=False,
                        )
                        ctrl.configure_trigger(mode="self")
                        return ctrl.run(n_waveforms=n, batch_size=min(1000, n),
                                         store_waveforms=store, timeout_s=60.0)
                    finally:
                        if bright: _awg_off(1)
                        try: P.bias_off(HUB.elec)
                        except Exception: pass
                try:
                    note_bias(v_set=bias, output_on=True)
                    result = await _run_in_thread(_run_acq)
                    log_msg(f"  done: n_waveforms={result.n_waveforms} "
                            f"channels={result.channel_ids}")
                    # Charge spectrum overlays dark/bright; the waveform
                    # viewer scrolls the capture channel's stored frames.
                    chg_slot = "bright" if bright else "dark"
                    _plot("charge_set", chg_slot, result, ch)
                    _plot("wave_set", result, ch, chg_slot,
                          float(pc_pre.value))
                    try:
                        p = MSTORE.save_l2_pulse_run(
                            result,
                            temperature_K       = float(temp_in.value),
                            illuminated         = bright,
                            bias_v              = bias,
                            capture_ch          = ch,
                            capture_thr_adc     = thr,
                            aux_trigger_ch      = aux_ch,
                            aux_trigger_thr_adc = aux_thr,
                            **_opt_kwargs(),
                        )
                        log_msg(f"  saved: {p}")
                    except Exception as e:
                        log_msg(f"  SAVE FAIL: {type(e).__name__}: {e}")
                except Exception as e:
                    log_msg(f"  PULSE FAIL: {type(e).__name__}: {e}")
                finally:
                    note_bias(v_set=0.0, output_on=False)
                    clear_activity()
            ui.button("run pulse", on_click=run_pulse).props("color=primary")

        # ==============================================================
        # CARD 4 — Scan (X or Y, with VUV / Laser illumination on ks33500b)
        # ==============================================================
        with ui.card().classes("daq-card"):
            ui.html("<h2>scan</h2>")
            ui.label(
                "Hold SiPM at fixed bias and sweep one stage axis around "
                "the center; at each point: move → de-energize → record "
                "→ re-energize → next.  Bias source: B2987.  AWG (33500B): "
                "ch1 for VUV beam, ch2 for Laser."
            ).classes("text-xs text-gray-400")
            with ui.row().classes("gap-2 items-end"):
                scan_axis = ui.toggle({"x": "X", "y": "Y"},
                                       value="x").props("dense")
                scan_meter = ui.select(
                    {"k6485": "K6485", "b2987": "B2987"},
                    value="k6485", label="meter").classes("w-32")
                scan_light = ui.toggle(
                    {"vuv":   "VUV beam (ch1)",
                     "laser": "Laser (ch2)"},
                    value="vuv").props("dense")
            with ui.row().classes("gap-2 items-end"):
                scan_bias  = ui.number(label="bias (V)",
                                        value=HUB.config.pulse_bias_v,
                                        step=0.1).classes("w-24 num")
                scan_start = ui.number(label="start (mm)", value=-7.5,
                                        step=0.1, format="%.3f") \
                    .classes("w-24 num")
                scan_stop  = ui.number(label="stop (mm)", value=7.5,
                                        step=0.1, format="%.3f") \
                    .classes("w-24 num")
                scan_step  = ui.number(label="step (mm)", value=0.5,
                                        step=0.01, format="%.3f") \
                    .classes("w-24 num")
                scan_npt   = ui.number(label="N / pt", value=5,
                                        step=1).classes("w-20 num")
                scan_settle = ui.number(label="settle (s)", value=0.1,
                                         step=0.01, format="%.2f") \
                    .classes("w-24 num")
            # If Location is enabled on the SiPM card, this button fills
            # start/stop with center ± 7.5 mm (= ±0.75 cm) along the
            # currently-selected axis.  Per the user's spec: "If I enter
            # a location, then scan x and scan y should be centered on
            # that location, running from -0.75 cm to 0.75."
            def _fill_range_from_center():
                if not loc_use.value:
                    log_msg("scan range: Location switch is off — enable it "
                            "(or just edit start/stop directly)")
                    return
                center = (float(cx_in.value)
                          if str(scan_axis.value) == "x"
                          else float(cy_in.value))
                scan_start.value = center - 7.5
                scan_stop.value  = center + 7.5
                log_msg(f"scan range set to {scan_start.value:+.3f} "
                        f"→ {scan_stop.value:+.3f} mm "
                        f"({str(scan_axis.value).upper()}-axis)")
            ui.button("use ±0.75 cm from center",
                       on_click=_fill_range_from_center) \
                .props("dense flat")
            # AWG params for whichever light mode is active.  Defaults to
            # the LED config; user can override per scan.
            with ui.row().classes("gap-2 items-end"):
                scan_freq  = ui.number(label="freq (Hz)",
                                        value=HUB.config.led_frequency_hz,
                                        step=10.0,
                                        format="%.1f").classes("w-28 num")
                scan_amp   = ui.number(label="amp (Vpp)",
                                        value=HUB.config.led_amplitude_v,
                                        step=0.1).classes("w-24 num")
                scan_offs  = ui.number(label="offset (V)",
                                        value=HUB.config.led_offset_v,
                                        step=0.1).classes("w-24 num")
                scan_width = ui.number(label="width (s)",
                                        value=HUB.config.led_pulse_width,
                                        step=1e-7,
                                        format="%.9f").classes("w-32 num")
            scan_status = ui.label("idle").classes("text-xs text-gray-400")

            async def run_scan():
                # Stage is mandatory (we move every point).  MUX is only
                # required if the operator turned it on in the SiPM card.
                if HUB.elec is None or HUB.stage is None:
                    log_msg("b2987 or stage not connected"); return
                if mux_use.value and HUB.mux is None:
                    log_msg("MUX enabled but mux not connected"); return
                meter = str(scan_meter.value or "k6485")
                if meter == "k6485" and HUB.k6485 is None:
                    log_msg("K6485 not connected"); return
                if HUB.ks33500b is None:
                    log_msg("Keysight 33500B not connected — scan needs AWG")
                    return
                # Light → AWG channel
                ks_ch = 1 if str(scan_light.value) == "vuv" else 2

                # MUX select only if enabled.  Scan does its own stage
                # moves so we skip the stage move here even when Location
                # is enabled — the per-point loop below will visit
                # (center_other, position_along_axis) starting from the
                # first scan point.
                if mux_use.value:
                    try:
                        await _run_in_thread(P.select_channel, HUB.mux,
                                              int(mux_in.value))
                    except Exception as e:
                        log_msg(f"  MUX FAIL: {type(e).__name__}: {e}"); return

                import numpy as np
                positions = np.arange(
                    float(scan_start.value),
                    float(scan_stop.value) + float(scan_step.value) * 0.5,
                    float(scan_step.value),
                ).astype(float)
                axis = str(scan_axis.value)
                # Resolve the "other axis" position.  If Location is on,
                # use the operator's center.  If off, default to 0 mm —
                # the scan still runs along the selected axis, just at
                # (0, pos_y) or (pos_x, 0).
                cx = float(cx_in.value) if loc_use.value else 0.0
                cy = float(cy_in.value) if loc_use.value else 0.0
                bias = float(scan_bias.value)
                npt  = int(scan_npt.value or 1)
                stl  = float(scan_settle.value or 0.0)
                light_mode = "vuv_beam" if str(scan_light.value) == "vuv" else "laser"
                sipm_label = (int(sipm_in.value) if sipm_use.value
                              else "anon")
                log_msg(f"SCAN {axis} sipm={sipm_label} "
                        f"meter={meter} light={light_mode} "
                        f"bias={bias:.2f} {len(positions)} pts × N={npt}")
                set_activity("L2 scan",
                             f"sipm {sipm_label} · {axis}-axis · "
                             f"{len(positions)} pts · {light_mode}")

                means: list[float] = []
                stds:  list[float] = []
                raws:  list[float] = []
                _plot("scan_begin", axis)   # replace this axis, keep the other
                try:
                    await _run_in_thread(
                        _awg_pulse_on, ks_ch,
                        float(scan_freq.value),
                        float(scan_amp.value),
                        float(scan_offs.value),
                        float(scan_width.value),
                    )
                    await _run_in_thread(P.set_bias, HUB.elec, bias,
                                          0.3)
                    note_bias(v_set=bias, output_on=True)

                    for i, pos in enumerate(positions):
                        if axis == "x":
                            x_target, y_target = float(pos), cy
                        else:
                            x_target, y_target = cx, float(pos)
                        scan_status.text = (f"pt {i+1}/{len(positions)}: "
                                            f"({x_target:.3f}, "
                                            f"{y_target:.3f}) mm")
                        # move → de-energize after; re-energizes
                        # automatically on the next move_to()
                        await _run_in_thread(
                            P.move_stage, HUB.stage,
                            x_target, y_target,
                            True,  # deenergize_after
                        )
                        if stl > 0:
                            await asyncio.sleep(stl)
                        # Sample N readings
                        def _take(n_=npt, m_=meter):
                            if m_ == "k6485":
                                arr, _ts = HUB.k6485.read_n(n_, 0.0)
                                return np.asarray(arr, dtype=np.float64)
                            else:
                                return np.array([HUB.elec.measure_current()
                                                  for _ in range(n_)],
                                                 dtype=np.float64)
                        arr = await _run_in_thread(_take)
                        means.append(float(np.mean(arr)))
                        stds.append(float(np.std(arr, ddof=1))
                                     if len(arr) > 1 else 0.0)
                        raws.extend(arr.tolist())
                        _plot("scan_point", axis, float(pos), means[-1])
                        log_msg(f"    {axis}={pos:+.3f} mm  "
                                f"I={means[-1]:+.3e} A ± {stds[-1]:.2e}")
                    scan_status.text = (f"done — {len(positions)} pts; "
                                        f"min |I|={min(abs(m) for m in means):.2e}, "
                                        f"max |I|={max(abs(m) for m in means):.2e}")
                    log_msg("  scan done")
                    try:
                        p = MSTORE.save_l2_scan(
                            positions_mm  = positions,
                            mean_current_a= np.array(means, dtype=np.float64),
                            std_current_a = np.array(stds,  dtype=np.float64),
                            raw_current_a = np.array(raws,  dtype=np.float64),
                            temperature_K = float(temp_in.value),
                            axis          = axis,
                            bias_v        = bias,
                            meter         = meter,
                            light_mode    = light_mode,
                            light_freq_hz = float(scan_freq.value),
                            light_amp_v   = float(scan_amp.value),
                            light_width_s = float(scan_width.value),
                            n_per_point   = npt,
                            settle_s      = stl,
                            **_opt_kwargs(),
                        )
                        log_msg(f"  saved: {p}")
                    except Exception as e:
                        log_msg(f"  SAVE FAIL: {type(e).__name__}: {e}")
                except Exception as e:
                    log_msg(f"  SCAN FAIL: {type(e).__name__}: {e}")
                    scan_status.text = f"FAIL: {type(e).__name__}: {e}"
                finally:
                    await _run_in_thread(_awg_off, ks_ch)
                    try: await _run_in_thread(P.bias_off, HUB.elec)
                    except Exception: pass
                    note_bias(v_set=0.0, output_on=False)
                    clear_activity()

            ui.button("run scan", on_click=run_scan).props("color=primary")

    # ==================================================================
    # RIGHT-HAND LIVE-PLOT COLUMN
    # Built after the control cards so the run handlers above can stream
    # into it via PLOTS[...].  Registers iv/scan/charge/wave callbacks.
    # ==================================================================
    with _right:
        _build_l2_plots(PLOTS, _plot_dirty)


# ===========================================================================
# Electrometer — embedded b2987 manual-control panel
# ===========================================================================

def _embedded_instr_tab(message: str, getter, build_fn,
                         instrument_key: str | None = None):
    """Shared scaffolding for the instrument tabs.

    Shows `message` plus a 'connect' button only when the instrument is
    disconnected — both vanish as soon as the controller is non-None.
    Wraps the embedded GUI in a `.instr-embed` container so CSS can
    compact the per-instrument log widget.
    """
    with ui.row().classes("items-center gap-2 w-full"):
        intro = ui.label(message).classes("text-gray-400 text-sm")
        connect_btn = ui.button("connect").props("color=primary dense")
        if instrument_key is not None:
            async def _do_connect(_k=instrument_key):
                ok = await _quick_connect(_k)
                if ok:
                    ui.notify(f"{_k}: connected", type="positive",
                              position="top", timeout=2500)
                else:
                    ui.notify(f"{_k}: connect failed (see status)",
                              type="negative", position="top", timeout=4000)
            connect_btn.on_click(_do_connect)
        else:
            connect_btn.set_visibility(False)

    def _refresh_intro():
        connected = getter() is not None
        intro.set_visibility(not connected)
        if instrument_key is not None:
            connect_btn.set_visibility(not connected)

    _refresh_intro()
    ui.timer(2.0, _refresh_intro)
    with ui.element("div").classes("instr-embed w-full"):
        build_fn(get_controller=getter, show_connection=False)


def _build_electrometer_tab():
    """Keysight B2987B electrometer panel — Quick-I/V style.

    Layout:
        [statusbar  dot · model · VISA addr · right state]
        [TOP grid]
          [LEFT  IV sweep card: head row + plot + 4-col sweep fields]
          [RIGHT  readout card  +  output card stacked]
        [3-col blocks: SOURCE | MEASURE | TRIGGER/TIMING]
        [footer]

    All inputs use label-above + unit-suffix-inside (no floating labels).
    Per-block Apply lights up accent-blue when the block is dirty.
    Output is the safety control: red when ON, neutral when OFF.
    """
    import math
    import numpy as _np

    # ---- SCPI sentinel + measurement formatter -----------------------
    _SENTINEL = 1e30

    def _is_sentinel(v):
        return isinstance(v, float) and (v != v or abs(v) > _SENTINEL)

    def _fmt_i(value):
        """Format current for the big mono readout. Returns inner HTML."""
        if _is_sentinel(value):
            return '<span class="i-val no-data">no data</span>'
        if value is None:
            return '<span class="i-val">—</span>'
        return f'<span class="i-val">{value:.3e}</span>'

    def _fmt_v(value):
        if _is_sentinel(value):
            return '<span class="v-val no-data">no data</span>'
        if value is None:
            return '<span class="v-val">—</span>'
        av = abs(value)
        txt = f"{value:.3e}" if (av != 0 and (av < 1e-3 or av >= 1e5)) else f"{value:.4f}"
        return f'<span class="v-val">{txt}</span>'

    def _hint_for_no_data() -> str:
        if HUB.elec is None:
            return "not connected"
        drv = HUB.elec._driver
        if not drv._output_on:
            return ("instrument returned 'no data' (9.91e+37) — "
                    "source output is OFF; turn it on in the OUTPUT card")
        return ("instrument returned 'no data' (9.91e+37) — "
                "check source range / aperture / current limit")

    # ---- Dirty-tracking state ---------------------------------------
    _dirty_specs: list = []
    _blocks: dict = {}   # block-name -> ui.element of block card

    def _is_close(a, b, *, rel=1e-6, abs_=1e-12) -> bool:
        if a is None or b is None:
            return a is b
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b)
        try:
            return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)
        except (TypeError, ValueError):
            return a == b

    def _track(block, fld_wrap, get_staged, get_applied, *, eq=None):
        _dirty_specs.append((block, fld_wrap, get_staged, get_applied, eq or _is_close))

    # ---- Field helpers (label-above, mono input, unit suffix) -------
    def _field_number(label_text, value, unit, *, step=1.0, fmt="%.3f"):
        with ui.element("div").classes("fld") as wrap:
            ui.html(f'<label class="fld-lbl">{label_text}</label>')
            n = ui.number(value=value, step=step, format=fmt) \
                .props(f'dense filled hide-bottom-space borderless="false" suffix="{unit}"')
            applied = ui.html('<span class="applied-note"></span>')
        return wrap, n, applied

    def _field_select(label_text, options, value, *, unit=""):
        with ui.element("div").classes("fld") as wrap:
            ui.html(f'<label class="fld-lbl">{label_text}</label>')
            s = ui.select(options, value=value) \
                .props('dense filled hide-bottom-space'
                       + (f' suffix="{unit}"' if unit else ''))
            applied = ui.html('<span class="applied-note"></span>')
        return wrap, s, applied

    def _field_toggle(label_text, value):
        with ui.element("div").classes("tgl-row") as wrap:
            ui.html(f'<span class="tgl-lbl">{label_text}</span>')
            sw = ui.switch(value=value).props("dense")
        return wrap, sw

    # ====================================================================
    # PANEL
    # ====================================================================
    with ui.element("div").classes("elec-panel w-full"):

        # -------- STATUSBAR ---------------------------------------------
        with ui.row().classes("statusbar w-full") as statusbar:
            ui.html('<span class="dot"></span>')
            model_html = ui.html('<span class="model">not connected</span>')
            addr_html  = ui.html('<span class="addr"></span>')
            right_status = ui.html('<span class="right-status">disconnected</span>')
            # quick connect — visible when not connected
            async def _do_connect():
                ok = await _quick_connect("elec")
                ui.notify("electrometer: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense flat") \
                .style("margin-left:.5rem")

        # -------- TOP REGION (sweep+plot | readout+output) --------------
        with ui.element("div").classes("ep-top w-full"):

            # ===== IV SWEEP card (left) =====
            with ui.card().classes("ep-card sweep-card") as sweep_block:
                _blocks["sweep"] = sweep_block
                with ui.row().classes("plot-head"):
                    ui.html('<p class="eyebrow">IV sweep</p>')
                    ui.html('<span class="spacer"></span>')
                    derived_html = ui.html('<span class="derived">— pts</span>')
                    sweep_status_html = ui.html('<span class="statuspill">idle</span>')
                    save_h5_sw = ui.switch("save .h5", value=True).props("dense")
                    run_btn = ui.button("▶ Run sweep").props("color=primary dense")

                # ECharts plot (320px box). Series 0 = staged voltages at y=0
                # (gray dots, updates live as user types). Series 1 = result
                # I(V) line + markers, populated after a sweep finishes.
                chart_opts = {
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 60, "right": 18, "top": 14, "bottom": 38},
                    "backgroundColor": "transparent",
                    "textStyle": {"color": "#dde3ee"},
                    "xAxis": {
                        "type": "value", "name": "V_source (V)",
                        "nameLocation": "middle", "nameGap": 22, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "yAxis": {
                        "type": "value", "name": "I (A)",
                        "nameLocation": "middle", "nameGap": 46, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10,
                                      "formatter": "{value}"},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "series": [
                        {"name":"staged","type":"scatter","symbol":"circle","symbolSize":3,
                         "data":[],"itemStyle":{"color":"#5c6775","opacity":0.6},"z":1},
                        {"name":"I vs V","type":"line","showSymbol":True,"symbolSize":4,
                         "data":[],"lineStyle":{"width":2,"color":"#3b82f6"},
                         "itemStyle":{"color":"#3b82f6"},"z":2},
                    ],
                }
                iv_chart = ui.echart(chart_opts).classes("plotbox")

                with ui.element("div").classes("sweep-fields"):
                    sv_start_wrap, sv_start_n, _ = _field_number(
                        "Start", HUB.config.iv_voltage_start, "V",
                        step=0.1, fmt="%.2f")
                    sv_stop_wrap,  sv_stop_n,  _ = _field_number(
                        "Stop",  HUB.config.iv_voltage_stop,  "V",
                        step=0.1, fmt="%.2f")
                    sv_step_wrap,  sv_step_n,  _ = _field_number(
                        "Step",  HUB.config.iv_voltage_step,  "V",
                        step=0.01, fmt="%.3f")
                    sv_avg_wrap,   sv_avg_n,   _ = _field_number(
                        "Avg / pt", HUB.config.iv_n_per_point, "samples",
                        step=1, fmt="%d")

            # ===== READOUT + OUTPUT stack (right) =====
            with ui.element("div").classes("readout-stack"):

                # ----- READOUT card -----
                with ui.card().classes("ep-card readout"):
                    ui.html('<p class="eyebrow">Live readout</p>')
                    with ui.element("div").classes("i-line"):
                        i_hero = ui.html(_fmt_i(None))
                        ui.html('<span class="i-unit">A</span>')
                    with ui.element("div").classes("v-line"):
                        v_hero = ui.html(_fmt_v(None))
                        ui.html('<span class="v-unit">V</span>')

                    async def do_read_i():
                        if HUB.elec is None:
                            ui.notify("electrometer not connected",
                                      type="warning", position="top", timeout=2500)
                            return
                        try:
                            i = await _run_in_thread(HUB.elec.measure_current)
                            if _is_sentinel(i):
                                hint = _hint_for_no_data()
                                i_hero.set_content(_fmt_i(i))
                                log_msg(f"I read: {hint}")
                                ui.notify(hint, type="warning",
                                          position="top", timeout=4500)
                                return
                            note_bias(i_meas=i)
                            i_hero.set_content(_fmt_i(i))
                            log_msg(f"I = {i:.3e} A")
                        except Exception as e:
                            log_msg(f"read I FAIL: {type(e).__name__}: {e}")
                            ui.notify(f"read I FAIL: {type(e).__name__}",
                                      type="negative", position="top", timeout=4000)

                    async def do_read_v():
                        if HUB.elec is None:
                            ui.notify("electrometer not connected",
                                      type="warning", position="top", timeout=2500)
                            return
                        try:
                            v = await _run_in_thread(HUB.elec._driver.measure_voltage)
                            if _is_sentinel(v):
                                hint = _hint_for_no_data()
                                v_hero.set_content(_fmt_v(v))
                                log_msg(f"V read: {hint}")
                                ui.notify(hint, type="warning",
                                          position="top", timeout=4500)
                                return
                            v_hero.set_content(_fmt_v(v))
                            log_msg(f"V = {v:.4f} V")
                        except Exception as e:
                            log_msg(f"read V FAIL: {type(e).__name__}: {e}")
                            ui.notify(f"read V FAIL: {type(e).__name__}",
                                      type="negative", position="top", timeout=4000)

                    with ui.element("div").classes("read-btns"):
                        ui.button("⌁ read I", on_click=do_read_i).props("flat")
                        ui.button("∿ read V", on_click=do_read_v).props("flat")

                # ----- OUTPUT card -----
                with ui.card().classes("ep-card output-card") as output_card:
                    ui.html('<p class="eyebrow">Source output</p>')
                    output_btn = ui.html(
                        '<button class="output-btn" data-on="false">'
                        '<span class="ic">■</span><span>output off</span>'
                        '</button>'
                    )
                    readback_html = ui.html(
                        '<div class="readback">source level '
                        '<span class="v">— V</span></div>'
                    )

                    async def toggle_output():
                        if HUB.elec is None:
                            ui.notify("not connected", type="warning",
                                      position="top", timeout=2000); return
                        drv = HUB.elec._driver
                        try:
                            if drv._output_on:
                                await _run_in_thread(HUB.elec.bias_off)
                                note_bias(output_on=False)
                                log_msg("output OFF")
                            else:
                                v = float(src_level_n.value)
                                await _run_in_thread(HUB.elec.set_bias, v, 0.1)
                                note_bias(v_set=v, output_on=True)
                                log_msg(f"output ON @ {v:.3f} V")
                        except Exception as e:
                            log_msg(f"output toggle FAIL: {type(e).__name__}: {e}")
                    output_btn.on("click", lambda _e=None: toggle_output())

        # -------- 3-COL BLOCKS GRID -------------------------------------
        with ui.element("div").classes("blocks w-full"):

            # ===== SOURCE / BIAS =====
            with ui.card().classes("ep-card block") as source_block:
                _blocks["source"] = source_block
                with ui.row().classes("block-head"):
                    ui.html('<p class="eyebrow">Source / bias</p>')
                    apply_source_btn = ui.button("apply").props("flat dense") \
                        .classes("apply-btn")
                with ui.element("div").classes("fields"):
                    src_fn_wrap, src_fn_sel, _ = _field_select(
                        "Function", {"VOLT": "Voltage"}, "VOLT")
                    src_level_wrap, src_level_n, src_level_applied = _field_number(
                        "Level", 0.0, "V", step=0.5, fmt="%.3f")
                    src_range_wrap, src_range_sel, src_range_applied = _field_select(
                        "Source range", {20:"20", 1000:"1000"}, 1000, unit="V")
                    src_climit_wrap, src_climit_n, src_climit_applied = _field_number(
                        "Current limit (compliance)", 1e-3, "A",
                        step=1e-9, fmt="%.2e")
                    src_ilim_wrap, src_ilim_sw = _field_toggle(
                        "Current-limiting resistor", False)

            # ===== MEASURE =====
            with ui.card().classes("ep-card block") as measure_block:
                _blocks["measure"] = measure_block
                with ui.row().classes("block-head"):
                    ui.html('<p class="eyebrow">Measure</p>')
                    apply_measure_btn = ui.button("apply").props("flat dense") \
                        .classes("apply-btn")
                with ui.element("div").classes("fields"):
                    mfn_wrap, mfn_sel, _ = _field_select(
                        "Function", {"CURR":"Current","VOLT":"Voltage"}, "CURR")
                    mrng_wrap, mrng_n, mrng_applied = _field_number(
                        "Range", 2e-6, "A", step=1e-9, fmt="%.2e")
                    nplc_wrap, nplc_n, nplc_applied = _field_number(
                        "Aperture (NPLC)", 1.0, "PLC", step=0.1, fmt="%.2f")
                    auto_wrap, auto_sw = _field_toggle("Auto-range", True)
                    zref_wrap, zref_sw = _field_toggle("Zero-correct", True)
                    mv_wrap, mv_sw = _field_toggle("Also measure voltage", False)

            # ===== TRIGGER / TIMING =====
            with ui.card().classes("ep-card block") as timing_block:
                _blocks["timing"] = timing_block
                with ui.row().classes("block-head"):
                    ui.html('<p class="eyebrow">Trigger / timing</p>')
                    apply_timing_btn = ui.button("apply").props("flat dense") \
                        .classes("apply-btn")
                with ui.element("div").classes("fields"):
                    trg_src_wrap, trg_src_sel, _ = _field_select(
                        "Trigger source",
                        {"AINT":"AUTO", "TIMER":"TIMER", "BUS":"BUS", "EXT":"EXT"},
                        "AINT")
                    trg_delay_wrap, trg_delay_n, trg_delay_applied = _field_number(
                        "Trigger delay", 0.1, "s", step=0.01, fmt="%.3f")
                    trg_timer_wrap, trg_timer_n, _ = _field_number(
                        "Timer interval (TIMER only)", 0.01, "s",
                        step=0.01, fmt="%.4f")

        # -------- FOOTER ------------------------------------------------
        with ui.element("div").classes("efooter w-full"):
            ui.html("nEXO SiPM tile characterization DAQ — Brunner neutrino lab, McGill")
            ui.html('<span class="spacer"></span>')
            ui.html('all tabs ported · legacy PyQt GUI: <code>python -m daq.app</code>')

    # ---- log widget (small, below the panel) -------------------------
    log = ui.log(max_lines=80).classes("h-12 w-full")
    def log_msg(s: str):
        log.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    # ====================================================================
    # WIRING
    # ====================================================================

    # ---- Block dirty tracking + Apply highlight ----------------------
    _track("source", src_level_wrap,
           lambda: float(src_level_n.value or 0),
           lambda: (HUB.elec and HUB.elec._driver._source_voltage))
    _track("source", src_range_wrap,
           lambda: int(src_range_sel.value or 0),
           lambda: (HUB.elec and HUB.elec._source_range))
    _track("source", src_climit_wrap,
           lambda: float(src_climit_n.value or 0),
           lambda: 1.0)  # compliance not exposed by the controller
    _track("source", src_ilim_wrap,
           lambda: bool(src_ilim_sw.value),
           lambda: bool(HUB.elec and HUB.elec._current_limit))
    _track("measure", mrng_wrap,
           lambda: float(mrng_n.value or 0),
           lambda: getattr(HUB.elec, "_current_range_v", None) if HUB.elec else None)
    _track("measure", nplc_wrap,
           lambda: float(nplc_n.value or 0),
           lambda: getattr(HUB.elec, "_current_aperture", None) * 60
                   if (HUB.elec and getattr(HUB.elec, "_current_aperture", None)) else None)
    _track("measure", auto_wrap,
           lambda: bool(auto_sw.value),
           lambda: bool(getattr(HUB.elec, "_current_range_auto", True)) if HUB.elec else None)
    _track("measure", zref_wrap,
           lambda: bool(zref_sw.value),
           lambda: bool(getattr(HUB.elec, "_zero_reference", True)) if HUB.elec else None)
    _track("measure", mv_wrap,
           lambda: bool(mv_sw.value),
           lambda: bool(getattr(HUB.elec, "_measure_voltage", False)) if HUB.elec else None)
    _track("timing", trg_delay_wrap,
           lambda: float(trg_delay_n.value or 0),
           lambda: getattr(HUB.elec, "_delay_s", None) if HUB.elec else None)

    # ---- Apply handlers --------------------------------------------------
    async def apply_source():
        if HUB.elec is None: log_msg("not connected"); return
        try:
            drv = HUB.elec._driver
            await _run_in_thread(
                HUB.elec.configure_sweep,
                source_range=int(src_range_sel.value),
                current_limit=bool(src_ilim_sw.value),
            )
            await _run_in_thread(drv.set_voltage, float(src_level_n.value))
            log_msg(f"source applied · level={float(src_level_n.value):.3f} V "
                    f"· range={src_range_sel.value} V "
                    f"· ilim={'on' if src_ilim_sw.value else 'off'}")
        except Exception as e:
            log_msg(f"source apply FAIL: {type(e).__name__}: {e}")

    async def apply_measure():
        if HUB.elec is None: log_msg("not connected"); return
        try:
            # NPLC × (1/60) ≈ aperture seconds (60 Hz line)
            aper_s = float(nplc_n.value) / 60.0
            await _run_in_thread(
                HUB.elec.configure_sweep,
                current_range_auto=bool(auto_sw.value),
                current_range_v=float(mrng_n.value),
                current_aperture_mode="FIXED",
                current_aperture_s=aper_s,
                zero_reference=bool(zref_sw.value),
                measure_voltage=bool(mv_sw.value),
            )
            log_msg(f"measure applied · auto={auto_sw.value} "
                    f"· range={float(mrng_n.value):.1e} A "
                    f"· NPLC={float(nplc_n.value):.2f} "
                    f"· zero-correct={zref_sw.value} · meas-v={mv_sw.value}")
        except Exception as e:
            log_msg(f"measure apply FAIL: {type(e).__name__}: {e}")

    async def apply_timing():
        if HUB.elec is None: log_msg("not connected"); return
        try:
            await _run_in_thread(
                HUB.elec.configure_sweep,
                delay_s=float(trg_delay_n.value),
            )
            log_msg(f"timing applied · delay={float(trg_delay_n.value):.3f} s "
                    f"· trigger={trg_src_sel.value}")
        except Exception as e:
            log_msg(f"timing apply FAIL: {type(e).__name__}: {e}")

    apply_source_btn.on_click(apply_source)
    apply_measure_btn.on_click(apply_measure)
    apply_timing_btn.on_click(apply_timing)

    # ---- Sweep + plot ------------------------------------------------
    def _planned_voltages() -> list:
        try:
            a = float(sv_start_n.value); b = float(sv_stop_n.value)
            s = float(sv_step_n.value)
            if s == 0: return []
            sign = 1.0 if b >= a else -1.0
            return list(_np.arange(a, b + sign * abs(s) * 0.5, sign * abs(s)))
        except (ValueError, TypeError):
            return []

    def _update_preview():
        vs = _planned_voltages()
        try: avg = int(sv_avg_n.value)
        except (ValueError, TypeError): avg = 0
        derived_html.set_content(
            f'<span class="derived">{len(vs)} pts × {avg} avg = '
            f'{len(vs) * avg} reads</span>'
        )
        iv_chart.options["series"][0]["data"] = [[float(v), 0.0] for v in vs]
        iv_chart.update()

    for fld in (sv_start_n, sv_stop_n, sv_step_n, sv_avg_n):
        fld.on_value_change(lambda *_: _update_preview())
    _update_preview()

    def _save_sweep_h5(result) -> str:
        import h5py
        out_dir = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "data",
        ))
        path = h5io.elec_sweep_filename(out_dir)
        with h5py.File(path, "w") as f:
            h5io.write_top_attrs(f, measurement_type="elec_sweep")
            g = f.create_group("/elec_sweep")
            h5io.write_sweep_result(g, result, attrs={
                "source_range_v":  HUB.elec._source_range,
                "current_limit":   HUB.elec._current_limit,
                "aperture_mode":   getattr(HUB.elec, "_current_aperture_mode", "AUTO"),
                "delay_s":         getattr(HUB.elec, "_delay_s", 0.0),
                "measure_voltage": getattr(HUB.elec, "_measure_voltage", False),
            })
        return path

    async def do_run_sweep():
        if HUB.elec is None: log_msg("not connected"); return
        vs = _planned_voltages()
        if not vs:
            log_msg("empty sweep range"); return
        try:    avg = max(1, int(sv_avg_n.value))
        except (ValueError, TypeError): avg = 1
        log_msg(f"sweep {len(vs)} pts {vs[0]:.2f} → {vs[-1]:.2f} V (n={avg})")
        set_activity("electrometer IV sweep",
                     f"{len(vs)} pts {vs[0]:.2f}..{vs[-1]:.2f} V")
        sweep_status_html.set_content('<span class="statuspill" style="color:var(--warn)">running…</span>')
        run_btn.props("disable")
        try:
            result = await _run_in_thread(HUB.elec.sweep, vs, avg)
            n_pts = len(result.avg_source_v)
            last_v = float(result.avg_source_v[-1])
            last_i = float(result.avg_current_a[-1])
            note_bias(v_set=last_v, i_meas=last_i, output_on=True)
            i_hero.set_content(_fmt_i(last_i))
            v_hero.set_content(_fmt_v(last_v))
            log_msg(f"  done: {n_pts} pts, I({last_v:.2f}V)={last_i:.3e} A")

            saved = None
            if bool(save_h5_sw.value):
                try:
                    saved = await _run_in_thread(_save_sweep_h5, result)
                    log_msg(f"  saved: {saved}")
                except Exception as e:
                    log_msg(f"  HDF5 save FAIL: {type(e).__name__}: {e}")
            sweep_status_html.set_content(
                f'<span class="statuspill" style="color:var(--ok)">done</span>'
            )
            iv_chart.options["series"][1]["data"] = list(zip(
                [float(x) for x in result.avg_source_v],
                [float(y) for y in result.avg_current_a],
            ))
            iv_chart.update()
        except Exception as e:
            log_msg(f"  sweep FAIL: {type(e).__name__}: {e}")
            sweep_status_html.set_content(
                f'<span class="statuspill" style="color:var(--bad)">failed</span>'
            )
        finally:
            clear_activity()
            run_btn.props(remove="disable")

    run_btn.on_click(do_run_sweep)

    # ---- 1-Hz tick: status bar + applied values + dirty marks --------
    def _set_applied(applied_html, value, unit=""):
        if value is None:
            applied_html.set_content('<span class="applied-note"></span>')
            return
        if isinstance(value, bool):
            txt = "on" if value else "off"
        elif isinstance(value, float):
            av = abs(value)
            if value != 0 and (av < 1e-3 or av >= 1e5):
                txt = f"{value:.3e}"
            else:
                txt = f"{value:.4g}"
        else:
            txt = str(value)
        if unit: txt = f"{txt} {unit}"
        applied_html.set_content(f'<span class="applied-note">applied: {txt}</span>')

    def tick():
        # Status bar + output card + connect button visibility
        if HUB.elec is None:
            statusbar.classes(remove="is-connected")
            model_html.set_content('<span class="model">not connected</span>')
            addr_html.set_content('<span class="addr"></span>')
            right_status.set_content('<span class="right-status">disconnected</span>')
            connect_btn.set_visibility(True)
            output_btn.set_content(
                '<button class="output-btn" data-on="false">'
                '<span class="ic">■</span><span>output —</span></button>'
            )
            output_card.classes(remove="is-on")
            readback_html.set_content(
                '<div class="readback">source level <span class="v">— V</span></div>'
            )
            for ap in (src_level_applied, src_range_applied, src_climit_applied,
                       mrng_applied, nplc_applied, trg_delay_applied):
                ap.set_content('<span class="applied-note"></span>')
            for blk in _blocks.values():
                blk.classes(remove="is-dirty")
            for _b, w, _gs, _ga, _eq in _dirty_specs:
                w.classes(remove="is-dirty")
            return

        statusbar.classes(add="is-connected")
        c = HUB.elec; drv = c._driver
        try: idn = c.identify()
        except Exception: idn = "Keysight B2987B"
        # split "Keysight B2987B [hardware] @ TCPIP::172.16.0.11::INSTR"
        if "@" in idn:
            left, _, right = idn.partition("@")
            model_html.set_content(f'<span class="model">{left.strip()}</span>')
            addr_html.set_content(f'<span class="addr">{right.strip()}</span>')
        else:
            model_html.set_content(f'<span class="model">{idn}</span>')
            addr_html.set_content('<span class="addr"></span>')
        right_status.set_content(
            '<span class="right-status">'
            + ("connected · OUTPUT ON" if drv._output_on else "connected · idle")
            + '</span>'
        )
        connect_btn.set_visibility(False)

        # Output card
        if drv._output_on:
            output_btn.set_content(
                '<button class="output-btn" data-on="true">'
                '<span class="ic">▶</span><span>output on</span></button>'
            )
            output_card.classes(add="is-on")
        else:
            output_btn.set_content(
                '<button class="output-btn" data-on="false">'
                '<span class="ic">■</span><span>output off</span></button>'
            )
            output_card.classes(remove="is-on")
        readback_html.set_content(
            f'<div class="readback">source level '
            f'<span class="v">{drv._source_voltage:.3f} V</span></div>'
        )

        # Applied-value notes
        _set_applied(src_level_applied, drv._source_voltage, "V")
        _set_applied(src_range_applied, c._source_range, "V")
        # Compliance isn't read back; show "—"
        src_climit_applied.set_content('<span class="applied-note">applied: —</span>')
        _set_applied(mrng_applied, getattr(c, "_current_range_v", None), "A")
        ap_s = getattr(c, "_current_aperture", None)
        _set_applied(nplc_applied, (ap_s * 60.0) if isinstance(ap_s, (int, float)) else None, "PLC")
        _set_applied(trg_delay_applied, getattr(c, "_delay_s", None), "s")

        # Dirty marks per field, then per block
        block_any_dirty = {k: False for k in _blocks}
        for block_name, wrap, get_staged, get_applied, eq in _dirty_specs:
            try:
                d = not eq(get_staged(), get_applied())
            except Exception:
                d = False
            if d:
                wrap.classes(add="is-dirty")
                block_any_dirty[block_name] = True
            else:
                wrap.classes(remove="is-dirty")
        for name, is_dirty in block_any_dirty.items():
            card = _blocks.get(name)
            if card is None: continue
            if is_dirty: card.classes(add="is-dirty")
            else:        card.classes(remove="is-dirty")

    tick()
    ui.timer(1.0, tick)


def _build_mux_tab():
    """96-channel IV-Pulse MUX front panel.

    Layout:
        [connect strip]
        [TOP grid 1.5fr | 1fr]
          [LEFT — Channel select: hero readout + channel/settle fields +
                   select / zero / refresh buttons]
          [RIGHT side column — Bypass toggle, then Arduino die temperature]
        [Channel sweep card — start/stop/dwell + run + status pill]
    """
    with ui.element("div").classes("mux-panel w-full"):

        # ---- Connect strip ----
        with ui.row().classes("connstrip w-full") as connstrip:
            ui.html('<span class="dot"></span>')
            conn_lbl = ui.html(
                '<span class="lbl">Not connected — 96-channel IV-Pulse MUX</span>'
            )
            async def _do_connect():
                ok = await _quick_connect("mux")
                ui.notify("mux: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense") \
                .style("margin-left:auto")

        # ---- Top grid: Channel select | (Bypass + Temperature) ----
        with ui.element("div").classes("top-grid w-full"):

            # ----- LEFT: Channel select -----
            with ui.card().classes("card-mux"):
                ui.html('<p class="eyebrow">Channel select</p>')

                # Hero readout — active channel
                with ui.element("div").classes("hero"):
                    ui.html('<span class="k">active</span>')
                    active_ch = ui.html('<span class="v">—</span>')

                # 2-col field row: channel + settle
                with ui.element("div") \
                        .style("display:grid; grid-template-columns:1fr 1fr; "
                               "gap:14px; margin-bottom:16px"):
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Channel (1–96)</label>')
                        ch_n = ui.number(value=1, step=1, format="%d",
                                         min=1, max=96) \
                            .props("dense filled hide-bottom-space")
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">Settle</label>')
                        settle_n = ui.number(value=0.050, step=0.01, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="s"')

                async def do_select():
                    if HUB.mux is None:
                        ui.notify("mux not connected",
                                  type="warning", position="top", timeout=2000)
                        return
                    ch = int(ch_n.value or 0)
                    if ch < 1 or ch > 96:
                        ui.notify("channel must be 1–96",
                                  type="warning", position="top", timeout=2000)
                        return
                    try:
                        await _run_in_thread(HUB.mux.select, ch,
                                             float(settle_n.value or 0))
                        log.info("mux: ch %d selected", ch)
                    except Exception as e:
                        ui.notify(f"select FAIL: {type(e).__name__}: {e}",
                                  type="negative", position="top", timeout=4000)

                async def do_zero():
                    if HUB.mux is None:
                        ui.notify("mux not connected",
                                  type="warning", position="top", timeout=2000)
                        return
                    try:
                        await _run_in_thread(HUB.mux.zero)
                        log.info("mux: zeroed")
                    except Exception as e:
                        ui.notify(f"zero FAIL: {type(e).__name__}: {e}",
                                  type="negative", position="top", timeout=4000)

                async def do_refresh():
                    # Pull state from driver into the readout. Same as the
                    # tick() refresh below but on user demand.
                    _refresh_state()

                with ui.row().classes("gap-2"):
                    ui.button("select", on_click=do_select).props("color=primary")
                    ui.button("zero", on_click=do_zero).props("flat")
                    ui.button("refresh", on_click=do_refresh).props("flat")

            # ----- RIGHT side column -----
            with ui.element("div").classes("side-col"):

                # ---- Bypass card ----
                with ui.card().classes("card-mux"):
                    ui.html('<p class="eyebrow">Bypass relay</p>')
                    with ui.row().classes("tgl-row w-full"):
                        ui.html('<span class="lbl">Bypass MUX</span>')
                        with ui.row().classes("items-center").style("gap:10px"):
                            bypass_state_html = ui.html('<span class="state">off</span>')
                            bypass_sw = ui.switch(value=False).props("dense")

                    async def on_bypass(_e):
                        if HUB.mux is None:
                            ui.notify("mux not connected", type="warning",
                                      position="top", timeout=2000)
                            # Roll back the toggle UI state
                            bypass_sw.set_value(not bool(_e.value))
                            return
                        on = bool(_e.value)
                        try:
                            if on:
                                await _run_in_thread(HUB.mux.bypass_on)
                            else:
                                await _run_in_thread(HUB.mux.bypass_off)
                            bypass_state_html.set_content(
                                f'<span class="state">{"on" if on else "off"}</span>'
                            )
                            log.info("mux: bypass %s", "on" if on else "off")
                        except Exception as e:
                            ui.notify(f"bypass FAIL: {type(e).__name__}: {e}",
                                      type="negative", position="top", timeout=4000)
                            bypass_sw.set_value(not on)
                    bypass_sw.on_value_change(on_bypass)

                # ---- Arduino die temperature card ----
                with ui.card().classes("card-mux"):
                    ui.html('<p class="eyebrow">Arduino die temperature</p>')
                    with ui.row().classes("items-end w-full").style("gap:12px"):
                        with ui.element("div").classes("fld").style("width:120px"):
                            ui.html('<label class="fld-lbl">N samples</label>')
                            nsamp_n = ui.number(value=3, step=1, format="%d") \
                                .props('dense filled hide-bottom-space suffix="#"')
                        temp_html = ui.html(
                            '<p class="readline">'
                            'T = <span class="muted">—</span> K</p>'
                        ).style("margin-left:.2rem; flex:1")

                        async def do_read_temp():
                            if HUB.mux is None:
                                ui.notify("mux not connected", type="warning",
                                          position="top", timeout=2000)
                                return
                            try:
                                samples = await _run_in_thread(
                                    HUB.mux.read_temperature,
                                    int(nsamp_n.value or 3),
                                )
                                # Defensive: handle list, numpy array, or
                                # None.  `not <array>` would trigger
                                # numpy's truth-value-ambiguity error.
                                if samples is None or len(samples) == 0:
                                    temp_html.set_content(
                                        '<p class="readline">'
                                        'T = <span class="muted">—</span> K</p>'
                                    )
                                    return
                                avg = sum(samples) / len(samples)
                                temp_html.set_content(
                                    f'<p class="readline">'
                                    f'T = <span>{avg:.2f}</span> K '
                                    f'<span class="muted">(n={len(samples)})</span></p>'
                                )
                                log.info("mux temperature: %.2f K (n=%d)",
                                         avg, len(samples))
                            except Exception as e:
                                ui.notify(
                                    f"read T FAIL: {type(e).__name__}: {e}",
                                    type="negative", position="top", timeout=4000,
                                )
                        ui.button("read", on_click=do_read_temp) \
                            .props("flat") \
                            .style("margin-left:auto")

        # ---- Channel sweep card ----
        with ui.card().classes("card-mux w-full"):
            ui.html('<p class="eyebrow">Channel sweep · no measurement callback</p>')
            ui.html('<p class="desc">Walks the MUX through a contiguous range '
                    'with a delay at each step. Use this for relay-click testing '
                    '— no measurement is taken.</p>')
            with ui.row().classes("items-end w-full").style("gap:12px; flex-wrap:wrap"):
                with ui.element("div").classes("fld").style("width:130px"):
                    ui.html('<label class="fld-lbl">Start ch</label>')
                    sw_start_n = ui.number(value=1, step=1, format="%d",
                                           min=1, max=96) \
                        .props("dense filled hide-bottom-space")
                with ui.element("div").classes("fld").style("width:130px"):
                    ui.html('<label class="fld-lbl">Stop ch</label>')
                    sw_stop_n = ui.number(value=96, step=1, format="%d",
                                          min=1, max=96) \
                        .props("dense filled hide-bottom-space")
                with ui.element("div").classes("fld").style("width:140px"):
                    ui.html('<label class="fld-lbl">Dwell</label>')
                    sw_dwell_n = ui.number(value=0.100, step=0.05, format="%.3f") \
                        .props('dense filled hide-bottom-space suffix="s"')
                sweep_btn = ui.button("▶ run sweep").props("color=primary")
                sweep_status_html = ui.html('<span class="statuspill">idle</span>') \
                    .style("margin-left:auto")

                async def do_mux_sweep():
                    if HUB.mux is None:
                        ui.notify("mux not connected", type="warning",
                                  position="top", timeout=2000)
                        return
                    a = int(sw_start_n.value or 1)
                    b = int(sw_stop_n.value or 96)
                    a = max(1, min(96, a)); b = max(1, min(96, b))
                    chans = list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
                    dwell = float(sw_dwell_n.value or 0)
                    sweep_status_html.set_content(
                        f'<span class="statuspill" style="color:var(--warn)">'
                        f'running… {chans[0]}→{chans[-1]} ({len(chans)} steps)</span>'
                    )
                    sweep_btn.props("disable")
                    set_activity("mux channel sweep",
                                 f"{chans[0]}→{chans[-1]} · dwell={dwell}s")
                    try:
                        await _run_in_thread(
                            HUB.mux.sweep, chans, lambda _ch: None, dwell, True,
                        )
                        sweep_status_html.set_content(
                            f'<span class="statuspill" style="color:var(--ok)">'
                            f'done · {len(chans)} steps</span>'
                        )
                        log.info("mux sweep done: %d steps", len(chans))
                    except Exception as e:
                        sweep_status_html.set_content(
                            f'<span class="statuspill" style="color:var(--bad)">'
                            f'failed: {type(e).__name__}</span>'
                        )
                        log.exception("mux sweep failed: %s", e)
                    finally:
                        clear_activity()
                        sweep_btn.props(remove="disable")
                sweep_btn.on_click(do_mux_sweep)

    # ---- Periodic refresh (state + connect strip + active channel) ----
    def _refresh_state():
        if HUB.mux is None:
            connstrip.classes(remove="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Not connected — 96-channel IV-Pulse MUX</span>'
            )
            connect_btn.set_visibility(True)
            active_ch.set_content('<span class="v">—</span>')
            return
        connstrip.classes(add="is-connected")
        conn_lbl.set_content(
            '<span class="lbl">Connected — 96-channel IV-Pulse MUX</span>'
        )
        connect_btn.set_visibility(False)
        try:
            ch = HUB.mux.active_channel()
        except Exception:
            ch = None
        if ch is None:
            active_ch.set_content('<span class="v" '
                                  'style="color:var(--mut)">none</span>')
        else:
            active_ch.set_content(f'<span class="v">ch{ch}</span>')

    _refresh_state()
    ui.timer(1.5, _refresh_state)


def _build_stage_tab():
    """Phidget XY stage panel.

    Layout:
        [connect strip]
        [layout — left controls column, right webcam card]
          LEFT stack:
            - Position & jog (hero readback + 5×5 jog pad + fine/coarse step)
            - Move (absolute | relative side-by-side, + de-energize-after toggle)
            - Coils & homing (axis select + energize/de-energize, axis + home)
          RIGHT:
            - Live webcam card

    Jog pad arrows: fine step (default 1 mm). Corner ±10 buttons: coarse
    step (default 10 mm). Both step values are editable.
    """

    with ui.element("div").classes("stage-panel w-full"):

        # ---- Connect strip ----
        with ui.row().classes("connstrip w-full") as connstrip:
            ui.html('<span class="dot"></span>')
            conn_lbl = ui.html(
                '<span class="lbl">Not connected — Phidget XY stage</span>'
            )
            async def _do_connect():
                ok = await _quick_connect("stage")
                ui.notify("stage: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense") \
                .style("margin-left:auto")

        with ui.element("div").classes("layout w-full"):

            # ============== LEFT column ==============
            with ui.element("div").classes("stack"):

                # ----- POSITION & JOG card -----
                with ui.card().classes("card-s"):
                    ui.html('<p class="eyebrow">Position &amp; jog</p>')
                    with ui.element("div").classes("pos-hero"):
                        pos_html = ui.html(
                            '<span class="v"><span class="muted">X — · Y —</span></span>'
                        )
                        ui.html('<span class="u">mm</span>')
                    coil_html = ui.html(
                        '<div class="coil-line">coils: <span>—</span></div>'
                    )

                    async def read_pos_now(notify_if_disc: bool = True):
                        if HUB.stage is None:
                            if notify_if_disc:
                                ui.notify("stage not connected",
                                          type="warning",
                                          position="top", timeout=2000)
                            return None
                        try:
                            x, y = await _run_in_thread(P.stage_position, HUB.stage)
                            pos_html.set_content(
                                f'<span class="v">X {x:+.3f} · Y {y:+.3f}</span>'
                            )
                            return (x, y)
                        except Exception as e:
                            ui.notify(f"read pos FAIL: {type(e).__name__}: {e}",
                                      type="negative", position="top",
                                      timeout=4000)
                            return None

                    async def do_jog(ax: str, sign: float, step: float):
                        if HUB.stage is None:
                            ui.notify("stage not connected",
                                      type="warning",
                                      position="top", timeout=2000); return
                        dx = sign * step if ax == "x" else 0.0
                        dy = sign * step if ax == "y" else 0.0
                        try:
                            await _run_in_thread(
                                HUB.stage.move_by, dx, dy,
                                HUB.config.stage_deenergize,
                            )
                            x, y = await _run_in_thread(P.stage_position, HUB.stage)
                            pos_html.set_content(
                                f'<span class="v">X {x:+.3f} · Y {y:+.3f}</span>'
                            )
                            log.info("stage jog %s%+.3f mm → (%.3f, %.3f)",
                                     ax, sign * step, x, y)
                        except Exception as e:
                            ui.notify(f"jog FAIL: {type(e).__name__}: {e}",
                                      type="negative", position="top",
                                      timeout=4000)

                    def _jog_fine(ax: str, sign: float):
                        return lambda: do_jog(ax, sign, float(fine_n.value or 0))
                    def _jog_coarse(ax: str, sign: float):
                        return lambda: do_jog(ax, sign, float(coarse_n.value or 0))

                    # Camera-view frame of reference for this rig:
                    #   stage +X  → DOWN  on the webcam
                    #   stage +Y  → LEFT  on the webcam
                    # The jog pad therefore reads as visual screen direction
                    # (clicking ↑ moves the stage up on the camera), not as
                    # axis names. The Move / Move-by inputs below still
                    # speak in (X, Y) axis terms.
                    with ui.element("div").classes("jogwrap"):
                        # 5×5 jog pad — labels read as camera-screen direction
                        with ui.element("div").classes("jogpad"):
                            # Top (visually up) → −X
                            ui.button("⇑", on_click=_jog_coarse("x", -1)) \
                                .classes("jb coarse p-yc1")
                            ui.button("↑", on_click=_jog_fine("x", -1)) \
                                .classes("jb fine p-yf1")
                            # Left (visually left) → +Y
                            ui.button("⇐", on_click=_jog_coarse("y", +1)) \
                                .classes("jb coarse p-xc0")
                            ui.button("←", on_click=_jog_fine("y", +1)) \
                                .classes("jb fine p-xf0")
                            ui.button("read",
                                      on_click=lambda: read_pos_now()) \
                                .classes("jb center p-ctr")
                            # Right (visually right) → −Y
                            ui.button("→", on_click=_jog_fine("y", -1)) \
                                .classes("jb fine p-xf1")
                            ui.button("⇒", on_click=_jog_coarse("y", -1)) \
                                .classes("jb coarse p-xc1")
                            # Bottom (visually down) → +X
                            ui.button("↓", on_click=_jog_fine("x", +1)) \
                                .classes("jb fine p-yf0")
                            ui.button("⇓", on_click=_jog_coarse("x", +1)) \
                                .classes("jb coarse p-yc0")

                        with ui.element("div").classes("jog-steps"):
                            # Camera-axis indicator diagram: a tiny SVG
                            # showing which axis points which way as seen
                            # through the lab webcam.
                            ui.html(
                                '<div class="axis-diagram" '
                                'title="frame of reference seen on the webcam">'
                                '<div class="axis-cap">camera view</div>'
                                '<svg viewBox="0 0 110 90" '
                                'width="100%" style="max-width:130px">'
                                '<defs>'
                                '<marker id="ah-acc" viewBox="0 0 10 10" '
                                'refX="9" refY="5" markerWidth="6" markerHeight="6" '
                                'orient="auto">'
                                '<path d="M0 0 L10 5 L0 10 z" fill="#58a6ff"/>'
                                '</marker>'
                                '</defs>'
                                # cross-hair origin
                                '<circle cx="60" cy="40" r="2.5" fill="#8a93a6"/>'
                                # +Y points LEFT
                                '<line x1="60" y1="40" x2="14" y2="40" '
                                'stroke="#58a6ff" stroke-width="1.8" '
                                'marker-end="url(#ah-acc)"/>'
                                '<text x="6" y="36" fill="#58a6ff" '
                                'font-size="11" font-family="ui-monospace,Menlo,monospace">'
                                'Y+</text>'
                                # +X points DOWN
                                '<line x1="60" y1="40" x2="60" y2="82" '
                                'stroke="#58a6ff" stroke-width="1.8" '
                                'marker-end="url(#ah-acc)"/>'
                                '<text x="66" y="80" fill="#58a6ff" '
                                'font-size="11" font-family="ui-monospace,Menlo,monospace">'
                                'X+</text>'
                                # negative ticks (faint)
                                '<line x1="60" y1="40" x2="100" y2="40" '
                                'stroke="#5c6775" stroke-width="1" '
                                'stroke-dasharray="2 2"/>'
                                '<text x="90" y="36" fill="#5c6775" '
                                'font-size="10" font-family="ui-monospace,Menlo,monospace">'
                                'Y−</text>'
                                '<line x1="60" y1="40" x2="60" y2="6" '
                                'stroke="#5c6775" stroke-width="1" '
                                'stroke-dasharray="2 2"/>'
                                '<text x="66" y="14" fill="#5c6775" '
                                'font-size="10" font-family="ui-monospace,Menlo,monospace">'
                                'X−</text>'
                                '</svg>'
                                '</div>'
                            )

                            with ui.element("div").classes("fld"):
                                ui.html(
                                    '<label class="fld-lbl-rich">'
                                    '<span class="swatch">'
                                    '<span class="box"></span>Fine step</span></label>'
                                )
                                fine_n = ui.number(value=1.0, step=0.1,
                                                    min=0.0, format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')
                            with ui.element("div").classes("fld"):
                                ui.html(
                                    '<label class="fld-lbl-rich">'
                                    '<span class="swatch">'
                                    '<span class="box coarse"></span>Coarse step</span></label>'
                                )
                                coarse_n = ui.number(value=10.0, step=1.0,
                                                      min=0.0, format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')
                            ui.html(
                                '<span class="hint">'
                                'Arrows match the camera view: ↑ moves the '
                                'stage up on the screen (sends −X). '
                                '⇈ ⇊ ⇇ ⇉ use Coarse step.'
                                '</span>'
                            )

                # ----- MOVE card -----
                with ui.card().classes("card-s"):
                    ui.html('<p class="eyebrow">Move</p>')
                    with ui.element("div").classes("moves"):
                        # Absolute
                        with ui.element("div"):
                            ui.html('<p class="subhead">Absolute</p>')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:10px"):
                                ui.html('<label class="fld-lbl">X target</label>')
                                abs_x_n = ui.number(value=0.0, step=0.1,
                                                     format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:12px"):
                                ui.html('<label class="fld-lbl">Y target</label>')
                                abs_y_n = ui.number(value=0.0, step=0.1,
                                                     format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')

                            async def do_move_abs():
                                if HUB.stage is None:
                                    ui.notify("stage not connected",
                                              type="warning",
                                              position="top", timeout=2000); return
                                try:
                                    await _run_in_thread(
                                        P.move_stage, HUB.stage,
                                        float(abs_x_n.value or 0),
                                        float(abs_y_n.value or 0),
                                        bool(deen_sw.value),
                                    )
                                    x, y = await _run_in_thread(P.stage_position, HUB.stage)
                                    pos_html.set_content(
                                        f'<span class="v">X {x:+.3f} · Y {y:+.3f}</span>'
                                    )
                                    log.info("stage move_abs → (%.3f, %.3f)", x, y)
                                except Exception as e:
                                    ui.notify(
                                        f"move FAIL: {type(e).__name__}: {e}",
                                        type="negative", position="top",
                                        timeout=4000,
                                    )

                            with ui.element("div").classes("btnrow"):
                                ui.button("move", on_click=do_move_abs) \
                                    .props("color=primary")

                        # Relative
                        with ui.element("div"):
                            ui.html('<p class="subhead">Relative</p>')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:10px"):
                                ui.html('<label class="fld-lbl">ΔX</label>')
                                rel_x_n = ui.number(value=0.0, step=0.1,
                                                     format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:12px"):
                                ui.html('<label class="fld-lbl">ΔY</label>')
                                rel_y_n = ui.number(value=0.0, step=0.1,
                                                     format="%.3f") \
                                    .props('dense filled hide-bottom-space suffix="mm"')

                            async def do_move_rel():
                                if HUB.stage is None:
                                    ui.notify("stage not connected",
                                              type="warning",
                                              position="top", timeout=2000); return
                                try:
                                    await _run_in_thread(
                                        HUB.stage.move_by,
                                        float(rel_x_n.value or 0),
                                        float(rel_y_n.value or 0),
                                        bool(deen_sw.value),
                                    )
                                    x, y = await _run_in_thread(P.stage_position, HUB.stage)
                                    pos_html.set_content(
                                        f'<span class="v">X {x:+.3f} · Y {y:+.3f}</span>'
                                    )
                                    log.info("stage move_rel Δ=(%+.3f, %+.3f) → (%.3f, %.3f)",
                                             rel_x_n.value, rel_y_n.value, x, y)
                                except Exception as e:
                                    ui.notify(
                                        f"move by FAIL: {type(e).__name__}: {e}",
                                        type="negative", position="top",
                                        timeout=4000,
                                    )

                            with ui.element("div").classes("btnrow"):
                                ui.button("move by", on_click=do_move_rel) \
                                    .props("color=primary")

                    # De-energize toggle (config — blue accent, not red)
                    with ui.row().classes("tgl-inline").style("margin-top:16px"):
                        deen_sw = ui.switch(
                            value=bool(HUB.config.stage_deenergize)
                        ).props("dense color=primary")
                        ui.html('<span>De-energize after move</span>')

            # ============== RIGHT stack: Webcam + Coils & homing ============
            with ui.element("div").classes("stack"):

                # ----- Webcam card -----
                with ui.card().classes("card-s"):
                    ui.html('<p class="eyebrow">Webcam · live</p>')
                    # Camera frame includes an axis-direction overlay
                    # (top-right corner). +X points DOWN on screen, +Y
                    # points LEFT — matches the jog-pad wiring.
                    ui.html(
                        '<div class="cam">'
                        '<span class="live-pill"><span class="d"></span>live</span>'
                        '<img src="/webcam.mjpeg" alt="webcam stream"/>'
                        '<div class="cam-axis">'
                        '<svg viewBox="0 0 70 70" width="60" height="60">'
                        '<defs>'
                        '<marker id="ah-w" viewBox="0 0 10 10" refX="9" refY="5" '
                        'markerWidth="6" markerHeight="6" orient="auto">'
                        '<path d="M0 0 L10 5 L0 10 z" fill="#58a6ff"/>'
                        '</marker>'
                        '</defs>'
                        '<circle cx="42" cy="28" r="2" fill="#dde3ee"/>'
                        '<line x1="42" y1="28" x2="10" y2="28" '
                        'stroke="#58a6ff" stroke-width="1.8" marker-end="url(#ah-w)"/>'
                        '<text x="2" y="24" fill="#58a6ff" font-size="10" '
                        'font-family="ui-monospace,Menlo,monospace">Y+</text>'
                        '<line x1="42" y1="28" x2="42" y2="60" '
                        'stroke="#58a6ff" stroke-width="1.8" marker-end="url(#ah-w)"/>'
                        '<text x="48" y="58" fill="#58a6ff" font-size="10" '
                        'font-family="ui-monospace,Menlo,monospace">X+</text>'
                        '</svg>'
                        '</div>'
                        '</div>'
                    )

                # ----- COILS & HOMING card -----
                with ui.card().classes("card-s"):
                    ui.html('<p class="eyebrow">Coils &amp; homing</p>')
                    with ui.element("div").classes("moves"):
                        # Coils sub-block
                        with ui.element("div"):
                            ui.html('<p class="subhead">Coils</p>')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:12px"):
                                ui.html('<label class="fld-lbl">Axis</label>')
                                coil_axis_sel = ui.select(
                                    {"both": "both", "x": "X", "y": "Y"},
                                    value="both",
                                ).props("dense filled hide-bottom-space")

                            async def do_energize():
                                if HUB.stage is None:
                                    ui.notify("stage not connected",
                                              type="warning",
                                              position="top", timeout=2000); return
                                try:
                                    await _run_in_thread(
                                        HUB.stage.energize,
                                        str(coil_axis_sel.value),
                                    )
                                    log.info("stage energize %s",
                                             coil_axis_sel.value)
                                except Exception as e:
                                    ui.notify(
                                        f"energize FAIL: {type(e).__name__}: {e}",
                                        type="negative", position="top",
                                        timeout=4000,
                                    )

                            async def do_deenergize():
                                if HUB.stage is None:
                                    ui.notify("stage not connected",
                                              type="warning",
                                              position="top", timeout=2000); return
                                try:
                                    await _run_in_thread(
                                        HUB.stage.deenergize,
                                        str(coil_axis_sel.value),
                                    )
                                    log.info("stage de-energize %s",
                                             coil_axis_sel.value)
                                except Exception as e:
                                    ui.notify(
                                        f"de-energize FAIL: {type(e).__name__}: {e}",
                                        type="negative", position="top",
                                        timeout=4000,
                                    )

                            with ui.element("div").classes("btnrow"):
                                ui.button("energize",
                                          on_click=do_energize).props("flat")
                                ui.button("de-energize",
                                          on_click=do_deenergize).props("flat")

                        # Home sub-block
                        with ui.element("div"):
                            ui.html('<p class="subhead">Home</p>')
                            with ui.element("div").classes("fld") \
                                    .style("margin-bottom:12px"):
                                ui.html('<label class="fld-lbl">Axis</label>')
                                home_axis_sel = ui.select(
                                    {"both": "both", "x": "X", "y": "Y"},
                                    value="both",
                                ).props("dense filled hide-bottom-space")

                            async def do_home():
                                if HUB.stage is None:
                                    ui.notify("stage not connected",
                                              type="warning",
                                              position="top", timeout=2000); return
                                try:
                                    await _run_in_thread(
                                        HUB.stage.home,
                                        str(home_axis_sel.value),
                                    )
                                    x, y = await _run_in_thread(P.stage_position, HUB.stage)
                                    pos_html.set_content(
                                        f'<span class="v">X {x:+.3f} · Y {y:+.3f}</span>'
                                    )
                                    log.info("stage homed %s → (%.3f, %.3f)",
                                             home_axis_sel.value, x, y)
                                except Exception as e:
                                    ui.notify(
                                        f"home FAIL: {type(e).__name__}: {e}",
                                        type="negative", position="top",
                                        timeout=4000,
                                    )

                            with ui.element("div").classes("btnrow"):
                                ui.button("home", on_click=do_home).props("flat")
                            ui.html(
                                '<p class="hint" style="margin-top:8px">'
                                'Drives to the limit switch and resets origin to 0.'
                                '</p>'
                            )

    # ---- Periodic refresh: connect strip + coil state + position ----
    def tick():
        if HUB.stage is None:
            connstrip.classes(remove="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Not connected — Phidget XY stage</span>'
            )
            connect_btn.set_visibility(True)
            coil_html.set_content(
                '<div class="coil-line">coils: <span>—</span></div>'
            )
            return
        connstrip.classes(add="is-connected")
        conn_lbl.set_content(
            '<span class="lbl">Connected — Phidget XY stage</span>'
        )
        connect_btn.set_visibility(False)
        try:
            ex, ey = HUB.stage.is_energized()
        except Exception:
            ex = ey = False
        if ex and ey:
            coil_state = '<span class="on">energized · X+Y</span>'
        elif ex or ey:
            ax = "X" if ex else "Y"
            coil_state = f'<span class="on">energized · {ax}</span>'
        else:
            coil_state = '<span>off</span>'
        coil_html.set_content(f'<div class="coil-line">coils: {coil_state}</div>')

    tick()
    ui.timer(2.0, tick)


def _build_k6485_tab():
    """Keithley 6485 picoammeter front panel.

    Layout:
        [connect strip]
        [TOP grid 1.6fr | 1fr]
          [LEFT — hero current readout + strip-chart plot + read buttons]
          [RIGHT side column — Range & integration card, Read settings card]

    Strip chart shows the last 40 readings. Single reads render as smaller
    dots, averaged reads as larger dots, both connected by a polyline.
    Empty placeholder until the first read lands.
    """
    # ----- local state -----
    _readings: list[dict] = []   # [{"i": float, "avg": bool}, ...]
    MAX_KEEP = 40

    with ui.element("div").classes("k6485-panel w-full"):

        # ---- Connect strip ----
        with ui.row().classes("connstrip w-full") as connstrip:
            ui.html('<span class="dot"></span>')
            conn_lbl = ui.html(
                '<span class="lbl">Not connected — Keithley 6485 picoammeter</span>'
            )
            async def _do_connect():
                ok = await _quick_connect("k6485")
                ui.notify("k6485: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense") \
                .style("margin-left:auto")

        # ---- Top grid: readout/plot (left) + settings column (right) ----
        with ui.element("div").classes("top-grid w-full"):

            # ============ LEFT: readout + plot ============
            with ui.card().classes("card-k"):
                with ui.row().classes("items-center w-full") \
                        .style("gap:14px; margin-bottom:10px"):
                    ui.html('<p class="eyebrow" style="margin:0">Current</p>')
                    ui.html('<span style="flex:1"></span>')
                    single_btn = ui.button("single read").props("color=primary")
                    averaged_btn = ui.button("averaged").props("flat")

                with ui.element("div").classes("hero"):
                    ui.html('<span class="k">I =</span>')
                    hero_val = ui.html('<span class="v">—</span>')
                    ui.html('<span class="u">A</span>')

                read_meta = ui.html('<span class="read-meta"></span>') \
                    .classes("read-meta")

                # ECharts strip-chart — line with circular symbols. Two
                # series so we can size single vs averaged markers
                # differently and keep them on one polyline (we just
                # render both series with the same baseline line).
                chart_opts = {
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 65, "right": 18, "top": 14, "bottom": 38},
                    "backgroundColor": "transparent",
                    "textStyle": {"color": "#dde3ee"},
                    "xAxis": {
                        "type": "value", "name": "reading #",
                        "nameLocation": "middle", "nameGap": 22, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "yAxis": {
                        "type": "value", "name": "current (A)",
                        "nameLocation": "middle", "nameGap": 48, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10,
                                      "formatter": "{value}"},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "series": [
                        # The full polyline (all readings)
                        {"name": "trace", "type": "line",
                         "data": [], "showSymbol": False,
                         "lineStyle": {"width": 1.5, "color": "#3b82f6"},
                         "z": 1},
                        # Single-read markers (small)
                        {"name": "single", "type": "scatter",
                         "data": [], "symbolSize": 4,
                         "itemStyle": {"color": "#3b82f6"}, "z": 2},
                        # Averaged-read markers (larger)
                        {"name": "averaged", "type": "scatter",
                         "data": [], "symbolSize": 8,
                         "itemStyle": {"color": "#3b82f6"}, "z": 3},
                    ],
                }

                with ui.element("div").classes("plotbox-k") as plotbox:
                    iv_chart = ui.echart(chart_opts) \
                        .classes("w-full").style("height:100%")
                    plot_empty = ui.html(
                        '<div class="plot-empty">'
                        'no data — take a reading</div>'
                    )

            # ============ RIGHT: settings stack ============
            with ui.element("div").classes("side-col"):

                # ---- Range & integration ----
                with ui.card().classes("card-k") as range_card:
                    with ui.row().classes("items-center w-full") \
                            .style("justify-content:space-between; margin-bottom:14px"):
                        ui.html('<p class="eyebrow" style="margin:0">Range &amp; integration</p>')
                        apply_range_btn = ui.button("apply").props("flat") \
                            .classes("apply-btn")

                    with ui.element("div").classes("fld") \
                            .style("margin-bottom:12px"):
                        ui.html('<label class="fld-lbl">Current range</label>')
                        range_sel = ui.select(
                            {"AUTO": "AUTO",
                             "2e-9": "2 nA", "20e-9": "20 nA", "200e-9": "200 nA",
                             "2e-6": "2 µA", "20e-6": "20 µA", "200e-6": "200 µA",
                             "2e-3": "2 mA"},
                            value="AUTO",
                        ).props("dense filled hide-bottom-space")

                    with ui.element("div").classes("fld") \
                            .style("margin-bottom:14px"):
                        ui.html('<label class="fld-lbl">NPLC</label>')
                        nplc_n = ui.number(value=1.0, step=0.1, format="%.2f") \
                            .props('dense filled hide-bottom-space suffix="PLC"')
                        ui.html('<span class="hint">'
                                '1 PLC = 16.7 ms @ 60 Hz</span>')

                    with ui.row().classes("tgl-row w-full") \
                            .style("margin-bottom:12px"):
                        ui.html('<span class="lbl">Zero check</span>')
                        with ui.row().classes("items-center").style("gap:10px"):
                            zc_state_html = ui.html('<span class="state">—</span>')
                            zc_sw = ui.switch(value=False).props("dense")

                    zero_correct_btn = ui.button("zero correct").props("flat")

                # ---- Read settings ----
                with ui.card().classes("card-k"):
                    ui.html('<p class="eyebrow">Read settings</p>')
                    with ui.element("div").classes("grid") \
                            .style("display:grid; grid-template-columns:1fr 1fr; gap:14px"):
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">N samples (avg)</label>')
                            nsamp_n = ui.number(value=10, step=1, format="%d") \
                                .props('dense filled hide-bottom-space suffix="#"')
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">Delay between</label>')
                            delay_n = ui.number(value=0.050, step=0.01, format="%.3f") \
                                .props('dense filled hide-bottom-space suffix="s"')

    # ===== Wiring =====

    def _fmt_i(v: float) -> str:
        return f"{v:.3e}"

    def _redraw_plot():
        if not _readings:
            iv_chart.options["series"][0]["data"] = []
            iv_chart.options["series"][1]["data"] = []
            iv_chart.options["series"][2]["data"] = []
            iv_chart.update()
            return
        # x-axis is reading index (1-based)
        line = [[i + 1, r["i"]] for i, r in enumerate(_readings)]
        singles = [[i + 1, r["i"]] for i, r in enumerate(_readings)
                   if not r["avg"]]
        averages = [[i + 1, r["i"]] for i, r in enumerate(_readings)
                    if r["avg"]]
        iv_chart.options["series"][0]["data"] = line
        iv_chart.options["series"][1]["data"] = singles
        iv_chart.options["series"][2]["data"] = averages
        iv_chart.update()

    def _push_reading(value: float, averaged: bool):
        _readings.append({"i": float(value), "avg": bool(averaged)})
        while len(_readings) > MAX_KEEP:
            _readings.pop(0)
        plot_empty.set_visibility(False)
        _redraw_plot()

    async def do_single_read():
        if HUB.k6485 is None:
            ui.notify("k6485 not connected", type="warning",
                      position="top", timeout=2000); return
        try:
            v = await _run_in_thread(HUB.k6485.read_single)
            hero_val.classes(remove="no-data")
            hero_val.set_content(f'<span class="v">{_fmt_i(v)}</span>')
            read_meta.set_content('single read')
            _push_reading(v, False)
            log.info("k6485 single read: %.3e A", v)
        except Exception as e:
            ui.notify(f"read FAIL: {type(e).__name__}: {e}",
                      type="negative", position="top", timeout=4000)
            log.exception("k6485 single read failed: %s", e)

    async def do_averaged_read():
        if HUB.k6485 is None:
            ui.notify("k6485 not connected", type="warning",
                      position="top", timeout=2000); return
        n = max(1, int(nsamp_n.value or 10))
        d = float(delay_n.value or 0)
        try:
            samples, _ts = await _run_in_thread(
                HUB.k6485.read_n, n, d,
            )
            arr = _np.asarray(samples, dtype=float) if (
                hasattr(samples, "__iter__")
            ) else _np.array([float(samples)])
            mean = float(arr.mean()) if arr.size else float("nan")
            sd   = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
            sem  = sd / (arr.size ** 0.5) if arr.size else 0.0
            hero_val.classes(remove="no-data")
            hero_val.set_content(f'<span class="v">{_fmt_i(mean)}</span>')
            read_meta.set_content(
                f'averaged · n={int(arr.size)} · sd={sd:.2e} · sem={sem:.2e}'
            )
            _push_reading(mean, True)
            log.info("k6485 avg read: %.3e A (n=%d sd=%.2e)", mean, arr.size, sd)
        except Exception as e:
            ui.notify(f"avg read FAIL: {type(e).__name__}: {e}",
                      type="negative", position="top", timeout=4000)
            log.exception("k6485 averaged read failed: %s", e)

    single_btn.on_click(do_single_read)
    averaged_btn.on_click(do_averaged_read)

    # Need numpy for the averaged stats
    import numpy as _np

    # ---- Range & NPLC apply (one Apply per block) ----
    def _mark_range_dirty(_e=None):
        apply_range_btn.classes(add="is-dirty")
    range_sel.on_value_change(_mark_range_dirty)
    nplc_n.on_value_change(_mark_range_dirty)

    async def apply_range():
        if HUB.k6485 is None:
            ui.notify("k6485 not connected", type="warning",
                      position="top", timeout=2000); return
        try:
            sel = str(range_sel.value or "AUTO")
            if sel == "AUTO":
                await _run_in_thread(HUB.k6485.set_range, "AUTO")
            else:
                await _run_in_thread(HUB.k6485.set_range, float(sel))
            await _run_in_thread(HUB.k6485.set_nplc, float(nplc_n.value or 1.0))
            apply_range_btn.classes(remove="is-dirty")
            ui.notify(
                f"applied · range={sel} · NPLC={float(nplc_n.value or 1.0):.2f}",
                type="positive", position="top", timeout=2200,
            )
            log.info("k6485 cfg: range=%s NPLC=%.2f", sel, float(nplc_n.value or 1.0))
        except Exception as e:
            ui.notify(f"apply FAIL: {type(e).__name__}: {e}",
                      type="negative", position="top", timeout=4000)
    apply_range_btn.on_click(apply_range)

    # ---- Zero-check toggle (single stateful, not two buttons) ----
    async def _on_zc(_e):
        if HUB.k6485 is None:
            ui.notify("k6485 not connected", type="warning",
                      position="top", timeout=2000)
            zc_sw.set_value(not bool(_e.value))
            return
        on = bool(_e.value)
        try:
            if on:
                await _run_in_thread(HUB.k6485.zero_check_on)
            else:
                await _run_in_thread(HUB.k6485.zero_check_off)
            zc_state_html.set_content(
                f'<span class="state">{"on" if on else "off"}</span>'
            )
            log.info("k6485 zero check %s", "on" if on else "off")
        except Exception as e:
            zc_sw.set_value(not on)
            ui.notify(f"zero-check FAIL: {type(e).__name__}: {e}",
                      type="negative", position="top", timeout=4000)
    zc_sw.on_value_change(_on_zc)

    async def do_zero_correct():
        if HUB.k6485 is None:
            ui.notify("k6485 not connected", type="warning",
                      position="top", timeout=2000); return
        try:
            await _run_in_thread(HUB.k6485.zero_correct)
            read_meta.set_content("zero correction captured")
            log.info("k6485 zero correct applied")
        except Exception as e:
            ui.notify(f"zero correct FAIL: {type(e).__name__}: {e}",
                      type="negative", position="top", timeout=4000)
    zero_correct_btn.on_click(do_zero_correct)

    # ---- Periodic refresh: connect strip ----
    def tick():
        if HUB.k6485 is None:
            connstrip.classes(remove="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Not connected — Keithley 6485 picoammeter</span>'
            )
            connect_btn.set_visibility(True)
        else:
            connstrip.classes(add="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Connected — Keithley 6485 picoammeter</span>'
            )
            connect_btn.set_visibility(False)
    tick()
    ui.timer(2.0, tick)


def _build_wfg_tab():
    from dg1022.gui import build_page as dg_build_page
    _embedded_instr_tab(
        "Not connected — Rigol DG1022 waveform generator.",
        getter=lambda: HUB.wfg, build_fn=dg_build_page,
        instrument_key="wfg",
    )


def _build_ks33500b_tab():
    """Keysight 33500B front panel — the visible WFG in this shell."""
    from ks33500b.gui import build_page as ks_build_page
    _embedded_instr_tab(
        "Not connected — Keysight 33500B waveform generator.",
        getter=lambda: HUB.ks33500b, build_fn=ks_build_page,
        instrument_key="ks33500b",
    )


def _build_nge_tab():
    """R&S NGE103 power supply (MUX rail) front panel.

    Layout:
        [connect strip]
        [Master card: All outputs toggle (danger=red when ON) + refresh]
        [Channel grid (auto-fit): one card per channel]
          [Header: 'channel N' + output toggle (danger=red when ON)]
          [Readback: measured V (big mono) + measured I (smaller mono) —
                     red border when output is live]
          [V setpoint + I limit fields with unit suffixes]
          [Per-channel Apply (accent when dirty)]

    Output toggles follow the app-wide convention: red when energised,
    neutral when off. Set fields use the accent-blue dirty marker until
    Apply commits both V setpoint and I limit.
    """
    N_CHANNELS = 3  # NGE103 = 3 channels

    # Per-channel UI state we need to mutate in handlers
    ch_widgets: dict = {}      # ch -> dict of element refs

    with ui.element("div").classes("psu-panel w-full"):

        # ---- Connect strip ----
        with ui.row().classes("connstrip w-full") as connstrip:
            ui.html('<span class="dot"></span>')
            conn_lbl = ui.html(
                '<span class="lbl">Not connected — R&amp;S NGE103 power supply (MUX rail)</span>'
            )
            async def _do_connect():
                ok = await _quick_connect("nge100")
                ui.notify("nge100: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense") \
                .style("margin-left:auto")

        # ---- Master card ----
        with ui.card().classes("card-psu w-full"):
            with ui.row().classes("master w-full"):
                ui.html('<p class="eyebrow">Master</p>')
                ch_count_html = ui.html('<span class="info">— channels</span>')
                with ui.row().classes("right items-center"):
                    with ui.row().classes("out-row items-center"):
                        ui.html('<span style="font-size:13px">All outputs</span>')
                        master_state_html = ui.html(
                            '<span class="state">off</span>'
                        )
                        master_sw = ui.switch(value=False) \
                            .props("dense color=negative") \
                            .classes("tgl-danger")
                    refresh_btn = ui.button("refresh").props("flat dense")

            async def _do_master_toggle(_e):
                if HUB.nge100 is None:
                    ui.notify("nge100 not connected", type="warning",
                              position="top", timeout=2000)
                    master_sw.set_value(not bool(_e.value))
                    return
                on = bool(_e.value)
                try:
                    if on:
                        await _run_in_thread(HUB.nge100.all_outputs_on)
                    else:
                        await _run_in_thread(HUB.nge100.all_outputs_off)
                    log.info("nge100 master output %s", "on" if on else "off")
                    _refresh_all()
                except Exception as e:
                    master_sw.set_value(not on)
                    ui.notify(f"master toggle FAIL: {type(e).__name__}: {e}",
                              type="negative", position="top", timeout=4000)
            master_sw.on_value_change(_do_master_toggle)

        # ---- Channel grid ----
        with ui.element("div").classes("chgrid w-full"):
            for ch in range(1, N_CHANNELS + 1):
                with ui.card().classes("card-psu"):
                    # Header: title + per-channel output toggle
                    with ui.row().classes("ch-head w-full"):
                        ui.html(f'<span class="title">channel {ch}</span>')
                        with ui.row().classes("out-row items-center"):
                            out_state_html = ui.html(
                                '<span class="state">off</span>'
                            )
                            out_sw = ui.switch(value=False) \
                                .props("dense color=negative") \
                                .classes("tgl-danger")

                    # Readback
                    with ui.element("div").classes("readback") as readback_el:
                        with ui.element("div").classes("row"):
                            ui.html('<span class="lab">measured V</span>')
                            vm_html = ui.html(
                                '<span><span class="val">—</span>'
                                '<span class="u">V</span></span>'
                            )
                        with ui.element("div").classes("row"):
                            ui.html('<span class="lab">measured I</span>')
                            im_html = ui.html(
                                '<span><span class="val small">—</span>'
                                '<span class="u">A</span></span>'
                            )

                    # V setpoint
                    with ui.element("div").classes("fld") \
                            .style("margin-bottom:12px"):
                        ui.html('<label class="fld-lbl">V setpoint</label>')
                        vset_n = ui.number(value=5.000, step=0.05, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="V"')

                    # I limit
                    with ui.element("div").classes("fld"):
                        ui.html('<label class="fld-lbl">I limit</label>')
                        ilim_n = ui.number(value=0.500, step=0.01, format="%.3f") \
                            .props('dense filled hide-bottom-space suffix="A"')

                    # Apply button (accent when dirty)
                    with ui.row().classes("ch-foot w-full"):
                        apply_ch_btn = ui.button("apply") \
                            .props("flat dense").classes("apply-btn")

                    ch_widgets[ch] = {
                        "out_sw":          out_sw,
                        "out_state_html":  out_state_html,
                        "readback_el":     readback_el,
                        "vm_html":         vm_html,
                        "im_html":         im_html,
                        "vset_n":          vset_n,
                        "ilim_n":          ilim_n,
                        "apply_btn":       apply_ch_btn,
                    }

                    # Wire per-channel handlers
                    def _make_out_handler(c=ch):
                        async def _on_out(_e):
                            if HUB.nge100 is None:
                                ui.notify("nge100 not connected", type="warning",
                                          position="top", timeout=2000)
                                ch_widgets[c]["out_sw"].set_value(
                                    not bool(_e.value)
                                ); return
                            on = bool(_e.value)
                            try:
                                if on:
                                    await _run_in_thread(HUB.nge100.output_on, c)
                                else:
                                    await _run_in_thread(HUB.nge100.output_off, c)
                                log.info("nge100 ch%d output %s",
                                         c, "on" if on else "off")
                                _refresh_channel(c)
                                _refresh_master_state()
                            except Exception as e:
                                ch_widgets[c]["out_sw"].set_value(not on)
                                ui.notify(
                                    f"ch{c} toggle FAIL: {type(e).__name__}: {e}",
                                    type="negative", position="top", timeout=4000,
                                )
                        return _on_out
                    out_sw.on_value_change(_make_out_handler(ch))

                    def _make_dirty(c=ch):
                        return lambda _e: ch_widgets[c]["apply_btn"] \
                            .classes(add="is-dirty")
                    vset_n.on_value_change(_make_dirty(ch))
                    ilim_n.on_value_change(_make_dirty(ch))

                    def _make_apply(c=ch):
                        async def _do_apply():
                            if HUB.nge100 is None:
                                ui.notify("nge100 not connected",
                                          type="warning",
                                          position="top", timeout=2000); return
                            w = ch_widgets[c]
                            try:
                                v = float(w["vset_n"].value or 0)
                                i = float(w["ilim_n"].value or 0)
                                await _run_in_thread(
                                    HUB.nge100.apply, c, v, i,
                                )
                                w["apply_btn"].classes(remove="is-dirty")
                                log.info("nge100 ch%d applied · V=%.3f I=%.3f",
                                         c, v, i)
                                ui.notify(
                                    f"ch{c} · V={v:.3f} V · I={i:.3f} A",
                                    type="positive", position="top", timeout=2000,
                                )
                                _refresh_channel(c)
                            except Exception as e:
                                ui.notify(
                                    f"ch{c} apply FAIL: {type(e).__name__}: {e}",
                                    type="negative", position="top", timeout=4000,
                                )
                        return _do_apply
                    apply_ch_btn.on_click(_make_apply(ch))

    # ===== Refresh helpers =====

    def _refresh_channel(ch: int):
        """Pull measured V/I + output state from the PSU and update the UI."""
        w = ch_widgets[ch]
        if HUB.nge100 is None:
            w["out_state_html"].set_content(
                '<span class="state">—</span>'
            )
            w["vm_html"].set_content(
                '<span><span class="val">—</span><span class="u">V</span></span>'
            )
            w["im_html"].set_content(
                '<span><span class="val small">—</span><span class="u">A</span></span>'
            )
            w["readback_el"].classes(remove="is-live")
            return
        try:
            is_on = bool(HUB.nge100.is_output_on(ch))
        except Exception:
            is_on = False
        try:
            v = HUB.nge100.measure_voltage(ch)
        except Exception:
            v = None
        try:
            i = HUB.nge100.measure_current(ch)
        except Exception:
            i = None
        # Update toggle state (without firing the change handler again)
        # if needed
        if bool(w["out_sw"].value) != is_on:
            w["out_sw"].value = is_on  # set directly to avoid loops
        w["out_state_html"].set_content(
            f'<span class="state{" is-on" if is_on else ""}">'
            f'{"on" if is_on else "off"}</span>'
        )
        if is_on:
            w["readback_el"].classes(add="is-live")
        else:
            w["readback_el"].classes(remove="is-live")
        v_txt = f"{v:.3f}" if isinstance(v, (int, float)) else "—"
        i_txt = f"{i:.3f}" if isinstance(i, (int, float)) else "—"
        w["vm_html"].set_content(
            f'<span><span class="val">{v_txt}</span><span class="u">V</span></span>'
        )
        w["im_html"].set_content(
            f'<span><span class="val small">{i_txt}</span><span class="u">A</span></span>'
        )

    def _refresh_master_state():
        on_count = 0
        if HUB.nge100 is not None:
            for ch in range(1, N_CHANNELS + 1):
                try:
                    if HUB.nge100.is_output_on(ch):
                        on_count += 1
                except Exception:
                    pass
        if on_count == N_CHANNELS:
            txt, on = "all on", True
        elif on_count == 0:
            txt, on = "off", False
        else:
            txt, on = f"{on_count} of {N_CHANNELS} on", True
        master_state_html.set_content(
            f'<span class="state{" is-on" if on_count > 0 else ""}">{txt}</span>'
        )
        if bool(master_sw.value) != on:
            master_sw.value = on

    def _refresh_all():
        for ch in range(1, N_CHANNELS + 1):
            _refresh_channel(ch)
        _refresh_master_state()

    refresh_btn.on_click(_refresh_all)

    # ---- Connection refresh (2 s tick) ----
    def tick():
        if HUB.nge100 is None:
            connstrip.classes(remove="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Not connected — R&amp;S NGE103 power supply (MUX rail)</span>'
            )
            connect_btn.set_visibility(True)
            ch_count_html.set_content('<span class="info">— channels</span>')
        else:
            connstrip.classes(add="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Connected — R&amp;S NGE103 power supply (MUX rail)</span>'
            )
            connect_btn.set_visibility(False)
            try:
                n = HUB.nge100.num_channels()
            except Exception:
                n = N_CHANNELS
            ch_count_html.set_content(f'<span class="info">{n} channels</span>')
        _refresh_all()

    tick()
    ui.timer(2.0, tick)


def _build_webcam_tab():
    """Embed the USB webcam (Logitech C525 on /dev/video0).

    The capture runs in a background thread shared across all viewers;
    multiple browser tabs cost nothing extra on the camera side.
    """
    from daq.webgui import webcam as wc
    wc.build_page()


def _build_data_tab():
    """HDF5 data explorer.

    Left: every ``*.h5`` under ./data (recursively — bench/elec runs at the
    top level, L1/L2 measurements in per-SiPM/per-T subfolders), newest
    first, with a name filter. Right: the selected file's group/dataset tree
    (``ui.tree``) and a detail pane that shows a node's attributes, a value
    preview + numeric stats, and a quick plot for 1D/2D numeric datasets.
    Introspection lives in :mod:`daq.h5browse` (pure, no GUI deps).
    """
    import numpy as np
    from pathlib import Path
    from daq import h5browse as HB
    from daq import plotting as P

    ui.label(
        "Browse every recorded .h5 under ./data. Pick a file for its "
        "group/dataset tree; click a node for attributes, a value preview, "
        "and a quick plot of numeric datasets."
    ).classes("text-gray-400 text-sm")

    state = {"path": None, "files": []}

    with ui.row().classes("w-full gap-3 no-wrap items-start"):
        # ---- left: file browser ----
        with ui.card().classes("daq-card").style("min-width:330px; max-width:360px"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.html("<h2>files</h2>")
                count_lbl = ui.label("").classes("text-gray-400 text-xs")
            flt = ui.input(placeholder="filter by name...") \
                .props("dense clearable").classes("w-full")
            ui.button("refresh", icon="refresh",
                      on_click=lambda: refresh_files()).props("flat dense no-caps")
            file_list = ui.column().classes("w-full gap-1") \
                .style("max-height:62vh; overflow-y:auto")

        # ---- right: structure + detail ----
        with ui.column().classes("flex-grow gap-3").style("min-width:0"):
            with ui.card().classes("daq-card w-full"):
                file_hdr = ui.row().classes("items-center gap-3 w-full")
                with file_hdr:
                    ui.html("<h2>structure</h2>")
                    hdr_info = ui.label("no file selected") \
                        .classes("text-gray-400 text-sm")
                    ui.space()
                    dl_btn = ui.button("download", icon="download") \
                        .props("flat dense no-caps")
                    dl_btn.set_visibility(False)
                tree_box = ui.column().classes("w-full") \
                    .style("max-height:42vh; overflow:auto")
                with tree_box:
                    ui.label("select a file on the left").classes(
                        "text-gray-500 text-sm")
            with ui.card().classes("daq-card w-full"):
                ui.html("<h2>detail</h2>")
                detail_box = ui.column().classes("w-full")
                with detail_box:
                    ui.label("select a node in the tree").classes(
                        "text-gray-500 text-sm")

    def _download():
        if state["path"]:
            ui.download.file(state["path"], Path(state["path"]).name)
    dl_btn.on_click(_download)

    def show_detail(h5path: str | None):
        detail_box.clear()
        with detail_box:
            if not state["path"] or not h5path:
                ui.label("select a node in the tree").classes(
                    "text-gray-500 text-sm")
                return
            try:
                info = HB.node_detail(state["path"], h5path)
            except Exception as e:
                ui.label(f"error: {type(e).__name__}: {e}").classes("text-red-400")
                return

            ui.html(f"<code>{h5path}</code> &middot; <b>{info['kind']}</b>"
                    + (f" &middot; {info['n_children']} child(ren)"
                       if info["kind"] == "group" else ""))

            if info["attrs"]:
                ui.table(
                    columns=[{"name": "k", "label": "attribute", "field": "k",
                              "align": "left"},
                             {"name": "v", "label": "value", "field": "v",
                              "align": "left"}],
                    rows=[{"k": k, "v": str(v)} for k, v in info["attrs"]],
                    row_key="k",
                ).props("dense flat").classes("w-full")
            else:
                ui.label("no attributes").classes("text-gray-500 text-xs")

            if info["kind"] != "dataset":
                return

            ui.html(f"shape <code>{info['shape']}</code> &middot; "
                    f"dtype <code>{info['dtype']}</code>")
            s = info.get("stats")
            if s and s.get("finite"):
                ui.html(
                    f"min <code>{s['min']:.4g}</code> &middot; "
                    f"max <code>{s['max']:.4g}</code> &middot; "
                    f"mean <code>{s['mean']:.4g}</code> &middot; "
                    f"std <code>{s['std']:.4g}</code> &middot; "
                    f"n <code>{s['n']}</code>"
                    + (f" (finite {s['finite']})" if s['finite'] != s['n'] else "")
                ).classes("text-gray-300 text-sm")

            ui.label("preview").classes("text-gray-400 text-xs mt-1")
            ui.code(info["preview"]).classes("w-full")

            if not info["plottable"]:
                return

            plot = ui.matplotlib(figsize=(8.5, 3.2)).classes("w-full")
            axp = plot.figure.add_subplot(111)
            P.apply_dark_style(plot.figure, axp)
            is_2d = info["ndim"] == 2
            row_in = ui.number(label="row", value=0, min=0,
                               max=max(0, info["shape"][0] - 1), step=1) \
                .classes("w-28 num")
            row_in.set_visibility(is_2d)

            def do_plot(_=None, hp=h5path):
                row = int(row_in.value) if is_2d else None
                try:
                    y = HB.read_dataset(state["path"], hp, row=row).ravel()
                except Exception as e:
                    ui.notify(f"plot failed: {e}", type="negative")
                    return
                axp.clear()
                P.apply_dark_style(plot.figure, axp)
                axp.plot(y, lw=0.8)
                title = hp + (f"  row {row}" if is_2d else "")
                axp.set_title(title, fontsize=9)
                plot.figure.tight_layout()
                plot.update()

            with ui.row().classes("items-center gap-2"):
                ui.button("plot", icon="show_chart", on_click=do_plot) \
                    .props("dense color=primary no-caps")
                if is_2d:
                    ui.label(f"of {info['shape'][0]} rows").classes(
                        "text-gray-500 text-xs")
            do_plot()

    def load_file(path: str):
        state["path"] = path
        hdr_info.set_text(Path(path).name)
        dl_btn.set_visibility(True)
        tree_box.clear()
        detail_box.clear()
        with detail_box:
            ui.label("select a node in the tree").classes(
                "text-gray-500 text-sm")
        with tree_box:
            try:
                nodes = HB.build_tree(path)
            except Exception as e:
                ui.label(f"cannot open: {type(e).__name__}: {e}").classes(
                    "text-red-400")
                return
            ui.tree(nodes, on_select=lambda e: show_detail(e.value)) \
                .expand(["/"])

    def refresh_files():
        files = HB.list_data_files()
        state["files"] = files
        render_file_list()

    def render_file_list():
        q = (flt.value or "").strip().lower()
        files = [f for f in state["files"] if q in f["rel"].lower()]
        count_lbl.set_text(f"{len(files)}/{len(state['files'])}")
        file_list.clear()
        with file_list:
            if not files:
                ui.label("no matching .h5 files").classes(
                    "text-gray-500 text-sm")
            for f in files:
                meta = (f"{HB.human_size(f['size'])} &middot; "
                        f"{time.strftime('%m-%d %H:%M', time.localtime(f['mtime']))}")
                with ui.element("div").classes("data-file-row").on(
                        "click", lambda _e=None, p=f["path"]: load_file(p)):
                    ui.html(f"<div class='df-name'>{f['rel']}</div>"
                            f"<div class='df-meta'>{meta}</div>")

    flt.on("update:model-value", lambda _e: render_file_list())
    refresh_files()


def _build_plots_tab():
    """Render saved bench HDF5 files using daq.plotting.

    Two source modes:
      - "live": newest data/bench_*.h5 (the most recent run)
      - "pick": choose an explicit file path from a dropdown of data/*.h5
    Plus optional second file for overlay across runs (multi-SiPM comparison).
    """
    from pathlib import Path
    from daq import plotting as P

    ui.label(
        "Pick a plot type and a source file. Toggle 'live' to plot the most "
        "recent bench_*.h5 in ./data. Add a second file (and labels) to "
        "overlay two runs for comparison."
    ).classes("text-gray-400 text-sm")

    def _list_files():
        d = Path(__file__).resolve().parents[2] / "data"
        if not d.is_dir(): return []
        return [str(p) for p in sorted(d.glob("bench_*.h5"),
                                        key=lambda p: p.stat().st_mtime,
                                        reverse=True)]

    files_now = _list_files()
    plot_choices = list(P.PLOTS.keys())

    with ui.card().classes("daq-card w-full"):
        ui.html("<h2>plot selection</h2>")
        with ui.row().classes("items-center gap-3 flex-wrap"):
            plot_type = ui.select(plot_choices, value=plot_choices[0],
                                  label="plot type").classes("w-48")
            live      = ui.switch("live (newest file)", value=True)
            file_a    = ui.select(files_now, value=(files_now[0] if files_now else None),
                                  label="file A").classes("w-96")
            label_a   = ui.input(label="label A", value="").classes("w-40")
            file_b    = ui.select([""] + files_now, value="",
                                  label="file B (overlay, optional)").classes("w-96")
            label_b   = ui.input(label="label B", value="").classes("w-40")
        file_a.bind_visibility_from(live, "value", lambda v: not v)
        file_b.bind_visibility_from(live, "value", lambda v: not v)

    with ui.card().classes("daq-card w-full"):
        ui.html("<h2>plot knobs</h2>")
        with ui.row().classes("items-center gap-3 flex-wrap"):
            channel = ui.number(label="channel", value=0, step=1).classes("w-24 num")
            index   = ui.number(label="waveform #", value=0, step=1).classes("w-32 num")
            bins    = ui.number(label="bins", value=80, step=10).classes("w-24 num")
            bias_g  = ui.select(["above_vbd", "below_vbd"], value="above_vbd",
                                label="bias group").classes("w-40")
            log_y   = ui.switch("log Y", value=False)
            base_sub= ui.switch("baseline subtract", value=True)

    msg_log = ui.log(max_lines=10).classes("h-24 w-full")
    def log_msg(s: str):
        msg_log.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    # Matplotlib canvas — the plot library draws into this Axes
    plot = ui.matplotlib(figsize=(9, 4.0)).classes("w-full")
    ax   = plot.figure.add_subplot(111)
    P.apply_dark_style(plot.figure, ax)

    def refresh_files():
        # Re-scan data/ in case new HDF5s appeared
        files = _list_files()
        file_a.options = files
        file_b.options = [""] + files
        if not file_a.value and files:
            file_a.value = files[0]
        file_a.update()
        file_b.update()
        log_msg(f"found {len(files)} bench HDF5 file(s)")

    def render():
        try:
            fn = P.PLOTS[str(plot_type.value)]["fn"]
        except KeyError:
            log_msg(f"unknown plot type {plot_type.value!r}"); return

        sources: list[tuple[str, str]] = []
        if live.value:
            latest = P.find_latest()
            if latest is None:
                log_msg("no bench_*.h5 in ./data"); return
            sources.append((label_a.value or latest.stem, str(latest)))
        else:
            if not file_a.value:
                log_msg("no file A selected"); return
            sources.append((label_a.value or Path(file_a.value).stem, file_a.value))
            if file_b.value:
                sources.append((label_b.value or Path(file_b.value).stem, file_b.value))

        opts = dict(
            channel=int(channel.value),
            index=int(index.value),
            bins=int(bins.value),
            bias_group=str(bias_g.value),
            log_y=bool(log_y.value),
            baseline_subtract=bool(base_sub.value),
        )

        ax.clear()
        P.apply_dark_style(plot.figure, ax)
        try:
            if len(sources) == 1:
                lbl, src = sources[0]
                fn(src, ax=ax, label=lbl, **opts)
            else:
                P.overlay_plots(fn, sources, ax=ax, **opts)
            plot.figure.tight_layout()
            plot.update()
            log_msg(f"rendered {plot_type.value} ({len(sources)} source(s))")
        except Exception as e:
            log_msg(f"plot FAIL: {type(e).__name__}: {e}")

    with ui.row().classes("gap-2 mt-2"):
        ui.button("render", on_click=render).props("color=primary")
        ui.button("refresh file list", on_click=refresh_files)


def _build_digitizer_tab():
    """CAEN VX2740 digitizer panel — Quick I/V style.

    Four sub-tabs inside one card area:
        CHANNELS    — 64-channel table with enable + per-ch threshold
                       (or global threshold when threshold-mode = global)
        ACQUISITION — capture window (pre/post µs) + sample rate + trigger
                       + run-acquisition / SW trigger / store-raw toggle
        WAVEFORMS   — single-waveform viewer with channel + index controls
        SPECTRUM    — pulse-amplitude histogram with bin-count control

    All controls go through the VX2740Controller stored on HUB.dig. The
    backend wrapper for the dig is a `_VX2740Backend` that exposes the
    underlying controller as `._ctrl`; we use that everywhere.
    """
    import numpy as _np

    cfg_type = (HUB.config.digitizer_type or "").lower()
    if cfg_type != "vx2740":
        ui.label(
            f"This tab is built for the VX2740 control surface. "
            f"The configured digitizer_type is {cfg_type!r}; switch to "
            f"'vx2740' in the Config tab to enable this panel."
        ).classes("text-gray-400 text-sm")
        return

    def get_vx_controller():
        backend = HUB.dig
        if backend is None:
            return None
        return getattr(backend, "_ctrl", None)

    # VX2740 constants (mirror the driver)
    N_CHANNELS = 64
    SAMPLE_RATE_HZ = 125e6  # MS/s
    SIPM_CHANNELS = {0, 1, 2, 3}
    PMT_CHANNEL = 4

    def _ch_tag(ch: int) -> str:
        if ch in SIPM_CHANNELS: return "SiPM"
        if ch == PMT_CHANNEL: return "PMT"
        return ""

    # ----- state -----
    # Staged per-channel enable and threshold (counts). Initialised from
    # the controller on first refresh.
    _ch_state = {
        ch: {"enable": ch in (SIPM_CHANNELS | {PMT_CHANNEL}),
             "threshold": 150}
        for ch in range(N_CHANNELS)
    }
    _last_result = {"r": None}

    # ===== Panel scaffold =====
    with ui.element("div").classes("dig-panel w-full"):

        # ----- Connect strip -----
        with ui.row().classes("connstrip w-full") as connstrip:
            ui.html('<span class="dot"></span>')
            conn_lbl = ui.html(
                '<span class="lbl">Not connected — CAEN VX2740 digitizer</span>'
            )
            async def _do_connect():
                ok = await _quick_connect("dig")
                ui.notify("digitizer: " + ("connected" if ok else "connect failed"),
                          type="positive" if ok else "negative",
                          position="top", timeout=2500)
            connect_btn = ui.button("connect", on_click=_do_connect) \
                .props("color=primary dense") \
                .style("margin-left:auto")

        # ----- Sub-tab strip -----
        with ui.row().classes("subtabs w-full"):
            tab_channels  = ui.button("channels").props("flat dense no-caps") \
                .classes("subtab")
            tab_acq       = ui.button("acquisition").props("flat dense no-caps") \
                .classes("subtab")
            tab_waveforms = ui.button("waveforms").props("flat dense no-caps") \
                .classes("subtab")
            tab_spectrum  = ui.button("spectrum").props("flat dense no-caps") \
                .classes("subtab")

        # ----- Containers for each panel -----
        panel_channels  = ui.element("div").classes("dpanel is-active")
        panel_acq       = ui.element("div").classes("dpanel")
        panel_waveforms = ui.element("div").classes("dpanel")
        panel_spectrum  = ui.element("div").classes("dpanel")

        def _show(which: str):
            for p in (panel_channels, panel_acq,
                      panel_waveforms, panel_spectrum):
                p.classes(remove="is-active")
            for b in (tab_channels, tab_acq,
                      tab_waveforms, tab_spectrum):
                b.classes(remove="is-active")
            if which == "channels":
                panel_channels.classes(add="is-active")
                tab_channels.classes(add="is-active")
            elif which == "acq":
                panel_acq.classes(add="is-active")
                tab_acq.classes(add="is-active")
            elif which == "wave":
                panel_waveforms.classes(add="is-active")
                tab_waveforms.classes(add="is-active")
            elif which == "spec":
                panel_spectrum.classes(add="is-active")
                tab_spectrum.classes(add="is-active")

        tab_channels.on_click(lambda: _show("channels"))
        tab_acq.on_click(lambda: _show("acq"))
        tab_waveforms.on_click(lambda: _show("wave"))
        tab_spectrum.on_click(lambda: _show("spec"))

        # ==========================================================
        # CHANNELS panel
        # ==========================================================
        with panel_channels:
            with ui.card().classes("card-dig").style("max-width:880px; margin:0 auto;"):
                # Header row: title + threshold-mode dropdown
                with ui.row().classes("items-center w-full") \
                        .style("justify-content:space-between; gap:16px; margin-bottom:14px;"):
                    with ui.element("div"):
                        ui.html('<p class="eyebrow" style="margin:0 0 4px">Channels</p>')
                        ui.html('<div style="font-size:15px">'
                                + f'{N_CHANNELS} channels · SiPM ch0–3 · PMT ch4'
                                + '</div>')
                    with ui.element("div").classes("fld").style("min-width:180px"):
                        ui.html('<label class="fld-lbl">Threshold mode</label>')
                        th_mode = ui.select(
                            {"per_channel": "per channel", "global": "global"},
                            value="per_channel",
                        ).props("dense filled hide-bottom-space")

                # Global threshold (visible only when mode = global)
                global_th_wrap = ui.element("div") \
                    .style("display:none; margin-bottom:14px")
                with global_th_wrap:
                    with ui.element("div").classes("fld") \
                            .style("max-width:220px"):
                        ui.html('<label class="fld-lbl">Global threshold</label>')
                        global_th_n = ui.number(value=150, step=1, format="%d") \
                            .props('dense filled hide-bottom-space suffix="ADC"')

                # The channel table
                channel_rows_container = ui.element("div").classes("w-full") \
                    .style("max-height:560px; overflow-y:auto;")

                # Bottom apply
                with ui.row().classes("w-full").style("justify-content:flex-end; margin-top:14px"):
                    apply_ch_btn = ui.button("apply").props("dense") \
                        .classes("apply-btn")

        # Render the channel table (called on every dirty/mode change)
        ch_enable_switches: dict = {}
        ch_threshold_nums: dict = {}

        def _set_dirty():
            apply_ch_btn.classes(add="is-dirty")

        # Per-channel UI element refs so toggle handlers can flip their
        # CSS classes without re-rendering the whole grid (which is
        # heavy for 64 channels).
        ch_label_els: dict = {}
        ch_thresh_wraps: dict = {}

        def _apply_cell_state(ch: int):
            state = _ch_state[ch]
            on = state["enable"]
            cls = "ch-toggle"
            if ch == PMT_CHANNEL:
                cls += " is-pmt"
            if on:
                cls += " is-on"
            lab = ch_label_els.get(ch)
            if lab is not None:
                lab.classes(replace=cls)
            wrap = ch_thresh_wraps.get(ch)
            if wrap is not None:
                if on:
                    wrap.classes(remove="is-off")
                else:
                    wrap.classes(add="is-off")

        def _render_channels():
            channel_rows_container.clear()
            ch_threshold_nums.clear()
            ch_label_els.clear()
            ch_thresh_wraps.clear()
            per_ch = (th_mode.value == "per_channel")

            with channel_rows_container:
                ui.html('<div class="ch-grid-header">'
                        f'{N_CHANNELS} channels · click a channel label to '
                        'enable / disable · threshold edits per cell</div>')

                with ui.element("div").classes("ch-grid w-full"):
                    for ch in range(N_CHANNELS):
                        state = _ch_state[ch]
                        with ui.element("div").classes("ch-cell"):
                            # Channel label is just the number, e.g. "ch4".
                            # SiPM / PMT roles are still tracked internally
                            # (PMT cell uses a different accent color), and
                            # the tooltip still shows the role for context.
                            tag = _ch_tag(ch)
                            cls = "ch-toggle"
                            if ch == PMT_CHANNEL:
                                cls += " is-pmt"
                            if state["enable"]:
                                cls += " is-on"
                            label_el = ui.html(
                                f'<span title="ch{ch}'
                                + (f' · {tag}' if tag else '')
                                + f'">ch{ch}</span>'
                            ).classes(cls)
                            ch_label_els[ch] = label_el

                            def _flip(_e=None, c=ch):
                                _ch_state[c]["enable"] = not _ch_state[c]["enable"]
                                _apply_cell_state(c)
                                _set_dirty()
                            label_el.on("click", _flip)

                            if per_ch:
                                thr_wrap = ui.element("div").classes(
                                    "ch-thresh"
                                    + ("" if state["enable"] else " is-off")
                                )
                                with thr_wrap:
                                    n = ui.number(value=state["threshold"],
                                                  step=1, format="%d") \
                                        .props('dense filled hide-bottom-space')
                                    n.on_value_change(
                                        lambda _e, c=ch: (
                                            _ch_state[c].__setitem__(
                                                "threshold", int(_e.value or 0)),
                                            _set_dirty(),
                                        )
                                    )
                                    ch_threshold_nums[ch] = n
                                ch_thresh_wraps[ch] = thr_wrap

        def _on_mode_change(_e=None):
            per_ch = (th_mode.value == "per_channel")
            global_th_wrap.style(
                "display:none; margin-bottom:14px" if per_ch
                else "display:block; margin-bottom:14px"
            )
            _set_dirty()
            _render_channels()
        th_mode.on_value_change(_on_mode_change)
        global_th_n.on_value_change(lambda _e: _set_dirty())
        _render_channels()

        async def _apply_channels():
            ctrl = get_vx_controller()
            if ctrl is None:
                ui.notify("digitizer not connected",
                          type="warning", position="top", timeout=2500)
                return
            try:
                # The 64-channel grid treats ch 4 as just another channel.
                # Pass every enabled index (incl. PMT_CHANNEL) in
                # sipm_channels so its data actually lands in the result —
                # `include_pmt=True` adds ch 4 to the controller's enable
                # list but the result loop iterates over `_sipm_channels`
                # only, so PMT-routed channels never make it into the
                # captured waveforms/amplitudes dicts.
                enabled_chs = sorted(
                    ch for ch in range(N_CHANNELS)
                    if _ch_state[ch]["enable"]
                )
                thresholds = {ch: int(_ch_state[ch]["threshold"])
                              for ch in enabled_chs}
                await _run_in_thread(
                    ctrl.configure_channels,
                    sipm_channels=enabled_chs,
                    threshold_mode=str(th_mode.value),
                    global_threshold=int(global_th_n.value or 150),
                    thresholds=thresholds,
                    include_pmt=False,
                )
                apply_ch_btn.classes(remove="is-dirty")
                ui.notify(
                    f"channels applied · {len(enabled_chs)} channel(s) "
                    f"· mode={th_mode.value}",
                    type="positive", position="top", timeout=2500,
                )
            except Exception as e:
                ui.notify(f"channels apply FAIL: {type(e).__name__}: {e}",
                          type="negative", position="top", timeout=4500)
        apply_ch_btn.on_click(_apply_channels)

        # ==========================================================
        # ACQUISITION panel
        # ==========================================================
        with panel_acq:
            with ui.element("div").classes("grid w-full") \
                    .style("display:grid; grid-template-columns:1.2fr 1fr; "
                           "gap:14px; max-width:1100px; margin:0 auto"):

                # ----- Capture window & trigger card -----
                with ui.card().classes("card-dig"):
                    ui.html('<p class="eyebrow">Capture window &amp; trigger</p>')
                    with ui.element("div").classes("grid") \
                            .style("display:grid; grid-template-columns:1fr 1fr; gap:14px"):
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">Pre-trigger</label>')
                            pre_n = ui.number(value=2.0, step=0.5, format="%.2f") \
                                .props('dense filled hide-bottom-space suffix="µs"')
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">Post-trigger</label>')
                            post_n = ui.number(value=10.0, step=0.5, format="%.2f") \
                                .props('dense filled hide-bottom-space suffix="µs"')
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">Trigger source</label>')
                            trig_sel = ui.select(
                                {"self": "self (level)",
                                 "external": "external",
                                 "software": "software"},
                                value="self",
                            ).props("dense filled hide-bottom-space")
                        with ui.element("div").classes("fld"):
                            ui.html('<label class="fld-lbl">Sample rate</label>')
                            ui.html('<div class="ip" style="height:36px; '
                                    'background:var(--panel2); border:1px solid var(--line); '
                                    'border-radius:8px; padding:0 12px; display:flex; '
                                    'align-items:center; font-family:ui-monospace; '
                                    'font-size:14px; color:var(--mut)">'
                                    '125 MS/s <span style="color:var(--mut); '
                                    'opacity:.7; margin-left:auto; font-size:12px">fixed</span>'
                                    '</div>')

                    samples_html = ui.html(
                        '<div class="derived">— samples</div>'
                    )

                    def _recalc_samples():
                        try:
                            total_us = float(pre_n.value or 0) + float(post_n.value or 0)
                        except (ValueError, TypeError):
                            total_us = 0
                        n = int(round(total_us * SAMPLE_RATE_HZ * 1e-6))
                        samples_html.set_content(
                            f'<div class="derived">{n} samples total · '
                            f'{total_us:.2f} µs at 125 MS/s</div>'
                        )
                    pre_n.on_value_change(lambda _e: _recalc_samples())
                    post_n.on_value_change(lambda _e: _recalc_samples())
                    _recalc_samples()

                # ----- Run card -----
                with ui.card().classes("card-dig"):
                    ui.html('<p class="eyebrow">Run</p>')
                    with ui.element("div").classes("fld").style("margin-bottom:12px"):
                        ui.html('<label class="fld-lbl">Waveforms</label>')
                        n_wf_n = ui.number(value=1000, step=100, format="%d") \
                            .props('dense filled hide-bottom-space suffix="#"')
                    with ui.row().classes("items-center w-full") \
                            .style("gap:10px; margin-bottom:12px"):
                        store_raw_sw = ui.switch(value=False).props("dense")
                        ui.html('Store raw waveforms '
                                '<span style="color:var(--mut)">'
                                '(memory-heavy)</span>')
                    with ui.element("div").classes("fld") \
                            .style("max-width:160px; margin-bottom:14px"):
                        ui.html('<label class="fld-lbl">Timeout</label>')
                        timeout_n = ui.number(value=60, step=5, format="%.0f") \
                            .props('dense filled hide-bottom-space suffix="s"')
                    with ui.row().classes("items-center w-full") \
                            .style("gap:10px; flex-wrap:wrap"):
                        apply_acq_btn = ui.button("apply config") \
                            .props("flat dense")
                        run_btn = ui.button("▶ run acquisition") \
                            .props("color=primary")
                        # Stop button is disabled until a run is in
                        # flight (toggled by _run_acquisition).
                        stop_btn = ui.button("■ stop") \
                            .props("color=negative dense disable")
                        sw_trig_btn = ui.button("send SW trigger") \
                            .props("flat dense")
                        acq_status = ui.html(
                            '<span class="statuspill">ready</span>'
                        ).style("margin-left:auto")

                    async def _apply_acq():
                        ctrl = get_vx_controller()
                        if ctrl is None:
                            ui.notify("not connected", type="warning",
                                      position="top", timeout=2500); return
                        try:
                            await _run_in_thread(
                                ctrl.configure_record_window,
                                float(pre_n.value or 0),
                                float(post_n.value or 0),
                            )
                            mode = str(trig_sel.value)
                            await _run_in_thread(
                                ctrl.configure_trigger, mode,
                            )
                            ui.notify(
                                f"acquisition cfg applied · "
                                f"pre={pre_n.value} post={post_n.value} µs · "
                                f"trigger={mode}",
                                type="positive", position="top", timeout=2500,
                            )
                            apply_acq_btn.classes(remove="is-dirty")
                        except Exception as e:
                            ui.notify(
                                f"acq apply FAIL: {type(e).__name__}: {e}",
                                type="negative", position="top", timeout=4500,
                            )

                    async def _run_acquisition():
                        ctrl = get_vx_controller()
                        if ctrl is None:
                            ui.notify("not connected", type="warning",
                                      position="top", timeout=2500); return
                        n = int(n_wf_n.value or 0)
                        store = bool(store_raw_sw.value)
                        tmo = float(timeout_n.value or 60)
                        acq_status.set_content(
                            '<span class="statuspill" '
                            'style="color:var(--warn)">running…</span>'
                        )
                        run_btn.props("disable")
                        stop_btn.props(remove="disable")
                        set_activity("vx2740 acquisition",
                                     f"{n} waveforms · store={store}")
                        try:
                            await _run_in_thread(ctrl.arm)
                            result = await _run_in_thread(
                                ctrl.acquire, n,
                                1000, store, tmo,
                            )
                            await _run_in_thread(ctrl.disarm)
                            _last_result["r"] = result
                            acq_status.set_content(
                                f'<span class="statuspill" '
                                f'style="color:var(--ok)">done · '
                                f'{result.n_waveforms} wfs</span>'
                            )
                            log.info("vx2740 acquired %d waveforms",
                                     result.n_waveforms)
                            # Auto-jump the waveform / spectrum dropdowns
                            # to a channel that actually has data, so the
                            # user isn't stuck on "no waveforms — re-run"
                            # when their enabled channel isn't ch 0.
                            try:
                                _chs_with_waves = [
                                    ch for ch in result.channel_ids
                                    if result.waveforms.get(ch) is not None
                                    and len(result.waveforms[ch]) > 0
                                ]
                                _chs_with_amps = [
                                    ch for ch in result.channel_ids
                                    if len(result.amplitudes.get(ch, [])) > 0
                                ]
                                if (_chs_with_waves
                                        and int(wave_ch.value or 0) not in _chs_with_waves):
                                    wave_ch.set_value(_chs_with_waves[0])
                                if (_chs_with_amps
                                        and int(spec_ch.value or 0) not in _chs_with_amps):
                                    spec_ch.set_value(_chs_with_amps[0])
                            except Exception:
                                pass
                            # Refresh waveform / spectrum plots if visible
                            _draw_waveform()
                            _draw_spectrum()
                        except Exception as e:
                            acq_status.set_content(
                                f'<span class="statuspill" '
                                f'style="color:var(--bad)">failed</span>'
                            )
                            log.exception("vx2740 acquire failed: %s", e)
                            ui.notify(
                                f"acquire FAIL: {type(e).__name__}: {e}",
                                type="negative", position="top", timeout=4500,
                            )
                        finally:
                            clear_activity()
                            run_btn.props(remove="disable")
                            stop_btn.props("disable")

                    async def _stop_acquisition():
                        # Stop button: ask the driver to bail out of the
                        # read loop ASAP.  Calling disarm() flips
                        # driver._armed to False; the read loop checks
                        # that at every 100 ms poll boundary and breaks
                        # out, returning whatever was collected so far.
                        ctrl = get_vx_controller()
                        if ctrl is None: return
                        acq_status.set_content(
                            '<span class="statuspill" '
                            'style="color:var(--warn)">stopping…</span>'
                        )
                        try:
                            await _run_in_thread(ctrl.disarm)
                        except Exception as e:
                            log.warning("stop disarm raised: %s", e)

                    async def _sw_trig():
                        ctrl = get_vx_controller()
                        if ctrl is None:
                            ui.notify("not connected", type="warning",
                                      position="top", timeout=2500); return
                        try:
                            await _run_in_thread(ctrl.send_software_trigger)
                            acq_status.set_content(
                                '<span class="statuspill">SW trigger sent</span>'
                            )
                        except Exception as e:
                            ui.notify(
                                f"SW trigger FAIL: {type(e).__name__}: {e}",
                                type="negative", position="top", timeout=4000,
                            )

                    apply_acq_btn.on_click(_apply_acq)
                    run_btn.on_click(_run_acquisition)
                    stop_btn.on_click(_stop_acquisition)
                    sw_trig_btn.on_click(_sw_trig)

                    # Mark "apply" dirty on field changes
                    def _mark_acq_dirty(*_):
                        apply_acq_btn.classes(add="is-dirty")
                    for f in (pre_n, post_n, trig_sel):
                        f.on_value_change(_mark_acq_dirty)

        # ==========================================================
        # WAVEFORMS panel
        # ==========================================================
        with panel_waveforms:
            with ui.card().classes("card-dig"):
                with ui.row().classes("items-end w-full") \
                        .style("gap:14px; margin-bottom:14px"):
                    ui.html('<p class="eyebrow" style="margin:0">Waveform viewer</p>')
                    ui.html('<div style="flex:1"></div>')
                    with ui.element("div").classes("fld").style("width:160px"):
                        ui.html('<label class="fld-lbl">Channel</label>')
                        wave_ch = ui.select(
                            {ch: f"ch{ch}" for ch in range(N_CHANNELS)},
                            value=0,
                        ).props("dense filled hide-bottom-space")
                    with ui.element("div").classes("fld").style("width:130px"):
                        ui.html('<label class="fld-lbl">Waveform #</label>')
                        wave_n = ui.number(value=0, step=1, format="%d") \
                            .props("dense filled hide-bottom-space")
                    prev_btn = ui.button("◀ prev").props("flat dense")
                    next_btn = ui.button("next ▶").props("flat dense")

                wave_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 60, "right": 18, "top": 14, "bottom": 38},
                    "backgroundColor": "transparent",
                    "textStyle": {"color": "#dde3ee"},
                    "xAxis": {
                        "type": "value", "name": "time (µs)",
                        "nameLocation": "middle", "nameGap": 22, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "yAxis": {
                        "type": "value", "name": "ADC counts",
                        "nameLocation": "middle", "nameGap": 46, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "series": [{
                        "type": "line", "showSymbol": False,
                        "data": [],
                        "lineStyle": {"width": 1.4, "color": "#3b82f6"},
                    }],
                }).classes("plotbox-dig")

                def _draw_waveform():
                    r = _last_result["r"]
                    if r is None:
                        wave_chart.options["series"][0]["data"] = []
                        wave_chart.update(); return
                    ch = int(wave_ch.value or 0)
                    idx = int(wave_n.value or 0)
                    # `wfs` may be a numpy 2-D array (from the controller's
                    # post-loop consolidate step), an empty list (no
                    # waveforms stored for this channel), or None (channel
                    # wasn't enabled in the result).  Use `len() == 0` —
                    # `not wfs` triggers numpy's truth-value ambiguity.
                    wfs = r.waveforms.get(ch) if hasattr(r, "waveforms") else None
                    if wfs is None or len(wfs) == 0 or idx >= len(wfs):
                        wave_chart.options["series"][0]["data"] = []
                        wave_chart.update(); return
                    wf = wfs[idx]
                    # x = sample-index * (1/SAMPLE_RATE) in microseconds
                    dt_us = 1e6 / SAMPLE_RATE_HZ
                    data = [[i * dt_us, float(v)] for i, v in enumerate(wf)]
                    wave_chart.options["series"][0]["data"] = data
                    wave_chart.update()

                wave_ch.on_value_change(lambda _e: _draw_waveform())
                wave_n.on_value_change(lambda _e: _draw_waveform())
                prev_btn.on_click(lambda: (
                    wave_n.set_value(max(0, int(wave_n.value or 0) - 1)),
                    _draw_waveform(),
                ))
                next_btn.on_click(lambda: (
                    wave_n.set_value(int(wave_n.value or 0) + 1),
                    _draw_waveform(),
                ))

        # ==========================================================
        # SPECTRUM panel
        # ==========================================================
        with panel_spectrum:
            with ui.card().classes("card-dig"):
                with ui.row().classes("items-end w-full") \
                        .style("gap:14px; margin-bottom:14px"):
                    ui.html('<p class="eyebrow" style="margin:0">'
                            'Pulse-amplitude spectrum</p>')
                    ui.html('<div style="flex:1"></div>')
                    with ui.element("div").classes("fld").style("width:160px"):
                        ui.html('<label class="fld-lbl">Channel</label>')
                        spec_ch = ui.select(
                            {ch: f"ch{ch}" for ch in range(N_CHANNELS)},
                            value=0,
                        ).props("dense filled hide-bottom-space")
                    with ui.element("div").classes("fld").style("width:120px"):
                        ui.html('<label class="fld-lbl">Bins</label>')
                        spec_bins = ui.number(value=100, step=10, format="%d") \
                            .props("dense filled hide-bottom-space")

                spec_chart = ui.echart({
                    "tooltip": {"trigger": "axis"},
                    "grid": {"left": 60, "right": 18, "top": 14, "bottom": 38},
                    "backgroundColor": "transparent",
                    "textStyle": {"color": "#dde3ee"},
                    "xAxis": {
                        "type": "value", "name": "pulse amplitude (ADC)",
                        "nameLocation": "middle", "nameGap": 22, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "yAxis": {
                        "type": "value", "name": "counts",
                        "nameLocation": "middle", "nameGap": 46, "scale": True,
                        "axisLine":  {"lineStyle": {"color": "#5c6775"}},
                        "axisLabel": {"color": "#8a93a6", "fontSize": 10},
                        "splitLine": {"lineStyle": {"color": "#1d2733"}},
                    },
                    "series": [{
                        "type": "bar",
                        "data": [],
                        "itemStyle": {"color": "#3b82f6"},
                        "barCategoryGap": "5%",
                    }],
                }).classes("plotbox-dig")

                def _draw_spectrum():
                    r = _last_result["r"]
                    if r is None:
                        spec_chart.options["series"][0]["data"] = []
                        spec_chart.update(); return
                    ch = int(spec_ch.value or 0)
                    amps = r.amplitudes.get(ch, []) if hasattr(r, "amplitudes") else []
                    if len(amps) == 0:
                        spec_chart.options["series"][0]["data"] = []
                        spec_chart.update(); return
                    bins = max(10, int(spec_bins.value or 100))
                    arr = _np.asarray(amps, dtype=float)
                    hist, edges = _np.histogram(arr, bins=bins)
                    data = [[float((edges[i] + edges[i + 1]) / 2), int(h)]
                            for i, h in enumerate(hist)]
                    spec_chart.options["series"][0]["data"] = data
                    spec_chart.update()

                spec_ch.on_value_change(lambda _e: _draw_spectrum())
                spec_bins.on_value_change(lambda _e: _draw_spectrum())

        # ---- Periodic connect-strip refresh ----
        def _refresh_conn():
            if HUB.dig is None:
                connstrip.classes(remove="is-connected")
                conn_lbl.set_content(
                    '<span class="lbl">Not connected — CAEN VX2740 digitizer</span>'
                )
                connect_btn.set_visibility(True)
                return
            connstrip.classes(add="is-connected")
            conn_lbl.set_content(
                '<span class="lbl">Connected — CAEN VX2740 digitizer</span>'
            )
            connect_btn.set_visibility(False)

        _refresh_conn()
        ui.timer(1.5, _refresh_conn)

def _build_level3_tab():
    ui.label("Run an IV sweep or pulse acquisition across the whole tile (all SiPMs). Results saved to HDF5.").classes("text-gray-400 text-sm")

    log_lbl  = ui.log(max_lines=24).classes("h-48 w-full")
    progress = ui.linear_progress(value=0).props("instant-feedback").classes("w-full")
    prog_lbl = ui.label("0 / 0 SiPMs").classes("num text-sm")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>output</h2>")
            run_dir = ui.input(label="run directory", value=HUB.config.data_dir).classes("w-72")
            run_id  = ui.input(label="run id",        value="run_001").classes("w-40")

        with ui.card().classes("daq-card"):
            ui.html("<h2>sweep options</h2>")
            temp_k     = ui.number(label="temperature (K)", value=300.0, step=1).classes("w-40 num")
            illum_chk  = ui.switch("illuminated", value=False)
            flux_int   = ui.number(label="flux check every N", value=HUB.config.flux_check_interval, step=1).classes("w-40 num")

        with ui.card().classes("daq-card"):
            ui.html("<h2>iv sweep</h2>")
            iv_start = ui.number(label="start (V)", value=HUB.config.iv_voltage_start, step=0.1).classes("w-32 num")
            iv_stop  = ui.number(label="stop (V)",  value=HUB.config.iv_voltage_stop,  step=0.1).classes("w-32 num")
            iv_step  = ui.number(label="step (V)",  value=HUB.config.iv_voltage_step,  step=0.01).classes("w-32 num")
            iv_npt   = ui.number(label="pts / V",   value=HUB.config.iv_n_per_point,   step=1).classes("w-32 num")

        with ui.card().classes("daq-card"):
            ui.html("<h2>pulse acquisition</h2>")
            p_bias = ui.number(label="bias (V)",  value=HUB.config.pulse_bias_v,      step=0.1).classes("w-32 num")
            p_nwfm = ui.number(label="waveforms", value=HUB.config.pulse_n_waveforms, step=100).classes("w-32 num")

    def _check_ready() -> bool:
        if not HUB.config.sipm_list():
            log_msg("no channel map loaded — go to Config tab and load yaml"); return False
        if not run_dir.value.strip():
            log_msg("run directory is empty"); return False
        return True

    def _on_progress(done, total, sipm_id):
        progress.value = (done / total) if total else 0
        prog_lbl.text  = f"{done} / {total} SiPMs"
        log_msg(f"  [{done}/{total}] SiPM {sipm_id} done")

    async def run_tile_iv():
        if not _check_ready(): return
        import numpy as np
        from daq.tile     import tile_iv_sweep
        from daq.storage  import RunFile, run_filename
        from daq.resume   import RunManifest
        voltages = list(np.arange(float(iv_start.value),
                                  float(iv_stop.value) + float(iv_step.value)*0.5,
                                  float(iv_step.value)))
        sipms = [s.sipm_id for s in HUB.config.sipm_list()]
        log_msg(f"tile IV — {len(sipms)} SiPMs × {len(voltages)} voltages")
        progress.value, prog_lbl.text = 0, f"0 / {len(sipms)} SiPMs"

        mdir = os.path.join(run_dir.value.strip(), run_id.value.strip())
        os.makedirs(mdir, exist_ok=True)
        manifest = RunManifest(mdir); manifest.generate(HUB.config); manifest.save()
        rf = RunFile(run_filename(run_dir.value.strip(), run_id.value.strip()))
        try:
            await _run_in_thread(
                tile_iv_sweep,
                sipms, HUB.instruments, HUB.config,
                float(temp_k.value), bool(illum_chk.value),
                voltages, int(iv_npt.value), int(flux_int.value),
                manifest, rf, _on_progress,
            )
            log_msg(f"tile IV complete — {len(sipms)} SiPMs")
        except Exception as e:
            log_msg(f"FAIL: {type(e).__name__}: {e}")

    async def run_tile_pulse():
        if not _check_ready(): return
        from daq.tile    import tile_pulse_run
        from daq.storage import RunFile, run_filename
        from daq.resume  import RunManifest
        sipms = [s.sipm_id for s in HUB.config.sipm_list()]
        log_msg(f"tile pulse — {len(sipms)} SiPMs × {int(p_nwfm.value)} waveforms")
        progress.value, prog_lbl.text = 0, f"0 / {len(sipms)} SiPMs"

        mdir = os.path.join(run_dir.value.strip(), run_id.value.strip())
        os.makedirs(mdir, exist_ok=True)
        manifest = RunManifest(mdir); manifest.generate(HUB.config); manifest.save()
        rf = RunFile(run_filename(run_dir.value.strip(), run_id.value.strip()))
        try:
            await _run_in_thread(
                tile_pulse_run,
                sipms, HUB.instruments, HUB.config,
                float(temp_k.value), bool(illum_chk.value),
                float(p_bias.value), int(p_nwfm.value), int(flux_int.value),
                manifest, rf, _on_progress,
            )
            log_msg(f"tile pulse complete — {len(sipms)} SiPMs")
        except Exception as e:
            log_msg(f"FAIL: {type(e).__name__}: {e}")

    with ui.row().classes("mt-2 gap-2"):
        ui.button("run tile iv sweep", on_click=run_tile_iv).props("color=primary")
        ui.button("run tile pulse",    on_click=run_tile_pulse).props("color=primary")


# ===========================================================================
# Level 4 — full sequence at one temperature
# ===========================================================================

def _build_level4_tab():
    ui.label("Full measurement sequence at one temperature: wait for stable T → dark IV → dark pulse → [illuminated IV+pulse if listed].").classes("text-gray-400 text-sm")

    log_lbl  = ui.log(max_lines=24).classes("h-48 w-full")
    stage_lbl = ui.label("stage: —").classes("num text-sm")
    progress  = ui.linear_progress(value=0).classes("w-full")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>output</h2>")
            run_dir = ui.input(label="run directory", value=HUB.config.data_dir).classes("w-72")
            run_id  = ui.input(label="run id",        value="run_001").classes("w-40")

        with ui.card().classes("daq-card"):
            ui.html("<h2>temperature</h2>")
            temp_k     = ui.number(label="setpoint (K)", value=165.0, step=1).classes("w-32 num")
            skip_wait  = ui.switch("skip wait (already at target)", value=False)
            tol        = ui.number(label="tolerance (K)", value=HUB.config.temp_tolerance_K, step=0.1).classes("w-32 num")
            stable_s   = ui.number(label="stable for (s)", value=HUB.config.temp_stable_s,    step=10).classes("w-32 num")

    def _check_ready() -> bool:
        if not HUB.config.sipm_list():
            log_msg("no channel map loaded"); return False
        if not run_dir.value.strip():
            log_msg("run directory is empty"); return False
        return True

    def _on_progress(stage, done, total, sipm_id):
        stage_lbl.text = f"stage: {stage}"
        progress.value = (done / total) if total else 0
        log_msg(f"  [{done}/{total}] {stage} — SiPM {sipm_id}")

    async def run_point():
        if not _check_ready(): return
        from daq.temppoint import run_temperature_point
        from daq.storage   import RunFile, run_filename
        from daq.resume    import RunManifest

        HUB.config.temp_tolerance_K = float(tol.value)
        HUB.config.temp_stable_s    = float(stable_s.value)

        mdir = os.path.join(run_dir.value.strip(), run_id.value.strip())
        os.makedirs(mdir, exist_ok=True)
        manifest = RunManifest(mdir); manifest.generate(HUB.config); manifest.save()
        rf = RunFile(run_filename(run_dir.value.strip(), run_id.value.strip()))

        log_msg(f"temp point {float(temp_k.value):.1f} K — "
                f"{len(HUB.config.sipm_list())} SiPMs   (skip_wait={skip_wait.value})")
        try:
            await _run_in_thread(
                run_temperature_point,
                float(temp_k.value), HUB.instruments, HUB.config,
                manifest, rf, _on_progress, bool(skip_wait.value),
            )
            log_msg(f"temp point {float(temp_k.value):.1f} K complete")
        except Exception as e:
            log_msg(f"FAIL: {type(e).__name__}: {e}")

    with ui.row().classes("mt-2"):
        ui.button("run temperature point", on_click=run_point).props("color=primary")


# ===========================================================================
# Level 5 — full run (temperature sweep, with resume)
# ===========================================================================

def _build_level5_tab():
    ui.label("Full experiment: loop over all temperatures in the schedule, with resume support.").classes("text-gray-400 text-sm")

    log_lbl   = ui.log(max_lines=24).classes("h-48 w-full")
    stage_lbl = ui.label("stage: —").classes("num text-sm")
    sipm_prog = ui.linear_progress(value=0).classes("w-full")
    sipm_lbl  = ui.label("SiPMs: 0 / 0").classes("num text-sm")
    temp_prog = ui.linear_progress(value=0).classes("w-full")
    temp_lbl  = ui.label("temperatures: 0 / 0").classes("num text-sm")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>run setup</h2>")
            run_dir = ui.input(label="data directory", value=HUB.config.data_dir).classes("w-72")
            run_id  = ui.input(label="run id",         value="run_001").classes("w-40")
            resume  = ui.switch("resume from existing", value=True)

    def _check_ready() -> bool:
        if not HUB.config.sipm_list():
            log_msg("no channel map loaded"); return False
        if not run_dir.value.strip():
            log_msg("data directory is empty"); return False
        return True

    def _on_progress(level, stage, done, total, sipm_id):
        if level == "temp":
            stage_lbl.text = f"stage: {stage}"
            sipm_prog.value = (done / total) if total else 0
            sipm_lbl.text   = f"SiPMs: {done} / {total}"
            if sipm_id is not None:
                log_msg(f"  [{done}/{total}] {stage} — SiPM {sipm_id}")
        elif level == "run":
            temp_prog.value = (done / total) if total else 0
            temp_lbl.text   = f"temperatures: {done} / {total}"
            log_msg(f"=== temperature {done}/{total} ===")

    async def start_run():
        if not _check_ready(): return
        from daq.run import run_experiment
        full_dir = os.path.join(run_dir.value.strip(), run_id.value.strip())
        n_temps = len(HUB.config.temperatures_K)
        n_sipms = len(HUB.config.sipm_list())
        log_msg(f"starting run — {n_temps} temperatures × {n_sipms} SiPMs   resume={resume.value}")
        sipm_prog.value, temp_prog.value = 0, 0
        sipm_lbl.text = f"SiPMs: 0 / {n_sipms}"
        temp_lbl.text = f"temperatures: 0 / {n_temps}"
        try:
            await _run_in_thread(
                run_experiment, HUB.config, full_dir, bool(resume.value), _on_progress
            )
            log_msg("run complete.")
            stage_lbl.text = "stage: done"
        except Exception as e:
            log_msg(f"FAIL: {type(e).__name__}: {e}")

    with ui.row().classes("mt-2"):
        ui.button("start run", on_click=start_run).props("color=primary")


# ===========================================================================
# Raster — 2D position scans
# ===========================================================================

def _build_raster_tab():
    ui.label("Build a list of raster scan specs and run them. Each scan saves a CSV.").classes("text-gray-400 text-sm")

    specs:   list  = []  # list[RasterSpec]
    results: list  = []  # list[RasterResult]

    log_lbl   = ui.log(max_lines=20).classes("h-40 w-full")
    spec_prog = ui.linear_progress(value=0).classes("w-full")
    spec_lbl  = ui.label("spec 0 / 0").classes("num text-sm")
    point_prog = ui.linear_progress(value=0).classes("w-full")
    point_lbl  = ui.label("point 0 / 0").classes("num text-sm")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        # ---- Spec Builder ----
        with ui.card().classes("daq-card"):
            ui.html("<h2>spec builder</h2>")
            b_ch    = ui.number(label="channel (1–96)", value=1,   step=1).classes("w-32 num")
            b_bias  = ui.number(label="bias (V)",       value=30.0, step=0.1).classes("w-32 num")
            with ui.row().classes("gap-2"):
                b_xs = ui.number(label="x start (mm)", value=0.0, step=0.1).classes("w-28 num")
                b_xe = ui.number(label="x stop (mm)",  value=10.0, step=0.1).classes("w-28 num")
                b_xn = ui.number(label="x pts",         value=11,  step=1).classes("w-20 num")
            with ui.row().classes("gap-2"):
                b_ys = ui.number(label="y start (mm)", value=0.0, step=0.1).classes("w-28 num")
                b_ye = ui.number(label="y stop (mm)",  value=10.0, step=0.1).classes("w-28 num")
                b_yn = ui.number(label="y pts",         value=11,  step=1).classes("w-20 num")
            b_npt   = ui.number(label="n / position",   value=3,   step=1).classes("w-32 num")
            b_stl   = ui.number(label="settle (s)",     value=0.05,step=0.01).classes("w-32 num")
            b_denrg = ui.switch("de-energize between", value=True)
            b_lbl   = ui.input(label="label").classes("w-64")

            b_info = ui.label("11×11 = 121 positions  ×3 = 363 measurements").classes("num text-xs text-gray-400")
            def update_info():
                nx, ny, npt = int(b_xn.value), int(b_yn.value), int(b_npt.value)
                b_info.text = f"{nx}×{ny} = {nx*ny} positions  ×{npt} = {nx*ny*npt} measurements"
            for w in (b_xn, b_yn, b_npt): w.on("update:model-value", lambda _e: update_info())

            def add_spec():
                from daq.raster import RasterSpec
                sp = RasterSpec.linspace(
                    channel=int(b_ch.value), bias_v=float(b_bias.value),
                    x_start=float(b_xs.value), x_stop=float(b_xe.value), num_x=int(b_xn.value),
                    y_start=float(b_ys.value), y_stop=float(b_ye.value), num_y=int(b_yn.value),
                    n_per_point=int(b_npt.value), settle_s=float(b_stl.value),
                    deenergize_between=bool(b_denrg.value), label=b_lbl.value.strip(),
                )
                specs.append(sp); refresh_list()
                log_msg(f"added: {sp.summary()}")
            ui.button("add to list ▶", on_click=add_spec).props("color=primary")

        # ---- Spec List ----
        with ui.card().classes("daq-card flex-1"):
            ui.html("<h2>spec list</h2>")
            list_html = ui.html("<em>no specs yet</em>").classes("text-sm num")
            def refresh_list():
                if not specs:
                    list_html.content = "<em>no specs yet</em>"
                else:
                    lines = "".join(
                        f'<div style="border-top:1px solid var(--line);padding:.2rem 0">'
                        f'<span class="num">{i+1}.</span> {sp.summary()}</div>'
                        for i, sp in enumerate(specs)
                    )
                    list_html.content = lines

            def clear_specs():
                specs.clear(); refresh_list(); log_msg("cleared")
            def remove_last():
                if specs:
                    sp = specs.pop(); refresh_list(); log_msg(f"removed: {sp.summary()}")

            with ui.row().classes("gap-2 mt-1"):
                ui.button("remove last", on_click=remove_last)
                ui.button("clear all", on_click=clear_specs).props("color=negative flat")

            ui.html("<h2 style='margin-top:.6rem'>auto-fill from tile map</h2>")
            t_xw = ui.number(label="x width (mm)", value=8.0, step=0.5).classes("w-32 num")
            t_yw = ui.number(label="y width (mm)", value=8.0, step=0.5).classes("w-32 num")
            t_xn = ui.number(label="x pts",         value=9,   step=1).classes("w-24 num")
            t_yn = ui.number(label="y pts",         value=9,   step=1).classes("w-24 num")
            t_bias = ui.number(label="bias (V)",   value=30.0, step=0.1).classes("w-32 num")
            t_npt  = ui.number(label="n / pos",     value=3,    step=1).classes("w-24 num")

            def add_tile():
                if not HUB.config.sipm_list():
                    log_msg("no channel map — load yaml first"); return
                from daq.raster import tile_raster_specs
                new = tile_raster_specs(
                    config=HUB.config,
                    x_width_mm=float(t_xw.value), y_width_mm=float(t_yw.value),
                    num_x=int(t_xn.value),       num_y=int(t_yn.value),
                    bias_v=float(t_bias.value),  n_per_point=int(t_npt.value),
                    settle_s=float(b_stl.value), deenergize_between=bool(b_denrg.value),
                )
                specs.extend(new); refresh_list()
                log_msg(f"added {len(new)} tile specs")
            ui.button("append tile specs", on_click=add_tile)

    # ---- Run + Save ----
    with ui.card().classes("daq-card w-full"):
        ui.html("<h2>run</h2>")
        save_dir = ui.input(label="save dir (CSV per scan)", value="").classes("w-full")

        def _on_progress(spec_idx, n_specs, done, total, pt):
            spec_prog.value = ((spec_idx + done/total) / n_specs) if n_specs and total else 0
            spec_lbl.text   = f"spec {spec_idx+1} / {n_specs}"
            point_prog.value = (done/total) if total else 0
            point_lbl.text   = f"point {done} / {total}"

        async def run_all():
            nonlocal results
            if not specs: log_msg("no specs"); return
            if HUB.stage is None or HUB.mux is None or HUB.elec is None:
                log_msg("stage/mux/elec must be connected"); return
            from daq.raster import multi_raster
            log_msg(f"running {len(specs)} spec(s)…")
            try:
                results = await _run_in_thread(
                    multi_raster, HUB.stage, HUB.mux, HUB.elec, list(specs), _on_progress,
                )
                log_msg(f"done — {len(results)} scan(s)")
                for r in results:
                    pts = len(r.points)
                    if pts:
                        log_msg(f"  [{r.spec.label or f'ch{r.spec.channel}'}] {pts} pts  "
                                f"I∈[{r.current_a.min():+.3e}, {r.current_a.max():+.3e}] A")
                if save_dir.value.strip():
                    do_save()
            except Exception as e:
                log_msg(f"FAIL: {type(e).__name__}: {e}")

        def do_save():
            if not results: log_msg("no results yet"); return
            import datetime
            d = save_dir.value.strip()
            if not d: log_msg("save dir empty"); return
            os.makedirs(d, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            for i, r in enumerate(results):
                lbl = r.spec.label or f"ch{r.spec.channel}"
                fname = os.path.join(d, f"raster_{ts}_{i:03d}_{lbl}.csv")
                r.to_csv(fname)
                log_msg(f"  saved: {fname}")

        with ui.row().classes("gap-2 mt-1"):
            ui.button("run all specs", on_click=run_all).props("color=primary")
            ui.button("save results CSV", on_click=do_save)


# ===========================================================================
# Alignment — re-zero coordinate origin
# ===========================================================================

def _build_alignment_tab():
    ui.label("Find the actual stage position of a reference SiPM (line scan + centroid) and re-zero the coordinate system so the channel map matches reality.").classes("text-gray-400 text-sm")

    state = {"centroid": None}  # mutable holder

    log_lbl = ui.log(max_lines=18).classes("h-40 w-full")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>current offset</h2>")
            off_x = ui.label(f"x: {HUB.config.position_offset_x_mm:+.4f} mm").classes("num")
            off_y = ui.label(f"y: {HUB.config.position_offset_y_mm:+.4f} mm").classes("num")
            def refresh_off():
                off_x.text = f"x: {HUB.config.position_offset_x_mm:+.4f} mm"
                off_y.text = f"y: {HUB.config.position_offset_y_mm:+.4f} mm"
            def clear_off():
                old = (HUB.config.position_offset_x_mm, HUB.config.position_offset_y_mm)
                HUB.config.clear_offset(); refresh_off()
                log_msg(f"offset cleared (was {old[0]:+.4f}, {old[1]:+.4f} mm)")
            ui.button("clear offset", on_click=clear_off).props("color=negative flat")

        with ui.card().classes("daq-card"):
            ui.html("<h2>reference sipm</h2>")
            sipm_in = ui.number(label="sipm id", value=1, step=1).classes("w-40 num")
            nom_lbl = ui.label("nominal: —").classes("num text-sm")
            def update_nom():
                try:
                    x, y = HUB.config.sipm_position_raw(int(sipm_in.value))
                    nom_lbl.text = f"nominal: ({x:.4f}, {y:.4f}) mm"
                except Exception as e:
                    nom_lbl.text = f"nominal: <err: {e}>"
            sipm_in.on("update:model-value", lambda _e: update_nom())
            update_nom()

        with ui.card().classes("daq-card"):
            ui.html("<h2>alignment scan</h2>")
            scan_type = ui.select(["Line X", "Line Y", "Box (2D)"], value="Line X").classes("w-40")
            scan_w    = ui.number(label="width (mm)", value=8.0,  step=0.5).classes("w-32 num")
            scan_n    = ui.number(label="n points",    value=17,   step=1).classes("w-32 num")
            scan_bias = ui.number(label="bias (V)",   value=30.0, step=0.1).classes("w-32 num")
            scan_npt  = ui.number(label="n / pos",     value=3,    step=1).classes("w-32 num")
            scan_stl  = ui.number(label="settle (s)",  value=0.05, step=0.01).classes("w-32 num")

            async def run_scan():
                sid = int(sipm_in.value)
                if HUB.stage is None or HUB.mux is None or HUB.elec is None:
                    log_msg("stage/mux/elec must be connected"); return
                if not HUB.config.sipm_list():
                    log_msg("no channel map loaded"); return
                try:
                    cx, cy = HUB.config.sipm_position(sid)
                    ch     = HUB.config.sipm_channel(sid)
                except Exception as e:
                    log_msg(f"sipm lookup failed: {e}"); return

                from daq.raster import RasterSpec, raster_scan, centroid_1d
                half, n, npt = float(scan_w.value)/2, int(scan_n.value), int(scan_npt.value)
                stl, bias    = float(scan_stl.value), float(scan_bias.value)
                t = scan_type.value
                if t == "Line X":
                    spec = RasterSpec.linspace(ch, bias, cx-half, cx+half, n, cy, cy, 1,
                                                n_per_point=npt, settle_s=stl,
                                                label=f"align_X_SiPM{sid}")
                elif t == "Line Y":
                    spec = RasterSpec.linspace(ch, bias, cx, cx, 1, cy-half, cy+half, n,
                                                n_per_point=npt, settle_s=stl,
                                                label=f"align_Y_SiPM{sid}")
                else:
                    spec = RasterSpec.linspace(ch, bias, cx-half, cx+half, n,
                                                cy-half, cy+half, n,
                                                n_per_point=npt, settle_s=stl,
                                                label=f"align_box_SiPM{sid}")
                log_msg(f"{t} scan on SiPM {sid} @ ({cx:.3f}, {cy:.3f}) mm  width={float(scan_w.value):.2f} mm")
                try:
                    result = await _run_in_thread(raster_scan, HUB.stage, HUB.mux, HUB.elec, spec, None)
                    cx_found, cy_found = centroid_1d(result)
                    state["centroid"] = (cx_found, cy_found)
                    cent_x.text = f"centroid x: {cx_found:+.4f} mm"
                    cent_y.text = f"centroid y: {cy_found:+.4f} mm"
                    man_x.value = cx_found
                    man_y.value = cy_found
                    log_msg(f"centroid: ({cx_found:+.4f}, {cy_found:+.4f}) mm")
                except Exception as e:
                    log_msg(f"FAIL: {type(e).__name__}: {e}")

            ui.button("run alignment scan", on_click=run_scan).props("color=primary")

        with ui.card().classes("daq-card"):
            ui.html("<h2>found / set origin</h2>")
            cent_x = ui.label("centroid x: —").classes("num text-sm")
            cent_y = ui.label("centroid y: —").classes("num text-sm")
            man_x = ui.number(label="actual x (mm)", value=0.0, step=0.001).classes("w-40 num")
            man_y = ui.number(label="actual y (mm)", value=0.0, step=0.001).classes("w-40 num")

            def set_origin():
                if not HUB.config.sipm_list():
                    log_msg("no channel map loaded"); return
                sid = int(sipm_in.value)
                old = (HUB.config.position_offset_x_mm, HUB.config.position_offset_y_mm)
                try:
                    HUB.config.set_origin(sid, float(man_x.value), float(man_y.value))
                except Exception as e:
                    log_msg(f"set_origin failed: {e}"); return
                new = (HUB.config.position_offset_x_mm, HUB.config.position_offset_y_mm)
                refresh_off()
                log_msg(
                    f"origin set using SiPM {sid} → "
                    f"actual ({float(man_x.value):+.4f}, {float(man_y.value):+.4f}) mm; "
                    f"offset {old[0]:+.4f},{old[1]:+.4f} → {new[0]:+.4f},{new[1]:+.4f}"
                )

            ui.button("set as origin for selected sipm", on_click=set_origin).props("color=primary")


# ===========================================================================
# Main page
# ===========================================================================

async def _emergency_bias_off():
    """Header-level safety button. Calls bias_off on the B2987 if connected.

    Runs the blocking VISA call in a thread so the GUI stays responsive,
    and reports outcome via a top-of-page notification (visible regardless
    of which tab is active).
    """
    if HUB.elec is None:
        ui.notify("BIAS OFF: electrometer not connected",
                  type="warning", position="top", timeout=4000)
        return
    ui.notify("BIAS OFF requested…", type="warning", position="top", timeout=2000)
    try:
        await _run_in_thread(HUB.elec.bias_off)
        ui.notify("BIAS OFF — output disabled", type="positive",
                  position="top", timeout=4000)
        log.warning("emergency BIAS OFF triggered from header")
    except Exception as e:
        ui.notify(f"BIAS OFF FAILED: {type(e).__name__}: {e}",
                  type="negative", position="top", timeout=8000)
        log.exception("emergency BIAS OFF failed")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
#
# Single shared password (lab-internal app on a trusted subnet — this is a
# casual gate, not real auth).  Override at runtime by setting DAQ_PASSWORD
# in the .env at the repo root.  Sessions are persisted via NiceGUI's
# app.storage.user, which requires storage_secret (configured in webapp.py).

_PASSWORD = os.environ.get("DAQ_PASSWORD", "x3n0ntpc")


def _is_authenticated() -> bool:
    """True iff the current request's storage.user has been logged in."""
    try:
        return bool(app.storage.user.get("authenticated", False))
    except Exception:
        # No request context (rare) — treat as not authenticated.
        return False


@ui.page("/login")
def login_page():
    ui.add_head_html(f"<style>{_XSPHERE_CSS}</style>")
    ui.dark_mode().enable()

    # Centred card on a dark backdrop.  The card matches the rest of the
    # app's "register" theme so login doesn't feel like a different app.
    with ui.element("div").style(
        "min-height:100vh; display:flex; align-items:center;"
        " justify-content:center; background:var(--bg); padding:1rem"
    ):
        with ui.card().classes("daq-card").style(
            "max-width:360px; width:100%; padding:1.2rem !important"
        ):
            ui.html(
                '<h2 style="font-size:1.05rem; font-weight:600; margin:0;'
                ' color:var(--acc); letter-spacing:.3px">'
                'nEXO SiPM DAQ &middot; sign in</h2>'
            )
            ui.label(
                "Lab-internal app. Enter the shared password and your name "
                "so other operators can tell who's connected."
            ).classes("text-xs").style(
                "color:var(--mut); margin:.4rem 0 .8rem"
            )

            # Prefill name from prior session (cookie-keyed); empty for
            # first-time browsers.
            saved = ""
            try:
                saved = app.storage.user.get("display_name", "") or ""
            except Exception:
                pass

            name_in = ui.input("Your name", value=saved) \
                .props("autofocus dense outlined").classes("w-full")
            pw_in = ui.input("Password", password=True,
                              password_toggle_button=True) \
                .props("dense outlined").classes("w-full")

            err_lbl = ui.label("").style(
                "color:var(--bad); font-size:.82rem;"
                " min-height:1.1rem; margin:.4rem 0"
            )

            def try_login():
                name = (name_in.value or "").strip() or "anonymous"
                if pw_in.value != _PASSWORD:
                    err_lbl.text = "Wrong password."
                    pw_in.value = ""
                    return
                app.storage.user["authenticated"] = True
                app.storage.user["display_name"]  = name
                ui.navigate.to("/")

            # Enter in either field submits.
            pw_in.on("keydown.enter", try_login)
            name_in.on("keydown.enter", try_login)

            ui.button("Sign in", on_click=try_login) \
                .props("color=primary").classes("w-full mt-2")


@ui.page("/")
def index():
    # Gate the main page on a valid session.  Anything else (FastAPI
    # endpoints, webcam stream, etc.) is gated separately at the route.
    if not _is_authenticated():
        ui.navigate.to("/login")
        return

    ui.add_head_html(f"<style>{_XSPHERE_CSS}</style>")
    ui.dark_mode().enable()

    # --- Session tracking (who has the control window open) ---------------
    client = ui.context.client
    client_id = client.id
    try:
        ip = client.request.client.host  # type: ignore[union-attr]
    except Exception:
        ip = "?"
    saved_name = app.storage.user.get("display_name", "")
    SESSIONS.register(client_id, saved_name, ip)
    client.on_disconnect(lambda: SESSIONS.unregister(client_id))

    def _open_name_dialog():
        with ui.dialog() as dlg, ui.card():
            ui.label("Who's at the controls?").style("font-weight:600;font-size:1rem")
            ui.label(
                "Shown in the header so other lab members can tell who's "
                "connected."
            ).classes("text-xs").style("color:var(--mut);max-width:24rem")
            name_in = ui.input(
                "Display name",
                value=app.storage.user.get("display_name", ""),
            ).props("autofocus")
            def _save():
                new_name = (name_in.value or "").strip() or "anonymous"
                app.storage.user["display_name"] = new_name
                SESSIONS.set_name(client_id, new_name)
                dlg.close()
                _refresh_users_pill()
            name_in.on("keydown.enter", _save)
            with ui.row():
                ui.button("Save", on_click=_save).props('color="primary"')
                ui.button("Cancel", on_click=dlg.close)
        dlg.open()

    # Hidden tabs hold the panel-switching state. The visible navigation is
    # the dropdown buttons inside the header, which drive tabs.set_value().
    with ui.tabs().classes("hidden-tabs") as tabs:
        t_status = ui.tab("status")
        t_conn   = ui.tab("connections")
        t_cfg    = ui.tab("config")
        t_elec   = ui.tab("electrometer")
        t_dig    = ui.tab("digitizer")
        t_mux    = ui.tab("mux")
        t_stage  = ui.tab("stage")
        t_k6485  = ui.tab("k6485")
        t_wfg    = ui.tab("wfg (dg1022)")    # Rigol — hidden from header
        t_ks     = ui.tab("wfg")             # Keysight 33500B — the visible one
        t_nge    = ui.tab("nge100")
        t_cam    = ui.tab("webcam")
        t_lab    = ui.tab("lab book")
        t_l1     = ui.tab("L1 — primitives")
        t_l2     = ui.tab("L2 — single SiPM")
        t_l3     = ui.tab("L3 — tile sweep")
        t_l4     = ui.tab("L4 — temp point")
        t_l5     = ui.tab("L5 — full run")
        t_rast   = ui.tab("raster")
        t_align  = ui.tab("alignment")
        t_plots  = ui.tab("plots")
        t_data   = ui.tab("data")

    # The "instruments" menu is replaced by clickable status pills in the
    # header (see _pill_tabs below). Webcam doesn't have a status pill so
    # it stays in the settings menu for discoverability.
    _menus = {
        # The Rigol DG1022 panel is reachable through this menu — it's
        # hidden from the header status pills (see _visible_specs()) but
        # still useful when the user wants to drive that WFG manually.
        "settings":     [("status", t_status), ("connections", t_conn),
                         ("config", t_cfg), ("lab book", t_lab),
                         ("webcam", t_cam),
                         ("wfg (dg1022, hidden)", t_wfg)],
        # "plots" is promoted out of this dropdown to its own header
        # button — it's the most-clicked destination, so it doesn't
        # belong two levels deep.
        "measurements": [("L1 — primitives", t_l1), ("L2 — single SiPM", t_l2),
                         ("L3 — tile sweep", t_l3), ("L4 — temp point", t_l4),
                         ("L5 — full run", t_l5), ("raster", t_rast),
                         ("alignment", t_align)],
    }

    # Map each instrument spec key to the tab the pill should navigate to.
    # 'sc' has no dedicated tab, so its pill jumps to Connections.
    _pill_tabs = {
        "elec":   t_elec,  "dig":   t_dig,   "mux":   t_mux,
        "stage":  t_stage, "k6485": t_k6485,
        "wfg":     t_wfg,        # Rigol — hidden, but mapped for completeness
        "ks33500b": t_ks,        # Keysight WFG — the visible pill
        "nge100": t_nge,   "sc":    t_conn,
    }

    # Sticky one-row header: title · menus · BIAS OFF · status pills · users.
    # Pinned at top of viewport (position:sticky in .daq-header CSS).
    header_pills: dict[str, ui.html] = {}
    with ui.element("header").classes("daq-header"):
        ui.html("<h1>nEXO SiPM DAQ&nbsp;·&nbsp;control</h1>")

        # Dropdown menus inline with the title.
        # set_value accepts the Tab object directly (passing t.name would
        # silently fail — ui.tab doesn't expose a public .name attribute).
        for menu_label, items in _menus.items():
            with ui.button(menu_label).props("flat no-caps").classes("menu-btn"):
                with ui.menu():
                    for it_label, it_tab in items:
                        ui.menu_item(it_label,
                                     on_click=lambda t=it_tab: tabs.set_value(t))

        # Direct shortcut to the plots tab — most-frequented destination,
        # promoted out of the measurements dropdown.
        ui.button("📊 plots",
                  on_click=lambda: tabs.set_value(t_plots)) \
            .props("flat no-caps").classes("menu-btn hdr-plots-btn")

        # Direct shortcut to the HDF5 data explorer — sits next to plots
        # since the two are the usual "look at recorded data" destinations.
        ui.button("🗂 data",
                  on_click=lambda: tabs.set_value(t_data)) \
            .props("flat no-caps").classes("menu-btn hdr-plots-btn")

        # ⚡ Connect-all in the header: tries each disconnected instrument
        # using addresses already in HUB.config (loaded from
        # .last_connections.json at startup). Hides itself once all are OK.
        async def _do_connect_all_header():
            ui.notify("Connect all: starting…", position="top", timeout=2000)
            ok, fail = [], []
            visible = _visible_specs()
            for spec in visible:
                if HUB.status.get(spec["key"], "").startswith("OK"):
                    ok.append(spec["key"]); continue
                if await _quick_connect(spec["key"]):
                    ok.append(spec["key"])
                else:
                    fail.append(spec["key"])
            msg = f"connect all: ✓ {len(ok)}/{len(visible)}"
            if fail: msg += f"  ·  ✗ {', '.join(fail)}"
            log.info(msg)
            try:
                ui.notify(msg, type="positive" if not fail else "warning",
                          position="top", timeout=6000)
            except RuntimeError:
                pass  # see do_connect_all: page may be gone after long connect loop

        connect_all_btn = ui.button("⚡ connect all",
                                    on_click=_do_connect_all_header) \
            .props("color=primary dense").classes("hdr-connect-all")

        ui.button("⛔ BIAS OFF", on_click=_emergency_bias_off) \
            .classes("estop").tooltip(
                "Disables B2987 output immediately (visible on every tab)."
            )
        for spec in _visible_specs():
            cls = _classify(HUB.status.get(spec["key"], "disconnected"))
            short = _short_status(HUB.status.get(spec["key"], "disconnected"))
            pill = ui.html(
                f'<span class="pill pill-clickable {cls}" '
                f'title="open {spec["name"]} panel">{spec["name"]}: {short}</span>'
            ).style("cursor:pointer")
            target = _pill_tabs.get(spec["key"])
            if target is not None:
                pill.on("click", lambda _e=None, t=target: tabs.set_value(t))
            header_pills[spec["key"]] = pill
        users_pill = ui.html("").style("cursor:pointer")
        users_pill.on("click", lambda _: _open_name_dialog())

        def _refresh_users_pill():
            users = SESSIONS.unique_users()
            label = ", ".join(n for n, _ip in users) if users else "—"
            n = len(users)
            users_pill.set_content(
                f'<span class="pill mut" title="Click to change your name. '
                f'Active: {label}">👤 {n} user{"" if n == 1 else "s"}: {label}</span>'
            )

        _refresh_users_pill()
        ui.timer(2.0, _refresh_users_pill)

        # Unified header-state poll: re-renders the instrument pills from
        # HUB.status (so header-driven connects light them green) and
        # toggles the ⚡ connect-all button visibility. Runs every 1 s.
        def _refresh_header_state():
            for spec in _visible_specs():
                txt = HUB.status.get(spec["key"], "disconnected")
                cls = _classify(txt)
                short = _short_status(txt)
                pill = header_pills.get(spec["key"])
                if pill is not None:
                    new_html = (
                        f'<span class="pill pill-clickable {cls}" '
                        f'title="open {spec["name"]} panel">'
                        f'{spec["name"]}: {short}</span>'
                    )
                    if pill.content != new_html:
                        pill.content = new_html
            any_disconnected = any(
                not HUB.status.get(spec["key"], "").startswith("OK")
                for spec in _visible_specs()
            )
            connect_all_btn.set_visibility(any_disconnected)

        _refresh_header_state()
        ui.timer(1.0, _refresh_header_state)

    # First-time visitor: prompt for a name once. Browser cookie remembers it.
    log.info("session connect: client=%s ip=%s saved_name=%r", client_id, ip, saved_name)
    if not saved_name:
        _open_name_dialog()


    with ui.tab_panels(tabs, value=t_status).classes("w-full"):
        with ui.tab_panel(t_status): _build_status_tab()
        with ui.tab_panel(t_conn):   _build_connections_tab(header_pills)
        with ui.tab_panel(t_cfg):    _build_config_tab()
        with ui.tab_panel(t_elec):   _build_electrometer_tab()
        with ui.tab_panel(t_dig):    _build_digitizer_tab()
        with ui.tab_panel(t_mux):    _build_mux_tab()
        with ui.tab_panel(t_stage):  _build_stage_tab()
        with ui.tab_panel(t_k6485):  _build_k6485_tab()
        with ui.tab_panel(t_wfg):    _build_wfg_tab()
        with ui.tab_panel(t_ks):     _build_ks33500b_tab()
        with ui.tab_panel(t_nge):    _build_nge_tab()
        with ui.tab_panel(t_cam):    _build_webcam_tab()
        with ui.tab_panel(t_plots):  _build_plots_tab()
        with ui.tab_panel(t_lab):    _build_labbook_tab()
        with ui.tab_panel(t_l1):     _build_level1_tab()
        with ui.tab_panel(t_l2):     _build_level2_tab()
        with ui.tab_panel(t_l3):     _build_level3_tab()
        with ui.tab_panel(t_l4):     _build_level4_tab()
        with ui.tab_panel(t_l5):     _build_level5_tab()
        with ui.tab_panel(t_rast):   _build_raster_tab()
        with ui.tab_panel(t_align):  _build_alignment_tab()
        with ui.tab_panel(t_data):   _build_data_tab()

    with ui.element("footer").classes("daq-footer"):
        ui.html("nEXO SiPM tile characterization DAQ &mdash; "
                "Brunner neutrino lab, McGill")
        ui.html('<span class="spacer"></span>')
        ui.html('all tabs ported &middot; legacy PyQt GUI: <code>python -m daq.app</code>')
        ui.html('<a href="https://github.com/" target="_blank">repo</a>')
        ui.html('<a href="/docs" target="_blank">api</a>')
