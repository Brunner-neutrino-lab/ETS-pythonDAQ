import unittest

from daq.config import ExperimentConfig
from daq.slowcontrol import SlowControl


class FakeRecord:
    def __init__(self, measurement, channel, value):
        self.values = {"_measurement": measurement, "channel": channel}
        self._value = value

    def get_measurement(self):
        return self.values["_measurement"]

    def get_value(self):
        return self._value


class FakeTable:
    def __init__(self, records):
        self.records = records


class FakeQueryAPI:
    def __init__(self, outinput, rtds):
        self.outinput = outinput
        self.rtds = rtds
        self.flux = None

    def query(self, flux, org=None):
        self.flux = flux
        tables = [FakeTable([FakeRecord("rtd", ch, v)])
                  for ch, v in self.rtds.items()]
        if self.outinput is not None:
            tables.append(FakeTable([FakeRecord("outinput", "1", self.outinput)]))
        return tables


RTDS = {"A": 159.0, "B": 165.8, "C": 156.4, "D": 183.8}


def make_sc(outinput, rtds=RTDS):
    sc = SlowControl(ExperimentConfig())
    sc._api = FakeQueryAPI(outinput, rtds)
    return sc


class PVReadingTest(unittest.TestCase):
    def test_follows_control_input(self):
        for code, ch in ((1, "A"), (2, "B"), (3, "C"), (4, "D")):
            with self.subTest(code=code):
                sc = make_sc(float(code))
                self.assertEqual(sc.pv_reading(), (ch, RTDS[ch]))
                self.assertEqual(sc.temperature_K(), RTDS[ch])

    def test_queries_configured_loop(self):
        sc = make_sc(2.0)
        sc._cfg.influxdb_pv_loop = 2
        sc.pv_reading()
        self.assertIn('r["channel"] == "2"', sc._api.flux)

    def test_missing_outinput_raises(self):
        with self.assertRaisesRegex(RuntimeError, "PV channel is unknown"):
            make_sc(None).pv_reading()

    def test_no_control_input_raises(self):
        with self.assertRaisesRegex(RuntimeError, "input is 0"):
            make_sc(0.0).pv_reading()

    def test_scanner_input_raises(self):
        with self.assertRaisesRegex(RuntimeError, "not one of A-D"):
            make_sc(5.0).pv_reading()

    def test_pv_channel_without_data_raises(self):
        with self.assertRaisesRegex(RuntimeError, "PV channel B"):
            make_sc(2.0, {"A": 159.0}).pv_reading()

    def test_celsius_conversion(self):
        sc = make_sc(2.0, {"B": -107.35})
        sc._cfg.influxdb_rtd_in_kelvin = False
        ch, T = sc.pv_reading()
        self.assertEqual(ch, "B")
        self.assertAlmostEqual(T, 165.8)


if __name__ == "__main__":
    unittest.main()
