"""Headless Streamlit dashboard smoke test (read-only; clicks no buttons)."""

import unittest

from streamlit.testing.v1 import AppTest

from tests.helpers import REPO_ROOT


class DashboardTests(unittest.TestCase):
    def test_renders(self):
        at = AppTest.from_file(str(REPO_ROOT / "dashboard" / "app.py"), default_timeout=120).run()
        self.assertFalse(at.exception, [str(e) for e in at.exception])
        texts = [m.value for m in at.markdown] + [i.value for i in at.info]
        self.assertTrue(any("Decision:" in t or "No check has been run yet" in t for t in texts))
        self.assertTrue(any(s.value.startswith("Verified:") for s in at.success) or len(at.error) > 0)


if __name__ == "__main__":
    unittest.main()
