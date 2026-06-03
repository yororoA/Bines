from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .db import (
    get_checkpoint_detail,
    get_checkpoint_writes,
    get_chroma_client,
    get_collection_items,
    get_connection,
    list_checkpoints,
    list_collections,
    list_threads,
)

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    db_path: str | Path | None = None,
    chroma_path: str | Path | None = None,
) -> FastAPI:
    conn = get_connection(db_path)
    chroma = get_chroma_client(chroma_path)

    # 修复：使用 FastAPI lifespan context manager 替代废弃的 @app.on_event("shutdown")
    # 之前使用 @app.on_event("shutdown")，在新版 FastAPI 中已废弃
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        conn.close()

    app = FastAPI(title="Data Viewer", version="0.2.0", lifespan=lifespan)

    @app.get("/api/threads")
    def api_list_threads() -> list[dict[str, Any]]:
        return list_threads(conn)

    @app.get("/api/threads/{thread_id}/checkpoints")
    def api_list_checkpoints(thread_id: str) -> list[dict[str, Any]]:
        rows = list_checkpoints(conn, thread_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found")
        return rows

    @app.get("/api/checkpoints/{checkpoint_id}")
    def api_checkpoint_detail(checkpoint_id: str) -> dict[str, Any]:
        detail = get_checkpoint_detail(conn, checkpoint_id)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Checkpoint '{checkpoint_id}' not found")
        detail["writes"] = get_checkpoint_writes(conn, checkpoint_id)
        return detail

    @app.get("/api/collections")
    def api_list_collections() -> list[dict[str, Any]]:
        return list_collections(chroma)

    @app.get("/api/collections/{name}/items")
    def api_collection_items(
        name: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
        q: str | None = Query(None),
    ) -> dict[str, Any]:
        try:
            return get_collection_items(chroma, name, offset, limit, q)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Collection '{name}' not found: {e}")

    @app.get("/", response_class=HTMLResponse)
    def index():
        html_file = _STATIC_DIR / "index.html"
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))

    return app
