"""Desktop compatibility facade over the same SQLite store as QQ Thinking."""
import json
import threading
from pathlib import Path
from chatBot.store import MemoryStore
from chatBot.messages import LOCAL_IDENTITY
from config import ROOT_DIR


class ShortTermMemory:
    def __init__(self, *args, store=None, **kwargs):
        self.store = store or MemoryStore()
        self.file_path = str(self.store.path)
        self.filepath = self.file_path

    def load(self):
        return self.get_messages()

    def get_messages(self):
        return self.store.context(LOCAL_IDENTITY, 60)

    def add_message(self, role, content):
        self.store.add(LOCAL_IDENTITY, role, content)


class DynamicMemory:
    def __init__(self, *args, store=None, **kwargs):
        self.store = store or MemoryStore()
        self.lock = threading.RLock()
        self.file_path = str(self.store.path)
        self.filepath = self.file_path
        self.load()

    def load(self):
        with self.lock:
            self.state = self.store.state(LOCAL_IDENTITY)
        return self.state

    def save(self):
        # Tool state writes use a transactional method; this method preserves old callers.
        return self.store.update_state(LOCAL_IDENTITY, self.state)

    def to_prompt_str(self):
        return json.dumps(self.load(), ensure_ascii=False)


class PermanentMemory:
    def __init__(self, *args, **kwargs):
        self.file_path = str(ROOT_DIR / "主人格详细介绍.md")
        self.filepath = self.file_path
        self.lock = threading.RLock()
        self.data = {"facts": []}
        store = MemoryStore()
        with store.connection() as db:
            row = db.execute("SELECT content FROM memories WHERE id='legacy:permanent' AND scope='owner'").fetchone()
        if row:
            self.data = json.loads(row["content"])
        self.load()

    def load(self):
        path = Path(self.file_path)
        self.content = path.read_text(encoding="utf-8") if path.exists() else "你是 Bines，情感对话助手。"
        return self.content

    def to_prompt_str(self):
        return self.content + "\n" + json.dumps(self.data, ensure_ascii=False)


class LayeredMemorySystem:
    def __init__(self):
        self.store = MemoryStore()
        self.short_term = ShortTermMemory(store=self.store)
        self.dynamic = DynamicMemory(store=self.store)
        self.permanent = PermanentMemory()
        self.updater = None

    def get_full_context_messages(self, query="", qq_context_data=None):
        if qq_context_data:
            raise PermissionError("QQ 上下文必须由独立 QQ Thinking 处理")
        from rag_memory import RAGMemory
        recalled = RAGMemory().get_relevant_context(query, k=5) if query else []
        return [
            {"role": "system", "content": self.permanent.to_prompt_str()},
            {"role": "system", "content": f"动态记忆：{self.dynamic.to_prompt_str()}"},
            {"role": "system", "content": "相关长期记忆：\n" + "\n".join(recalled)},
            *self.store.context(LOCAL_IDENTITY, 60),
        ]

    def add_interaction(self, user_input, reply):
        self.store.add(LOCAL_IDENTITY, "user", user_input)
        if reply:
            self.store.add(LOCAL_IDENTITY, "assistant", reply)

    def set_dynamic_memory_updater(self, updater):
        self.updater = updater

    def start_maintenance_services(self):
        # Persistent SQLite replaces the missing RAG/GC implementation.
        # No heavyweight model downloads or background cross-scope summarization.
        pass
