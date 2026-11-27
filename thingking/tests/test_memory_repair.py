"""History repair tests use only isolated databases and deterministic model output."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memory.repair import apply, prepare, transcript_messages
from memory.repository import MemoryRepository


class HistoryRepairTest(unittest.TestCase):
    def test_original_dates_override_bad_import_day_and_unknown_group_is_isolated(self):
        row = {"id": "old", "scope": "owner", "day_key": "2026-01-30",
               "content": "User: [2026/01/18 01:30]: [我喜欢这首歌]\nAssistant: 记住啦\n"
                          "User: [2026/02/19 02:00]: [[QQ群消息][陌生人]: 我有个秘密]\nAssistant: 嗯"}
        records = transcript_messages(row, {"管理员"})
        self.assertEqual(records[0]["day_key"], "2026-01-17")
        self.assertEqual(records[0]["scope"], "owner")
        self.assertEqual(records[2]["scope"], "archive:qq_group:unknown")
        self.assertEqual(records[3]["scope"], "archive:qq_group:unknown")

    def test_repair_archives_originals_and_uses_scoped_user_evidence_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = MemoryRepository(root / "source.db")
            raw_id = repo.add_memory("excerpt", "owner",
                                     "User: [2026/01/18 12:00]: [我喜欢 Grapefruit Moon]\nAssistant: 记住啦",
                                     day_key="2026-01-30", origin="legacy")
            repo.close()
            plan = prepare(root / "source.db", root)
            day = plan["days"][0]
            first = next(m for m in day["messages"] if m["role"] == "user")
            result = {**{k: day[k] for k in ("scope", "day_key", "hash")},
                      "summary": "管理员喜欢 Grapefruit Moon", "diary": "2026-01-18\n管理员说喜欢这首歌。",
                      "importance": 8, "facts": [{"subject": "owner", "content": "管理员喜欢 Grapefruit Moon",
                                                "source_message_id": first["repair_id"]}],
                      "quote_ids": []}
            (root / "validated.json").write_text(json.dumps([result]))
            repo = MemoryRepository(root / "source.db")
            try:
                stats = apply(root, repo)
                self.assertEqual(stats["episodes"], 1)
                self.assertEqual(stats["diaries"], 1)
                self.assertEqual(stats["facts"], 1)
                self.assertTrue(repo.get_memory(raw_id)["deleted"])
                self.assertIn("User:", repo.get_memory(raw_id)["content"])
                active = repo.list_memories(kinds=["episode", "diary"])
                self.assertEqual({r["day_key"] for r in active}, {"2026-01-18"})
                self.assertTrue(all(r["meta"]["source_message_ids"] for r in active))
                again = apply(root, repo)
                self.assertEqual(again["episodes"], 0)
                self.assertEqual(repo.count_memories(), 3)
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()
