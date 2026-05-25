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
             "rigoldg1022-python"):
    _p = os.path.join(_REPO, _pkg)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from daq.config import ExperimentConfig
from daq.gui.hub import InstrumentHub
from daq import primitives as P
from daq import measurement as M

log = logging.getLogger("daq.webgui")


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

HUB = InstrumentHub()


_INSTRUMENT_SPECS = [
    {"key": "elec",  "name": "b2987",     "addr_attr": "b2987b_visa",
     "addr_label": "VISA",  "connect": "connect_elec",  "disconnect": "disconnect_elec"},
    {"key": "dig",   "name": "digitizer", "addr_attr": "digitizer_address",
     "addr_label": "addr", "connect": "connect_dig",   "disconnect": "disconnect_dig"},
    {"key": "mux",   "name": "mux",       "addr_attr": "mux_port",
     "addr_label": "port", "connect": "connect_mux",   "disconnect": "disconnect_mux"},
    {"key": "k6485", "name": "k6485",     "addr_attr": "k6485_port",
     "addr_label": "VISA", "connect": "connect_k6485", "disconnect": "disconnect_k6485"},
    {"key": "wfg",   "name": "wfg (dg1022)", "addr_attr": "wfg_visa",
     "addr_label": "VISA / device", "connect": "connect_wfg", "disconnect": "disconnect_wfg"},
    {"key": "stage", "name": "stage",     "addr_attr": None,
     "addr_label": "serials","connect": "connect_stage", "disconnect": "disconnect_stage"},
    {"key": "sc",    "name": "slow ctrl", "addr_attr": "influxdb_url",
     "addr_label": "URL",  "connect": "connect_sc",    "disconnect": "disconnect_sc"},
]


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

/* Sticky header with title + status pills */
.daq-header {
  display:flex; align-items:center; gap:.8rem; flex-wrap:wrap;
  padding:.55rem 1rem; background:var(--panel); border-bottom:1px solid var(--line);
  position:sticky; top:0; z-index:5;
}
.daq-header h1 { font-size:1.05rem; margin:0 .4rem 0 0; font-weight:600;
  letter-spacing:.3px; color:var(--fg); }
.daq-header .sub { color:var(--mut); font-size:.78rem; }
.pill { padding:.15rem .55rem; border-radius:999px; font-size:.78rem;
  font-weight:600; white-space:nowrap; display:inline-flex; align-items:center; gap:.3rem; }
.pill.ok   { background:rgba(63,185,80,.18);  color:var(--ok); }
.pill.bad  { background:rgba(248,81,73,.18);  color:var(--bad); }
.pill.warn { background:rgba(210,153,34,.18); color:var(--warn); }
.pill.mut  { background:rgba(138,147,166,.15);color:var(--mut); }
.dot { width:.55rem; height:.55rem; border-radius:50%; background:currentColor; display:inline-block; }

/* Tabs (Quasar overrides) */
.q-tab { color:var(--mut) !important; text-transform:none !important;
  letter-spacing:.2px; font-size:.88rem; padding:0 1rem !important; }
.q-tab--active { color:var(--acc) !important; }
.q-tab__indicator { background:var(--acc) !important; }
.q-tab-panel { background:var(--bg) !important; padding:.6rem !important; }

/* Cards = dark panels */
.q-card, .daq-card {
  background:var(--panel) !important; color:var(--fg) !important;
  border:1px solid var(--line); border-radius:10px;
  box-shadow:none !important; padding:.55rem .85rem .7rem !important;
}
.daq-card h2 { font-size:.92rem; margin:.05rem 0 .45rem; color:var(--acc);
  font-weight:600; letter-spacing:.3px; }

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
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_in_thread(fn: Callable, *args, **kwargs):
    """Run a blocking instrument call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


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


# ===========================================================================
# Connections tab
# ===========================================================================

def _build_connections_tab(header_pills: dict[str, ui.html]):
    """`header_pills` is a dict of instrument-key → ui.html element in the
    sticky header that this tab updates as connections change."""

    ui.label("Connect each instrument independently. Header pills mirror live status.").classes("text-gray-400 text-sm")

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
                f'<span class="pill {cls}">{spec["name"]}: {_short_status(text)}</span>'
            )

    for spec in _INSTRUMENT_SPECS:
        key = spec["key"]
        with ui.card().classes("daq-card w-full"):
            ui.html(f'<h2>{spec["name"]}</h2>')
            with ui.row().classes("items-center w-full no-wrap"):
                if spec["addr_attr"] is not None:
                    inp = ui.input(label=spec["addr_label"],
                                   value=getattr(HUB.config, spec["addr_attr"])).classes("flex-1")
                    addr_inputs[key] = inp
                else:
                    ui.label(
                        f'x={HUB.config.stage_serial_x}  '
                        f'y={HUB.config.stage_serial_y}  '
                        f'hub={HUB.config.stage_serial_limit}'
                    ).classes("flex-1 text-gray-400 num")

                async def do_connect(spec=spec):
                    k = spec["key"]
                    if k in addr_inputs and spec["addr_attr"] is not None:
                        setattr(HUB.config, spec["addr_attr"], addr_inputs[k].value)
                    HUB.status[k] = "connecting…"
                    refresh(k)
                    try:
                        await _run_in_thread(getattr(HUB, spec["connect"]))
                    except Exception as e:
                        HUB.status[k] = f"FAIL: {type(e).__name__}: {e}"
                    refresh(k)

                async def do_disconnect(spec=spec):
                    k = spec["key"]
                    try:
                        await _run_in_thread(getattr(HUB, spec["disconnect"]))
                    except Exception as e:
                        HUB.status[k] = f"disconnect failed: {e}"
                    refresh(k)

                ui.button("connect",    on_click=do_connect).props("color=primary")
                ui.button("disconnect", on_click=do_disconnect).props("color=negative flat")

                status_labels[key] = ui.label(HUB.status.get(key, "disconnected")).classes("ml-2")
                refresh(key)


# ===========================================================================
# Config tab
# ===========================================================================

def _build_config_tab():
    ui.label("Edit run parameters. Load/save YAML on disk.").classes("text-gray-400 text-sm")

    yaml_path = ui.input(label="YAML path",
                         value=os.path.join(_REPO, "run_config.yaml")).classes("w-full")
    msg_log = ui.log(max_lines=10).classes("h-32 w-full")
    def log_msg(s: str): msg_log.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    inputs: dict[str, ui.input | ui.number] = {}
    def field(label: str, attr: str, **kw):
        w = ui.number(label=label, value=getattr(HUB.config, attr), **kw).classes("w-40 num")
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

    def apply_to_hub():
        for attr, w in inputs.items():
            current = getattr(HUB.config, attr)
            v = w.value
            try:
                v = int(v) if isinstance(current, int) else float(v)
            except (TypeError, ValueError):
                pass
            setattr(HUB.config, attr, v)
        try:
            HUB.config.temperatures_K = [
                float(x.strip()) for x in temps_in.value.split(",") if x.strip()
            ]
            HUB.config.illuminated_temperatures_K = [
                float(x.strip()) for x in illum_in.value.split(",") if x.strip()
            ]
        except ValueError as e:
            log_msg(f"temp parse error: {e}"); return
        log_msg("applied to hub")

    def load_yaml():
        try:
            HUB.config = ExperimentConfig.from_yaml(yaml_path.value)
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
            apply_to_hub()
            HUB.config.to_yaml(yaml_path.value)
            log_msg(f"saved → {yaml_path.value}")
        except Exception as e:
            log_msg(f"save failed: {type(e).__name__}: {e}")

    with ui.row().classes("mt-2"):
        ui.button("load yaml",    on_click=load_yaml).props("color=primary")
        ui.button("save yaml",    on_click=save_yaml)
        ui.button("apply to hub", on_click=apply_to_hub).props("color=secondary")


# ===========================================================================
# Level 1 — primitives
# ===========================================================================

def _build_level1_tab():
    ui.label("Manual single-instrument operations. Connect first on the Connections tab.").classes("text-gray-400 text-sm")

    log_lbl = ui.log(max_lines=18).classes("h-40 w-full")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):

        with ui.card().classes("daq-card"):
            ui.html("<h2>stage</h2>")
            x_in = ui.number(label="x (mm)", value=0.0, step=0.1).classes("w-32 num")
            y_in = ui.number(label="y (mm)", value=0.0, step=0.1).classes("w-32 num")
            async def move():
                if HUB.stage is None: log_msg("stage not connected"); return
                log_msg(f"move → ({x_in.value:.2f}, {y_in.value:.2f}) mm")
                try:
                    await _run_in_thread(P.move_stage, HUB.stage,
                                         x_mm=float(x_in.value), y_mm=float(y_in.value),
                                         deenergize_after=HUB.config.stage_deenergize)
                    x, y = await _run_in_thread(P.stage_position, HUB.stage)
                    log_msg(f"  arrived ({x:.3f}, {y:.3f}) mm")
                except Exception as e:
                    log_msg(f"  move FAIL: {type(e).__name__}: {e}")
            async def read_pos():
                if HUB.stage is None: log_msg("stage not connected"); return
                try:
                    x, y = await _run_in_thread(P.stage_position, HUB.stage)
                    log_msg(f"position: ({x:.3f}, {y:.3f}) mm")
                except Exception as e:
                    log_msg(f"  read FAIL: {type(e).__name__}: {e}")
            async def do_home():
                if HUB.stage is None: log_msg("stage not connected"); return
                log_msg("homing stage…")
                try:
                    await _run_in_thread(P.home_stage, HUB.stage)
                    log_msg("  homed")
                except Exception as e:
                    log_msg(f"  home FAIL: {type(e).__name__}: {e}")
            with ui.row().classes("gap-2"):
                ui.button("move",     on_click=move).props("color=primary")
                ui.button("read pos", on_click=read_pos)
                ui.button("home",     on_click=do_home).props("color=warning")

        with ui.card().classes("daq-card"):
            ui.html("<h2>mux</h2>")
            ch_in = ui.number(label="channel (1–96)", value=1, step=1).classes("w-40 num")
            async def select_ch():
                if HUB.mux is None: log_msg("mux not connected"); return
                ch = int(ch_in.value); log_msg(f"mux → ch {ch}")
                try:
                    await _run_in_thread(P.select_channel, HUB.mux, ch)
                    log_msg(f"  ch {ch} active")
                except Exception as e:
                    log_msg(f"  select FAIL: {type(e).__name__}: {e}")
            async def zero_mux():
                if HUB.mux is None: log_msg("mux not connected"); return
                try:
                    await _run_in_thread(P.zero_channels, HUB.mux)
                    log_msg("mux zeroed")
                except Exception as e:
                    log_msg(f"  zero FAIL: {type(e).__name__}: {e}")
            with ui.row().classes("gap-2"):
                ui.button("select", on_click=select_ch).props("color=primary")
                ui.button("zero",   on_click=zero_mux)

        with ui.card().classes("daq-card"):
            ui.html("<h2>bias (b2987)</h2>")
            v_in = ui.number(label="voltage (V)", value=0.0, step=0.5).classes("w-40 num")
            async def set_v():
                if HUB.elec is None: log_msg("electrometer not connected"); return
                v = float(v_in.value); log_msg(f"set_bias {v:.3f} V")
                try:
                    await _run_in_thread(P.set_bias, HUB.elec, v, 0.2)
                    log_msg("  bias on")
                except Exception as e:
                    log_msg(f"  set_bias FAIL: {type(e).__name__}: {e}")
            async def bias_off():
                if HUB.elec is None: log_msg("electrometer not connected"); return
                try:
                    await _run_in_thread(P.bias_off, HUB.elec); log_msg("bias off")
                except Exception as e:
                    log_msg(f"  bias_off FAIL: {type(e).__name__}: {e}")
            async def read_i():
                if HUB.elec is None: log_msg("electrometer not connected"); return
                try:
                    i = await _run_in_thread(P.measure_current, HUB.elec)
                    log_msg(f"I = {i:.3e} A")
                except Exception as e:
                    log_msg(f"  measure FAIL: {type(e).__name__}: {e}")
            with ui.row().classes("gap-2"):
                ui.button("set bias", on_click=set_v).props("color=primary")
                ui.button("bias OFF", on_click=bias_off).props("color=negative")
                ui.button("read I",   on_click=read_i)

        with ui.card().classes("daq-card"):
            ui.html("<h2>flux / temperature</h2>")
            async def read_flux():
                if HUB.k6485 is None: log_msg("k6485 not connected"); return
                try:
                    f = await _run_in_thread(P.read_flux, HUB.k6485)
                    log_msg(f"flux = {f:.3e} A")
                except Exception as e:
                    log_msg(f"  flux FAIL: {type(e).__name__}: {e}")
            async def read_temp():
                if HUB.sc is None: log_msg("slow control not connected"); return
                try:
                    T = await _run_in_thread(P.read_temperature, HUB.sc)
                    log_msg(f"T = {T:.3f} K")
                except Exception as e:
                    log_msg(f"  temp FAIL: {type(e).__name__}: {e}")
            with ui.row().classes("gap-2"):
                ui.button("read flux", on_click=read_flux)
                ui.button("read T",    on_click=read_temp)


# ===========================================================================
# Level 2 — single SiPM
# ===========================================================================

def _build_level2_tab():
    ui.label("Run an IV sweep or pulse acquisition on one SiPM. Results not saved (Level 3+ writes HDF5).").classes("text-gray-400 text-sm")

    log_lbl = ui.log(max_lines=24).classes("h-64 w-full")
    def log_msg(s: str): log_lbl.push(f"[{time.strftime('%H:%M:%S')}] {s}")

    with ui.row().classes("w-full gap-3 items-start"):
        with ui.card().classes("daq-card"):
            ui.html("<h2>SiPM selection</h2>")
            sipm_in = ui.number(label="SiPM id", value=1, step=1).classes("w-40 num")
            illum   = ui.switch("illuminated", value=False)
            def refresh_list():
                ids = [e.sipm_id for e in HUB.config.sipm_list()]
                if ids:
                    log_msg(f"channel map: {len(ids)} SiPMs ({min(ids)}..{max(ids)})")
                else:
                    log_msg("no channel map — go to Config tab and load yaml")
            ui.button("inspect map", on_click=refresh_list)

        with ui.card().classes("daq-card"):
            ui.html("<h2>iv sweep</h2>")
            iv_start = ui.number(label="start (V)", value=HUB.config.iv_voltage_start, step=0.1).classes("w-32 num")
            iv_stop  = ui.number(label="stop (V)",  value=HUB.config.iv_voltage_stop,  step=0.1).classes("w-32 num")
            iv_step  = ui.number(label="step (V)",  value=HUB.config.iv_voltage_step,  step=0.01).classes("w-32 num")
            iv_npt   = ui.number(label="pts / V",   value=HUB.config.iv_n_per_point,   step=1).classes("w-32 num")
            async def run_iv():
                if HUB.elec is None: log_msg("electrometer not connected"); return
                try:
                    import numpy as np
                    voltages = np.arange(float(iv_start.value),
                                         float(iv_stop.value) + float(iv_step.value)*0.5,
                                         float(iv_step.value)).tolist()
                    log_msg(f"IV sipm={int(sipm_in.value)} illum={illum.value} "
                            f"{len(voltages)} voltages")
                    result = await _run_in_thread(
                        M.iv_sweep,
                        int(sipm_in.value), HUB.instruments, HUB.config,
                        illum.value, voltages, int(iv_npt.value),
                    )
                    log_msg(f"  done: {len(result.avg_source_v)} pts  "
                            f"I({result.avg_source_v[-1]:.2f}V) = {result.avg_current_a[-1]:.3e} A")
                except Exception as e:
                    log_msg(f"  IV FAIL: {type(e).__name__}: {e}")
            ui.button("run iv sweep", on_click=run_iv).props("color=primary")

        with ui.card().classes("daq-card"):
            ui.html("<h2>pulse acquisition</h2>")
            pulse_v = ui.number(label="bias (V)",  value=HUB.config.pulse_bias_v,      step=0.1).classes("w-32 num")
            pulse_n = ui.number(label="waveforms", value=HUB.config.pulse_n_waveforms, step=100).classes("w-32 num")
            async def run_pulse():
                if HUB.elec is None or HUB.dig is None:
                    log_msg("electrometer or digitizer not connected"); return
                try:
                    log_msg(f"PULSE sipm={int(sipm_in.value)} illum={illum.value} "
                            f"bias={pulse_v.value} V n={int(pulse_n.value)}")
                    result = await _run_in_thread(
                        M.pulse_run,
                        int(sipm_in.value), HUB.instruments, HUB.config,
                        illum.value, float(pulse_v.value), int(pulse_n.value),
                    )
                    log_msg(f"  done: n_waveforms={result.n_waveforms} channels={result.channel_ids}")
                except Exception as e:
                    log_msg(f"  PULSE FAIL: {type(e).__name__}: {e}")
            ui.button("run pulse", on_click=run_pulse).props("color=primary")


# ===========================================================================
# Electrometer — embedded b2987 manual-control panel
# ===========================================================================

def _build_electrometer_tab():
    """Embed the b2987 standalone GUI, sharing the DAQ's connected controller."""
    from b2987b.gui import build_page as b2987_build_page

    ui.label(
        "Manual control of the B2987 electrometer. The connection is shared "
        "with the Connections tab — connect there first; this tab drives the "
        "same controller."
    ).classes("text-gray-400 text-sm")

    # The getter is called on each user action, so reconnects via the
    # Connections tab are picked up automatically (no need to refresh).
    b2987_build_page(get_controller=lambda: HUB.elec, show_connection=False)


def _build_mux_tab():
    """Embed the pulse-mux standalone GUI, sharing the DAQ's connected MUX."""
    from pulse_mux.gui import build_page as mux_build_page

    ui.label(
        "Manual control of the 96-channel IV-Pulse MUX. The connection is "
        "shared with the Connections tab — connect there first; this tab "
        "drives the same controller."
    ).classes("text-gray-400 text-sm")
    mux_build_page(get_controller=lambda: HUB.mux, show_connection=False)


def _build_digitizer_tab():
    """Embed the vx2740 standalone GUI, sharing the DAQ's connected controller."""
    # Only the VX2740 has a NiceGUI panel today; the RTO2024 path still
    # uses its old PyQt5 GUI, so guard the import accordingly.
    cfg_type = (HUB.config.digitizer_type or "").lower()
    if cfg_type != "vx2740":
        ui.label(
            f"This tab embeds the VX2740 control panel. The configured "
            f"digitizer_type is {cfg_type!r}; switch to 'vx2740' in the "
            f"Config tab to enable this panel."
        ).classes("text-gray-400 text-sm")
        return

    from vx2740.gui import build_page as vx_build_page

    ui.label(
        "Manual control of the CAEN VX2740 digitizer. The connection is "
        "shared with the Connections tab — connect there first; this tab "
        "drives the same controller. Embedded mode uses the controller "
        "directly, so 'apply config' / 'run acquisition' here are seen by "
        "Level 2+ runs as well."
    ).classes("text-gray-400 text-sm")

    # The embedded digitizer needs the *controller*, not the make_digitizer
    # backend wrapper that the daq.digitizer module wraps it in.  The hub
    # currently stores the backend (which holds the controller as ._ctrl
    # via the _VX2740Backend wrapper).  Reach into it for the controller.
    def get_vx_controller():
        backend = HUB.dig
        if backend is None:
            return None
        # _VX2740Backend stores the underlying VX2740Controller as ._ctrl
        return getattr(backend, "_ctrl", None)

    vx_build_page(get_controller=get_vx_controller, show_connection=False)

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

@ui.page("/")
def index():
    ui.add_head_html(f"<style>{_XSPHERE_CSS}</style>")
    ui.dark_mode().enable()

    # Sticky header with title + per-instrument status pills
    header_pills: dict[str, ui.html] = {}
    with ui.element("header").classes("daq-header"):
        ui.html("<h1>nEXO SiPM DAQ&nbsp;·&nbsp;control</h1>")
        for spec in _INSTRUMENT_SPECS:
            cls = _classify(HUB.status.get(spec["key"], "disconnected"))
            short = _short_status(HUB.status.get(spec["key"], "disconnected"))
            header_pills[spec["key"]] = ui.html(
                f'<span class="pill {cls}">{spec["name"]}: {short}</span>'
            )
        ui.html('<span style="flex:1"></span>')
        ui.html('<span class="sub">all tabs ported · PyQt GUI still at <code>python -m daq.app</code></span>')

    with ui.tabs().classes("w-full") as tabs:
        t_conn  = ui.tab("connections")
        t_cfg   = ui.tab("config")
        t_elec  = ui.tab("electrometer")
        t_dig   = ui.tab("digitizer")
        t_mux   = ui.tab("mux")
        t_l1    = ui.tab("L1 — primitives")
        t_l2    = ui.tab("L2 — single SiPM")
        t_l3    = ui.tab("L3 — tile sweep")
        t_l4    = ui.tab("L4 — temp point")
        t_l5    = ui.tab("L5 — full run")
        t_rast  = ui.tab("raster")
        t_align = ui.tab("alignment")

    with ui.tab_panels(tabs, value=t_conn).classes("w-full"):
        with ui.tab_panel(t_conn):  _build_connections_tab(header_pills)
        with ui.tab_panel(t_cfg):   _build_config_tab()
        with ui.tab_panel(t_elec):  _build_electrometer_tab()
        with ui.tab_panel(t_dig):   _build_digitizer_tab()
        with ui.tab_panel(t_mux):   _build_mux_tab()
        with ui.tab_panel(t_l1):    _build_level1_tab()
        with ui.tab_panel(t_l2):    _build_level2_tab()
        with ui.tab_panel(t_l3):    _build_level3_tab()
        with ui.tab_panel(t_l4):    _build_level4_tab()
        with ui.tab_panel(t_l5):    _build_level5_tab()
        with ui.tab_panel(t_rast):  _build_raster_tab()
        with ui.tab_panel(t_align): _build_alignment_tab()
