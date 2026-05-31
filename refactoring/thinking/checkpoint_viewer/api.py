from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .db import (
    get_checkpoint_detail,
    get_checkpoint_writes,
    get_connection,
    list_checkpoints,
    list_threads,
)

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Checkpoint Viewer", version="0.1.0")

    conn = get_connection(db_path)

    @app.on_event("shutdown")
    def _close_db():
        conn.close()

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

    @app.get("/", response_class=HTMLResponse)
    def index():
        html_file = _STATIC_DIR / "index.html"
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))

    return app
