import json
import unittest
from unittest.mock import Mock

from common.intention_note import review_intention_note


class IntentionNoteTest(unittest.TestCase):
    report = "刚把车票照片发给她，还在等她回话；旧地图资料失效了，相册又报错，先安静躺着。"
    short_report = "相册和扫描都还停着，趴一会儿"
    generic_report = ("前面提到的几个方面暂时都告一段落，相关的部分也都已经有所交代，"
                      "眼下似乎没有需要另外展开的地方，接下来保持原有的节奏会比较妥当。")

    def test_confirmed_routine_review_can_be_empty(self):
        for note in (self.report, self.short_report, self.generic_report,
                     "刚才连着查了好几趟，能看的都看完了，先趴一会儿。"):
            with self.subTest(note=note):
                model = Mock()
                model.complete_text.return_value = '{"verdict":"routine"}'
                self.assertEqual(review_intention_note(note, optional=True, choice="review", model=model), "")
                messages = model.complete_text.call_args.args[0]
                self.assertIn("json", messages[0]["content"].lower())
                data = json.loads(messages[1]["content"])
                self.assertEqual(set(data), {"choice", "普通等待可留空", "topic", "intent", "reason分句"})
                self.assertEqual(model.complete_text.call_args.kwargs["max_retries"], 0)
                self.assertEqual(model.complete_text.call_args.kwargs["thinking"], "disabled")
                model.complete_text.assert_called_once()

    def test_simple_or_genuinely_blocked_notes_pass_without_api_call(self):
        model = Mock()
        for note in ("", "眼睛酸了，想趴一会儿", "原件还在暗房修复，馆员说下午五点才能取",
                     "这张照片扫描出来怎么偏蓝了", "最后那四拍我还想再听一次",
                     "收据还没来，先不重发", "我想看看私下里他说的「报错，别催」",
                     "我还惦记他那句「" + self.generic_report + "」，这是什么意思",
                     "刚读到 '" + self.generic_report + "'"):
            self.assertEqual(review_intention_note(note, model=model), note)
        model.complete_text.assert_not_called()

    def test_only_a_complete_source_clause_can_replace_the_note(self):
        core = "验证码里的「灯，塔」我还没认全，得等对方确认第2张"
        note = "主人还没回话；" + core + "，浏览器封着，先趴着。"
        model = Mock()
        model.complete_text.return_value = '{"verdict":"excerpt","first":1,"last":2}'
        self.assertEqual(review_intention_note(note, choice="pause", topic="登录", model=model), core)
        model.complete_text.return_value = '{"verdict":"excerpt","first":3,"last":3}'
        self.assertEqual(review_intention_note(note, choice="pause", topic="登录", model=model), note)
        mixed = "目录页面还是报错，等他回话那件事不急，我想看看春秋两条潮线中间那排红字"
        model.complete_text.return_value = '{"verdict":"excerpt","first":2,"last":2}'
        self.assertEqual(review_intention_note(mixed, choice="curiosity", topic="潮线图", model=model),
                         "我想看看春秋两条潮线中间那排红字")

    def test_no_silent_action_and_no_loss_of_a_new_thought_or_qualification(self):
        model = Mock()
        model.complete_text.return_value = '{"verdict":"routine"}'
        for choice in ("pause", "all", "contact", "finish", "curiosity", "continue"):
            self.assertEqual(review_intention_note(self.report, choice=choice, optional=True, model=model), self.report)
        note = self.report + "我想再听听那声响。"
        self.assertEqual(review_intention_note(note, choice="review", optional=True, model=model), note)
        note = "他还没回，浏览器也锁着，我会过去，除非那边关门了"
        model.complete_text.return_value = '{"verdict":"excerpt","first":2,"last":2}'
        self.assertEqual(review_intention_note(note, choice="initiative", model=model), note)

    def test_invalid_model_responses_and_outages_keep_the_original(self):
        for response in ('{"verdict":"excerpt","first":true,"last":1}', '{"verdict":"excerpt","first":3,"last":1}',
                         '{"verdict":"excerpt","first":99,"last":99}', '{"verdict":"rewrite","note":"编个新理由"}',
                         '{"verdict":"keep"}', '[]', 'bad'):
            model = Mock()
            model.complete_text.return_value = response
            self.assertEqual(review_intention_note(self.report, optional=True, choice="review", model=model), self.report)
            model.complete_text.assert_called_once()
        model = Mock()
        model.complete_text.side_effect = TimeoutError()
        with self.assertLogs("bines.intention_note", level="DEBUG") as log:
            self.assertEqual(review_intention_note(self.report, choice="all", model=model), self.report)
        self.assertIn("TimeoutError", log.output[0])
        self.assertNotIn(self.report, log.output[0])
        model.complete_text.assert_called_once()
