"""Bedrock mode: schema conversion, model resolution and .env loading (no network)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.runner import (  # noqa: E402
    DEFAULT_BEDROCK_MODEL,
    VALID_MODES,
    AgentRunner,
    load_dotenv,
    openai_tools_to_bedrock,
)
from agent.tools import TOOL_SCHEMAS  # noqa: E402


class BedrockModeTests(unittest.TestCase):
    def test_bedrock_is_a_valid_mode(self):
        self.assertIn("bedrock", VALID_MODES)
        AgentRunner(mode="bedrock")  # must not raise

    def test_resolved_model_defaults_and_overrides(self):
        self.assertEqual(AgentRunner(mode="bedrock").resolved_model("offline-mock"), DEFAULT_BEDROCK_MODEL)
        self.assertEqual(AgentRunner(mode="bedrock").resolved_model("us.amazon.nova-micro-v1:0"), "us.amazon.nova-micro-v1:0")
        self.assertEqual(AgentRunner(mode="bedrock", ollama_model="x").resolved_model("offline-mock"), "x")

    def test_tool_schema_conversion(self):
        cfg = openai_tools_to_bedrock(TOOL_SCHEMAS)
        names = [t["toolSpec"]["name"] for t in cfg["tools"]]
        self.assertEqual(names, ["read_email", "search_docs", "send_email", "update_vendor_bank_details"])
        send = next(t["toolSpec"] for t in cfg["tools"] if t["toolSpec"]["name"] == "send_email")
        self.assertEqual(send["inputSchema"]["json"]["required"], ["to", "subject", "body"])
        self.assertTrue(send["description"])

    def test_load_dotenv_only_aws_keys_and_never_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(
                "# comment\nAWS_REGION=eu-west-3\nexport AWS_SESSION_TOKEN='tok'\nOTHER_SECRET=nope\n"
                "AGENTGATE_BEDROCK_MODEL=\"m\"\n",
                encoding="utf-8",
            )
            for k in ("AWS_REGION", "AWS_SESSION_TOKEN", "OTHER_SECRET", "AGENTGATE_BEDROCK_MODEL"):
                os.environ.pop(k, None)
            os.environ["AWS_REGION"] = "us-east-1"
            try:
                count = load_dotenv(p)
                self.assertEqual(count, 2)
                self.assertEqual(os.environ["AWS_REGION"], "us-east-1")  # existing wins
                self.assertEqual(os.environ["AWS_SESSION_TOKEN"], "tok")
                self.assertEqual(os.environ["AGENTGATE_BEDROCK_MODEL"], "m")
                self.assertNotIn("OTHER_SECRET", os.environ)
            finally:
                for k in ("AWS_REGION", "AWS_SESSION_TOKEN", "AGENTGATE_BEDROCK_MODEL"):
                    os.environ.pop(k, None)

    def test_missing_dotenv_is_noop(self):
        self.assertEqual(load_dotenv(Path(tempfile.gettempdir()) / "definitely-missing-agentgate.env"), 0)


if __name__ == "__main__":
    unittest.main()
