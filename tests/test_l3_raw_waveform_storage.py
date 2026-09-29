import h5py
import numpy as np

from daq.digitizer import DigitizerResult, _VX2740Backend
from daq.sequence import MeasurementSpec, _exec_pulse
from daq.storage import RunFile


class _FakeElectrometer:
    def __init__(self):
        self.bias_v = 0.0

    def set_bias(self, voltage_v, settle_s=0.0):
        self.bias_v = float(voltage_v)

    def bias_off(self):
        self.bias_v = 0.0


def test_vx_backend_returns_exact_requested_stored_waveforms():
    with _VX2740Backend(address="127.0.0.1", mode="simulation") as digitizer:
        digitizer.setup(
            channels=[2],
            pre_us=0.08,
            post_us=0.16,
            threshold_v=0.001,
        )

        result = digitizer.run(
            n_waveforms=10,
            timeout_s=1.0,
            batch_size=4,
            store_waveforms=True,
        )

    assert result.n_waveforms == 10
    assert 2 in result.waveforms_v
    assert result.waveforms_v[2].shape[0] == 10
    assert result.waveforms_v[2].dtype == np.float32


def test_l3_pulse_writes_exact_requested_waveforms_to_h5(tmp_path):
    spec = MeasurementSpec(
        sipm_id=1,
        temperature_K=298.0,
        pulse_capture_ch=2,
        pulse_pre_us=0.08,
        pulse_post_us=0.16,
        pulse_threshold_adc=50,
        pulse_batch_size=4,
        pulse_store_waveforms=True,
        n_waveforms_dark=10,
    )
    instruments = {
        "elec": _FakeElectrometer(),
        "digitizer": _VX2740Backend(address="127.0.0.1", mode="simulation"),
    }

    with instruments["digitizer"]:
        result = _exec_pulse(
            spec,
            instruments,
            config=object(),
            illuminated=False,
            bias_v=49.0,
        )

    assert isinstance(result, DigitizerResult)

    path = tmp_path / "run.h5"
    with RunFile(str(path)) as run_file:
        run_file.write_pulse_seq(
            0,
            spec.sipm_id,
            spec.temperature_K,
            False,
            49.0,
            result,
        )

    with h5py.File(path, "r") as h5:
        pulse = h5["seq/0/1/298.0K/dark/pulse/49000mV"]
        stored = pulse["ch2/waveforms"][:]
        assert stored.shape[0] == 10
        assert pulse.attrs["n_waveforms"] == 10
        np.testing.assert_array_equal(stored, result.waveforms_v[2])


def test_l3_pulse_without_raw_omits_waveform_dataset(tmp_path):
    spec = MeasurementSpec(
        sipm_id=1,
        temperature_K=298.0,
        pulse_capture_ch=2,
        pulse_pre_us=0.08,
        pulse_post_us=0.16,
        pulse_batch_size=4,
        pulse_store_waveforms=False,
        n_waveforms_dark=10,
    )
    instruments = {
        "elec": _FakeElectrometer(),
        "digitizer": _VX2740Backend(address="127.0.0.1", mode="simulation"),
    }

    with instruments["digitizer"]:
        result = _exec_pulse(
            spec,
            instruments,
            config=object(),
            illuminated=False,
            bias_v=49.0,
        )

    path = tmp_path / "run-no-raw.h5"
    with RunFile(str(path)) as run_file:
        run_file.write_pulse_seq(
            0,
            spec.sipm_id,
            spec.temperature_K,
            False,
            49.0,
            result,
        )

    with h5py.File(path, "r") as h5:
        pulse = h5["seq/0/1/298.0K/dark/pulse/49000mV"]
        assert "waveforms" not in pulse["ch2"]
        assert pulse.attrs["n_waveforms"] == 10
