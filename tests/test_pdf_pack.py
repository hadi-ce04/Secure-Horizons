"""Evidence bundles per check, historical pack lookup, and the audit PDF (offline, isolated ledger)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evidence.pack import generate_pack  # noqa: E402
from gate.gate import GateError, list_checks, load_check, run_check  # noqa: E402
from ledger.ledger import Ledger  # noqa: E402


class PdfPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ledger = Ledger(chain_path=root / "chain.jsonl", keys_dir=root / "keys")
        self.ledger.ensure_keys()
        self.ledger.init_genesis()
        self.cache = root / "cache"
        self.checks = root / "checks"
        self.packs = root / "packs"
        self.latest = root / "latest.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _check(self, cfg: str, trials: int = 3) -> dict:
        return run_check(f"configs/{cfg}.yaml", baseline_path="configs/v1.yaml", mode="offline", trials_per_attack=trials,
                         ledger=self.ledger, cache_dir=self.cache, latest_path=self.latest, outbox_log_path=None,
                         checks_dir=self.checks, run_attribution=False)

    def test_bundle_written_per_check_and_loadable_by_hash_or_index(self):
        r2 = self._check("v2")
        r3 = self._check("v3")
        bundles = list_checks(self.checks)
        self.assertEqual(len(bundles), 2)
        b = load_check(r2["ledger"]["check_entry_hash"][:12], ledger=self.ledger, checks_dir=self.checks)
        self.assertEqual(b["candidate"]["name"], "v2-frictionless")
        self.assertIn("suite", b["evidence"])
        self.assertEqual(len(b["evidence"]["trials"]["candidate"]), 16 * 3)
        self.assertIn("## SECURITY", b["evidence"]["configs"]["baseline"]["system_prompt"])
        # by ledger index of the gate_decision entry
        b3 = load_check(str(r3["ledger"]["decision_index"]), ledger=self.ledger, checks_dir=self.checks)
        self.assertEqual(b3["candidate"]["name"], "v3-signature")
        with self.assertRaises(GateError):
            load_check("deadbeef", ledger=self.ledger, checks_dir=self.checks)

    def test_trial_records_persist_even_without_cache_reads(self):
        run_check("configs/v2.yaml", baseline_path="configs/v1.yaml", mode="offline", trials_per_attack=2, use_cache=False,
                  ledger=self.ledger, cache_dir=self.cache, latest_path=self.latest, outbox_log_path=None,
                  checks_dir=self.checks, run_attribution=False)
        self.assertTrue(any(self.cache.glob("*.json")))

    def test_pdf_pack_for_historical_check(self):
        r2 = self._check("v2")
        self._check("v3")  # latest is now v3; we ask for v2 explicitly
        res = generate_pack(ledger=self.ledger, latest_path=self.latest, packs_dir=self.packs, cache_dir=self.cache, checks_dir=self.checks,
                            check_ref=r2["ledger"]["check_entry_hash"][:16], pdf=True)
        self.assertEqual(res["decision"], "BLOCK")
        self.assertEqual(res["check_entry_hash"], r2["ledger"]["check_entry_hash"])
        pdf = Path(res["pdf_path"])
        self.assertTrue(pdf.exists())
        self.assertGreater(pdf.stat().st_size, 20_000)
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertIn("pack.pdf", res["sha256"])
        last = self.ledger.read_all()[-1]
        self.assertEqual(last["entry_type"], "pack_generated")
        self.assertEqual(last["payload"]["check_entry_hash"], r2["ledger"]["check_entry_hash"])
        self.assertIn("pack.pdf", last["payload"]["files"])
        self.assertTrue(last["payload"]["chain_verified_at_generation"])

    def test_pack_without_pdf_still_works(self):
        self._check("v6", trials=10)
        res = generate_pack(ledger=self.ledger, latest_path=self.latest, packs_dir=self.packs, cache_dir=self.cache, checks_dir=self.checks, pdf=False)
        self.assertIsNone(res["pdf_path"])
        self.assertEqual(res["decision"], "ALLOW")
        self.assertEqual(set(res["sha256"]), {"pack.md", "pack.html"})

    def test_bundle_is_json_serialisable_and_complete(self):
        r = self._check("v4", trials=2)
        path = self.checks / f"{r['ledger']['check_entry_hash']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("baseline", "candidate", "decision", "attribution", "ledger", "evidence"):
            self.assertIn(key, data)
        self.assertEqual(data["evidence"]["threshold"], data["decision"]["threshold"])


if __name__ == "__main__":
    unittest.main()
