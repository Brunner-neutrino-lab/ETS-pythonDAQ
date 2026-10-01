import json
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
from daq.digitizer import DigitizerResult, _vx_result_to_normalised
from daq.l3job import L3Job, L3JobRequest
from daq.resume import RunManifest
from daq.sequence import (
    SCHEMA_VERSION,
    MeasurementSpec,
    build_sequence_steps,
    run_sequence,
    sequence_hash,
    spec_to_dict,
)
from daq.storage import RunFile


class FakeElectrometer:
    hv_confirm = object()
    hv_threshold = 60.0

    def __init__(self):
        self.bias_off_calls = 0

    def set_bias(self, _voltage_v, settle_s=0.0):
        return None

    def bias_off(self):
        self.bias_off_calls += 1


class FakePulseController:
    def __init__(self):
        self.channel = None

    def configure_record_window(self, **_kwargs):
        return None

    def configure_channels(self, sipm_channels, **_kwargs):
        self.channel = int(sipm_channels[0])

    def configure_trigger(self, **_kwargs):
        return None


class FakePulseDigitizer:
    def __init__(self, timeout_channels=(), fatal_channels=()):
        self._ctrl = FakePulseController()
        self.timeout_channels = set(timeout_channels)
        self.fatal_channels = set(fatal_channels)
        self.run_channels = []

    def run(self, n_waveforms, **_kwargs):
        channel = self._ctrl.channel
        self.run_channels.append(channel)
        if channel in self.timeout_channels:
            raise TimeoutError(f"channel {channel} produced no events")
        if channel in self.fatal_channels:
            raise ValueError(f"invalid configuration for channel {channel}")
        return DigitizerResult(
            amplitudes_v={channel: np.array([0.01], dtype=np.float32)},
            timestamps={channel: np.array([0.1], dtype=np.float64)},
            waveforms_v={
                channel: np.ones((n_waveforms, 4), dtype=np.float32),
            },
            n_waveforms=n_waveforms,
            source="vx2740",
            channel_ids=[channel],
        )


def pulse_spec(sipm_id, channel):
    return MeasurementSpec(
        sipm_id=sipm_id,
        mux_channel=sipm_id,
        temperature_K=298.0,
        do_iv=False,
        do_pulse=True,
        do_scan=False,
        dark=True,
        illuminated=False,
        pulse_bias_v=[49.0],
        pulse_capture_ch=channel,
        pulse_store_waveforms=True,
        pulse_batch_size=2,
        n_waveforms_dark=2,
    )


def test_empty_vx_waveform_channel_is_reported_as_missing_data():
    raw = SimpleNamespace(
        n_waveforms=40_000,
        source="vx2740",
        run_timestamp=time.time(),
        channel_ids=[9],
        time_axis=None,
        amplitudes={9: np.array([], dtype=np.float32)},
        timestamps={9: np.array([], dtype=np.float64)},
        waveforms={9: []},
    )

    with pytest.raises(RuntimeError, match=r"channel 9.*waveform"):
        _vx_result_to_normalised(raw)


def test_pulse_timeout_records_failure_and_continues_with_next_entry(tmp_path):
    specs = [pulse_spec(9, 9), pulse_spec(10, 10)]
    specs[0].pulse_bias_v = [49.0, 50.0]
    manifest_dir = tmp_path / "manifest"
    manifest = RunManifest(str(manifest_dir))
    manifest.set_steps(build_sequence_steps(specs))
    manifest.save()
    digitizer = FakePulseDigitizer(timeout_channels={9})
    h5_path = tmp_path / "run.h5"

    with RunFile(str(h5_path)) as run_file:
        summary = run_sequence(
            specs,
            {"elec": FakeElectrometer(), "digitizer": digitizer},
            ExperimentConfig(),
            run_file=run_file,
            manifest=manifest,
        )

    assert digitizer.run_channels == [9, 10]
    assert summary["n_done"] == 1
    assert summary["n_failed"] == 1
    assert summary["failures"][0]["entry_idx"] == 0
    assert summary["failures"][0]["sipm_id"] == 9
    assert summary["failures"][0]["error"].startswith("TimeoutError:")

    steps = build_sequence_steps(specs)
    assert manifest.is_done(steps[0].step_id) is False
    assert manifest.is_done(steps[1].step_id) is False
    assert manifest.is_done(steps[2].step_id) is True

    reloaded = RunManifest(str(manifest_dir))
    reloaded.load()
    assert reloaded.is_done(steps[0].step_id) is False
    assert reloaded.is_done(steps[1].step_id) is False
    assert reloaded.is_done(steps[2].step_id) is True

    failures = [
        json.loads(line)
        for line in (manifest_dir / "run_failures.jsonl").read_text().splitlines()
    ]
    assert failures[0]["step_id"] == steps[0].step_id
    assert failures[0]["error"].startswith("TimeoutError:")

    with h5py.File(h5_path, "r") as h5:
        assert "seq/0" not in h5
        assert h5["seq/1/10/298.0K/dark/pulse/49000mV/ch10/waveforms"].shape == (
            2,
            4,
        )


def test_l3_job_finishes_with_errors_and_exposes_skipped_entry(tmp_path):
    specs = (pulse_spec(9, 9), pulse_spec(10, 10))
    request = L3JobRequest(
        specs=specs,
        run_dir=str(tmp_path),
        run_id="run_001",
        resume=False,
        iv_timeout_s=1_200.0,
    )
    job = L3Job()
    assert job.claim(request) is True

    summary = job.run_claimed(
        request,
        {
            "elec": FakeElectrometer(),
            "digitizer": FakePulseDigitizer(timeout_channels={9}),
        },
        ExperimentConfig(),
    )

    snapshot = job.snapshot()
    assert summary["n_failed"] == 1
    assert snapshot.status == "completed_with_errors"
    assert snapshot.error is None
    assert any("SiPM 9" in line and "SKIPPED" in line for _, line in snapshot.logs)


def test_nonrecoverable_pulse_error_still_stops_sequence(tmp_path):
    specs = [pulse_spec(9, 9), pulse_spec(10, 10)]
    digitizer = FakePulseDigitizer(fatal_channels={9})
    manifest_dir = tmp_path / "manifest"
    manifest = RunManifest(str(manifest_dir))
    manifest.set_steps(build_sequence_steps(specs))
    manifest.save()

    with RunFile(str(tmp_path / "run.h5")) as run_file:
        with pytest.raises(ValueError, match="invalid configuration"):
            run_sequence(
                specs,
                {"elec": FakeElectrometer(), "digitizer": digitizer},
                ExperimentConfig(),
                run_file=run_file,
                manifest=manifest,
            )

    assert digitizer.run_channels == [9]
    assert not (manifest_dir / "run_failures.jsonl").exists()


def test_resume_appends_to_manifest_original_h5(tmp_path):
    specs = (pulse_spec(9, 9), pulse_spec(10, 10))
    run_id = "run_001"
    original_h5 = tmp_path / "run_001_20260930_215812.h5"
    first_result = DigitizerResult(
        amplitudes_v={9: np.array([0.01], dtype=np.float32)},
        timestamps={9: np.array([0.1], dtype=np.float64)},
        waveforms_v={9: np.ones((2, 4), dtype=np.float32)},
        n_waveforms=2,
        source="vx2740",
        channel_ids=[9],
    )
    with RunFile(str(original_h5), config=ExperimentConfig()) as run_file:
        run_file.write_sequence_meta(
            {
                "schema_version": SCHEMA_VERSION,
                "hash": sequence_hash(list(specs)),
                "iv_timeout_s": 1_200.0,
                "entries": [spec_to_dict(spec) for spec in specs],
            }
        )
        run_file.write_pulse_seq(0, 9, 298.0, False, 49.0, first_result)

    manifest = RunManifest(str(tmp_path / run_id))
    steps = build_sequence_steps(specs)
    manifest.set_steps(steps)
    manifest.save()
    manifest.mark_done(
        steps[0].step_id,
        hdf5_path=str(original_h5),
        hdf5_group="/seq/0/9/298.0K/dark/pulse/49000mV",
    )

    request = L3JobRequest(
        specs=specs,
        run_dir=str(tmp_path),
        run_id=run_id,
        resume=True,
        iv_timeout_s=1_200.0,
    )
    job = L3Job()
    assert job.claim(request) is True
    summary = job.run_claimed(
        request,
        {"elec": FakeElectrometer(), "digitizer": FakePulseDigitizer()},
        ExperimentConfig(),
    )

    assert summary["n_skipped"] == 1
    assert list(tmp_path.glob("run_001_*.h5")) == [original_h5]
    with h5py.File(original_h5, "r") as h5:
        assert h5["seq/0/9/298.0K/dark/pulse/49000mV/ch9/waveforms"].shape == (
            2,
            4,
        )
        assert h5["seq/1/10/298.0K/dark/pulse/49000mV/ch10/waveforms"].shape == (
            2,
            4,
        )


def test_resume_refuses_h5_from_a_different_sequence(tmp_path):
    specs = (pulse_spec(9, 9), pulse_spec(10, 10))
    run_id = "run_001"
    original_h5 = tmp_path / "run_001_20260930_215812.h5"
    with RunFile(str(original_h5), config=ExperimentConfig()) as run_file:
        run_file.write_sequence_meta(
            {
                "schema_version": SCHEMA_VERSION,
                "hash": "different-sequence",
                "iv_timeout_s": 1_200.0,
                "entries": [],
            }
        )

    manifest = RunManifest(str(tmp_path / run_id))
    steps = build_sequence_steps(specs)
    manifest.set_steps(steps)
    manifest.save()
    manifest.mark_done(
        steps[0].step_id,
        hdf5_path=str(original_h5),
        hdf5_group="/seq/0/9/298.0K/dark/pulse/49000mV",
    )
    request = L3JobRequest(
        specs=specs,
        run_dir=str(tmp_path),
        run_id=run_id,
        resume=True,
        iv_timeout_s=1_200.0,
    )
    job = L3Job()
    assert job.claim(request) is True

    with pytest.raises(RuntimeError, match=r"sequence.*does not match"):
        job.run_claimed(
            request,
            {"elec": FakeElectrometer(), "digitizer": FakePulseDigitizer()},
            ExperimentConfig(),
        )
