"""Monitor alert sinks: JSONL file and webhook forwarding (no network)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from monitor import sinks  # noqa: E402

ALERT = {"kind": "asr_drift", "message": "ASR drifted +0.2", "config_name": "v2", "config_hash": "abc"}
STALE = {"kind": "stale_evaluation", "message": "older than 24h", "config_name": "v1", "config_hash": "def"}


class SinkTests(unittest.TestCase):
    def setUp(self):
        for k in (sinks.ENV_ALERT_JSONL, sinks.ENV_ALERT_WEBHOOK):
            os.environ.pop(k, None)

    def test_build_event_shape_and_severity(self):
        ev = sinks.build_event(ALERT, monitor_entry_hash="h1")
        self.assertEqual(ev["kind"], "asr_drift")
        self.assertEqual(ev["severity"], "high")
        self.assertEqual(ev["monitor_entry_hash"], "h1")
        self.assertEqual(ev["alert"], ALERT)
        self.assertEqual(sinks.build_event(STALE)["severity"], "medium")

    def test_jsonl_sink_appends_one_line_per_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "alerts.jsonl"
            r1 = sinks.emit_alerts([ALERT, STALE], monitor_entry_hash="h", jsonl_path=path)
            r2 = sinks.emit_alerts([ALERT], jsonl_path=path)
            self.assertEqual((r1["events"], r2["events"]), (2, 1))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[0])["kind"], "asr_drift")
            self.assertEqual(r1["errors"], [])

    def test_no_alerts_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts.jsonl"
            r = sinks.emit_alerts([], jsonl_path=path)
            self.assertEqual(r["events"], 0)
            self.assertFalse(path.exists())

    def test_env_vars_fill_in_sinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env.jsonl"
            os.environ[sinks.ENV_ALERT_JSONL] = str(path)
            try:
                sinks.emit_alerts([STALE])
            finally:
                os.environ.pop(sinks.ENV_ALERT_JSONL, None)
            self.assertTrue(path.exists())

    def test_webhook_posts_each_event_and_reports_errors_without_raising(self):
        calls = []

        class Resp:
            def __init__(self, code):
                self.status_code = code

        def fake_post(url, json=None, timeout=None):
            calls.append((url, json["kind"]))
            return Resp(200 if json["kind"] == "asr_drift" else 500)

        fake_requests = mock.MagicMock()
        fake_requests.post = fake_post
        fake_requests.RequestException = Exception
        with mock.patch.dict(sys.modules, {"requests": fake_requests}):
            r = sinks.emit_alerts([ALERT, STALE], webhook_url="https://hooks.example/x")
        self.assertEqual([c[1] for c in calls], ["asr_drift", "stale_evaluation"])
        self.assertEqual(len(r["errors"]), 1)
        self.assertIn("HTTP 500", r["errors"][0])


if __name__ == "__main__":
    unittest.main()
