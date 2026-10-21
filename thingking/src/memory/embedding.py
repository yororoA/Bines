"""Embedding model loader (heavy dependencies are imported only inside the Memory Server)."""
from __future__ import annotations

import os
import threading
from typing import List, Protocol, Sequence


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...


_lock = threading.Lock()
_cached = None


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

        from langchain_huggingface import HuggingFaceEmbeddings

        model_name = os.environ.get("RAG_EMBEDDING_MODEL", RAG_EMBEDDING_MODEL)
        device = _detect_device()
        print(f"[Memory] Loading embedding model {model_name} on {device}", flush=True)
        _cached = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": device, "trust_remote_code": True},
            encode_kwargs={"normalize_embeddings": True},
        )
        return _cached
