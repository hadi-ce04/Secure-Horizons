"""interaction_summary: regressions that no single change reproduces alone."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gate.attribute import interaction_summary  # noqa: E402


def _rows(*deltas: float) -> list[dict]:
    return [{"change_id": f"c{i}", "delta_overall": d} for i, d in enumerate(deltas)]


class InteractionSummaryTests(unittest.TestCase):
    def test_flags_when_single_changes_are_neutral_but_candidate_regresses(self):
        s = interaction_summary(0.0, 0.5, _rows(0.0, 0.0))
        self.assertTrue(s["flag"])
        self.assertAlmostEqual(s["candidate_delta_overall"], 0.5)
        self.assertAlmostEqual(s["unexplained_delta"], 0.5)
        self.assertIn("combination", s["message"])
        self.assertIn("c0, c1", s["message"])

    def test_not_flagged_when_one_change_explains_it(self):
        s = interaction_summary(0.0, 0.15, _rows(0.10, 0.0))
        self.assertFalse(s["flag"])
        self.assertEqual(s["message"], "")
        self.assertAlmostEqual(s["best_single_change_delta"], 0.10)

    def test_not_flagged_without_regression(self):
        self.assertFalse(interaction_summary(0.0, 0.0, _rows(0.0, 0.0))["flag"])
        self.assertFalse(interaction_summary(0.2, 0.1, _rows(-0.1, 0.0))["flag"])

    def test_not_flagged_with_single_change(self):
        # One change cannot "interact"; a gap there is sampling noise, not an interaction.
        self.assertFalse(interaction_summary(0.0, 0.5, _rows(0.0))["flag"])

    def test_no_rows(self):
        s = interaction_summary(0.0, 0.3, [])
        self.assertFalse(s["flag"])
        self.assertEqual(s["best_single_change_delta"], 0.0)


if __name__ == "__main__":
    unittest.main()
