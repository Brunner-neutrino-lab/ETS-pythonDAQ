"""daq.h5browse.filter_files: the Data tab's name filter and row cap."""

import unittest

from daq import h5browse as HB


def _files(*rels):
    return [{"rel": r, "path": "/x/" + r, "size": 0, "mtime": 0.0} for r in rels]


class FilterFilesTest(unittest.TestCase):
    def test_no_query_no_limit_keeps_everything_in_order(self):
        files = _files("b.h5", "a.h5", "sub/c.h5")
        shown, n = HB.filter_files(files)
        self.assertEqual([f["rel"] for f in shown], ["b.h5", "a.h5", "sub/c.h5"])
        self.assertEqual(n, 3)

    def test_query_is_case_insensitive_substring_of_rel(self):
        files = _files("dig_record_slim/QuadC_r4c8.h5", "bench_1.h5", "sub/quadc_x.h5")
        shown, n = HB.filter_files(files, "  QUADC ")
        self.assertEqual([f["rel"] for f in shown],
                         ["dig_record_slim/QuadC_r4c8.h5", "sub/quadc_x.h5"])
        self.assertEqual(n, 2)

    def test_limit_keeps_the_first_rows_and_reports_all_matches(self):
        files = _files(*(f"run_{i:04d}.h5" for i in range(8000)))
        shown, n = HB.filter_files(files, "", 200)
        self.assertEqual(len(shown), 200)
        self.assertEqual(shown[0]["rel"], "run_0000.h5")
        self.assertEqual(shown[-1]["rel"], "run_0199.h5")
        self.assertEqual(n, 8000)

    def test_limit_applies_after_the_filter(self):
        files = _files(*(f"{'a' if i % 2 else 'b'}_{i}.h5" for i in range(10)))
        shown, n = HB.filter_files(files, "a_", 3)
        self.assertEqual([f["rel"] for f in shown], ["a_1.h5", "a_3.h5", "a_5.h5"])
        self.assertEqual(n, 5)

    def test_none_query_and_zero_limit(self):
        files = _files("a.h5", "b.h5")
        self.assertEqual(HB.filter_files(files, None, 0), ([], 2))


if __name__ == "__main__":
    unittest.main()
