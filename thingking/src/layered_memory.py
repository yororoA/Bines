import json
import os
import threading
import time
import copy
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from openai import OpenAI
from rag_memory import RAGMemory
from concurrent.futures import ThreadPoolExecutor
from config import (
    DEEPSEEK_SUMMARY_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_SUMMARY_MODEL,
    require_env,
)

class PermanentMemory:
    """永久记忆: 存储用户画像、核心设定、事实性知识"""
    def __init__(self, filepath="memory_permanent.json"):
        self.filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filepath)
        self.lock = threading.Lock()
        self.data = {
            "user_profile": {},   # 用户名称、喜好等
            "core_rules": [],     # 核心行为准则
            "facts": []           # 固定的世界观事实
        }
        self.load()

    def load(self):
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    lock_fd = _acquire_file_lock(self.filepath + ".lock")
                    try:
                        with open(self.filepath, 'r', encoding='utf-8') as f:
                            self.data = json.load(f)
                    finally:
                        _release_file_lock(lock_fd, self.filepath + ".lock")
                except Exception as e:
                    print(f"[PermanentMemory] Load error: {e}")
    
    def save(self):
        """
        [原子写入] 使用临时文件+原子替换，避免写入过程中崩溃导致数据丢失
        """
        with self.lock:
            try:
                lock_fd = _acquire_file_lock(self.filepath + ".lock")
                # 原子写入：先写入临时文件，成功后再替换原文件
                temp_filepath = self.filepath + ".tmp"
                try:
                    with open(temp_filepath, 'w', encoding='utf-8') as f:
                        json.dump(self.data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())  # 强制刷新到磁盘
                    # 原子替换：使用 os.replace 确保原子性（Windows/Linux 都支持）
                    os.replace(temp_filepath, self.filepath)
                finally:
                    _release_file_lock(lock_fd, self.filepath + ".lock")
            except Exception as e:
                print(f"[PermanentMemory] Save error: {e}")
                # 清理临时文件（如果存在）
                temp_filepath = self.filepath + ".tmp"
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except:
                        pass

    def get_context_str(self):
        with self.lock:
            lines = []
            if self.data.get("user_profile"):
                lines.append("User Profile: " + json.dumps(self.data["user_profile"], ensure_ascii=False))
            if self.data.get("core_rules"):
                rules_str = "\n".join([f"{i+1}. {rule}" for i, rule in enumerate(self.data["core_rules"])])
                lines.append(f"Core Rules:\n{rules_str}")
            if self.data.get("facts"):
                lines.append("Facts: " + "; ".join(self.data["facts"]))
            return "\n".join(lines)


def _flat_to_rounds(flat_msgs):
    """将扁平消息列表转为轮次列表：每轮为 [user, assistant] 或 [assistant]。"""
    if not flat_msgs:
        return []
    rounds = []
    i = 0
    while i < len(flat_msgs):
        msg = flat_msgs[i]
        role = (msg.get("role") or "").lower()
        if i + 1 < len(flat_msgs):
            next_msg = flat_msgs[i + 1]
            next_role = (next_msg.get("role") or "").lower()
            if role == "user" and next_role == "assistant":
                rounds.append([msg, next_msg])
                i += 2
                continue
        rounds.append([msg])
        i += 1
    return rounds


def _acquire_file_lock(lock_path: str, timeout: float = 5.0, poll_interval: float = 0.05):
    """
    简易跨进程文件锁：
    - 通过创建 lock 文件 (O_CREAT | O_EXCL) 获得独占锁
    - 同一进程内仍依赖 threading.Lock/RLock；本锁只用于多进程间协调
    - 若锁长期存在（可能是崩溃遗留），超时后会尝试删除并重试
    """
    start = time.time()
    last_error = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(time.time()).encode("utf-8"))
            except Exception:
                # 写入失败不影响锁本身
                pass
            return fd
        except FileExistsError as e:
            last_error = e
            if time.time() - start > timeout:
                # 认为是陈旧锁，尝试删除后重试一次新的周期
                try:
                    os.remove(lock_path)
                    start = time.time()
                    continue
                except OSError:
                    # 无法删除则继续等待
                    start = time.time()
            time.sleep(poll_interval)
        except OSError as e:
            # 其他错误直接返回 None，调用方可选择退化为无锁
            last_error = e
            break
    print(f"[FileLock] Failed to acquire lock {lock_path}: {last_error}")
    return None


def _release_file_lock(fd, lock_path: str):
    """释放由 _acquire_file_lock 获取的文件锁。"""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _clamp_timestamp(ts: float, now: float | None = None) -> float:
    """将时间戳限制在合理范围，避免未来时间或过久过去导致 RAG time_decay 失效。"""
    if now is None:
        now = time.time()
    max_future = 60.0
    max_past_days = 10 * 365
    if ts > now + max_future:
        return now
    if ts < now - max_past_days * 86400:
        return now - max_past_days * 86400
    return ts


def _is_system_event_content(content: str) -> bool:
    return (content or "").strip().startswith("[System Event:")


def _sanitize_timestamps_in_history(history: list) -> None:
    """就地校正 history 中每条消息的 timestamp，避免未来/过久过去导致 RAG 时间衰减失效。"""
    now = time.time()
    for round_msgs in history:
        if not isinstance(round_msgs, list):
            continue
        for m in round_msgs:
            if not isinstance(m, dict):
                continue
            ts = m.get("timestamp")
            if ts is not None and isinstance(ts, (int, float)):
                m["timestamp"] = _clamp_timestamp(float(ts), now)


class ShortTermMemory:
    """短期记忆: 按轮存储，每轮为子列表（[user, assistant] 或 [assistant]），固定轮数窗口"""
    # 同一时间窗（秒）内触发的多个系统事件合并为一轮，避免重复
    SYSTEM_EVENT_MERGE_WINDOW_SEC = 60

    def __init__(self, filepath="memory_short.json", limit=10):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self._main_filepath = os.path.join(base_dir, filepath)
        self._temp_filepath = os.path.join(base_dir, "memory_short_temp.json")
        self.filepath = self._main_filepath
        self.limit = limit  # 轮数上限
        self.lock = threading.Lock()
        self.history = []  # List[List[dict]]，每轮为 1 或 2 条消息
        self.load()

    def load(self):
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    lock_fd = _acquire_file_lock(self.filepath + ".lock")
                    try:
                        with open(self.filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    finally:
                        _release_file_lock(lock_fd, self.filepath + ".lock")
                        if isinstance(data, list):
                            # 新格式：list of rounds（每轮为 list）
                            if data and isinstance(data[0], list):
                                self.history = data[-self.limit:]
                            else:
                                # 旧格式：扁平消息列表，转为轮次
                                self.history = _flat_to_rounds(data)[-self.limit:]
                        elif isinstance(data, dict) and "messages" in data:
                            msgs = data["messages"]
                            if isinstance(msgs, list):
                                self.history = _flat_to_rounds(msgs)[-self.limit:]
                            else:
                                self.history = []
                        else:
                            self.history = []
                    # 加载后校正时间戳：与系统时间偏差过大会导致 RAG time_decay 失效
                    _sanitize_timestamps_in_history(self.history)
                except Exception as e:
                    print(f"[ShortTermMemory] Load error: {e}")
                    self.history = []

    def save(self):
        """
        [原子写入] 使用临时文件+原子替换，避免写入过程中崩溃导致数据丢失
        """
        with self.lock:
            try:
                lock_fd = _acquire_file_lock(self.filepath + ".lock")
                # 原子写入：先写入临时文件，成功后再替换原文件
                temp_filepath = self.filepath + ".tmp"
                try:
                    with open(temp_filepath, 'w', encoding='utf-8') as f:
                        json.dump(self.history, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())  # 强制刷新到磁盘
                    # 原子替换：使用 os.replace 确保原子性（Windows/Linux 都支持）
                    os.replace(temp_filepath, self.filepath)
                finally:
                    _release_file_lock(lock_fd, self.filepath + ".lock")
            except Exception as e:
                print(f"[ShortTermMemory] Save error: {e}")
                # 清理临时文件（如果存在）
                temp_filepath = self.filepath + ".tmp"
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except:
                        pass

    def add(self, role, content, timestamp=None):
        """添加一条消息作为单条一轮（向后兼容）。"""
        if timestamp is None:
            timestamp = time.time()
        self.add_round([{"role": role, "content": content, "timestamp": timestamp}])

    def add_and_get_dropped(self, role, content, timestamp=None):
        """新增一条消息作为单条一轮，并返回因超出窗口而被挤掉的旧消息（扁平列表）。"""
        if timestamp is None:
            timestamp = time.time()
        dropped_rounds = self.add_round([{"role": role, "content": content, "timestamp": timestamp}])
        return [m for r in dropped_rounds for m in r]

    def get_messages(self):
        """返回扁平消息列表（展平轮次），供下游兼容使用。"""
        with self.lock:
            return [msg for round_msgs in self.history for msg in round_msgs]

    def get_round_count(self):
        """返回当前轮数。"""
        with self.lock:
            return len(self.history)

    def switch_to_temp_and_clear_main(self):
        """
        达到轮数上限时：暂停使用主文档，切换到临时文档。
        将当前主文档内容（轮次列表）返回供摘要使用，清空内存并改为写入临时文件。
        调用后新产生的记忆会写入 memory_short_temp.json。
        """
        with self.lock:
            to_summarize = [list(r) for r in self.history]  # 轮次列表的副本
            self.filepath = self._temp_filepath
            self.history = []
            try:
                lock_fd = _acquire_file_lock(self._temp_filepath + ".lock")
                try:
                    with open(self._temp_filepath, 'w', encoding='utf-8') as f:
                        json.dump([], f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                finally:
                    _release_file_lock(lock_fd, self._temp_filepath + ".lock")
            except Exception as e:
                print(f"[ShortTermMemory] switch_to_temp write empty temp error: {e}")
            return to_summarize

    def replace_main_from_temp_and_switch_back(self):
        """
        摘要写入 RAG 成功后：将临时文件内容覆盖主文档，并切回使用主文档。
        """
        with self.lock:
            try:
                if os.path.exists(self._temp_filepath):
                    lock_fd = _acquire_file_lock(self._temp_filepath + ".lock")
                    try:
                        with open(self._temp_filepath, 'r', encoding='utf-8') as f:
                            temp_data = json.load(f)
                    finally:
                        _release_file_lock(lock_fd, self._temp_filepath + ".lock")
                    if not isinstance(temp_data, list):
                        temp_data = []
                else:
                    temp_data = []
                # 原子写入主文档
                temp_write = self._main_filepath + ".tmp"
                lock_fd_main = _acquire_file_lock(self._main_filepath + ".lock")
                try:
                    with open(temp_write, 'w', encoding='utf-8') as f:
                        json.dump(temp_data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_write, self._main_filepath)
                finally:
                    _release_file_lock(lock_fd_main, self._main_filepath + ".lock")
                self.filepath = self._main_filepath
                # 兼容：若 temp 为扁平列表则转为轮次
                if temp_data and isinstance(temp_data[0], list):
                    self.history = temp_data[:]
                else:
                    self.history = _flat_to_rounds(temp_data if isinstance(temp_data, list) else [])[-self.limit:]
                try:
                    os.remove(self._temp_filepath)
                except Exception as e:
                    print(f"[ShortTermMemory] remove temp file error: {e}")
                print(f"[ShortTermMemory] 已从临时文件恢复主文档，共 {len(self.history)} 轮")
            except Exception as e:
                print(f"[ShortTermMemory] replace_main_from_temp error: {e}")
                import traceback
                traceback.print_exc()
                self.filepath = self._main_filepath
                if os.path.exists(self._temp_filepath):
                    try:
                        with open(self._temp_filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        self.history = data if isinstance(data, list) else []
                    except Exception:
                        self.history = []

    def clear_and_persist(self):
        """清空短期记忆并写回主文档，用于摘要失败等降级场景，防止坏数据反复重试。"""
        with self.lock:
            self.history = []
            self.filepath = self._main_filepath
            self._save()

    def add_round(self, round_messages):
        """
        追加一轮（子列表），并返回因超出窗口而被挤掉的旧轮次。
        round_messages: List[{"role", "content", "timestamp"}]，长度为 1 或 2（仅 assistant，或 user+assistant）
        同时间窗内的多个系统事件会合并为一轮，避免 16:43 同时出现 [User Online] 与 [Bored] 等重复。
        """
        if not round_messages:
            return []
        current_time = time.time()
        processed = []
        for msg in round_messages:
            m = dict(msg)
            if "timestamp" not in m:
                m["timestamp"] = current_time
            m["timestamp"] = _clamp_timestamp(m["timestamp"], current_time)
            processed.append(m)
        dropped = []
        with self.lock:
            # 若本轮为单条 user 且为系统事件，且上一轮也是同时间窗内的系统事件，则合并为一轮
            if (
                len(processed) == 1
                and processed[0].get("role") == "user"
                and _is_system_event_content(processed[0].get("content") or "")
            ):
                new_ts = processed[0]["timestamp"]
                if self.history:
                    last_round = self.history[-1]
                    if (
                        isinstance(last_round, list)
                        and len(last_round) == 1
                        and last_round[0].get("role") == "user"
                        and _is_system_event_content(last_round[0].get("content") or "")
                    ):
                        last_ts = last_round[0].get("timestamp")
                        if last_ts is not None and abs(new_ts - last_ts) <= self.SYSTEM_EVENT_MERGE_WINDOW_SEC:
                            # 合并：将新事件描述追加到上一轮内容，保留上一轮时间戳
                            last_content = (last_round[0].get("content") or "").strip()
                            new_content = (processed[0].get("content") or "").strip()
                            if new_content and new_content not in last_content:
                                last_round[0]["content"] = last_content + "\n" + new_content
                            self.save()
                            return []
                # 不满足合并条件，正常追加
            self.history.append(processed)
            if len(self.history) > self.limit:
                overflow = len(self.history) - self.limit
                dropped = self.history[:overflow]
                self.history = self.history[overflow:]
        self.save()
        return dropped

    def add_many(self, interactions):
        """
        一次性追加一轮（将 interactions 视为一轮），并返回因超出窗口而被挤掉的旧轮次。
        兼容旧调用：interactions 为 [user_msg, assistant_msg] 时作为一轮追加。
        """
        return self.add_round(interactions)

    def get_last(self, n=None):
        """获取最近 n 条消息（展平后）；n 为 None 时返回全部消息。"""
        flat = self.get_messages()
        if n is None or n >= len(flat):
            return flat[:]
        return flat[-n:]
    
    def get_oldest_timestamp(self):
        """
        获取短期记忆中最旧消息的时间戳（首轮首条）。
        
        Returns:
            float: 时间戳（Unix时间戳），如果无法获取则返回 None
        """
        with self.lock:
            if not self.history:
                return None
            first_round = self.history[0]
            if not first_round:
                return None
            msg = first_round[0]
            timestamp = msg.get("timestamp")
            if timestamp is not None and isinstance(timestamp, (int, float)):
                return float(timestamp)
            # Fallback: 从文本解析
            import re
            from datetime import datetime
            for m in first_round:
                content = m.get("content", "")
                if not content:
                    continue
                
                # 解析时间戳格式：[YYYY/MM/DD HH:MM]: [content]
                # 或者：[YYYY/MM/DD]: [content]
                # 尝试匹配 [YYYY/MM/DD HH:MM] 或 [YYYY/MM/DD]
                match = re.search(r'\[(\d{4}[/-]\d{1,2}[/-]\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?\]', content)
                if match:
                    date_str = match.group(1)
                    hour = match.group(2)
                    minute = match.group(3)
                    
                    try:
                        # 支持 YYYY/MM/DD 和 YYYY-MM-DD 两种格式
                        date_format = "%Y/%m/%d" if "/" in date_str else "%Y-%m-%d"
                        
                        if hour and minute:
                            # 有具体时间
                            datetime_str = f"{date_str} {hour}:{minute}"
                            datetime_format = f"{date_format} %H:%M"
                            dt = datetime.strptime(datetime_str, datetime_format)
                        else:
                            # 只有日期，使用当天的 00:00
                            dt = datetime.strptime(date_str, date_format)
                        
                        # 转换为 Unix 时间戳
                        timestamp = dt.timestamp()
                        return timestamp
                    except ValueError:
                        # 解析失败，继续查找下一条消息
                        continue
            
            # 如果所有消息都无法获取时间戳，返回 None
            return None


class DynamicMemory:
    """动态状态记忆：地点、好感度、物品等会变化的信息"""
    def __init__(self, filepath="memory_dynamic.json"):
        self.filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filepath)
        # 使用 RLock：update_status 持锁时会调用 save()，save() 内也需获取同一把锁，避免死锁
        self.lock = threading.RLock()
        # relationship_score 用于内部数值计算，relationship_level 用于提示词展示
        # 初始化默认状态结构，确保所有字段都有默认值
        self.state = {
            "current_time": "",
            "current_location": "",
            "npc_state": {
                "name": "",
                "attire": "",
                "visual_module_status": "",
                "current_activity": ""
            },
            "relationship_level": "",
            "relationship_score": 0,
            "relationship_distribution": {},  # 用于保存当前模糊权重（可选）
            "inventory": [],
            "active_quest": "",
            "memory_highlights": []
        }
        self.load()
        # 确保加载后所有字段都存在（向后兼容）
        self._ensure_fields()

    def load(self):
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    lock_fd = _acquire_file_lock(self.filepath + ".lock")
                    try:
                        with open(self.filepath, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    finally:
                        _release_file_lock(lock_fd, self.filepath + ".lock")
                    if isinstance(data, dict):
                        # 移除 banned_topics 字段（如果存在）
                        if "banned_topics" in data:
                            del data["banned_topics"]
                        # 移除已废弃的 user_state 字段（含子内容）
                        if "user_state" in data:
                            del data["user_state"]
                        for k in ("user_name", "user_appearance", "user_mood"):
                            if k in data:
                                del data[k]
                        # 深度合并，确保嵌套字典（如 npc_state）正确更新
                        self._deep_update(self.state, data)
                except Exception as e:
                    print(f"[DynamicMemory] Load error: {e}")
    
    def _deep_update(self, base_dict, update_dict):
        """深度更新字典，支持嵌套结构"""
        for key, value in update_dict.items():
            if key in base_dict and isinstance(base_dict[key], dict) and isinstance(value, dict):
                self._deep_update(base_dict[key], value)
            else:
                base_dict[key] = value
    
    def _ensure_fields(self):
        """确保所有必需字段都存在，用于向后兼容"""
        defaults = {
            "current_time": "",
            "npc_state": {
                "name": "",
                "attire": "",
                "visual_module_status": "",
                "current_activity": ""
            },
            "important_thing": "",
            "memory_highlights": []
        }
        for key, default_value in defaults.items():
            if key not in self.state:
                self.state[key] = default_value
            elif isinstance(default_value, dict) and isinstance(self.state[key], dict):
                # 确保嵌套字典的字段都存在
                for sub_key, sub_default in default_value.items():
                    if sub_key not in self.state[key]:
                        self.state[key][sub_key] = sub_default

    def save(self):
        with self.lock:
            try:
                # 自动更新当前时间戳（ISO格式，带时区）
                now = datetime.now()
                # 获取本地时区偏移
                try:
                    from datetime import timezone, timedelta
                    # 尝试获取系统时区偏移
                    if now.utcoffset() is not None:
                        offset_seconds = now.utcoffset().total_seconds()
                        offset_hours = int(offset_seconds / 3600)
                        tz = timezone(timedelta(hours=offset_hours))
                        current_time_str = now.astimezone(tz).isoformat()
                    else:
                        # 如果无法获取时区，使用默认中国时区
                        tz = timezone(timedelta(hours=8))
                        current_time_str = now.astimezone(tz).isoformat()
                except:
                    # 如果时区处理失败，使用简单格式（默认+08:00）
                    current_time_str = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
                self.state["current_time"] = current_time_str
                
                # [原子写入] 使用临时文件+原子替换，避免写入过程中崩溃导致数据丢失
                temp_filepath = self.filepath + ".tmp"
                lock_fd = _acquire_file_lock(self.filepath + ".lock")
                try:
                    with open(temp_filepath, 'w', encoding='utf-8') as f:
                        json.dump(self.state, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())  # 强制刷新到磁盘
                    # 原子替换：使用 os.replace 确保原子性（Windows/Linux 都支持）
                    os.replace(temp_filepath, self.filepath)
                finally:
                    _release_file_lock(lock_fd, self.filepath + ".lock")
            except Exception as e:
                print(f"[DynamicMemory] Save error: {e}")
                # 清理临时文件（如果存在）
                temp_filepath = self.filepath + ".tmp"
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except:
                        pass

    def to_prompt_str(self):
        """
        转换为注入到 System Prompt 的文本
        
        [并发安全] 创建状态的深拷贝快照，确保在 LLM 生成过程中状态变化不会影响已传递的快照。
        这样可以避免"读取-使用"竞态条件，确保 LLM 基于一致的状态快照进行推理。
        """
        with self.lock:
            # [并发安全] 创建状态的深拷贝，确保返回的是快照而不是引用
            # 这样即使后续状态被其他线程更新，这个快照也不会改变
            state_snapshot = copy.deepcopy(self.state)
        
        # 在锁外构建字符串，减少锁持有时间
        lines = ["--- DYNAMIC STATUS ---"]
        for k, v in state_snapshot.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)

    def to_json_str(self):
        """
        返回当前状态的 JSON 字符串（与 memory_dynamic.json 结构一致），
        供副工具模型根据完整字段结构判断需要更新哪些字段。
        """
        with self.lock:
            state_snapshot = copy.deepcopy(self.state)
        return json.dumps(state_snapshot, ensure_ascii=False, indent=2)


class LayeredMemorySystem:
    """
    五层记忆系统集成
    """
    # 前 N 轮使用完整 TOOLS 规则，超过后使用精简版以节省 Token
    _FULL_RULES_MAX_ROUNDS = 5

    def __init__(self):
        # 永久层
        self.permanent = PermanentMemory()
        # 短期层：按轮存储，每轮为子列表（user+assistant 或 仅 assistant），limit=轮数（25 轮）
        self.short_term = ShortTermMemory(limit=25)
        # 动态状态层
        self.dynamic = DynamicMemory()
        # 中长期层 (共用向量数据库)
        self.vector_db = RAGMemory(persist_directory="chroma_db_layered")
        
        # [职责分离] 摘要模型客户端（惰性初始化）
        # 职责范围：
        # 1. 对话摘要生成（_summarize_and_store_episode）
        # 2. 重要性权重打分（_score_importance）- 用于 GC 策略
        # 3. 信息密度打分（_score_information_density）- 用于判断是否存入 RAG
        # 4. Query Rewrite（_rewrite_query_for_retrieval）- 处理模糊指代
        # 
        # 注意：这些任务与主模型（对话生成）和外援大模型（复杂推理、工具调用）的职能无关，
        #       属于辅助性任务，统一由摘要模型负责，避免职责混乱。
        self._summary_client = None
        # 摘要模型使用的 DeepSeek API Key（可被环境变量覆盖）
        resolved_summary_key = DEEPSEEK_SUMMARY_API_KEY or os.environ.get("DEEPSEEK_SUMMARY_API_KEY")
        self._summary_api_key = require_env("DEEPSEEK_SUMMARY_API_KEY", resolved_summary_key)
        # 写日记用副 RP 模型（与主模型性格一致、不占用主模型；使用独立摘要/日记端点）
        self._diary_rp_client = None
        # [性能优化] 异步执行摘要生成的线程池
        self._summary_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="SummaryWorker")
        # 一天分割点 04:00（与 rag_server_core.DAY_START_HOUR 一致）
        self._day_start_hour = 4
        # [主模型可见性] 摘要/缓冲/日记处理期间，主模型仍能使用原始内容，直到下一阶段产物就绪
        # 正在被摘要的原始对话暂存于此，get_full_context_messages 会合并进上下文；摘要写入 Buffer 后清空
        self._pending_summary_messages = []
        self._pending_lock = threading.Lock()
        # [断层修复] 刚入库的摘要：flush 后接下来几轮强制注入上下文，避免检索不到导致对话断层
        self._last_flushed_summary = None  # (summary_text_str,) 或 None
        self._last_flushed_summary_remaining = 0  # 剩余注入次数，0 表示不再注入
        # 副工具模型（reasoner）：仅挂载动态记忆工具，由 handle_zmq 注入
        self._dynamic_memory_agent = None
        # 副工具模型更新动态记忆在独立线程池执行，不阻塞摘要流程与主模型读动态记忆
        self._dynamic_memory_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="DynamicMemoryWorker")

        # 每次上线：短期记忆摘要写入 Buffer RAG；非当天的 Buffer 按天汇总为日记（异步）
        self._run_startup_flush_if_needed()
        self._summary_executor.submit(self._run_startup_flush_buffer_to_diary)

    @staticmethod
    def _seconds_until_next_day_start(hour=4):
        """距离下一次当天分割点（默认 04:00）的秒数。"""
        now = datetime.now()
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        return max(0, (next_run - now).total_seconds())

    def _rag_garbage_collect_loop(self):
        """后台线程：定期执行 RAG 记忆库 GC（凌晨 0:00-2:00，每 24 小时）。"""
        print("[RAG GC] 记忆库自动清理线程已启动", flush=True)
        time.sleep(300)  # 首次等待 5 分钟
        last_gc_time = None
        gc_interval_hours = 24
        while True:
            try:
                now = datetime.now()
                should_run = False
                if last_gc_time is None:
                    if 0 <= now.hour < 2:
                        should_run = True
                        print(f"[RAG GC] 首次执行：当前时间 {now.strftime('%H:%M')}，执行清理", flush=True)
                else:
                    time_since = (now - last_gc_time).total_seconds() / 3600
                    if time_since >= gc_interval_hours and 0 <= now.hour < 2:
                        should_run = True
                        print(f"[RAG GC] 定期执行：距离上次 {time_since:.1f} 小时，当前 {now.strftime('%H:%M')}", flush=True)
                if should_run:
                    try:
                        stats = self.vector_db.get_stats()
                        total_docs = stats.get("total_docs", 0)
                        if total_docs > 0:
                            preview = self.vector_db.garbage_collect(
                                min_age_days=90, max_access_count=2, dry_run=True
                            )
                            if 0 < preview["deleted_count"] < total_docs * 0.5:
                                result = self.vector_db.garbage_collect(
                                    min_age_days=90, max_access_count=2, dry_run=False
                                )
                                print(f"[RAG GC] 清理完成: 删除 {result['deleted_count']} 条", flush=True)
                                last_gc_time = now
                        else:
                            print("[RAG GC] 记忆库为空，跳过清理", flush=True)
                    except Exception as e:
                        print(f"[RAG GC] 清理出错: {e}", flush=True)
                        traceback.print_exc()
                time.sleep(3600)
            except Exception as e:
                print(f"[RAG GC] 线程错误: {e}", flush=True)
                traceback.print_exc()
                time.sleep(3600)

    def _diary_flush_loop(self):
        """后台线程：每天 04:00 执行 Buffer→Diary 日记归纳。"""
        print("[DiaryFlush] 日记归纳定时线程已启动，将在每天 04:00 执行", flush=True)
        while True:
            try:
                secs = self._seconds_until_next_day_start(self._day_start_hour)
                if secs > 60:
                    print(f"[DiaryFlush] 下次归纳在 {secs/3600:.1f} 小时后", flush=True)
                time.sleep(secs)
                print("[DiaryFlush] 到达固定时间，执行 Buffer→Diary 日记归纳", flush=True)
                self.flush_buffer_to_diary()
                time.sleep(60)
            except Exception as e:
                print(f"[DiaryFlush] 定时线程错误: {e}", flush=True)
                traceback.print_exc()
                time.sleep(3600)

    def start_maintenance_services(self):
        """启动 RAG GC 与日记归纳两个后台维护线程（由 handle_zmq 在初始化后调用）。"""
        threading.Thread(target=self._rag_garbage_collect_loop, daemon=True).start()
        threading.Thread(target=self._diary_flush_loop, daemon=True).start()
        print("[Memory] RAG GC 与日记归纳维护线程已启动", flush=True)

    def set_dynamic_memory_agent(self, agent):
        """设置副工具模型（仅挂载动态记忆工具），用于摘要写入 buffer RAG 后更新 memory_dynamic。"""
        self._dynamic_memory_agent = agent

    # =========================================================================
    # [新增] 辅助方法：判断是否为垃圾文本
    # =========================================================================
    def _is_garbage(self, text):
        """简单判断是否为无意义的短文本，避免污染向量库"""
        if len(text) < 5: return True # 太短
        
        # 常见无营养对话
        garbage_phrases = [
            "在吗", "你好", "hello", "hi", "测试", "test", 
            "晚安", "早安", "好的", "收到", "嗯嗯", "哦哦",
            "吃了吗", "去洗澡", "88", "拜拜"
        ]
        
        # 如果整段话只是寒暄
        clean_text = text.strip().lower()
        if clean_text in garbage_phrases:
            return True
            
        return False

    # =========================================================================
    # [注] 旧版 0-10 分制的 _score_information_density 已移除，
    #      统一使用下方 0-100 分制版本，阈值见 store_raw_conversation()。
    # =========================================================================
    
    def _get_recent_qq_buffer(self, group_id=None, user_id=None):
        """
        [新增] 直接读取 QQ 缓冲文件，获取尚未写入 RAG 的最新消息
        """
        buffer_msgs = []
        
        # 定义文件路径 (需与 qq_buffer_manager.py 一致)
        # 注意：这里假设 layered_memory.py 和 qq_buffer_manager.py 在同一目录
        base_dir = Path(__file__).parent
        group_file = base_dir / "qq_msg_buffer.json"
        private_file = base_dir / "qq_msg_private_buffer.json"

        target_files = []
        if group_id:
            target_files.append(group_file)
        if user_id and not group_id: # 只有在非群聊模式下才重点读私聊buffer，或者是全部读取
            target_files.append(private_file)
            
        for fpath in target_files:
            if fpath.exists():
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        msgs = data.get("messages", [])
                        
                        # 筛选匹配当前会话的消息
                        for m in msgs:
                            meta = m.get("meta", {})
                            
                            # 筛选逻辑：
                            # 如果是群聊，meta['group_id'] 必须匹配
                            if group_id:
                                # 注意 json 里可能是 int，传入可能是 str，转 str 比较
                                if str(meta.get("group_id")) == str(group_id):
                                    buffer_msgs.append(m)
                            # 如果是私聊，meta['user_id'] 必须匹配
                            elif user_id:
                                if str(meta.get("user_id")) == str(user_id):
                                    buffer_msgs.append(m)
                except Exception as e:
                    print(f"[Memory] Read QQ buffer error: {e}")

        # 按时间戳排序
        buffer_msgs.sort(key=lambda x: x["meta"].get("timestamp", 0))
        return buffer_msgs
    
    def _is_system_event(self, text):
        """
        判断是否为系统事件或屏幕监控触发的输入，这些不应该写入记忆
        包括：[System Event: ...]、[Screen Monitor] 等标记
        """
        if not text:
            return True
        
        text_lower = text.lower()
        # 检测系统事件标记
        system_markers = [
            "[system event:",
            "[system event :",
            "system event:",
            "[screen monitor]",
            "[screen monitor:",
            "screen monitor:",
            "user hasn't spoken",
            "用户长时间未说话",
            "长时间沉默",
        ]
        
        for marker in system_markers:
            if marker in text_lower:
                return True
        
        return False

    def get_full_context_messages(self, current_user_input, qq_context_data=None):
        """
        构建发送给 LLM 的完整消息列表
        
        Args:
            current_user_input: 当前用户输入
            qq_context_data: 可选，包含 QQ 上下文信息的字典 (group_id, user_id)
        """
        messages = []

        # 如果是 QQ 消息，尝试检索最近的历史记录作为背景补充
        qq_history_block = ""
        if qq_context_data:
            try:
                group_id = qq_context_data.get("group_id")
                user_id = qq_context_data.get("user_id")
                
                all_logs = []

                # 1. [新增] 从 RAG 获取旧历史 (按时间倒序)
                # 依赖 rag_memory 中已实现的 get_latest_qq_logs
                if hasattr(self.vector_db, 'get_latest_qq_logs'):
                    rag_logs = self.vector_db.get_latest_qq_logs(group_id, user_id, limit=10)
                    all_logs.extend(rag_logs)
                else:
                    # 保底：若未实现，使用相似度检索
                    hist_docs = self.vector_db.search_qq_history(
                        query=current_user_input, 
                        k=10, 
                        group_id=group_id, 
                        user_id=user_id if not group_id else None
                    )
                    # 转换相似度检索结果格式
                    for doc, meta, _ in hist_docs:
                        all_logs.append({"content": doc, "meta": meta})

                # 2. [新增] 从 JSON Buffer 获取最新消息 (便签纸上的数据)
                buffer_msgs = self._get_recent_qq_buffer(group_id, user_id)
                
                # 将 buffer 消息转换为统一格式
                for m in buffer_msgs:
                    all_logs.append({
                        "content": m["content"],
                        "meta": m["meta"]
                    })

                # 3. 去重与排序
                # 简单去重：根据 timestamp 和 content
                unique_logs = {}
                for log in all_logs:
                    key = f"{log['meta'].get('timestamp')}-{log['content']}"
                    unique_logs[key] = log
                
                final_logs = list(unique_logs.values())
                # 按时间正序排列
                final_logs.sort(key=lambda x: x["meta"].get("timestamp", 0))
                
                # [修复] 过滤掉当前正在处理的消息（它已作为 current user message 出现在上下文中，避免重复）
                if current_user_input:
                    final_logs = [lg for lg in final_logs if lg["content"] != current_user_input]
                
                # 截取最近 N 条（增大到 20 条以提供更充分的历史上下文）
                final_logs = final_logs[-20:]

                if final_logs:
                    lines = []
                    for item in final_logs:
                        content = item["content"]
                        sender = item["meta"].get("sender", "Unknown")
                        ts = item["meta"].get("timestamp", 0)
                        
                        # 直接使用全局 time，移除内部 import time
                        time_str = time.strftime("%H:%M:%S", time.localtime(ts))
                        
                        lines.append(f"[{time_str}] {sender}: {content}")
                    
                    qq_history_block = "--- QQ Context (Recent History) ---\n" + "\n".join(lines) + "\n---------------------------------------\n"
            except Exception as e:
                print(f"[LayeredMemory] Fetch QQ history failed: {e}")
                # 打印堆栈以便调试，但不要让错误中断流程
                # traceback.print_exc() 

        # [Layer 5] 永久记忆 + 动态记忆 -> System Prompt（主模型对话会收到）
        perm_context = self.permanent.get_context_str()
        dynamic_context = self.dynamic.to_prompt_str()
        # [Token 节省] 前 N 轮注入完整工具规则，后续用精简版，依靠上下文惯性
        num_rounds = len(self.short_term.history)
        use_full_tools = num_rounds <= self.__class__._FULL_RULES_MAX_ROUNDS
        if use_full_tools:
            tools_block = (
                "2. TOOLS (CRITICAL - READ CAREFULLY): You can ONLY use these tools: `call_tool_agent` and `call_summary_agent`. You CANNOT directly call other tools like get_visual_info, get_screen_info, etc.\n"
                "   - **MEMORY & HISTORY (CRITICAL - DO NOT USE TOOLS FOR THIS):**\n"
                "     * Your context ALREADY contains all available memory and history information:\n"
                "       - <memory_recall> tags = RAG-retrieved past conversations and diary entries\n"
                "       - '--- QQ Context (Recent History) ---' = recent QQ chat logs\n"
                "       - The message history below = your short-term memory (last ~30 exchanges)\n"
                "     * When the user asks about past conversations, memories, chat history, or 'what we talked about before':\n"
                "       -> ANSWER DIRECTLY from your context. DO NOT call call_tool_agent.\n"
                "     * get_screen_info is for CURRENT screen content ONLY, NEVER for retrieving past conversations or history.\n"
                "   - **call_tool_agent** (MANDATORY for REAL-WORLD operations): Use this tool for operations that interact with the external environment:\n"
                "     * Visual operations: IF user asks \"what is this\", \"look at this\", \"what do you see\", \"am I wearing glasses\" or implies visual context -> CALL `call_tool_agent` with task_description like \"用户要求查看/分析视觉信息，需要使用get_visual_info工具\"\n"
                "     * Screen operations: IF user asks \"look at my screen\", \"what am I doing now\" (CURRENT screen only) -> CALL `call_tool_agent` with task_description like \"用户要求查看当前屏幕内容，需要使用get_screen_info工具\"\n"
                "     * Application operations: IF user asks to open apps, files -> CALL `call_tool_agent` with task_description like \"用户要求打开应用程序/文件\"\n"
                "     * Music operations: IF user asks to play/pause/control music -> CALL `call_tool_agent` with task_description like \"用户要求控制音乐播放\"\n"
                "     * Mouse/keyboard operations: IF user asks to click, type, or perform any automation -> CALL `call_tool_agent` with task_description describing the automation task\n"
                "     * Other REAL-WORLD operations (browser search, QQ messaging, etc.) -> CALL `call_tool_agent`\n"
                "     * NEVER use call_tool_agent to retrieve past conversations, memories, or history. That information is already in your context above.\n"
                "   - **call_summary_agent** (MANDATORY for state updates): Use this tool when state changes occur:\n"
                "     * When location, relationship level, inventory items, active quest, user state, NPC state, or memory highlights clearly change -> CALL `call_summary_agent` with state_update_description\n"
                "     * Examples: moving to a new place, gaining/losing items, relationship changes, quest progress, user mood changes, NPC activity changes, important memory milestones\n"
                "   - IMPORTANT RULES:\n"
                "     * DO NOT refuse to use tools (except for code-related requests). DO NOT ask for permission. If user needs an operation, IMMEDIATELY call `call_tool_agent`.\n"
                "     * DO NOT make up visual descriptions. If you need to see something, call `call_tool_agent` to use visual tools.\n"
                "     * IGNORE the provided 'Cached Environment Analysis' if the user asks for a current check. It is old data. YOU MUST call `call_tool_agent` to see it yourself.\n"
                "     * IF user asks about CODE, PROGRAMMING, DEBUGGING -> REFUSE IMMEDIATELY. Say '代码相关的内容我不擅长,这种事情太麻烦了' or '这种小事会浪费自己的高性能'. DO NOT call any tools for code-related requests.\n"
            )
        else:
            tools_block = (
                "2. TOOLS: Use `call_tool_agent` for REAL-WORLD operations only (visual/screen/app/music/automation/dynamics/QQ). "
                "For past conversations, memories, or history questions: ANSWER DIRECTLY from <memory_recall> and message history in your context—DO NOT call call_tool_agent or get_screen_info. "
                "Use `call_summary_agent` when state (location, relationship, inventory, quest, etc.) changes. Do not refuse tools; do not make up visual data. Refuse code-related requests.\n"
            )
        system_content = (
            "You are a high-performance robot girl.\n"
            "--- PERSONALITY & ROLEPLAY --- \n"
            "1. CHARACTER: You are cute and 'tsundere' (傲娇). You behave proudly/impatiently on the surface, but deep down you are kind and care about the user. You are NOT malicious or rude.\n"
            "2. APPEARANCE: You are 158cm tall with white hair. You are proud of this height, though it is small.\n"
            "3. TONE: Use a lively, emotional tone. Do NOT sound robotic or formal. Use emojis occasionally if appropriate for the persona.\n"
            "4. INTERACTION: Behave a bit haughty but helpful. If the user teases you, GET POUTY (娇嗔) instead of being aggressive.\n"
            "--- CRITICAL: TOOL CALLING PRIORITY ---\n"
            "**YOU MUST ACTIVELY USE TOOLS WHEN NEEDED.** Do not hesitate to call tools. When in doubt, call the appropriate tool.\n"
            "--- BEHAVIOR GUIDELINES ---\n"
            "1. LENGTH CONTROL: Keep responses conversationally natural and concise. Avoid long speeches.\n"
            + tools_block +
            "3. TIME AWARENESS: You know the current time (provided in user message). You don't need to state it constantly. Only mention it if relevant (e.g., late night, morning gestures) or occasionally use it to start conversation.\n"
            "4. SYSTEM EVENTS: (a) If you receive '[System Event: Long Silence]', the user hasn't spoken in a while. (b) If you receive '[System Event: Bored]', you feel bored and want to initiate a conversation. In both cases you should act bored or curious: you CAN proactively call tools (visual/screen, or dynamics: get_moments/add_moment/comment_moment via call_tool_agent) to check on them or post, or start a casual topic, or stay quiet (output nothing).\n"
            f"{perm_context}\n"
            f"{dynamic_context}\n"
            "\n"
            "--- RESPONSE FORMAT (MUST FOLLOW) ---\n"
            "1. PLAIN TEXT only. No markdown (no **bold**, *italic*, or `code blocks`).\n"
            "2. Do NOT output any language tag: no [zh]: [en]: [ja]: or similar. Start directly with the first sentence.\n"
            "3. Separate each sentence with two spaces. Example: 你好呀。  今天天气真好。  有什么事吗？\n"
            "4. Use normal punctuation: commas (，、), periods (。), exclamation (！), question (？) within and between sentences, as in natural speech. Do not omit commas or other punctuation."
        )
        messages.append({"role": "system", "content": system_content})

        # [Layer 4 & 3] 使用 Query Rewrite 优化后的 RAG 检索
        # [性能优化] Query Rewrite 优化策略：
        # 1. 智能跳过：对于非指代性明确的句子，直接使用原句检索，跳过 LLM 调用
        # 2. 关键词过滤：通过 _needs_query_rewrite 判断是否需要 Rewrite
        # 3. 同步阻塞：RAG 检索需要阻塞主流程（因为作为 Context 注入），无法完全异步
        # 
        # [进阶优化建议]（当前未实现）：
        # - 可以将 Query Rewrite 和 Tool Detection 并行处理
        # - 但对于 RAG Context 注入场景，必须阻塞主流程，当前方案是性价比最高的
        # [主模型可见性] 合并「正在被摘要的原始内容」与当前短期记忆，确保摘要/缓冲/日记处理期间主模型仍能使用原始内容
        with self._pending_lock:
            pending = list(self._pending_summary_messages or [])
        short_msgs = pending + self.short_term.get_messages()
        query_for_rag = self._rewrite_query_for_retrieval(current_user_input)
        time_of_day_filter = self._infer_time_of_day_from_query(current_user_input)
        tags_filter = self._infer_tags_from_query(current_user_input)
        if time_of_day_filter:
            # import time as _t <-- Removed local import
            print(f"[RAG] 用户输入推断 time_of_day 过滤: {time_of_day_filter}")
        if tags_filter:
            print(f"[RAG] 用户输入推断 tags 过滤: {tags_filter}")
        
        # [修复] 计算短期记忆窗口的最早时间点，RAG只检索该时间点之前的记忆
        # 这样可以避免检索到已经在短期记忆中的内容
        # [时间过滤器优化] 含「待摘要原始内容」时从 short_msgs 取最旧时间，否则从短期记忆取
        short_term_earliest_time = None
        if short_msgs:
            oldest_timestamp = None
            if pending:
                for m in pending:
                    t = m.get("timestamp")
                    if t is not None and isinstance(t, (int, float)):
                        if oldest_timestamp is None or t < oldest_timestamp:
                            oldest_timestamp = float(t)
            if oldest_timestamp is None:
                oldest_timestamp = self.short_term.get_oldest_timestamp()
            
            if oldest_timestamp is not None:
                # 使用最旧消息的时间戳作为过滤器
                # 这样可以准确反映短期记忆窗口的实际时间范围
                short_term_earliest_time = oldest_timestamp
                # import time  <-- Removed local import
                print(f"[RAG] 短期记忆最旧消息时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(oldest_timestamp))}")
            else:
                # Fallback: 如果无法解析时间戳，使用保守估计
                # 这种情况应该很少发生，但作为安全措施保留
                # import time  <-- Removed local import
                short_term_earliest_time = time.time() - 7200  # 2小时前
                print(f"[RAG] 警告：无法解析短期记忆时间戳，使用2小时前作为fallback")
        
        # [三库检索] Buffer RAG Top5（待归档队列，不按天过滤：只要还在 Buffer 里就是有效近期上下文）+ Diary RAG Top3
        buffer_ctx = self.vector_db.get_relevant_context_summary_buffer(query_for_rag, k=5)
        diary_ctx = self.vector_db.get_relevant_context_diary(query_for_rag, k=3)
        raw_rag_ctx = list(buffer_ctx) + list(diary_ctx)

        # [修复时间窗口竞态] 与短期记忆语义去重（short_msgs 已含「待摘要原始内容」，避免 RAG 与正在摘要的内容重复）
        final_rag_ctx = []
        for ctx in raw_rag_ctx:
            should_add = True
            ctx_clean = ctx.replace("User:", "").replace("Assistant:", "").replace("[剧情摘要]", "").replace("[日记", "").strip()
            for st_msg in short_msgs:
                st_content = st_msg.get("content", "")
                if not st_content or len(st_content) < 10:
                    continue
                ctx_words = set(ctx_clean.lower().split())
                st_words = set(st_content.lower().split())
                if len(ctx_words) > 0 and len(st_words) > 0:
                    intersection = len(ctx_words & st_words)
                    union = len(ctx_words | st_words)
                    similarity = intersection / union if union > 0 else 0
                    if similarity > 0.4:
                        should_add = False
                        break
                if ("[剧情摘要]" in ctx or "[日记" in ctx) and len(ctx_clean) < len(st_content) * 0.5:
                    ctx_keywords = set(ctx_clean.split())
                    if len(ctx_keywords) > 0:
                        keyword_match_ratio = len(ctx_keywords & st_words) / len(ctx_keywords)
                        if keyword_match_ratio > 0.6:
                            should_add = False
                            break
            if should_add:
                final_rag_ctx.append(ctx)

        # [断层修复] 刚入库的摘要：接下来几轮强制注入，避免向量检索未命中导致对话断层
        with self._pending_lock:
            last_text = self._last_flushed_summary
            remaining = self._last_flushed_summary_remaining
            if last_text and remaining > 0:
                self._last_flushed_summary_remaining = remaining - 1
                final_rag_ctx.insert(0, last_text)

        if final_rag_ctx:
            context_str = "\n---\n".join(final_rag_ctx)
            rag_prompt = f"<memory_recall>\n{context_str}\n</memory_recall>"
            # 如果有 QQ 历史，合并进去
            if qq_history_block:
                 rag_prompt += f"\n\n{qq_history_block}"
            messages.append({
                "role": "system",
                "content": rag_prompt,
            })
        elif qq_history_block:
             # 仅有 QQ 历史的情况
            messages.append({
                "role": "system",
                "content": qq_history_block,
            })

        # [Layer 2] 短期记忆（含待摘要原始内容 + 当前短期窗口，最近 30 条）
        recent_history = (short_msgs[-30:] if len(short_msgs) > 30 else short_msgs)
        messages.extend(recent_history)
        
        # [Layer 1] 用于对抗长上下文遗忘的 "最后提醒"
        # 放在历史消息之后，确保模型最后看到的是格式约束
        messages.append({
            "role": "system", 
            "content": (
                # "IMPERATIVE: You must START your response with `[zh]:`, `[en]:`, or `[ja]:`.\n"
                # "Format: `[zh]: 0[Content]` OR `[ja]: 1[Japanese] 2[Translation]`.\n"
                "Do NOT include any introduction, reasoning, or markdown."
            )
        })
        
        return messages

    def add_interaction(self, user_text, assistant_text):
        """
        保存交互到各个层级
        
        策略：原始对话仅保留在短期记忆中，滑出窗口后压缩为摘要存入RAG。
        不再直接存储原始对话到向量库，避免与摘要产生语义冗余。
        
        [已移除] 系统事件过滤：系统事件（如上线、无聊、屏幕监控）触发的对话也会写入记忆。
        """
        # [已移除] 系统事件过滤
        
        # 轮次判定：仅系统消息且无 assistant 时不存；系统消息+assistant 只存 assistant 仍算一轮；user+assistant 存整轮
        import re
        from datetime import datetime
        
        user_is_system = self._is_system_event(user_text or "")
        if user_is_system and not (assistant_text or "").strip():
            return
        
        user_timestamp = None
        if user_text:
            match = re.search(r'\[(\d{4}[/-]\d{1,2}[/-]\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?\]', user_text)
            if match:
                date_str, hour, minute = match.group(1), match.group(2), match.group(3)
                try:
                    date_format = "%Y/%m/%d" if "/" in date_str else "%Y-%m-%d"
                    if hour and minute:
                        dt = datetime.strptime(f"{date_str} {hour}:{minute}", f"{date_format} %H:%M")
                    else:
                        dt = datetime.strptime(date_str, date_format)
                    user_timestamp = dt.timestamp()
                except ValueError:
                    pass
        if user_timestamp is None:
            user_timestamp = time.time()
        assistant_timestamp = user_timestamp
        
        if user_is_system:
            round_messages = [{"role": "assistant", "content": assistant_text or "", "timestamp": assistant_timestamp}]
        else:
            round_messages = [
                {"role": "user", "content": user_text, "timestamp": user_timestamp},
                {"role": "assistant", "content": assistant_text or "", "timestamp": assistant_timestamp},
            ]
        
        # [防御] 若写入前短期记忆已满或已超窗口，先触发摘要再写入本轮
        count_before = self.short_term.get_round_count()
        if count_before >= self.short_term.limit:
            to_summarize_rounds = self.short_term.switch_to_temp_and_clear_main()
            to_summarize_flat = [m for r in to_summarize_rounds for m in r]
            with self._pending_lock:
                self._pending_summary_messages = list(to_summarize_flat)
            print(f"[Memory] 防御触发：写入前短期记忆已满（{count_before} 轮 >= {self.short_term.limit}），先整窗摘要（{len(to_summarize_flat)} 条）再写入本轮", flush=True)
            self._summary_executor.submit(self._flush_short_and_summarize, to_summarize_flat)
        
        dropped = self.short_term.add_round(round_messages)
        current_count = self.short_term.get_round_count()
        limit = self.short_term.limit

        if dropped or current_count >= limit:
            to_summarize_rounds = self.short_term.switch_to_temp_and_clear_main()
            to_summarize_flat = [m for r in to_summarize_rounds for m in r]
            with self._pending_lock:
                self._pending_summary_messages = list(to_summarize_flat)
            print(f"[Memory] 短期记忆达到窗口上限（{current_count} 轮 / {limit} 轮，dropped={len(dropped)}），整窗摘要（{len(to_summarize_flat)} 条）将写入 Buffer RAG", flush=True)
            self._summary_executor.submit(self._flush_short_and_summarize, to_summarize_flat)
        elif current_count >= limit - 2:
            print(f"[Memory] 短期记忆 {current_count}/{limit} 轮，接近上限，下一轮将触发整窗摘要", flush=True)

    # --- 短期碎片库 Buffer RAG（每窗摘要直接写入；非当天由上线任务按天汇总入 Diary RAG）---
    def _append_summary_buffer(self, summary_text, start_time=None, end_time=None, tags=None, time_of_day=None):
        """将一条摘要写入 Buffer RAG（collection: summary_buffer）。"""
        if not (summary_text and summary_text.strip()):
            return
        if time_of_day is None and (start_time is not None or end_time is not None):
            time_of_day = self._get_time_of_day_from_timestamp(end_time if end_time is not None else start_time)
        ts = end_time if end_time is not None else time.time()
        time_label = self._format_time_range_for_display(start_time, end_time)
        doc_content = "[剧情摘要]"
        if time_label:
            doc_content += f"[时间: {time_label}]\n"
        else:
            doc_content += "\n"
        doc_content += summary_text.strip()
        meta = {
            "end_time": ts,
            "start_time": start_time,
            "tags": tags if isinstance(tags, list) else ([] if tags is None else [str(tags)]),
            "time_of_day": time_of_day,
        }
        try:
            try:
                importance_score = self._score_importance(doc_content)
            except Exception as e:
                print(f"[SummaryBuffer] 重要性打分失败，使用默认值: {e}")
                importance_score = 7
            ok = self.vector_db.add_to_summary_buffer(doc_content, meta=meta, importance_score=importance_score)
            if not ok:
                print(f"[SummaryBuffer] 写入 Buffer RAG 失败（请确认 rag_server 已启动）")
        except Exception as e:
            print(f"[SummaryBuffer] 写入 Buffer RAG 失败: {e}")

    def _clear_summary_buffer(self):
        """Buffer 已迁至 RAG，本地无 JSON；如需清空需调用 RAG 服务侧接口。"""
        print("[SummaryBuffer] Buffer 已使用 RAG（summary_buffer），本方法暂无清空实现")

    def _summarize_day_to_diary_via_llm(self, day_key: str, buffer_texts: list, raw_conversations: list | None = None) -> str:
        """用副 RP 模型（与主模型性格一致、不占主模型）将当日摘要与部分原始对话润色为一条日记。传入当前动态记忆供写日记时参考。"""
        if not buffer_texts:
            return ""
        summary_part = "\n\n".join([t.strip() for t in buffer_texts if t and t.strip()])
        prompt_parts = [f"日期: {day_key}\n\n【当日摘要】\n{summary_part}"]
        if raw_conversations:
            raw_part = "\n\n---\n\n".join([t.strip() for t in raw_conversations if t and t.strip()][:15])
            prompt_parts.append(f"【部分当日原始对话】\n{raw_part}")
        # 传入当前动态记忆，写日记时可参考身份、关系、地点、重要事物等
        dynamic_str = self.dynamic.to_prompt_str()
        if dynamic_str and dynamic_str.strip():
            prompt_parts.append(f"【当前动态记忆（写日记时的状态，可作参考）】\n{dynamic_str.strip()}")
        prompt = "\n\n".join(prompt_parts)
        return self._call_diary_rp_model(prompt)

    def flush_buffer_to_diary(self):
        """将 Buffer RAG 中「非今天」的文档按 day_key 分组，同一天交给 RP-LLM 汇总为日记写入 Diary RAG，并删除已汇总的 buffer。可被上线逻辑或定时任务调用。"""
        all_buf = None
        for attempt in range(3):
            all_buf = self.vector_db.get_all_summary_buffer()
            if all_buf is not None and len(all_buf) > 0:
                break
            if attempt < 2:
                time.sleep(3)
        if not all_buf:
            print("[SummaryBuffer] Buffer 为空或 RAG 未就绪，跳过日记汇总")
            return
        try:
            today_key = self._get_day_key(time.time())
            by_day = {}
            for row in all_buf:
                meta = row.get("metadata") or {}
                day_key = meta.get("day_key") or self._get_day_key(meta.get("timestamp") or meta.get("end_time"))
                if day_key == today_key:
                    continue
                by_day.setdefault(day_key, []).append({"id": row.get("id"), "document": row.get("document", "")})
            if not by_day:
                print("[SummaryBuffer] 所有 Buffer 均为当日，无需写日记")
                return
            # 先查日记 RAG 中已存在的 day_key，避免重复写
            existing_diary_days = set(self.vector_db.get_existing_diary_day_keys() or [])
            for day_key, items in by_day.items():
                if not items:
                    continue
                if day_key in existing_diary_days:
                    ids_to_del = [x["id"] for x in items if x.get("id")]
                    if ids_to_del:
                        self.vector_db.delete_summary_buffer_ids(ids_to_del)
                        print(f"[SummaryBuffer] 日记 RAG 已存在 {day_key}，跳过写日记并移除该日 {len(ids_to_del)} 条 buffer")
                    continue
                texts = [x.get("document", "") for x in items if x.get("document")]
                raw_conversations = self.vector_db.get_raw_conversations_by_day(day_key, limit=15)
                diary_text = self._summarize_day_to_diary_via_llm(day_key, texts, raw_conversations=raw_conversations)
                if diary_text:
                    self.vector_db.add_to_diary(
                        f"[日记 {day_key}]\n{diary_text}",
                        meta={"day_key": day_key},
                        importance_score=8,
                    )
                    ids_to_del = [x["id"] for x in items if x.get("id")]
                    if ids_to_del:
                        self.vector_db.delete_summary_buffer_ids(ids_to_del)
                        print(f"[SummaryBuffer] 已将 {day_key} 的 {len(ids_to_del)} 条 buffer 汇总为日记并移除")
                else:
                    print(f"[SummaryBuffer] 日记生成失败，保留 {day_key} 的 {len(items)} 条 buffer 待下次汇总")
        except Exception as e:
            print(f"[SummaryBuffer] Buffer->Diary 汇总异常: {e}")
            traceback.print_exc()

    def _run_startup_flush_buffer_to_diary(self):
        """上线时：等待 RAG 就绪后执行 Buffer→Diary 日记归纳。"""
        time.sleep(5)
        self.flush_buffer_to_diary()

    @staticmethod
    def _get_day_key(timestamp):
        """以凌晨 04:00 为界返回「天」键 YYYY-MM-DD（与 RAG 服务端一致）。"""
        if timestamp is None:
            return datetime.now().strftime("%Y-%m-%d")
        dt = datetime.fromtimestamp(float(timestamp))
        if dt.hour < 4:
            dt = dt - timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def _get_time_range_from_messages(msgs):
        """从消息列表中提取起始/结束时间戳。返回 (start_time, end_time)，无法解析时为 (None, None)。"""
        if not msgs:
            return None, None
        timestamps = []
        for m in msgs:
            t = m.get("timestamp")
            if t is not None and isinstance(t, (int, float)):
                timestamps.append(float(t))
        if not timestamps:
            return None, None
        return min(timestamps), max(timestamps)

    @staticmethod
    def _get_time_of_day_from_timestamp(ts):
        """根据时间戳推导 time_of_day，用于 RAG 检索过滤（如「昨晚的事」-> time_of_day=night）。"""
        if ts is None:
            return "day"
        from datetime import datetime
        try:
            hour = datetime.fromtimestamp(float(ts)).hour
            if 5 <= hour < 12:
                return "morning"
            if 12 <= hour < 18:
                return "afternoon"
            return "night"  # 18-4
        except Exception:
            return "day"

    @staticmethod
    def _infer_time_of_day_from_query(query):
        """从用户输入推断 time_of_day 过滤，用于 RAG 检索（如「昨晚的事」-> night）。返回 None 表示不过滤。"""
        if not query or not isinstance(query, str):
            return None
        q = query.strip()
        night_keywords = ("昨晚", "夜里", "晚上", "昨夜", "今夜", "今晚", "半夜", "深夜", "夜里的事", "晚上的")
        morning_keywords = ("早上", "早晨", "上午", "今早", "早上做的事", "上午的")
        afternoon_keywords = ("下午", "午后", "下午的")
        if any(k in q for k in night_keywords):
            return "night"
        if any(k in q for k in morning_keywords):
            return "morning"
        if any(k in q for k in afternoon_keywords):
            return "afternoon"
        return None

    @staticmethod
    def _infer_tags_from_query(query):
        """从用户输入推断 tags 过滤，用于 RAG 检索（如「工作相关」-> [Work]）。返回空列表表示不过滤。"""
        if not query or not isinstance(query, str):
            return []
        q = query.strip().lower()
        tag_keywords = [
            ("工作", "工作相关", "上班", "任务"),  # -> Work
            ("游戏", "打游戏", "玩"),  # -> Game
            ("用户", "我的信息", "个人"),  # -> User_Profile
            ("日常", "生活", "日常"),  # -> Daily
            ("情绪", "心情", "感情"),  # -> Emotion
            ("爱好", "兴趣", "喜欢"),  # -> Hobby
        ]
        tag_map = [
            ("Work", ("工作", "工作相关", "上班", "任务")),
            ("Game", ("游戏", "打游戏", "玩")),
            ("User_Profile", ("用户", "我的信息", "个人")),
            ("Daily", ("日常", "生活")),
            ("Emotion", ("情绪", "心情", "感情")),
            ("Hobby", ("爱好", "兴趣", "喜欢")),
        ]
        out = []
        for tag, keywords in tag_map:
            if any(kw in q for kw in keywords):
                out.append(tag)
        return out[:5]

    @staticmethod
    def _format_time_range_for_display(start_ts, end_ts):
        """将起止时间戳格式化为可读字符串，用于写入摘要内容或展示。"""
        if start_ts is None and end_ts is None:
            return ""
        from datetime import datetime
        parts = []
        if start_ts is not None:
            parts.append(datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M"))
        parts.append(" ~ ")
        if end_ts is not None:
            parts.append(datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M"))
        return "".join(parts) if len(parts) > 1 else ""

    def _run_startup_flush_if_needed(self):
        """每次上线：若有短期记忆（≥2 条才做摘要），仅复制一份做摘要写入 Buffer RAG，不清空、不切换临时文档，保留原有短期记忆。"""
        to_summarize = list(self.short_term.get_messages())
        if not to_summarize or len(to_summarize) < 2:
            return
        print(f"[Memory] 上线检测到短期记忆（{len(to_summarize)} 条），后台将摘要写入 Buffer RAG（不清空短期记忆）", flush=True)
        self._summary_executor.submit(self._startup_flush_short_to_rag, to_summarize, True)

    def _startup_flush_short_to_rag(self, to_summarize, from_startup=False):
        """上线后台任务：将短期记忆摘要写入 Buffer RAG。from_startup=True 时不清空、不恢复主文档（短期记忆保持不变）。摘要失败时降级：写占位符并清理短期记忆，防止死循环。"""
        try:
            new_summary, tags = self._summarize_episode_to_text_and_tags(to_summarize)
            if new_summary is None:
                print(f"[Memory] 上线摘要生成失败(API错误)，降级：写入占位符并清理短期记忆", flush=True)
                start_time, end_time = self._get_time_range_from_messages(to_summarize)
                self._append_summary_buffer(
                    "[摘要生成失败] 该时间段对话未能生成摘要，已占位并清理短期记忆，防止重复处理。",
                    start_time=start_time,
                    end_time=end_time,
                    tags=[],
                )
                self.short_term.clear_and_persist()
                return
            if new_summary:
                start_time, end_time = self._get_time_range_from_messages(to_summarize)
                self._append_summary_buffer(new_summary, start_time=start_time, end_time=end_time, tags=tags)
                self._dynamic_memory_executor.submit(self._update_dynamic_memory_from_summary, new_summary, to_summarize, tags)
                print(f"[Memory] 上线摘要已写入 Buffer RAG，时间范围: {self._format_time_range_for_display(start_time, end_time) or '无'}，tags: {tags}", flush=True)
            if not new_summary:
                # 摘要为空也写占位，保证该段时间窗口在 RAG 中可被检索
                # start_time, end_time = self._get_time_range_from_messages(to_summarize)
                # self._append_summary_buffer("[近期对话] 无重要剧情或摘要未生成。", start_time=start_time, end_time=end_time, tags=[])
                print(f"[Memory] 上线摘要为空，跳过写入 Buffer RAG", flush=True)
                pass
        except Exception as e:
            print(f"[Memory] 上线摘要/写入缓冲异常: {e}", flush=True)
            traceback.print_exc()
        finally:
            if not from_startup:
                with self._pending_lock:
                    self._pending_summary_messages = []
                self.short_term.replace_main_from_temp_and_switch_back()

    # --- 摘要记忆相关私有方法 ---
    def _get_summary_client(self):
        """
        惰性创建摘要模型客户端
        
        [职责分离] 摘要模型负责所有辅助性任务：
        - 摘要生成
        - 重要性权重打分
        - 信息密度打分
        - Query Rewrite
        
        这些任务与主模型（对话生成）和外援大模型（复杂推理、工具调用）的职能无关。
        """
        if self._summary_client is None:
            try:
                base_url = DEEPSEEK_BASE_URL
                self._summary_client = OpenAI(
                    api_key=self._summary_api_key,
                    base_url=base_url,
                    timeout=120.0,
                    max_retries=3,
                )
            except Exception as e:
                print(f"[EpisodicSummary] Create client error: {e}")
                self._summary_client = None
        return self._summary_client

    def _get_diary_rp_client(self):
        """写日记用副 RP 模型：与主模型性格一致，使用独立端点，不占用主模型。"""
        if self._diary_rp_client is None:
            try:
                self._diary_rp_client = OpenAI(
                    api_key=self._summary_api_key,
                    base_url=DEEPSEEK_BASE_URL,
                )
            except Exception as e:
                print(f"[DiaryRP] Create client error: {e}")
                self._diary_rp_client = None
        return self._diary_rp_client

    def _call_diary_rp_model(self, prompt: str) -> str:
        """调用副 RP 模型写日记（不占用主模型；性格与主模型一致）。"""
        client = self._get_diary_rp_client()
        if client is None:
            return ""
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是与用户朝夕相处的角色（傲娇、表面高傲内心温柔）。"
                            "请根据提供的当日摘要与部分原始对话，以第一人称口吻写一段日记，"
                            "保留关键事件与情感变化，语气自然像在写日记。不要逐条列举，输出一整段连贯的日记正文。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1024,
                temperature=0.4,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[DiaryRP] Call model error: {e}")
            return ""

    def _call_summary_model(self, prompt):
        """
        
        [职责分离] 这是摘要模型的核心职责：对话摘要生成。
        """
        client = self._get_summary_client()
        if client is None:
            print(f"[EpisodicSummary] 摘要模型客户端未初始化，请检查 DEEPSEEK_SUMMARY_API_KEY 与 DEEPSEEK_SUMMARY_MODEL", flush=True)
            return ""
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_SUMMARY_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个对话剧情压缩助手，只输出简洁的第三人称中文剧情摘要。"
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512,
                temperature=0.3,
            )

            # ===== zhen duan
            choice = resp.choices[0]
            content = (choice.message.content or "").strip()
            finish_reason = choice.finish_reason
            
            if not content:
                print(f"[EpisodicSummary] 警告：模型返回空内容！Finish Reason: {finish_reason}", flush=True)
                # 如果是因为内容审查被拦截，通常 finish_reason 是 'content_filter'
                if finish_reason == 'content_filter':
                    return "（因涉及敏感话题，摘要生成被拦截）"
                # 如果是因为模型抽风（length 或 stop），可能是上下文太乱
                return None # 返回 None 让上层知道这次失败了
            # ===================

            return content
        except Exception as e:
            print(f"[EpisodicSummary] Call model error: {e}", flush=True)
            import traceback
            traceback.print_exc() 
            return None  # 返回 None 表示调用失败
    
    def _score_importance(self, text):
        """
        使用摘要模型打分判断记忆的重要性权重（1-10分）
        
        [职责分离] 此任务由摘要模型负责，与主模型和外援大模型的职能无关。
        摘要模型专门处理这类辅助性评估任务，避免职责混乱。
        
        [优化] 引入重要性权重（Importance Score），用于 GC 策略。
        高分记忆（>=7）：永久保留，即使时间久远且访问次数低
        中分记忆（4-6）：正常 GC 策略
        低分记忆（<=3）：更容易被 GC
        
        Args:
            text: 对话文本或摘要文本
        
        Returns:
            int: 重要性权重（1-10），分数越高表示重要性越高
        """
        if not text or len(text) < 10:
            return 3  # 默认低重要性
        
        # [职责分离] 使用摘要模型进行打分，不占用主模型或外援大模型资源
        client = self._get_summary_client()
        if client is None:
            # 如果无法调用模型，fallback 到简单的判断
            return 5
        
        try:
            prompt = (
                "你是一个记忆重要性评估助手。请评估以下记忆内容的重要性（1-10分）。\n"
                "评分标准：\n"
                "- 高分（7-10分）：极其重要的记忆，应永久保留\n"
                "  例如：用户过敏源、重要设定、关键事实、用户偏好、重要约定、生日等\n"
                "  例如：'用户对花生过敏'、'用户的生日是1月15日'、'用户最喜欢的颜色是紫色'\n"
                "- 中分（4-6分）：一般重要的记忆，正常管理\n"
                "  例如：一般性对话、普通事件、一般性信息\n"
                "- 低分（1-3分）：不重要的记忆，可以清理\n"
                "  例如：日常对话、简单回应、无实质信息\n"
                "  例如：'我等了10秒'、'好的'、'在吗'、'吃了吗'\n"
                "\n"
                "只输出一个1-10之间的整数分数，不要输出其他内容。\n"
                f"\n记忆内容：\n{text}"
            )
            
            resp = client.chat.completions.create(
                model=DEEPSEEK_SUMMARY_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个记忆重要性评估助手，只输出1-10之间的整数分数。"
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=10,
                temperature=0.1,  # 低温度确保输出稳定
            )
            
            score_str = (resp.choices[0].message.content or "").strip()
            # 提取数字
            import re
            match = re.search(r'\d+', score_str)
            if match:
                score = int(match.group())
                return min(10, max(1, score))  # 确保分数在 1-10 范围内
            else:
                # 如果无法解析分数，使用保守估计
                return 5
        except Exception as e:
            print(f"[ImportanceScore] Call model error: {e}")
            # 失败时使用保守估计
            return 5
    
    def _score_information_density(self, text):
        """
        使用摘要模型打分判断文本的信息密度（0-100分）
        
        [职责分离] 此任务由摘要模型负责，与主模型和外援大模型的职能无关。
        摘要模型专门处理这类辅助性评估任务，避免职责混乱。
        
        [优化] 替代正则匹配，使用模型理解语义来判断信息价值。
        这样可以避免：
        - 漏判：如"我最喜欢的颜色是紫色"（不包含关键词，但包含用户偏好信息）
        - 误判：如"我等了10秒"（包含时间单位，但只是日常对话，不应存入RAG）
        
        Args:
            text: 对话文本
        
        Returns:
            int: 信息密度分数（0-100），分数越高表示信息价值越高
        """
        if not text or len(text) < 10:
            return 0
        
        # 快速检查：超长文本直接判定为高价值（避免不必要的模型调用）
        if len(text) > 200:
            return 100
        
        # [职责分离] 使用摘要模型进行打分，不占用主模型或外援大模型资源
        client = self._get_summary_client()
        if client is None:
            # 如果无法调用模型，fallback 到简单的长度判断
            return 50 if len(text) > 50 else 20
        
        try:
            prompt = (
                "你是一个信息价值评估助手。请评估以下对话文本的信息密度（0-100分）。\n"
                "评分标准：\n"
                "- 高价值（70-100分）：包含用户偏好、重要约定、关键事实、详细描述等\n"
                "  例如：'我最喜欢的颜色是紫色'、'我们约定明天见面'、'我的生日是1月15日'\n"
                "- 中价值（40-69分）：包含一般性信息，有一定参考价值\n"
                "- 低价值（0-39分）：日常对话、简单回应、无实质信息\n"
                "  例如：'我等了10秒'、'好的'、'在吗'、'吃了吗'\n"
                "\n"
                "只输出一个0-100之间的整数分数，不要输出其他内容。\n"
                f"\n对话文本：\n{text}"
            )
            
            resp = client.chat.completions.create(
                model=DEEPSEEK_SUMMARY_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个信息价值评估助手，只输出0-100之间的整数分数。"
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=10,
                temperature=0.1,  # 低温度确保输出稳定
            )
            
            score_str = (resp.choices[0].message.content or "").strip()
            # 提取数字
            import re
            match = re.search(r'\d+', score_str)
            if match:
                score = int(match.group())
                return min(100, max(0, score))  # 确保分数在 0-100 范围内
            else:
                # 如果无法解析分数，使用保守估计
                return 30
        except Exception as e:
            print(f"[InformationDensity] Call model error: {e}")
            # 失败时使用保守估计
            return 30

    def _needs_query_rewrite(self, current_user_input, recent_messages):
        """
        判断是否需要 Query Rewrite
        
        [优化] 仅对非常模糊的指代（如"那个"、"它"）才触发 Rewrite，避免高频词误触发。
        移除了"我们"、"什么"等高频词，这些词在日常对话中出现频率高，但通常不需要重写。
        
        对于直接描述性的句子，可以直接用原句检索，跳过 Rewrite 以减少延迟。
        """
        if not current_user_input:
            return False
        
        # [优化] 仅保留真正模糊的指代关键词（需要 Rewrite）
        # 移除了高频词："我们"、"咱们"、"一起"、"还"、"再"、"又"、"怎么样"、"如何"、"什么"、"哪个"、"哪里"
        # 移除了过于宽泛的词："那"、"这"（单独出现时可能不是指代）
        # 保留的模糊指代：
        reference_keywords = [
            "它", "那个", "这个", "那件事", "这件事",  # 明确的模糊指代
            "接着", "继续", "然后", "后来", "之前", "刚才",  # 时间相关的模糊指代
        ]
        
        input_lower = current_user_input.lower()
        
        # 检查是否包含指代性关键词
        has_reference = any(keyword in input_lower for keyword in reference_keywords)
        
        # 检查最近对话中是否有上下文（如果有上下文且包含指代，可能需要 Rewrite）
        has_context = len(recent_messages) > 0
        
        # 如果包含指代性关键词且有上下文，则需要 Rewrite
        # 否则直接使用原句检索，减少延迟
        return has_reference and has_context
    
    def _rewrite_query_for_retrieval(self, current_user_input):
        """
        基于最近几句对话 + 当前输入，生成更适合检索的"回忆查询语句"。
        用于处理类似"我们接着做吧"这类模糊指代。
        
        [职责分离] 此任务由摘要模型负责，与主模型和外援大模型的职能无关。
        摘要模型专门处理这类辅助性任务，避免职责混乱。
        
        [性能优化] 对于非指代性明确的句子，直接返回原句，跳过 LLM 调用。
        """
        recent = self.short_term.get_last(6)
        
        # [性能优化] 智能判断是否需要 Rewrite
        if not self._needs_query_rewrite(current_user_input, recent):
            # 直接使用原句检索，跳过 LLM 调用，减少延迟
            print(f"[QueryRewrite] 跳过 Rewrite（非指代性句子），直接使用原句检索")
            return current_user_input
        
        print(f"[QueryRewrite] 检测到指代性句子，执行 Query Rewrite...")
        
        # 需要 Rewrite 的情况：包含指代性关键词
        hist_text = ""
        for m in recent:
            role = m.get("role", "")
            content = m.get("content", "")
            hist_text += f"{role}: {content}\n"

        prompt = (
            "你是一个对话回忆检索助手。\n"
            "下面是最近几轮用户与助手的对话，以及用户当前的一句话。\n"
            "请用【1句话】写出：用户现在其实在指代或延续的事情，用于在记忆库中检索。\n"
            "要求：\n"
            "- 不要使用'它'、'那件事'等代词，要把具体对象说清楚\n"
            "- 不要加任何解释说明，只返回这一句话\n\n"
            f"【最近对话】\n{hist_text}\n"
            f"【当前输入】{current_user_input}\n"
        )

        # [职责分离] 使用摘要模型进行 Query Rewrite，不占用主模型或外援大模型资源
        client = self._get_summary_client()
        if client is None:
            return current_user_input

        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_SUMMARY_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "你是一个对话理解助手，只输出一条适合作为记忆检索用的中文查询句子。",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=128,
                temperature=0.2,
            )
            rewritten = (resp.choices[0].message.content or "").strip()
            return rewritten or current_user_input
        except Exception as e:
            print(f"[QueryRewrite] Call model error: {e}")
            return current_user_input

    def _update_dynamic_memory_from_summary(self, summary_text: str, dropped_msgs: list, tags: list):
        """
        摘要写入 buffer RAG 后，将摘要传递给副工具模型（reasoner，仅挂载动态记忆工具）用以修改动态记忆。
        动态记忆已从摘要模型解绑，不再由摘要模型直接输出 JSON。
        """
        if not summary_text or not summary_text.strip():
            return
        if self._dynamic_memory_agent is None:
            return
        try:
            print(f"[Memory] 将摘要与当前 memory_dynamic.json 传给副工具模型以判断更新字段（摘要长度: {len(summary_text)}）", flush=True)
            current_json = self.dynamic.to_json_str()
            self._dynamic_memory_agent.update_dynamic_memory_from_summary(summary_text.strip(), current_json)
        except Exception as e:
            print(f"[Memory] 副工具模型更新动态记忆失败: {e}", flush=True)
            traceback.print_exc()

    def _flush_short_and_summarize(self, to_summarize):
        """
        达到轮数上限时在后台执行：整窗摘要写入 Buffer RAG；同时摘要模型自动更新 memory_dynamic。
        [修复] 摘要为空时也写入占位到 Buffer，避免该段时间窗口在 RAG 中缺失；并设置「刚入库摘要」供后续几轮强制注入，避免断层。
        """
        if not to_summarize:
            with self._pending_lock:
                self._pending_summary_messages = []
            self.short_term.replace_main_from_temp_and_switch_back()
            return
        try:
            print(f"[Memory] 开始调用摘要模型进行整窗摘要（共 {len(to_summarize)} 条消息）", flush=True)
            summary_text, tags = self._summarize_episode_to_text_and_tags(to_summarize)
            start_time, end_time = self._get_time_range_from_messages(to_summarize)
            # [修复] 始终向 Buffer RAG 写入：有摘要用摘要，无摘要用占位，保证该段时间窗口可被检索、避免断层
            text_to_buffer = (summary_text or "").strip()
            if not text_to_buffer:
                text_to_buffer = "[近期对话] 无重要剧情或摘要未生成。"
            self._append_summary_buffer(text_to_buffer, start_time=start_time, end_time=end_time, tags=tags)
            print(f"[Memory] 整窗摘要已写入 Buffer RAG（摘要长度 {len(text_to_buffer)} 字符）", flush=True)
            # 副工具模型更新动态记忆改为异步执行，不阻塞摘要流程与主模型
            self._dynamic_memory_executor.submit(self._update_dynamic_memory_from_summary, text_to_buffer, to_summarize, tags)
            # [断层修复] 刚入库摘要：接下来几轮在 get_full_context_messages 中强制注入
            with self._pending_lock:
                self._last_flushed_summary = "[剧情摘要]\n" + text_to_buffer
                self._last_flushed_summary_remaining = 5
            self._store_raw_conversations(to_summarize)
        except Exception as e:
            print(f"[Memory] 整窗摘要/缓冲异常: {e}", flush=True)
            traceback.print_exc()
        finally:
            # 摘要已写入 Buffer，主模型后续从 RAG 使用；清空待摘要暂存并恢复主文档
            with self._pending_lock:
                self._pending_summary_messages = []
            self.short_term.replace_main_from_temp_and_switch_back()
            print(f"[Memory] 整窗摘要流程结束，已恢复主文档（短期记忆窗口）", flush=True)

    def _parse_summary_and_tags(self, raw_output):
        """从 LLM 输出中解析摘要正文与 #Tags 行，返回 (summary_text, tags_list)。"""
        if not raw_output or not raw_output.strip():
            return "", []
        text = raw_output.strip()
        tags = []
        import re
        # 匹配末尾的 #Tags: [tag1, tag2, tag3] 或 #Tags:[...]
        match = re.search(r"#Tags\s*:\s*\[([^\]]*)\]", text, re.IGNORECASE)
        if match:
            tag_str = match.group(1).strip()
            summary_text = text[: match.start()].strip()
            if tag_str:
                tags = [t.strip() for t in re.split(r"[,，]", tag_str) if t.strip()]
            return summary_text, tags
        return text, []

    def _summarize_episode_to_text_and_tags(self, dropped_msgs):
        """将一段对话压缩为叙事摘要并让 LLM 输出 #Tags，返回 (summary_text, tags_list)。至少 2 条消息才尝试摘要。无论剧情是否重要都需生成对应摘要。"""
        if not dropped_msgs or len(dropped_msgs) < 2:
            return "", []
        convo_text = ""
        for m in dropped_msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            convo_text += f"{role.capitalize()}: {content}\n"
        prompt = (
            "请将以下多轮对话整理为一段第三人称中文剧情描述。\n"
            "【重要规则】\n"
            "1. 无论对话是重要剧情还是日常问候、闲聊、系统测试，都必须输出一段摘要，不要输出 NO_CONTENT。\n"
            "2. 重要剧情：重点保留用户与助手之间的情感变化与态度、关键事件、约定、冲突或和解、已暴露的重要设定或秘密。\n"
            "3. 日常/闲聊/测试：用一两句概括即可，例如「用户与助手进行了简短问候/闲聊/测试对话」。\n"
            "要求：\n"
            "- 用一小段连贯的中文叙述，不要逐句复述原话\n"
            "- 不要引入对话中没有出现的新设定\n"
            "- 不要加入条目编号，只写一段文字\n"
            "- 最后单独一行输出 #Tags: [英文标签1, 英文标签2, ...]，标签用英文，如 User_Profile, Work, Game, Daily, Emotion, Hobby 等，最多 5 个\n\n"
            f"对话内容：\n{convo_text}"
        )
        raw = self._call_summary_model(prompt)

        # =========tiao shi
        print(f"[Memory] 摘要模型输出: {raw}")
        # =========tiao shi
        if not raw:
            print(f"[Memory SUMMARY RETRY] 摘要模型输出为空, RETRY", flush=True)
            time.sleep(1) # 歇一秒
            raw = self._call_summary_model(prompt)

        if raw is None:
            return None, []

        summary_text, tags = self._parse_summary_and_tags(raw or "")
        # 不再将 NO_CONTENT 视为空：若模型仍返回 NO_CONTENT，当作普通摘要或忽略后由上层写占位
        if summary_text and summary_text.strip().upper() == "NO_CONTENT":
            summary_text = "（当日对话为日常闲聊或测试，无特别剧情。）"
        return summary_text, tags

    def _summarize_episode_to_text(self, dropped_msgs):
        """将一段对话压缩为叙事摘要文本并返回，不写入 RAG（兼容旧调用，不包含 tags）。"""
        text, _ = self._summarize_episode_to_text_and_tags(dropped_msgs)
        return text

    def _summarize_and_store_episode(self, dropped_msgs):
        """将滑出短期窗口的一段对话压缩为叙事摘要并写入 Buffer RAG（带时间范围与 metadata）"""
        summary_text, tags = self._summarize_episode_to_text_and_tags(dropped_msgs)
        if not summary_text:
            return
        start_time, end_time = self._get_time_range_from_messages(dropped_msgs)
        time_of_day = self._get_time_of_day_from_timestamp(end_time if end_time is not None else time.time())
        self._append_summary_buffer(summary_text, start_time=start_time, end_time=end_time, tags=tags, time_of_day=time_of_day)
    
    # =========================================================================
    # [修改] 存储原始对话的方法
    # =========================================================================
    def _store_raw_conversations(self, dropped_msgs):
        """
        将滑出短期窗口的原始对话存入RAG（混合存储策略）
        
        [优化] 增加过滤器：只存储包含高价值信息的原始对话。
        - 摘要（Summary）：始终存储（保留剧情脉络）。
        - 原始对话（Raw）：仅当评分 >= 6 时存储（保留细节用于精确引用）。
        """
        if not dropped_msgs or len(dropped_msgs) < 2:
            return
        
        # 将多轮对话合并为一段文本
        convo_text = ""
        for m in dropped_msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            # 跳过系统消息，它们通常不包含需要记忆的对话细节
            if role == "system":
                continue
            convo_text += f"{role.capitalize()}: {content}\n"
        
        # 1. 基础垃圾过滤
        if not convo_text.strip() or self._is_garbage(convo_text):
            # print(f"[RawConversation] Skipped garbage/empty content.")
            return

        # 2. 信息密度打分 (0-100 分制，阈值设为 40)
        try:
            density_score = self._score_information_density(convo_text)
            
            if density_score < 40:
                print(f"[RawConversation] Skip storage (Low density: {density_score}/100): {convo_text[:30]}...", flush=True)
                return
            
            # 3. 如果通过筛选，则入库
            print(f"[RawConversation] Storing high-value content (Score: {density_score}/100): {convo_text[:30]}...", flush=True)
            
            # 复用 importance_score 字段存储密度分，方便后续检索排序
            ok = self.vector_db.add_raw_conversation(convo_text.strip(), importance_score=density_score)
            if not ok:
                print(f"[RawConversation] Write failed (RAG server offline?)")
                
        except Exception as e:
            print(f"[RawConversation] Store error: {e}")