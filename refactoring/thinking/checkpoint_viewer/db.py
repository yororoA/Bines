import json
import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_serde = JsonPlusSerializer()

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "memory_data" / "checkpoints" / "checkpoints.db"


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = str(db_path or DEFAULT_DB_PATH)
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def list_threads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            thread_id,
            COUNT(*) AS checkpoint_count,
            MAX(checkpoint_id) AS latest_checkpoint_id
        FROM checkpoints
        WHERE checkpoint_ns = ''
        GROUP BY thread_id
        ORDER BY latest_checkpoint_id DESC
        """
    ).fetchall()

    threads = []
    for row in rows:
        meta = conn.execute(
            "SELECT metadata FROM checkpoints "
            "WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ?",
            (row["thread_id"], row["latest_checkpoint_id"]),
        ).fetchone()
        metadata = json.loads(meta["metadata"]) if meta and meta["metadata"] else {}
        threads.append({
            "thread_id": row["thread_id"],
            "checkpoint_count": row["checkpoint_count"],
            "latest_checkpoint_id": row["latest_checkpoint_id"],
            "latest_step": metadata.get("step"),
            "latest_source": metadata.get("source"),
        })
    return threads


def list_checkpoints(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT checkpoint_id, parent_checkpoint_id, type, metadata
        FROM checkpoints
        WHERE thread_id = ? AND checkpoint_ns = ''
        ORDER BY checkpoint_id DESC
        """,
        (thread_id,),
    ).fetchall()

    results = []
    for row in rows:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        results.append({
            "checkpoint_id": row["checkpoint_id"],
            "parent_checkpoint_id": row["parent_checkpoint_id"],
            "type": row["type"],
            "source": metadata.get("source"),
            "step": metadata.get("step"),
            "run_id": metadata.get("run_id"),
            "parents": metadata.get("parents"),
        })
    return results


def _deserialize_value(type_str: str | None, blob: bytes | None) -> Any:
    if blob is None:
        return None
    if type_str is None:
        type_str = "msgpack"
    try:
        return _serde.loads_typed((type_str, blob))
    except Exception:
        try:
            return json.loads(blob)
        except Exception:
            return repr(blob)


def get_checkpoint_detail(conn: sqlite3.Connection, checkpoint_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
        FROM checkpoints
        WHERE checkpoint_id = ?
        LIMIT 1
        """,
        (checkpoint_id,),
    ).fetchone()

    if not row:
        return None

    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    checkpoint_data = _deserialize_value(row["type"], row["checkpoint"])

    channel_values = {}
    if isinstance(checkpoint_data, dict):
        channel_values = checkpoint_data.get("channel_values", {})

    messages = _extract_messages(channel_values.get("messages", []))
    tasks_done = channel_values.get("tasks_done", {})
    thoughts = channel_values.get("thoughts", [])
    thread_id = channel_values.get("thread_id", row["thread_id"])

    return {
        "thread_id": thread_id,
        "checkpoint_id": row["checkpoint_id"],
        "parent_checkpoint_id": row["parent_checkpoint_id"],
        "metadata": metadata,
        "messages": messages,
        "tasks_done": _serialize_tasks(tasks_done),
        "thoughts": thoughts,
        "iteration_count": channel_values.get("iteration_count"),
        "last_task_count": channel_values.get("last_task_count"),
        "convergence_counter": channel_values.get("convergence_counter"),
        "already_said": channel_values.get("already_said", []),
        "persona_mood": channel_values.get("persona_mood", {}),
        "diary_triggered_day": channel_values.get("diary_triggered_day", ""),
        "invocation_count": channel_values.get("invocation_count"),
        "raw_channel_keys": list(channel_values.keys()),
    }


def get_checkpoint_writes(conn: sqlite3.Connection, checkpoint_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT task_id, idx, channel, type, value
        FROM writes
        WHERE checkpoint_id = ?
        ORDER BY task_id, idx
        """,
        (checkpoint_id,),
    ).fetchall()

    results = []
    for row in rows:
        value = _deserialize_value(row["type"], row["value"])
        results.append({
            "task_id": row["task_id"],
            "idx": row["idx"],
            "channel": row["channel"],
            "value": _safe_serialize(value),
        })
    return results


def _extract_messages(messages: list) -> list[dict[str, Any]]:
    extracted = []
    for msg in messages:
        if isinstance(msg, dict):
            msg_type = msg.get("type", "unknown")
            content = msg.get("content", "")
            extracted.append({"type": msg_type, "content": content})
        elif hasattr(msg, "type") and hasattr(msg, "content"):
            extracted.append({"type": msg.type, "content": str(msg.content)})
        else:
            extracted.append({"type": "unknown", "content": str(msg)})
    return extracted


def _serialize_tasks(tasks: dict) -> dict[str, list[dict[str, Any]]]:
    result = {}
    for key, items in tasks.items():
        result[key] = []
        for item in items:
            if isinstance(item, dict):
                result[key].append(item)
            elif hasattr(item, "__dict__"):
                result[key].append(item.__dict__)
            else:
                result[key].append(str(item))
    return result


def _safe_serialize(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(item) for item in obj]
    if hasattr(obj, "__dict__"):
        return _safe_serialize(obj.__dict__)
    return str(obj)
