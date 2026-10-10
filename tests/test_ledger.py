"""Ledger and standalone verifier tests (temp chain and keys only)."""

import ast
import base64
import json
import multiprocessing
import threading
import unittest

from cryptography.hazmat.primitives import serialization

from ledger.ledger import GENESIS_PREV_HASH, Ledger, LedgerError, canonical_json, sha256_hex
from tests.helpers import VERIFY_SCRIPT, TempWorkspace, concurrent_append_worker, hold_ledger_lock


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.ledger = self.ws.ledger

    def tearDown(self):
        self.ws.cleanup()

    def _lines(self):
        return self.ws.chain.read_text(encoding="utf-8").splitlines()

    def _write_lines(self, lines):
        self.ws.chain.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_genesis_shape_and_idempotent(self):
        g = self.ledger.init_genesis()
        self.assertEqual(g["index"], 0)
        self.assertEqual(g["entry_type"], "genesis")
        self.assertEqual(g["prev_hash"], GENESIS_PREV_HASH)
        self.assertEqual(set(g), {"index", "timestamp", "entry_type", "payload", "prev_hash", "entry_hash", "signature"})
        self.assertTrue(g["timestamp"].endswith("Z"))
        again = self.ledger.init_genesis()
        self.assertEqual(again["entry_hash"], g["entry_hash"])
        self.assertEqual(len(self.ledger.read_all()), 1)
        self.assertTrue((self.ws.keys / "private.pem").exists())
        self.assertTrue((self.ws.keys / "public.pem").exists())

    def test_append_chains_and_signs(self):
        g = self.ledger.init_genesis()
        e1 = self.ledger.append("check_run", {"a": 1, "t": (1, 2)})
        e2 = self.ledger.append("gate_decision", {"decision": "ALLOW"})
        self.assertEqual(e1["prev_hash"], g["entry_hash"])
        self.assertEqual(e2["prev_hash"], e1["entry_hash"])
        self.assertEqual(e1["payload"], {"a": 1, "t": [1, 2]})
        body = {k: e1[k] for k in ("index", "timestamp", "entry_type", "payload", "prev_hash")}
        self.assertEqual(sha256_hex(canonical_json(body)), e1["entry_hash"])
        pub = serialization.load_pem_public_key((self.ws.keys / "public.pem").read_bytes())
        pub.verify(base64.b64decode(e1["signature"]), e1["entry_hash"].encode("utf-8"))
        self.assertEqual(self.ledger.head_hash(), e2["entry_hash"])
        self.assertEqual(self.ledger.latest("check_run")["index"], 1)
        with self.assertRaises(LedgerError):
            self.ledger.append("bogus", {})
        with self.assertRaises(LedgerError):
            self.ledger.append("check_run", ["not", "dict"])

    def test_refuses_to_regenerate_keys_with_existing_chain(self):
        self.ledger.init_genesis()
        (self.ws.keys / "private.pem").unlink()
        (self.ws.keys / "public.pem").unlink()
        from ledger.ledger import Ledger

        fresh = Ledger(self.ws.chain, self.ws.keys)
        with self.assertRaises(LedgerError):
            fresh.ensure_keys()

    def test_verify_ok(self):
        self.ledger.init_genesis()
        self.ledger.append("check_run", {"x": 1})
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OK: ledger verified (2 entries)", proc.stdout)

    def _populated(self):
        self.ledger.init_genesis()
        for i in range(3):
            self.ledger.append("check_run", {"i": i})
        return self._lines()

    def test_tamper_payload(self):
        lines = self._populated()
        entry = json.loads(lines[1])
        entry["payload"]["i"] = 99
        lines[1] = json.dumps(entry)
        self._write_lines(lines)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("entry_hash mismatch", proc.stdout)
        self.assertIn("index 1", proc.stdout)

    def test_tamper_deleted_line(self):
        lines = self._populated()
        del lines[2]
        self._write_lines(lines)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("index", proc.stdout)
        self.assertIn("continuity", proc.stdout)

    def test_tamper_swapped_signature(self):
        lines = self._populated()
        a, b = json.loads(lines[1]), json.loads(lines[2])
        a["signature"], b["signature"] = b["signature"], a["signature"]
        lines[1], lines[2] = json.dumps(a), json.dumps(b)
        self._write_lines(lines)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signature", proc.stdout)

    def test_malformed_line(self):
        lines = self._populated()
        lines[3] = "{not json"
        self._write_lines(lines)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("malformed JSON", proc.stdout)

    def test_verify_is_standalone(self):
        tree = ast.parse(VERIFY_SCRIPT.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        allowed = {"json", "sys", "hashlib", "base64", "pathlib", "cryptography.hazmat.primitives.asymmetric.ed25519"}
        self.assertTrue(modules <= allowed, modules - allowed)


class LedgerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace()
        self.ledger = self.ws.ledger

    def tearDown(self):
        self.ws.cleanup()

    def _assert_contiguous_chain(self, entries, expected):
        self.assertEqual(len(entries), expected)
        self.assertEqual([e["index"] for e in entries], list(range(expected)))
        for k in range(1, len(entries)):
            self.assertEqual(entries[k]["prev_hash"], entries[k - 1]["entry_hash"])

    def test_multiprocess_concurrent_appends(self):
        self.ledger.init_genesis()  # keys exist before the children start
        workers, per_worker = 4, 25
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(workers)
        procs = [
            ctx.Process(
                target=concurrent_append_worker,
                args=(str(self.ws.chain), str(self.ws.keys), w, per_worker, barrier),
            )
            for w in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=180)
        for p in procs:
            if p.is_alive():
                p.terminate()
            self.assertEqual(p.exitcode, 0)

        total = workers * per_worker + 1
        entries = Ledger(self.ws.chain, self.ws.keys).read_all()
        self._assert_contiguous_chain(entries, total)
        pairs = {(e["payload"]["worker"], e["payload"]["i"]) for e in entries[1:]}
        self.assertEqual(pairs, {(w, i) for w in range(workers) for i in range(per_worker)})
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"OK: ledger verified ({total} entries)", proc.stdout)
        self.assertTrue((self.ws.root / "chain.jsonl.lock").exists())

    def test_lock_timeout_raises_clear_error(self):
        self.ledger.init_genesis()
        ctx = multiprocessing.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(
            target=hold_ledger_lock,
            args=(str(self.ws.chain), str(self.ws.keys), ready, release, 60.0),
        )
        holder.start()
        try:
            self.assertTrue(ready.wait(60), "lock holder did not start")
            waiter = Ledger(self.ws.chain, self.ws.keys, lock_timeout=0.3)
            with self.assertRaises(LedgerError) as cm:
                waiter.append("check_run", {})
            self.assertIn("Timed out", str(cm.exception))
            self.assertIn("lock", str(cm.exception))
        finally:
            release.set()
            holder.join(timeout=60)
        self.assertEqual(holder.exitcode, 0)
        self.assertEqual(len(self.ledger.read_all()), 1)

    def test_append_with_builder_sees_entries_and_can_abort(self):
        self.ledger.init_genesis()
        self.ledger.append("check_run", {"x": 1})
        seen = []

        def builder(entries):
            seen.append(len(entries))
            return {"n": len(entries)}

        entry = self.ledger.append_with("check_run", builder)
        self.assertEqual(seen, [2])
        self.assertEqual(entry["index"], 2)
        self.assertEqual(entry["payload"], {"n": 2})

        def boom(entries):
            raise ValueError("abort")

        before = len(self.ws.chain.read_text(encoding="utf-8").splitlines())
        with self.assertRaises(ValueError):
            self.ledger.append_with("check_run", boom)
        with self.assertRaises(LedgerError):
            self.ledger.append_with("check_run", lambda entries: ["not", "dict"])
        self.assertEqual(len(self.ws.chain.read_text(encoding="utf-8").splitlines()), before)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_threaded_concurrent_appends(self):
        self.ledger.init_genesis()
        ledgers = [Ledger(self.ws.chain, self.ws.keys), Ledger(self.ws.chain, self.ws.keys)]
        errors = []

        def work(t):
            try:
                for i in range(10):
                    ledgers[t % 2].append("check_run", {"t": t, "i": i})
            except Exception as exc:  # surfaced via the errors list
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=120)
        self.assertEqual(errors, [])
        self._assert_contiguous_chain(self.ledger.read_all(), 81)
        proc = self.ws.verify()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
