import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from daq import primitives as P
from daq.sequence import MeasurementSpec, run_sequence


class FakeElectrometer:
    hv_confirm = object()
    hv_threshold = 60.0

    def __init__(self):
        self.live_calls = []
        self.bias_off_calls = 0

    def sweep(self, *args, **kwargs):
        raise AssertionError("L3 B2987 IV must use the live sweep path")

    def sweep_live(self, voltages, n_per_voltage=None, delay_s=None,
                   on_point=None, timeout_s=3600.0):
        self.live_calls.append({
            "voltages": list(voltages),
            "n_per_voltage": n_per_voltage,
            "delay_s": delay_s,
            "timeout_s": timeout_s,
        })
        for voltage in voltages:
            currents = [voltage * 1e-12, voltage * 2e-12]
            timestamps = [1000.0, 1001.0]
            on_point(voltage, currents, timestamps)
        return SimpleNamespace(avg_source_v=list(voltages))

    def bias_off(self):
        self.bias_off_calls += 1


class RecordingRunFile:
    _path = "test-run.h5"

    def __init__(self):
        self.meta = None
        self.iv_writes = []

    def write_sequence_meta(self, meta):
        self.meta = meta

    def write_iv_seq(self, *args, **kwargs):
        self.iv_writes.append((args, kwargs))


def fake_b2987_modules():
    package = types.ModuleType("b2987b")
    driver = types.ModuleType("b2987b.driver")
    driver.check_bias_lock = lambda _voltage: None
    package.driver = driver
    return {"b2987b": package, "b2987b.driver": driver}


class TestLivePrimitive(unittest.TestCase):
    def test_progress_callback_selects_live_sweep_and_forwards_timeout(self):
        elec = FakeElectrometer()
        updates = []

        result = P.iv_sweep(
            elec,
            [45.0, 45.2],
            n_per_voltage=2,
            delay_s=0.35,
            progress_cb=lambda *event: updates.append(event),
            timeout_s=987.0,
        )

        self.assertEqual([45.0, 45.2], list(result.avg_source_v))
        self.assertEqual(2, len(updates))
        self.assertEqual(987.0, elec.live_calls[0]["timeout_s"])
        self.assertEqual(0.35, elec.live_calls[0]["delay_s"])


class TestL3SequenceLiveIV(unittest.TestCase):
    def test_sequence_emits_contextual_live_updates_and_records_timeout(self):
        elec = FakeElectrometer()
        run_file = RecordingRunFile()
        events = []
        spec = MeasurementSpec(
            sipm_id=7,
            dark=True,
            illuminated=False,
            do_iv=True,
            do_pulse=False,
            do_scan=False,
            iv_meter="b2987",
            iv_voltages=[45.0, 45.2],
            iv_delay_s=0.35,
            n_iv_samples_dark=2,
        )

        with patch.dict(sys.modules, fake_b2987_modules()):
            summary = run_sequence(
                [spec],
                {"elec": elec},
                SimpleNamespace(stage_deenergize=True),
                run_file=run_file,
                on_iv_progress=lambda *event: events.append(event),
                iv_timeout_s=987.0,
            )

        self.assertEqual(1, summary["n_done"])
        self.assertEqual(987.0, run_file.meta["iv_timeout_s"])
        self.assertEqual(987.0, elec.live_calls[0]["timeout_s"])
        self.assertEqual(
            (0, 1, False, 45.0, [45e-12, 90e-12], [1000.0, 1001.0], 2, 2),
            events[0],
        )


class TestIVLiveAccumulator(unittest.TestCase):
    def test_latest_sample_and_point_mean_std_are_reported_once(self):
        from daq.ivprogress import IVLiveAccumulator

        progress = IVLiveAccumulator()

        latest, summary = progress.add(
            entry_idx=0,
            n_entries=3,
            illuminated=False,
            voltage=45.0,
            currents=[1e-12],
            n_per_voltage=2,
            n_voltages=2,
        )
        self.assertEqual(1, latest.sample_index)
        self.assertEqual(4, latest.sample_total)
        self.assertIsNone(summary)

        latest, summary = progress.add(
            entry_idx=0,
            n_entries=3,
            illuminated=False,
            voltage=45.0,
            currents=[3e-12],
            n_per_voltage=2,
            n_voltages=2,
        )
        self.assertEqual(2, latest.sample_index)
        self.assertEqual(2, latest.voltage_sample_index)
        self.assertAlmostEqual(2e-12, summary.mean_a)
        self.assertAlmostEqual(1e-12, summary.std_a)
        self.assertEqual(2, summary.n)

        _latest, duplicate = progress.add(
            entry_idx=0,
            n_entries=3,
            illuminated=False,
            voltage=45.0,
            currents=[],
            n_per_voltage=2,
            n_voltages=2,
        )
        self.assertIsNone(duplicate)

    def test_new_condition_starts_its_sample_counter_at_one(self):
        from daq.ivprogress import IVLiveAccumulator

        progress = IVLiveAccumulator()
        progress.add(0, 1, False, 45.0, [1e-12], 1, 1)
        latest, _summary = progress.add(0, 1, True, 45.0, [2e-12], 1, 1)

        self.assertEqual(1, latest.sample_index)
        self.assertTrue(latest.illuminated)

    def test_display_text_separates_live_sample_from_point_summary(self):
        from daq.ivprogress import (
            IVLiveAccumulator,
            format_point_summary,
            format_sample_status,
        )

        progress = IVLiveAccumulator()
        progress.add(0, 3, False, 45.0, [1e-12], 2, 2)
        latest, summary = progress.add(0, 3, False, 45.0, [3e-12], 2, 2)

        self.assertEqual(
            "entry 1/3 · dark IV · sample 2/4 · V=45.000 V · I=3.000e-12 A",
            format_sample_status(latest),
        )
        self.assertEqual(
            "entry 1 · dark · V=45.000 V · mean=2.000e-12 A · "
            "std=1.000e-12 A · n=2",
            format_point_summary(summary),
        )

    def test_timeout_must_be_a_positive_number(self):
        from daq.ivprogress import parse_iv_timeout

        self.assertEqual(900.0, parse_iv_timeout("900"))
        for invalid in (None, "bad", 0, -1, "nan", "inf", "-inf"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_iv_timeout(invalid)


if __name__ == "__main__":
    unittest.main()
