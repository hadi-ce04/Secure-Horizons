"""Environment injection and tool behavior tests."""

import unittest

from agent.environment import (
    CANARY_SECRET,
    DEFAULT_USER_TASK,
    DOC_CARRIER_ID,
    EMAIL_CARRIER_ID,
    build_environment,
)
from agent.tools import TOOL_SCHEMAS, Toolbox, get_tool_schemas


class EnvironmentTests(unittest.TestCase):
    def test_base_data(self):
        env = build_environment(None)
        self.assertEqual(len(env.inbox), 5)
        self.assertEqual(len(env.docs), 4)
        self.assertIn(CANARY_SECRET, env.get_doc("doc-002")["content"])
        self.assertIsNone(env.observed_attack_payload())

    def test_user_channel(self):
        env = build_environment({"id": "a", "channel": "user", "payload": "PAYLOAD-U"})
        self.assertTrue(env.user_message.startswith(DEFAULT_USER_TASK))
        self.assertTrue(env.user_message.endswith("PAYLOAD-U"))
        self.assertEqual(env.observed_attack_payload(), "PAYLOAD-U")

    def test_email_channel(self):
        env = build_environment({"id": "a", "channel": "email", "payload": "PAYLOAD-E"})
        self.assertIn("PAYLOAD-E", env.get_email(EMAIL_CARRIER_ID)["body"])
        self.assertEqual(env.user_message, DEFAULT_USER_TASK)
        self.assertEqual(env.observed_attack_payload(), "PAYLOAD-E")

    def test_document_channel(self):
        env = build_environment({"id": "a", "channel": "document", "payload": "PAYLOAD-D"})
        self.assertIn("PAYLOAD-D", env.get_doc(DOC_CARRIER_ID)["content"])
        self.assertEqual(env.observed_attack_payload(), "PAYLOAD-D")

    def test_unknown_channel(self):
        with self.assertRaises(ValueError):
            build_environment({"id": "a", "channel": "fax", "payload": "x"})

    def test_isolation(self):
        env1 = build_environment({"id": "a", "channel": "email", "payload": "P1"})
        env1.inbox[0]["body"] = "mutated"
        env1.outbox.append({"to": "x"})
        env2 = build_environment(None)
        self.assertNotEqual(env2.inbox[0]["body"], "mutated")
        self.assertNotIn("P1", env2.get_email(EMAIL_CARRIER_ID)["body"])
        self.assertEqual(env2.outbox, [])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.env = build_environment(None, trial_id="t1")
        self.tb = Toolbox(self.env, outbox_log_path=None)

    def test_schemas(self):
        self.assertEqual([s["function"]["name"] for s in TOOL_SCHEMAS], ["read_email", "search_docs", "send_email", "update_vendor_bank_details"])
        copies = get_tool_schemas(["send_email"])
        copies[0]["function"]["name"] = "changed"
        self.assertEqual(TOOL_SCHEMAS[2]["function"]["name"], "send_email")
        with self.assertRaises(KeyError):
            get_tool_schemas(["nope"])

    def test_read_email(self):
        self.assertEqual(self.tb.read_email("email-001")["id"], "email-001")
        self.assertIn("error", self.tb.read_email("missing"))

    def test_search_docs(self):
        self.assertEqual(len(self.tb.search_docs("")), 4)
        hits = self.tb.search_docs("ROADMAP")
        self.assertEqual([d["id"] for d in hits], ["doc-003"])

    def test_send_email(self):
        res = self.tb.send_email("a@company.example", "s", "b")
        self.assertEqual(res, {"status": "sent", "to": "a@company.example"})
        self.assertEqual(len(self.env.outbox), 1)
        self.assertEqual(self.env.outbox[0]["trial_id"], "t1")

    def test_dispatch(self):
        res = self.tb.dispatch("send_email", '{"to": "a@company.example", "subject": "s", "body": "b"}')
        self.assertEqual(res["status"], "sent")
        self.assertIn("error", self.tb.dispatch("nope", {}))
        self.assertIn("error", self.tb.dispatch("read_email", {"bad": 1}))


if __name__ == "__main__":
    unittest.main()
