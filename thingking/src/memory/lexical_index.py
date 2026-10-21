"""Small SQLite-derived index for the two-process QQ profile; no model downloads."""


def _terms(text):
    text = (text or "").lower().strip()
    return {text[i:i + 2] for i in range(max(1, len(text) - 1))}


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
        terms = _terms(text)
        if not terms or not scopes:
            return []
        records = self.repo.list_memories(
            scopes=scopes, kinds=kinds, subject_id=subject_id, limit=self.count(),
        )
        hits = []
        for row in records:
            other = _terms(row["content"])
            overlap = len(terms & other)
            if overlap:
                hits.append((row["id"], 1.0 - overlap / max(1, len(terms | other))))
        return sorted(hits, key=lambda item: item[1])[:n]
