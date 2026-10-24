"""Memory admin web page (registered as "RAG Web"). All data access goes through the Memory Server."""
import os
import sys
import time

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

THINKING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(THINKING_DIR)
for path in (PROJECT_ROOT, os.path.join(THINKING_DIR, "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from config import HTTP_HOST, HTTP_PORTS, ZMQ_HOST, ZMQ_PORTS  # noqa: E402
from memory.client import MemoryClient, MemoryUnavailable  # noqa: E402
from memory.models import MEMORY_KINDS  # noqa: E402

app = Flask(__name__, static_folder="rag_web_static", static_url_path="")
app.config["JSON_AS_ASCII"] = False
CORS(app)
client = MemoryClient(ZMQ_HOST, ZMQ_PORTS["RAG_SERVER_REQREP"])
if not client.ping(timeout_ms=2000):
    from chatBot.memory_hub import GatewayMemoryClient
    client = GatewayMemoryClient()


def _ok(result=None, **extra):
    return jsonify({"success": True, "result": result, **extra})


@app.errorhandler(Exception)
def _error(exc):
    from werkzeug.exceptions import HTTPException
    status = (exc.code if isinstance(exc, HTTPException)
              else 400 if isinstance(exc, (ValueError, TypeError))
              else 503 if isinstance(exc, MemoryUnavailable) else 500)
    return jsonify({"success": False, "error": str(exc)}), status


def _list_arg(name):
    value = request.args.get(name) or ""
    return [item for item in value.split(",") if item] or None


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/stats")
def stats():
    return _ok(client.stats(), kinds=list(MEMORY_KINDS))


@app.route("/api/memories")
def list_memories():
    return _ok(client.list_memories(
        kinds=_list_arg("kinds"),
        scopes=_list_arg("scopes"),
        subject_id=request.args.get("subject_id") or None,
        keyword=request.args.get("keyword") or "",
        day_key=request.args.get("day_key") or "",
        offset=int(request.args.get("offset") or 0),
        limit=int(request.args.get("limit") or 50),
        order=request.args.get("order") or "recent",
    ))

@app.route("/api/nodes")
def list_nodes():
    return _ok(client.list_association_nodes(
        scopes=_list_arg("scopes"), source_type=request.args.get("source_type", ""),
        status=request.args.get("status", ""), keyword=request.args.get("keyword", ""),
        ready=int(request.args["ready"]) if request.args.get("ready") else None,
        offset=int(request.args.get("offset", 0)), limit=int(request.args.get("limit", 50)),
    ))


@app.route("/api/detail")
def memory_detail():
    result = client.memory_detail(node_id=request.args.get("node_id") or None,
                                  memory_id=request.args.get("memory_id") or None,
                                  scope=request.args.get("scope") or None)
    if result is None:
        return jsonify({"success": False, "error": "记录不存在或来源已失效"}), 404
    return _ok(result)


@app.route("/api/jobs")
def list_jobs():
    return _ok(client.list_jobs(offset=int(request.args.get("offset", 0)),
                                limit=int(request.args.get("limit", 30)),
                                status=request.args.get("status", "")))


@app.route("/api/search", methods=["POST"])
def search():
    body = request.get_json(force=True) or {}
    query = (body.get("query") or "").strip()
    scopes = body.get("scopes") or []
    if not query or not scopes:
        return jsonify({"success": False, "error": "query 和 scopes 不能为空"}), 400
    return _ok(client.search(query, scopes, body.get("kinds") or None, int(body.get("k") or 20)))


@app.route("/api/memory", methods=["POST"])
def add_memory():
    body = request.get_json(force=True) or {}
    return _ok(client.add_memory(
        body.get("kind") or "semantic", body.get("scope") or "owner", body.get("content") or "",
        subject_id=body.get("subject_id") or None, importance=body.get("importance", 5),
        day_key=body.get("day_key") or None, tags=body.get("tags") or [],
    ))


@app.route("/api/memory/<memory_id>", methods=["GET"])
def get_memory(memory_id):
    return _ok(client.get_memory(memory_id))


@app.route("/api/memory/<memory_id>", methods=["PUT"])
def update_memory(memory_id):
    body = request.get_json(force=True) or {}
    fields = {key: body[key] for key in ("content", "importance", "tags", "subject_id", "day_key") if key in body}
    return _ok(client.update_memory(memory_id, **fields))


@app.route("/api/memory/<memory_id>", methods=["DELETE"])
def delete_memory(memory_id):
    return _ok(client.delete_memory(memory_id))


@app.route("/api/conversations")
def conversations():
    return _ok(client.list_conversations())


@app.route("/api/conversations/<path:conversation_id>/messages")
def conversation_messages(conversation_id):
    return _ok(client.conversation_messages(
        conversation_id, int(request.args.get("offset") or 0), int(request.args.get("limit") or 100)
    ))


@app.route("/api/state/<scope>")
def state(scope):
    return _ok(client.get_state(scope))


@app.route("/api/life/<scope>")
def life(scope):
    return _ok(client.life_snapshot(scope))


@app.route("/api/rebuild_index", methods=["POST"])
def rebuild_index():
    return _ok(client.rebuild_index())


@app.route("/api/export")
def export():
    import json

    data = client.export(_list_arg("scopes"))
    filename = f"bines_memory_export_{time.strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    print(f"记忆管理页面: http://{HTTP_HOST}:{HTTP_PORTS['RAG_MEMORY']}", flush=True)
    app.run(host=HTTP_HOST, port=HTTP_PORTS["RAG_MEMORY"], debug=False, use_reloader=False)
