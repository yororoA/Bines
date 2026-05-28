from __future__ import annotations

from pathlib import Path


class FileCache:
    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}

    def read(self, file_path: str | Path, encoding: str = "utf-8") -> str:
        p = Path(file_path)
        if not p.exists():
            return ""
        mtime = p.stat().st_mtime
        key = str(p.resolve())
        cached = self._cache.get(key)
        if cached is not None and cached[1] == mtime:
            return cached[0]
        content = p.read_text(encoding=encoding)
        self._cache[key] = (content, mtime)
        return content

    def invalidate(self, file_path: str | Path | None = None):
        if file_path is None:
            self._cache.clear()
        else:
            key = str(Path(file_path).resolve())
            self._cache.pop(key, None)


file_cache = FileCache()
