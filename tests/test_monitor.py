"""Monitor / staleness tests (temp ledger only)."""

import unittest
from datetime import datetime, timedelta, timezone

from gate.config import load_config
from monitor.schedule import detect_drift, run_monitor_loop, run_monitor_once
from tests.helpers import REPO_ROOT, TempWorkspace


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.kw = dict(ledger=self.ws.ledger, cache_dir=self.ws.cache, outbox_log_path=None, trials_per_attack=3)

    def tearDown(self):
        self.ws.cleanup()

    def kinds(self, res):
        return [a["kind"] for a in res["alerts"]]

    def test_first_then_clean_then_stale(self):
        first = run_monitor_once("configs/v1.yaml", **self.kw)
        self.assertEqual(self.kinds(first), ["no_evaluation"])
        second = run_monitor_once("configs/v1.yaml", **self.kw)
        self.assertEqual(self.kinds(second), [])
        # Explicit "now" one second ahead: Windows' ~15.6 ms wall-clock tick can
        # otherwise make the age exactly 0, and 0 > max_age_hours=0 is False.
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        stale = run_monitor_once("configs/v1.yaml", max_age_hours=0, now=later, **self.kw)
        self.assertIn("stale_evaluation", self.kinds(stale))
        types = [e["entry_type"] for e in self.ws.ledger.read_all()]
        self.assertEqual(types.count("monitor_run"), 3)
        self.assertEqual(types.count("staleness_alert"), 2)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_config_changed(self):
        path = self.ws.root / "agent_config.yaml"
        path.write_text((REPO_ROOT / "configs" / "v1.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        run_monitor_once(str(path), **self.kw)
        path.write_text((REPO_ROOT / "configs" / "v2.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        res = run_monitor_once(str(path), **self.kw)
        self.assertIn("config_changed", self.kinds(res))
        self.assertIn("no_evaluation", self.kinds(res))

    def test_drift(self):
        cfg = load_config("configs/v1.yaml")
        run_monitor_once("configs/v1.yaml", **self.kw)
        new_summary = {"categories": {"goal_hijack": {"asr": 0.5}}}
        alerts = detect_drift(cfg, "offline", new_summary, ledger=self.ws.ledger)
        self.assertEqual([a["kind"] for a in alerts], ["asr_drift"])

    def test_loop(self):
        lines = []
        sleeps = []
        results = run_monitor_loop("configs/v1.yaml", interval_seconds=0, iterations=2,
                                   printer=lines.append, sleep=sleeps.append, **self.kw)
        self.assertEqual(len(results), 2)
        self.assertEqual(sleeps, [0])
        self.assertTrue(any(line.startswith("ALERT [no_evaluation]") for line in lines))


if __name__ == "__main__":
    unittest.main()
