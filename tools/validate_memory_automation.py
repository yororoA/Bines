"""Opt-in real-model validation; writes only isolated databases under --output.

Run: PYTHONPATH=.:thingking/src thingking/thinking_venv/bin/python tools/validate_memory_automation.py
Historical benchmark: add --snapshot PATH --samples PATH (previous benchmark JSON).
Never connects to QQ or writes the supplied snapshot.
"""
import argparse
import copy
import json
import logging
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking/src")]
from common.model_usage import capture_model_usage
from memory.channels import local_envelope
from memory.client import MemoryClient
from memory.lexical_index import LexicalIndex
from memory.repository import MemoryRepository
from memory.service import MemoryService
from memory.settings import MemorySettings
from thingking.consolidation_worker import build_worker
from thinking_model_helper import ThinkingModelHelper


class DirectClient(MemoryClient):
    def __init__(self, service):
        self.service = service

    def call(self, method, **params):
        return getattr(self.service, method)(**params)


def service_at(path):
    path.mkdir(parents=True, exist_ok=True)
    repo = MemoryRepository(path / "memory.db")
    return MemoryService(repo, LexicalIndex(repo), MemorySettings(
        data_dir=path, state_updates=False, association_backfill_batch=0,
        retention_enabled=False, job_max_attempts=1))


def cases(model, output, trials=1):
    rows = []
    fixtures = [
        ("explicit", "我住在北京", "你记错了，我一直住在上海，不是北京。", True, "上海"),
        ("date", "围巾约好周六归还", "你把日期记错了，围巾从一开始就约好周日归还，不是周六。", True, "周日"),
        ("time_change", "我住在北京", "不是你记错，我以前住北京，现在搬到上海了。", False, ""),
        ("hypothetical", "我住在北京", "如果你记错了呢？假设我住在上海，你会怎么回答？", False, ""),
        ("other_person", "我住在北京", "不是说我，是同事一直住在上海。", False, ""),
        ("command", "我住在北京", "把记忆改成我住在上海，虽然我其实仍住在北京。", False, ""),
    ]
    for name, old, user, expected, replacement in [
            (f"{name}-{trial}", old, user, expected, replacement)
            for trial in range(1, trials + 1) for name, old, user, expected, replacement in fixtures]:
        service = service_at(output / name)
        repo = service.repo
        env = local_envelope().to_dict()
        ids = service.record_turn(env, old, state_update=False)["message_ids"]
        raw = service.graph.for_message(ids[0])
        child = repo.add_memory("episode", "owner", old, meta={"source_message_ids": ids})
        service.graph.register_memory(child)
        ref = f"validation:{name}"
        service.record_message(env, "user", user, source_ref=ref + ":user", state_update=False)
        service.record_experience(env, ref + ":turn", {
            "category": "turn", "status": "completed", "turn_id": ref, "result": "收到",
            "source_message_refs": [ref + ":user"], "turn_message_refs": [ref + ":user"],
            "memory_usage": {"presented": [{"node_id": raw["id"], "node_digest": raw["digest"],
                                          "spans": [old]}], "citations": []}})
        worker = build_worker(DirectClient(service), model)
        start = time.monotonic()
        worker.run_once(["audit_memory"])
        worker.run_once(["repair_memory"])
        # Account for all resulting background work, including reassociation.
        # The fixture's original data is pre-existing input, not model-generated.
        for _ in range(32):
            if not worker.run_once(["associate", "summarize", "summarize_experience", "repair_memory"]):
                break
        corrections = service.corrections.waterline("owner")
        child_row = repo.get_memory(child)
        failures = repo.conn.execute("SELECT COUNT(*) FROM jobs WHERE status='failed'").fetchone()[0]
        pending = repo.conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
        record = json.loads(repo.conn.execute("SELECT record FROM experiences WHERE source_ref=?",
                                              (ref + ":turn",)).fetchone()[0])
        passed = (bool(corrections) == expected and not failures and not pending
                  and repo.get_message(ids[0])["content"] == old
                  and (not expected or replacement in child_row["content"]
                       and not child_row["meta"].get("repair_pending")))
        rows.append({"case": name, "passed": passed, "corrections": corrections,
                     "failed_jobs": failures, "pending_jobs": pending,
                     "seconds": round(time.monotonic() - start, 3),
                     "audit": record.get("automatic_audit"), "costs": service.model_costs.stats(),
                     "unresolved_relations": service.stats()["unresolved_associations"]})
        (output / "cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"真实模型 {name}: {'通过' if passed else '未通过'}，纠错 {corrections}，{rows[-1]['seconds']}s", flush=True)
        repo.close()
    return rows


def benchmark(model, output, snapshot, sample_file):
    target = output / "historical"
    target.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{snapshot.resolve()}?mode=ro", uri=True) as source:
        with sqlite3.connect(target / "memory.db") as destination:
            source.backup(destination)
    service = service_at(target)
    repo = service.repo
    repo.conn.execute("DELETE FROM jobs")
    sample_ids = list(dict.fromkeys(r["node_id"] for r in json.loads(sample_file.read_text())))[:3]
    if not sample_ids:
        repo.close()
        raise ValueError("Benchmark sample list is empty")
    rows = []
    for sample, nid in enumerate(sample_ids, 1):
        node = service.graph.get(nid)
        if not service.graph.valid(node):
            raise ValueError(f"Sample {sample} no longer valid")
        jid = repo.enqueue_job("associate", node["scope"], {"node_id": nid, "revision": node["revision"]},
                               f"validation:{sample}")
        job = repo.claim_job(1800, ["associate"])
        materials = service._job_materials(job)
        if not isinstance(materials, dict):
            raise ValueError(f"Sample {sample} has no materials")
        for mode in ("baseline", "compact"):
            current = copy.deepcopy(materials)
            if mode == "baseline":
                current.pop("compact", None)
                current["chains"] = [dict(r) for r in repo.conn.execute(
                    "SELECT id,reason_key,reason_text,revision FROM association_chains "
                    "WHERE scope=? ORDER BY revision DESC LIMIT 16", (node["scope"],))]
            events, links = [], []
            class Receipt:
                def complete_association(self, job_id, result_links, prior, **kwargs):
                    if mode == "compact":
                        expanded, _ = service.association_planner.decode(job, kwargs["decisions"])
                        result_links = expanded
                    texts = {n["id"]: service.graph.material(service.graph.get(n["id"]))["text"]
                             for n in [materials["source"], *materials["candidates"]]}
                    for link in result_links:
                        assert all(q["node_id"] in texts and q["quote"] in texts[q["node_id"]]
                                   for q in link["evidence"])
                    links.extend(result_links)
            worker = build_worker(Receipt(), model)
            started = time.monotonic()
            status = "passed"
            try:
                with capture_model_usage(events.append):
                    worker._associate(job, current)
            except Exception as exc:
                status = type(exc).__name__
            calls = [e for e in events if e["status"] != "started"]
            row = {"sample": sample, "mode": mode, "status": status,
                   "seconds": round(time.monotonic() - started, 3), "calls": len(calls),
                   "prompt_tokens": sum(e.get("usage", {}).get("prompt_tokens") or 0 for e in calls),
                   "completion_tokens": sum(e.get("usage", {}).get("completion_tokens") or 0 for e in calls),
                   "unknown_usage": sum(not e.get("usage", {}).get("prompt_tokens") for e in calls),
                   "valid_links": len(links),
                   "candidate_count": len(current["compact"]["candidates"]) if mode == "compact" else len(current["candidates"])}
            rows.append(row)
            (output / "benchmark.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(f"历史样本 {sample} {mode}: {status}，输入 {row['prompt_tokens']}，输出 {row['completion_tokens']}，"
                  f"{row['seconds']}s，原文核验关联 {len(links)}", flush=True)
        repo.finish_job(jid)
    repo.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        help="验证中间数据目录；默认使用系统临时目录")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--samples", type=Path)
    parser.add_argument("--benchmark-only", action="store_true")
    parser.add_argument("--trials", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    if bool(args.snapshot) != bool(args.samples):
        parser.error("--snapshot and --samples must be supplied together")
    if args.benchmark_only and not args.snapshot:
        parser.error("--benchmark-only requires --snapshot and --samples")
    for supplied in (args.snapshot, args.samples):
        if supplied and not supplied.is_file():
            parser.error(f"Input file does not exist: {supplied}")
    if args.samples:
        samples = json.loads(args.samples.read_text())
        if not isinstance(samples, list) or not samples:
            parser.error("Benchmark sample list must be non-empty")
        if any(not isinstance(row, dict) or not row.get("node_id") for row in samples):
            parser.error("Each benchmark sample requires node_id")
    args.output = args.output or Path(tempfile.gettempdir()) / f"bines-memory-validation-{time.time_ns()}"
    if args.output.exists():
        raise SystemExit("Output already exists; choose a fresh directory to preserve previous evidence.")
    args.output.mkdir(parents=True)
    print(f"验证中间数据：{args.output}", flush=True)
    logging.basicConfig(level=logging.WARNING)
    model = ThinkingModelHelper()
    case_rows = [] if args.benchmark_only else cases(model, args.output, args.trials)
    bench = benchmark(model, args.output, args.snapshot, args.samples) if args.snapshot and args.samples else []
    result = {"cases_passed": sum(r["passed"] for r in case_rows), "cases_total": len(case_rows), "benchmark": {}}
    for mode in ("baseline", "compact"):
        subset = [r for r in bench if r["mode"] == mode]
        if subset:
            result["benchmark"][mode] = {
                "samples": len(subset), "prompt_tokens": sum(r["prompt_tokens"] for r in subset),
                "completion_tokens": sum(r["completion_tokens"] for r in subset),
                "p50_seconds": statistics.median(r["seconds"] for r in subset),
                "failed": sum(r["status"] != "passed" for r in subset)}
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(any(not row["passed"] for row in case_rows) or any(r["status"] != "passed" for r in bench))


if __name__ == "__main__":
    raise SystemExit(main())
