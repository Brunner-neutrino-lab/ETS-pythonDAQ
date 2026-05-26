"""
scripts/plot_bench.py

CLI driver for the daq.plotting library. Examples:

  # Single plot from a specific file
  python scripts/plot_bench.py iv data/bench_20260526_113743.h5

  # Live mode: most-recent bench_*.h5 in ./data
  python scripts/plot_bench.py iv --live

  # Choose a different plot (see --list)
  python scripts/plot_bench.py spectrum --live --channel 0 --bins 100

  # Overlay multiple files (for SiPM comparison)
  python scripts/plot_bench.py iv \
      data/bench_A.h5 data/bench_B.h5 \
      --label "SiPM A" --label "SiPM B"

  # List available plot types
  python scripts/plot_bench.py --list

By default each plot saves PNG under ./plots/<plot_type>_<timestamp>.png.
Use --show to also pop up an interactive window (requires a display).
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# Make daq importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
import matplotlib.pyplot as plt

from daq.plotting import PLOTS, overlay_plots, find_latest, apply_dark_style


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("plot",  nargs="?",
                    help="plot type (see --list for choices)")
    p.add_argument("files", nargs="*",
                    help="one or more HDF5 files; if multiple, plots are overlayed")
    p.add_argument("--live", action="store_true",
                    help="use the newest bench_*.h5 in ./data (ignores positional files)")
    p.add_argument("--list", action="store_true",
                    help="list available plot types and exit")
    p.add_argument("--label", action="append", default=[],
                    help="label per file (for overlay legend); repeat once per file")
    p.add_argument("--out-dir", default=str(ROOT / "plots"),
                    help="output directory for PNGs (default: ./plots)")
    p.add_argument("--show", action="store_true",
                    help="also display the plot interactively (Agg backend by default)")
    # Per-plot knobs
    p.add_argument("--channel", type=int, default=0)
    p.add_argument("--index",   type=int, default=0)
    p.add_argument("--bins",    type=int, default=100)
    p.add_argument("--bias-group", default="above_vbd",
                    help="for k6485 plots: below_vbd or above_vbd")
    p.add_argument("--log-y",   action="store_true")
    p.add_argument("--no-baseline-subtract", action="store_true",
                    help="for waveform plots: skip baseline subtraction")
    args = p.parse_args()

    if args.list or not args.plot:
        print("Available plot types:\n")
        for k, info in PLOTS.items():
            print(f"  {k:14s}  {info['desc']}")
        if not args.plot:
            return 0
    if args.plot not in PLOTS:
        print(f"unknown plot type: {args.plot!r}.  Use --list to see options.",
              file=sys.stderr)
        return 2

    # Resolve file source(s)
    if args.live:
        latest = find_latest()
        if latest is None:
            print("no bench_*.h5 in ./data", file=sys.stderr); return 2
        sources = [latest]
        print(f"live: {latest}")
    else:
        if not args.files:
            print("either pass file(s) or use --live", file=sys.stderr); return 2
        sources = [Path(f) for f in args.files]

    # Per-plot kwargs (we just pass them all — each fn picks what it knows)
    fn   = PLOTS[args.plot]["fn"]
    opts = dict(
        channel = args.channel,
        index   = args.index,
        bins    = args.bins,
        bias_group = args.bias_group,
        log_y   = args.log_y,
        baseline_subtract = not args.no_baseline_subtract,
    )

    # Build figure
    if args.show:
        matplotlib.use("TkAgg", force=True)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    apply_dark_style(fig, ax)

    if len(sources) == 1:
        label = args.label[0] if args.label else None
        fn(sources[0], ax=ax, label=label, **opts)
    else:
        # Pair up labels (default to file stems)
        labels = list(args.label)
        while len(labels) < len(sources):
            labels.append(sources[len(labels)].stem)
        overlay_plots(fn, list(zip(labels, sources)), ax=ax, **opts)

    fig.tight_layout()

    # Save
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{args.plot}_{ts}.png"
    fig.savefig(out, dpi=120, facecolor=fig.get_facecolor())
    print(f"wrote {out}")

    if args.show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
