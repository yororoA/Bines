import os
import math
import time
import threading
import hashlib
import re
from config import RAG_EMBEDDING_MODEL, HF_ENDPOINT_DEFAULT, HF_DOWNLOAD_TIMEOUT

# 在导入HuggingFace库之前设置镜像源和超时配置，避免网络超时
# 优先使用环境变量，如果没有设置则自动使用镜像源
if not os.environ.get('HF_ENDPOINT') and not os.environ.get('HF_MIRROR'):
    # 自动设置镜像源（使用全局配置的默认值）
    os.environ['HF_ENDPOINT'] = HF_ENDPOINT_DEFAULT
    print(f"[RAG] 自动设置 HuggingFace 镜像源: {HF_ENDPOINT_DEFAULT}")
    print("[RAG] 如需使用其他镜像，请设置环境变量: HF_ENDPOINT=your-mirror-url")

# 设置HuggingFace Hub的超时时间（增加读取超时时间）
# 这些环境变量会被huggingface_hub库识别
timeout_str = str(HF_DOWNLOAD_TIMEOUT)
os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', timeout_str)  # 5分钟超时
os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', timeout_str)
print(f"[RAG] 设置下载超时时间: {timeout_str}秒")

from datetime import datetime, timedelta

from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document

# 一天的分割点：凌晨 04:00（04:00 之前算前一天）
DAY_START_HOUR = 4


def _clamp_ts_for_storage(ts):
    """写入 RAG 前将时间戳限制为不超过当前时间，避免手动/未来时间导致 time_decay 失效。"""
    now = time.time()
    if ts is None:
        return now
    return min(float(ts), now)


def get_day_key(timestamp):
    """给定时间戳，返回「天」键（YYYY-MM-DD），以 04:00 为界。"""
    if timestamp is None:
        return datetime.now().strftime("%Y-%m-%d")
    dt = datetime.fromtimestamp(float(timestamp))
    if dt.hour < DAY_START_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def get_day_key_to_time_range(day_key: str):
    """
    给定 day_key（YYYY-MM-DD），返回该「天」对应的时间戳范围 [start_ts, end_ts]（闭区间），
    以 04:00 为界：当天 04:00:00 至次日 03:59:59。
    用于判断摘要的 [start_time, end_time] 是否与查询日有交集。
    """
    if not day_key or not str(day_key).strip():
        return None, None
    try:
        d = datetime.strptime(str(day_key).strip()[:10], "%Y-%m-%d")
        day_start = d.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
        return time.mktime(day_start.timetuple()) + day_start.microsecond / 1e6, time.mktime(day_end.timetuple()) + day_end.microsecond / 1e6
    except Exception:
        return None, None


def _sanitize_metadata_for_chroma(meta: dict | None) -> dict:
    """Chroma 只接受 str/int/float/bool 类型的 metadata 值，过滤掉不合法项避免写入失败。"""
    if not meta or not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list):
            # Chroma 不支持 list，转为逗号分隔字符串（元素为 str）
            try:
                out[k] = ",".join(str(x) for x in v)
            except Exception:
                continue
        elif isinstance(v, (dict, object)) and type(v).__name__ != "type":
            continue
    return out

# 全局缓存：共享embedding模型实例，避免重复加载
_global_embedding_model = None
_global_embedding_lock = None


def _detect_device():
    """
    检测可用的计算设备（GPU 优先）

    [性能优化] 优先使用 GPU（CUDA 或 MPS）加速 Embedding 模型，减少 CPU 资源占用。
    这样可以避免与语音合成或主 LLM 推理抢占 CPU 资源，减少卡顿。

    Returns:
        str: 'cuda', 'mps', 或 'cpu'
    """
    # 1. 检测 CUDA（NVIDIA GPU）
    try:
        import torch
        if torch.cuda.is_available():
            print(f"✓ 检测到 CUDA GPU: {torch.cuda.get_device_name(0)}")
            return 'cuda'
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ CUDA 检测失败: {e}")

    # 2. 检测 MPS（Apple Silicon GPU）
    try:
        import torch
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            print("✓ 检测到 Apple Silicon GPU (MPS)")
            return 'mps'
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ MPS 检测失败: {e}")

    # 3. Fallback 到 CPU
    print("⚠️ 未检测到 GPU，使用 CPU（性能较慢）")
    return 'cpu'


def _get_global_embedding_model():
    """获取全局共享的embedding模型（单例模式）"""
    global _global_embedding_model, _global_embedding_lock

    if _global_embedding_model is not None:
        return _global_embedding_model

    # 使用线程锁确保只加载一次
    if _global_embedding_lock is None:
        _global_embedding_lock = threading.Lock()

    with _global_embedding_lock:
        # 双重检查，防止多线程重复加载
        if _global_embedding_model is not None:
            return _global_embedding_model

        print("正在加载 Embedding 模型...", end=" ")
        model_name = os.environ.get("RAG_EMBEDDING_MODEL", RAG_EMBEDDING_MODEL)
        print(f"模型: {model_name}")
        print("提示: 模型将缓存到内存中，后续实例将复用此模型，无需重复加载")
        current_mirror = os.environ.get('HF_ENDPOINT', '未设置')
        print(f"当前使用的镜像源: {current_mirror}")

        # [性能优化] 检测并使用 GPU（如果可用）
        device = _detect_device()
        if device != 'cpu':
            print(f"✓ 将使用 {device.upper()} 加速 Embedding 模型")
        else:
            print("⚠️ 使用 CPU，建议安装 PyTorch with CUDA/MPS 以获得更好性能")

        # 检查本地缓存
        cache_dir = os.path.expanduser('~/.cache/huggingface/hub')
        if os.path.exists(cache_dir):
            print(f"✓ 检测到本地缓存目录: {cache_dir}")
            print("  模型文件已缓存，正在从磁盘加载到内存（这需要一些时间）...")
        else:
            print("⚠️ 未找到本地缓存，首次运行需要从网络下载模型...")

        # 使用 HuggingFace 的轻量级模型将文本转换为向量
        # 增加超时时间和重试配置，使用本地缓存
        max_retries = 3
        retry_delay = 5

        for attempt in range(max_retries):
            try:
                # 检查是否已设置镜像源环境变量
                use_mirror = os.environ.get('HF_ENDPOINT', '') or os.environ.get('HF_MIRROR', '')

                if not use_mirror and attempt > 0:
                    # 如果第一次失败，尝试使用镜像源
                    print(f"第 {attempt + 1} 次尝试: 使用镜像源...")
                    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

                _global_embedding_model = HuggingFaceEmbeddings(
                    model_name=model_name,
                    model_kwargs={
                        'device': device,  # [性能优化] 使用 GPU（如果可用）
                        'trust_remote_code': True
                    },
                    encode_kwargs={
                        'normalize_embeddings': True
                    },
                    # 使用本地缓存，避免重复下载
                    cache_folder=None  # 使用默认缓存目录
                )
                print(f"✓ Embedding 模型加载成功（已缓存到内存，设备: {device.upper()}）")
                return _global_embedding_model

            except Exception as e:
                error_msg = str(e)
                print(f"⚠️ 第 {attempt + 1} 次尝试失败")

                # 检查是否是网络超时错误
                is_timeout = ("timeout" in error_msg.lower() or
                              "timed out" in error_msg.lower() or
                              "ReadTimeoutError" in str(type(e).__name__))

                if is_timeout:
                    print(f"   错误类型: 网络连接超时")
                    print(f"   当前镜像源: {os.environ.get('HF_ENDPOINT', '未设置')}")
                    print(f"   提示: 模型文件较大，下载可能需要较长时间")
                    if attempt < max_retries - 1:
                        print(f"   等待 {retry_delay} 秒后重试（第 {attempt + 2} 次）...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # 指数退避
                    else:
                        print("\n" + "=" * 60)
                        print("❌ 模型加载失败: 网络连接超时（已重试 3 次）")
                        print("\n解决方案:")
                        print("1. 检查网络连接，确保可以访问镜像源")
                        print("2. 尝试使用VPN或代理:")
                        print("   Windows: set HTTPS_PROXY=http://your-proxy:port")
                        print("   Linux/Mac: export HTTPS_PROXY=http://your-proxy:port")
                        print("3. 等待网络状况改善后重试（模型下载可能需要几分钟）")
                        print("4. 如果模型已部分下载，可以等待下载完成")
                        print("   缓存目录通常在: ~/.cache/huggingface/hub/")
                        print("=" * 60)
                        raise
                else:
                    # 其他类型的错误
                    print(f"   错误信息: {error_msg[:300]}")
                    if attempt < max_retries - 1:
                        print(f"   等待 {retry_delay} 秒后重试...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        print("\n" + "=" * 60)
                        print("❌ 模型加载失败，已重试 3 次")
                        print(f"最后错误: {error_msg[:300]}")
                        print("\n解决方案:")
                        print("1. 检查网络连接")
                        print("2. 检查是否有足够的磁盘空间")
                        print("3. 尝试手动下载模型")
                        print("=" * 60)
                        raise

        return _global_embedding_model


def _normalize_content_for_hash(text):
    """
    规范化文本内容用于 Hash 计算，去除标点、空格等不影响语义的字符

    [优化] 仅对"核心语义内容"进行 Hash，避免标点符号、空格变化导致 Hash 不同
    这样可以提高去重准确率，减少因格式差异导致的冗余插入

    Args:
        text: 原始文本

    Returns:
        str: 规范化后的文本
    """
    if not text:
        return ""

    # 1. 转换为小写（统一大小写）
    text = text.lower()

    # 2. 移除所有标点符号和特殊字符（保留中文、英文、数字）
    # 保留中文字符、英文字母、数字
    text = re.sub(r'[^\u4e00-\u9fa5a-z0-9\s]', '', text)

    # 3. 将多个连续空格/换行符合并为单个空格
    text = re.sub(r'\s+', ' ', text)

    # 4. 去除首尾空格
    text = text.strip()

    return text


class RAGMemory:
    def __init__(self, persist_directory="./rag_db"):
        """
        初始化 RAG 记忆系统

        [性能优化] 使用全局缓存的 embedding 模型，避免重复加载。

        [多进程警告] ChromaDB 使用 SQLite 作为后端，不支持多进程并发访问。
        如果未来开启多进程（ProcessPool），可能会导致数据库锁死。
        建议：
        1. 使用多线程（ThreadPoolExecutor）而不是多进程
        2. 如果必须使用多进程，考虑使用 ChromaDB 的 HTTP 服务器模式
        3. 或者使用支持多进程的向量数据库（如 Qdrant、Milvus）
        """
        # 确保路径是绝对路径
        self.persist_directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), persist_directory)

        # 使用全局缓存的embedding模型，避免重复加载
        self.embedding_fn = _get_global_embedding_model()

        # 初始化 Chroma 向量数据库 (持久化存储)
        # [性能优化] ChromaDB 实例化开销较小，但在多进程环境下需要注意 SQLite 锁死问题
        self.vector_store = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embedding_fn,
            collection_name="chat_memory"
        )
        self.summary_buffer_store = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embedding_fn,
            collection_name="summary_buffer",
        )
        self.diary_store = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embedding_fn,
            collection_name="long_term_diary",
        )
        self.qq_history_store = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embedding_fn,
            collection_name="qq_history_store",
        )
        print(f"RAG 向量记忆库已加载: {self.persist_directory} (chat_memory, summary_buffer, long_term_diary, qq_history_store)")

    def add_interaction(self, user_text, assistant_text, importance_score=5):
        """
        保存一次交互对话
        若库中已存在高度相似的内容，则删除旧文档并插入更新后的新文档（真正的更新逻辑）

        Args:
            user_text: 用户输入文本
            assistant_text: 助手回复文本
            importance_score: 重要性权重（1-10），默认5。高分记忆（>=7）永久保留，低分记忆更容易被GC
        """
        if not user_text or not assistant_text:
            return

        content = f"User: {user_text}\nAssistant: {assistant_text}"

        # [优化] 使用规范化后的内容计算 Hash，避免标点、空格变化导致 Hash 不同
        # 仅对"核心语义内容"进行 Hash，提高去重准确率
        normalized_content = _normalize_content_for_hash(content)
        content_hash = hashlib.md5(normalized_content.encode('utf-8')).hexdigest()

        # 1. 先进行相似度检查
        # [优化] 使用更严格的阈值（0.1）来识别高度相似的文档
        # Chroma 默认距离是 L2，越小越相似。0.1 表示几乎完全相同（同义改写或格式差异）
        similar_docs = self.vector_store.similarity_search_with_score(content, k=5)

        count = 1
        ids_to_delete = []

        if similar_docs:
            # 第一步：收集所有相似文档的元信息和内容，确定最大count值
            similar_contents = []  # 存储需要删除的文档内容
            similar_content_hashes = []  # 存储相似文档的 content_hash
            for doc, score in similar_docs:
                # [优化] 使用更严格的阈值（0.1）来识别高度相似的文档
                # 如果向量距离 < 0.1，视为重复（即使 content_hash 不同）
                if score < 0.1:
                    existing_metadata = doc.metadata or {}
                    # 只处理conversation类型的文档
                    if existing_metadata.get("type") == "conversation":
                        # 读取旧的 count，取最大值（可能有多个重复条目）
                        old_count = existing_metadata.get("access_count", 1)
                        count = max(count, old_count + 1)
                        # 记录需要删除的文档内容
                        similar_contents.append(doc.page_content)
                        # 记录相似文档的 content_hash（用于后续查询文档ID）
                        similar_content_hashes.append(existing_metadata.get("content_hash"))

            # 第二步：通过 content_hash 精确查找需要删除的文档ID
            # [优化] 优先使用向量距离去重，content_hash 作为辅助
            if similar_contents and count > 1:
                try:
                    # [优化] 优先使用向量距离去重
                    # 如果向量距离 < 0.1，通过相似文档的 content_hash 查找需要删除的文档ID
                    # content_hash 作为辅助手段，用于查找格式不同但语义相同的文档

                    # 通过相似文档的 content_hash 查找需要删除的文档ID
                    for similar_hash in similar_content_hashes:
                        if similar_hash:
                            try:
                                matching_docs = self.vector_store.get(
                                    where={"content_hash": similar_hash, "type": "conversation"}
                                )
                                if matching_docs and "ids" in matching_docs and matching_docs["ids"]:
                                    ids_to_delete.extend(matching_docs["ids"])
                            except Exception:
                                pass

                    # 如果向量距离去重没有找到足够的文档，尝试使用当前文档的 content_hash 作为补充
                    if len(ids_to_delete) == 0:
                        try:
                            # 使用 content_hash 进行精确查询（作为辅助手段）
                            matching_docs = self.vector_store.get(
                                where={"content_hash": content_hash, "type": "conversation"}
                            )

                            if matching_docs and "ids" in matching_docs and matching_docs["ids"]:
                                # 找到匹配的文档，添加到删除列表
                                ids_to_delete.extend(matching_docs["ids"])
                                print(f"[RAG] 通过 content_hash 找到 {len(matching_docs['ids'])} 条重复记忆（辅助去重）")
                        except Exception as hash_error:
                            # 如果隐式查询失败，尝试只查询 content_hash（fallback）
                            try:
                                matching_docs = self.vector_store.get(
                                    where={"content_hash": content_hash}
                                )
                                if matching_docs and "ids" in matching_docs and matching_docs["ids"]:
                                    # 过滤出 conversation 类型
                                    metadatas = matching_docs.get("metadatas", [])
                                    filtered_ids = []
                                    for idx, meta in enumerate(metadatas):
                                        if meta and meta.get("type") == "conversation":
                                            filtered_ids.append(matching_docs["ids"][idx])
                                    if filtered_ids:
                                        ids_to_delete.extend(filtered_ids)
                                        print(f"[RAG] 通过 content_hash 找到 {len(filtered_ids)} 条重复记忆（已过滤类型，辅助去重）")
                            except Exception as simple_error:
                                # 如果查询失败，fallback 到接受轻微冗余
                                print(f"[RAG] content_hash 查询失败，将插入新文档（count={count}）: {simple_error}")

                    # 执行删除
                    if ids_to_delete:
                        # 去重（避免重复删除）
                        ids_to_delete = list(set(ids_to_delete))
                        self.vector_store.delete(ids=ids_to_delete)
                        print(f"[RAG] 已删除 {len(ids_to_delete)} 条重复记忆（向量距离去重，distance < 0.1），执行更新逻辑（count={count}）...")
                    elif count > 1:
                        # 如果无法精确定位，新文档的 count 已经设置为 old_count + 1，权重更高
                        print(f"[RAG] 无法精确定位重复文档ID，将插入新文档（count={count}）...")
                except Exception as e:
                    print(f"[RAG] 删除旧文档失败: {e}，将插入新文档（count={count}）...")

        ts = time.time()
        doc = Document(
            page_content=content,
            metadata={
                "type": "conversation",
                "user_content": user_text[:50],
                "access_count": count,
                "timestamp": ts,
                "day_key": get_day_key(ts),  # 按日期检索
                "content_hash": content_hash,
                "importance_score": importance_score,
            }
        )
        self.vector_store.add_documents([doc])

    def add_episode_summary(self, content: str, meta: dict | None = None, importance_score=7):
        """
        写入一条"剧情摘要"类型的记忆，用于中长期回忆。

        Args:
            content: 已经生成好的摘要文本
            meta: 额外的元数据字典（如果包含 timestamp，将使用该时间戳，否则使用当前时间）
            importance_score: 重要性权重（1-10），默认7（高重要性，永久保留）
        """
        if not content:
            print("[RAG Server] 警告: add_episode_summary 收到空内容，跳过存储")
            return
        ts = (meta or {}).get("end_time") or (meta or {}).get("timestamp") or time.time()
        ts = _clamp_ts_for_storage(ts)
        metadata = {
            "type": "episode_summary",
            "access_count": 1,
            "timestamp": ts,
            "day_key": get_day_key(ts),  # 按日期检索
            "priority": "high",
            "importance_score": importance_score,
        }
        if meta and isinstance(meta, dict):
            metadata.update(_sanitize_metadata_for_chroma(meta))
        else:
            metadata = _sanitize_metadata_for_chroma(metadata)
        doc = Document(page_content=content, metadata=metadata)
        try:
            self.vector_store.add_documents([doc])
            print(f"[RAG Server] 成功存储剧情摘要（长度: {len(content)} 字符，重要性: {importance_score}/10，类型: episode_summary）")
        except Exception as e:
            print(f"[RAG Server] 存储剧情摘要失败: {e}")
            import traceback
            traceback.print_exc()
            raise

    def add_raw_conversation(self, content: str, meta: dict | None = None, importance_score=5):
        """
        写入一条"原始对话"类型的记忆，用于保留具体细节和原话。

        [混合存储策略] 原始对话权重较低，用于精确检索，补充摘要的细节。

        Args:
            content: 原始对话文本（格式：User: xxx\nAssistant: xxx）
            meta: 额外的元数据字典（如果包含 timestamp，将使用该时间戳，否则使用当前时间）
            importance_score: 重要性权重（1-10），默认5（中等重要性）
        """
        if not content:
            return

        # [优化] 使用规范化后的内容计算 Hash，避免标点、空格变化导致 Hash 不同
        normalized_content = _normalize_content_for_hash(content)
        content_hash = hashlib.md5(normalized_content.encode('utf-8')).hexdigest()

        ts = (meta or {}).get("timestamp") or (meta or {}).get("end_time") or time.time()
        ts = _clamp_ts_for_storage(ts)
        metadata = {
            "type": "raw_conversation",
            "access_count": 1,
            "timestamp": ts,
            "day_key": get_day_key(ts),  # 按日期检索
            "priority": "low",
            "content_hash": content_hash,
            "importance_score": importance_score,
        }
        if meta and isinstance(meta, dict):
            metadata.update(_sanitize_metadata_for_chroma(meta))
        else:
            metadata = _sanitize_metadata_for_chroma(metadata)
        doc = Document(page_content=content, metadata=metadata)
        try:
            self.vector_store.add_documents([doc])
        except Exception as e:
            print(f"[RAG Server] 存储原始对话失败: {e}")
            import traceback
            traceback.print_exc()
            raise

    def add_to_summary_buffer(self, content: str, meta: dict | None = None, importance_score: int = 7):
        """写入短期碎片库 Buffer RAG（每个短期记忆窗口的原始摘要）。meta 中应有 timestamp；会自动写入 day_key（04:00 为界）。"""
        if not content:
            return
        ts = (meta or {}).get("end_time") or (meta or {}).get("timestamp") or time.time()
        ts = _clamp_ts_for_storage(ts)
        day_key = get_day_key(ts)
        metadata = {
            "type": "summary_buffer",
            "timestamp": ts,
            "day_key": day_key,
            "importance_score": importance_score,
        }
        if meta and isinstance(meta, dict):
            metadata.update(_sanitize_metadata_for_chroma(meta))
        else:
            metadata = _sanitize_metadata_for_chroma(metadata)
        doc = Document(page_content=content, metadata=metadata)
        try:
            self.summary_buffer_store.add_documents([doc])
        except Exception as e:
            print(f"[RAG Server] 存储摘要 Buffer 失败: {e}")
            import traceback
            traceback.print_exc()
            raise

    def add_to_diary(self, content: str, meta: dict | None = None, importance_score: int = 8):
        """写入长期记忆库 Diary RAG（按天汇总、RP-LLM 润色的日记）。"""
        if not content:
            return
        metadata = {
            "type": "diary",
            "timestamp": time.time(),
            "importance_score": importance_score,
        }
        if meta and isinstance(meta, dict):
            metadata.update(_sanitize_metadata_for_chroma(meta))
        else:
            metadata = _sanitize_metadata_for_chroma(metadata)
        doc = Document(page_content=content, metadata=metadata)
        try:
            self.diary_store.add_documents([doc])
        except Exception as e:
            print(f"[RAG Server] 存储日记失败: {e}")
            import traceback
            traceback.print_exc()
            raise

    def add_qq_log(self, content: str, meta: dict | None = None):
        """
        写入一条 QQ 消息记录（群聊背景消息等）。
        纯文本存入单独的 collection (qq_history_store)，用于专门检索。
        """
        if not content:
            return
        
        ts = (meta or {}).get("timestamp") or time.time()
        ts = _clamp_ts_for_storage(ts)
        
        # 规范化内容生成 hash
        normalized_content = _normalize_content_for_hash(content)
        content_hash = hashlib.md5(normalized_content.encode('utf-8')).hexdigest()
        
        metadata = {
            "type": "qq_log",
            "timestamp": ts,
            "day_key": get_day_key(ts),
            "content_hash": content_hash,
        }
        if meta and isinstance(meta, dict):
            # 将 meta 中的字段合并进来（例如 sender, group_id, user_id, is_group）
            safe_meta = _sanitize_metadata_for_chroma(meta)
            metadata.update(safe_meta)
            
        doc = Document(page_content=content, metadata=metadata)
        try:
            self.qq_history_store.add_documents([doc])
            # print(f"[RAG] 存储QQ消息成功: {content[:20]}...")
        except Exception as e:
            print(f"[RAG] 存储QQ消息失败: {e}")
            import traceback
            traceback.print_exc()

    def search_qq_history(self, query: str, k: int = 5, day_key: str | None = None, group_id: str | None = None, user_id: str | None = None):
        """
        搜索 QQ 历史记录。支持按日期、群号、用户过滤。
        """
        filter_dict = {}
        if day_key:
            filter_dict["day_key"] = day_key
        if group_id:
            # metadata 中存储的是 str 等基本类型，传入值需转 str
            filter_dict["group_id"] = str(group_id)
        if user_id:
            filter_dict["user_id"] = str(user_id)
            
        # 如果 filter_dict 为空，则传 None
        where_filter = filter_dict if filter_dict else None
        
        try:
            results = self.qq_history_store.similarity_search_with_score(
                query, k=k, filter=where_filter
            )
            # 返回格式: [(content, metadata, score), ...]
            return [(doc.page_content, doc.metadata, score) for doc, score in results]
        except Exception as e:
            print(f"[RAG] 搜索QQ历史失败: {e}")
            traceback.print_exc()
            return []

    def _summary_doc_matches_day(self, doc, day_key: str, day_start_ts: float, day_end_ts: float) -> bool:
        """判断摘要文档的时间范围 [start_time, end_time] 是否与查询日时间范围有交集；无时间范围时退化为 day_key 相等。时间戳支持秒或毫秒（>1e12 时按毫秒转秒）。"""
        meta = (doc.metadata or {}) if hasattr(doc, "metadata") else {}
        doc_start = meta.get("start_time")
        doc_end = meta.get("end_time")
        if doc_start is not None or doc_end is not None:
            try:
                ds = float(doc_start) if doc_start is not None else float(doc_end)
                de = float(doc_end) if doc_end is not None else float(doc_start)
                if ds > 1e12:
                    ds = ds / 1000.0
                if de > 1e12:
                    de = de / 1000.0
            except (TypeError, ValueError):
                return (meta.get("day_key") or "").strip() == (day_key or "").strip()
            return max(ds, day_start_ts) <= min(de, day_end_ts)
        return (meta.get("day_key") or "").strip() == (day_key or "").strip()

    def get_relevant_context_summary_buffer(self, query: str, k: int = 5, day_key: str | None = None):
        """检索 Buffer RAG；可选 day_key（YYYY-MM-DD）按日期过滤。有 start_time/end_time 的摘要按时间范围交集匹配，避免跨天摘要漏检。"""
        try:
            store = self.summary_buffer_store
            day_start_ts, day_end_ts = None, None
            if day_key:
                day_start_ts, day_end_ts = get_day_key_to_time_range(day_key)
            raw = store.similarity_search_with_score(query, k=k * 4 if day_key else k)
            if day_key and day_start_ts is not None and day_end_ts is not None:
                raw = [(doc, sc) for doc, sc in raw if self._summary_doc_matches_day(doc, day_key, day_start_ts, day_end_ts)][:k]
            elif day_key:
                raw = [(doc, sc) for doc, sc in raw if (doc.metadata or {}).get("day_key") == day_key][:k]
            return [doc.page_content for doc, _ in raw]
        except Exception as e:
            print(f"[RAG] summary_buffer 检索失败: {e}")
            return []

    def get_relevant_context_diary(self, query: str, k: int = 3, day_key: str | None = None):
        """检索长期日记（Diary RAG）；可选 day_key 按日期过滤，三库均支持按日期检索。"""
        try:
            store = self.diary_store
            if day_key:
                coll = getattr(store, "_collection", None) or getattr(store, "collection", None)
                if coll is None and hasattr(store, "_client"):
                    try:
                        coll = store._client.get_collection(name="long_term_diary")
                    except Exception:
                        coll = None
                if coll is not None:
                    emb = self.embedding_fn.embed_query(query)
                    raw = coll.query(
                        query_embeddings=[emb],
                        n_results=k,
                        where={"day_key": day_key},
                        include=["documents", "metadatas", "distances"],
                    )
                    docs = (raw.get("documents") or [[]])[0]
                    return list(docs) if docs else []
            raw = store.similarity_search_with_score(query, k=k)
            if day_key:
                raw = [(doc, sc) for doc, sc in raw if (doc.metadata or {}).get("day_key") == day_key][:k]
            return [doc.page_content for doc, _ in raw]
        except Exception as e:
            print(f"[RAG] long_term_diary 检索失败: {e}")
            return []

    def get_raw_conversations_by_day(self, day_key: str, limit: int = 20) -> list:
        """按 day_key 取 chat_memory 中当日原始对话（含 conversation / raw_conversation），用于写日记时传入同日对话。"""
        if not day_key or not str(day_key).strip():
            return []
        dk = str(day_key).strip()
        try:
            data = self.vector_store.get(
                where={"$and": [{"day_key": dk}, {"type": {"$in": ["raw_conversation", "conversation"]}}]},
                include=["documents"],
            )
            docs = data.get("documents") or []
            if isinstance(docs, list) and docs and isinstance(docs[0], list):
                docs = docs[0]
            return [d for d in docs[:limit] if d and isinstance(d, str) and d.strip()]
        except Exception as e:
            try:
                data = self.vector_store.get(include=["documents", "metadatas"])
                all_docs = data.get("documents") or []
                all_metas = data.get("metadatas") or []
                if isinstance(all_docs, list) and all_docs and isinstance(all_docs[0], list):
                    all_docs = all_docs[0]
                out = []
                for i, doc in enumerate(all_docs[: limit * 3]):
                    if not doc or not isinstance(doc, str):
                        continue
                    meta = (all_metas[i] if i < len(all_metas) else None) or {}
                    if meta.get("day_key") != day_key:
                        continue
                    if meta.get("type") in ("raw_conversation", "conversation"):
                        out.append(doc.strip())
                    if len(out) >= limit:
                        break
                return out
            except Exception as e2:
                print(f"[RAG] get_raw_conversations_by_day 失败: {e2}")
                return []

    def get_all_summary_buffer(self):
        """获取 Buffer RAG 中全部文档（用于上线按天汇总后删除）。返回 [{"id": ..., "document": ..., "metadata": ...}, ...]"""
        try:
            # Chroma get() 的 include 不支持 ids，不传 include 以获取默认返回值（含 ids）
            data = self.summary_buffer_store.get()
            ids = data.get("ids") or []
            docs = data.get("documents") or []
            metas = data.get("metadatas") or []
            out = []
            for i in range(len(ids)):
                out.append({
                    "id": ids[i],
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": (metas[i] if i < len(metas) else None) or {},
                })
            return out
        except Exception as e:
            print(f"[RAG] get_all_summary_buffer 失败: {e}")
            return []

    def delete_summary_buffer_ids(self, ids: list):
        """删除 Buffer RAG 中指定 id 的文档（汇总入日记后调用）。"""
        if not ids:
            return
        try:
            self.summary_buffer_store.delete(ids=ids)
        except Exception as e:
            print(f"[RAG] delete_summary_buffer_ids 失败: {e}")

    def get_existing_diary_day_keys(self):
        """返回日记 RAG 中已存在日记的 day_key 集合（用于上线汇总时跳过已写过的日期，避免重复写 02/01 等）。"""
        try:
            data = self.diary_store.get()
            metas = data.get("metadatas") or []
            day_keys = set()
            for m in metas:
                if isinstance(m, dict) and m.get("day_key"):
                    day_keys.add(str(m["day_key"]).strip())
            return list(day_keys)
        except Exception as e:
            print(f"[RAG] get_existing_diary_day_keys 失败: {e}")
            return []

    def get_latest_qq_logs(self, group_id=None, user_id=None, limit=10):
        """
        [新增] 纯按时间倒序获取最新的 QQ 记录，用于上下文补充
        """
        try:
            where_filter = {}
            if group_id:
                where_filter["group_id"] = str(group_id)
            if user_id:
                where_filter["user_id"] = str(user_id)
            
            # Chroma 的 get 方法不支持按时间排序返回，只能取出来自己排
            # 这是一个性能瓶颈，但对于轻量级应用可以接受
            # 或者只获取最近写入的（Chroma 默认存储顺序通常是写入顺序，但不保证）
            
            result = self.qq_history_store.get(
                where=where_filter,
                limit=limit * 2, # 多取一点以防万一
                include=["documents", "metadatas"]
            )
            
            # 组合数据
            logs = []
            ids = result.get("ids", [])
            docs = result.get("documents", [])
            metas = result.get("metadatas", [])
            
            for i in range(len(ids)):
                if i < len(docs) and i < len(metas):
                    logs.append({
                        "content": docs[i],
                        "meta": metas[i]
                    })
            
            # Python 侧按时间戳倒序排序
            logs.sort(key=lambda x: x["meta"].get("timestamp", 0), reverse=True)
            
            return logs[:limit]
            
        except Exception as e:
            print(f"[RAG] get_latest_qq_logs failed: {e}")
            return []

    def get_relevant_context(self, query, k=4, min_timestamp=None, time_of_day=None, tags=None, day_key=None):
        """
        根据用户的 query 检索历史记忆，并根据 'access_count' 和时间衰减进行加权重排。

        Args:
            query: 检索查询文本
            k: 返回的文档数量
            min_timestamp: 可选，最小时间戳过滤器
            time_of_day: 可选，按 metadata.time_of_day 过滤（morning/afternoon/night）
            tags: 可选，按 metadata.tags 包含任一标签过滤
            day_key: 可选，按日期过滤（YYYY-MM-DD，04:00 为界），三库均支持按日期检索
        """
        try:
            results_with_score = []
            where_clause = None
            where_parts = []
            if day_key and str(day_key).strip():
                where_parts.append({"day_key": str(day_key).strip()})
            if time_of_day and str(time_of_day).strip():
                where_parts.append({"time_of_day": str(time_of_day).strip().lower()})
            tag_filters = []
            if tags and isinstance(tags, list):
                for tag in tags[:5]:
                    if tag and str(tag).strip():
                        tag_filters.append({"tags": {"$contains": str(tag).strip()}})
            if tag_filters:
                where_parts.append(tag_filters[0] if len(tag_filters) == 1 else {"$or": tag_filters})
            if where_parts:
                where_clause = where_parts[0] if len(where_parts) == 1 else {"$and": where_parts}

            if where_clause is not None:
                try:
                    emb = self.embedding_fn.embed_query(query)
                    coll = getattr(self.vector_store, "_collection", None) or getattr(self.vector_store, "collection", None)
                    if coll is not None and emb is not None:
                        raw = coll.query(
                            query_embeddings=[emb],
                            n_results=min(k * 3, 50),
                            where=where_clause,
                            include=["documents", "metadatas", "distances"],
                        )
                        ids = (raw.get("ids") or [[]])[0]
                        docs_list = (raw.get("documents") or [[]])[0]
                        metas_list = (raw.get("metadatas") or [[]])[0]
                        dists = (raw.get("distances") or [[]])[0]
                        for i in range(len(ids)):
                            content = docs_list[i] if i < len(docs_list) else ""
                            meta = (metas_list[i] if i < len(metas_list) else None) or {}
                            dist = float(dists[i]) if i < len(dists) else 0.0
                            results_with_score.append((Document(page_content=content, metadata=meta), dist))
                except Exception as e:
                    print(f"[RAG] metadata 过滤检索失败，回退到无过滤: {e}")
            if not results_with_score:
                results_with_score = self.vector_store.similarity_search_with_score(query, k=k * 3)

            # 若请求了 tags 但未走 where（如 Chroma 不支持 $contains），在 Python 侧按 tags 过滤
            if tags and isinstance(tags, list) and tags:
                tag_set = {str(t).strip() for t in tags if t}
                if tag_set:
                    filtered = []
                    for doc, dist in results_with_score:
                        meta_tags = doc.metadata.get("tags") if isinstance(doc.metadata.get("tags"), list) else []
                        if any(t in meta_tags for t in tag_set):
                            filtered.append((doc, dist))
                    if filtered:
                        results_with_score = filtered

            # [修复] 如果提供了时间过滤器，先过滤掉时间戳小于min_timestamp的文档
            if min_timestamp is not None:
                filtered_results = []
                for doc, score in results_with_score:
                    meta = doc.metadata or {}
                    doc_timestamp = meta.get("timestamp", 0)
                    # 只保留时间戳小于min_timestamp的文档（即短期记忆窗口之前的记忆）
                    if doc_timestamp < min_timestamp:
                        filtered_results.append((doc, score))
                results_with_score = filtered_results

            # 2. 计算加权分数
            # 原始 score 是 distance (越小越好)。需要转换为 relevance (越大越好)
            # 基础公式: relevance = 1 / (1 + distance)
            # 访问强度: (1 + log(count))
            # 时间衰减: exp(-lambda * age_days)
            lambda_ = 0.03  # 时间衰减系数，可按需要微调

            scored_candidates = []
            now = time.time()
            for doc, distance in results_with_score:
                meta = doc.metadata or {}
                count = meta.get("access_count", 1)
                # 使用 min(timestamp, now) 避免未来时间戳导致 time_decay 失效（如手动改 JSON）
                timestamp = meta.get("timestamp", now)
                timestamp = min(float(timestamp), now) if timestamp is not None else now
                age_days = max((now - timestamp) / 86400.0, 0.0)

                # 基础相关性 (防止除零)
                relevance = 1.0 / (1.0 + distance)

                # 记忆强度的对数加成
                try:
                    strength_multiplier = 1.0 + math.log(max(count, 1))
                except ValueError:
                    strength_multiplier = 1.0

                # 时间衰减：越新的记忆衰减越小
                time_decay = math.exp(-lambda_ * age_days)

                final_score = relevance * strength_multiplier * time_decay

                # [混合存储策略] 根据类型调整权重
                # 摘要权重高，优先召回；原始对话权重低，作为补充
                doc_type = meta.get("type", "conversation")
                priority = meta.get("priority", "normal")

                if doc_type == "episode_summary" or priority == "high":
                    # 摘要权重高，优先召回
                    final_score *= 1.3
                elif doc_type == "raw_conversation" or priority == "low":
                    # 原始对话权重低，降低召回优先级（但仍保留，用于精确检索）
                    final_score *= 0.7

                scored_candidates.append((doc, final_score))

            # 3. 按最终分数降序排列
            scored_candidates.sort(key=lambda x: x[1], reverse=True)

            # 4. 取前 k 个并返回内容
            final_docs = [item[0].page_content for item in scored_candidates[:k]]

            return final_docs

        except Exception as e:
            print(f"RAG 检索失败: {e}")
            return []

    def clear(self):
        """清空记忆库"""
        try:
            # 删除集合中的所有数据
            ids = self.vector_store.get()["ids"]
            if ids:
                self.vector_store.delete(ids=ids)
            print("记忆库已清空")
        except Exception as e:
            print(f"清空记忆库异常: {e}")

    def garbage_collect(self,
                        min_age_days=90,
                        max_access_count=2,
                        dry_run=False,
                        batch_size=1000):
        """
        记忆清理（GC）：删除访问计数低且时间久远的记忆

        [性能优化] 使用批量处理和 where 过滤，避免全量加载导致 OOM

        Args:
            min_age_days: 最小年龄（天数），只有超过这个时间的记忆才会被考虑删除
            max_access_count: 最大访问计数，访问计数 <= 此值的记忆会被删除
            dry_run: 如果为 True，只统计不实际删除，用于预览清理效果
            batch_size: 批量处理大小，避免一次性加载过多数据

        Returns:
            dict: 清理统计信息，包含：
                - total_docs: 总文档数
                - deleted_count: 删除的文档数
                - kept_count: 保留的文档数
                - deleted_by_age: 因时间久远删除的数量
                - deleted_by_count: 因访问计数低删除的数量
        """
        try:
            now = time.time()
            min_timestamp = now - (min_age_days * 86400)  # 转换为秒

            # [性能优化] 使用 where 过滤，只查询需要检查的文档
            # 先获取总文档数（不加载内容）
            try:
                # 尝试使用 where 过滤获取符合条件的文档
                # 注意：Chroma 的 where 对时间戳过滤支持可能有限，这里先尝试
                old_docs = self.vector_store.get(
                    where={"timestamp": {"$lt": min_timestamp}}
                ) if hasattr(self.vector_store, 'get') else None
            except Exception:
                # 如果 where 过滤失败，fallback 到批量处理
                old_docs = None

            # [性能优化] 使用批量处理，避免全量加载
            # 分批获取文档，每次只处理 batch_size 个
            ids_to_delete = []
            stats = {
                "total_docs": 0,
                "deleted_count": 0,
                "kept_count": 0,
                "deleted_by_age": 0,
                "deleted_by_count": 0
            }

            # 批量处理：每次只加载 batch_size 个文档
            offset = 0
            while True:
                try:
                    # 获取一批文档（只获取 IDs 和 metadatas，不加载 documents）
                    batch = self.vector_store.get(
                        limit=batch_size,
                        offset=offset,
                        include=["metadatas"]  # 只获取 metadatas，不获取 documents
                    )

                    if not batch or "ids" not in batch or not batch["ids"]:
                        # 没有更多文档了
                        break

                    batch_ids = batch["ids"]
                    batch_metadatas = batch.get("metadatas", [])
                    stats["total_docs"] += len(batch_ids)

                    # 处理这一批文档
                    for idx in range(len(batch_ids)):
                        metadata = batch_metadatas[idx] if idx < len(batch_metadatas) else {}
                        doc_id = batch_ids[idx]

                        if not metadata:
                            # 没有元数据的文档，跳过（可能是系统文档）
                            stats["kept_count"] += 1
                            continue

                        doc_timestamp = metadata.get("timestamp", 0)
                        access_count = metadata.get("access_count", 1)
                        doc_type = metadata.get("type", "conversation")
                        importance_score = metadata.get("importance_score", 5)  # 默认重要性为5（中等）

                        # 计算文档年龄（天数）
                        age_days = (now - doc_timestamp) / 86400.0 if doc_timestamp > 0 else 0

                        # [优化] 引入重要性权重（Importance Score）保护重要记忆
                        # 高分记忆（>= 7）：永久保留，即使时间久远且访问次数低
                        # 中分记忆（4-6）：正常 GC 策略
                        # 低分记忆（<= 3）：更容易被 GC

                        # 判断是否需要删除
                        should_delete = False

                        # 保护高分记忆：重要性 >= 7 的记忆永久保留
                        if importance_score >= 7:
                            should_delete = False
                        # 保护中高分记忆：重要性 >= 5 的记忆需要更严格的条件
                        elif importance_score >= 5:
                            # 条件1：时间久远且访问计数低（更严格的条件）
                            if age_days >= min_age_days * 1.5 and access_count <= max_access_count:
                                should_delete = True
                                stats["deleted_by_age"] += 1
                                stats["deleted_by_count"] += 1
                            # 条件2：时间极其久远（超过 min_age_days * 3），无论访问计数如何
                            elif age_days >= min_age_days * 3:
                                should_delete = True
                                stats["deleted_by_age"] += 1
                        # 低分记忆：更容易被 GC
                        else:
                            # 条件1：时间久远且访问计数低
                            if age_days >= min_age_days and access_count <= max_access_count:
                                should_delete = True
                                stats["deleted_by_age"] += 1
                                stats["deleted_by_count"] += 1
                            # 条件2：时间极其久远（超过 min_age_days * 2），无论访问计数如何
                            elif age_days >= min_age_days * 2:
                                should_delete = True
                                stats["deleted_by_age"] += 1
                            # 条件3：访问计数为1且时间超过 min_age_days（从未被强化过的记忆）
                            elif access_count == 1 and age_days >= min_age_days:
                                should_delete = True
                                stats["deleted_by_count"] += 1

                        # 保护重要记忆：剧情摘要类型的记忆需要更严格的条件
                        if doc_type == "episode_summary":
                            # 剧情摘要需要更长时间（2倍）或更低的访问计数（1）才删除
                            if age_days < min_age_days * 2 or access_count > 1:
                                should_delete = False

                        if should_delete:
                            ids_to_delete.append(doc_id)
                            stats["deleted_count"] += 1
                        else:
                            stats["kept_count"] += 1

                    # 如果这批文档数量少于 batch_size，说明已经处理完所有文档
                    if len(batch_ids) < batch_size:
                        break

                    offset += batch_size

                    # [性能优化] 每处理一批后，如果积累的待删除ID过多，先执行一次删除
                    # 避免内存中积累过多ID
                    if len(ids_to_delete) >= batch_size and not dry_run:
                        try:
                            self.vector_store.delete(ids=ids_to_delete[:batch_size])
                            ids_to_delete = ids_to_delete[batch_size:]
                            print(f"[RAG GC] 已删除一批记忆，剩余待删除: {len(ids_to_delete)}")
                        except Exception as e:
                            print(f"[RAG GC] 批量删除失败: {e}")

                except Exception as batch_error:
                    print(f"[RAG GC] 批量处理错误: {batch_error}")
                    break

            # 执行最后的删除（如果不是 dry_run）
            if ids_to_delete and not dry_run:
                try:
                    self.vector_store.delete(ids=ids_to_delete)
                    print(f"[RAG GC] 已删除最后 {len(ids_to_delete)} 条记忆")
                except Exception as e:
                    print(f"[RAG GC] 删除失败: {e}")
                    stats["deleted_count"] -= len(ids_to_delete)  # 调整计数
            elif ids_to_delete and dry_run:
                print(f"[RAG GC] [预览模式] 将删除 {len(ids_to_delete)} 条记忆")

            return stats

        except Exception as e:
            print(f"[RAG GC] 清理过程异常: {e}")
            import traceback
            traceback.print_exc()
            return {
                "total_docs": 0,
                "deleted_count": 0,
                "kept_count": 0,
                "deleted_by_age": 0,
                "deleted_by_count": 0,
                "error": str(e)
            }

    def get_stats(self, batch_size=1000):
        """
        获取记忆库统计信息

        [性能优化] 使用批量处理替代全量加载，避免内存飙升
        严禁无条件的 get()，使用分页/迭代器方式统计

        Args:
            batch_size: 批量处理大小，默认1000

        Returns:
            dict: 统计信息，包含总文档数、各类型文档数、平均访问计数等
        """
        try:
            # [性能优化] 尝试使用 ChromaDB 的 count() 方法获取总数（最快）
            total_docs = 0
            try:
                # ChromaDB 的 _collection 可能有 count() 方法
                if hasattr(self.vector_store, '_collection') and hasattr(self.vector_store._collection, 'count'):
                    total_docs = self.vector_store._collection.count()
                elif hasattr(self.vector_store, 'collection') and hasattr(self.vector_store.collection, 'count'):
                    total_docs = self.vector_store.collection.count()
                else:
                    # 如果 count() 不可用，使用批量处理估算
                    total_docs = None
            except Exception as count_error:
                # count() 方法不可用，使用批量处理
                total_docs = None

            # 统计信息
            stats = {
                "total_docs": 0,
                "by_type": {},
                "access_counts": [],
                "timestamps": []
            }

            now = time.time()

            # [性能优化] 使用批量处理，避免全量加载
            offset = 0
            processed_count = 0

            while True:
                try:
                    # 获取一批文档（只获取 metadatas，不获取 documents）
                    batch = self.vector_store.get(
                        limit=batch_size,
                        offset=offset,
                        include=["metadatas"]  # 只获取 metadatas，不获取 documents
                    )

                    if not batch or "ids" not in batch or not batch["ids"]:
                        # 没有更多文档了
                        break

                    batch_ids = batch["ids"]
                    batch_metadatas = batch.get("metadatas", [])

                    # 处理这一批文档
                    for idx in range(len(batch_ids)):
                        metadata = batch_metadatas[idx] if idx < len(batch_metadatas) else {}
                        if metadata:
                            doc_type = metadata.get("type", "conversation")
                            stats["by_type"][doc_type] = stats["by_type"].get(doc_type, 0) + 1

                            access_count = metadata.get("access_count", 1)
                            stats["access_counts"].append(access_count)

                            doc_timestamp = metadata.get("timestamp", 0)
                            if doc_timestamp > 0:
                                age_days = (now - doc_timestamp) / 86400.0
                                stats["timestamps"].append(age_days)

                    processed_count += len(batch_ids)

                    # 如果这一批文档数量少于 batch_size，说明已经处理完所有文档
                    if len(batch_ids) < batch_size:
                        break

                    offset += batch_size

                except Exception as batch_error:
                    print(f"[RAG Stats] 批量处理错误: {batch_error}")
                    break

            # 使用处理的数量或 count() 的结果
            if total_docs is None:
                total_docs = processed_count

            stats["total_docs"] = total_docs

            # 计算统计值
            result = {
                "total_docs": stats["total_docs"],
                "by_type": stats["by_type"],
                "avg_access_count": sum(stats["access_counts"]) / len(stats["access_counts"]) if stats["access_counts"] else 0,
                "min_access_count": min(stats["access_counts"]) if stats["access_counts"] else 0,
                "max_access_count": max(stats["access_counts"]) if stats["access_counts"] else 0,
            }

            if stats["timestamps"]:
                result["oldest_doc_age_days"] = max(stats["timestamps"])
                result["newest_doc_age_days"] = min(stats["timestamps"])
            else:
                result["oldest_doc_age_days"] = 0
                result["newest_doc_age_days"] = 0

            return result

        except Exception as e:
            print(f"[RAG Stats] 获取统计信息异常: {e}")
            import traceback
            traceback.print_exc()
            return {
                "total_docs": 0,
                "by_type": {},
                "avg_access_count": 0,
                "oldest_doc_age_days": 0,
                "newest_doc_age_days": 0,
                "error": str(e)
            }

