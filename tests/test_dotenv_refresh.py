"""load_dotenv refreshes credentials it injected earlier (rotated keys in a long-running process)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import runner  # noqa: E402

KEY = "AWS_ACCESS_KEY_ID"
REAL = "AWS_DEFAULT_REGION"


class DotenvRefreshTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in (KEY, REAL)}
        runner._DOTENV_INJECTED.discard(KEY)
        runner._DOTENV_INJECTED.discard(REAL)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        runner._DOTENV_INJECTED.discard(KEY)
        runner._DOTENV_INJECTED.discard(REAL)

    def test_rotated_value_is_picked_up_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(f"{KEY}=AKIA-old-test\n", encoding="utf-8")
            self.assertEqual(runner.load_dotenv(p), 1)
            self.assertEqual(os.environ[KEY], "AKIA-old-test")
            p.write_text(f"{KEY}=AKIA-new-test\n", encoding="utf-8")
            self.assertEqual(runner.load_dotenv(p), 1)
            self.assertEqual(os.environ[KEY], "AKIA-new-test")
            self.assertEqual(runner.load_dotenv(p), 0)  # unchanged file: nothing to update

    def test_real_environment_still_wins(self):
        os.environ[REAL] = "from-shell"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(f"{REAL}=from-file\n", encoding="utf-8")
            self.assertEqual(runner.load_dotenv(p), 0)
            self.assertEqual(os.environ[REAL], "from-shell")


if __name__ == "__main__":
    unittest.main()
