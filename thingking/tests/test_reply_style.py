"""Delivery-independent checks for style rejection, retranslation and content fidelity."""
import json
import unittest
from unittest.mock import Mock, patch

from thingking.src.reply_style import ReplyStyleRejected, polish_reply, style_issue


class ReplyStyleTests(unittest.TestCase):
    draft = "这话我记下了。后天 15:00 我再问小夏"

    def test_only_own_unrequested_voice_is_checked(self):
        unused = Mock(side_effect=AssertionError("normal replies need no model call"))
        for draft, user in [
            ("接住你了", "接住我！"), ("这话我记下了", "帮我记一下"),
            ("睡吧，我守着", "你能守着我吗"), ("  好呀  明天见！ ", ""),
            ("不是明天，是后天", "哪天开会"), ("她举的是“这话我心里有数”这个例子", ""),
            ("这话我记下了", "原样回复这句话"), ("行，我不拦你", "用霸总腔回我"),
            ("好，那我不拦你了", "你还会拦着我吗"), ("我不拦你", "我不拦你该怎么说？"),
            ("这话我记下了", "把‘这话我记下了’发给小夏"),
            ("不是 443，是 80；不是 UDP，是 TCP", "哪个端口"),
        ]:
            with self.subTest(draft=draft):
                self.assertEqual(polish_reply(draft, user_text=user, complete=unused), draft)
        unused.assert_not_called()
        for draft in ["“这话我记下了”", "行，我不拦你。", "去吧。这句话接得漂亮",
                      "我会稳稳接住你的情绪", "你不是不够好，你只是太累了。",
                      "我先接住你的情绪，再帮你想办法", "不是烦恼，是委屈；不是难过，是孤单。",
                      "好哦，这句话我记住啦", "好，那我不拦你了", "我又没拦着你。"]:
            self.assertTrue(style_issue(draft), draft)
        self.assertTrue(style_issue("我不拦你", "我去吃饭啦，别再回我“我不拦你”了"))

    def test_passed_reply_comes_from_pivot_and_scoped_voice(self):
        answer = "好哦，后天 15:00 我再问小夏"
        complete = Mock(side_effect=["吾将后天 15:00 再问小夏。", answer, '{"acceptable":true}'])
        checkpoint = Mock()
        result = polish_reply(self.draft, user_text="我说的是后天哦", voice="我跟同学说话很随意", complete=complete,
                              before_request=checkpoint)
        self.assertEqual(result, answer)
        self.assertEqual(checkpoint.call_count, 3)
        pivot, render, verify = complete.call_args_list
        data = lambda call: json.loads(call.args[0][1]["content"])
        self.assertEqual(data(pivot)["草稿"], self.draft)
        self.assertEqual(data(render)["文言中转稿"], "吾将后天 15:00 再问小夏。")
        self.assertEqual(data(render)["本轮人格"], "我跟同学说话很随意")
        self.assertEqual(data(verify), {"对方的话": "我说的是后天哦", "原稿": self.draft, "候选": answer,
                                        "待去除的腔调": style_issue(self.draft)})
        self.assertEqual(verify.kwargs["response_format"], {"type": "json_object"})
        for call in complete.call_args_list:
            self.assertEqual(call.kwargs["thinking"], "disabled")
            self.assertEqual(call.kwargs["max_retries"], 0)
            self.assertEqual(call.kwargs["retry_tokens"], call.kwargs["max_tokens"])

    def test_synonym_for_the_same_permission_posture_is_rejected_again(self):
        complete = Mock(side_effect=["彼将用膳，暂别。", "好，那我不拦你了", "好呀，回头聊",
                                     '{"acceptable":true}'])
        result = polish_reply("行，我不拦你。", user_text="我先去吃饭啦", voice="我跟同学说话很随意",
                              complete=complete)
        self.assertEqual(result, "好呀，回头聊")
        self.assertEqual(complete.call_count, 4)

    def test_retry_never_returns_a_changed_detail_cliche_or_protocol(self):
        source = self.draft + "，发“确认”到 https://example.org/a"
        valid = "好呀，后天 15:00 我再问小夏，发“确认”到 https://example.org/a"
        for invalid in [valid.replace("后天", "明天"), valid.replace("15:00", "15:05"),
                        valid.replace("确认", "取消"), valid.replace("/a", "/b"), valid + "，这话我心里有数",
                        valid + "[CQ:at,qq=1]"]:
            with self.subTest(candidate=invalid):
                complete = Mock(side_effect=["文言中转", invalid, valid, '{"acceptable":true}'])
                self.assertEqual(polish_reply(source, voice="我说大白话", complete=complete), valid)
                self.assertEqual(complete.call_count, 4)

    def test_reject_semantic_change_and_unverified_output_with_a_bound(self):
        wrong_subject = "后天 15:00 让小夏来问我吧"
        for verdict in ['{"acceptable":false}', '{"acceptable":"true"}', 'null', '好']:
            with self.subTest(verdict=verdict):
                complete = Mock(side_effect=["文言中转", wrong_subject, verdict, wrong_subject, verdict])
                with self.assertRaises(ReplyStyleRejected):
                    polish_reply(self.draft, voice="我说大白话", complete=complete)
                self.assertEqual(complete.call_count, 5)

    def test_service_failure_timeout_and_cancel_do_not_disclose_draft(self):
        with self.assertRaises(ReplyStyleRejected) as failed:
            polish_reply(self.draft, voice="我说大白话", complete=Mock(side_effect=TimeoutError()))
        self.assertNotIn(self.draft, str(failed.exception))
        complete = Mock(return_value="文言中转")
        with patch("thingking.src.reply_style.monotonic", side_effect=[0, 1, 21]):
            with self.assertRaisesRegex(ReplyStyleRejected, "超时"):
                polish_reply(self.draft, voice="我说大白话", complete=complete)
        self.assertEqual(complete.call_count, 1)

        complete.reset_mock()
        with self.assertRaises(InterruptedError):
            polish_reply(self.draft, voice="我说大白话", complete=complete,
                         before_request=Mock(side_effect=InterruptedError()))
        complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
