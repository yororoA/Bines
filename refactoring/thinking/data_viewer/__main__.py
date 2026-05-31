import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Checkpoint Viewer - LangGraph checkpoint 数据浏览器")
    parser.add_argument(
        "--port", type=int, default=None,
        help="服务端口 (默认使用 thinking_settings.VIEWER_PORT 或 8501)",
    )
    parser.add_argument(
        "--db-path", type=str, default=None,
        help="SQLite checkpoint 数据库路径 (默认 memory_data/checkpoints/checkpoints.db)",
    )
    parser.add_argument(
        "--chroma-path", type=str, default=None,
        help="ChromaDB 数据目录路径 (默认 memory_data/chroma_db)",
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1",
        help="监听地址 (默认 127.0.0.1)",
    )
    args = parser.parse_args()

    port = args.port
    if port is None:
        try:
            from thinking_settings import thinking_settings
            port = thinking_settings.VIEWER_PORT
        except Exception:
            port = 8501

    db_path = args.db_path
    if db_path is None:
        db_path = Path(__file__).resolve().parents[1] / "memory_data" / "checkpoints" / "checkpoints.db"

    chroma_path = args.chroma_path
    if chroma_path is None:
        chroma_path = Path(__file__).resolve().parents[1] / "memory_data" / "chroma_db"

    if not Path(db_path).exists():
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn is not installed. Run: pip install uvicorn[standard]", file=sys.stderr)
        sys.exit(1)

    from .api import create_app

    app = create_app(db_path, chroma_path)
    print(f"Data Viewer running at http://{args.host}:{port}")
    print(f"Database: {db_path}")
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
