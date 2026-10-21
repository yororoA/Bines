import hashlib
import math
import shutil
import sys
import tempfile
from pathlib import Path

THINKING_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = THINKING_DIR.parent
for path in (str(ROOT_DIR), str(THINKING_DIR / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from memory.repository import MemoryRepository  # noqa: E402
from memory.service import MemoryService  # noqa: E402
from memory.settings import MemorySettings  # noqa: E402
from memory.vector_index import VectorIndex  # noqa: E402

OWNER_QQ = "10001"
STRANGER_QQ = "20002"
GROUP_ID = "30003"


class FakeEmbedder:
    """Deterministic character-bigram embedding: similar strings get similar vectors."""

    dim = 128

    def _embed(self, text):
        vector = [0.0] * self.dim
        text = text or ""
        grams = [text[i:i + 2] for i in range(max(1, len(text) - 1))]
        for gram in grams:
            bucket = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % self.dim
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts):
        return [self._embed(t) for t in texts]

    def embed_query(self, text):
        return self._embed(text)


class Clock:
    def __init__(self, start=1_790_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MemoryFixture:
    def __init__(self, **settings_overrides):
        self.tmp = Path(tempfile.mkdtemp(prefix="bines_memory_test_"))
        overrides = {"summarize_trigger": 6, "summarize_chunk": 4, "working_window": 4}
        overrides.update(settings_overrides)
        self.settings = MemorySettings(data_dir=self.tmp, owner_qq_ids=frozenset({OWNER_QQ}), **overrides)
        self.clock = Clock()
        self.repo = MemoryRepository(self.settings.db_path, clock=self.clock)
        self.index = VectorIndex(self.settings.index_dir, FakeEmbedder())
        self.service = MemoryService(self.repo, self.index, self.settings, clock=self.clock)
        self.service.sync_identities()

    def close(self):
        self.repo.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def local_env(ts=None, content=""):
    from memory.channels import local_envelope

    return local_envelope(content, timestamp=ts).to_dict()


def qq_private_env(user_id, ts=None, name="", content=""):
    from memory.channels import qq_envelope

    return qq_envelope({"user_id": user_id, "sender_name": name or user_id}, content, ts).to_dict()


def qq_group_env(user_id, group_id=GROUP_ID, ts=None, name="", content=""):
    from memory.channels import qq_envelope

    return qq_envelope({"user_id": user_id, "group_id": group_id, "sender_name": name or user_id}, content, ts).to_dict()
