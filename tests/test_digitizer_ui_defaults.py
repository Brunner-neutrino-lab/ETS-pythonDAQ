import unittest

from daq.webgui import shell


class DigitizerUiDefaultsTest(unittest.TestCase):
    def test_all_channels_start_disabled_with_1000_adc_threshold(self):
        make_state = getattr(shell, "_digitizer_initial_channel_state", None)
        self.assertIsNotNone(
            make_state,
            "digitizer UI must expose its initial channel-state builder",
        )

        state = make_state()

        self.assertEqual(set(state), set(range(64)))
        self.assertEqual(
            [channel for channel, values in state.items() if values["enable"]],
            [],
        )
        self.assertEqual(
            {values["threshold"] for values in state.values()},
            {1000},
        )

    def test_sipm_roles_have_no_pmt_special_case(self):
        channel_tag = getattr(shell, "_digitizer_channel_tag", None)
        self.assertIsNotNone(
            channel_tag,
            "digitizer UI must expose its channel-role mapping",
        )

        expected_sipm_channels = set(range(1, 24)) | {27}
        actual_sipm_channels = {
            channel for channel in range(64)
            if channel_tag(channel) == "SiPM"
        }

        self.assertEqual(actual_sipm_channels, expected_sipm_channels)
        self.assertNotIn("PMT", {channel_tag(channel) for channel in range(64)})


if __name__ == "__main__":
    unittest.main()
