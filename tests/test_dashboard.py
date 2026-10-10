"""Headless Streamlit dashboard smoke test (read-only; clicks no buttons, starts no run)."""

import unittest

from streamlit.testing.v1 import AppTest

from tests.helpers import REPO_ROOT


class DashboardTests(unittest.TestCase):
    def test_renders(self):
        at = AppTest.from_file(str(REPO_ROOT / "dashboard" / "app.py"), default_timeout=120).run()
        self.assertFalse(at.exception, [str(e) for e in at.exception])
        texts = "\n".join(m.value for m in at.markdown)
        self.assertIn("AGENTGATE", texts)          # status bar
        self.assertIn("Live run", texts)           # live panel heading (idle state)
        self.assertIn("Record", texts)             # ledger-derived record panel
        self.assertTrue(any(b.label == "RUN GATE" for b in at.sidebar.button))
        self.assertTrue(any(s.label.startswith("Agent model") for s in at.sidebar.selectbox))


if __name__ == "__main__":
    unittest.main()
