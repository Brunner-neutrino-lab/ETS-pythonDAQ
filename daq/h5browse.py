"""
daq/h5browse.py

Read-only introspection for the web GUI's Data tab. Walks any HDF5 file in
the data directory into a NiceGUI ``ui.tree`` node list, and summarizes a
single group or dataset (attributes, shape/dtype, a small value preview,
numeric stats) for the detail pane.

Pure data layer: no NiceGUI imports, so the walking/formatting logic can be
exercised without a browser. The GUI side lives in
``daq.webgui.shell._build_data_tab``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import h5py
import numpy as np


def data_root() -> Path:
    """Repo-root ``data/`` dir — same anchor the plots tab uses."""
    return Path(__file__).resolve().parents[1] / "data"


# Top-level HDF5 group name -> ordered list of daq.plotting PLOTS keys that
# make sense for it. Clicking a node anywhere under one of these groups offers
# the corresponding analysis plot(s). Values are plain strings so this module
# stays free of a daq.plotting import; the GUI resolves them via P.PLOTS.
GROUP_PLOTS: dict[str, list[str]] = {
    "iv":                       ["iv", "iv_leakage"],
    "k6485":                    ["k6485_bars", "k6485_ts"],
    "k6485_noise_floor":        ["k6485_noise_floor"],
    "vx2740":                   ["mean_waveform", "spectrum", "waveform"],
    "vx2740_ov_scan":           ["ov_scan", "ov_spectra"],
    "vx2740_ov_scan_clean":     ["ov_scan_clean", "ov_scan_clean_gain",
                                 "pulse_area_scatter"],
    "vx2740_led_amp_sweep":     ["led_amp_sweep"],
    "vx2740_led_width_sweep":   ["led_width_sweep"],
    "vx2740_dcr_vs_ov":         ["dcr_vs_ov"],
    "vx2740_crosstalk_ap":      ["crosstalk_ap"],
    "vx2740_noise_floor":       ["vx2740_noise_floor"],
    "vx2740_thresh_scan_dark":  ["threshold_scan"],
    "vx2740_thresh_scan_light": ["threshold_scan"],
}


def domain_plots_for(h5path: str) -> list[str]:
    """PLOTS keys applicable to the clicked node's top-level group."""
    top = h5path.strip("/").split("/", 1)[0] if h5path.strip("/") else ""
    return GROUP_PLOTS.get(top, [])


def context_hints(h5path: str) -> dict:
    """Plot kwargs implied by *where* in the tree the user clicked: a ``chN``
    component fixes the channel, ``above/below_vbd`` the K6485 bias group,
    and ``dark``/``light`` (component or thresh-scan group suffix) the
    threshold-scan side. Lets a click on /vx2740/ch3/... plot channel 3."""
    parts = [p for p in h5path.split("/") if p]
    hints: dict = {}
    for p in parts:
        m = re.fullmatch(r"ch(\d+)", p)
        if m:
            hints["channel"] = int(m.group(1))
        if p in ("above_vbd", "below_vbd"):
            hints["bias_group"] = p
        if p in ("dark", "light"):
            hints["which"] = p
    top = parts[0] if parts else ""
    if top.endswith("_dark"):
        hints["which"] = "dark"
    elif top.endswith("_light"):
        hints["which"] = "light"
    return hints


# --- file management (create folder / move / delete) ----------------------
#
# Every path is resolved and checked to stay within the data root before any
# filesystem mutation — the rel paths come from the browser, so a "../.."
# must never escape into the rest of the repo.

def _safe_under_root(root: Path, rel: str) -> Path:
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes data root: {rel!r}")
    return target


def list_folders(data_dir=None) -> list[str]:
    """Relative folder paths under the data root, root first ("" == root)."""
    root = Path(data_dir) if data_dir else data_root()
    if not root.is_dir():
        return [""]
    folders = [""]
    folders += sorted(str(p.relative_to(root))
                      for p in root.rglob("*") if p.is_dir())
    return folders


def make_folder(name: str, data_dir=None) -> str:
    name = (name or "").strip().strip("/")
    if not name:
        raise ValueError("empty folder name")
    root = Path(data_dir) if data_dir else data_root()
    target = _safe_under_root(root, name)
    target.mkdir(parents=True, exist_ok=False)
    return str(target.relative_to(root.resolve()))


def move_file(rel_path: str, dest_folder: str, data_dir=None) -> str:
    root = (Path(data_dir) if data_dir else data_root()).resolve()
    src = _safe_under_root(root, rel_path)
    if not src.is_file():
        raise FileNotFoundError(rel_path)
    dest_dir = _safe_under_root(root, dest_folder or "")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.resolve() == src:
        return str(src.relative_to(root))
    if dest.exists():
        raise FileExistsError(str(dest.relative_to(root)))
    shutil.move(str(src), str(dest))
    return str(dest.relative_to(root))


def delete_file(rel_path: str, data_dir=None) -> None:
    root = (Path(data_dir) if data_dir else data_root()).resolve()
    src = _safe_under_root(root, rel_path)
    if not src.is_file():
        raise FileNotFoundError(rel_path)
    src.unlink()


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def list_data_files(data_dir=None) -> list[dict]:
    """Every ``*.h5`` under the data dir (recursively), newest first.

    L2/L1 measurements land in per-SiPM/per-T subfolders, bench/elec runs at
    the top level — rglob covers both. Each entry: path, rel, size, mtime.
    """
    root = Path(data_dir) if data_dir else data_root()
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*.h5"):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "path": str(p),
            "rel": str(p.relative_to(root)),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def _ds_suffix(obj: h5py.Dataset) -> str:
    shape = "x".join(str(s) for s in obj.shape) or "scalar"
    return f"[{shape} {obj.dtype}]"


def build_tree(path) -> list[dict]:
    """HDF5 hierarchy as a ``ui.tree`` node list.

    Node ``id`` is the object's internal HDF5 path ("/" for root), which is
    what :func:`node_detail` and :func:`read_dataset` re-open by.
    """
    def walk(group, prefix):
        nodes = []
        for key in group.keys():
            obj = group[key]
            node_path = f"{prefix}/{key}" if prefix != "/" else f"/{key}"
            if isinstance(obj, h5py.Group):
                nodes.append({"id": node_path, "label": f"{key}/",
                              "children": walk(obj, node_path)})
            else:
                nodes.append({"id": node_path,
                              "label": f"{key}  {_ds_suffix(obj)}"})
        return nodes

    with h5py.File(path, "r") as f:
        return [{"id": "/", "label": Path(path).name, "children": walk(f, "/")}]


def _fmt_attr(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.ndarray):
        if v.size <= 16:
            return np.array2string(v, precision=4, separator=", ")
        return f"<{v.dtype} array, shape {v.shape}>"
    if isinstance(v, np.generic):
        return v.item()
    return v


def _sample(obj: h5py.Dataset, cap: int = 2_000_000) -> np.ndarray:
    """Bounded read for stats: whole dataset if small, else a leading slice
    along axis 0 so we never pull a multi-GB waveform set into memory."""
    if obj.size <= cap:
        return obj[...]
    if obj.ndim >= 1 and obj.shape[0] > 0:
        per_row = int(np.prod(obj.shape[1:])) or 1
        n = max(1, cap // per_row)
        return obj[:n]
    return obj[...]


def _numeric_stats(arr) -> dict | None:
    a = np.asarray(arr).ravel()
    if a.size == 0 or not np.issubdtype(a.dtype, np.number):
        return None
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return {"n": int(a.size), "finite": 0}
    return {
        "n": int(a.size),
        "finite": int(finite.size),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": float(finite.mean()),
        "std": float(finite.std()),
    }


def _preview(obj: h5py.Dataset, max_items: int = 24) -> str:
    try:
        if obj.ndim == 0:
            return repr(obj[()])
        first = int(obj.shape[0])
        if obj.ndim == 1:
            head = np.asarray(obj[:min(first, max_items)])
            s = np.array2string(head, precision=5, separator=", ",
                                threshold=max_items)
            if first > max_items:
                s += f"  ... (+{first - max_items} more)"
            return s
        head = np.asarray(obj[0]).ravel()
        s = np.array2string(head[:max_items], precision=5, separator=", ",
                            threshold=max_items)
        more = " ..." if head.size > max_items else ""
        return f"row[0] of {first}: {s}{more}"
    except Exception as e:  # corrupt/odd dtype — show why, don't crash the tab
        return f"<unreadable: {type(e).__name__}: {e}>"


def node_detail(path, h5path: str) -> dict:
    """Summarize one group/dataset for the detail pane."""
    with h5py.File(path, "r") as f:
        obj = f[h5path]
        attrs = [(k, _fmt_attr(v)) for k, v in obj.attrs.items()]
        if isinstance(obj, h5py.Group):
            return {"kind": "group", "h5path": h5path, "attrs": attrs,
                    "n_children": len(obj.keys())}
        numeric = bool(np.issubdtype(obj.dtype, np.number))
        stats = _numeric_stats(_sample(obj)) if numeric and obj.size else None
        return {
            "kind": "dataset", "h5path": h5path, "attrs": attrs,
            "shape": tuple(obj.shape), "dtype": str(obj.dtype),
            "ndim": int(obj.ndim), "numeric": numeric,
            "preview": _preview(obj), "stats": stats,
            "plottable": numeric and obj.size > 0 and obj.ndim in (1, 2),
        }


def read_dataset(path, h5path: str, row: int | None = None) -> np.ndarray:
    """Read a dataset for plotting. For 2D datasets, ``row`` selects a single
    row (e.g. one waveform) so we plot a 1D trace instead of 1e6 points."""
    with h5py.File(path, "r") as f:
        obj = f[h5path]
        if row is not None and obj.ndim == 2:
            return np.asarray(obj[int(row)])
        return np.asarray(obj[...])
