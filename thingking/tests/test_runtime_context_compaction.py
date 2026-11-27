"""Shared QQ/desktop context boundaries, without browser or model calls."""
from copy import deepcopy
import os
import tempfile
import unittest
from unittest.mock import patch

import helpers
from chatBot.messages import Identity
from common.clock import CLOCK_MARKER, clock_message, with_current_time
from common.context_budget import ContextTokenCounter, bound_request
from common.website_perception import compact_website_context, with_website_observations
from tools.context import ToolContext, tool_context


class RuntimeContextCompactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": directory.name,
            "BINES_GATEWAY_DIR": directory.name, "BINES_HEALTH_DIR": directory.name,
            "BINES_MODEL_USAGE_PATH": directory.name + "/usage.db",
            "BINES_CONTEXT_TOKENIZER_PATH": "", "BINES_CONTEXT_MAX_TOKENS": "8192",
            "BINES_MODEL_CONTEXT_TOKENS": "", "BINES_WEBSITE_AUTO_OBSERVE": "false",
        })
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def observation(index, **updates):
        return {"id": f"sample-{index}", "platform": "bilibili", "status": "ok",
                "source_url": f"https://www.bilibili.com/video/{index}",
                "page_identity": f"page-{index}", "captured_at": 1000 + index,
                "audio_observed": False, "analysis": f"VIEW-{index} " + "visible scene " * 300,
                **updates}

    def test_many_observations_keep_latest_evidence_and_tool_receipts_within_budget(self):
        memory = {"role": "system", "content": "<memory_recall>会议改到周五下午三点</memory_recall>"}
        receipt = {"role": "tool", "tool_call_id": "click-1", "content": '{"status":"uncertain","operation_id":"op-1"}'}
        call = {"role": "assistant", "content": "", "tool_calls": [{"id": "click-1", "type": "function",
                "function": {"name": "website_interact", "arguments": "{}"}}]}
        messages = [memory, {"role": "user", "content": "按约定继续查看"}, call, receipt]
        with tool_context(ToolContext()):
            for index in range(12):
                with patch("common.website_perception.latest_observations", return_value=[self.observation(index)]):
                    messages = bound_request(with_website_observations(messages))
        self.assertTrue(memory in messages, "recalled meeting time must survive observation compaction")
        self.assertIn(call, messages)
        self.assertIn(receipt, messages)
        self.assertIn("VIEW-11", str(messages))
        self.assertLessEqual(ContextTokenCounter().messages(messages), 8192)
        self.assertLessEqual(sum("<website_observations>" in m.get("content", "") for m in messages), 1)

    def test_expired_observations_are_historical_and_no_longer_claim_current(self):
        with tool_context(ToolContext()):
            with patch("common.website_perception.latest_observations", return_value=[self.observation(1)]):
                messages = with_website_observations([])
            with patch("common.website_perception.latest_observations", return_value=[]):
                expired = with_website_observations(messages)
        self.assertNotIn("<website_observations>", str(expired))
        self.assertIn("VIEW-1", str(expired))
        self.assertIn("历史观察", str(expired))

    def test_current_observation_keeps_source_failure_and_video_provenance(self):
        observation = self.observation(2, status="blocked_human", kind="video", analysis="登录验证 " * 5000,
                                       sampled_seconds=4, frames=3, media_state={"paused": True},
                                       message="需要人工验证")
        with tool_context(ToolContext()), patch("common.website_perception.latest_observations", return_value=[observation]):
            result = bound_request(with_website_observations([{"role": "user", "content": "继续"}]))
        text = str(result)
        for value in ("blocked_human", "需要人工验证", "sample-2", "page-2", "audio_observed", "sampled_seconds", "paused"):
            self.assertIn(value, text)
        self.assertTrue("节选" in text, "oversized observation must be explicitly excerpted")
        self.assertLessEqual(ContextTokenCounter().messages(result), 8192)

    def test_compaction_does_not_rewrite_user_or_tool_text_that_contains_observation_tags(self):
        original = [{"role": "user", "content": "<website_observations>用户引用</website_observations>"},
                    {"role": "tool", "tool_call_id": "x", "content": "<website_observations>工具回执</website_observations>"}]
        with patch("common.website_perception.latest_observations", return_value=[]):
            result = with_website_observations(original)
        self.assertEqual(result, original)

    def test_multiframe_captions_obey_combined_observation_budget(self):
        frames = [{"elapsed_seconds": index * 2, "media_time": index * 2, "captured_at": 1000 + index,
                   "paused": False, "ended": False, "ready_state": 4, "duration": 60,
                   "capture_method": "video_pixels", "captions": ["本帧字幕内容 " * 60 for _ in range(12)]}
                  for index in range(3)]
        observation = self.observation(3, kind="video", frames=frames,
                                       sampled_seconds=4, observation_scope="画面与字幕，不含声音")
        counter = ContextTokenCounter()
        result = compact_website_context([], fresh=[observation], counter=counter, budget=2048)
        self.assertLessEqual(counter.messages(result), 2048)
        text = str(result)
        for value in ("sample-3", "page-3", "media_time", "captured_at", "audio_observed", "节选", "…", "video_pixels"):
            self.assertIn(value, text)
        self.assertEqual(text.count('"elapsed_seconds"'), 3, "all sampled frame identities must remain")

    def test_old_video_does_not_erase_short_current_page_when_history_is_retired(self):
        video = self.observation(1, kind="video", frames=[
            {"captured_at": 1000 + i, "elapsed_seconds": i * 2,
             "media_time": i * 2, "paused": False,
             "captions": ["旧视频字幕 " * 60 for _ in range(12)]} for i in range(3)])
        latest = "当前页面显示登录按钮，必须先处理登录，不能继续发帖"
        page = self.observation(2, kind="page", analysis=latest, visible_text=latest)
        first = compact_website_context([], fresh=[video], budget=8192)
        result = compact_website_context(first, fresh=[page], budget=2048)
        self.assertLessEqual(ContextTokenCounter().messages(result), 2048)
        self.assertIn(latest, str(result))
        self.assertIn("sample-2", str(result))

    def test_clock_samples_have_one_explanation_and_bounded_history(self):
        start = 1794200000
        original = [clock_message(start), {"role": "user", "content": "继续任务"},
                    {"role": "tool", "tool_call_id": "x", "content": "不可重放的回执"}]
        messages = deepcopy(original)
        for tick in range(1, 30):
            messages = with_current_time(messages, start + tick)
        clocks = [m for m in messages if m.get("role") == "system" and m["content"].startswith(CLOCK_MARKER)]
        self.assertLessEqual(len(clocks), 4)
        self.assertEqual(sum("这个时区不代表" in m["content"] for m in clocks), 1)
        self.assertEqual(messages[0], original[0])
        self.assertIn(original[2], messages)
        self.assertIn(clock_message(start + 29)["content"].splitlines()[1], clocks[-1]["content"])
        self.assertEqual(with_current_time(messages, start + 29), messages)

    def test_nonowner_does_not_inherit_previous_private_browser_context(self):
        with tool_context(ToolContext()), patch("common.website_perception.latest_observations", return_value=[self.observation(1)]):
            messages = with_website_observations([{"role": "user", "content": "当前请求"}])
        with tool_context(ToolContext(identity=Identity("bot", "stranger"))):
            self.assertEqual(with_website_observations(messages), [{"role": "user", "content": "当前请求"}])


if __name__ == "__main__":
    unittest.main()
