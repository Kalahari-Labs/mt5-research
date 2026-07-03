"""Unit tests for the walk-forward fold splitter — the no-look-ahead guarantee.
Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from walkforward import make_folds, Fold, _eligible_combos   # noqa: E402


class TestFoldSplitterNoLookahead(unittest.TestCase):
    def test_oos_strictly_after_is_end(self):
        folds = make_folds(15_000, 3_000, 500, 500)
        self.assertTrue(len(folds) > 0)
        for f in folds:
            # The required guarantee: every OOS window starts strictly AFTER the
            # last in-sample index (is_end is exclusive => last IS idx = is_end-1).
            self.assertGreater(f.oos_start, f.is_end - 1)
            self.assertEqual(f.oos_start, f.is_end)        # no gap, no overlap

    def test_window_sizes_exact(self):
        folds = make_folds(15_000, 3_000, 500, 500)
        for f in folds:
            self.assertEqual(f.is_end - f.is_start, 3_000)
            self.assertEqual(f.oos_end - f.oos_start, 500)

    def test_oos_windows_disjoint_and_contiguous_when_step_eq_oos(self):
        folds = make_folds(15_000, 3_000, 500, 500)
        for a, b in zip(folds, folds[1:]):
            self.assertLessEqual(a.oos_end, b.oos_start + 1)
            self.assertEqual(b.oos_start, a.oos_end)       # tile with no overlap

    def test_fold_count(self):
        # starts at 0,500,...,11500 while start+3500<=15000 -> 24 folds
        self.assertEqual(len(make_folds(15_000, 3_000, 500, 500)), 24)

    def test_generic_params_no_overlap(self):
        for f in make_folds(1_000, 100, 50, 50):
            self.assertGreater(f.oos_start, f.is_end - 1)
            self.assertGreaterEqual(f.oos_start, f.is_end)

    def test_no_folds_when_too_little_data(self):
        self.assertEqual(make_folds(100, 3_000, 500, 500), [])


class TestGrid(unittest.TestCase):
    def test_eligible_combos_require_fast_lt_slow(self):
        combos = _eligible_combos((5, 10, 30), (10, 30, 50))
        for f, s in combos:
            self.assertLess(f, s)
        self.assertNotIn((30, 30), combos)
        self.assertIn((5, 50), combos)


if __name__ == "__main__":
    unittest.main(verbosity=2)
