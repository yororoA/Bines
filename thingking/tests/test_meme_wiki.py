import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "thingking" / "src"), str(ROOT)]

from chatBot.messages import Identity
from skill_runner import QQ_ALLOWED_ACTIONS, SkillRunner
from tools import TOOLS_REGISTRY, meme_tool
from tools.context import ToolContext, tool_context


class MemeWikiTests(unittest.TestCase):
    def setUp(self):
        meme_tool._search.cache_clear()
        self.addCleanup(meme_tool._search.cache_clear)
        self.http = self.enterContext(patch.object(meme_tool.requests, "get"))
        self.response = MagicMock(status_code=200)
        self.http.return_value.__enter__.return_value = self.response

    def test_qq_dispatch_only_sends_term_and_uses_exact_alias(self):
        self.response.json.return_value = {"items": [
            {"title": "无关结果", "slug": "unrelated", "summary": "正文碰巧提到梗词"},
            {"title": "示例梗", "aliases": ["小小梗"], "summary": "朋友打趣时用", "slug": "a/b"},
        ]}
        runner = SkillRunner(TOOLS_REGISTRY, skills_dir=ROOT / "skills",
                             enabled_actions_path=ROOT / "server/skill_actions.json",
                             qq_allowed_actions=QQ_ALLOWED_ACTIONS)
        command = {"skill": "web-research", "action": "search_meme", "args": {"term": "小小梗"}}
        with tool_context(ToolContext(Identity("123", "456"), event_id="1",
                                      user_text="我明天要去医院。“小小梗”是什么梗？")):
            self.assertIn("search_meme", {row["function"]["name"] for row in runner.function_tools("QQ")})
            first = json.loads(runner.execute(command, source="QQ"))
            second = json.loads(runner.execute(command, source="QQ"))
        self.assertEqual(first, second)
        self.assertEqual(first["items"], [{"title": "示例梗", "summary": "朋友打趣时用",
                                           "url": meme_tool._API + "/a%2Fb"}])
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args, (meme_tool._API,))
        self.assertEqual(self.http.call_args.kwargs["params"], {"q": "小小梗", "limit": 3})

    def test_chat_fragments_identifiers_and_absent_terms_never_leave_device(self):
        text = "明天三点去医院。电话13800138000是什么梗？退退退是什么梗？看 https://example.com"
        with tool_context(ToolContext(user_text=text)):
            for term in (text, "明天三点去医院", "13800138000", "https://example.com", "历史聊天", ""):
                with self.subTest(term=term):
                    self.assertEqual(meme_tool.search_meme(term)["code"], "invalid_meme_term")
        self.http.assert_not_called()

    def test_new_quoted_terms_work_across_a_pause_without_a_fixed_vocabulary(self):
        for text in ("你听过『蓝瘦香菇』吗，这个梗该怎么接", "你听过‘蓝瘦香菇’吗？这个梗挺有意思"):
            with self.subTest(text=text), tool_context(ToolContext(user_text=text)):
                self.response.json.return_value = {"items": []}
                self.assertEqual(meme_tool.search_meme("蓝瘦香菇")["status"], "ok")
        self.assertEqual(self.http.call_args.kwargs["params"]["q"], "蓝瘦香菇")

    def test_only_the_verified_quote_of_the_meme_question_can_supply_its_name(self):
        from chatBot.media import resolve_replies
        def request(eid, mid, text):
            return {"event_id": eid, "message": [{"type": "reply", "data": {"id": mid}},
                                                  {"type": "text", "data": {"text": text}}]}

        question = "这是什么梗，接一句"
        events = [request("target", "11", question), request("other", "12", "帮我记一下"),
                  request("recalled", "13", question)]
        for quoted in ("蓝瘦香菇", "今天去医院了，我说的是『蓝瘦香菇』这个梗。"):
            rows = {"11": {"status": "ok", "data": {"message": quoted}},
                    "12": {"status": "ok", "data": {"message": "其他词条"}},
                    "13": {"status": "failed", "data": {"message": "撤回词条"}}}
            _, verified = resolve_replies(events, None, lambda _, mid: rows[mid])
            terms = meme_tool.quoted_meme_terms(events, verified)
            self.assertEqual(terms, {"蓝瘦香菇"})
            with tool_context(ToolContext(user_text=question, quoted_meme_terms=terms)):
                self.response.json.return_value = {"items": []}
                self.assertEqual(meme_tool.search_meme("蓝瘦香菇")["status"], "ok")
                for private in ("今天去医院了", "其他词条", "撤回词条"):
                    self.assertEqual(meme_tool.search_meme(private)["code"], "invalid_meme_term")
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.kwargs["params"]["q"], "蓝瘦香菇")
        for private in ("验证码1234", "我家住在这里", "今天去医院了", "123456", "这个人叫『老王』"):
            self.assertFalse(meme_tool.quoted_meme_terms(events, [{"source_event_id": "target", "message": private}]))

    def test_empty_and_unavailable_results_leave_the_chat_recoverable(self):
        with tool_context(ToolContext(user_text="退退退")):
            self.response.json.return_value = {"items": []}
            self.assertEqual(meme_tool.search_meme("退退退"), {"status": "ok", "items": []})
            for status in (429, 503):
                meme_tool._search.cache_clear()
                self.response.status_code = status
                self.assertEqual(meme_tool.search_meme("退退退")["code"], "wiki_unavailable")
            meme_tool._search.cache_clear()
            self.http.side_effect = requests.Timeout()
            self.assertEqual(meme_tool.search_meme("退退退")["code"], "wiki_unavailable")
