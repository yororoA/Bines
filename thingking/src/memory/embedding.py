"""Embedding model loader (heavy dependencies are imported only inside the Memory Server)."""
from __future__ import annotations

import os
import threading
import hashlib
import json
from pathlib import Path
from typing import List, Protocol, Sequence


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...


_lock = threading.Lock()
_cached = None


def embedding_identity(embedder):
    """Semantic identity includes dimensions; custom embedders get a probe digest."""
    vector = embedder.embed_query("Bines embedding identity / 向量兼容性")
    identity = dict(getattr(embedder, "bines_identity", {}) or {})
    identity["dimensions"] = len(vector)
    if not identity.get("model_revision"):
        identity["implementation"] = f"{type(embedder).__module__}.{type(embedder).__qualname__}"
        identity["probe_sha256"] = hashlib.sha256(
            json.dumps([round(float(x), 7) for x in vector]).encode()).hexdigest()
    identity["key_schema"] = 3
    return identity


class _IdentifiedEmbedder:
    def __init__(self, delegate, identity):
        self.delegate, self.bines_identity = delegate, identity

    def embed_documents(self, texts):
        return self.delegate.embed_documents(texts)

    def embed_query(self, text):
        return self.delegate.embed_query(text)


def _revision(path):
    """Hash local config and weight identities without reading GB-sized weights."""
    path = Path(path)
    if not path.is_dir():
        return ""
    if path.parent.name == "snapshots":
        return path.name
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_file() and item.suffix in {".json", ".safetensors", ".bin", ".model", ".txt"}:
            stat = item.stat()
            content = hashlib.sha256(item.read_bytes()).hexdigest() if stat.st_size <= 1024 * 1024 else None
            files.append([str(item.relative_to(path)), stat.st_size, content or stat.st_mtime_ns])
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()


def _detect_device() -> str:
    forced = os.environ.get("BINES_EMBEDDING_DEVICE", "").strip()
    if forced:
        return forced
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def load_embedder() -> Embedder:
    """Load the sentence-transformers model configured by ``RAG_EMBEDDING_MODEL`` once per process."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        from config import HF_DOWNLOAD_TIMEOUT, HF_ENDPOINT_DEFAULT, RAG_EMBEDDING_MODEL

        if not os.environ.get("HF_ENDPOINT") and not os.environ.get("HF_MIRROR"):
            os.environ["HF_ENDPOINT"] = HF_ENDPOINT_DEFAULT
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", str(HF_DOWNLOAD_TIMEOUT))
        os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", str(HF_DOWNLOAD_TIMEOUT))

        model_name = os.environ.get("RAG_EMBEDDING_MODEL", RAG_EMBEDDING_MODEL)
        revision = os.environ.get("RAG_EMBEDDING_REVISION", "").strip() or None
        device = _detect_device()
        print(f"[Memory] Loading embedding model {model_name} on {device}", flush=True)
        from huggingface_hub import snapshot_download
        from langchain_huggingface import HuggingFaceEmbeddings

        options = {
            "encode_kwargs": {"normalize_embeddings": True},
            "model_kwargs": {"device": device, "trust_remote_code": True},
        }
        try:
            # Resolve the repository id to a concrete snapshot directory first.
            # Passing a filesystem path prevents optional-file HEAD requests that
            # some Transformers components make even with local_files_only=True.
            local_model = (model_name if Path(model_name).is_dir() else
                           snapshot_download(repo_id=model_name, revision=revision, local_files_only=True))
        except Exception:
            if os.environ.get("BINES_EMBEDDING_OFFLINE", "").lower() in {"1", "true", "yes", "on"}:
                raise
            print(f"[Memory] Local model cache is incomplete; downloading {model_name}", flush=True)
            local_model = snapshot_download(repo_id=model_name, revision=revision)
        delegate = HuggingFaceEmbeddings(model_name=local_model, **options)
        _cached = _IdentifiedEmbedder(delegate, {
            "model": model_name, "requested_revision": revision or "default",
            "model_revision": _revision(local_model), "normalize_embeddings": True,
            "preprocessing": "langchain_huggingface:newlines-to-spaces:v1",
        })
        return _cached
