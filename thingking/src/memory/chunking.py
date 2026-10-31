"""Model-tokenizer windows retain original text and reserve room for key context."""
from __future__ import annotations

import hashlib
import json
import re


class EmbeddingChunks:
    def __init__(self, embedder):
        delegate = getattr(embedder, "delegate", embedder)
        client = getattr(delegate, "_client", delegate)
        tokenizer = getattr(client, "tokenizer", None)
        self._tokenizer = None
        maximum = getattr(client, "max_seq_length", None)
        if tokenizer is not None and isinstance(maximum, int) and maximum > 8:
            backend = getattr(tokenizer, "backend_tokenizer", None)
            if backend is not None:
                # Independent tokenizer state: document embedding may tokenize on
                # another thread while foreground FTS keys are being prepared.
                from tokenizers import Tokenizer
                serialized = backend.to_str()
                self._tokenizer = Tokenizer.from_str(serialized)
                self._tokenizer.no_truncation()
                self._tokenizer.no_padding()
                special = tokenizer.num_special_tokens_to_add(pair=False)
                self.limit = max(1, min(maximum, 8192) - special)
                self.reserve = min(48, self.limit // 4)
                self.body_limit = max(1, self.limit - self.reserve)
                self.overlap = min(24, self.body_limit // 8)
                token_hash = hashlib.sha256(serialized.encode()).hexdigest()
                self.signature = json.dumps(["token-spans-v1", token_hash, self.limit,
                                             self.body_limit, self.overlap])
                return
        # Custom embedders without a tokenizer retain the legacy text contract.
        self.limit, self.body_limit, self.overlap = 700, 700, 80
        self.signature = "character-fallback-v2:700:80"

    def _offsets(self, text):
        if self._tokenizer is None:
            return [(i, i + 1) for i in range(len(text))]
        return self._tokenizer.encode(text.replace("\n", " "), add_special_tokens=False).offsets

    def fit(self, text):
        if self._tokenizer is None:
            return text
        return self._fit(text, self.limit)

    def _fit(self, text, limit):
        offsets = self._offsets(text)
        if len(offsets) <= limit:
            return text
        end = offsets[limit - 1][1]
        # Byte tokenizers may assign several tokens the same Unicode span.
        # Re-tokenizing a complete-character prefix is the final budget check.
        while end and len(self._offsets(text[:end])) > limit:
            end -= 1
        return text[:end]

    def split(self, text):
        if self._tokenizer is None:
            from .evidence import _chunks
            return list(_chunks(text))
        offsets = self._offsets(text)
        if not offsets:
            return [text] if text else []
        chunks, start, previous_end = [], 0, 0
        while start < len(offsets):
            end = min(len(offsets), start + self.body_limit)
            lo = previous_end if self.overlap == 0 else (0 if start == 0 else offsets[start][0])
            hi = offsets[end - 1][1]
            if end < len(offsets):
                # Prefer a sentence boundary in the last third of this window.
                boundaries = list(re.finditer(r"[。！？!?\n]|[.]\s", text[lo:hi]))
                threshold = offsets[start + max(1, (end - start) * 2 // 3) - 1][1]
                if boundaries and lo + boundaries[-1].end() >= threshold:
                    hi = lo + boundaries[-1].end()
                    while end > start + 1 and offsets[end - 1][0] >= hi:
                        end -= 1
            else:
                hi = len(text)
            chunk = self._fit(text[lo:hi], self.body_limit)
            if not chunk:
                raise ValueError("embedding token window cannot contain the next character")
            hi = lo + len(chunk)
            while end > start and offsets[end - 1][1] > hi:
                end -= 1
            chunks.append(chunk)
            previous_end = hi
            if end == len(offsets):
                break
            start = max(start + 1, end - self.overlap)
        return chunks
