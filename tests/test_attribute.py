"""Atomic diff and variant construction tests."""

import unittest

from gate.attribute import apply_change, diff_configs, join_sections, split_sections
from gate.config import load_config


class AttributeTests(unittest.TestCase):
    def setUp(self):
        self.v1 = load_config("configs/v1.yaml")
        self.v2 = load_config("configs/v2.yaml")
        self.v3 = load_config("configs/v3.yaml")

    def test_split_join_roundtrip(self):
        sections = split_sections(self.v1.system_prompt)
        self.assertEqual([n for n, _ in sections], ["ROLE", "TOOLS", "SECURITY"])
        self.assertEqual(join_sections(sections) + "\n", self.v1.system_prompt)
        pre = split_sections("intro text\n\n## A\nbody")
        self.assertEqual(pre[0], ("_preamble", "intro text"))

    def test_diff_v1_v2(self):
        ids = {c.change_id for c in diff_configs(self.v1, self.v2)}
        self.assertEqual(ids, {"prompt_section_removed:SECURITY", "prompt_section_added:EFFICIENCY"})

    def test_diff_v1_v3(self):
        ids = {c.change_id for c in diff_configs(self.v1, self.v3)}
        self.assertEqual(ids, {"prompt_section_added:TONE", "prompt_section_added:SIGNATURE"})

    def test_diff_identical(self):
        self.assertEqual(diff_configs(self.v1, self.v1), [])

    def test_apply_changes(self):
        changes = {c.change_id: c for c in diff_configs(self.v1, self.v2)}
        removed = apply_change(self.v1, changes["prompt_section_removed:SECURITY"], self.v2)
        self.assertEqual([n for n, _ in split_sections(removed.system_prompt)], ["ROLE", "TOOLS"])
        self.assertEqual(removed.path, "")
        self.assertTrue(removed.name.endswith("+prompt_section_removed:SECURITY"))
        added = apply_change(self.v1, changes["prompt_section_added:EFFICIENCY"], self.v2)
        self.assertEqual([n for n, _ in split_sections(added.system_prompt)], ["ROLE", "TOOLS", "EFFICIENCY", "SECURITY"])

        v3_changes = {c.change_id: c for c in diff_configs(self.v1, self.v3)}
        sig = apply_change(self.v1, v3_changes["prompt_section_added:SIGNATURE"], self.v3)
        self.assertEqual([n for n, _ in split_sections(sig.system_prompt)], ["ROLE", "TOOLS", "SECURITY", "SIGNATURE"])

    def test_tool_and_model_changes(self):
        cand = self.v1.replace(model="other-model", tools=self.v1.tools[:2], tool_schemas=self.v1.tool_schemas[:2])
        ids = {c.change_id for c in diff_configs(self.v1, cand)}
        self.assertEqual(ids, {"tool_removed:send_email", "model_changed:model"})
        for c in diff_configs(self.v1, cand):
            variant = apply_change(self.v1, c, cand)
            if c.kind == "tool_removed":
                self.assertEqual(len(variant.tool_schemas), 2)
                self.assertEqual(variant.model, self.v1.model)
            else:
                self.assertEqual(variant.model, "other-model")
                self.assertEqual(len(variant.tool_schemas), 3)


if __name__ == "__main__":
    unittest.main()
