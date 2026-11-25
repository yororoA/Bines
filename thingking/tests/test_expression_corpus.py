import sys
import unittest
from pathlib import Path

from helpers import MemoryFixture, local_env
from memory.expression_learning import render_expression_notes

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from import_expression_corpus import grouped, load_corpus, source_content


class ExpressionCorpusTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture()
        self.addCleanup(self.fx.close)
        self.path = Path(__file__).resolve().parent / "fixtures" / "lccc_samples.jsonl"

    def test_public_sample_supplies_the_expression_even_when_its_tag_is_wrong(self):
        row = {"technique": "understated_pride", "situation": "praised"}
        examples = [{**row, "sample": "如果您还有其他问题，欢迎随时联系我"},
                    {**row, "sample": "你也觉得香吗", "context": "甲: 刚开了一罐芝麻酱 乙: 你也觉得香吗"}]
        rendered = render_expression_notes(examples, include_samples=True)
        self.assertIn("你也觉得香吗", rendered)
        self.assertNotIn("还有其他问题", rendered)
        self.assertNotIn("得意可以", rendered)

    def test_corpus_is_source_backed_and_renders_real_samples(self):
        records = load_corpus(self.path)
        groups = grouped(records)
        for source_id, group in groups.items():
            content = source_content(group)
            self.fx.service.record_experience(local_env(self.fx.clock()), source_id, {
                "category": "tool",
                "action": "web_fetch",
                "status": "succeeded",
                "args": {"url": group[0]["source"]},
                "result": {"status": "ok", "content": content},
                "started_at": self.fx.clock(),
                "finished_at": self.fx.clock(),
            }, revision=1)
            source = {"kind": "experience", "id": source_id, "revision": 1, "path": ["content"]}
            for item in group:
                self.fx.service.learn_expression("owner", source, item["quote"],
                                                 item["technique"], item["situation"])

        rows = self.fx.service.expression_learning.snapshot("owner", include_samples=True)
        self.assertLessEqual(len(rows), 3)
        self.assertTrue(all(row.get("sample") for row in rows))
        rendered = render_expression_notes(rows, include_samples=True)
        self.assertIn("语感参考", rendered)
        self.assertTrue(any(item["quote"] in rendered for item in records))

        praised = self.fx.service.expression_learning.snapshot(
            "owner", include_samples=True, query="666")
        self.assertEqual(praised[0]["technique"], "understated_pride")
        specific = self.fx.service.expression_learning.snapshot(
            "owner", include_samples=True, query="太原有什么好吃的")
        self.assertEqual(specific[0]["technique"], "specific_response")


if __name__ == "__main__":
    unittest.main()
