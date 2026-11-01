"""Cache-friendly prefixes and complete accounting, with no provider calls."""
import json
import base64
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from helpers import local_env
from common.model_usage import capture_model_usage, measured_request
import prompt_builder


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage.db"
        env = patch.dict(os.environ, {"BINES_MODEL_USAGE_PATH": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def rows(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM model_calls")]

    def test_dynamic_state_and_recall_leave_persona_and_history_prefix_unchanged(self):
        working = [{"role": "user", "content": "过去的一句话", "ts": 100}]
        first_context = {**prompt_builder.empty_context(local_env()), "working": working,
                         "state": {"current_location": "家里"}}
        second_context = {**prompt_builder.empty_context(local_env()), "working": working,
                          "state": {"current_location": "学校"}}
        first = prompt_builder.build_conversation_messages(first_context)
        second = prompt_builder.build_conversation_messages(second_context)
        self.assertEqual(first[:2], second[:2])
        self.assertNotIn("家里", first[0]["content"])
        self.assertIn("家里", str(first[2:]))
        self.assertIn("学校", str(second[2:]))

    def test_stream_usage_chunk_is_recorded_once_and_worker_mirror_shares_id(self):
        chunks = iter([
            NS(choices=[NS(finish_reason=None, delta=NS(content="hi"))], usage=None),
            NS(choices=[NS(finish_reason="stop", delta=NS(content=None))], usage=None),
            NS(choices=[], usage=NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                                   prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20)),
        ])
        create, events = Mock(return_value=chunks), []
        with capture_model_usage(events.append, kind="memory.summarize"):
            stream = measured_request(create, model="test", stream=True)
            self.assertEqual(len(list(stream)), 3)
            stream.close()
        self.assertTrue(create.call_args.kwargs["stream_options"]["include_usage"])
        self.assertEqual(len(events), 2)
        row, = self.rows()
        self.assertEqual(row["call_id"], events[0]["call_id"])
        self.assertEqual(row["kind"], "memory.summarize")
        self.assertEqual(json.loads(row["usage"])["prompt_cache_hit_tokens"], 80)
        self.assertEqual(json.loads(row["usage"])["total_tokens"], 120)
        self.assertEqual(row["status"], "returned")

    def test_closed_stream_and_missing_usage_remain_unknown(self):
        stream = measured_request(lambda **_: iter([NS(choices=[], usage=None)]), model="test", stream=True)
        next(stream)
        stream.close()
        measured_request(lambda **_: {"choices": [{"finish_reason": "stop"}]}, model="test")
        rows = self.rows()
        self.assertEqual({row["status"] for row in rows}, {"interrupted", "returned"})
        self.assertTrue(all(json.loads(row["usage"]) is None for row in rows))
        self.assertTrue(all("messages" not in row and "content" not in row for row in rows))

    def test_reservation_failure_does_not_call_provider(self):
        create = Mock()
        with capture_model_usage(Mock(side_effect=ValueError("defer"))):
            with self.assertRaises(ValueError):
                measured_request(create, model="test")
        create.assert_not_called()

    def test_vision_reuse_requires_same_bytes_task_scope_and_current_visibility_key(self):
        import vlm_client
        vlm_client._DESCRIPTIONS.clear()
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic-test-image").decode()
        result = {"choices": [{"finish_reason": "stop", "message": {"content": "图片描述"}}],
                  "usage": {"prompt_tokens": 80, "completion_tokens": 10, "total_tokens": 90}}
        http = Mock()
        http.__enter__ = Mock(return_value=http)
        http.__exit__ = Mock(return_value=False)
        http.status_code, http.json.return_value = 200, result
        with patch("vlm_client.requests.post", return_value=http) as post:
            for _ in range(2):
                self.assertEqual(vlm_client.call_vision(image, "描述", api_key="test", reuse_key="scope:message"),
                                 "图片描述")
            self.assertEqual(post.call_count, 1)
            vlm_client.call_vision(image, "读文字", api_key="test", reuse_key="scope:message")
            vlm_client.call_vision(image, "描述", api_key="test", reuse_key="other:message")
            vlm_client.call_vision(image, "描述", api_key="test")  # live screen: no reuse
            vlm_client.call_vision(image, "描述", api_key="test")
            for _ in range(2):  # Mutable remote URL: no reuse even with same key.
                vlm_client.call_vision("https://example.com/image.png", "描述", api_key="test", reuse_key="scope:message")
            self.assertEqual(post.call_count, 7)
        self.assertEqual(len(self.rows()), 7)
        self.assertTrue(all(row["kind"] == "vision" for row in self.rows()))


if __name__ == "__main__":
    unittest.main()
