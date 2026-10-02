import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import h5py
import numpy as np

_REPO = Path(__file__).resolve().parents[1]
for _pkg in ("keysight2987b-python", "vx2740-python"):
    sys.path.insert(0, str(_REPO / _pkg))

from daq import characterization as CH
from daq import h5browse, labbook
from daq.sequence import (MeasurementSpec, SequenceFile, load_sequence,
                          pulse_threshold_for, save_sequence, sequence_hash,
                          spec_to_dict)

# Truth for the fake bench: MUX channel -> (V_BD, A0 in ADC/V).
TRUTH = {1: (45.10, 290.0), 2: (45.35, 305.0), 23: (44.90, 280.0)}


class FakeElec:
    def __init__(self):
        self.v = 0.0
        self.on = False
        self.log = []

    def set_bias(self, v, settle_s=0.0):
        self.v, self.on = float(v), True
        self.log.append(("set", float(v)))

    def bias_off(self):
        self.on = False
        self.log.append(("off",))


class FakeMux:
    def __init__(self):
        self.ch = None

    def select(self, ch, settle_s=0.05):
        self.ch = int(ch)


class FakeCtrl:
    """Self-triggered SPE pulses whose amplitude follows TRUTH for the
    selected MUX channel; 10 % of triggers are 2 PE (crosstalk)."""

    def __init__(self, elec, mux, n_short=None):
        self.elec, self.mux = elec, mux
        self.rng = np.random.default_rng(1)
        self.n_short = n_short

    def configure_record_window(self, pre_us, post_us):
        self.pre = int(round(pre_us * 125))
        self.n_samples = self.pre + int(round(post_us * 125))

    def configure_channels(self, sipm_channels, thresholds, threshold_mode,
                           include_pmt):
        self.ch = sipm_channels[0]
        self.thr = thresholds[self.ch]

    def configure_trigger(self, mode):
        self.mode = mode

    def arm(self):
        assert self.elec.on, "acquiring with the bias off"

    def disarm(self):
        pass

    def acquire(self, n, batch_size, store_waveforms, timeout_s):
        vbd, a0 = TRUTH[self.mux.ch]
        spe = a0 * (self.elec.v - vbd)
        n_got = n if self.n_short is None else self.n_short
        pe = np.where(self.rng.random(n_got) < 0.1, 2, 1)
        amps = (pe * spe * self.rng.normal(1, 0.05, n_got)).astype(np.float32)
        t = np.arange(self.n_samples)
        shape = np.exp(-0.5 * ((t - self.pre - 125) / 50.0) ** 2)
        waves = (32768 + amps[:, None] * shape[None, :]).astype(np.uint16)
        return SimpleNamespace(amplitudes={self.ch: amps},
                               timestamps={self.ch: np.arange(n_got) * 5e-3},
                               waveforms={self.ch: waves}, n_waveforms=n_got)


class TempDirs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        p = patch.object(CH, "CHAR_DIR", self.tmp / "characterization")
        p.start()
        self.addCleanup(p.stop)


class AnalysisTests(unittest.TestCase):
    def test_two_point_fit_is_exact(self):
        fit = CH.fit_gain([48.0, 49.0], [870.0, 1160.0], [10.0, 10.0])
        self.assertTrue(fit["ok"])
        self.assertAlmostEqual(fit["a0"], 290.0)
        self.assertAlmostEqual(fit["vbd"], 48.0 - 870.0 / 290.0)

    def test_two_point_errors_match_propagation(self):
        v1, v2, a1, a2, e = 48.0, 49.0, 870.0, 1160.0, 10.0
        fit = CH.fit_gain([v1, v2], [a1, a2], [e, e])
        self.assertAlmostEqual(fit["a0_err"], math.sqrt(2) * e / (v2 - v1))
        # V_BD = v1 - a1 / A0, differentiated w.r.t. a1 and a2
        a0 = (a2 - a1) / (v2 - v1)
        d1 = -1 / a0 - a1 / a0 ** 2 / (v2 - v1)
        d2 = a1 / a0 ** 2 / (v2 - v1)
        self.assertAlmostEqual(fit["vbd_err"], math.hypot(d1 * e, d2 * e))

    def test_falling_amplitude_is_no_fit(self):
        self.assertFalse(CH.fit_gain([48, 49], [900, 800], [5, 5])["ok"])
        self.assertFalse(CH.fit_gain([48], [900], [5])["ok"])

    def test_median_summary_ignores_tail(self):
        rng = np.random.default_rng(0)
        a = np.concatenate([rng.normal(1000, 50, 900), rng.normal(2000, 80, 100)])
        s = CH.amplitude_summary(a)
        self.assertLess(abs(s["median"] - 1000), 30)
        self.assertLess(abs(s["sigma"] - 50), 15)
        self.assertTrue(np.isnan(CH.amplitude_summary([])["median"]))

    def test_parse_channels(self):
        self.assertEqual(CH.parse_channels("1-3, 23-24 2"), [1, 2, 3, 23, 24])
        self.assertEqual(len(CH.parse_channels(CH.DEFAULT_MUX_CHANNELS)), 24)
        with self.assertRaises(ValueError):
            CH.parse_channels("1-x")


class TableTests(TempDirs):
    def test_new_edit_history_and_csv(self):
        CH.new_table("t1_q1_165K", temperature_K=165.0, mux_channels=[1, 2])
        with self.assertRaises(FileExistsError):
            CH.new_table("t1_q1_165K")
        CH.edit_row("t1_q1_165K", 1, "lei", vbd_v="45.1", a0_adc_per_v=290)
        CH.edit_row("t1_q1_165K", 1, "riya", vbd_v=45.2, feedthrough="J3-07")
        row = CH.get_row(CH.load_table("t1_q1_165K"), 1)
        self.assertEqual(row["vbd_v"], 45.2)
        self.assertEqual(row["feedthrough"], "J3-07")
        self.assertEqual(row["source"], "entered by riya")
        self.assertEqual(row["history"][-1]["vbd_v"], 45.1)

        csv_text = CH.table_to_csv(CH.load_table("t1_q1_165K"))
        self.assertIn("spe_ov3_adc", csv_text.splitlines()[0])
        edited = csv_text.replace("J3-07", "J3-08")
        self.assertEqual(CH.merge_csv("t1_q1_165K", edited, "lucas"), 2)
        row = CH.get_row(CH.load_table("t1_q1_165K"), 1)
        self.assertEqual(row["feedthrough"], "J3-08")
        self.assertEqual(row["vbd_v"], 45.2)        # unchanged value, no new history
        self.assertEqual(len(row["history"]), 1)

    def test_coarse_specs_follow_spe(self):
        CH.new_table("t", temperature_K=165.0, mux_channels=[1, 2])
        CH.edit_row("t", 1, "x", vbd_v=45.0, a0_adc_per_v=300.0, dig_ch=5)
        specs, skipped = CH.coarse_specs(CH.load_table("t"), ov_v=[2, 3, 4, 5, 6])
        self.assertEqual(len(specs), 1)
        self.assertEqual(skipped, [(2, "no V_BD/A0")])
        s = specs[0]
        self.assertEqual(s.pulse_bias_v, [47.0, 48.0, 49.0, 50.0, 51.0])
        self.assertEqual(s.pulse_capture_ch, 5)
        self.assertEqual(s.temperature_K, 165.0)
        self.assertEqual([pulse_threshold_for(s, v) for v in s.pulse_bias_v],
                         [300, 450, 600, 750, 900])


class RunTests(TempDirs):
    def _run(self, channels, n_short=None, abort=None, **kw):
        elec, mux = FakeElec(), FakeMux()
        ctrl = FakeCtrl(elec, mux, n_short=n_short)
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=ctrl)}
        CH.new_table("bench", temperature_K=165.0, mux_channels=[])
        plan = CH.InitialPlan(table="bench", channels=channels, vbd_est_v=45.0,
                              n_waveforms=200, threshold_adc=450,
                              settle_s=0.0, **kw)
        h5 = str(self.tmp / "initial.h5")
        with patch("b2987b.driver.check_bias_lock", lambda v: None):
            results = CH.run_initial(plan, inst, h5_path=h5, abort=abort)
        return results, plan, elec, h5

    def test_recovers_gain_and_records_everything(self):
        results, plan, elec, h5 = self._run([(1, 1), (2, 2), (23, 23)])
        for r in results:
            vbd, a0 = TRUTH[r["mux_ch"]]
            self.assertTrue(r["fit"]["ok"])
            self.assertLess(abs(r["fit"]["vbd"] - vbd), 0.1)
            self.assertLess(abs(r["fit"]["a0"] - a0) / a0, 0.03)
            self.assertEqual(r["flags"], [])
            self.assertEqual(r["points"][0]["samples"].shape,
                             (CH.N_SAMPLE_WAVEFORMS, 1500))
        self.assertEqual(elec.log[-1], ("off",))
        self.assertFalse(elec.on)

        table = CH.load_table("bench")
        row = CH.get_row(table, 23)
        self.assertAlmostEqual(row["vbd_v"], results[2]["fit"]["vbd"], places=3)
        self.assertEqual(len(row["initial"]["points"]), 2)
        import csv, io
        rec = [r for r in csv.DictReader(io.StringIO(CH.table_to_csv(table)))
               if r["mux_ch"] == "23"][0]
        self.assertEqual(float(rec["bias2_v"]), 49.0)
        self.assertAlmostEqual(float(rec["median1_adc"]),
                               results[2]["points"][0]["median"], places=0)
        self.assertEqual(rec["initial_threshold_adc"], "450")

        with h5py.File(h5, "r") as f:
            self.assertEqual(f.attrs["measurement_type"], "initial_characterization")
            g = f["mux02/48000mV/ch2"]
            self.assertEqual(g["amplitudes_adc"].shape, (200,))
            self.assertEqual(g["waveforms"].shape, (200, 1500))
            self.assertAlmostEqual(f["mux02"].attrs["a0_adc_per_v"],
                                   results[1]["fit"]["a0"])

    def test_timeout_is_flagged(self):
        results, *_ = self._run([(1, 1)], n_short=50)
        self.assertIn("50/200 waveforms", " ".join(results[0]["flags"]))

    def test_threshold_far_from_half_spe_is_flagged(self):
        elec, mux = FakeElec(), FakeMux()
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=FakeCtrl(elec, mux))}
        CH.new_table("bench2", mux_channels=[])
        plan = CH.InitialPlan(table="bench2", channels=[(1, 1)], vbd_est_v=45.0,
                              n_waveforms=200, threshold_adc=1000, settle_s=0.0)
        with patch("b2987b.driver.check_bias_lock", lambda v: None):
            r = CH.run_initial(plan, inst)[0]
        self.assertTrue(any("re-run with" in f for f in r["flags"]))

    def test_implausible_fit_stays_out_of_the_table(self):
        CH.new_table("bench4", mux_channels=[1])
        CH.edit_row("bench4", 1, "x", vbd_v=45.1, a0_adc_per_v=290)
        elec, mux = FakeElec(), FakeMux()
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=FakeCtrl(elec, mux))}
        # estimate 3 V off: the fit itself is fine, but too far to trust
        plan = CH.InitialPlan(table="bench4", channels=[(1, 1)], vbd_est_v=48.0,
                              n_waveforms=200, threshold_adc=1300, settle_s=0.0)
        with patch("b2987b.driver.check_bias_lock", lambda v: None):
            r = CH.run_initial(plan, inst)[0]
        self.assertTrue(r["fit"]["ok"])
        self.assertIn("not stored in the table", r["flags"])
        row = CH.get_row(CH.load_table("bench4"), 1)
        self.assertEqual(row["vbd_v"], 45.1)
        self.assertEqual(row["initial"]["flags"], r["flags"])

    def test_coarse_skips_non_positive_gain(self):
        CH.new_table("bench5", mux_channels=[1])
        CH.edit_row("bench5", 1, "x", vbd_v=-603.3, a0_adc_per_v=1.4)
        specs, skipped = CH.coarse_specs(CH.load_table("bench5"), ov_v=[2, 3])
        self.assertEqual(specs, [])
        self.assertEqual(skipped, [(1, "V_BD or A0 not positive")])

    def test_stop_mid_channel_records_nothing(self):
        abort = {"flag": False}

        class StopAfterFirst(FakeCtrl):
            def acquire(self, *a, **k):
                abort["flag"] = True
                return super().acquire(*a, **k)

        elec, mux = FakeElec(), FakeMux()
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=StopAfterFirst(elec, mux))}
        CH.new_table("bench3", mux_channels=[])
        plan = CH.InitialPlan(table="bench3", channels=[(1, 1), (2, 2)],
                              vbd_est_v=45.0, settle_s=0.0)
        with patch("b2987b.driver.check_bias_lock", lambda v: None):
            results = CH.run_initial(plan, inst, abort=abort)
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["fit"])
        self.assertIsNone(CH.get_row(CH.load_table("bench3"), 1))
        self.assertFalse(elec.on)

    def test_bias_lock_refusal_moves_nothing(self):
        elec, mux = FakeElec(), FakeMux()
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=FakeCtrl(elec, mux))}
        plan = CH.InitialPlan(table="none", channels=[(1, 1)], vbd_est_v=60.0)

        def refuse(v):
            raise RuntimeError("bias lock")
        with patch("b2987b.driver.check_bias_lock", refuse):
            with self.assertRaises(RuntimeError):
                CH.run_initial(plan, inst)
        self.assertEqual(elec.log, [])
        self.assertIsNone(mux.ch)


class JobAndLabbookTests(TempDirs):
    def setUp(self):
        super().setUp()
        attach = self.tmp / "attach"
        attach.mkdir()
        for name, value in (("_ENTRIES_PATH", str(self.tmp / "e.jsonl")),
                            ("_HISTORY_PATH", str(self.tmp / "h.jsonl")),
                            ("_ATTACH_DIR", str(attach))):
            p = patch.object(labbook, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_job_posts_one_entry_with_plots(self):
        elec, mux = FakeElec(), FakeMux()
        inst = {"elec": elec, "ivmux": mux,
                "digitizer": SimpleNamespace(_ctrl=FakeCtrl(elec, mux))}
        CH.new_table("tile1_q1_165K", temperature_K=165.0, mux_channels=[1, 2])
        plan = CH.InitialPlan(table="tile1_q1_165K", channels=[(1, 1), (2, 2)],
                              vbd_est_v=45.0, threshold_adc=450, settle_s=0.0,
                              temperature_K=165.0, user="lei")
        job = CH.InitialCharJob()
        self.assertTrue(job.claim(plan))
        self.assertFalse(job.claim(plan))
        posted = []

        def on_finish(j):
            j.labbook_id = CH.post_labbook(j)
            posted.append(j.labbook_id)

        with patch("b2987b.driver.check_bias_lock", lambda v: None):
            job.run_claimed(inst, on_finish=on_finish)
        snap = job.snapshot()
        self.assertEqual(snap["status"], "done")
        self.assertFalse(snap["running"])
        self.assertEqual(snap["progress"], 1.0)
        entry = labbook.get(posted[0])
        self.assertEqual(entry["user"], "lei")
        self.assertIn("tile1_q1_165K", entry["subject"])
        self.assertIn("MUX 2 (dig 2): V_BD", entry["body"])
        self.assertEqual(len(entry["attachments"]), 3)   # summary + 2 channels
        for a in entry["attachments"]:
            with open(os.path.join(labbook.attachments_dir(), a), "rb") as f:
                self.assertEqual(f.read(4), b"\x89PNG")
        self.assertTrue(Path(job.h5_path).is_file())


class SequenceTests(unittest.TestCase):
    def test_unset_gain_fields_keep_old_hash(self):
        import dataclasses
        import hashlib
        spec = MeasurementSpec(sipm_id=3, mux_channel=3, do_pulse=True,
                               pulse_bias_v=[49.0])
        old = dataclasses.asdict(spec)
        for k in ("vbd_v", "spe_adc_per_v", "pulse_threshold_pe"):
            del old[k]
        blob = json.dumps([old], sort_keys=True)
        self.assertEqual(sequence_hash([spec]),
                         hashlib.sha1(blob.encode()).hexdigest()[:12])
        self.assertNotIn("vbd_v", spec_to_dict(spec))

    def test_threshold_rule_and_yaml_round_trip(self):
        spec = MeasurementSpec(pulse_threshold_adc=77, pulse_bias_v=[44.0, 48.0],
                               vbd_v=45.0, spe_adc_per_v=300.0,
                               pulse_threshold_pe=0.5)
        self.assertEqual(pulse_threshold_for(spec, 48.0), 450)
        self.assertEqual(pulse_threshold_for(spec, 44.0), 77)   # below V_BD
        plain = MeasurementSpec(pulse_threshold_adc=77)
        self.assertEqual(pulse_threshold_for(plain, 48.0), 77)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.yaml")
            save_sequence(SequenceFile(entries=[spec, plain]), p)
            back = load_sequence(p).entries
        self.assertEqual(back[0].spe_adc_per_v, 300.0)
        self.assertIsNone(back[1].vbd_v)
        self.assertEqual(sequence_hash(back), sequence_hash([spec, plain]))


class AmplitudeReadTests(unittest.TestCase):
    def test_volts_from_vx2740_come_back_as_adc(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.h5")
            with h5py.File(p, "w") as f:
                g = f.create_group("seq/0/1/165.0K/dark/pulse/49000mV")
                g.attrs["source"] = "vx2740"
                g.create_dataset("ch1/amplitudes_v",
                                 data=np.array([1000, 2000], np.float32) / 32768)
                f.create_dataset("rto/amplitudes_v", data=[0.01, 0.02])
                f.create_dataset("x/amplitudes_adc", data=[5.0])
            vals, unit = h5browse.read_amplitudes(
                p, "/seq/0/1/165.0K/dark/pulse/49000mV/ch1/amplitudes_v")
            self.assertEqual(unit, "ADC")
            np.testing.assert_allclose(vals, [1000, 2000], rtol=1e-6)
            self.assertEqual(h5browse.read_amplitudes(p, "/rto/amplitudes_v")[1], "V")
            self.assertEqual(h5browse.read_amplitudes(p, "/x/amplitudes_adc")[1], "ADC")
        self.assertTrue(h5browse.is_amplitude_dataset("/a/ch1/amplitudes_v"))
        self.assertFalse(h5browse.is_amplitude_dataset("/a/ch1/timestamps_s"))


if __name__ == "__main__":
    unittest.main()
