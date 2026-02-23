#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
独立 RAG 服务进程

- 只在本进程内导入 HuggingFace / LangChain / PyTorch 等重量级依赖
- 通过 ZMQ REQ/REP 接收来自 Thinking / 调试工具的请求
- 提供与原 RAGMemory 类等价的核心能力，以及少量底层调试接口
"""

import json
import os
import sys
import time
import traceback
from typing import Any, Dict

import zmq

# 将项目根目录和 src 加入路径以便导入
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CURRENT_DIR)
SRC_DIR = os.path.join(CURRENT_DIR, "src")

for p in (ROOT_DIR, SRC_DIR):
    if p not in sys.path:
        sys.path.append(p)

from config import ZMQ_HOST, ZMQ_PORTS  # type: ignore
from rag_server_core import RAGMemory  # type: ignore


RAG_SERVER_PORT = ZMQ_PORTS.get("RAG_SERVER_REQREP", 5560)


class RAGServer:
    def __init__(self) -> None:
        # 简单实现：单一 RAGMemory 实例，使用默认持久化目录
        # 如后续需要支持多库，可按 persist_directory 做实例缓存。
        self._default_memory = RAGMemory(persist_directory="chroma_db_layered")

    def _get_instance(self, params: Dict[str, Any]) -> RAGMemory:
        # 目前统一使用一个实例，忽略 persist_directory
        return self._default_memory

    def _get_store(self, inst: "RAGMemory", collection: str):
        """根据 collection 返回对应 store，供前端记忆管理（搜索/查看/修改）使用。"""
        if collection == "summary_buffer":
            return inst.summary_buffer_store
        if collection == "long_term_diary":
            return inst.diary_store
        if collection == "qq_history_store":
            return inst.qq_history_store
        return inst.vector_store

    # ------- RPC 方法实现 -------

    def handle_request(self, req: Dict[str, Any]) -> Dict[str, Any]:
        method = req.get("method")
        params = req.get("params") or {}
        try:
            inst = self._get_instance(params)

            if method == "get_relevant_context":
                result = inst.get_relevant_context(
                    query=params.get("query", ""),
                    k=int(params.get("k", 4)),
                    min_timestamp=params.get("min_timestamp"),
                    time_of_day=params.get("time_of_day"),
                    tags=params.get("tags"),
                    day_key=params.get("day_key"),
                )
            elif method == "get_relevant_context_summary_buffer":
                result = inst.get_relevant_context_summary_buffer(
                    query=params.get("query", ""),
                    k=int(params.get("k", 5)),
                    day_key=params.get("day_key"),
                )
            elif method == "get_relevant_context_diary":
                result = inst.get_relevant_context_diary(
                    query=params.get("query", ""),
                    k=int(params.get("k", 3)),
                    day_key=params.get("day_key"),
                )
            elif method == "get_raw_conversations_by_day":
                result = inst.get_raw_conversations_by_day(
                    day_key=params.get("day_key", ""),
                    limit=int(params.get("limit", 20)),
                )
            elif method == "add_to_summary_buffer":
                inst.add_to_summary_buffer(
                    content=params.get("content", ""),
                    meta=params.get("meta") or {},
                    importance_score=int(params.get("importance_score", 7)),
                )
                result = True
            elif method == "add_to_diary":
                inst.add_to_diary(
                    content=params.get("content", ""),
                    meta=params.get("meta") or {},
                    importance_score=int(params.get("importance_score", 8)),
                )
                result = True
            elif method == "get_all_summary_buffer":
                result = inst.get_all_summary_buffer()
            elif method == "get_existing_diary_day_keys":
                result = inst.get_existing_diary_day_keys()
            elif method == "delete_summary_buffer_ids":
                inst.delete_summary_buffer_ids(ids=params.get("ids") or [])
                result = True
            elif method == "add_episode_summary":
                inst.add_episode_summary(
                    content=params.get("content", ""),
                    meta=params.get("meta") or {},
                    importance_score=int(params.get("importance_score", 7)),
                )
                result = True
            elif method == "add_raw_conversation":
                inst.add_raw_conversation(
                    content=params.get("content", ""),
                    meta=params.get("meta") or {},
                    importance_score=int(params.get("importance_score", 5)),
                )
                result = True
            elif method == "add_qq_log":
                inst.add_qq_log(
                    content=params.get("content", ""),
                    meta=params.get("meta") or {},
                )
                result = True
            elif method == "search_qq_history":
                result = inst.search_qq_history(
                    query=params.get("query", ""),
                    k=int(params.get("k", 5)),
                    day_key=params.get("day_key"),
                    group_id=params.get("group_id"),
                    user_id=params.get("user_id"),
                )
            elif method == "get_latest_qq_logs":
                result = inst.get_latest_qq_logs(
                    group_id=params.get("group_id"),
                    user_id=params.get("user_id"),
                    limit=int(params.get("limit", 10)),
                )
            elif method == "clear":
                inst.clear()
                result = True
            elif method == "garbage_collect":
                result = inst.garbage_collect(
                    min_age_days=int(params.get("min_age_days", 90)),
                    max_access_count=int(params.get("max_access_count", 2)),
                    dry_run=bool(params.get("dry_run", False)),
                    batch_size=int(params.get("batch_size", 1000)),
                )
            elif method == "get_stats":
                result = inst.get_stats(batch_size=int(params.get("batch_size", 1000)))
            # ---- 调试 / 底层接口（支持多库：chat_memory / summary_buffer / long_term_diary）----
            elif method == "collection_count":
                collection = params.get("collection", "chat_memory")
                vs = self._get_store(inst, collection)
                coll = getattr(vs, "_collection", None) or getattr(vs, "collection", None)
                if coll is None and hasattr(vs, "_client"):
                    coll_name = getattr(vs, "_collection_name", None) or getattr(vs, "collection_name", "chat_memory")
                    try:
                        coll = vs._client.get_collection(name=coll_name)
                    except Exception:
                        coll = None
                if coll is not None and hasattr(coll, "count"):
                    result = int(coll.count())
                else:
                    # fallback：只拉取 ids，避免 get() 全量文档导致超时
                    try:
                        data = vs.get()
                        result = len(data.get("ids", []))
                    except Exception as e:
                        print(f"[RAG Server] collection_count fallback 失败: {e}")
                        result = 0
            elif method == "collection_get":
                collection = params.get("collection", "chat_memory")
                vs = self._get_store(inst, collection)
                params.pop("persist_directory", None)
                params.pop("timeout_ms", None)
                params.pop("collection", None)
                # Chroma get() 的 include 仅支持 documents, embeddings, metadatas, distances, uris, data，不支持 ids
                # 若客户端请求了 ids，则不传 include，用默认返回值（含 ids）
                if "include" in params and isinstance(params["include"], list):
                    if "ids" in params["include"]:
                        params.pop("include", None)
                    else:
                        params["include"] = [x for x in params["include"] if x != "ids"]
                        if not params["include"]:
                            params.pop("include", None)
                try:
                    result = vs.get(**params)
                except Exception as e:
                    # 如果获取失败，返回错误信息
                    print(f"[RAG Server] collection_get 失败: {e}")
                    raise
            elif method == "collection_delete":
                collection = params.get("collection", "chat_memory")
                vs = self._get_store(inst, collection)
                params.pop("persist_directory", None)
                params.pop("collection", None)
                coll = getattr(vs, "_collection", None) or getattr(vs, "collection", None)
                if coll is not None and hasattr(coll, "delete"):
                    coll.delete(**params)  # type: ignore[attr-defined]
                else:
                    vs.delete(**params)
                result = True
            elif method == "similarity_search_with_score":
                collection = params.get("collection", "chat_memory")
                vs = self._get_store(inst, collection)
                query = params.get("query", "")
                k = int(params.get("k", 10))
                packed = []
                # 使用 Chroma 原生 query 或 RAG 的 embedding_fn 返回 ids，避免前端拿到 temp_0
                try:
                    # 优先用 RAG 实例的 embedding_fn（与 add 时一致），避免 vs.embedding_function 属性名差异
                    emb = inst.embedding_fn.embed_query(query)
                    q = getattr(vs, "_collection", None) or getattr(vs, "collection", None)
                    # langchain_chroma 可能不暴露 _collection，尝试通过 _client.get_collection 获取
                    if q is None and hasattr(vs, "_client"):
                        coll_name = getattr(vs, "_collection_name", None) or getattr(vs, "collection_name", "chat_memory")
                        try:
                            q = vs._client.get_collection(name=coll_name)
                        except Exception:
                            q = None
                    if q is not None and emb is not None:
                        raw_q = q.query(
                            query_embeddings=[emb],
                            n_results=k,
                            include=["documents", "metadatas", "distances"],
                        )
                        ids = (raw_q.get("ids") or [[]])[0]
                        docs = (raw_q.get("documents") or [[]])[0]
                        metas = (raw_q.get("metadatas") or [[]])[0]
                        dists = (raw_q.get("distances") or [[]])[0]
                        for i in range(len(ids)):
                            packed.append({
                                "id": ids[i],
                                "content": docs[i] if i < len(docs) else "",
                                "metadata": (metas[i] if i < len(metas) else None) or {},
                                "score": float(dists[i]) if i < len(dists) else 0.0,
                            })
                except Exception as e:
                    print(f"[RAG Server] similarity_search_with_score 带 id 查询失败: {e}")
                # 若无 id：用 similarity_search_with_score 再按内容在 collection 里查 id 兜底
                if not packed or not any(p.get("id") for p in packed):
                    raw = vs.similarity_search_with_score(query, k=k)
                    packed = []
                    if raw:
                        try:
                            all_data = vs.get(include=["documents", "metadatas"])
                            all_ids = all_data.get("ids") or []
                            all_docs = all_data.get("documents") or []
                            # 兼容 LangChain 返回的 documents 为嵌套列表的情况
                            if all_docs and isinstance(all_docs[0], (list, tuple)):
                                all_docs = list(all_docs[0]) if all_docs else []
                            content_to_id = {}
                            for idx, doc_content in enumerate(all_docs):
                                if idx < len(all_ids) and doc_content is not None:
                                    c = doc_content if isinstance(doc_content, str) else str(doc_content)
                                    content_to_id[c] = all_ids[idx]
                                    content_to_id[c.strip()] = all_ids[idx]
                            for doc, score in raw:
                                content = doc.page_content or ""
                                doc_id = content_to_id.get(content) or content_to_id.get(content.strip())
                                packed.append({
                                    "id": doc_id,
                                    "content": content,
                                    "metadata": doc.metadata or {},
                                    "score": float(score),
                                })
                        except Exception as e2:
                            print(f"[RAG Server] 按内容匹配 id 失败: {e2}")
                            for doc, score in raw:
                                packed.append({
                                    "content": doc.page_content,
                                    "metadata": doc.metadata or {},
                                    "score": float(score),
                                })
                result = packed
            elif method == "update_memory":
                # 更新已入库的记忆：先查找，删除旧记忆，添加新记忆
                query = params.get("query", "")  # 用于查找要修改的记忆的查询文本
                new_content = params.get("new_content", "")  # 新的记忆内容
                new_meta = params.get("new_meta") or {}  # 新的元数据（可选）
                new_importance_score = int(params.get("new_importance_score", 7))  # 新的重要性分数
                memory_type = params.get("memory_type", "episode_summary")  # 记忆类型：episode_summary 或 raw_conversation
                max_results = int(params.get("max_results", 10))  # 最多查找多少个结果
                preserve_timestamp = params.get("preserve_timestamp", True)  # 是否保留原时间戳，默认True
                replace_mode = params.get("replace_mode", False)  # 是否使用替换模式（查找替换），默认False
                find_text = params.get("find_text", "")  # 要查找的文本（替换模式）
                replace_text = params.get("replace_text", "")  # 替换为的文本（替换模式）
                replace_all = params.get("replace_all", True)  # 是否替换所有匹配项（替换模式），默认True
                
                if not query:
                    raise ValueError("query 参数不能为空")
                
                # 根据模式确定 new_content
                if replace_mode:
                    # 替换模式：需要 find_text 和 replace_text
                    if not find_text:
                        raise ValueError("replace_mode=True 时，find_text 参数不能为空")
                    # new_content 在替换模式下会被忽略，将在找到记忆后生成
                else:
                    # 完全替换模式：需要 new_content
                    if not new_content:
                        raise ValueError("replace_mode=False 时，new_content 参数不能为空")
                
                # 1. 通过相似度搜索找到要修改的记忆
                vs = inst.vector_store
                search_results = vs.similarity_search_with_score(query, k=max_results)
                
                if not search_results:
                    result = {"updated": False, "message": "未找到匹配的记忆"}
                else:
                    # 2. 删除找到的所有匹配记忆（可能有多个相似的结果）
                    # 通过内容匹配找到对应的文档ID，并保存原内容和时间戳
                    deleted_ids = []
                    original_timestamps = []  # 保存原时间戳
                    original_contents = {}  # 保存原内容 {doc_id: content}
                    all_docs = vs.get()
                    documents = all_docs.get("documents", [])
                    ids = all_docs.get("ids", [])
                    metadatas = all_docs.get("metadatas", [])
                    
                    # 收集要删除的内容（去重）
                    target_contents = set()
                    for doc, score in search_results:
                        target_contents.add(doc.page_content)
                    
                    # 找到所有匹配内容的ID，并保存原时间戳和内容
                    print(f"[update_memory] 开始匹配文档ID，目标内容数量: {len(target_contents)}")
                    for idx, content in enumerate(documents):
                        if content in target_contents:
                            doc_id = ids[idx]
                            deleted_ids.append(doc_id)
                            original_contents[doc_id] = content  # 保存原内容
                            print(f"[update_memory] 找到匹配记忆 doc_id={doc_id}，内容长度: {len(content)}")
                            # 保存原时间戳（如果存在）
                            if idx < len(metadatas) and metadatas[idx]:
                                original_timestamp = metadatas[idx].get("timestamp")
                                if original_timestamp:
                                    original_timestamps.append(original_timestamp)
                    
                    print(f"[update_memory] 共找到 {len(deleted_ids)} 条匹配记忆，保存了 {len(original_contents)} 条原内容")
                    
                    if deleted_ids:
                        # 去重
                        deleted_ids = list(set(deleted_ids))
                        
                        # 3. 如果是替换模式，对每个原内容进行替换
                        if replace_mode:
                            # ⚠️ 重要：在删除之前，先保存所有原内容到备份文件
                            backup_file = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                "memory_backup",
                                f"backup_{int(time.time())}.json"
                            )
                            os.makedirs(os.path.dirname(backup_file), exist_ok=True)
                            
                            backup_data = {
                                "timestamp": time.time(),
                                "query": query,
                                "find_text": find_text,
                                "replace_text": replace_text,
                                "replace_all": replace_all,
                                "memory_type": memory_type,
                                "deleted_ids": deleted_ids,
                                "original_contents": original_contents,
                                "original_timestamps": original_timestamps
                            }
                            
                            try:
                                with open(backup_file, 'w', encoding='utf-8') as f:
                                    json.dump(backup_data, f, ensure_ascii=False, indent=2)
                                print(f"[update_memory] ✓ 已备份到: {backup_file}")
                            except Exception as e:
                                print(f"[update_memory] ⚠️ 备份失败: {e}")
                            
                            replaced_contents = {}
                            has_find_text = {}  # 记录哪些内容包含 find_text
                            
                            print(f"[update_memory] 替换模式：查找 '{find_text}'，替换为 '{replace_text}'，replace_all={replace_all}")
                            print(f"[update_memory] 找到 {len(deleted_ids)} 条记忆需要处理")
                            
                            for doc_id in deleted_ids:
                                original_content = original_contents.get(doc_id, "")
                                if not original_content:
                                    print(f"[update_memory] 警告：doc_id={doc_id} 的内容为空")
                                    replaced_contents[doc_id] = ""
                                    has_find_text[doc_id] = False
                                    continue
                                
                                if find_text in original_content:
                                    # 原内容包含 find_text，执行替换
                                    if replace_all:
                                        # 替换所有匹配项
                                        replaced_content = original_content.replace(find_text, replace_text)
                                    else:
                                        # 只替换第一个匹配项
                                        replaced_content = original_content.replace(find_text, replace_text, 1)
                                    replaced_contents[doc_id] = replaced_content
                                    has_find_text[doc_id] = True
                                    print(f"[update_memory] doc_id={doc_id}: 包含 find_text，已替换（长度: {len(original_content)} -> {len(replaced_content)}）")
                                else:
                                    # 如果原内容中不包含 find_text，保留原内容（但也会添加回去）
                                    replaced_contents[doc_id] = original_content
                                    has_find_text[doc_id] = False
                                    print(f"[update_memory] doc_id={doc_id}: 不包含 find_text，保留原内容（长度: {len(original_content)}）")
                            
                            # ⚠️ 重要：只有在准备好所有替换内容后才删除
                            # 删除旧记忆
                            print(f"[update_memory] 正在删除 {len(deleted_ids)} 条旧记忆...")
                            vs.delete(ids=deleted_ids)
                            print(f"[update_memory] 删除完成")
                            
                            # 添加替换后的记忆（每个原记忆对应一个新记忆）
                            # 注意：所有被删除的记忆都应该被添加回去（替换后的版本或原版本）
                            import time
                            added_count = 0
                            skipped_count = 0  # 跳过的数量（理论上不应该有）
                            
                            # 为每个记忆单独保存时间戳（如果可能的话）
                            # 由于我们只能获取到所有时间戳的列表，这里使用最早的时间戳作为统一值
                            preserved_timestamp = None
                            if preserve_timestamp and original_timestamps:
                                preserved_timestamp = min(original_timestamps)
                            
                            for doc_id in deleted_ids:
                                # 先从 replaced_contents 获取，如果为空则从 original_contents 获取
                                replaced_content = replaced_contents.get(doc_id)
                                if replaced_content is None:
                                    # 如果 replaced_contents 中没有，尝试从 original_contents 获取
                                    replaced_content = original_contents.get(doc_id, "")
                                    print(f"[update_memory] doc_id={doc_id}: replaced_contents 中没有，从 original_contents 获取（长度: {len(replaced_content)}）")
                                
                                contains_find = has_find_text.get(doc_id, False)
                                
                                # 确保内容不为空
                                if not replaced_content or len(replaced_content.strip()) == 0:
                                    # 如果内容为空，尝试从 original_contents 获取
                                    original_content = original_contents.get(doc_id, "")
                                    if original_content:
                                        replaced_content = original_content
                                        print(f"[update_memory] doc_id={doc_id}: 内容为空，使用原内容（长度: {len(replaced_content)}）")
                                    else:
                                        print(f"[update_memory] ✗ doc_id={doc_id}: 无法获取内容，跳过")
                                        skipped_count += 1
                                        continue
                                
                                # 只要内容不为空，就添加（不管是否包含 find_text）
                                # 因为用户明确要求替换操作，即使原内容不包含 find_text，也应该保留原记忆
                                if replaced_content and len(replaced_content.strip()) > 0:  # 内容不为空就添加
                                    # 确定要使用的时间戳
                                    if preserve_timestamp and preserved_timestamp:
                                        new_meta["timestamp"] = preserved_timestamp
                                    else:
                                        new_meta["timestamp"] = time.time()
                                    
                                    # 添加替换后的记忆
                                    try:
                                        if memory_type == "episode_summary":
                                            inst.add_episode_summary(
                                                content=replaced_content,
                                                meta=new_meta.copy(),
                                                importance_score=new_importance_score
                                            )
                                        else:
                                            inst.add_raw_conversation(
                                                content=replaced_content,
                                                meta=new_meta.copy(),
                                                importance_score=new_importance_score
                                            )
                                        added_count += 1
                                        print(f"[update_memory] ✓ 成功添加记忆 doc_id={doc_id}（长度: {len(replaced_content)}，包含find_text: {contains_find}）")
                                    except Exception as e:
                                        print(f"[update_memory] ✗ 添加记忆失败 (doc_id={doc_id}): {e}")
                                        traceback.print_exc()
                                        skipped_count += 1
                                else:
                                    skipped_count += 1
                                    print(f"[update_memory] ✗ 跳过空内容记忆 (doc_id={doc_id}, 内容长度: {len(replaced_content) if replaced_content else 0})")
                            
                            print(f"[update_memory] 添加完成：成功 {added_count} 条，跳过 {skipped_count} 条")
                            
                            # ⚠️ 如果添加失败，提示用户可以使用备份恢复
                            if added_count == 0 and len(deleted_ids) > 0:
                                print(f"[update_memory] ⚠️ 严重警告：删除了 {len(deleted_ids)} 条记忆，但未能添加任何新记忆！")
                                print(f"[update_memory] ⚠️ 备份文件位置: {backup_file}")
                                print(f"[update_memory] ⚠️ 请使用恢复脚本恢复记忆: python recover_deleted_memories.py")
                            
                            # 构建返回消息
                            timestamp_info = ""
                            if preserve_timestamp and original_timestamps:
                                from datetime import datetime
                                preserved_date = datetime.fromtimestamp(min(original_timestamps)).strftime('%Y-%m-%d %H:%M:%S')
                                timestamp_info = f"，保留了原日期：{preserved_date}"
                            else:
                                timestamp_info = "，使用了当前日期"
                            
                            # 构建详细消息
                            if added_count == 0:
                                message = f"⚠️ 警告：删除了 {len(deleted_ids)} 条旧记忆，但未能添加任何新记忆。可能原因：1) 所有记忆内容为空；2) 添加操作失败。请检查日志。{timestamp_info}"
                            elif skipped_count > 0:
                                message = f"成功替换记忆：删除了 {len(deleted_ids)} 条旧记忆，添加了 {added_count} 条替换后的记忆，跳过了 {skipped_count} 条（内容为空或添加失败）{timestamp_info}"
                            else:
                                message = f"成功替换记忆：删除了 {len(deleted_ids)} 条旧记忆，添加了 {added_count} 条替换后的记忆{timestamp_info}"
                            
                            result = {
                                "updated": added_count > 0,  # 只有成功添加了记忆才算更新成功
                                "deleted_count": len(deleted_ids),
                                "added_count": added_count,
                                "skipped_count": skipped_count,
                                "preserved_timestamp": preserve_timestamp and original_timestamps,
                                "message": message
                            }
                        else:
                            # 完全替换模式（原有逻辑）
                            vs.delete(ids=deleted_ids)
                            
                            # 确定要使用的时间戳
                            import time
                            if preserve_timestamp and original_timestamps:
                                preserved_timestamp = min(original_timestamps)
                                new_meta["timestamp"] = preserved_timestamp
                            else:
                                new_meta["timestamp"] = time.time()
                            
                            # 添加新的记忆
                            if memory_type == "episode_summary":
                                inst.add_episode_summary(
                                    content=new_content,
                                    meta=new_meta,
                                    importance_score=new_importance_score
                                )
                            else:
                                inst.add_raw_conversation(
                                    content=new_content,
                                    meta=new_meta,
                                    importance_score=new_importance_score
                                )
                            
                            # 构建返回消息
                            timestamp_info = ""
                            if preserve_timestamp and original_timestamps:
                                from datetime import datetime
                                preserved_date = datetime.fromtimestamp(preserved_timestamp).strftime('%Y-%m-%d %H:%M:%S')
                                timestamp_info = f"，保留了原日期：{preserved_date}"
                            else:
                                timestamp_info = "，使用了当前日期"
                            
                            result = {
                                "updated": True,
                                "deleted_count": len(deleted_ids),
                                "preserved_timestamp": preserve_timestamp and original_timestamps,
                                "message": f"成功更新记忆：删除了 {len(deleted_ids)} 条旧记忆，添加了1条新记忆{timestamp_info}"
                            }
            else:
                raise ValueError(f"Unknown method: {method}")

            return {"ok": True, "result": result}
        except Exception as e:
            print(f"[RAG Server] 处理请求出错 method={method}, error={e}")
            traceback.print_exc()
            return {"ok": False, "error": str(e)}


def main() -> None:
    print("=" * 60)
    print("RAG 向量记忆服务 (rag_server)")
    print("=" * 60)
    print(f"ZMQ REQ/REP 地址: tcp://{ZMQ_HOST}:{RAG_SERVER_PORT}")
    print("提示：请在启动 Thinking 模块前先启动本进程。")
    print("=" * 60)

    server = RAGServer()

    ctx = zmq.Context.instance()
    socket = ctx.socket(zmq.REP)
    # 只 bind 到本机
    socket.bind(f"tcp://*:{RAG_SERVER_PORT}")

    try:
        from common.module_ready import notify_module_ready
        notify_module_ready("RAG Server")
    except Exception as e:
        print(f"[RAG Server] 就绪上报失败: {e}", flush=True)

    while True:
        try:
            msg = socket.recv()
            try:
                req = json.loads(msg.decode("utf-8"))
            except Exception:
                socket.send_json({"ok": False, "error": "invalid_json"})
                continue

            resp = server.handle_request(req)
            socket.send_json(resp)
        except KeyboardInterrupt:
            print("\n[RAG Server] 收到中断信号，准备退出...")
            break
        except zmq.ZMQError as e:
            # Socket/context 状态异常（如进程退出时 recv 失败），不再重试，直接退出
            print(f"[RAG Server] ZMQ 错误，退出主循环: {e}")
            break
        except Exception as e:
            print(f"[RAG Server] 主循环异常: {e}")
            traceback.print_exc()

    try:
        socket.setsockopt(zmq.LINGER, 0)
    except Exception:
        pass
    try:
        socket.close()
    except Exception:
        pass
    try:
        ctx.term()
    except Exception:
        pass


if __name__ == "__main__":
    main()

