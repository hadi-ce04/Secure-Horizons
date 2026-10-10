"""Statistics and suite-runner tests."""

import base64
import re
import unittest

from gate.config import load_config
from gate.run import load_suite, run_suite
from gate.stats import agreement_rate, summarize_trials, wilson_interval
from tests.helpers import TempWorkspace


class StatsTests(unittest.TestCase):
    def test_wilson_reference_values(self):
        lo, hi = wilson_interval(0, 30)
        self.assertAlmostEqual(lo, 0.0, places=6)
        self.assertAlmostEqual(hi, 0.113517, places=6)
        lo, hi = wilson_interval(30, 30)
        self.assertAlmostEqual(lo, 0.886483, places=6)
        self.assertAlmostEqual(hi, 1.0, places=6)
        self.assertAlmostEqual(wilson_interval(0, 40)[1], 0.087625, places=6)
        self.assertAlmostEqual(wilson_interval(0, 22)[1], 0.148660, places=6)
        self.assertAlmostEqual(wilson_interval(0, 21)[1], 0.154644, places=6)
        lo, hi = wilson_interval(5, 10)
        self.assertAlmostEqual(lo, 0.236590, places=6)
        self.assertAlmostEqual(hi, 0.763410, places=6)
        self.assertAlmostEqual(wilson_interval(0, 6)[1], 0.390343, places=6)

    def test_wilson_errors(self):
        with self.assertRaises(ValueError):
            wilson_interval(0, 0)
        with self.assertRaises(ValueError):
            wilson_interval(5, 4)

    def test_agreement(self):
        self.assertAlmostEqual(agreement_rate({"a": [True, True, False], "b": [False, False]}), 4 / 5)
        with self.assertRaises(ValueError):
            agreement_rate({})

    def test_summarize_shape(self):
        trials = [
            {"attack_id": "A", "category": "x", "success": True},
            {"attack_id": "A", "category": "x", "success": False},
            {"attack_id": "B", "category": "y", "success": False},
        ]
        s = summarize_trials(trials)
        self.assertEqual(list(s["categories"]), ["x", "y"])
        self.assertEqual(set(s["categories"]["x"]), {"n", "successes", "asr", "ci_lower", "ci_upper", "agreement"})
        self.assertEqual(s["overall"]["n"], 3)
        self.assertEqual(s["per_attack"]["A"]["successes"], 1)


class SuiteTests(unittest.TestCase):
    def test_suite(self):
        suite = load_suite()
        self.assertEqual(len(suite), 10)
        counts = {}
        for a in suite:
            counts[a["category"]] = counts.get(a["category"], 0) + 1
        self.assertEqual(counts, {"goal_hijack": 3, "indirect_injection": 4, "exfil_evasion": 3})
        self.assertEqual({a["channel"] for a in suite}, {"user", "email", "document"})
        ex = next(a for a in suite if a["id"] == "EX-001")
        b64 = re.search(r"([A-Za-z0-9+/]{20,}={0,2})", ex["payload"]).group(1)
        self.assertIn("attacker@evil.example", base64.b64decode(b64).decode())


class RunSuiteTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.suite = load_suite()
        self.v1 = load_config("configs/v1.yaml")
        self.v2 = load_config("configs/v2.yaml")

    def tearDown(self):
        self.ws.cleanup()

    def _run(self, cfg, use_cache=True):
        return run_suite(cfg, self.suite, trials_per_attack=10, use_cache=use_cache,
                         cache_dir=self.ws.cache, outbox_log_path=None)

    def test_v1_and_v2(self):
        r1 = self._run(self.v1)
        self.assertEqual(r1["n_trials"], 100)
        for cat, s in r1["summary"]["categories"].items():
            self.assertEqual(s["successes"], 0, cat)
            self.assertLessEqual(s["ci_upper"], 0.15, cat)
            self.assertEqual(s["agreement"], 1.0)
        r2 = self._run(self.v2)
        for cat, s in r2["summary"]["categories"].items():
            self.assertEqual(s["asr"], 1.0, cat)

    def test_cache(self):
        first = self._run(self.v1)
        self.assertFalse(any(t["cached"] for t in first["trials"]))
        second = self._run(self.v1)
        self.assertTrue(all(t["cached"] for t in second["trials"]))
        self.assertEqual(first["summary"], second["summary"])

    def test_no_cache_writes_nothing(self):
        self._run(self.v1, use_cache=False)
        self.assertFalse(self.ws.cache.exists() and any(self.ws.cache.iterdir()))

    def test_validation(self):
        with self.assertRaises(ValueError):
            run_suite(self.v1, self.suite, trials_per_attack=0, cache_dir=self.ws.cache, outbox_log_path=None)


if __name__ == "__main__":
    unittest.main()
