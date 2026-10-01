import json
import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

SUBMODULE = Path(__file__).resolve().parents[1] / "keysight2987b-python"
sys.path.insert(0, str(SUBMODULE))

from daq.config import ExperimentConfig
from daq.resume import RunManifest
from daq.sequence import MeasurementSpec, build_sequence_steps, run_sequence
from daq.storage import RunFile


class FakeElectrometer:
    hv_confirm = object()
    hv_threshold = 60.0

    def __init__(self):
        self.bias_off_calls = 0

    def sweep_live(self, voltages, n_per_voltage=None, delay_s=None,
                   on_point=None, timeout_s=3600.0):
        run_timestamp = time.time()
        source_v = []
        current_a = []
        timestamp_s = []
        for voltage in voltages:
            currents = [float(voltage) * 1e-12] * int(n_per_voltage)
            timestamps = [time.time()] * int(n_per_voltage)
            if on_point is not None:
                on_point(voltage, currents, timestamps)
            source_v.extend([float(voltage)] * int(n_per_voltage))
            current_a.extend(currents)
            timestamp_s.extend(timestamps)

        source_v = np.asarray(source_v, dtype=np.float64)
        current_a = np.asarray(current_a, dtype=np.float64)
        timestamp_s = np.asarray(timestamp_s, dtype=np.float64)
        avg_source_v = np.asarray(list(voltages), dtype=np.float64)
        avg_current_a = np.asarray(
            [float(voltage) * 1e-12 for voltage in voltages],
            dtype=np.float64,
        )
        zeros = np.zeros_like(avg_source_v)
        return SimpleNamespace(
            source_v=source_v,
            current_a=current_a,
            voltage_v=source_v,
            timestamp_s=timestamp_s,
            avg_source_v=avg_source_v,
            avg_current_a=avg_current_a,
            avg_voltage_v=avg_source_v,
            err_current_a=zeros,
            err_voltage_v=zeros,
            n_per_voltage=int(n_per_voltage),
            run_timestamp=run_timestamp,
        )

    def bias_off(self):
        self.bias_off_calls += 1


def _deleted_client_callback(*_args):
    raise RuntimeError("The client this element belongs to has been deleted.")


def test_deleted_ui_callbacks_do_not_abort_sequence_or_persistence(tmp_path):
    """Removing a browser client must not stop later DAQ sequence steps."""
    specs = [
        MeasurementSpec(
            sipm_id=sipm_id,
            mux_channel=sipm_id,
            temperature_K=165.0,
            do_iv=True,
            do_pulse=False,
            do_scan=False,
            iv_meter="b2987",
            iv_voltages=[45.0, 45.1],
            n_iv_samples_dark=1,
        )
        for sipm_id in (1, 2)
    ]
    elec = FakeElectrometer()
    manifest = RunManifest(str(tmp_path / "run_manifest"))
    manifest.set_steps(build_sequence_steps(specs))
    manifest.save()
    h5_path = tmp_path / "run.h5"

    with RunFile(str(h5_path)) as run_file:
        summary = run_sequence(
            specs,
            {"elec": elec},
            SimpleNamespace(stage_deenergize=True),
            run_file=run_file,
            manifest=manifest,
            on_progress=_deleted_client_callback,
            on_iv_progress=_deleted_client_callback,
            iv_timeout_s=30.0,
        )

    assert summary == {
        "n_entries": 2,
        "n_done": 2,
        "n_skipped": 0,
        "aborted": False,
    }
    assert elec.bias_off_calls >= 2

    with h5py.File(h5_path, "r") as h5:
        assert h5["seq/0/1/165.0K/dark/iv/avg_current_a"].shape == (2,)
        assert h5["seq/1/2/165.0K/dark/iv/avg_current_a"].shape == (2,)

    with open(tmp_path / "run_manifest" / "run_log.jsonl") as stream:
        completed = [json.loads(line)["step_id"] for line in stream]
    assert completed == ["seq0_s1_iv_dark", "seq1_s2_iv_dark"]


def _load_l3job_api():
    spec = importlib.util.find_spec("daq.l3job")
    assert spec is not None, "daq.l3job must own client-independent L3 runs"
    module = importlib.import_module("daq.l3job")
    assert hasattr(module, "L3Job")
    assert hasattr(module, "L3JobRequest")
    return module.L3Job, module.L3JobRequest


def test_l3_job_runs_without_a_page_and_keeps_a_reconnectable_snapshot(tmp_path):
    L3Job, L3JobRequest = _load_l3job_api()
    specs = tuple(
        MeasurementSpec(
            sipm_id=sipm_id,
            mux_channel=sipm_id,
            temperature_K=165.0,
            do_iv=True,
            do_pulse=False,
            do_scan=False,
            iv_meter="b2987",
            iv_voltages=[45.0, 45.1],
            n_iv_samples_dark=1,
        )
        for sipm_id in (1, 2)
    )
    request = L3JobRequest(
        specs=specs,
        run_dir=str(tmp_path),
        run_id="run_001",
        resume=False,
        iv_timeout_s=30.0,
    )
    elec = FakeElectrometer()
    job = L3Job()

    assert job.claim(request) is True
    assert job.claim(request) is False
    summary = job.run_claimed(
        request,
        {"elec": elec},
        ExperimentConfig(),
    )

    assert summary == {
        "n_entries": 2,
        "n_done": 2,
        "n_skipped": 0,
        "aborted": False,
    }
    first_view = job.snapshot()
    reconnect_view = job.snapshot()
    assert reconnect_view == first_view
    assert reconnect_view.status == "done"
    assert reconnect_view.run_id == "run_001"
    assert reconnect_view.entry_idx == 1
    assert reconnect_view.entry_progress == 1.0
    assert reconnect_view.step_progress == 1.0
    assert "entry 2/2" in reconnect_view.iv_status
    assert reconnect_view.error is None
    assert elec.bias_off_calls >= 2

    h5_files = list(tmp_path.glob("run_001_*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as h5:
        assert h5["seq/0/1/165.0K/dark/iv/avg_current_a"].shape == (2,)
        assert h5["seq/1/2/165.0K/dark/iv/avg_current_a"].shape == (2,)

    with open(tmp_path / "run_001" / "run_log.jsonl") as stream:
        completed = [json.loads(line)["step_id"] for line in stream]
    assert completed == ["seq0_s1_iv_dark", "seq1_s2_iv_dark"]


def test_l3_job_releases_claim_when_background_scheduling_fails(tmp_path):
    L3Job, L3JobRequest = _load_l3job_api()
    request = L3JobRequest(
        specs=(),
        run_dir=str(tmp_path),
        run_id="run_001",
        resume=False,
        iv_timeout_s=30.0,
    )
    job = L3Job()

    assert job.claim(request) is True
    job.fail_to_start(RuntimeError("scheduler unavailable"))

    snapshot = job.snapshot()
    assert snapshot.status == "failed"
    assert snapshot.error == "RuntimeError: scheduler unavailable"
    assert job.claim(request) is True


def test_l3_job_releases_claim_when_manifest_setup_fails(tmp_path):
    L3Job, L3JobRequest = _load_l3job_api()
    blocked_run_dir = tmp_path / "not_a_directory"
    blocked_run_dir.write_text("occupied")
    request = L3JobRequest(
        specs=(),
        run_dir=str(blocked_run_dir),
        run_id="run_001",
        resume=False,
        iv_timeout_s=30.0,
    )
    job = L3Job()

    assert job.claim(request) is True
    with pytest.raises(OSError):
        job.run_claimed(request, {}, ExperimentConfig())

    snapshot = job.snapshot()
    assert snapshot.status == "failed"
    assert snapshot.error is not None
    assert job.is_running() is False
    assert job.claim(request) is True
