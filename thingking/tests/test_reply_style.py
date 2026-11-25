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

    def test_plain_reactions_and_deliberately_formal_writing_do_not_trigger_a_rewrite(self):
        unused = Mock(side_effect=AssertionError("unnecessary model call"))
        closing = "如果您还有其他问题，请随时联系我。"
        self.assertTrue(style_issue(closing, "我刚写完一份报告，终于下班了"))
        self.assertTrue(style_issue(closing, "别用客服口吻跟我说话"))
        self.assertTrue(style_issue(closing, "你怎么总是用客服口吻"))
        self.assertTrue(style_issue(closing, "客服口吻太重了"))
        for draft, user in [
            ("没看懂", "你知道他刚才在说什么吗"), ("不知道呀", "谁先到的"),
            ("哇！", "我过啦"), ("挺好", "我终于换了个新猫窝"), ("祝你顺利", "祝我面试顺利吧"),
            ("右上角点导出，再选 CSV", "这表格怎么导出？"),
            ("猫又跑到打印机上去了？？", "我们猫老霸占打印机"),
            (closing, "帮我写一封给客户的邮件"), (closing, "用正式语气回复"),
            (closing, "把“如果您还有其他问题，请随时联系我。”发给小夏"),
        ]:
            with self.subTest(draft=draft, user=user):
                self.assertEqual(polish_reply(draft, user_text=user, complete=unused), draft)
        unused.assert_not_called()

    def test_context_review_accepts_specific_answers_and_abstains_on_failure(self):
        draft = "值得去，你喜欢的那幅画就在二楼"
        for decision in ('{"verdict":"ok"}', '{"verdict":"maybe"}', 'bad json', TimeoutError()):
            with self.subTest(decision=decision):
                complete = Mock(side_effect=[decision])
                self.assertEqual(polish_reply(draft, user_text="你觉得这展值得去吗？", complete=complete), draft)
                complete.assert_called_once()

    def test_requests_for_original_wording_keep_the_source_intact(self):
        unused = Mock(side_effect=AssertionError("a quoted source must not be rephrased"))
        for query in ("你当时的原话是什么？", "你昨天日记全文怎么写的", "我不是问时间，我要看日记原文", "小结怎么写的"):
            for draft in ("这话我记下了", "“这话我记下了”", "我理解你的感受。"):
                with self.subTest(query=query, draft=draft):
                    self.assertEqual(polish_reply(draft, user_text=query, complete=unused), draft)
        unused.assert_not_called()
        for query in ("别给我念原话，我想听听你的想法", "不用读日记原文，你自己觉得呢？"):
            self.assertTrue(style_issue("这话我记下了", query), query)

    def test_rewriting_preserves_negation_scope_quantity_responsibility_and_promises(self):
        # A semantic judge can approve these near-identical sentences despite a
        # change in policy, speaker or certainty. Never submit the changed one to it.
        for fact, changed in (
            ("两张票不能都退", "两张票都不能退"),
            ("小夏没说一定来", "小夏说不一定来"),
            ("只有小夏说周一可以来", "小夏说只有周一可以来"),
            ("除了小夏，三个人同意了", "三个人里，就小夏没同意"),
            ("有一成把握赶不上", "差一成就赶不上了"),
            ("小夏答应替我垫付", "我答应替小夏垫付"),
            ("我只负责发通知", "我负责这件事"),
        ):
            with self.subTest(fact=fact):
                complete = Mock(side_effect=["文言", changed, fact, '{"acceptable":true}'])
                self.assertEqual(polish_reply("这话我记下了。" + fact, user_text="那这事怎么办", voice="我说大白话",
                                              complete=complete), fact)
                self.assertEqual(complete.call_count, 4)
                actual = json.loads(complete.call_args_list[-1].args[0][1]["content"])
                self.assertEqual(actual["候选"], fact)

    def test_unlisted_long_speech_or_stock_acknowledgement_is_judged_in_context(self):
        user = "我刚拼完的积木散了一地"
        for draft in ("我理解你的感受。",
                      "生活里总会有这样值得珍惜的小插曲。",
                      "山再高也有翻过去的时候，路再长也有走到尽头的一天，你所经历的一切终会成为走向下一程的风景。"):
            with self.subTest(draft=draft):
                complete = Mock(side_effect=['{"verdict":"bland"}', "闻之惜焉。", "啊，刚拼完就散了啊",
                                             '{"acceptable":true}'])
                self.assertEqual(polish_reply(draft, user_text=user, voice="我平时说大白话", complete=complete),
                                 "啊，刚拼完就散了啊")

    def test_generic_reply_gets_a_grounded_reaction_without_inventing_experience(self):
        draft = "听起来真不错，坚持下去一定会收获更多快乐。"
        user = "我终于跑完5公里了！"
        complete = Mock(side_effect=['{"verdict":"bland"}', "闻君有成，甚喜。", "5公里跑完了耶！",
                                     '{"acceptable":true}'])
        self.assertEqual(polish_reply(draft, user_text=user, voice="我有时会跟着高兴", complete=complete),
                         "5公里跑完了耶！")
        self.assertEqual(complete.call_count, 4)
        self.assertNotIn("本轮人格", complete.call_args_list[0].args[0][1]["content"])

    def test_written_analysis_can_be_useful_for_an_instruction_or_stilted_in_chat(self):
        draft = "总体来说，这一安排具有一定的合理性。"
        complete = Mock(side_effect=['{"verdict":"formal"}', "可也。", "行，周末见", '{"acceptable":true}'])
        self.assertEqual(polish_reply(draft, user_text="周末再见呗", voice="我常说半句", complete=complete),
                         "行，周末见")

    def test_synonym_for_the_same_permission_posture_is_rejected_again(self):
        complete = Mock(side_effect=["彼将用膳，暂别。", "好，那我不拦你了", "好呀，回头聊",
                                     '{"acceptable":true}'])
        result = polish_reply("行，我不拦你。", user_text="我先去吃饭啦", voice="我跟同学说话很随意",
                              complete=complete)
        self.assertEqual(result, "好呀，回头聊")
        self.assertEqual(complete.call_count, 4)

    def test_render_cannot_reintroduce_softer_version_of_unrequested_memory_ack(self):
        fact = "两张票不能都退"
        complete = Mock(side_effect=["文言", "行，我记着了。" + fact, fact, '{"acceptable":true}'])
        self.assertEqual(polish_reply("这话我记下了。" + fact, user_text="票还能退吗", voice="我说大白话",
                                      complete=complete), fact)
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
