"""Offline, source-frozen retrieval evaluation. Private artifacts must stay ignored."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".tools" / "memory_export"
INITIAL_COMMIT = "f9bb5ae5415763430cb4c6e516a84ee9560da31a"
INITIAL_SOURCE_SHA256 = "5d0d5fdaa3b41ad3eb16a62def283073f0c26c6c40f2a5777b8a8dfcc92e95bd"
SCHEMA_VERSION = 1
CATEGORIES = {
    "literal", "synonym", "proper_name_number", "multi_evidence",
    "temporal", "unanswerable", "scope_isolation",
}
EXTRA_SOURCE_FILES = ("common/env.py", "config.py", "config.example.py", "runtime_environments.py")
PROMPT_SOURCE_FILES = ("thingking/src/prompt_builder.py", "thingking/src/persona_profile.py",
                       "thingking/src/relationship_state.py", "thingking/persona/persona.yaml",
                       "common/clock.py", "common/context_budget.py")
KS = (1, 5, 10)
sys.dont_write_bytecode = True


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class EvaluationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise EvaluationError(message)


def private_path(path):
    path = path.expanduser().absolute()
    require(path.resolve().is_relative_to(PRIVATE.resolve()), "artifacts must be inside .tools/memory_export")
    require(not any(p.is_symlink() for p in (path, *path.parents)), "artifact symlinks are not allowed")
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", str(path)], cwd=ROOT, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    require(result.returncode == 0, "artifact path must be git-ignored")
    return path


def current_sources():
    paths = list((ROOT / "thingking/src/memory").rglob("*.py"))
    paths.extend(ROOT / name for name in EXTRA_SOURCE_FILES)
    paths.extend(ROOT / name for name in PROMPT_SOURCE_FILES)
    return {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in sorted(paths)}


def initial_sources():
    names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", INITIAL_COMMIT, "--", "thingking/src/memory"],
        cwd=ROOT, text=True,
    ).splitlines()
    names = [name for name in names if name.endswith(".py")] + list(EXTRA_SOURCE_FILES)
    return {
        name: subprocess.check_output(["git", "show", f"{INITIAL_COMMIT}:{name}"], cwd=ROOT, text=True)
        for name in names
    }


def materialize_sources(files, target):
    for name, content in files.items():
        path = Path(name)
        require(not path.is_absolute() and ".." not in path.parts, "unsafe source snapshot path")
        require(
            name in (*EXTRA_SOURCE_FILES, *PROMPT_SOURCE_FILES)
            or (name.startswith("thingking/src/memory/") and name.endswith(".py")),
            "unexpected source snapshot entry",
        )
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")


@contextmanager
def readonly_database(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def sqlite_backup(source, destination):
    with readonly_database(source) as src:
        dest = sqlite3.connect(destination)
        try:
            src.backup(dest)
        finally:
            dest.close()


def data_manifest(data_dir):
    paths = [data_dir / "memory.db"]
    wal = data_dir / "memory.db-wal"
    if wal.exists():
        paths.append(wal)
    index = data_dir / "index"
    if index.exists():
        require(not index.is_symlink(), "index symlinks are not allowed")
        paths.extend(p for p in index.rglob("*") if p.is_file() and not p.name.endswith("-shm"))
    require(not any(p.is_symlink() for p in paths), "data symlinks are not allowed")
    return {str(p.relative_to(data_dir)): file_digest(p) for p in sorted(paths)}


def snapshot_data(source, target):
    target.mkdir()
    sqlite_backup(source / "memory.db", target / "memory.db")
    index = source / "index"
    if index.exists():
        require(not any(p.is_symlink() for p in index.rglob("*")), "index symlinks are not allowed")
        # SQLite files use backup, not a torn copy of a possibly live WAL database.
        shutil.copytree(index, target / "index", ignore=shutil.ignore_patterns("*.sqlite3*", "*.db*"))
        for path in index.rglob("*"):
            if path.is_file() and path.suffix in {".sqlite3", ".db"}:
                destination = target / "index" / path.relative_to(index)
                destination.parent.mkdir(parents=True, exist_ok=True)
                sqlite_backup(path, destination)


def database_digest(conn):
    result = hashlib.sha256()
    schema = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    result.update(canonical(schema).encode())
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        escaped = table.replace('"', '""')
        rows = sorted(canonical(row) for row in conn.execute(f'SELECT * FROM "{escaped}"'))
        result.update(canonical([table, rows]).encode())
    return result.hexdigest()


def catalog_from(conn):
    catalog = {}
    for kind, table, where in (("memory", "memories", " WHERE deleted=0"), ("message", "messages", "")):
        for row in conn.execute(f"SELECT id,scope,content,source_ref FROM {table}{where}"):
            catalog[f"{kind}:{row[0]}"] = dict(zip(("id", "scope", "content", "source_ref"), row))
    return catalog


def load_dataset(path, catalog):
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    require(len(rows) >= 20, "at least 20 annotated queries are required")
    require(len({q["id"] for q in rows}) == len(rows), "duplicate query IDs")
    require(CATEGORIES <= {q["category"] for q in rows}, "required evaluation categories are missing")
    for q in rows:
        label = q["id"]
        require(isinstance(q.get("question"), str) and q["question"].strip(), f"{label}: empty question")
        scopes = q.get("scopes")
        require(isinstance(scopes, list) and scopes and all(isinstance(s, str) and s for s in scopes),
                f"{label}: invalid scopes")
        require(len(scopes) == len(set(scopes)), f"{label}: repeated scopes")
        gold = q.get("relevant_sources")
        require(isinstance(gold, list) and len(gold) == len(set(gold)), f"{label}: invalid gold sources")
        require(type(q.get("answerable")) is bool and q["answerable"] == bool(gold),
                f"{label}: answerability and gold sources disagree")
        require(bool(q.get("expected_answer")) or bool(q.get("required_terms")), f"{label}: missing answer label")
        if not q["answerable"]:
            require(bool(q.get("negative_review")), f"{label}: missing negative review")
        forbidden = q.get("forbidden_sources", [])
        if q["category"] == "scope_isolation":
            require(bool(forbidden), f"{label}: isolation query needs forbidden sources")
        if q["category"] == "multi_evidence":
            require(len(gold) > 1, f"{label}: multi-evidence query needs multiple sources")
        for ref in [*gold, *forbidden]:
            require(ref in catalog, f"{label}: missing or deleted annotated source")
            require((catalog[ref]["scope"] in scopes) == (ref in gold),
                    f"{label}: annotated source scope mismatch")
            quote = q.get("evidence_quotes", {}).get(ref)
            require(isinstance(quote, str) and quote and quote in catalog[ref]["content"],
                    f"{label}: reviewed quotation no longer matches")
        if q.get("paired_with"):
            paired = next((p for p in rows if p["id"] == q["paired_with"]), None)
            require(paired is not None and paired["answerable"] and paired["question"] == q["question"]
                    and paired["scopes"] != scopes, f"{label}: invalid isolation pair")
    return rows, hashlib.sha256(raw).hexdigest()


def resolve_evidence(item, catalog, aliases):
    kind = item.get("source_type")
    kind = {"memories": "memory", "messages": "message"}.get(kind, kind)
    refs = []
    source_id = item.get("source_id")
    if source_id is not None and kind in {"memory", "message"}:
        value = str(source_id)
        refs.append(value if value.startswith(f"{kind}:") else f"{kind}:{value}")
    for field, prefix in (("memory_id", "memory"), ("message_id", "message")):
        if item.get(field) is not None:
            value = str(item[field])
            refs.append(value if value.startswith(f"{prefix}:") else f"{prefix}:{value}")
    source_ref = item.get("source_ref")
    if source_ref:
        matches = [source_ref] if source_ref in catalog else aliases.get(source_ref, [])
        if kind in {"memory", "message"}:
            matches = [ref for ref in matches if ref.startswith(kind + ":")]
        if len(matches) == 1:
            refs.extend(matches)
        elif not refs:
            return None
    refs = list(dict.fromkeys(refs))
    return refs[0] if len(refs) == 1 and refs[0] in catalog else None


def normalize_candidates(candidates, backend, catalog, scopes):
    require(isinstance(candidates, list), "service must return a list of candidates")
    aliases = {}
    for ref, row in catalog.items():
        if row["source_ref"]:
            aliases.setdefault(row["source_ref"], []).append(ref)
    ranked, seen, leaks, unresolved = [], set(), set(), 0
    for i, candidate in enumerate(candidates):
        require(isinstance(candidate, dict), "candidate must be an object")
        if candidate.get("scope") and candidate["scope"] not in scopes:
            leaks.add(f"candidate:{i}")
        metadata = candidate.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("scope") and metadata["scope"] not in scopes:
            leaks.add(f"metadata:{i}")
        for field, prefix in (("memory_id", "memory"), ("message_id", "message")):
            value = str(candidate.get(field, ""))
            ref = value if value.startswith(prefix + ":") else prefix + ":" + value
            if ref in catalog and catalog[ref]["scope"] not in scopes:
                leaks.add(ref)
        if backend == "hybrid":
            require(isinstance(candidate.get("evidence"), list), "hybrid candidate requires an evidence list")
        evidence = candidate.get("evidence")
        if evidence is None:
            evidence = [{"source_type": candidate.get("_source_type", "memory"),
                         "source_id": candidate.get("id")}]
        elif not evidence:
            evidence = [candidate]
        for j, item in enumerate(evidence):
            require(isinstance(item, dict), "evidence must be an object")
            ref = resolve_evidence(item, catalog, aliases)
            if ref is None:
                unresolved += 1
                ref = f"unresolved:{i}:{j}"
            elif catalog[ref]["scope"] not in scopes:
                leaks.add(ref)
            if item.get("scope") and item["scope"] not in scopes:
                leaks.add(f"evidence:{i}:{j}")
            if ref not in seen:
                ranked.append(ref)
                seen.add(ref)
    return ranked, sorted(leaks), unresolved


def query_metrics(ranked, gold, candidate_count, ks=KS):
    gold = set(gold)
    scores = {}
    for k in ks:
        top = ranked[:k]
        found = gold.intersection(top)
        first = next((i for i, ref in enumerate(top, 1) if ref in gold), None)
        dcg = sum(1 / math.log2(i + 1) for i, ref in enumerate(top, 1) if ref in gold)
        ideal = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(gold)) + 1))
        scores[str(k)] = {
            "evidence_recall": len(found) / len(gold) if gold else None,
            "mrr": 1 / first if first else (0.0 if gold else None),
            "ndcg": dcg / ideal if ideal else None,
            "all_evidence": float(found == gold) if len(gold) > 1 else None,
        }
    return {
        "at_k": scores,
        "unanswerable_retrieval_false_positive": bool(candidate_count) if not gold else None,
    }


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def average(values):
    values = [value for value in values if value is not None]
    return statistics.fmean(values) if values else None


def summarize(rows):
    latencies = [ms for row in rows for ms in row["latency_ms"]]
    return {
        "queries": len(rows),
        "answerable_queries": sum(row["answerable"] for row in rows),
        "multi_evidence_queries": sum(len(row["relevant_sources"]) > 1 for row in rows),
        "unanswerable_queries": sum(not row["answerable"] for row in rows),
        "at_k": {
            str(k): {metric: average(row["metrics"]["at_k"][str(k)][metric] for row in rows)
                     for metric in ("evidence_recall", "mrr", "ndcg", "all_evidence")}
            for k in KS
        },
        "unanswerable_retrieval_false_positive_rate": average(
            row["metrics"]["unanswerable_retrieval_false_positive"] for row in rows
        ),
        "scope_leak_query_rate": average(bool(row["scope_leaks"]) for row in rows),
        "scope_leak_queries": sum(bool(row["scope_leaks"]) for row in rows),
        "unresolved_evidence_count": sum(row["unresolved_evidence_count"] for row in rows),
        "unstable_queries": sum(row["unstable"] for row in rows),
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "answer_correctness": {"status": "not_evaluated", "reason": "No LLM or answer judge was called."},
        "prompt_quality": {key: average(row.get("prompt_quality", {}).get(key) for row in rows)
                           for key in ("answer_span_coverage", "retrieved_designated_source_retention",
                                       "designated_source_quote_presentation", "presented_item_rate",
                                       "temporal_label_coverage", "expired_current_profile_count")},
    }


def envelope_for(q, clock):
    scope = q["scopes"][0]
    require(len(q["scopes"]) == 1, "build_context supports exactly one scope per query")
    env = {"source": "retrieval_eval", "channel_type": "local", "conversation_id": "local",
           "actor_id": "", "timestamp": clock, **q.get("envelope", {})}
    if scope == "owner":
        return env
    parts = scope.split(":")
    if len(parts) == 2 and parts[0] in {"qq_group", "qq_private"}:
        kind, identifier, account = parts[0].removeprefix("qq_"), parts[1], ""
    elif len(parts) == 4 and parts[0] == "qq" and parts[2] in {"group", "private"}:
        _, account, kind, identifier = parts
    else:
        raise EvaluationError("cannot infer context envelope; unsupported scope format")
    env.update(channel_type=f"qq_{kind}", external_account_id=account)
    env["external_group_id" if kind == "group" else "external_user_id"] = identifier
    return env


def retrieve(service, q, api, clock):
    if api == "search":
        return service.search(q["question"], q["scopes"], k=max(KS))
    context = service.build_context(envelope_for(q, clock), query=q["question"])
    require(context["scope"] in q["scopes"], "context resolved to an unexpected scope")
    result = []
    # Context is not a ranked search API. This fixed export order is reported separately.
    for section in ("recalled", "profile", "bridge", "working", "other_channels"):
        values = context.get(section) or []
        if isinstance(values, dict):
            values = [values]
        for value in values:
            result.append({**value, "_context_section": section,
                           "_source_type": "message" if section in {"working", "other_channels"} else "memory"})
    return result


def prompt_quality(service, q, clock):
    """Measure final presentation, separately from retrieval and answer accuracy.

    Exact required-term/answer-span presence does not prove a correct answer.
    No model or judge is called, and presentation probing creates no usage events.
    """
    from unittest.mock import patch
    import prompt_builder
    from memory.retrieval import time_intent
    context = service.build_context(envelope_for(q, clock), query=q["question"])
    presented = []
    class Probe:
        def present_memory(self, item, spans):
            presented.append((item, list(spans)))
    with patch.object(prompt_builder, "_runtime_journal", return_value=Probe()), \
            patch.object(prompt_builder, "clock_message", return_value=prompt_builder.clock_message(clock)):
        messages = prompt_builder.build_conversation_messages(context)
    text = "\n".join(m["content"] for m in messages)
    labels = q.get("required_terms") or ([q["expected_answer"]] if q.get("expected_answer") else [])
    labels = [labels] if isinstance(labels, str) else labels
    items = [*(context.get("profile") or []), *(context.get("recalled") or [])]
    if context.get("bridge"):
        items.append(context["bridge"])
    sources, quotes, times = [], [], []
    shown = {item["id"] for item, _ in presented if item.get("id")}
    for item in items:
        source = (item.get("meta") or {}).get("fact_source_message_id")
        evidence = item.get("evidence") or []
        if source and evidence:
            sources.append(any(e.get("source_type") == "message" and str(e.get("source_id")) == str(source)
                               for e in evidence))
            originals = [e.get("content", "") for e in evidence
                         if e.get("source_type") == "message" and str(e.get("source_id")) == str(source)]
            quotes.append(any(span and any(span in original for original in originals)
                              for shown_item, spans in presented if shown_item.get("id") == item.get("id")
                              for span in spans))
        citation = item.get("citation_id")
        if item.get("temporal") and citation and citation in text:
            times.append(prompt_builder._temporal_label(item).strip() in text)
    historical = time_intent(q["question"])[1]
    return {
        "answer_span_coverage": average(label in text for label in labels),
        "retrieved_designated_source_retention": average(sources),
        "designated_source_quote_presentation": average(quotes),
        "presented_item_rate": average(item.get("id") in shown for item in items),
        "temporal_label_coverage": average(times),
        "expired_current_profile_count": (sum(not item["temporal"]["is_current"] for item in context.get("profile") or []
                                               if item.get("temporal")) if not historical else None),
        "context_budget": context.get("context_budget"),
        "answer_correctness": "not_evaluated",
    }


class IndexProbe:
    """Make swallowed vector failures visible; do not change scores or candidates."""

    def __init__(self, index):
        self.index = index
        self.failures = 0

    def __getattr__(self, name):
        target = getattr(self.index, name)
        if name != "query_keys":
            return target
        # Preserve hasattr() capability detection for lexical/legacy indexes.
        def query_keys(*args, **kwargs):
            try:
                return target(*args, **kwargs)
            except Exception:
                self.failures += 1
                raise
        return query_keys

    def query(self, *args, **kwargs):
        try:
            return self.index.query(*args, **kwargs)
        except Exception:
            self.failures += 1
            raise


@contextmanager
def offline_guard(temp_root):
    active = [True]

    system_temp = Path(tempfile.gettempdir()).resolve()

    def check_path(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = Path(os.fsdecode(path)).resolve()
            # 允许框架自身的系统临时文件；项目数据、代码与配置仍只能写临时副本。
            require(resolved.is_relative_to(temp_root) or resolved.is_relative_to(system_temp),
                    f"backend attempted a write outside its temporary sandbox: {resolved}")

    def audit(event, args):
        if not active[0]:
            return
        if event.startswith("socket.") and event in {
            "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto", "socket.bind"
        }:
            raise PermissionError("network access is disabled for evaluation")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            raise PermissionError("backend subprocesses are disabled for evaluation")
        if event == "sqlite3.connect" and args[0] != ":memory:":
            check_path(args[0])
        if event == "open":
            path, mode, flags = args
            if (mode and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                check_path(path)
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate"}:
            check_path(args[0])
        if event in {"os.rename", "os.link", "os.symlink"}:
            check_path(args[0])
            check_path(args[1])

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active[0] = False


def runtime_environment(temp_root, data_dir):
    os.environ.update({
        "BINES_ENV_FILE": str(temp_root / "absent.env"),
        "BINES_MEMORY_DIR": str(data_dir),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "ANONYMIZED_TELEMETRY": "False", "TOKENIZERS_PARALLELISM": "false",
        "XDG_CACHE_HOME": str(temp_root / "cache"), "MPLCONFIGDIR": str(temp_root / "mpl"),
    })
    cache = ROOT / "thingking/models/hf-cache"
    os.environ.setdefault("HF_HOME", str(temp_root / "hf-home"))
    os.environ.setdefault("HF_HUB_CACHE", str(temp_root / "hf-home" / "hub"))
    local_model = ROOT / "thingking/models/all-MiniLM-L6-v2"
    if local_model.is_dir():
        os.environ.setdefault("RAG_EMBEDDING_MODEL", str(local_model))


def evaluate(args, source_files, qrows, catalog, data_dir, code_dir, clock):
    runtime_environment(code_dir.parent, data_dir)
    materialize_sources(source_files, code_dir)
    sys.path[:0] = [str(code_dir / "thingking/src"), str(code_dir)]
    for name in list(sys.modules):
        if name == "memory" or name.startswith("memory.") or name in {
            "common", "common.env", "common.clock", "config", "runtime_environments",
            "prompt_builder", "persona_profile", "relationship_state"}:
            del sys.modules[name]
    with offline_guard(code_dir.parent):
        repository = importlib.import_module("memory.repository")
        settings_module = importlib.import_module("memory.settings")
        service_module = importlib.import_module("memory.service")
        settings = settings_module.MemorySettings(data_dir=data_dir, state_updates=False)
        repo = repository.MemoryRepository(settings.db_path, clock=lambda: clock)
        try:
            started = time.perf_counter()
            if args.vector:
                vector = importlib.import_module("memory.vector_index")
                embedding = importlib.import_module("memory.embedding")
                index = vector.VectorIndex(settings.index_dir, embedding.load_embedder())
            else:
                lexical = importlib.import_module("memory.lexical_index")
                index = lexical.LexicalIndex(repo)
            probe = IndexProbe(index)
            service = service_module.MemoryService(repo, probe, settings, clock=lambda: clock)
            if args.vector:
                service.reconcile_index()
            initialization_ms = (time.perf_counter() - started) * 1000
            api = args.baseline_api if args.backend == "baseline" else "search"
            rows = []
            for q in qrows:
                warmup_ms, latency_ms, rankings, candidates = [], [], [], None
                for iteration in range(args.warmup + args.repeats):
                    started = time.perf_counter()
                    returned = retrieve(service, q, api, clock)
                    elapsed = (time.perf_counter() - started) * 1000
                    require(probe.failures == 0, "index query failed; refusing a silently degraded run")
                    ranked, leaks, unresolved = normalize_candidates(returned, args.backend, catalog, q["scopes"])
                    if iteration < args.warmup:
                        warmup_ms.append(elapsed)
                    else:
                        latency_ms.append(elapsed)
                        rankings.append(digest([ranked, leaks, unresolved]))
                        if candidates is None:
                            candidates = returned
                            first = ranked, leaks, unresolved
                ranked, leaks, unresolved = first
                presentation = (prompt_quality(service, q, clock) if all(name in source_files for name in PROMPT_SOURCE_FILES)
                                else {"status": "not_evaluated", "reason": "frozen baseline lacks prompt sources"})
                rows.append({
                    **q, "ranked_evidence": ranked, "candidates": candidates,
                    "scope_leaks": leaks, "unresolved_evidence_count": unresolved,
                    "latency_ms": latency_ms, "warmup_latency_ms": warmup_ms,
                    "latency_p50_ms": percentile(latency_ms, 0.5),
                    "latency_p95_ms": percentile(latency_ms, 0.95),
                    "unstable": len(set(rankings)) > 1,
                    "metrics": query_metrics(ranked, q["relevant_sources"], len(candidates)),
                    "answer_correctness": "not_evaluated",
                    "prompt_quality": presentation,
                })
            settings_dict = asdict(settings)
            settings_dict["data_dir"] = "<temporary>"
            settings_dict["owner_qq_ids"] = sorted(settings_dict["owner_qq_ids"])
            return rows, settings_dict, initialization_ms
        finally:
            repo.close()


def comparison(reference, report):
    reasons = []
    for key in ("dataset_sha256", "corpus_sha256", "api", "vector", "ks", "settings", "clock"):
        if reference[key] != report[key]:
            reasons.append(key + " differs")
    if report["vector"] and reference["index_sha256"] != report["index_sha256"]:
        reasons.append("index_sha256 differs")
    if reference["protocol"] != report["protocol"]:
        reasons.append("evaluation protocol differs")
    if reasons:
        return {"comparable": False, "reasons": reasons}
    before, after = reference["summary"], report["summary"]
    return {
        "comparable": True,
        "at_k_delta": {
            str(k): {key: after["at_k"][str(k)][key] - value if value is not None else None
                     for key, value in before["at_k"][str(k)].items()} for k in KS
        },
        "latency_p50_delta_ms": after["latency_ms"]["p50"] - before["latency_ms"]["p50"],
        "latency_p95_delta_ms": after["latency_ms"]["p95"] - before["latency_ms"]["p95"],
        "latency_environment_matches": reference["runtime"] == report["runtime"],
        "unanswerable_false_positive_rate_delta": (
            after["unanswerable_retrieval_false_positive_rate"]
            - before["unanswerable_retrieval_false_positive_rate"]
        ),
        "scope_leak_query_rate_delta": after["scope_leak_query_rate"] - before["scope_leak_query_rate"],
        "latency_caveat": "Descriptive only; hardware, dependencies and system load must also match.",
    }


def run(args):
    dataset = private_path(args.dataset)
    output = private_path(args.output)
    baseline_path = private_path(args.baseline or dataset.with_name("recall_baseline.json"))
    require(not output.exists(), "output exists; frozen artifacts are never overwritten")
    require(args.repeats > 0 and args.warmup >= 0, "invalid repeat/warmup count")
    require(args.backend == "baseline" or args.baseline_api == "search", "hybrid uses search only")
    if args.vector:
        require(all(importlib.util.find_spec(name) for name in
                    ("chromadb", "langchain_huggingface", "sentence_transformers")),
                "--vector requires preinstalled chromadb, langchain_huggingface and sentence_transformers")
    source = args.data_dir.expanduser().resolve()
    require((source / "memory.db").is_file(), "data-dir must contain memory.db")
    require(not output.is_relative_to(source), "output cannot be inside data-dir")
    reference = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    if reference:
        require(reference["backend"] == "baseline" and reference["schema_version"] == SCHEMA_VERSION,
                "invalid frozen baseline")
        require(digest(reference["frozen_source"]) == INITIAL_SOURCE_SHA256 == reference["source_sha256"],
                "baseline source snapshot does not match the pinned pre-refactor code")
    else:
        require(args.backend == "baseline" and output == baseline_path, "create the canonical baseline first")
    if args.backend == "baseline":
        sources = reference["frozen_source"] if reference else initial_sources()
    else:
        sources = current_sources()
    if args.backend == "baseline":
        require(digest(sources) == INITIAL_SOURCE_SHA256, "initial code hash mismatch; supply the original baseline")
    before = data_manifest(source)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="bines-memory-eval-") as temp:
        temp_root = Path(temp).resolve()
        copied_data = temp_root / "data"
        snapshot_data(source, copied_data)
        copy_ms = (time.perf_counter() - started) * 1000
        require(before == data_manifest(source), "source changed during backup; use a quiescent snapshot")
        with readonly_database(copied_data / "memory.db") as conn:
            corpus_hash = database_digest(conn)
            catalog = catalog_from(conn)
            qrows, dataset_hash = load_dataset(dataset, catalog)
        if reference:
            require(dataset_hash == reference["dataset_sha256"], "dataset changed; comparisons require identical labels")
            require(corpus_hash == reference["corpus_sha256"], "corpus changed; restore the same offline snapshot")
        clock = reference["clock"] if reference else time.time()
        rows, settings, initialization_ms = evaluate(
            args, sources, qrows, catalog, copied_data, temp_root / "code", clock,
        )
    after = data_manifest(source)
    require(before == after, "source changed during evaluation; result not published")
    dependencies = {}
    for name in ("chromadb", "langchain-huggingface", "sentence-transformers", "torch"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    report = {
        "schema_version": SCHEMA_VERSION, "backend": args.backend,
        "api": args.baseline_api if args.backend == "baseline" else "search",
        "vector": args.vector, "ks": list(KS), "clock": clock, "settings": settings,
        "created_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": dataset_hash,
        "corpus_sha256": corpus_hash, "source_sha256": digest(sources),
        "baseline_commit": INITIAL_COMMIT,
        "index_sha256": digest({k: v for k, v in before.items() if k.startswith("index/")}),
        "source_data_files_before": before, "source_data_files_after": after,
        "production_files_unchanged": True, "backup_ms": copy_ms, "initialization_ms": initialization_ms,
        "protocol": {
            "warmup": args.warmup, "repeats": args.repeats,
            "ranking": "unique evidence in candidate order, then evidence list order; first k evidence",
            "context_order": ["recalled", "profile", "bridge", "working", "other_channels"],
            "no_answer_false_positive": "any returned candidate; no evaluator relevance threshold",
            "llm_called": False, "reindex_called": False, "network_allowed": False,
        },
        "runtime": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                    "dependencies": dependencies, "embedding_model": os.environ.get("RAG_EMBEDDING_MODEL"),
                    "embedding_device": os.environ.get("BINES_EMBEDDING_DEVICE")},
        "category_counts": dict(Counter(q["category"] for q in qrows)),
        "summary": summarize(rows),
        "by_category": {category: summarize([r for r in rows if r["category"] == category])
                        for category in sorted({r["category"] for r in rows})},
        "queries": rows,
    }
    if args.backend == "baseline":
        report["frozen_source"] = sources
    if reference:
        report["baseline_artifact_sha256"] = file_digest(baseline_path)
        report["comparison"] = comparison(reference, report)
    # Exclusive creation protects the first run from accidental redefinition.
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
    print(json.dumps({"output": str(output), "backend": args.backend, "api": report["api"],
                      "summary": report["summary"]}, ensure_ascii=False, indent=2))


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "memory:a": {"scope": "owner", "source_ref": "legacy:a"},
            "message:1": {"scope": "group", "source_ref": "event:1"},
            "message:2": {"scope": "group", "source_ref": "event:2"},
        }

    def test_legacy_memory_id(self):
        ranked, leaks, unknown = normalize_candidates([{"id": "a"}], "baseline", self.catalog, ["owner"])
        self.assertEqual((ranked, leaks, unknown), (["memory:a"], [], 0))

    def test_hybrid_evidence_deduplication_and_leak(self):
        candidates = [{"memory_id": "a", "evidence": [
            {"source_type": "message", "source_id": 1}, {"source_ref": "event:1"},
            {"source_type": "memory", "source_id": "a"},
        ]}]
        ranked, leaks, unknown = normalize_candidates(candidates, "hybrid", self.catalog, ["group"])
        self.assertEqual(ranked, ["message:1", "memory:a"])
        self.assertEqual(leaks, ["memory:a"])
        self.assertEqual(unknown, 0)

    def test_unknown_evidence_consumes_rank(self):
        rows = [{"evidence": [{"source_type": "message", "source_id": 999}]},
                {"evidence": [{"message_id": 1}]}]
        ranked, _, unknown = normalize_candidates(rows, "hybrid", self.catalog, ["group"])
        self.assertEqual(unknown, 1)
        self.assertEqual(query_metrics(ranked, ["message:1"], 2)["at_k"]["5"]["mrr"], 0.5)

    def test_conflicting_source_ref_not_credited(self):
        rows = [{"evidence": [{"source_type": "message", "source_id": 1, "source_ref": "event:2"}]}]
        ranked, _, unknown = normalize_candidates(rows, "hybrid", self.catalog, ["group"])
        self.assertEqual(unknown, 1)
        self.assertNotIn("message:1", ranked)

    def test_candidate_metadata_cannot_hide_scope_leak(self):
        rows = [{"memory_id": "a", "metadata": {"scope": "owner"},
                 "evidence": [{"message_id": 1}]}]
        _, leaks, _ = normalize_candidates(rows, "hybrid", self.catalog, ["group"])
        self.assertEqual(leaks, ["memory:a", "metadata:0"])

    def test_empty_evidence_uses_explicit_candidate_id(self):
        rows = [{"message_id": 1, "evidence": []}]
        ranked, _, unknown = normalize_candidates(rows, "hybrid", self.catalog, ["group"])
        self.assertEqual((ranked, unknown), (["message:1"], 0))

    def test_multiple_hops_and_ndcg(self):
        result = query_metrics(["x", "a", "b"], ["a", "b"], 3)["at_k"]
        self.assertEqual(result["1"]["all_evidence"], 0)
        self.assertEqual(result["5"]["all_evidence"], 1)
        self.assertEqual(result["5"]["evidence_recall"], 1)
        self.assertEqual(result["5"]["mrr"], 0.5)
        self.assertAlmostEqual(result["5"]["ndcg"], (1 / math.log2(3) + 0.5) / (1 + 1 / math.log2(3)))

    def test_no_answer_not_answer_accuracy(self):
        for count in (0, 1):
            result = query_metrics([], [], count)
            self.assertEqual(result["unanswerable_retrieval_false_positive"], bool(count))
            self.assertIsNone(result["at_k"]["10"]["evidence_recall"])

    def test_empty_results(self):
        self.assertEqual(query_metrics([], ["a"], 0)["at_k"]["10"]["ndcg"], 0)

    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 0.95), 3.85)

    def test_hybrid_contract_required(self):
        with self.assertRaises(ValueError):
            normalize_candidates([{"id": "a"}], "hybrid", self.catalog, ["owner"])

    def test_context_message_type(self):
        ranked, _, _ = normalize_candidates(
            [{"id": 1, "_source_type": "message"}], "baseline", self.catalog, ["group"],
        )
        self.assertEqual(ranked, ["message:1"])

    def test_source_path_traversal(self):
        with self.assertRaises(ValueError):
            materialize_sources({"../bad.py": ""}, Path("/unused"))

    def test_account_scope_envelope(self):
        env = envelope_for({"scopes": ["qq:bot:group:room"]}, 1)
        self.assertEqual((env["external_account_id"], env["external_group_id"]), ("bot", "room"))

    def test_sqlite_backup_includes_wal_without_source_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, dest = root / "source.db", root / "copy.db"
            conn = sqlite3.connect(source)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE fixture(value TEXT)")
                conn.execute("INSERT INTO fixture VALUES ('fixture-only')")
                conn.commit()
                before = [file_digest(source), file_digest(root / "source.db-wal")]
                sqlite_backup(source, dest)
                with readonly_database(dest) as copied:
                    self.assertEqual(copied.execute("SELECT value FROM fixture").fetchone()[0], "fixture-only")
                self.assertEqual(before, [file_digest(source), file_digest(root / "source.db-wal")])
            finally:
                conn.close()

    def test_guard_blocks_external_writes_and_network(self):
        import socket
        with tempfile.TemporaryDirectory() as temp:
            with offline_guard(Path(temp).resolve()):
                with self.assertRaises(EvaluationError):
                    sqlite3.connect(ROOT / "forbidden-test.db")
                with self.assertRaises(PermissionError):
                    socket.getaddrinfo("example.invalid", 443)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--backend", choices=("baseline", "hybrid"), default="baseline")
    parser.add_argument("--vector", action="store_true", help="use the copied index and an already cached model")
    parser.add_argument("--baseline-api", choices=("search", "build_context"), default="search")
    parser.add_argument("--baseline", type=Path, help="frozen reference, default: dataset sibling recall_baseline.json")
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ScoringTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if any(getattr(args, field) is None for field in ("dataset", "data_dir", "output")):
        parser.error("--dataset, --data-dir and --output are required")
    logging.disable(logging.CRITICAL)
    try:
        run(args)
    except Exception as error:
        # Backend errors can contain private query text. Do not echo their contents.
        print(f"Evaluation aborted ({type(error).__name__}); no completed report published.", file=sys.stderr)
        if isinstance(error, EvaluationError):
            print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
