"""Run the configured real model against recent source text in an isolated DB.

Production is opened read-only. No QQ messages or production memory writes.
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking"), str(ROOT / "thingking" / "src")]

from memory.models import day_key
from memory.repository import MemoryRepository
from memory.lexical_index import LexicalIndex
from memory.service import MemoryService
from memory.settings import MemorySettings, load_settings
from memory.server import dispatch
from memory.channels import local_envelope
from consolidation_worker import build_worker


def main():
    db_path = load_settings().db_path
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    rows = source.execute(
        "SELECT id,role,content,ts,day_key,conversation_id FROM messages WHERE scope='owner' "
        "AND role IN ('user','assistant') AND origin NOT IN ('recalled','withdrawn','deleted','retracted','system') "
        "ORDER BY ts DESC,id DESC LIMIT 160").fetchall()
    source.close()
    groups = {}
    for row in reversed(rows):
        groups.setdefault((row["day_key"], row["conversation_id"]), []).append(dict(row))
    selected = next((group for group in reversed(list(groups.values()))
                     if sum(r["role"] == "user" for r in group) >= 4), None)
    if not selected:
        raise RuntimeError("至少需要同一天同一会话的四条真实用户消息")
    turns = []
    for row in selected:
        if row["role"] == "user":
            turns.append([row])
        elif turns and len(turns[-1]) == 1:
            turns[-1].append(row)
    turns = turns[-4:]
    current_time = [max(r["ts"] for r in selected) + 86400 * 2]
    clock = lambda: current_time[0]
    with tempfile.TemporaryDirectory(prefix="bines-live-memory-") as directory:
        repo = MemoryRepository(":memory:", clock)
        settings = MemorySettings(data_dir=Path(directory), state_updates=False, retention_enabled=True,
                                  summarize_trigger=8, summarize_chunk=8, summarize_idle_min=4)
        service = MemoryService(repo, LexicalIndex(repo), settings, clock)
        nodes = []
        for turn in turns:
            env = local_envelope(timestamp=turn[0]["ts"]).to_dict()
            result = service.record_turn(env, turn[0]["content"], turn[1]["content"] if len(turn) > 1 else "")
            nodes.append(service.graph.for_message(result["message_ids"][0])["id"])

        class Client:
            def claim_job(self, kinds=None):
                return service.claim_job(kinds)

            def __getattr__(self, method):
                def call(*args, **params):
                    if args:
                        return getattr(service, method)(*args, **params)
                    result = dispatch(service, {"method": method, "params": params})
                    if not result["ok"]:
                        raise RuntimeError(result["error"])
                    return result["result"]
                return call

        worker = build_worker(Client())
        for iteration in range(20):
            service.enqueue_due_diaries()
            worked = worker.run_once()
            print(f"真实模型验证 · 完成第 {iteration + 1} 次任务", flush=True)
            if not worked:
                pending = repo.conn.execute(
                    "SELECT MIN(available_at) FROM jobs WHERE status='pending'").fetchone()[0]
                if pending is None:
                    break
                current_time[0] = max(clock() + 1, pending + 1)
        open_jobs = repo.conn.execute(
            "SELECT kind,status,last_error FROM jobs WHERE status!='done'").fetchall()
        if open_jobs:
            raise RuntimeError("存在未完成任务：" + str([dict(r) for r in open_jobs]))
        counts = {r["kind"]: r["n"] for r in repo.conn.execute(
            "SELECT kind,COUNT(*) n FROM jobs WHERE status='done' GROUP BY kind")}
        statuses = [service.graph.get(n)["retrieval_status"] for n in nodes]
        assert counts.get("summarize") == 1 and counts.get("diary") == 1, counts
        assert statuses.count("hot") == 3 and statuses.count("cold") == 1, statuses
        assert all(repo.get_message(i)["episode_id"] for n in nodes for i in service.graph.get(n)["message_ids"])
        snapshots = [service.retention.get(r[0]) for r in repo.conn.execute("SELECT job_id FROM consolidation_snapshots")]
        assert sorted(len(s["data"]["protected"]) for s in snapshots) == [1, 3]
        print(json.dumps({"success": True, "real_model": True, "production_read_only": True,
                          "retrieval": "isolated lexical index", "source_message_ids": [r["id"] for t in turns for r in t],
                          "source_day": day_key(turns[0][0]["ts"]), "jobs": counts,
                          "association_edges": service.stats()["association_edges"],
                          "raw_hot": 3, "raw_cold": 1, "snapshot_protected": [1, 3]}, ensure_ascii=False), flush=True)
        repo.close()


if __name__ == "__main__":
    main()
