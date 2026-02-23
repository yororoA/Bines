"""
RAG 记忆图形化管理工具 - Web 服务器
基于 debug_rag.py 的功能，提供 Web API 接口
"""
import sys
import os
import json
import re
import time
import datetime
from flask import Flask, request, jsonify, send_from_directory, Response

# 与 rag_server_core 一致：一天以 04:00 为界
DAY_START_HOUR = 4

def _day_to_time_range(day_str):
    """给定 YYYY-MM-DD，返回该日（04:00 为界）的时间戳范围 (start_ts, end_ts)。"""
    if not day_str or not str(day_str).strip():
        return None, None
    try:
        s = str(day_str).strip().replace("/", "-")[:10]
        d = datetime.datetime.strptime(s, "%Y-%m-%d")
        day_start = d.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        day_end = day_start + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
        return time.mktime(day_start.timetuple()) + day_start.microsecond / 1e6, time.mktime(day_end.timetuple()) + day_end.microsecond / 1e6
    except Exception:
        return None, None

def _to_seconds_ts(v):
    """将 start_time/end_time 转为秒级时间戳。支持 float/int 或毫秒（>1e12 时除以 1000）。"""
    if v is None:
        return None
    try:
        t = float(v)
        if t > 1e12:
            t = t / 1000.0
        return t
    except (TypeError, ValueError):
        return None

def _parse_date_from_summary_content(content):
    """从摘要正文解析日期（如 [2026/01/30 14:00 - 15:00] 或 **[2026-01-30]**），返回 YYYY-MM-DD 或 None。"""
    if not content or not isinstance(content, str):
        return None
    # 匹配 [YYYY/MM/DD 或 [YYYY-MM-DD 或 **[YYYY/MM/DD 等
    m = re.search(r'\[(\d{4})[/-](\d{1,2})[/-](\d{1,2})', content)
    if not m:
        return None
    try:
        y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{y}-{mo}-{d}"
    except (IndexError, ValueError):
        return None

def _summary_doc_matches_day_range(meta, target_start_ts, target_end_ts):
    """摘要文档的 [start_time, end_time] 与目标时间范围有交集则返回 True；无时间范围时返回 None（由调用方用 day_key 判断）。"""
    meta = (meta or {}) if isinstance(meta, dict) else {}
    doc_start = meta.get("start_time")
    doc_end = meta.get("end_time")
    if doc_start is None and doc_end is None:
        return None  # 表示需退化为 day_key 判断
    ds = _to_seconds_ts(doc_start) if doc_start is not None else _to_seconds_ts(doc_end)
    de = _to_seconds_ts(doc_end) if doc_end is not None else _to_seconds_ts(doc_start)
    if ds is None or de is None:
        return None
    return max(ds, target_start_ts) <= min(de, target_end_ts)
from flask_cors import CORS

# 将 src 加入路径以便导入
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from rag_memory import RAGMemory

app = Flask(__name__, static_folder='rag_web_static', static_url_path='')
# 返回 JSON 时保留中文等 Unicode 原文，不转成 \uXXXX
app.config['JSON_AS_ASCII'] = False
CORS(app)  # 允许跨域请求

# 初始化 RAG Memory
rag = None

def init_rag():
    """初始化 RAG Memory 客户端实例（连接独立 rag_server）"""
    global rag
    if rag is None:
        print("正在初始化 RAG Memory 客户端...")
        rag = RAGMemory(persist_directory="chroma_db_layered")
        print("RAG Memory 客户端初始化完成")
    return rag

@app.route('/')
def index():
    """返回前端页面"""
    return send_from_directory('rag_web_static', 'index.html')

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """获取记忆库统计信息。支持 ?collection=chat_memory|summary_buffer|long_term_diary，默认当前库。"""
    try:
        collection = request.args.get('collection', 'chat_memory')
        rag_instance = init_rag()
        count = rag_instance.collection_count(collection=collection)
        return jsonify({
            'success': True,
            'total_count': count,
            'collection': collection
        })
    except Exception as e:
        error_msg = str(e)
        # 如果是超时错误，提供更友好的提示
        if "timeout" in error_msg.lower() or "超时" in error_msg:
            return jsonify({
                'success': False,
                'error': '获取统计信息超时。请检查 RAG 服务器是否正在运行，或稍后重试。'
            }), 500
        return jsonify({
            'success': False,
            'error': error_msg
        }), 500

@app.route('/api/search', methods=['POST'])
def search_memories():
    """搜索相关记忆"""
    try:
        data = request.json
        query = data.get('query', '').strip()
        k = data.get('k', 10)
        collection = data.get('collection', 'chat_memory')
        
        if not query:
            return jsonify({
                'success': False,
                'error': '查询内容不能为空'
            }), 400
        
        rag_instance = init_rag()
        
        try:
            raw_results = rag_instance.similarity_search_with_score(query, k=k, collection=collection)
        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower() or "超时" in error_msg:
                return jsonify({
                    'success': False,
                    'error': '搜索超时。请检查 RAG 服务器是否正在运行，或减少返回数量（k 值）。'
                }), 500
            raise
        
        # 2. 计算加权分数（使用服务端返回的文档 id，避免前端 temp_0 导致「未找到ID」）
        import math
        scored_results = []
        for item in raw_results:
            distance = item.get("score", 0.0)
            meta = item.get("metadata") or {}
            content = item.get("content", "")
            count = meta.get("access_count", 1)
            relevance = 1.0 / (1.0 + distance)
            strength_multiplier = 1.0 + math.log(count)
            final_score = relevance * strength_multiplier
            doc_id = item.get("id") or meta.get("id")
            scored_results.append({
                'id': doc_id,
                'content': content,
                'metadata': meta,
                'distance': float(distance),
                'relevance': float(relevance),
                'final_score': float(final_score),
                'access_count': count
            })
        
        # 3. 按最终分数排序
        scored_results.sort(key=lambda x: x['final_score'], reverse=True)
        
        # 3.5 兜底：若仍有结果缺少 id（避免前端显示 temp），用 collection 按内容解析 id
        missing_id_indices = [i for i, r in enumerate(scored_results) if not r.get('id')]
        if missing_id_indices:
            try:
                all_data = rag_instance.collection_get(collection=collection, include=["ids", "documents"], timeout_ms=30000)
                all_ids = all_data.get("ids") or []
                all_docs = all_data.get("documents") or []
                if all_docs and isinstance(all_docs[0], (list, tuple)):
                    all_docs = list(all_docs[0]) if all_docs else []
                content_to_id = {}
                for idx, doc_content in enumerate(all_docs):
                    if idx < len(all_ids) and doc_content:
                        c = doc_content if isinstance(doc_content, str) else str(doc_content)
                        content_to_id[c] = all_ids[idx]
                        content_to_id[c.strip()] = all_ids[idx]
                for i in missing_id_indices:
                    content = (scored_results[i].get("content") or "").strip()
                    resolved_id = content_to_id.get(scored_results[i].get("content")) or content_to_id.get(content)
                    if resolved_id:
                        scored_results[i]["id"] = resolved_id
            except Exception as e:
                print(f"[search_memories] 按内容解析 id 兜底失败: {e}")
        
        # 4. 获取实际注入 Prompt 的内容（仅原始对话库支持 get_relevant_context）
        try:
            final_ctx = rag_instance.get_relevant_context(query, k=4) if collection == 'chat_memory' else []
        except Exception as e:
            # 如果获取上下文失败，仍然返回搜索结果
            print(f"[search_memories] 获取上下文失败: {e}")
            final_ctx = []
        
        return jsonify({
            'success': True,
            'raw_results': scored_results,
            'final_context': final_ctx,
            'query': query,
            'collection': collection
        })
    except Exception as e:
        error_msg = str(e)
        # 如果是超时错误，提供更友好的提示
        if "timeout" in error_msg.lower() or "超时" in error_msg:
            return jsonify({
                'success': False,
                'error': '搜索超时。请检查 RAG 服务器是否正在运行，或减少返回数量（k 值）。'
            }), 500
        return jsonify({
            'success': False,
            'error': error_msg
        }), 500

@app.route('/api/search_by_keyword', methods=['POST'])
def search_by_keyword():
    """按关键词搜索记忆（精确匹配）"""
    try:
        data = request.json
        keyword = data.get('keyword', '').strip()
        collection = data.get('collection', 'chat_memory')
        
        if not keyword:
            return jsonify({
                'success': False,
                'error': '关键词不能为空'
            }), 400
        
        rag_instance = init_rag()
        results = rag_instance.collection_get(collection=collection, where_document={"$contains": keyword})
        
        memories = []
        docs = results.get('documents', [])
        ids = results.get('ids', [])
        metadatas = results.get('metadatas', [])
        for i, doc_content in enumerate(docs):
            doc_id = ids[i] if i < len(ids) else None
            metadata = metadatas[i] if i < len(metadatas) else {}
            
            memories.append({
                'id': doc_id,
                'content': doc_content,
                'metadata': metadata
            })
        
        return jsonify({
            'success': True,
            'memories': memories,
            'count': len(memories),
            'keyword': keyword,
            'collection': collection
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/search_by_date', methods=['POST'])
def search_by_date():
    """按日期搜索记忆。原始对话库从内容解析日期；摘要/日记库按 metadata.day_key 过滤。"""
    try:
        data = request.json
        date_param = data.get('date', '').strip()
        collection = data.get('collection', 'chat_memory')
        
        if not date_param:
            return jsonify({
                'success': False,
                'error': '日期参数不能为空'
            }), 400
        
        rag_instance = init_rag()
        # 摘要/日记库按日期过滤依赖 metadatas（day_key、start_time、end_time），显式请求确保返回
        get_kw = {}
        if collection in ("summary_buffer", "long_term_diary", "qq_history_store"):
            get_kw["include"] = ["ids", "documents", "metadatas"]
        all_data = rag_instance.collection_get(collection=collection, **get_kw)
        ids = all_data.get('ids', [])
        docs = all_data.get('documents', [])
        metadatas = all_data.get('metadatas', [])
        # Chroma 有时返回嵌套列表 [[id1, id2, ...]]，统一为平铺 [id1, id2, ...]
        if ids and isinstance(ids[0], list):
            ids = ids[0]
        if docs and isinstance(docs[0], list):
            docs = docs[0]
        if metadatas and isinstance(metadatas[0], list):
            metadatas = metadatas[0]
        
        if not ids:
            return jsonify({
                'success': True,
                'memories': [],
                'count': 0,
                'date': date_param,
                'collection': collection
            })
        
        memories = []
        # 仅当格式为 "日期-日期"（恰好两段）时视为范围，避免 "2026-01-30" 被误判为范围
        parts = [p.strip() for p in date_param.split("-")]
        is_range = (len(parts) == 2)
        start_dt = None
        end_dt = None
        if is_range:
            try:
                start_dt = datetime.datetime.strptime(parts[0].replace("/", "-")[:10], "%Y-%m-%d")
                end_dt = datetime.datetime.strptime(parts[1].replace("/", "-")[:10], "%Y-%m-%d")
            except ValueError:
                is_range = False
        
        # 摘要/日记库/QQ库：按 metadata.day_key（YYYY-MM-DD）过滤；摘要库对有 start_time/end_time 的条目按时间范围交集匹配
        if collection in ("summary_buffer", "long_term_diary", "qq_history_store"):
            def norm_day(s):
                if not s:
                    return None
                s = s.strip().replace("/", "-")
                try:
                    return datetime.datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    return None
            target_day = norm_day(date_param)
            for i in range(len(ids)):
                raw_meta = metadatas[i] if i < len(metadatas) else None
                meta = (raw_meta or {}) if isinstance(raw_meta, dict) else {}
                day_key = (meta.get("day_key") or "").strip()
                include = False
                if collection == "summary_buffer":
                    # 摘要库：有 start_time/end_time 时按时间范围交集判断，避免跨天摘要漏检
                    if is_range:
                        curr = start_dt
                        while curr <= end_dt:
                            ts_start, ts_end = _day_to_time_range(curr.strftime("%Y-%m-%d"))
                            if ts_start is not None and ts_end is not None:
                                match = _summary_doc_matches_day_range(meta, ts_start, ts_end)
                                if match is True:
                                    include = True
                                    break
                            curr += datetime.timedelta(days=1)
                        if not include and day_key:
                            try:
                                doc_dt = datetime.datetime.strptime(day_key[:10], "%Y-%m-%d")
                                include = start_dt <= doc_dt <= end_dt
                            except ValueError:
                                pass
                        if not include:
                            doc_date = _parse_date_from_summary_content(docs[i] if i < len(docs) else "")
                            if doc_date:
                                try:
                                    doc_dt = datetime.datetime.strptime(doc_date[:10], "%Y-%m-%d")
                                    include = start_dt <= doc_dt <= end_dt
                                except ValueError:
                                    pass
                    else:
                        ts_start, ts_end = _day_to_time_range(target_day or "")
                        if ts_start is not None and ts_end is not None:
                            match = _summary_doc_matches_day_range(meta, ts_start, ts_end)
                            if match is True:
                                include = True
                            elif match is None:
                                include = (day_key[:10] == (target_day or "")) if day_key else False
                        else:
                            include = (day_key[:10] == (target_day or "")) if day_key else False
                        # 无 metadata 日期时从正文解析（如 [2026/01/30 14:00 - 15:00]）
                        if not include and (target_day or ""):
                            doc_date = _parse_date_from_summary_content(docs[i] if i < len(docs) else "")
                            if doc_date and doc_date[:10] == (target_day or ""):
                                include = True
                else:
                    # 日记库/QQ库：仅按 day_key 匹配
                    if not day_key:
                        continue
                    if is_range:
                        try:
                            doc_dt = datetime.datetime.strptime(day_key[:10], "%Y-%m-%d")
                            include = start_dt <= doc_dt <= end_dt
                        except ValueError:
                            pass
                    else:
                        include = day_key[:10] == (target_day or "")
                if include:
                    memories.append({
                        'id': ids[i],
                        'content': docs[i] if i < len(docs) else '',
                        'metadata': meta,
                        'date': day_key or target_day or date_param
                    })
        else:
            # 原始对话库：从内容解析 "User: [2026/01/15]:"
            for i, doc_content in enumerate(docs):
                match = re.search(r"User: \[(\d{4}/\d{2}/\d{2})(?:\s+\d{2}:\d{2})?\]:", doc_content or "")
                if match:
                    doc_date_str = match.group(1)
                    should_include = False
                    if is_range:
                        try:
                            doc_dt = datetime.datetime.strptime(doc_date_str, "%Y/%m/%d")
                            if start_dt <= doc_dt <= end_dt:
                                should_include = True
                        except ValueError:
                            pass
                    else:
                        if doc_date_str == date_param:
                            should_include = True
                    if should_include:
                        memories.append({
                            'id': ids[i] if i < len(ids) else None,
                            'content': doc_content,
                            'metadata': metadatas[i] if i < len(metadatas) else {},
                            'date': doc_date_str
                        })
        
        return jsonify({
            'success': True,
            'memories': memories,
            'count': len(memories),
            'date': date_param,
            'collection': collection
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/delete', methods=['POST'])
def delete_memories():
    """删除记忆"""
    try:
        data = request.json
        ids = data.get('ids', [])
        collection = data.get('collection', 'chat_memory')
        
        if not ids:
            return jsonify({
                'success': False,
                'error': '未指定要删除的记忆 ID'
            }), 400
        
        rag_instance = init_rag()
        rag_instance.collection_delete(collection=collection, ids=ids)
        
        return jsonify({
            'success': True,
            'deleted_count': len(ids)
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/delete_by_keyword', methods=['POST'])
def delete_by_keyword():
    """按关键词删除记忆"""
    try:
        data = request.json
        keyword = data.get('keyword', '').strip()
        
        if not keyword:
            return jsonify({
                'success': False,
                'error': '关键词不能为空'
            }), 400
        
        rag_instance = init_rag()
        collection = data.get('collection', 'chat_memory')
        results = rag_instance.collection_get(collection=collection, where_document={"$contains": keyword})
        ids_to_delete = results.get('ids', [])
        
        if not ids_to_delete:
            return jsonify({
                'success': True,
                'deleted_count': 0,
                'message': f'未找到包含 "{keyword}" 的记忆'
            })
        rag_instance.collection_delete(collection=collection, ids=ids_to_delete)
        
        return jsonify({
            'success': True,
            'deleted_count': len(ids_to_delete),
            'deleted_ids': ids_to_delete
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/delete_by_date', methods=['POST'])
def delete_by_date():
    """按日期删除记忆"""
    try:
        data = request.json
        date_param = data.get('date', '').strip()
        collection = data.get('collection', 'chat_memory')
        
        if not date_param:
            return jsonify({
                'success': False,
                'error': '日期参数不能为空'
            }), 400
        
        rag_instance = init_rag()
        get_kw = {}
        if collection in ("summary_buffer", "long_term_diary", "qq_history_store"):
            get_kw["include"] = ["ids", "documents", "metadatas"]
        all_data = rag_instance.collection_get(collection=collection, **get_kw)
        ids = all_data.get('ids', [])
        docs = all_data.get('documents', [])
        metadatas = all_data.get('metadatas', [])
        if ids and isinstance(ids[0], list):
            ids = ids[0]
        if docs and isinstance(docs[0], list):
            docs = docs[0]
        if metadatas and isinstance(metadatas[0], list):
            metadatas = metadatas[0]
        
        if not ids:
            return jsonify({
                'success': True,
                'deleted_count': 0,
                'message': '数据库为空'
            })
        
        ids_to_delete = []
        memories_to_delete = []
        
        parts = [p.strip() for p in date_param.split("-")]
        is_range = (len(parts) == 2)
        start_dt = None
        end_dt = None
        if is_range:
            try:
                start_dt = datetime.datetime.strptime(parts[0].replace("/", "-")[:10], "%Y-%m-%d")
                end_dt = datetime.datetime.strptime(parts[1].replace("/", "-")[:10], "%Y-%m-%d")
            except ValueError:
                is_range = False
        
        # 摘要/日记库/QQ库：按 metadata.day_key（YYYY-MM-DD）过滤；摘要库对有 start_time/end_time 的条目按时间范围交集匹配
        if collection in ("summary_buffer", "long_term_diary", "qq_history_store"):
            def norm_day(s):
                if not s:
                    return None
                s = s.strip().replace("/", "-")
                try:
                    return datetime.datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
                except ValueError:
                    return None
            target_day = norm_day(date_param)
            for i in range(len(ids)):
                raw_meta = metadatas[i] if i < len(metadatas) else None
                meta = (raw_meta or {}) if isinstance(raw_meta, dict) else {}
                day_key = (meta.get("day_key") or "").strip()
                include = False
                if collection == "summary_buffer":
                    if is_range:
                        curr = start_dt
                        while curr <= end_dt:
                            ts_start, ts_end = _day_to_time_range(curr.strftime("%Y-%m-%d"))
                            if ts_start is not None and ts_end is not None:
                                match = _summary_doc_matches_day_range(meta, ts_start, ts_end)
                                if match is True:
                                    include = True
                                    break
                            curr += datetime.timedelta(days=1)
                        if not include and day_key:
                            try:
                                doc_dt = datetime.datetime.strptime(day_key[:10], "%Y-%m-%d")
                                include = start_dt <= doc_dt <= end_dt
                            except ValueError:
                                pass
                        if not include:
                            doc_date = _parse_date_from_summary_content(docs[i] if i < len(docs) else "")
                            if doc_date:
                                try:
                                    doc_dt = datetime.datetime.strptime(doc_date[:10], "%Y-%m-%d")
                                    include = start_dt <= doc_dt <= end_dt
                                except ValueError:
                                    pass
                    else:
                        ts_start, ts_end = _day_to_time_range(target_day or "")
                        if ts_start is not None and ts_end is not None:
                            match = _summary_doc_matches_day_range(meta, ts_start, ts_end)
                            if match is True:
                                include = True
                            elif match is None:
                                include = (day_key[:10] == (target_day or "")) if day_key else False
                        else:
                            include = (day_key[:10] == (target_day or "")) if day_key else False
                        if not include and (target_day or ""):
                            doc_date = _parse_date_from_summary_content(docs[i] if i < len(docs) else "")
                            if doc_date and doc_date[:10] == (target_day or ""):
                                include = True
                else:
                    if not day_key:
                        continue
                    if is_range:
                        try:
                            doc_dt = datetime.datetime.strptime(day_key[:10], "%Y-%m-%d")
                            include = start_dt <= doc_dt <= end_dt
                        except ValueError:
                            pass
                    else:
                        include = day_key[:10] == (target_day or "")
                if include:
                    ids_to_delete.append(ids[i])
                    memories_to_delete.append({
                        'id': ids[i],
                        'content': docs[i] if i < len(docs) else '',
                        'date': day_key or target_day or date_param
                    })
        else:
            for i, doc_content in enumerate(docs):
                match = re.search(r"User: \[(\d{4}/\d{2}/\d{2})(?:\s+\d{2}:\d{2})?\]:", doc_content or "")
                if match:
                    doc_date_str = match.group(1)
                    should_delete = False
                    if is_range:
                        try:
                            doc_dt = datetime.datetime.strptime(doc_date_str, "%Y/%m/%d")
                            if start_dt <= doc_dt <= end_dt:
                                should_delete = True
                        except ValueError:
                            pass
                    else:
                        if doc_date_str == date_param:
                            should_delete = True
                    if should_delete:
                        ids_to_delete.append(ids[i])
                        memories_to_delete.append({
                            'id': ids[i],
                            'content': doc_content,
                            'date': doc_date_str
                        })
        
        return jsonify({
            'success': True,
            'found_count': len(ids_to_delete),
            'memories': memories_to_delete,
            'ids': ids_to_delete
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/get_all', methods=['GET'])
def get_all_memories():
    """获取所有记忆（分页）。支持 ?collection=chat_memory|summary_buffer|long_term_diary"""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        collection = request.args.get('collection', 'chat_memory')
        
        rag_instance = init_rag()
        try:
            all_data = rag_instance.collection_get(collection=collection, timeout_ms=60000)
        except Exception as e:
            print(f"[get_all_memories] 完整获取超时，尝试优化方式: {e}")
            all_data = rag_instance.collection_get(collection=collection, include=["ids", "metadatas"], timeout_ms=30000)
            if not all_data.get('ids'):
                raise
        
        ids = all_data.get('ids', [])
        docs = all_data.get('documents', [])
        metadatas = all_data.get('metadatas', [])
        
        total = len(ids)
        
        # 分页
        start = (page - 1) * per_page
        end = start + per_page
        
        memories = []
        for i in range(start, min(end, total)):
            memories.append({
                'id': ids[i],
                'content': docs[i] if i < len(docs) else '',
                'metadata': metadatas[i] if i < len(metadatas) else {}
            })
        
        return jsonify({
            'success': True,
            'memories': memories,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        error_msg = str(e)
        # 如果是超时错误，提供更友好的提示
        if "timeout" in error_msg.lower() or "超时" in error_msg:
            return jsonify({
                'success': False,
                'error': '获取记忆超时。记忆库可能较大，请稍后重试或使用搜索功能查找特定记忆。'
            }), 500
        return jsonify({
            'success': False,
            'error': error_msg
        }), 500

@app.route('/api/get_memory_by_id', methods=['GET'])
def get_memory_by_id():
    """通过ID获取单条记忆。支持 ?collection=chat_memory|summary_buffer|long_term_diary"""
    try:
        memory_id = request.args.get('id', '').strip()
        collection = request.args.get('collection', 'chat_memory')
        
        if not memory_id:
            return jsonify({
                'success': False,
                'error': '记忆ID不能为空'
            }), 400
        
        rag_instance = init_rag()
        result = rag_instance.collection_get(collection=collection, ids=[memory_id])
        
        ids = result.get('ids', [])
        documents = result.get('documents', [])
        metadatas = result.get('metadatas', [])
        
        if not ids or len(ids) == 0:
            return jsonify({
                'success': False,
                'error': f'未找到ID为 {memory_id} 的记忆'
            }), 404
        
        # 返回第一条匹配的记忆
        memory = {
            'id': ids[0],
            'content': documents[0] if documents else '',
            'metadata': metadatas[0] if metadatas else {}
        }
        
        return jsonify({
            'success': True,
            'memory': memory
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/add_memory', methods=['POST'])
def add_memory():
    """向当前选中的库添加一条记忆。支持 collection=chat_memory|summary_buffer|long_term_diary。"""
    try:
        data = request.json
        content = (data.get('content') or '').strip()
        collection = data.get('collection', 'chat_memory')
        memory_type = data.get('memory_type', 'episode_summary')  # 仅 chat_memory 使用：episode_summary | raw_conversation
        importance_score = int(data.get('importance_score', 7))
        importance_score = max(1, min(10, importance_score))

        if not content:
            return jsonify({
                'success': False,
                'error': '记忆内容不能为空'
            }), 400

        if collection == 'chat_memory' and memory_type not in ('episode_summary', 'raw_conversation'):
            memory_type = 'episode_summary'

        rag_instance = init_rag()
        import time
        ts = time.time()
        meta = {'timestamp': ts, 'importance_score': importance_score}
        # day_key: 以 04:00 为界，与 rag_server_core 一致
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.hour < 4:
            dt = dt - datetime.timedelta(days=1)
        meta['day_key'] = dt.strftime('%Y-%m-%d')

        ok = False
        if collection == 'summary_buffer':
            ok = rag_instance.add_to_summary_buffer(
                content=content,
                meta=meta,
                importance_score=importance_score
            )
        elif collection == 'long_term_diary':
            ok = rag_instance.add_to_diary(
                content=content,
                meta=meta,
                importance_score=importance_score
            )
        elif collection == 'qq_history_store':
             return jsonify({
                'success': False,
                'error': 'QQ 历史 RAG 不支持手动添加记忆'
            }), 400
        else:
            meta['type'] = memory_type
            if memory_type == 'episode_summary':
                ok = rag_instance.add_episode_summary(
                    content=content,
                    meta=meta,
                    importance_score=importance_score
                )
            else:
                ok = rag_instance.add_raw_conversation(
                    content=content,
                    meta=meta,
                    importance_score=importance_score
                )

        if not ok:
            return jsonify({
                'success': False,
                'error': '写入 RAG 失败，请确认 RAG 服务（rag_server）已启动且无异常'
            }), 500

        return jsonify({
            'success': True,
            'message': '已成功添加一条记忆',
            'collection': collection
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/update_memory_by_id', methods=['POST'])
def update_memory_by_id():
    """通过ID直接更新记忆内容。支持 collection=chat_memory|summary_buffer|long_term_diary"""
    try:
        data = request.json
        memory_id = data.get('memory_id', '').strip()
        new_content = data.get('new_content', '').strip()
        importance_score = int(data.get('importance_score', 7))
        preserve_timestamp = data.get('preserve_timestamp', True)
        collection = data.get('collection', 'chat_memory')

        if collection == 'qq_history_store':
             return jsonify({
                'success': False,
                'error': 'QQ 历史 RAG 不支持修改'
             }), 400
        
        if not memory_id:
            return jsonify({
                'success': False,
                'error': 'memory_id 参数不能为空'
            }), 400
        
        if not new_content:
            return jsonify({
                'success': False,
                'error': 'new_content 参数不能为空'
            }), 400
        
        rag_instance = init_rag()
        result = rag_instance.collection_get(collection=collection, ids=[memory_id])
        ids = result.get('ids', [])
        documents = result.get('documents', [])
        metadatas = result.get('metadatas', [])
        
        if not ids or len(ids) == 0:
            return jsonify({
                'success': False,
                'error': f'未找到ID为 {memory_id} 的记忆'
            }), 404
        
        original_metadata = metadatas[0] if metadatas else {}
        original_timestamp = original_metadata.get('timestamp')
        rag_instance.collection_delete(collection=collection, ids=[memory_id])
        
        new_meta = original_metadata.copy() if original_metadata else {}
        new_meta['importance_score'] = importance_score
        if preserve_timestamp and original_timestamp:
            new_meta['timestamp'] = original_timestamp
        else:
            import time
            new_meta['timestamp'] = time.time()
        
        ok = False
        if collection == 'summary_buffer':
            ok = rag_instance.add_to_summary_buffer(
                content=new_content,
                meta=new_meta,
                importance_score=importance_score
            )
        elif collection == 'long_term_diary':
            ok = rag_instance.add_to_diary(
                content=new_content,
                meta=new_meta,
                importance_score=importance_score
            )
        else:
            memory_type = original_metadata.get('type', 'episode_summary')
            if memory_type == 'episode_summary':
                ok = rag_instance.add_episode_summary(
                    content=new_content,
                    meta=new_meta,
                    importance_score=importance_score
                )
            else:
                ok = rag_instance.add_raw_conversation(
                    content=new_content,
                    meta=new_meta,
                    importance_score=importance_score
                )

        if not ok:
            return jsonify({
                'success': False,
                'error': '更新后写入 RAG 失败，请确认 RAG 服务（rag_server）已启动'
            }), 500

        timestamp_info = "保留了原日期" if (preserve_timestamp and original_timestamp) else "使用了当前日期"
        return jsonify({
            'success': True,
            'message': f'记忆已成功更新（{timestamp_info}）',
            'preserved_timestamp': preserve_timestamp and original_timestamp
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/update_memory', methods=['POST'])
def update_memory():
    """更新已入库的记忆"""
    try:
        data = request.json
        query = data.get('query', '').strip()
        new_content = data.get('new_content', '').strip()
        new_meta = data.get('new_meta') or {}
        new_importance_score = int(data.get('new_importance_score', 7))
        memory_type = data.get('memory_type', 'episode_summary')
        max_results = int(data.get('max_results', 10))
        preserve_timestamp = data.get('preserve_timestamp', True)  # 默认保留原日期
        replace_mode = data.get('replace_mode', False)  # 是否使用替换模式
        find_text = data.get('find_text', '').strip()  # 要查找的文本（替换模式）
        replace_text = data.get('replace_text', '').strip()  # 替换为的文本（替换模式）
        replace_all = data.get('replace_all', True)  # 是否替换所有匹配项
        
        # 根据模式验证参数
        if not query:
            return jsonify({
                'success': False,
                'error': 'query 参数不能为空'
            }), 400
        
        if replace_mode:
            # 查找替换模式：需要 find_text
            if not find_text:
                return jsonify({
                    'success': False,
                    'error': '查找替换模式下，find_text 参数不能为空'
                }), 400
        else:
            # 完全替换模式：需要 new_content
            if not new_content:
                return jsonify({
                    'success': False,
                    'error': '完全替换模式下，new_content 参数不能为空'
                }), 400
        
        rag_instance = init_rag()
        
        # 调用更新记忆方法
        result = rag_instance.update_memory(
            query=query,
            new_content=new_content,
            new_meta=new_meta,
            new_importance_score=new_importance_score,
            memory_type=memory_type,
            max_results=max_results,
            preserve_timestamp=preserve_timestamp,
            replace_mode=replace_mode,
            find_text=find_text,
            replace_text=replace_text,
            replace_all=replace_all
        )
        
        if result.get('updated'):
            response_data = {
                'success': True,
                'message': result.get('message', '记忆更新成功'),
                'deleted_count': result.get('deleted_count', 0),
                'preserved_timestamp': result.get('preserved_timestamp', False)
            }
            if 'added_count' in result:
                response_data['added_count'] = result.get('added_count', 0)
            return jsonify(response_data)
        else:
            return jsonify({
                'success': False,
                'error': result.get('message', '更新失败')
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/export_all', methods=['GET'])
def export_all_memories():
    """导出全部记忆为JSON文件。支持 ?collection=chat_memory|summary_buffer|long_term_diary"""
    try:
        collection = request.args.get('collection', 'chat_memory')
        rag_instance = init_rag()
        all_data = rag_instance.collection_get(collection=collection, timeout_ms=60000)
        ids = all_data.get('ids', [])
        docs = all_data.get('documents', [])
        metadatas = all_data.get('metadatas', [])
        
        if not ids:
            return jsonify({
                'success': False,
                'error': '记忆库为空，无需导出'
            }), 400
        
        # 构建导出数据
        export_data = {
            'export_time': datetime.datetime.now().isoformat(),
            'total_count': len(ids),
            'memories': []
        }
        
        for i in range(len(ids)):
            memory_item = {
                'id': ids[i],
                'content': docs[i] if i < len(docs) else '',
                'metadata': metadatas[i] if i < len(metadatas) else {}
            }
            
            # 添加格式化时间戳
            if 'timestamp' in memory_item['metadata']:
                ts = memory_item['metadata']['timestamp']
                memory_item['formatted_time'] = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            
            export_data['memories'].append(memory_item)
        
        # 显式用 ensure_ascii=False 序列化，保证导出的 JSON 里中文为原文而非 \uXXXX
        body = json.dumps(export_data, ensure_ascii=False, indent=2)
        response = Response(body, mimetype='application/json; charset=utf-8')
        suffix = {"chat_memory": "chat", "summary_buffer": "summary", "long_term_diary": "diary"}.get(collection, "chat")
        response.headers['Content-Disposition'] = f'attachment; filename=rag_memory_export_{suffix}_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        return response
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

if __name__ == '__main__':
    print("="*60)
    print("RAG 记忆图形化管理工具 - Web 服务器")
    print("="*60)
    # 注意：模块管理控制台已占用 5000 端口，因此本工具使用 5001 端口以避免冲突
    print("访问 http://localhost:5001 使用 RAG 记忆图形化界面")
    print("="*60)
    
    # 确保静态文件目录存在
    static_dir = os.path.join(os.path.dirname(__file__), 'rag_web_static')
    if not os.path.exists(static_dir):
        os.makedirs(static_dir)
    
    app.run(host='0.0.0.0', port=5001, debug=True)
