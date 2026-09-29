import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SUBMODULE = Path(__file__).resolve().parents[1] / "keysight2987b-python"
sys.path.insert(0, str(SUBMODULE))

from b2987b.controller import B2987BController  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeDriver:
    def __init__(self, clock):
        self.clock = clock
        self.acquired = 0
        self.ammeter_is_off = False
        self.output_is_off = False

    def reset_stop(self): pass
    def _guard_hv(self, _voltages): pass
    def set_source_range(self, _value): pass
    def set_current_limit(self, _value): pass
    def configure_current_sense(self, *_args, **_kwargs): pass
    def set_voltage(self, _value): pass
    def output_on(self): pass
    def ammeter_on(self): pass
    def check_stop(self): pass
    def write_level(self, _value): pass

    def acquire_current(self, n, delay_s=0.0, timeout_s=600.0):
        self.clock.now += 0.3
        self.acquired += n
        return {
            "current_a": [1e-12] * n,
            "timestamp_s": [1000.0 + self.acquired] * n,
        }

    def ammeter_off(self):
        self.ammeter_is_off = True

    def output_off(self):
        self.output_is_off = True


class TestLiveSweepTimeout(unittest.TestCase):
    def test_overall_deadline_stops_between_acquisition_chunks(self):
        clock = FakeClock()
        driver = FakeDriver(clock)
        controller = B2987BController(mode="simulation")
        controller._driver = driver
        controller._current_aperture_mode = "FIXED"
        controller._current_aperture = 2.0

        with patch("b2987b.controller.time.monotonic", clock.monotonic), \
             patch("b2987b.controller.time.sleep", clock.sleep):
            with self.assertRaisesRegex(TimeoutError, "1.0 s"):
                controller.sweep_live(
                    [45.0] * 10,
                    n_per_voltage=1,
                    delay_s=0.0,
                    timeout_s=1.0,
                )

        self.assertEqual(2, driver.acquired)
        self.assertTrue(driver.ammeter_is_off)
        self.assertTrue(driver.output_is_off)
        self.assertFalse(controller._bias_active)


if __name__ == "__main__":
    unittest.main()
