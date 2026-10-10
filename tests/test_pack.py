"""Evidence pack tests (temp ledger, latest path, and packs dir)."""

import hashlib
import unittest
from pathlib import Path

from evidence.pack import generate_pack, list_packs, load_mapping
from gate.gate import run_check
from tests.helpers import TempWorkspace


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PackTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()

    def tearDown(self):
        self.ws.cleanup()

    def test_mapping(self):
        m = load_mapping()
        self.assertEqual(len(m["controls"]), 7)

    def test_pack_for_v2(self):
        check = run_check("configs/v2.yaml", "configs/v1.yaml", ledger=self.ws.ledger, cache_dir=self.ws.cache,
                          latest_path=self.ws.latest, outbox_log_path=None)
        res = generate_pack(ledger=self.ws.ledger, latest_path=self.ws.latest, packs_dir=self.ws.packs,
                            cache_dir=self.ws.cache, outbox_log_path=None)
        md = Path(res["md_path"]).read_text(encoding="utf-8")
        html = Path(res["html_path"]).read_text(encoding="utf-8")
        self.assertIn("BLOCK", md)
        self.assertIn(check["candidate"]["hash"], md)
        self.assertIn("Art. 12", md)
        self.assertIn("prompt_section_removed:SECURITY", md)
        self.assertIn('lang="en"', html)
        self.assertIn('scope="col"', html)
        self.assertIn('scope="row"', html)
        self.assertIn("<caption>", html)
        entry = self.ws.ledger.head()
        self.assertEqual(entry["entry_type"], "pack_generated")
        self.assertEqual(entry["payload"]["files"]["pack.md"], sha(res["md_path"]))
        self.assertEqual(entry["payload"]["files"]["pack.html"], sha(res["html_path"]))
        self.assertEqual(list_packs(self.ws.packs)[0], Path(res["pack_dir"]))
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_pack_runs_check_when_missing(self):
        res = generate_pack(ledger=self.ws.ledger, latest_path=self.ws.latest, packs_dir=self.ws.packs,
                            cache_dir=self.ws.cache, outbox_log_path=None)
        self.assertEqual(res["decision"], "ALLOW")
        types = [e["entry_type"] for e in self.ws.ledger.read_all()]
        self.assertEqual(types, ["genesis", "check_run", "gate_decision", "pack_generated"])


if __name__ == "__main__":
    unittest.main()
