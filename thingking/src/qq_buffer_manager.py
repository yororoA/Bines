import json
import os
import time
import threading
from pathlib import Path
from collections import defaultdict

# 假设 BUFFER_FILE_PATH
BUFFER_FILE_PATH = Path(__file__).parent / "qq_msg_buffer.json"
BUFFER_FILE_PATH_PRIVATE = Path(__file__).parent / "qq_msg_private_buffer.json"
# 假设窗口大小（消息数量）
BUFFER_SIZE_THRESHOLD = 20
# 假设时间窗口（秒，例如10分钟）
TIME_WINDOW_SECONDS = 600

class QQBufferManager:
    def __init__(self, summary_agent_instance, rag_server_client):
        self.buffer_file = BUFFER_FILE_PATH
        self.private_buffer_file = BUFFER_FILE_PATH_PRIVATE
        
        self.buffer = self._load_buffer(self.buffer_file)
        self.private_buffer = self._load_buffer(self.private_buffer_file)
        
        self.lock = threading.Lock()
        self.summary_agent = summary_agent_instance
        self.rag_client = rag_server_client # 这是一个能够发送ZMQ请求的函数或对象
        
    def _load_buffer(self, file_path):
        """加载 Buffer，如果不存在则初始化文件"""
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[QQBuffer] Load failed ({file_path.name}): {e}")
        
        # [修改] 文件不存在或读取失败，初始化并写入文件
        empty_data = {"messages": []}
        self._save_buffer(file_path, empty_data)
        return empty_data

    def _save_buffer(self, file_path, data):
        try:
            # 确保父目录存在
            if not file_path.parent.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[QQBuffer] Save failed ({file_path.name}): {e}")

    def add_message(self, content, meta):
        """
        添加消息到缓冲区
        """
        with self.lock:
            msg = {
                "content": content,
                "meta": meta,
                "received_at": time.time()
            }
            
            # [修改] 更严谨的判断逻辑
            # 只有当 group_id 存在且不为 0/None 时，才视为群聊
            # 显式转换 ensure 逻辑一致性
            gid = meta.get("group_id")
            is_group_flag = meta.get("is_group", False)
            
            # 判定：如果有 group_id (且不是0)，则是群聊；或者显式标记为 is_group
            is_group = bool(gid) or is_group_flag
            
            if is_group:
                self.buffer["messages"].append(msg)
                self._save_buffer(self.buffer_file, self.buffer)
                # print(f"[QQBuffer] Added to GROUP buffer", flush=True) # 调试用
            else:
                self.private_buffer["messages"].append(msg)
                self._save_buffer(self.private_buffer_file, self.private_buffer)
                # print(f"[QQBuffer] Added to PRIVATE buffer", flush=True) # 调试用
            
            # 检查是否应该触发处理
            if self._should_process(is_group):
                return True
        return False

    def _should_process(self, check_group=True):
        """
        check_group: True=检查群聊 buffer, False=检查私聊 buffer
        """
        if check_group:
            msgs = self.buffer.get("messages", [])
        else:
            msgs = self.private_buffer.get("messages", [])
            
        if not msgs:
            return False
            
        # [优化] 如果消息数量太少(例如只有1条)，即使超时也不处理，除非是超长文本
        # 这防止了刚上线发了一句"online"就立刻触发摘要清空
        if len(msgs) < 2 and len(msgs[0].get("content", "")) < 50:
             return False

        # 数量阈值
        if len(msgs) >= BUFFER_SIZE_THRESHOLD:
            return True
            
        # 时间阈值（基于最早一条未处理消息）
        first_msg_time = msgs[0].get("received_at", 0)
        if time.time() - first_msg_time > TIME_WINDOW_SECONDS:
            return True
            
        return False

    def process_buffer(self):
        """
        处理缓冲区中的消息：存入 RAG 并清理，但保留最近几条以防断层
        """
        with self.lock:
            # === 处理群聊 Buffer ===
            group_msgs = list(self.buffer.get("messages", []))
            if group_msgs:
                self._process_messages(group_msgs, "group")
                # [优化] 不清空全部，保留最后 N 条作为热数据，防止 RAG 延迟导致的上下文断层
                # 增大保留数量（5→12），确保最近的对话记录在 RAG 索引完成前始终可从 Buffer 读取
                keep_count = 12
                if len(group_msgs) > keep_count:
                    self.buffer["messages"] = group_msgs[-keep_count:]
                else:
                    self.buffer["messages"] = [] # 如果本来就少，还是清空吧，避免死循环处理
                
                self._save_buffer(self.buffer_file, self.buffer)

            # === 处理私聊 Buffer ===
            private_msgs = list(self.private_buffer.get("messages", []))
            if private_msgs:
                self._process_messages(private_msgs, "private")
                # [优化] 同上，保留热数据
                keep_count = 12
                if len(private_msgs) > keep_count:
                    self.private_buffer["messages"] = private_msgs[-keep_count:]
                else:
                    self.private_buffer["messages"] = []
                
                self._save_buffer(self.private_buffer_file, self.private_buffer)

    def _process_messages(self, all_messages, type_tag):
        # 1. 分组
        sessions = defaultdict(list)
        for msg in all_messages:
            meta = msg.get("meta", {})
            group_id = meta.get("group_id")
            user_id = meta.get("user_id")
            
            # 优先按群组分，私聊按用户分
            if group_id:
                key = f"group_{group_id}"
            else:
                key = f"private_{user_id}"
            
            sessions[key].append(msg)

        # 2. 处理每个会话
        for session_key, msgs in sessions.items():
            print(f"[QQBuffer] Processing {type_tag} session {session_key} with {len(msgs)} messages...")
            try:
                self._process_single_session(session_key, msgs)
            except Exception as e:
                print(f"[QQBuffer] Failed to process session {session_key}: {e}")

    def _process_single_session(self, session_key, msgs):
        if not msgs:
            return

        # 准备文本供摘要
        lines = []
        raw_docs = [] # 准备存入 QQ History RAG
        
        # 获取会话的一些元数据 (取第一条消息的即可)
        first_meta = msgs[0]["meta"]
        
        for m in msgs:
            sender = m["meta"].get("sender", "Unknown")
            content = m["content"]
            ts = m["meta"].get("timestamp")
            time_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
            lines.append(f"[{time_str}] {sender}: {content}")
            
            # 构造用于 QQ History RAG 的记录
            # 注意：QQ History RAG 我们之前是单条存的。现在既然是批量，我们可以循环存，或者 RAG Server 支持批量接口？
            # 暂时循环存，或者看看 rag_client 是否支持
            raw_docs.append({
                "content": content,
                "meta": m["meta"]
            })

        full_text = "\n".join(lines)
        
        # 生成摘要
        # 调用 SummaryAgent (假设有这个方法，或者我们需要构造一个 prompt)
        # 这里我们模拟一个 prompt，调用 summary_agent 的 LLM
        # 注意：summary_agent 可能没有直接暴露 generate_summary，我们需要看 agents.py
        
        summary = self._generate_summary(session_key, full_text)
        
        if summary:
            print(f"[QQBuffer] Generated summary for {session_key}: {summary[:50]}...")
            
            # 存入 摘要 RAG (summary_buffer)
            # 使用 rag_client 发送
            self.rag_client.add_to_summary_buffer(
                content=f"【QQ会话摘要 - {session_key}】\n{summary}",
                meta={
                    "timestamp": time.time(),
                    "source": "qq_buffer",
                    "session_key": session_key
                }
            )
            
        # 存入 QQ History RAG (Raw Messages)
        for doc in raw_docs:
            self.rag_client.add_qq_log(
                content=doc["content"],
                meta=doc["meta"]
            )
        print(f"[QQBuffer] Saved {len(raw_docs)} raw messages to QQ History RAG.")

    def _generate_summary(self, session_key, text):
        """使用 SummaryAgent 生成摘要"""
        try:
            if hasattr(self.summary_agent, 'summarize_content'):
                return self.summary_agent.summarize_content(text)
            else:
                print(f"[QQBuffer] SummaryAgent missing summarize_content method")
                return text[:200] + "..."
        except Exception as e:
            print(f"[QQBuffer] Summary generation failed: {e}")
            return f"聊天记录片段 ({len(text)} chars)"

