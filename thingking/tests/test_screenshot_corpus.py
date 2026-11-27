import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from import_chat_screenshots import candidates, parse_dialogue, quality_score


class ScreenshotCorpusTest(unittest.TestCase):
    def test_parse_and_rank_playful_replies(self):
        raw = (
            '```json\n[{"speaker":"A","text":"我今天又被老师点名了"},'
            '{"speaker":"B","text":"哈哈 你也太惨了吧 笑死"},'
            '{"speaker":"A","text":"别笑 下次就轮到你了"},'
            '{"speaker":"B","text":"？"}]\n```'
        )
        lines = parse_dialogue(raw)
        self.assertEqual(len(lines), 4)
        self.assertGreater(quality_score("哈哈 你也太惨了吧 笑死"),
                           quality_score("明天上午九点开会"))
        self.assertLess(quality_score("666是想不出怎么回了吧"), 1.5)
        self.assertGreaterEqual(quality_score("因为想好的内容不适合发。"), 1.5)

        items = candidates(lines, source="https://example.invalid/post",
                           source_id="screenshot:test", max_quotes=5, min_score=1.0)
        self.assertTrue(items)
        self.assertTrue(all(item["quote"] in item["context"] for item in items))
        self.assertTrue(all(item["source_id"] == "screenshot:test" for item in items))
        self.assertTrue(all(item["technique"] and item["situation"] for item in items))


if __name__ == "__main__":
    unittest.main()
