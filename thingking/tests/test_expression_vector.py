import unittest
from types import SimpleNamespace

from memory.expression_learning import render_expression_notes
from memory.expression_vector import ExpressionVectorStore, _clip
from memory.service import MemoryService


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class FakeVectorStore(ExpressionVectorStore):
    def __init__(self, hits):
        super().__init__("http://qdrant", "test_expression", FakeEmbedder(),
                         timeout=0.2, min_score=0.35, top_k=12)
        self._ready = True
        self._dimensions = 2
        self.hits = hits
        self.requests = []

    def _request(self, method, path, *, allow_404=False, **kwargs):
        self.requests.append((method, path, kwargs))
        if path.endswith("/points/search"):
            return {"result": self.hits}
        return {}


def hit(point_id, score, quote, context, technique="light_teasing",
        situation="teased", vector=None):
    return {
        "id": point_id,
        "score": score,
        "vector": vector or [1.0, 0.0],
        "payload": {
            "quote": quote,
            "context": context,
            "technique": technique,
            "situation": situation,
            "quality": 1.5,
            "source": "https://example.com/post",
            "source_title": "公开聊天截图",
        },
    }


class ExpressionVectorTest(unittest.TestCase):
    def test_search_filters_scope_and_returns_context(self):
        store = FakeVectorStore([
            hit("a", 0.91, "准备刺猹是吧", "她: 你咋不睡觉\n她: 准备刺猹是吧"),
            hit("b", 0.72, "你这技术不行啊", "他: 还守啥瓜田\n她: 你这技术不行啊"),
        ])
        rows = store.search("owner", "大半夜不睡觉想上天吗", limit=3)
        self.assertEqual([row["sample"] for row in rows][0], "准备刺猹是吧")
        self.assertEqual(rows[0]["context"], "她: 你咋不睡觉 她: 准备刺猹是吧")
        body = store.requests[-1][2]["json"]
        self.assertEqual(body["filter"]["must"][0]["key"], "scope")
        self.assertEqual(body["filter"]["must"][0]["match"]["value"], "owner")
        self.assertEqual(body["score_threshold"], 0.35)

    def test_mmr_prefers_a_diverse_second_hit(self):
        store = FakeVectorStore([
            hit("a", 0.90, "A", "ctx", vector=[1.0, 0.0]),
            hit("b", 0.89, "B", "ctx", vector=[0.99, 0.1]),
            hit("c", 0.80, "C", "ctx", vector=[0.0, 1.0]),
        ])
        rows = store.search("owner", "随便聊聊", limit=2)
        self.assertEqual([row["sample"] for row in rows], ["A", "C"])

    def test_vector_rows_keep_same_technique_when_samples_differ(self):
        rows = [
            {"id": "a", "technique": "light_teasing", "situation": "teased",
             "sample": "半夜不睡要出去偷牛吗", "context": "她: 你咋不睡觉\n她: 半夜不睡要出去偷牛吗"},
            {"id": "b", "technique": "light_teasing", "situation": "teased",
             "sample": "你这技术不行啊", "context": "他: 还守啥瓜田\n她: 你这技术不行啊"},
        ]
        rendered = render_expression_notes(rows, limit=3, include_samples=True)
        self.assertIn("偷牛", rendered)
        self.assertIn("技术不行", rendered)
        self.assertIn("当时：", rendered)

    def test_clip_bounds_text(self):
        self.assertEqual(_clip("  a \n b  ", 10), "a b")
        self.assertTrue(_clip("很长" * 100, 12).endswith("…"))

    def test_empty_vector_result_does_not_fall_back_to_lexical(self):
        class EmptyVector:
            def search(self, scope, query, limit=3):
                return []

        class ForbiddenLearning:
            def snapshot(self, *args, **kwargs):
                raise AssertionError("empty vector result must not trigger lexical fallback")

        dummy = SimpleNamespace(expression_vector=EmptyVector(),
                                expression_learning=ForbiddenLearning())
        self.assertEqual(MemoryService._expression_notes(dummy, "owner", "帮我修函数"), [])

    def test_vector_error_falls_back_to_lexical(self):
        class BrokenVector:
            def search(self, scope, query, limit=3):
                raise RuntimeError("qdrant down")

        class Lexical:
            def snapshot(self, *args, **kwargs):
                return [{"technique": "light_teasing", "situation": "casual"}]

        dummy = SimpleNamespace(expression_vector=BrokenVector(),
                                expression_learning=Lexical())
        self.assertEqual(
            MemoryService._expression_notes(dummy, "owner", "随便聊聊"),
            [{"technique": "light_teasing", "situation": "casual"}])


if __name__ == "__main__":
    unittest.main()
