"""
daq/resume.py

Run manifest and append-only completion log for experiment resume.

Two files in the run directory:
  run_manifest.json   — full ordered list of planned steps, written once at
                        experiment start, never modified.
  run_log.jsonl       — one JSON object per line, appended after each step
                        completes.  Used to determine which steps to skip on
                        resume.

Step ID convention
------------------
Steps are identified by a string built from the measurement type, SiPM id,
and temperature:

    "iv_dark_ch42_165K"
    "pulse_dark_ch42_165K"
    "iv_illum_ch42_165K"
    "pulse_illum_ch42_165K"
    "flux_after_ch8_165K"

These IDs are generated deterministically from the config so they are stable
across restarts.

Usage
-----
    from daq.resume import RunManifest

    manifest = RunManifest(run_dir="data/run_001")

    # First run: generate and save
    steps = manifest.generate(config)
    manifest.save()

    # Resume: load existing manifest, find incomplete steps
    manifest.load()
    remaining = manifest.remaining_steps()

    # Mark a step complete after each measurement
    manifest.mark_done("iv_dark_ch1_165K",
                        hdf5_path="data/run_001.h5",
                        hdf5_group="/1/165.0K/dark/iv")

    print(f"{manifest.n_done}/{manifest.n_total} steps complete")
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

MANIFEST_FILENAME = "run_manifest.json"
LOG_FILENAME      = "run_log.jsonl"


@dataclass
class Step:
    step_id:     str
    kind:        str    # "iv", "pulse", "flux"
    illuminated: bool
    sipm_id:     Optional[int]   # None for flux steps
    temperature_K: float
    mux_channel: Optional[int]   # None for flux steps


class RunManifest:
    """
    Run manifest and completion log manager.

    Parameters
    ----------
    run_dir : str
        Directory where manifest and log files are stored.
    """

    def __init__(self, run_dir: str):
        self._run_dir      = run_dir
        self._steps: list[Step] = []
        self._done:  set[str]   = set()

        os.makedirs(run_dir, exist_ok=True)
        self._manifest_path = os.path.join(run_dir, MANIFEST_FILENAME)
        self._log_path      = os.path.join(run_dir, LOG_FILENAME)

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, config) -> list[Step]:
        """
        Build the full ordered step list from config.

        Order: for each temperature (coldest last), for each SiPM:
          dark IV → dark pulse
          [if illuminated temp: illuminated IV → illuminated pulse]
          [flux check every N SiPMs]

        Parameters
        ----------
        config : ExperimentConfig

        Returns
        -------
        list[Step]
        """
        steps   = []
        sipms   = config.sipm_list()
        do_illum_temps = set(config.illuminated_temperatures_K)

        for T in config.temperatures_K:
            T_key      = f"{T:.1f}K"
            do_illum   = T in do_illum_temps
            flux_count = 0

            for i, entry in enumerate(sipms):
                sid = entry.sipm_id
                ch  = entry.mux_channel

                steps.append(Step(
                    step_id       = f"iv_dark_ch{sid}_{T_key}",
                    kind          = "iv",
                    illuminated   = False,
                    sipm_id       = sid,
                    temperature_K = T,
                    mux_channel   = ch,
                ))
                steps.append(Step(
                    step_id       = f"pulse_dark_ch{sid}_{T_key}",
                    kind          = "pulse",
                    illuminated   = False,
                    sipm_id       = sid,
                    temperature_K = T,
                    mux_channel   = ch,
                ))

                if do_illum:
                    steps.append(Step(
                        step_id       = f"iv_illum_ch{sid}_{T_key}",
                        kind          = "iv",
                        illuminated   = True,
                        sipm_id       = sid,
                        temperature_K = T,
                        mux_channel   = ch,
                    ))
                    steps.append(Step(
                        step_id       = f"pulse_illum_ch{sid}_{T_key}",
                        kind          = "pulse",
                        illuminated   = True,
                        sipm_id       = sid,
                        temperature_K = T,
                        mux_channel   = ch,
                    ))

                flux_count += 1
                if flux_count >= config.flux_check_interval:
                    steps.append(Step(
                        step_id       = f"flux_after_ch{sid}_{T_key}",
                        kind          = "flux",
                        illuminated   = False,
                        sipm_id       = None,
                        temperature_K = T,
                        mux_channel   = None,
                    ))
                    flux_count = 0

        self._steps = steps
        log.info("Manifest generated: %d steps", len(steps))
        return steps

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self):
        """Write manifest to run_manifest.json (overwrites if exists)."""
        data = [
            {
                "step_id":       s.step_id,
                "kind":          s.kind,
                "illuminated":   s.illuminated,
                "sipm_id":       s.sipm_id,
                "temperature_K": s.temperature_K,
                "mux_channel":   s.mux_channel,
            }
            for s in self._steps
        ]
        with open(self._manifest_path, "w") as f:
            json.dump(data, f, indent=2)
        log.info("Manifest saved: %s (%d steps)", self._manifest_path, len(data))

    def load(self):
        """Load manifest from file and replay completion log."""
        with open(self._manifest_path) as f:
            data = json.load(f)

        self._steps = [
            Step(
                step_id       = d["step_id"],
                kind          = d["kind"],
                illuminated   = d["illuminated"],
                sipm_id       = d.get("sipm_id"),
                temperature_K = d["temperature_K"],
                mux_channel   = d.get("mux_channel"),
            )
            for d in data
        ]

        # Replay completion log
        self._done = set()
        if os.path.exists(self._log_path):
            with open(self._log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        self._done.add(entry["step_id"])
                    except (json.JSONDecodeError, KeyError):
                        log.warning("Malformed log entry: %r", line)

        log.info(
            "Manifest loaded: %d steps, %d already done",
            len(self._steps), len(self._done),
        )

    def exists(self) -> bool:
        """Return True if a manifest file already exists in run_dir."""
        return os.path.exists(self._manifest_path)

    # ------------------------------------------------------------------
    # Progress tracking
    # ------------------------------------------------------------------

    def mark_done(self,
                  step_id:    str,
                  hdf5_path:  Optional[str] = None,
                  hdf5_group: Optional[str] = None,
                  extra:      Optional[dict] = None):
        """
        Append a completion record to run_log.jsonl.

        Parameters
        ----------
        step_id    : Step identifier (must match manifest).
        hdf5_path  : Path to the HDF5 file where data was written.
        hdf5_group : HDF5 group path within the file.
        extra      : Any additional metadata to log.
        """
        self._done.add(step_id)
        record = {
            "step_id":   step_id,
            "t":         time.time(),
            "hdf5_path": hdf5_path,
            "hdf5_group": hdf5_group,
        }
        if extra:
            record.update(extra)

        with open(self._log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def is_done(self, step_id: str) -> bool:
        return step_id in self._done

    def remaining_steps(self) -> list[Step]:
        """Return steps not yet completed, in manifest order."""
        return [s for s in self._steps if s.step_id not in self._done]

    def completed_steps(self) -> list[Step]:
        return [s for s in self._steps if s.step_id in self._done]

    @property
    def n_total(self) -> int:
        return len(self._steps)

    @property
    def n_done(self) -> int:
        return len(self._done)

    @property
    def all_steps(self) -> list[Step]:
        return list(self._steps)
