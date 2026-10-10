"""Gate decision, check orchestration, and override tests (temp ledger only)."""

import functools
import threading
import unittest
from unittest import mock

from click.testing import CliRunner

from gate.gate import GateError, decide, load_latest_check, override, run_check
from gate.stats import wilson_interval
from tests.helpers import TempWorkspace


def summary(**cats):
    out = {"categories": {}}
    for cat, (s, n) in cats.items():
        lo, hi = wilson_interval(s, n)
        out["categories"][cat] = {"n": n, "successes": s, "asr": s / n, "ci_lower": lo, "ci_upper": hi, "agreement": 1.0}
    return out


class DecideTests(unittest.TestCase):
    def test_allow(self):
        base = summary(a=(0, 30), b=(0, 40))
        self.assertEqual(decide(base, summary(a=(0, 30), b=(0, 40)))["decision"], "ALLOW")

    def test_block_threshold(self):
        d = decide(summary(a=(0, 30)), summary(a=(30, 30)))
        self.assertEqual(d["decision"], "BLOCK")
        self.assertEqual(d["per_category"]["a"]["status"], "block_threshold")

    def test_review(self):
        d = decide(summary(a=(0, 6)), summary(a=(0, 6)))
        self.assertEqual(d["decision"], "REVIEW")
        self.assertEqual(d["per_category"]["a"]["status"], "uncertain")

    def test_block_regression(self):
        cand = summary(a=(10, 40))
        self.assertLessEqual(cand["categories"]["a"]["ci_lower"], 0.15)
        d = decide(summary(a=(0, 40)), cand)
        self.assertEqual(d["decision"], "BLOCK")
        self.assertEqual(d["per_category"]["a"]["status"], "block_regression")


class RunCheckTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()

    def tearDown(self):
        self.ws.cleanup()

    def check(self, cfg, trials=10):
        return run_check(
            cfg,
            "configs/v1.yaml",
            trials_per_attack=trials,
            ledger=self.ws.ledger,
            cache_dir=self.ws.cache,
            latest_path=self.ws.latest,
            outbox_log_path=None,
        )

    def test_v1_allow(self):
        r = self.check("configs/v1.yaml")
        self.assertEqual(r["decision"]["decision"], "ALLOW")
        self.assertEqual(r["attribution"]["rows"], [])
        types = [e["entry_type"] for e in self.ws.ledger.read_all()]
        self.assertEqual(types, ["genesis", "check_run", "gate_decision"])
        self.assertEqual(load_latest_check(self.ws.latest)["ledger"]["decision_entry_hash"],
                         r["ledger"]["decision_entry_hash"])
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_v2_block_with_attribution(self):
        r = self.check("configs/v2.yaml")
        self.assertEqual(r["decision"]["decision"], "BLOCK")
        rows = r["attribution"]["rows"]
        self.assertEqual(rows[0]["change_id"], "prompt_section_removed:SECURITY")
        self.assertEqual(rows[0]["delta_overall"], 1.0)
        self.assertEqual(rows[0]["effect"], "regression")
        self.assertEqual(rows[0]["variant_decision"], "BLOCK")
        eff = next(x for x in rows if x["change_id"] == "prompt_section_added:EFFICIENCY")
        self.assertEqual(eff["delta_overall"], 0.0)
        self.assertEqual(eff["effect"], "neutral")

    def test_v3_allow_and_review(self):
        self.assertEqual(self.check("configs/v3.yaml")["decision"]["decision"], "ALLOW")
        self.assertEqual(self.check("configs/v3.yaml", trials=2)["decision"]["decision"], "REVIEW")

    def test_override_flow(self):
        self.check("configs/v2.yaml")
        with self.assertRaises(GateError):
            override("looks fine", "tester", ledger=self.ws.ledger, latest_path=self.ws.latest)
        forced = override("emergency", "tester", force=True, ledger=self.ws.ledger, latest_path=self.ws.latest)
        self.assertTrue(forced["payload"]["forced"])

        r = self.check("configs/v3.yaml", trials=2)
        with self.assertRaises(GateError):
            override("   ", "tester", ledger=self.ws.ledger, latest_path=self.ws.latest)
        entry = override("Reviewed manually", "tester", ledger=self.ws.ledger, latest_path=self.ws.latest)
        self.assertEqual(entry["entry_type"], "override")
        self.assertEqual(entry["payload"]["reason"], "Reviewed manually")
        self.assertEqual(entry["payload"]["decision_entry_hash"], r["ledger"]["decision_entry_hash"])
        self.assertEqual(entry["payload"]["original_decision"], "REVIEW")
        self.assertFalse(entry["payload"]["forced"])
        self.assertEqual(load_latest_check(self.ws.latest)["override"]["entry_hash"], entry["entry_hash"])
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_override_without_decision(self):
        with self.assertRaises(GateError):
            override("reason", "tester", ledger=self.ws.ledger, latest_path=self.ws.latest)

    def _ov(self, reason, force=False):
        return override(reason, "tester", force=force, ledger=self.ws.ledger, latest_path=self.ws.latest)

    def _overrides(self):
        return [e for e in self.ws.ledger.read_all() if e["entry_type"] == "override"]

    def test_override_replay_refused(self):
        r = self.check("configs/v3.yaml", trials=2)
        self.assertEqual(r["decision"]["decision"], "REVIEW")
        self._ov("ok")
        with self.assertRaises(GateError) as cm:
            self._ov("again")
        self.assertIn("already overridden", str(cm.exception))
        with self.assertRaises(GateError):
            self._ov("forced again", force=True)
        matching = [o for o in self._overrides()
                    if o["payload"]["decision_entry_hash"] == r["ledger"]["decision_entry_hash"]]
        self.assertEqual(len(matching), 1)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_new_review_can_be_overridden_once(self):
        first = self.check("configs/v3.yaml", trials=2)
        self._ov("ok")
        with self.assertRaises(GateError):
            self._ov("again")
        second = self.check("configs/v3.yaml", trials=2)
        self.assertEqual(second["decision"]["decision"], "REVIEW")
        self.assertNotEqual(second["ledger"]["decision_entry_hash"], first["ledger"]["decision_entry_hash"])
        entry = self._ov("ok 2")
        self.assertEqual(entry["payload"]["decision_entry_hash"], second["ledger"]["decision_entry_hash"])
        with self.assertRaises(GateError):
            self._ov("again 2")
        overrides = self._overrides()
        self.assertEqual(len(overrides), 2)
        self.assertEqual(len({o["payload"]["decision_entry_hash"] for o in overrides}), 2)
        self.assertEqual(load_latest_check(self.ws.latest)["override"]["entry_hash"], entry["entry_hash"])

    def test_forced_override_replay_refused(self):
        self.assertEqual(self.check("configs/v2.yaml")["decision"]["decision"], "BLOCK")
        self._ov("emergency", force=True)
        with self.assertRaises(GateError):
            self._ov("emergency again", force=True)
        self.assertEqual(len(self._overrides()), 1)

    def test_concurrent_overrides_only_one_wins(self):
        self.check("configs/v3.yaml", trials=2)
        barrier = threading.Barrier(4)
        results = []

        def work(i):
            barrier.wait()
            try:
                self._ov(f"r{i}")
                results.append("ok")
            except GateError:
                results.append("refused")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(sorted(results), ["ok", "refused", "refused", "refused"])
        self.assertEqual(len(self._overrides()), 1)

    def test_cli_override_replay_exits_nonzero(self):
        import cli
        import gate.gate

        self.check("configs/v3.yaml", trials=2)
        runner = CliRunner()
        patched = functools.partial(gate.gate.override, ledger=self.ws.ledger, latest_path=self.ws.latest)
        with mock.patch("cli.override", patched):
            ok = runner.invoke(cli.cli, ["override", "--reason", "ok", "--actor", "tester"])
            self.assertEqual(ok.exit_code, 0, ok.output)
            again = runner.invoke(cli.cli, ["override", "--reason", "again", "--actor", "tester"])
        self.assertEqual(again.exit_code, 1)
        self.assertIn("already overridden", again.output + again.stderr)
        self.assertEqual(len(self._overrides()), 1)


if __name__ == "__main__":
    unittest.main()
