"""Synthetic regression: a rewritten diary records its current, possibly empty facets."""
from helpers import approved_diary
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ExpressionDiaryRewriteTests(unittest.TestCase):
    def setUp(self):
        self.env_dir = tempfile.TemporaryDirectory(prefix="bines-expression-rewrite-")
        self.addCleanup(self.env_dir.cleanup)
        root = Path(self.env_dir.name)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(root / "memory"),
            "BINES_GATEWAY_DIR": str(root / "gateway"), "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_MODEL_USAGE_PATH": str(root / "usage.db"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "spool"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        from helpers import MemoryFixture
        self.fx = MemoryFixture()
        self.addCleanup(self.fx.close)

    def test_rewrite_with_empty_frozen_notes_clears_previous_expression_metadata(self):
        from helpers import local_env
        service, repo = self.fx.service, self.fx.repo
        quote = "才不是特意等你夸的，不过你这句话我还挺高兴。"
        saved = service.record_experience(local_env(), "synthetic:expression-source", {
            "action": "web_fetch", "category": "tool", "status": "succeeded",
            "args": {}, "result": {"content": quote}, "finished_at": self.fx.clock(),
        })
        receipt = service.learn_expression("owner", {
            "kind": "experience", "id": "synthetic:expression-source", "revision": 1, "path": ["content"],
        }, quote, "warm_direct", "diary")
        ts = self.fx.clock() - 86400
        day = service._day(ts)
        message = repo.add_message("local", "owner", "user", "合成测试约好周末看电影", ts, day, actor_id="owner")
        episode = repo.add_memory("episode", "owner", "合成周末电影约定", day_key=day, end_ts=ts,
                                  meta={"source_message_ids": [message]})
        repo.conn.execute("UPDATE messages SET episode_id=? WHERE id=?", (episode, message))
        service.enqueue_due_diaries()
        first = service.claim_job(["diary"])
        original = service.complete_diary(first["job"]["id"], "合成日记初稿", attribution_review=approved_diary(first, '合成日记初稿'))
        self.assertEqual(repo.get_memory(original["diary_id"])["meta"]["expression_notes"][0]["id"], receipt["id"])

        repo.delete_memory(saved["id"])
        repo.update_memory(episode, content="合成周末电影约定更新")
        service.diary_queue.mark("owner", day)
        service.enqueue_due_diaries()
        second = service.claim_job(["diary"])
        self.assertEqual(second["materials"]["expression_notes"], [])
        rewritten = service.complete_diary(second["job"]["id"], "合成日记修改稿", attribution_review=approved_diary(second, '合成日记修改稿'))
        self.assertEqual(rewritten["status"], "done")
        self.assertEqual(rewritten["diary_id"], original["diary_id"])
        self.assertEqual(repo.get_memory(rewritten["diary_id"])["meta"]["expression_notes"], [])


if __name__ == "__main__":
    unittest.main()
