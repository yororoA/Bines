"""
轻量级 RAG 客户端。

原来的重型实现已移动到 `rag_server_core.py`，只在独立的 `rag_server` 进程里导入。
本模块提供同名的 `RAGMemory` 类，但内部通过 ZMQ 调用远端 RAG 服务，
避免在 Thinking 进程里加载 HuggingFace / LangChain / PyTorch 等重量级依赖。
"""

import json
import traceback
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import sys

import zmq

# 确保可以从项目根目录导入 config（无论当前工作目录在哪里）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ZMQ_HOST, ZMQ_PORTS

# 为 RAG 服务预留的 REQ/REP 端口（在 config.py 中配置）
RAG_SERVER_PORT = ZMQ_PORTS.get("RAG_SERVER_REQREP", 5560)


def _call_rag_server(method: str, params: Optional[Dict[str, Any]] = None, timeout_ms: int = 15000) -> Dict[str, Any]:
    """
    向 RAG 服务发送一次 RPC 请求。

    使用一次性 REQ socket，避免多线程下的 socket 复用问题。
    """
    params = params or {}
    ctx = zmq.Context.instance()
    socket = ctx.socket(zmq.REQ)
    socket.linger = 0
    socket.rcvtimeo = timeout_ms
    socket.sndtimeo = timeout_ms
    try:
        socket.connect(f"tcp://{ZMQ_HOST}:{RAG_SERVER_PORT}")
        payload = {"method": method, "params": params}
        socket.send_json(payload)
        reply = socket.recv_json()
        return reply
    except zmq.error.Again:
        print(f"[RAG Client] 调用超时: method={method}")
        print(f"[RAG Client] 提示: 请确认 RAG 服务进程 (rag_server.py) 已启动，且端口 {RAG_SERVER_PORT} 可访问；若服务刚启动，首次请求可能较慢。")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        print(f"[RAG Client] 调用失败: method={method}, error={e}")
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
    finally:
        socket.close()


class RAGMemory:
    """
    轻量级客户端实现，接口尽量保持与原来一致，
    但所有实际计算都在独立的 RAG 服务进程中完成。
    """

    def __init__(self, persist_directory: str = "chroma_db_layered"):
        # 目录只作为标识传给服务端（服务端会在自己的进程里使用它）
        self.persist_directory = persist_directory

    # ========= 核心 API =========

    def get_relevant_context(
        self,
        query: str,
        k: int = 4,
        min_timestamp: Optional[float] = None,
        time_of_day: Optional[str] = None,
        tags: Optional[List[str]] = None,
        day_key: Optional[str] = None,
    ) -> List[str]:
        """三库均支持 day_key（YYYY-MM-DD，04:00 为界）按日期检索。"""
        req = {
            "query": query,
            "k": k,
            "min_timestamp": min_timestamp,
            "time_of_day": time_of_day,
            "tags": tags,
            "day_key": day_key,
            "persist_directory": self.persist_directory,
        }
        # get_relevant_context 可能需要较长时间，使用更长的超时时间
        timeout_ms = 30000 if k <= 10 else 60000  # k 值大时使用更长的超时时间
        resp = _call_rag_server("get_relevant_context", req, timeout_ms=timeout_ms)
        if resp.get("ok"):
            return resp.get("result", []) or []
        return []

    def get_relevant_context_summary_buffer(
        self,
        query: str,
        k: int = 5,
        day_key: Optional[str] = None,
    ) -> List[str]:
        """检索 Buffer RAG；可选 day_key 按日期过滤，三库均支持按日期检索。"""
        req = {
            "query": query,
            "k": k,
            "day_key": day_key,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("get_relevant_context_summary_buffer", req, timeout_ms=30000)
        if resp.get("ok"):
            return resp.get("result", []) or []
        return []

    def get_relevant_context_diary(self, query: str, k: int = 3, day_key: Optional[str] = None) -> List[str]:
        """检索长期日记 RAG；可选 day_key 按日期过滤，三库均支持按日期检索。"""
        req = {
            "query": query,
            "k": k,
            "day_key": day_key,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("get_relevant_context_diary", req, timeout_ms=30000)
        if resp.get("ok"):
            return resp.get("result", []) or []
        return []

    def get_raw_conversations_by_day(self, day_key: str, limit: int = 20) -> List[str]:
        """按 day_key 取当日原始对话（conversation/raw_conversation），用于写日记时传入同日对话。"""
        if not day_key:
            return []
        req = {
            "day_key": str(day_key).strip(),
            "limit": limit,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("get_raw_conversations_by_day", req, timeout_ms=30000)
        if resp.get("ok"):
            return resp.get("result") or []
        return []

    def add_to_summary_buffer(
        self,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
        importance_score: int = 7,
    ) -> bool:
        """写入短期碎片库 Buffer RAG。成功返回 True，失败返回 False。"""
        if not content:
            return False
        req = {
            "content": content,
            "meta": meta or {},
            "importance_score": importance_score,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("add_to_summary_buffer", req, timeout_ms=20000)
        if not resp.get("ok"):
            print(f"[RAG Client] add_to_summary_buffer 失败: {resp.get('error', '未知')}")
            return False
        return True

    def add_to_diary(
        self,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
        importance_score: int = 8,
    ) -> bool:
        """写入长期日记 RAG。成功返回 True，失败返回 False。"""
        if not content:
            return False
        req = {
            "content": content,
            "meta": meta or {},
            "importance_score": importance_score,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("add_to_diary", req, timeout_ms=20000)
        if not resp.get("ok"):
            print(f"[RAG Client] add_to_diary 失败: {resp.get('error', '未知')}")
            return False
        return True

    def get_all_summary_buffer(self) -> List[Dict[str, Any]]:
        """获取 Buffer RAG 全部文档（用于上线按天汇总）。"""
        req = {"persist_directory": self.persist_directory}
        resp = _call_rag_server("get_all_summary_buffer", req, timeout_ms=60000)
        if resp.get("ok"):
            return resp.get("result") or []
        return []

    def get_existing_diary_day_keys(self) -> List[str]:
        """返回日记 RAG 中已存在日记的 day_key 列表（用于上线汇总时跳过已写过的日期）。"""
        req = {"persist_directory": self.persist_directory}
        resp = _call_rag_server("get_existing_diary_day_keys", req, timeout_ms=15000)
        if resp.get("ok"):
            return resp.get("result") or []
        return []

    def delete_summary_buffer_ids(self, ids: List[str]) -> None:
        """删除 Buffer RAG 中指定 id（汇总入日记后调用）。"""
        if not ids:
            return
        req = {"ids": ids, "persist_directory": self.persist_directory}
        _call_rag_server("delete_summary_buffer_ids", req, timeout_ms=30000)

    def add_episode_summary(self, content: str, meta: Optional[Dict[str, Any]] = None, importance_score: int = 7) -> bool:
        """写入剧情摘要到 chat_memory。成功返回 True，失败返回 False。"""
        if not content:
            return False
        req = {
            "content": content,
            "meta": meta or {},
            "importance_score": importance_score,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("add_episode_summary", req, timeout_ms=20000)
        if resp.get("ok"):
            return True
        error_msg = resp.get("error", "未知错误")
        print(f"[RAG Client] 存储剧情摘要失败: {error_msg}")
        if "timeout" in error_msg.lower() or "Connection refused" in error_msg:
            print(f"[RAG Client] 提示: 请确保 RAG 服务器正在运行（端口 {RAG_SERVER_PORT}）")
        return False

    def add_raw_conversation(self, content: str, meta: Optional[Dict[str, Any]] = None, importance_score: int = 5) -> bool:
        """写入原始对话到 chat_memory。成功返回 True，失败返回 False。"""
        if not content:
            return False
        req = {
            "content": content,
            "meta": meta or {},
            "importance_score": importance_score,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("add_raw_conversation", req, timeout_ms=20000)
        if resp.get("ok"):
            return True
        error_msg = resp.get("error", "未知错误")
        print(f"[RAG Client] 存储原始对话失败: {error_msg}")
        if "timeout" in error_msg.lower() or "Connection refused" in error_msg:
            print(f"[RAG Client] 提示: 请确保 RAG 服务器正在运行（端口 {RAG_SERVER_PORT}）")
        return False

    def get_latest_qq_logs(self, group_id=None, user_id=None, limit=10):
        req = {
            "group_id": group_id,
            "user_id": user_id,
            "limit": limit,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("get_latest_qq_logs", req, timeout_ms=5000)
        if resp.get("ok"):
            return resp.get("result", [])
        return []

    def clear(self) -> None:
        req = {"persist_directory": self.persist_directory}
        # clear 操作可能需要较长时间（特别是数据量大时），使用更长的超时时间
        _call_rag_server("clear", req, timeout_ms=60000)  # 60秒

    def garbage_collect(
        self,
        min_age_days: int = 90,
        max_access_count: int = 2,
        dry_run: bool = False,
        batch_size: int = 1000,
    ) -> Dict[str, Any]:
        req = {
            "min_age_days": min_age_days,
            "max_access_count": max_access_count,
            "dry_run": dry_run,
            "batch_size": batch_size,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("garbage_collect", req, timeout_ms=600_000)
        if resp.get("ok"):
            return resp.get("result") or {}
        return {
            "total_docs": 0,
            "deleted_count": 0,
            "kept_count": 0,
            "deleted_by_age": 0,
            "deleted_by_count": 0,
            "error": resp.get("error", "unknown"),
        }

    def get_stats(self, batch_size: int = 1000) -> Dict[str, Any]:
        req = {
            "batch_size": batch_size,
            "persist_directory": self.persist_directory,
        }
        # get_stats 可能需要较长时间（特别是数据量大时），使用更长的超时时间
        resp = _call_rag_server("get_stats", req, timeout_ms=60000)  # 60秒
        if resp.get("ok"):
            return resp.get("result") or {}
        return {
            "total_docs": 0,
            "by_type": {},
            "avg_access_count": 0,
            "oldest_doc_age_days": 0,
            "newest_doc_age_days": 0,
            "error": resp.get("error", "unknown"),
        }

    # ========= 调试 / 管理用的底层 API =========
    # 这些接口主要给 debug_rag.py / rag_web_server.py 使用，
    # 模拟原来对 vector_store._collection 的直接访问。

    def collection_count(self, collection: str = "chat_memory") -> int:
        """返回指定库的记忆条数。collection: chat_memory | summary_buffer | long_term_diary"""
        req = {"persist_directory": self.persist_directory, "collection": collection}
        resp = _call_rag_server("collection_count", req, timeout_ms=60000)
        if resp.get("ok"):
            return int(resp.get("result") or 0)
        return 0

    def collection_get(self, collection: str = "chat_memory", **kwargs) -> Dict[str, Any]:
        """获取指定库的数据。collection: chat_memory | summary_buffer | long_term_diary"""
        params = dict(kwargs)
        params["persist_directory"] = self.persist_directory
        params["collection"] = collection
        timeout_ms = kwargs.get("timeout_ms", 60000)
        resp = _call_rag_server("collection_get", params, timeout_ms=timeout_ms)
        if resp.get("ok"):
            return resp.get("result") or {}
        return {"ids": [], "documents": [], "metadatas": []}

    def collection_delete(self, collection: str = "chat_memory", **kwargs) -> None:
        """删除指定库中的文档。collection: chat_memory | summary_buffer | long_term_diary"""
        params = dict(kwargs)
        params["persist_directory"] = self.persist_directory
        params["collection"] = collection
        timeout_ms = kwargs.get("timeout_ms", 30000)
        _call_rag_server("collection_delete", params, timeout_ms=timeout_ms)

    def similarity_search_with_score(self, query: str, k: int = 10, collection: str = "chat_memory") -> List[Dict[str, Any]]:
        """
        返回结构：
        [
          {
            "content": str,
            "metadata": dict,
            "score": float,
          },
          ...
        ]
        """
        req = {
            "query": query,
            "k": k,
            "persist_directory": self.persist_directory,
            "collection": collection,
        }
        timeout_ms = 30000 if k <= 20 else 60000
        resp = _call_rag_server("similarity_search_with_score", req, timeout_ms=timeout_ms)
        if resp.get("ok"):
            return resp.get("result") or []
        return []

    def search_qq_history(
        self,
        query: str,
        k: int = 5,
        day_key: Optional[str] = None,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit_time_range: Optional[int] = None
    ) -> List[Tuple[str, Dict[str, Any], float]]:
        """
        检索 QQ 历史记录。
        
        Args:
            query: 查询内容
            k: 返回数量
            day_key: 日期过滤
            group_id: 群号过滤
            user_id: 用户号过滤
            limit_time_range: 仅检索最近 N 秒内的消息（可选，用于上下文补充）
        
        Returns:
            List[(content, metadata, score)]
        """
        req = {
            "query": query,
            "k": k,
            "day_key": day_key,
            "group_id": group_id,
            "user_id": user_id,
            "limit_time_range": limit_time_range,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("search_qq_history", req, timeout_ms=10000)
        if resp.get("ok"):
            # 结果格式转换回 Tuple
            raw = resp.get("result") or []
            return [(item[0], item[1], item[2]) for item in raw]
        return []

    def update_memory(
        self,
        query: str,
        new_content: str = "",
        new_meta: Optional[Dict[str, Any]] = None,
        new_importance_score: int = 7,
        memory_type: str = "episode_summary",
        max_results: int = 10,
        preserve_timestamp: bool = True,
        replace_mode: bool = False,
        find_text: str = "",
        replace_text: str = "",
        replace_all: bool = True,
    ) -> Dict[str, Any]:
        """
        更新已入库的记忆细节
        
        通过查询文本找到匹配的记忆，删除旧记忆并添加新记忆。
        这是一个"删除+添加"的组合操作，因为向量数据库不支持直接更新。
        
        Args:
            query: 用于查找要修改的记忆的查询文本（应该与要修改的记忆内容相似）
            new_content: 新的记忆内容
            new_meta: 新的元数据字典（可选）
            new_importance_score: 新的重要性分数（1-10），默认7
            memory_type: 记忆类型，"episode_summary"（剧情摘要）或 "raw_conversation"（原始对话），默认"episode_summary"
            max_results: 最多查找多少个匹配结果，默认10
            preserve_timestamp: 是否保留原时间戳（日期），默认True
            replace_mode: 是否使用替换模式，默认False。如果为True，使用查找替换；如果为False，使用完全替换
            find_text: 要查找的文本（替换模式时必需）
            replace_text: 替换为的文本（替换模式时必需）
            replace_all: 是否替换所有匹配项（替换模式），默认True。如果为False，只替换第一个匹配项
        
        Returns:
            dict: 更新结果，包含：
                - updated: bool, 是否成功更新
                - deleted_count: int, 删除的旧记忆数量
                - added_count: int, 添加的新记忆数量（替换模式）
                - preserved_timestamp: bool, 是否保留了原时间戳
                - message: str, 操作结果描述
        
        示例：
            # 示例1：完全替换模式（保留原日期）
            result = rag_memory.update_memory(
                query="用户和管理员在咖啡厅聊天",
                new_content="[剧情摘要]\\n用户和管理员在咖啡厅进行了深入的对话，讨论了关于AI的话题。",
                memory_type="episode_summary",
                preserve_timestamp=True
            )
            
            # 示例2：查找替换模式（在原文本中查找"a"并替换为"d"）
            # 原记忆："abc" -> 替换后："dbc"
            result = rag_memory.update_memory(
                query="abc",  # 用于查找包含"abc"的记忆
                replace_mode=True,
                find_text="a",
                replace_text="d",
                replace_all=True  # 替换所有匹配项
            )
        """
        req = {
            "query": query,
            "new_content": new_content,
            "new_meta": new_meta or {},
            "new_importance_score": new_importance_score,
            "memory_type": memory_type,
            "max_results": max_results,
            "preserve_timestamp": preserve_timestamp,
            "replace_mode": replace_mode,
            "find_text": find_text,
            "replace_text": replace_text,
            "replace_all": replace_all,
            "persist_directory": self.persist_directory,
        }
        resp = _call_rag_server("update_memory", req, timeout_ms=30000)
        if resp.get("ok"):
            return resp.get("result") or {"updated": False, "message": "未知错误"}
        return {
            "updated": False,
            "deleted_count": 0,
            "preserved_timestamp": False,
            "message": f"更新失败: {resp.get('error', '未知错误')}"
        }