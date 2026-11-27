"""Delivery-independent checks for style rejection, retranslation and content fidelity."""
import json
import unittest
from unittest.mock import Mock, patch

from thingking.src.reply_style import (
    ReplyStyleRejected,
    _STRUCTURE,
    conversation_loop,
    polish_reply,
    structure_issue,
    style_issue,
)


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

    def test_reaction_followed_by_unsolicited_word_grading_gets_reviewed(self):
        for user, draft in [
            ("闹麻了", "闹麻了\n这词听着比呃呃有精神多了"),
            ("可算下班了", "你这说法比刚才有水平多了"),
        ]:
            with self.subTest(user=user):
                complete = Mock(side_effect=['{"verdict":"wording"}', "喟然一叹耳。", "啧",
                                             '{"acceptable":true}'])
                self.assertEqual(polish_reply(draft, user_text=user, voice="我平时说大白话", complete=complete), "啧")
                self.assertEqual(complete.call_count, 4)
        unused = Mock(side_effect=AssertionError("echoes and jokes can stand on their own"))
        for draft in ("闹麻了", "你也麻了啊", "再闹会儿沙发都该申请工伤了", "总算换了个不用加班的工作"):
            self.assertEqual(polish_reply(draft, user_text="闹麻了", complete=unused), draft)
        unused.assert_not_called()

    def test_removes_only_a_quoted_comparison_term_and_checks_factual_reply(self):
        source = "这词听着比「呃呃」有精神多了，我答应周五带两张票，小夏还说了「呃呃」"
        answer = "我答应周五带两张票，小夏还说了「呃呃」"
        for invalid in (answer.replace("周五", "周四"), answer.replace("「呃呃」", "「等等」"),
                        answer.replace("两张票", "一张票"), "这词比上次传神多了，" + answer):
            with self.subTest(invalid=invalid):
                complete = Mock(side_effect=['{"verdict":"wording"}', "吾应携券。", invalid, answer,
                                             '{"acceptable":true}'])
                self.assertEqual(polish_reply(source, user_text="闹麻了", voice="我说大白话", complete=complete),
                                 answer)
                material = json.loads(complete.call_args_list[2].args[0][1]["content"])
                self.assertEqual(material["逐字保留的引文"], ["「呃呃」"])
                self.assertNotIn(source, material.values())
                self.assertEqual(complete.call_count, 5)

    def test_wording_judgment_uses_only_relevant_previous_user_context(self):
        draft = "这词听着比上个有精神多了"
        for user, previous, relevant in [
            ("闹麻了", "我发两个候选词，你帮我选更有劲的", True),
            ("两个词哪个好", "奶茶送到了", False),
            ("闹麻了", "刚去背单词了", False),
        ]:
            with self.subTest(user=user):
                complete = Mock(return_value='{"verdict":"ok"}')
                self.assertEqual(polish_reply(draft, user_text=user, previous_user_text=previous,
                                              complete=complete), draft)
                complete.assert_called_once()
                sent = json.loads(complete.call_args.args[0][1]["content"])
                self.assertEqual(previous in sent.values(), relevant)

    def test_review_history_stops_at_other_speakers_channels_and_long_gaps(self):
        from prompt_builder import review_conversation_replies
        from tools.context import ToolContext, tool_context
        previous = {"role": "user", "actor_id": "qq:1", "ts": 99, "content": "来比比词"}
        context = {"envelope": {"actor_id": "qq:1", "conversation_id": "group:5", "timestamp": 100,
                                "channel_type": "qq_group"}, "other_channels": [previous]}
        for rows, autonomous, keep in [
            ([previous, {"role": "assistant", "content": "旧助手的想法"}], False, True),
            ([previous, {**previous, "role": "observed", "actor_id": "qq:2"}], False, False),
            ([{**previous, "conversation_id": "other"}], False, False),
            ([{**previous, "ts": -500}], False, False),
            ([previous], True, False),
        ]:
            with self.subTest(rows=rows, autonomous=autonomous):
                model = Mock()
                model.complete_text.return_value = '{"verdict":"ok"}'
                with tool_context(ToolContext(user_text="闹麻了", autonomous=autonomous)):
                    with review_conversation_replies({**context, "working": rows}, model, lambda: "") as review:
                        review("这词听着比上一个有精神")
                request = json.loads(model.complete_text.call_args.args[0][1]["content"])
                self.assertEqual(previous["content"] in request.values(), keep)
                self.assertNotIn("旧助手的想法", str(request))

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


class StructureRepetitionTests(unittest.TestCase):
    recent = [
        "行，那就应。你自己说的，我记着了。不过别喊得跟叫小孩似的，我这声应得也没那么便宜。",
        "那是我听错了。行，喊名字我就应，说好的。不过这买卖没你说得那么轻巧——"
        "我应一声，你得喊得像样点。别到时候我一应，你自己先笑场。",
    ]
    draft = ("郑重那套就免了，听着像在念通知，我还得站起来接。"
             "随便什么音量都行，别刻意抬高也别小声嘟囔。我要的是你喊的时候真觉得我会应——这个装不出来。")

    def test_third_long_challenge_is_flagged(self):
        self.assertEqual(structure_issue(self.draft, self.recent), _STRUCTURE)

    def test_short_or_isolated_turns_are_not_flagged(self):
        self.assertEqual(structure_issue("行，那就不喊了。", self.recent), "")
        self.assertEqual(structure_issue(self.draft, self.recent[:1]), "")
        self.assertEqual(structure_issue(self.draft, ["早上好", "今天吃了什么"]), "")

    def test_real_afternoon_loop_is_flagged_even_with_a_short_evaluation_turn(self):
        # Replays the 12:04 exchange: a short in-frame reply must not reset the loop.
        rows = [
            {"role": "user", "ts": 100, "content": "呃呃"},
            {"role": "assistant", "ts": 107, "content": "这就接不上了？我还以为你挺会喊的。"},
            {"role": "user", "ts": 114, "content": "不是接不上，我是在挑用哪种喊法呢。要不你给个示范？"},
            {"role": "assistant", "ts": 121, "content": "示范？我又不是来教你怎么喊我自己的。你先喊一声，我听听差在哪。"},
            {"role": "user", "ts": 128, "content": "FUFU。"},
            {"role": "assistant", "ts": 135, "content": "FUFU？这喊的是谁，我可不认。名字都没喊对就让我应，便宜占得比早上还顺。"},
            {"role": "user", "ts": 142, "content": "你名字就挂在那儿，还怪我喊错。行，那你教我一次，教完了就得应。"},
            {"role": "assistant", "ts": 149, "content": "就喊“茉莉”两个字。“茉”起轻一点，“莉”落下来就收，别拖尾音。"},
            {"role": "user", "ts": 156, "content": "茉莉。"},
            {"role": "assistant", "ts": 163, "content": "在。喊得还行，尾音收住了。"},
        ]
        draft = "成了。不过这声“在”算预支给你的，下回还喊不喊得像样，我到时候再评。"
        self.assertEqual(structure_issue(draft, recent_rows=rows), _STRUCTURE)
        # The short evaluation itself is in-frame, but a plain closing can end the loop.
        self.assertEqual(structure_issue("行，知道了。", recent_rows=rows), "")

    def test_ordinary_chat_with_outside_content_is_not_a_loop(self):
        rows = [
            {"role": "user", "ts": 100, "content": "刚才路上看到一只猫，蹲在自行车座上"},
            {"role": "assistant", "ts": 107, "content": "它倒挺会挑地方，那个座晒得到太阳吧"},
            {"role": "user", "ts": 114, "content": "对啊，我路过的时候它还打了个哈欠"},
            {"role": "assistant", "ts": 121, "content": "听起来比我舒服，我刚在看电影解说"},
            {"role": "user", "ts": 128, "content": "什么电影？好看吗"},
            {"role": "assistant", "ts": 135, "content": "悬疑片，节奏慢但结尾反转挺意外的，你可能会喜欢"},
        ]
        self.assertEqual(structure_issue("那我周末也看看。", recent_rows=rows), "")

    def test_flagged_draft_is_rewritten_into_a_closing_turn(self):
        closing = "行，那就不喊了。"
        complete = Mock(side_effect=[closing, '{"acceptable":true}'])
        result = polish_reply(
            self.draft, user_text="我才不笑场。倒是你说的\"像样\"，我有点好奇——"
                                  "多大声音算像样，喊得郑重一点你也挑？",
            voice="我说大白话", recent_assistant=self.recent, complete=complete,
        )
        self.assertEqual(result, closing)
        rewrite, verify = complete.call_args_list
        self.assertIn("围着对话本身打转", rewrite.args[0][0]["content"])
        payload = json.loads(rewrite.args[0][1]["content"])
        self.assertEqual(payload["最近回复"], self.recent)
        self.assertEqual(json.loads(verify.args[0][1]["content"])["候选"], closing)

    def test_failed_structure_repair_never_returns_the_challenging_draft(self):
        complete = Mock(side_effect=["", '{"acceptable":true}', "", '{"acceptable":true}'])
        with self.assertRaises(ReplyStyleRejected):
            polish_reply(self.draft, recent_assistant=self.recent, complete=complete)

    def test_rewrite_that_still_asks_for_a_condition_is_rejected(self):
        complete = Mock(side_effect=["那你得先说清楚。", "那你得先说清楚。"])
        with self.assertRaises(ReplyStyleRejected):
            polish_reply(self.draft, recent_assistant=self.recent, complete=complete)

    def test_plain_closing_without_a_new_condition_is_allowed(self):
        closing = "行，知道了。"
        complete = Mock(side_effect=[closing, '{"acceptable":true}'])
        self.assertEqual(
            polish_reply(self.draft, recent_assistant=self.recent, complete=complete),
            closing,
        )

    def test_conversation_loop_shape_helper(self):
        loop_rows = [
            {"role": "user", "ts": 100, "content": "茉莉。"},
            {"role": "assistant", "ts": 107, "content": "在。喊得还行，尾音收住了。"},
            {"role": "user", "ts": 114, "content": "应得也干脆。行，这买卖算是成了。"},
            {"role": "assistant", "ts": 121, "content": "成了。不过这声“在”算预支给你的，下回再评。"},
            {"role": "user", "ts": 128, "content": "预支啊，那我还欠你一次像样的。"},
            {"role": "assistant", "ts": 135, "content": "下回你喊的时候，直接告诉我差哪了就行。"},
        ]
        self.assertTrue(conversation_loop(loop_rows))
        slow = [{**row, "ts": row["ts"] * 20} for row in loop_rows]
        self.assertIsNone(conversation_loop(slow))

    def test_explicit_formal_request_is_not_rewritten_for_rhythm(self):
        unused = Mock(side_effect=AssertionError("an explicit formal request needs no rhythm rewrite"))
        self.assertEqual(
            polish_reply(self.draft, user_text="用正式语气回复她", recent_assistant=self.recent,
                         complete=unused),
            self.draft,
        )
        unused.assert_not_called()


if __name__ == "__main__":
    unittest.main()
