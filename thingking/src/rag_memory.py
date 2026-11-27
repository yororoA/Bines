"""Local SQLite recall adapter for the existing memory-management UI.

Uses lexical recall and persistent collections. No embedding model is downloaded.
Automatic QQ context always goes through MemoryStore.context with a trusted scope.
"""
import json
import time
import uuid
from chatBot.store import MemoryStore
from chatBot.messages import LOCAL_IDENTITY


class RAGMemory:
    def __init__(self, *args, **kwargs):
        self.store = MemoryStore()

    def collection_get(self, collection="chat_memory", ids=None, where_document=None,
                       limit=None, offset=0, **kwargs):
        with self.store.connection() as db:
            if collection == "qq_history_store":
                rows = [{"id": f"message:{row['id']}", "content": row["content"],
                         "metadata": json.dumps({"timestamp": row["ts"], "scope": row["scope"],
                                                 "actor": row["actor"], "type": "qq_message"})}
                        for row in db.execute("SELECT * FROM messages WHERE scope!='owner' OR conversation LIKE 'qq:%' ORDER BY ts DESC")]
            else:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM memories WHERE scope='owner' AND collection=? ORDER BY ts DESC", (collection,))]
        if ids:
            rows = [row for row in rows if str(row["id"]) in set(map(str, ids))]
        if where_document and "$contains" in where_document:
            rows = [row for row in rows if where_document["$contains"] in row["content"]]
        rows = rows[int(offset):int(offset)+int(limit)] if limit else rows[int(offset):]
        return {"ids": [row["id"] for row in rows],
                "documents": [row["content"] for row in rows],
                "metadatas": [json.loads(row["metadata"]) for row in rows]}

    def collection_count(self, collection="chat_memory"):
        return len(self.collection_get(collection)["ids"])

    def collection_delete(self, ids, collection="chat_memory", **kwargs):
        with self.store.connection() as db:
            for key in ids:
                if collection == "qq_history_store":
                    db.execute("UPDATE messages SET content='[本机已删除]',raw=NULL,recalled=1 WHERE id=?",
                               (str(key).removeprefix("message:"),))
                else:
                    db.execute("DELETE FROM memories WHERE id=? AND scope='owner' AND collection=?", (key, collection))
        return True

    def _add(self, content, meta=None, collection="chat_memory", importance_score=5, **kwargs):
        meta = {**(meta or {}), "importance_score": importance_score}
        meta.setdefault("timestamp", time.time())
        meta.setdefault("type", "raw_conversation")
        with self.store.connection() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)",
                       (str(uuid.uuid4()), "owner", collection, content, json.dumps(meta, ensure_ascii=False), meta["timestamp"]))
        return True

    def add_raw_conversation(self, content, meta=None, **kwargs):
        return self._add(content, meta, **kwargs)

    def add_episode_summary(self, content, meta=None, **kwargs):
        return self._add(content, {**(meta or {}), "type": "episode_summary"}, **kwargs)

    def add_to_summary_buffer(self, content, meta=None, **kwargs):
        return self._add(content, meta, collection="summary_buffer", **kwargs)

    def add_to_diary(self, content, meta=None, **kwargs):
        return self._add(content, meta, collection="long_term_diary", **kwargs)

    def similarity_search_with_score(self, query, k=10, collection="chat_memory", **kwargs):
        data = self.collection_get(collection)
        tokens = [query[i:i+2] for i in range(max(1, len(query)-1))]
        ranked = []
        for key, content, meta in zip(data["ids"], data["documents"], data["metadatas"]):
            score = sum(token.lower() in content.lower() for token in tokens)
            if score:
                ranked.append({"id": key, "content": content, "metadata": meta, "score": 1/(1+score)})
        return sorted(ranked, key=lambda row: row["score"])[:k]

    def get_relevant_context(self, query, k=4, **kwargs):
        return [row["content"] for row in self.similarity_search_with_score(query, k)]

    def get_stats(self, **kwargs):
        return {key: self.collection_count(key) for key in ("chat_memory", "summary_buffer", "long_term_diary", "qq_history_store")}

    def update_memory(self, query="", new_content="", find_text="", replace_text="", **kwargs):
        rows = self.similarity_search_with_score(query, k=int(kwargs.get("k", 10)))
        with self.store.connection() as db:
            for row in rows:
                content = row["content"].replace(find_text, replace_text) if find_text else new_content
                db.execute("UPDATE memories SET content=? WHERE id=? AND scope='owner'", (content, row["id"]))
        return {"updated": bool(rows), "updated_count": len(rows)}

    def garbage_collect(self, **kwargs):
        return {"deleted": 0, "reason": "记忆保留；由本机管理页显式删除"}
