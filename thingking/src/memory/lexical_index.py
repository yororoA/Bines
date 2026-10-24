"""Small SQLite-derived index for the two-process QQ profile; no model downloads."""
import re

_FILLERS = ("请问", "请你", "帮我", "帮忙", "你还记得", "还记得", "你记得", "记不记得",
            "是什么", "什么", "什么时候", "多少", "我们", "你和我", "我的", "你的", "之前", "以前",
            "那个", "这个", "一下", "是否", "怎么", "时候", "可以", "知道", "告诉我",
            "一起", "发生了", "发生", "的时候")


def _terms(text):
    text = (text or "").lower().strip()
    return {text[i:i + 2] for i in range(max(1, len(text) - 1)) if not text[i:i + 2].isspace()}


def query_terms(text):
    """去掉提问套话后匹配主题，中文不依赖分词模型。"""
    text = (text or "").lower().strip()
    for filler in sorted(_FILLERS, key=len, reverse=True):
        text = text.replace(filler, " ")
    text = re.sub(r"([唱听看玩])过", r"\1 ", text)
    text = re.sub(r"[的了呢吗啊？?！!]", " ", text)
    words = re.findall(r"[\w]+", text)
    return set().union(*(set([word]) if word.isascii() else _terms(word) for word in words)) if words else set()


class LexicalIndex:
    persistent = False

    def __init__(self, repo):
        self.repo = repo

    def count(self):
        return self.repo.count_memories()

    def upsert(self, records):
        # Chroma's indexed_at belongs to Chroma, so switching profiles can reconcile it.
        return []

    def delete(self, ids):
        pass

    def reset(self):
        pass

    def query(self, text, scopes, kinds=None, n=10, subject_id=None):
        terms = query_terms(text)
        if not terms or not scopes:
            return []
        records = self.repo.list_memories(
            scopes=scopes, kinds=kinds, subject_id=subject_id, limit=self.count(),
        )
        hits = []
        for row in records:
            content = row["content"].lower()
            overlap = sum(term in content for term in terms)
            if overlap:
                # 长摘要不因包含额外信息而降到近零；按问题主题的覆盖率排序。
                hits.append((row["id"], 1.0 - overlap / max(1, len(terms))))
        return sorted(hits, key=lambda item: item[1])[:n]
