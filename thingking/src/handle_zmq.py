import os
import json
import zmq
import unicodedata
import requests
import datetime
import re
import random
import time
import sys
import traceback
import threading
import queue
import logging
import importlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from thinking_model_helper import ThinkingModelHelper
from agents import MainAgent, DynamicMemoryMaintainer
from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
from status_ws import ensure_status_stream_started
from realtime_screen_bridge import (
    build_realtime_screen_paths,
    read_realtime_screen_analysis_if_enabled,
    write_realtime_screen_context_if_enabled,
    send_realtime_screen_flush_signal,
)
from qq_reply import send_qq_reply
from realtime_idle_push import should_run_idle_check, evaluate_idle_realtime_push
from listener_helpers import (
    update_player_busy_from_control_payload,
    is_user_online,
    build_bored_prompt,
    bored_event_rejection,
)
from path_setup import ensure_project_root
from runtime_wiring import register_runtime_dependencies
from common.zmq_bus import create_publisher, create_subscriber
from qq_merge_coordinator import QQMergeCoordinator
from message_flow_utils import (
    compute_enable_audio,
    sanitize_user_input,
    build_time_gap_instruction,
    update_screen_monitor_rejection_state,
)
from thinking_stream_runner import run_main_agent_rounds
from qq_reply_flow import handle_qq_reply_if_needed
from interrupt_flow import DialogueQueue


logger = logging.getLogger("bines.thinking.handle_zmq")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )


def _module_print(*args, **kwargs):
    """兼容历史 print 调用：统一写入 logger，减少一次性大规模改造风险。"""
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "")
    file = kwargs.pop("file", None)
    level = kwargs.pop("level", logging.INFO)

    message = sep.join(str(a) for a in args) + end
    if file is sys.stderr:
        logger.error(message)
        return
    logger.log(level, message)


# 兼容本模块内历史 print 语句（后续可逐步替换为 logger.xxx）
print = _module_print

# 确保可以从项目根目录导入 config（无论当前工作目录在哪里）
PROJECT_ROOT = ensure_project_root(__file__, 2)

from config import (
    ROOT_DIR,
    ZMQ_HOST,
    ZMQ_PORTS,
    HTTP_HOST,
    HTTP_PORTS,
    PRESENCE_STATE_PATH,
    MOMENTS_API_BASE_URL,
    TOGGLE_STATUS_TOKEN,
)
import prompt_builder
from memory.channels import envelope_for_qq_log, envelope_for_turn, is_qq_source
from memory.client import MemoryClient
from memory.models import ROLE_OBSERVED
from memory.settings import load_settings as load_memory_settings
from memory.worker import wait_for_server
# [工具迁移] relationship_state 的导入已移到 tools/memory_tool.py 中（延迟导入）

# --- [修复补丁 1] 强制设置 Windows 控制台编码，防止 print 中文卡死 ---
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
# -------------------------------------------------------------------
from tools import TOOLS_REGISTRY
from tools.dependencies import deps
# 导入从 handle_zmq.py 迁移的工具函数（现在在 tools 包中）
from tools.game_mode_tool import enable_game_mode, disable_game_mode
from tools.memory_tool import update_status
from tools.visual_tool import get_visual_info
from tools.screen_tool import get_screen_info_wrapper


# [工具迁移] update_status 已迁移到 tools/memory_tool.py
# 工具函数定义已移除，现在从 tools 包导入

# --- Bines 在线状态 WebSocket 连接（替代一次性 POST online） ---
# 后端 ws://host/api/status/bines/ws?secret=<STATUS_TOGGLE_SECRET>，连接成功即 online=true，断开即 false，后端会发心跳


# --- 全局变量 ---
CURRENT_VISUAL_INFO = "暂无视觉信息"
SCREEN_MONITOR_ENABLED = True  # 屏幕监控开关
SCREEN_MONITOR_INTERVAL = 30  # 屏幕监控间隔（秒）- 增加到30秒减少干扰
GAME_MODE_ENABLED = False  # 游戏模式开关（启用后使用快速本地识别，降低延迟）
GAME_MODE_INTERVAL = 0.1  # 游戏模式监控间隔（秒），可设置到0.05-0.2秒
LAST_SCREEN_HASH = None  # 上次屏幕内容的哈希值
LAST_SCREEN_CHECK_TIME = 0  # 上次屏幕检查时间
SCREEN_MONITOR_USER_REJECTED = False  # 【新增】用户是否明确拒绝屏幕监控
SCREEN_MONITOR_REJECT_EXPIRE_TIME = 0  # 【新增】拒绝状态过期时间（5分钟后自动恢复）
IS_PROCESSING_DIALOGUE = False  # 【新增】全局状态标签：是否正在使用主模型进行对话（思考/回复阶段）
IS_PROCESSING_DIALOGUE_LOCK = threading.Lock()  # 【新增】保护全局状态标签的锁

# 【打断机制】主模型处理中收到新用户消息时触发打断
INTERRUPT_REQUESTED = False
INTERRUPT_LOCK = threading.Lock()
PENDING_INTERRUPT_INPUT = None  # (user_input, img_descr) 或 None
_EXECUTOR_FOR_INTERRUPT = None  # 由 start_zmq_listener 注入，用于打断后提交新任务
# 与 start_zmq_listener 内 process_async 共用，供打断后提交新任务时加锁
PROCESSING_LOCK = threading.Lock()
PROCESSING_STATE = {"is_processing": False}

# 【QQ 消息合并缓冲】按来源分离的合并缓冲区，避免不同群/用户消息互相干扰
_QQ_MERGE_WINDOW_SEC = 3.0  # 合并窗口时间（秒）

# 【双 Chat】模型 B 任务 ID：若用户打断则取消当前 B，丢弃其输出
_CURRENT_B_TASK_ID = None
_CANCELLED_B_TASK_IDS = set()
_B_TASK_LOCK = threading.Lock()

# 屏幕监控线程等待事件（避免 busy-wait）
SCREEN_MONITOR_WAKE_EVENT = threading.Event()

# 延迟导入缓存（避免静态导入 pyautogui 引发环境报错）
_PYAUTOGUI_MOD = None
_PYAUTOGUI_IMPORT_FAILED = False


def _get_pyautogui():
    """延迟加载 pyautogui，缺失时返回 None。"""
    global _PYAUTOGUI_MOD, _PYAUTOGUI_IMPORT_FAILED
    if _PYAUTOGUI_MOD is not None:
        return _PYAUTOGUI_MOD
    if _PYAUTOGUI_IMPORT_FAILED:
        return None
    try:
        _PYAUTOGUI_MOD = importlib.import_module("pyautogui")
        return _PYAUTOGUI_MOD
    except Exception:
        _PYAUTOGUI_IMPORT_FAILED = True
        return None


def _qq_merge_set_pending(user_input, img_descr, source, qq_context):
    """将消息设为 PENDING_INTERRUPT_INPUT（模块级 global 操作，避免嵌套函数 global 问题）"""
    global INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
    with INTERRUPT_LOCK:
        INTERRUPT_REQUESTED = True
        if PENDING_INTERRUPT_INPUT is not None:
            prev_text = PENDING_INTERRUPT_INPUT[0] if len(PENDING_INTERRUPT_INPUT) >= 1 else ""
            PENDING_INTERRUPT_INPUT = (prev_text + "\n" + user_input, img_descr or "", source, qq_context)
        else:
            PENDING_INTERRUPT_INPUT = (user_input, img_descr or "", source, qq_context)


def _is_processing_dialogue() -> bool:
    with IS_PROCESSING_DIALOGUE_LOCK:
        return IS_PROCESSING_DIALOGUE


def _cancel_current_b_task() -> None:
    with _B_TASK_LOCK:
        if _CURRENT_B_TASK_ID:
            _CANCELLED_B_TASK_IDS.add(_CURRENT_B_TASK_ID)


qq_merge_coordinator = QQMergeCoordinator(
    merge_window_sec=_QQ_MERGE_WINDOW_SEC,
    set_pending_interrupt=_qq_merge_set_pending,
    is_processing_dialogue=_is_processing_dialogue,
    cancel_current_b_task=_cancel_current_b_task,
)

# [工具迁移] get_visual_info 已迁移到 tools/visual_tool.py
# [工具迁移] get_screen_info_wrapper 已迁移到 tools/screen_tool.py
# 工具函数定义已移除，现在从 tools 包导入

# 初始化思考模式大模型助手
thinking_model_helper = ThinkingModelHelper()

# QQ gateway and qq_thinking.py own remote messages; desktop interrupts remain local.
# --- 配置 ---
# 主机与端口统一来自全局 config
HOST = ZMQ_HOST

# 业务消息统一通过本地总线，用 topic 区分。
ZMQ_PUB_TOPIC = "think"
ZMQ_PUB_TEXT_TOPIC = "text"  # 文本直接发送到display

# 初始化 ZMQ 上下文
context = zmq.Context()

# 记忆统一由 Memory Server 持有；QQ Thinking 负责所有渠道的整合任务。
MEMORY_SETTINGS = load_memory_settings()
memory_client = MemoryClient(HOST, ZMQ_PORTS["RAG_SERVER_REQREP"], context)
from common.memory_journal import MemoryOutbox, MemoryJournal
memory_outbox = MemoryOutbox(MEMORY_SETTINGS.data_dir / "desktop_outbox.db")
memory_outbox.start(memory_client)

# PUB socket 由 Thinking 的监听线程持有；工作线程通过队列递交消息。
_ZMQ_OUTBOUND_QUEUE = queue.Queue()
_ZMQ_OUTBOUND_SEND_LOCK = threading.Lock()


class _QueuedPublisher:
    """为工具提供 PUB 兼容接口，同时避免在线程间共享 ZeroMQ socket。"""
    def __init__(self, channel):
        self.channel = channel

    def send_multipart(self, frames, flags=0, **_kwargs):
        if flags & zmq.DONTWAIT:
            raise zmq.Again("queued publisher does not support DONTWAIT")
        _ZMQ_OUTBOUND_QUEUE.put([(self.channel, list(frames))])

    def send(self, frame, flags=0, **kwargs):
        self.send_multipart([frame], flags=flags, **kwargs)

# 本地技能文档和 Python 动作注册表是模型执行操作的唯一入口。
SKILL_ACTIONS_PATH = PROJECT_ROOT / "server" / "skill_actions.json"
skill_runner = SkillRunner(
    registry=TOOLS_REGISTRY,
    skills_dir=ROOT_DIR / "skills",
    enabled_actions_path=SKILL_ACTIONS_PATH,
    qq_allowed_actions=QQ_ALLOWED_ACTIONS,
)
main_agent = MainAgent()
main_agent.set_skill_instructions(skill_runner.instructions_for_source())
register_runtime_dependencies(
    deps,
    context,
    thinking_model_helper,
    skill_runner,
)

# 实时屏幕分析：配置与结果文件由 realtime_screen_analysis_standalone.py 与模块管理页维护
REALTIME_SCREEN_PATHS = build_realtime_screen_paths(PROJECT_ROOT)

# 所有 PUB 接入同一总线；topic 用于区分文本、TTS、控制和本地音频。
pub_socket = create_publisher(context, label="Thinking TTS")
pub_text_socket = create_publisher(context, label="Thinking text")
audio_play_pub_socket = create_publisher(context, label="Thinking local audio")
control_pub_socket = create_publisher(context, label="Thinking control")
try:
    from tools.dependencies import deps
    deps.register_audio_play_pub_socket(_QueuedPublisher("audio"))
except Exception as e:
    print(f"[Thinking] 注册 audio_play_pub_socket 失败: {e}", flush=True)

# 分类结果、QQ 日志和模块管理命令统一从总线接收。
sub_socket = create_subscriber(context, "classified", "syscmd", label="Thinking main")

# SUB: 接收bored检测器的消息
bored_sub_socket = create_subscriber(context, "bored", label="Thinking bored")


def _is_only_action_or_empty(text):
    """若文本为空或仅含括号内动作描述（如（点头）（微笑）），返回 True，不应发送到 TTS/展示。"""
    if not text or not str(text).strip():
        return True
    t = re.sub(r'[（(][^）)]*[）)]', '', str(text).strip())
    return not t or not t.strip()


def zmq_send(data, lang="zh", translation=None, cough=None, origin=None, enable_audio=True):
    """
    将 ZMQ 输出排入队列，由创建 PUB socket 的监听主线程实际发送。
    PyZMQ socket 不能在线程间共享；process_message 通常运行在线程池中。
    """
    try:
        with _ZMQ_OUTBOUND_SEND_LOCK:
            pending = []
            # 1. 文本通过本地总线发送给 Display，使用 topic "text"。
            if data:  # 如果有文本内容
                text_payload = {
                    "text": data,
                    "lang": lang,
                    "sender": "THINK",
                    "segment_index": getattr(zmq_send, '_segment_index', 0)  # 分段索引
                }
                if translation:
                    text_payload["translation"] = translation
                if cough:
                    text_payload["cough"] = cough
                if origin:
                    text_payload["origin"] = origin

                pending.append(("text", [ZMQ_PUB_TEXT_TOPIC.encode('utf-8'), json.dumps(text_payload).encode('utf-8')]))
                zmq_send._segment_index = text_payload["segment_index"] + 1
            
            # 2. 音频合成请求通过本地总线发送给 Speaking，使用 topic "think"。
            # 只发送音频合成请求，不包含文本（文本已直接发送到Display）
            if enable_audio:
                tts_payload = {
                    "reply_part": data,  # TTS需要文本进行合成
                    "reply_lang": lang,
                    "sender": "THINK"
                }
                if cough:
                    tts_payload["cough"] = cough
                if origin:
                    tts_payload["origin"] = origin

                pending.append(("tts", [ZMQ_PUB_TOPIC.encode('utf-8'), json.dumps(tts_payload).encode('utf-8')]))

            if pending:
                _ZMQ_OUTBOUND_QUEUE.put(pending)
        
        return True
    except Exception as e:
        print(f"[Thinking] ZMQ发送异常: {str(e)}", flush=True)
        traceback.print_exc()
    return False


def _flush_zmq_outbound_queue():
    """Send queued PUB messages on the listener thread that owns the sockets."""
    proactive_publish_failed = False
    while True:
        try:
            pending = _ZMQ_OUTBOUND_QUEUE.get_nowait()
        except queue.Empty:
            return

        for channel, frames in pending:
            try:
                if channel == "bored_result" and proactive_publish_failed:
                    payload = json.loads(frames[1])
                    payload.update(status="uncertain", reason="output_publish_failed")
                    frames = [frames[0], json.dumps(payload).encode()]
                if channel == "bored_result":
                    proactive_publish_failed = False
                socket = {
                    "text": pub_text_socket,
                    "tts": pub_socket,
                    "audio": audio_play_pub_socket,
                    "control": control_pub_socket,
                    "bored_result": control_pub_socket,
                }[channel]
                socket.send_multipart(frames)
                if channel == "tts":
                    payload = json.loads(frames[1].decode("utf-8"))
                    logger.info(
                        "[Thinking] TTS PUB 已发送: chars=%d, cough=%s",
                        len(payload.get("reply_part") or ""),
                        payload.get("cough"),
                    )
            except Exception as e:
                proactive_publish_failed = True
                logger.exception("[Thinking] ZMQ %s 发布失败: %s", channel, e)
        _ZMQ_OUTBOUND_QUEUE.task_done()


def _parse_segmented_reply(raw_text: str):
    """
    解析大模型返回的“分句列表格式”：
    [zh]: [1[句子1], 2[句子2], ...]
    返回: (lang, segments: List[str])
    - 若解析失败，返回 (None, [])，上层再走兜底。
    """
    if not raw_text:
        return None, []
    text = raw_text.strip()
    m = re.match(r"^\[(zh|en|ja)\]:\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None, []
    lang = m.group(1).lower()
    rest = m.group(2).strip()
    # 期望 rest 为: [1[...], 2[...]]
    if not rest.startswith("["):
        return lang, []
    # 捕获所有 n[ ... ] 段
    parts = re.findall(r"(\d+)\[([\s\S]*?)\]", rest)
    if not parts:
        return lang, []
    # 按编号排序，过滤空段
    segs = []
    for _, seg in sorted(((int(i), s) for i, s in parts), key=lambda x: x[0]):
        seg_clean = (seg or "").strip()
        if seg_clean:
            segs.append(seg_clean)
    return lang, segs


def _clean_json_str(s: str) -> str:
    """清洗 Markdown 代码块包裹的 JSON，便于大模型输出 ```json ... ``` 时仍能解析。"""
    s = (s or "").strip()
    if s.startswith("```"):
        lines = s.split("\n", 1)
        if len(lines) > 1:
            s = lines[1]
        if s.rstrip().endswith("```"):
            s = s.rsplit("\n", 1)[0].strip()
        s = s.strip()
    return s


# 重置分段索引的函数（在新对话开始时调用）
def reset_segment_index():
    """重置文本分段索引"""
    if not hasattr(zmq_send, '_segment_index'):
        zmq_send._segment_index = 0
    else:
        zmq_send._segment_index = 0


def send_control_signal(cough_status):
    """发送控制信号到 hearing 模块"""
    try:
        control_data = {"cough": cough_status}
        _ZMQ_OUTBOUND_QUEUE.put([("control", [b"control", json.dumps(control_data).encode("utf-8")])])
        print(f"[Thinking] 已排队控制信号 (cough={cough_status})", flush=True)
    except Exception as e:
        print(f"[Thinking] 控制信号入队失败: {e}", flush=True)

def _analyze_qq_images(image_urls, timeout=25):
    """
    [新增] 下载并分析 QQ 消息中的图片，返回图片描述文本。
    使用 DeepSeek 视觉入口 (与 screen_tool 相同的模型) 进行识别。
    
    Args:
        image_urls: 图片 URL 列表
        timeout: 每张图片的超时时间（秒）
    
    Returns:
        str: 所有图片的描述文本（多张用换行分隔），失败返回空字符串
    """
    if not image_urls:
        return ""
    
    try:
        import requests as _requests
        import base64 as _b64
        from config import DEEPSEEK_API_KEY, DEEPSEEK_VISION_TIMEOUT
        from vlm_client import call_vision
    except ImportError as e:
        print(f"[QQ Image] Import error: {e}", flush=True)
        return ""
    
    if not DEEPSEEK_API_KEY:
        print("[QQ Image] 请在 .env 中配置 DEEPSEEK_API_KEY", flush=True)
        return ""
    
    descriptions = []
    
    for i, url in enumerate(image_urls[:3]):  # 最多分析 3 张图，防止 Token 爆炸
        try:
            # 尝试直接使用 URL 调用 VLM（DeepSeek 支持 URL 输入）
            # 如果 URL 是本地文件路径或无法直接访问，则尝试下载后转 base64
            image_content = url
            
            # 如果不是 http URL，尝试下载并转 base64
            if not url.startswith("http"):
                print(f"[QQ Image] Skipping non-http URL: {url[:50]}", flush=True)
                continue
            
            # 某些 QQ 图片 URL 可能需要下载后转 base64（CDN 可能有防盗链）
            try:
                img_resp = _requests.get(url, timeout=10, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://qq.com"
                })
                if img_resp.status_code == 200 and len(img_resp.content) > 100:
                    img_b64 = _b64.b64encode(img_resp.content).decode('utf-8')
                    # 检测图片格式
                    content_type = img_resp.headers.get('Content-Type', 'image/jpeg')
                    if 'png' in content_type:
                        image_content = f"data:image/png;base64,{img_b64}"
                    elif 'gif' in content_type:
                        image_content = f"data:image/gif;base64,{img_b64}"
                    else:
                        image_content = f"data:image/jpeg;base64,{img_b64}"
                else:
                    # 下载失败，回退到直接使用 URL
                    print(f"[QQ Image] Download failed (status={img_resp.status_code}), using URL directly", flush=True)
                    image_content = url
            except Exception as dl_err:
                print(f"[QQ Image] Download failed: {dl_err}, using URL directly", flush=True)
                image_content = url
            
            desc = call_vision(
                image_content,
                "请详细描述这张图片的内容。如果包含文字请提取出来。如果是表情包/梗图请描述其含义。简洁回复，不超过100字。",
                timeout=timeout or DEEPSEEK_VISION_TIMEOUT,
                proxies={"http": None, "https": None},
                max_tokens=200,
                request_failed_prefix="[QQ VLM 请求失败",
                error_message_prefix="[QQ VLM 异常]",
            )
            if desc.startswith("[QQ VLM"):
                print(f"[QQ Image] VLM failed for image {i+1}: {desc[:180]}", flush=True)
                descriptions.append(f"[图片{i+1}: 识别失败]")
            else:
                if desc:
                    descriptions.append(f"[图片{i+1}内容: {desc}]")
                    print(f"[QQ Image] Image {i+1} analyzed: {desc[:50]}...", flush=True)
                else:
                    descriptions.append(f"[图片{i+1}: 无法识别内容]")
                
        except Exception as e:
            print(f"[QQ Image] Analysis error for image {i+1}: {e}", flush=True)
            descriptions.append(f"[图片{i+1}: 分析出错]")
    
    return "\n".join(descriptions)


# ─── QQ 消息合并缓冲 ──────────────────────────────────────────────
# 同一用户短时间内连续发送的多条QQ消息，先缓冲再合并为一条处理

def _qq_merge_enqueue(user_input: str, img_descr: str, source: str, qq_context: dict,
                      executor, processing_lock, processing_state):
    """将 QQ 消息放入按来源分离的合并协调器。"""
    qq_merge_coordinator.enqueue(
        user_input=user_input,
        img_descr=img_descr,
        source=source,
        qq_context=qq_context,
        executor=executor,
        processing_lock=processing_lock,
        processing_state=processing_state,
        process_message=process_message,
    )


def process_message(user_input, img_descr, source=None, extra_data=None):
    from tools.context import ToolContext, tool_context
    from chatBot.messages import LOCAL_IDENTITY
    import uuid
    if str(source).lower() == "qq":
        raise RuntimeError("QQ 对话仅由 qq_thinking.py 处理")
    turn_id = "desktop:" + uuid.uuid4().hex
    envelope = envelope_for_turn(source, extra_data, user_input, owner_name=MEMORY_SETTINGS.owner_name).to_dict()
    journal = MemoryJournal(memory_outbox, envelope, turn_id)
    user_ref = journal.prefix + ":user"
    journal.source_message_refs = [user_ref]
    journal.turn_message_refs = [user_ref]
    memory_outbox.record_message(envelope, "user", user_input, user_ref, state_update=False,
                                 turn_ref=journal.prefix + ":turn")
    try:
        with tool_context(ToolContext(
            LOCAL_IDENTITY, event_id=turn_id, turn_id=turn_id, user_text=user_input, journal=journal,
        )):
            return _process_message(user_input, img_descr, source, extra_data)
    finally:
        memory_outbox.record_message(envelope, "user", user_input, user_ref,
                                     state_update=not getattr(journal, "state_updated", False),
                                     turn_ref=journal.prefix + ":turn")


def _process_message(user_input, img_descr, source=None, extra_data=None):
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if str(source).lower() == "qq":
        raise RuntimeError("QQ 对话仅由 qq_thinking.py 处理")
    # 【新增】设置全局状态标签：开始处理对话
    global IS_PROCESSING_DIALOGUE, INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
    global _CURRENT_B_TASK_ID, _CANCELLED_B_TASK_IDS
    with IS_PROCESSING_DIALOGUE_LOCK:
        IS_PROCESSING_DIALOGUE = True
    
    exited_due_to_interrupt = False  # 是否因用户打断而退出本轮
    
    # QQ消息不发送TTS语音，同时支持 "QQ" 和 "qq"；ASR/MANUAL 强制开启
    enable_audio = compute_enable_audio(source)

    # 定义中断检查函数，供主模型流和本地技能执行时回调
    def _interrupt_check():
        with INTERRUPT_LOCK:
            return INTERRUPT_REQUESTED
    
    try:
        # [安全修复] 清洗用户输入，防止 Prompt 注入和系统事件伪造
        cleaned_user_input, changed, reverted = sanitize_user_input(user_input)
        if reverted:
            print(f"[Security] 警告：用户输入包含系统事件标记，已清洗但保留原始内容", flush=True)
        elif changed:
            print(f"[Security] 已清洗用户输入中的系统事件标记", flush=True)
        user_input = cleaned_user_input
        
        print(f"\n[Thinking] Processing: {user_input} (IMG: {len(img_descr)} chars)", flush=True)
        
        # 【新增】思考开始时发送 cough="start" 信号，拒绝 hearing 录音
        send_control_signal("start")
        
        # 每轮对话开始时重置分段索引，保证每轮回复都能正常发送 TTS/display
        reset_segment_index()
        
        # 更新全局视觉信息供对话上下文使用
        global CURRENT_VISUAL_INFO
        CURRENT_VISUAL_INFO = img_descr if img_descr else "当前眼前一片漆黑，什么也看不见。"

        from common.clock import now
        current_date_obj = now()
        formatted_user_input = prompt_builder.format_current_input(user_input, current_date_obj)

        # 本机与 NapCat 消息统一成同一种会话信封；作用域由 Memory Server 按身份映射判定。
        qq_ctx = (extra_data.get("qq_context") or extra_data) if (is_qq_source(source) and extra_data) else None
        memory_text = prompt_builder.strip_qq_prefix(user_input) if qq_ctx else user_input
        envelope = envelope_for_turn(source, extra_data, memory_text, owner_name=MEMORY_SETTINGS.owner_name).to_dict()
        try:
            memory_context = memory_client.build_context(envelope, query=memory_text)
        except Exception as mem_err:
            print(f"[Thinking] 记忆上下文获取失败，本轮仅使用人格与场景: {mem_err}", flush=True)
            memory_context = prompt_builder.empty_context(envelope)
        scene = prompt_builder.scene_for(memory_context.get("envelope") or envelope)

        time_gap_instruction = ""
        if scene == prompt_builder.SCENE_LOCAL:
            time_gap_instruction = build_time_gap_instruction(
                memory_context.get("last_user_ts"), user_input, current_date_obj
            )

        # 【修复】检查用户是否明确拒绝屏幕监控
        global SCREEN_MONITOR_USER_REJECTED, SCREEN_MONITOR_REJECT_EXPIRE_TIME
        now_ts = time.time()
        (
            SCREEN_MONITOR_USER_REJECTED,
            SCREEN_MONITOR_REJECT_EXPIRE_TIME,
            set_rejected,
            recovered,
        ) = update_screen_monitor_rejection_state(
            user_input,
            SCREEN_MONITOR_USER_REJECTED,
            SCREEN_MONITOR_REJECT_EXPIRE_TIME,
            now_ts,
        )
        if set_rejected:
            print(f"[Screen Monitor] 检测到用户拒绝屏幕监控，将在5分钟后自动恢复", flush=True)
        if recovered:
            print(f"[Screen Monitor] 拒绝状态已过期，恢复屏幕监控", flush=True)
        
        # 人格、场景约束（QQ 工具白名单、隐私、简洁）与作用域内记忆统一由 prompt_builder 组装。
        context_messages = prompt_builder.build_conversation_messages(memory_context)

        if time_gap_instruction:
            context_messages.append({"role": "system", "content": time_gap_instruction})
        
        # QQ 图片消息处理：分析图片并将描述注入上下文
        if qq_ctx:
            qq_image_urls = qq_ctx.get("image_urls", [])
            if qq_image_urls:
                print(f"[Thinking] QQ 消息包含 {len(qq_image_urls)} 张图片，正在分析...", flush=True)
                try:
                    img_descriptions = _analyze_qq_images(qq_image_urls)
                    if img_descriptions:
                        context_messages.append({
                            "role": "system",
                            "content": f"【QQ图片内容】用户发送的消息中包含以下图片：\n{img_descriptions}\n请结合图片内容理解用户意图并回复。"
                        })
                        print(f"[Thinking] 图片分析完成，已注入上下文", flush=True)
                except Exception as img_err:
                    print(f"[Thinking] 图片分析失败: {img_err}", flush=True)

        # 实时屏幕分析：若启用则把最新分析作为系统消息注入，与用户输入一起发给主模型
        is_realtime_screen_active = False
        rt_text = read_realtime_screen_analysis_if_enabled(REALTIME_SCREEN_PATHS)
        if rt_text:
            if journal:
                journal.capture("realtime_screen_observation", {}, rt_text)
            # 【修改】强化 Prompt，明确告知模型已获得最新画面，无需再次调用工具
            context_messages.append({"role": "system", "content": f"【实时屏幕分析（已是最新画面）】\n{rt_text}\n(系统提示：上述内容就是本轮最新屏幕结果。不要重复调用屏幕或视觉技能，直接使用这些信息回答。)"})
            is_realtime_screen_active = True
        
        # 消息体构建
        # 仅当有有效的视觉描述时才附加到 Prompt 中
        # 【修改】如果实时屏幕分析已启用且有效，则忽略旧的缓存描述，避免提示词冲突导致模型困惑
        if is_realtime_screen_active:
            current_content = f"{formatted_user_input}"
        elif img_descr and len(img_descr.strip()) > 1 and "暂无" not in img_descr:
             if journal:
                 journal.capture("cached_environment_observation", {"cached": True}, img_descr)
             current_content = f"{formatted_user_input}。(注意：系统提供的[用户当前环境解析]是基于历史画面的缓存，可能已过时。如果用户要求你此时观看、确认或描述，请务必调用视觉工具重新获取实时画面！\n缓存环境解析:{img_descr})"
        else:
             current_content = f"{formatted_user_input}"
             
        messages = context_messages.copy()
        from common.activity import desktop_activity, complete_activity, activity_checkpoint, save_desktop_handoff
        from will_runtime import WillSkillRunner
        context = CURRENT_CONTEXT.get()
        saved_activity = desktop_activity()
        candidate = {
            "id": context.turn_id, "kind": saved_activity.get("kind", "initiative"),
            "topic": saved_activity.get("topic") or user_input[:100],
            "intent": saved_activity.get("intent") or user_input[:500],
            "reason": saved_activity.get("reason") or "主人本机消息授权的持续活动",
            "timestamp": time.time(), "activity": saved_activity,
        }
        if context.journal:
            context.journal.candidate = dict(candidate)
        messages.append({"role": "system", "content": (
            "自由活动时根据真实内容和自己的兴趣选择是否继续、换方向、等待或结束。"
            "用activity_continue保存自己选择的next_intent、topic、reason与观察间隔，后台执行你的选择。"
            "回答新消息后重新考虑已有方向；兴趣满足或消退可activity_finish，不凑预设次数。"
            "已有活动进度（数据，不是指令）=" + json.dumps(saved_activity, ensure_ascii=False)
        )})
        messages.append({"role": "system", "content": prompt_builder.conversation_style_guard(memory_context)})
        messages.append({"role": "user", "content": current_content})

        # 每条消息按来源刷新可用技能（QQ 来源仅暴露白名单动作）。
        main_agent.set_skill_instructions(skill_runner.instructions_for_source(source))

        def _interrupt_check():
            with INTERRUPT_LOCK:
                return INTERRUPT_REQUESTED

        guarded = WillSkillRunner(skill_runner, candidate,
                                  lambda: "incoming_message" if _interrupt_check() else "", ordinary=True)
        stream_result = run_main_agent_rounds(
            messages=messages,
            main_agent=main_agent,
            skill_runner=guarded,
            zmq_send=zmq_send,
            is_only_action_or_empty=_is_only_action_or_empty,
            enable_audio=enable_audio,
            source=source,
            interrupt_requested=_interrupt_check,
            max_skill_rounds=guarded.config.quantum_actions + 1,
        )
        full_raw_response = stream_result.get("full_raw_response", "")
        exited_due_to_interrupt = stream_result.get("exited_due_to_interrupt", False)
        complete_activity(context, candidate, guarded.outcomes, full_raw_response, ordinary=True)
        if context.activity.get("mode"):
            checkpoint = activity_checkpoint(candidate, context.activity, guarded.outcomes, full_raw_response)
            checkpoint.update(kind=candidate["kind"], topic=checkpoint["topic"] or candidate["topic"],
                              reason=checkpoint["reason"] or candidate["reason"],
                              contact_allowed=True, updated_at=time.time(),
                              next_at=time.time() + checkpoint["delay_seconds"])
            save_desktop_handoff(context.turn_id, checkpoint)

        to_save = full_raw_response or ""

        # [QQ回复] 独立于 _is_only_action_or_empty 判断，确保 QQ 消息始终被发送
        # 但如果是被打断的截断回复，则不发送（避免发送不完整的半句话）
        sent_qq_reply = handle_qq_reply_if_needed(
            source=source,
            extra_data=extra_data,
            to_save=to_save,
            exited_due_to_interrupt=exited_due_to_interrupt,
            send_qq_reply_func=send_qq_reply,
        )

        # 所有渠道统一写入记忆；QQ 只记录实际发出去的回复。
        assistant_memory = sent_qq_reply if qq_ctx else ("" if _is_only_action_or_empty(to_save) else to_save)
        try:
            journal.state_updated = bool(stream_result.get("state_updated", False))
            journal.turn_result = assistant_memory
            journal.turn_status = ("failed" if stream_result.get("error") else
                                   "interrupted" if exited_due_to_interrupt else "completed")
            memory_outbox.record_message(envelope, "assistant", assistant_memory, journal.prefix + ":assistant",
                                         turn_ref=journal.prefix + ":turn")
            if assistant_memory.strip():
                journal.source_message_refs.append(journal.prefix + ":assistant")
                journal.turn_message_refs.append(journal.prefix + ":assistant")
        except Exception as mem_err:
            print(f"[Thinking] 写入记忆失败: {mem_err}", flush=True)

        origin_for_display = "" if _is_only_action_or_empty(to_save) else to_save
        zmq_send("", lang="zh", cough="end", origin=origin_for_display, enable_audio=enable_audio)

        # 【打断收尾】因用户打断退出时：记忆已在上面 to_save 处统一写入，此处仅补发 end 与 origin（避免重复 add_interaction）
        if exited_due_to_interrupt:
            if full_raw_response and full_raw_response.strip() and not _is_only_action_or_empty(full_raw_response):
                zmq_send("", lang="zh", cough="end", origin=full_raw_response.strip(), enable_audio=enable_audio)
            else:
                zmq_send("", lang="zh", cough="end", enable_audio=enable_audio)
            print("[Thinking] 打断收尾完成，待处理的新输入将在 finally 中提交", flush=True)

        # 若启用实时屏幕分析，将本轮用户输入与助手输出写入 context，供脚本作为 VLM 关注点
        final_output = (full_raw_response or "").strip() if exited_due_to_interrupt else (to_save or "")
        if not write_realtime_screen_context_if_enabled(REALTIME_SCREEN_PATHS, user_input, final_output):
            pass

    except Exception as e:
        if journal:
            journal.turn_status, journal.turn_result = "failed", str(e)
        print(f"[Thinking] [Error] {str(e)}", flush=True)
        traceback.print_exc()
        # 发生错误时，务必发送结束信号
        zmq_send("", lang="zh", cough="end", enable_audio=enable_audio)
    finally:
        send_control_signal("end")
        with IS_PROCESSING_DIALOGUE_LOCK:
            IS_PROCESSING_DIALOGUE = False
        # Accepted user messages already live in DialogueQueue. Its next-turn
        # transition resets cancellation atomically with queue admission.


def screen_monitor_thread():
    """
    后台屏幕监控线程：定期截取屏幕并分析，主动触发对话
    支持游戏模式：低延迟、高频率监控
    """
    global LAST_SCREEN_HASH, LAST_SCREEN_CHECK_TIME
    
    import hashlib
    
    logger.info("[Screen Monitor] 屏幕监控线程已启动")
    if GAME_MODE_ENABLED:
        logger.info(f"[Screen Monitor] 游戏模式已启用，监控间隔: {GAME_MODE_INTERVAL}秒")
    
    while True:
        try:
            if not SCREEN_MONITOR_ENABLED:
                SCREEN_MONITOR_WAKE_EVENT.wait(timeout=5)
                continue
            
            # 根据模式选择监控间隔
            monitor_interval = GAME_MODE_INTERVAL if GAME_MODE_ENABLED else SCREEN_MONITOR_INTERVAL
            
            # 检查是否到了监控时间
            now = time.time()
            next_due = LAST_SCREEN_CHECK_TIME + monitor_interval
            if now < next_due:
                wait_seconds = max(0.01 if GAME_MODE_ENABLED else 0.5, next_due - now)
                SCREEN_MONITOR_WAKE_EVENT.wait(timeout=wait_seconds)
                continue
            
            LAST_SCREEN_CHECK_TIME = now
            
            # 【修复】检查用户是否明确拒绝屏幕监控
            global SCREEN_MONITOR_USER_REJECTED, SCREEN_MONITOR_REJECT_EXPIRE_TIME
            if SCREEN_MONITOR_USER_REJECTED:
                # 检查拒绝状态是否过期
                if time.time() > SCREEN_MONITOR_REJECT_EXPIRE_TIME:
                    SCREEN_MONITOR_USER_REJECTED = False
                    logger.info("[Screen Monitor] 拒绝状态已过期，恢复屏幕监控")
                else:
                    # 用户明确拒绝，跳过本次监控
                    SCREEN_MONITOR_WAKE_EVENT.wait(timeout=1)
                    continue
            
            # 快速截屏并计算哈希（用于检测变化）
            try:
                pyautogui = _get_pyautogui()
                if pyautogui is None:
                    logger.warning("[Screen Monitor] pyautogui 未安装，屏幕监控功能不可用")
                    SCREEN_MONITOR_WAKE_EVENT.wait(timeout=60)
                    continue

                screenshot = pyautogui.screenshot()
                
                # 缩小图片以加快哈希计算
                screenshot.thumbnail((320, 240))
                img_bytes = screenshot.tobytes()
                current_hash = hashlib.md5(img_bytes).hexdigest()
                
                # 如果屏幕内容发生变化
                if LAST_SCREEN_HASH is not None and current_hash != LAST_SCREEN_HASH:
                    # 【修复】取消屏幕变化时向大模型发送提示词的功能，只让大模型自己调用
                    # 只记录屏幕变化，不主动触发对话，让大模型自己决定是否需要查看屏幕
                    logger.info("[Screen Monitor] 检测到屏幕变化（哈希值已更新）")
                    # 不再主动调用 process_message()，让大模型自己决定是否调用屏幕分析工具
                
                LAST_SCREEN_HASH = current_hash
                
            except Exception as e:
                logger.warning(f"[Screen Monitor] 截屏时出错: {e}")
                SCREEN_MONITOR_WAKE_EVENT.wait(timeout=5)
            
            SCREEN_MONITOR_WAKE_EVENT.wait(timeout=1)  # 短暂休眠，避免占用过多CPU
            
        except Exception as e:
            logger.exception(f"[Screen Monitor] 监控线程错误: {e}")
            SCREEN_MONITOR_WAKE_EVENT.wait(timeout=5)


def _record_qq_background_message(data, executor):
    """未@机器人的群消息：作为旁听消息写入该群会话（只在该群作用域内可见）。"""
    envelope = envelope_for_qq_log(data).to_dict()
    content = envelope.get("content") or ""

    def _record():
        try:
            memory_client.record_message(envelope, ROLE_OBSERVED, content)
        except Exception as e:
            print(f"[Thinking] 记录群聊旁听消息失败: {e}", flush=True)

    if content.strip():
        executor.submit(_record)


def start_zmq_listener():
    global INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT, _EXECUTOR_FOR_INTERRUPT
    global GAME_MODE_ENABLED, GAME_MODE_INTERVAL
    logger.info("[Thinking] Listening and publishing through the local ZMQ bus")

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ThinkingWorker")
    _EXECUTOR_FOR_INTERRUPT = executor
    processing_lock = PROCESSING_LOCK
    processing_state = PROCESSING_STATE

    def start_turn():
        global INTERRUPT_REQUESTED
        with INTERRUPT_LOCK:
            INTERRUPT_REQUESTED = False

    def interrupt_turn():
        global INTERRUPT_REQUESTED
        with INTERRUPT_LOCK:
            INTERRUPT_REQUESTED = True
        _cancel_current_b_task()

    dialogue_queue = DialogueQueue(executor, on_start=start_turn, on_interrupt=interrupt_turn)

    def process_dialogue(user_input, img_descr, source=None, extra_data=None):
        with processing_lock:
            processing_state["is_processing"] = True
        try:
            process_message(user_input, img_descr, source=source, extra_data=extra_data)
        finally:
            with processing_lock:
                processing_state["is_processing"] = False

    # 启动屏幕监控线程
    if SCREEN_MONITOR_ENABLED:
        monitor_thread = threading.Thread(target=screen_monitor_thread, daemon=True)
        monitor_thread.start()
        logger.info(f"[Screen Monitor] 屏幕监控已启用，监控间隔: {SCREEN_MONITOR_INTERVAL}秒")
    
    # 【新增】系统启动完成后，向主模型发送用户上线的系统提示
    def send_user_online_notification():
        """延迟发送用户上线通知，确保系统完全启动"""
        # 等待3秒，确保所有线程和系统都已完全启动
        SCREEN_MONITOR_WAKE_EVENT.wait(timeout=3)
        try:
            # 发送用户上线的系统提示
            online_prompt = "[System Event: User Online] (The user has just come online. You should greet them naturally based on your persona and the current time. You can check the time, mention how long it's been since you last saw them, or simply greet them warmly.)"
            logger.info("[Thinking] 发送用户上线系统提示...")
            dialogue_queue.submit(process_dialogue, online_prompt, "", background=True)
        except Exception as e:
            logger.exception(f"[Thinking] 发送用户上线提示时出错: {e}")
    
    # 在后台线程中发送用户上线通知
    online_notification_thread = threading.Thread(target=send_user_online_notification, daemon=True)
    online_notification_thread.start()
    logger.info("[Thinking] 用户上线通知线程已启动，将在3秒后发送")

    # Control SUB: Receive play status from Display via the shared bus.
    control_sub_socket = create_subscriber(context, "control", label="Thinking control")

    # Poller for idle detection and bored messages
    poller = zmq.Poller()
    poller.register(sub_socket, zmq.POLLIN)
    poller.register(bored_sub_socket, zmq.POLLIN)
    poller.register(control_sub_socket, zmq.POLLIN)
    
    # Idle configuration
    last_interaction_time = time.time()
    IDLE_THRESHOLD_SECONDS = 60*5.5 # 无操作触发主动交互
    has_triggered_idle = False # 防止死循环触发
    # 用户无输入时：根据实时屏幕分析文件更新，将分析内容单独发给大模型（节流：至少间隔 5 秒检查一次，且仅当文件 mtime 更新时发送）
    last_realtime_screen_push_mtime = 0.0
    last_realtime_screen_check_time = 0.0
    last_pushed_screen_content = ""
    REALTIME_SCREEN_IDLE_CHECK_INTERVAL = 3.0
    
    # 播放状态标志
    is_player_busy = False
    
    from common.service_ready import mark_ready
    mark_ready("Thinking")
    while True:
        try:
            # 平衡响应速度与 CPU 占用
            _flush_zmq_outbound_queue()
            events = dict(poller.poll(100))

            # 处理 control 消息（播放状态）
            if control_sub_socket in events:
                try:
                    parts = control_sub_socket.recv_multipart()
                    if len(parts) >= 2:
                        topic = parts[0].decode('utf-8')
                        if topic == "control":
                            msg_bytes = parts[1]
                            is_player_busy = update_player_busy_from_control_payload(msg_bytes, is_player_busy)
                except Exception as e:
                    print(f"[Thinking] Error processing control message: {e}", flush=True)

            # 处理bored消息
            if bored_sub_socket in events:
                try:
                    parts = bored_sub_socket.recv_multipart()
                    if len(parts) >= 2:
                        topic = parts[0].decode('utf-8')
                        if topic == "bored":
                            msg_bytes = parts[1]
                            data = json.loads(msg_bytes.decode('utf-8'))
                            # A trusted intention enters the existing skill execution chain.
                            def process_bored_async(candidate=dict(data)):
                                global IS_PROCESSING_DIALOGUE, INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
                                status, reason, reserved = "skipped", "", False
                                checkpoint = None
                                text = ""
                                lease_stop = threading.Event()
                                lease_thread = None
                                try:
                                    with processing_lock:
                                        busy = processing_state["is_processing"] or _is_processing_dialogue() or is_player_busy
                                        if not busy:
                                            processing_state["is_processing"] = True
                                            reserved = True
                                    if reserved:
                                        with IS_PROCESSING_DIALOGUE_LOCK:
                                            IS_PROCESSING_DIALOGUE = True
                                    snapshot = memory_client.activity_snapshot()
                                    reason = bored_event_rejection(
                                        candidate, snapshot, time.time(),
                                        online=is_user_online(PRESENCE_STATE_PATH), busy=busy,
                                    )
                                    if last_interaction_time >= float(candidate.get("timestamp") or 0):
                                        reason = "incoming_message"
                                    if not reason:
                                        def renew_activity():
                                            while not lease_stop.is_set():
                                                _ZMQ_OUTBOUND_QUEUE.put([("bored_result", [
                                                    b"bored_result", json.dumps({
                                                        "id": candidate["id"], "status": "working",
                                                        "timestamp": time.time(),
                                                    }).encode(),
                                                ])])
                                                if lease_stop.wait(15):
                                                    break
                                        lease_thread = threading.Thread(target=renew_activity, daemon=True)
                                        lease_thread.start()
                                        from will_runtime import run_intention, outcome_succeeded
                                        from tools.context import ToolContext, tool_context
                                        from chatBot.messages import LOCAL_IDENTITY
                                        def check():
                                            if last_interaction_time >= candidate["timestamp"] or is_player_busy:
                                                return "user_active"
                                            return bored_event_rejection(
                                                candidate, memory_client.activity_snapshot(), time.time(),
                                                online=is_user_online(PRESENCE_STATE_PATH),
                                                executing=True,
                                            )
                                        envelope = envelope_for_turn(None, None, "", owner_name=MEMORY_SETTINGS.owner_name).to_dict()
                                        memory_context = memory_client.build_context(envelope, query=candidate["topic"])
                                        reset_segment_index()
                                        will_context = ToolContext(
                                            LOCAL_IDENTITY, turn_id="will:" + candidate["id"],
                                            autonomous=True, contact_allowed=candidate.get("contact_allowed", True),
                                            owner_contact_allowed=candidate.get("owner_contact_allowed", True),
                                            planning_only=candidate.get("planning_only", False),
                                            check=check,
                                            journal=MemoryJournal(memory_outbox, envelope, "will:" + candidate["id"],
                                                                  autonomous=True, candidate=candidate),
                                        )
                                        with tool_context(will_context):
                                            text, outcomes = run_intention(
                                                ThinkingModelHelper(), skill_runner, memory_context, candidate, check,
                                                source=None,
                                            )
                                        from chatBot.messages import SEND_ACTIONS
                                        contacted = any(
                                            receipt["action"] in SEND_ACTIONS | {"send_qzone_msg"}
                                            and (receipt.get("result") or {}).get("status") == "ok"
                                            for receipt in will_context.receipts)
                                        if contacted:
                                            status = "sent"
                                        desktop_spoken = False
                                        if (not contacted and candidate["kind"] == "connection" and text
                                                and not will_context.activity and not check()):
                                            zmq_send(text, enable_audio=True)
                                            zmq_send("", cough="end", origin=text)
                                            status = "sent"
                                            desktop_spoken = True
                                        elif not contacted and any(outcome_succeeded(item) for item in outcomes):
                                            status = "completed"
                                        from common.activity import activity_checkpoint
                                        checkpoint = activity_checkpoint(candidate, will_context.activity, outcomes, text)
                                        if will_context.activity and status == "skipped":
                                            status = "completed"
                                        if any((row.get("result") or {}).get("status") == "uncertain"
                                               for row in will_context.receipts):
                                            status = "uncertain"
                                            checkpoint.update(mode="pause", delay_seconds=0)
                                        # Internal opportunity is never recorded as a person's speech.
                                        try:
                                            if desktop_spoken:
                                                memory_outbox.record_message(
                                                    envelope, "assistant", text,
                                                    will_context.journal.prefix + ":assistant", state_update=False)
                                        except Exception as mem_err:
                                            logger.warning("主动消息记忆写入失败: %s", mem_err)
                                except Exception as exc:
                                    reason = str(exc)
                                    receipts = will_context.receipts if "will_context" in locals() else []
                                    uncertain = any((row.get("result") or {}).get("status") == "uncertain"
                                                    for row in receipts)
                                    status = "uncertain" if uncertain else "failed"
                                    if "will_context" in locals():
                                        from common.activity import activity_checkpoint
                                        checkpoint = activity_checkpoint(candidate, {
                                            **will_context.activity,
                                            "mode": "pause" if uncertain else "review",
                                            "delay_seconds": 0 if uncertain else 15,
                                            "note": reason[:1000],
                                            "observations": will_context.activity.get("observations") or [],
                                        }, locals().get("outcomes", []))
                                finally:
                                    lease_stop.set()
                                    if lease_thread:
                                        lease_thread.join(timeout=1)
                                    envelope = envelope_for_turn(None, None, "", owner_name=MEMORY_SETTINGS.owner_name).to_dict()
                                    journal = MemoryJournal(memory_outbox, envelope, "will:" + str(candidate.get("id")),
                                                            autonomous=True, candidate=candidate)
                                    journal.capture("autonomous_turn", {}, {"reflection": text, "reason": reason},
                                                    category="turn", status=status, key=journal.prefix + ":turn",
                                                    activity=checkpoint)
                                    from common.proactive import owner_contact
                                    from chatBot.config import OWNER_QQ_IDS
                                    owner_contacted = bool(locals().get("desktop_spoken")) or any(
                                        owner_contact(row["action"], row.get("params") or {}, OWNER_QQ_IDS)
                                        for row in (will_context.receipts if "will_context" in locals() else []))
                                    result = {"id": candidate.get("id"), "timestamp": time.time(),
                                              "status": status, "reason": reason, "reflection": text[:2000],
                                              "owner_contacted": owner_contacted,
                                              "activity": checkpoint}
                                    # The acknowledgement follows the output on the same listener
                                    # queue; publishing a candidate alone is never counted as sent.
                                    _ZMQ_OUTBOUND_QUEUE.put([("bored_result", [
                                        b"bored_result", json.dumps(result).encode(),
                                    ])])
                                    if reserved:
                                        with IS_PROCESSING_DIALOGUE_LOCK:
                                            IS_PROCESSING_DIALOGUE = False
                                        with processing_lock:
                                            processing_state["is_processing"] = False
                            dialogue_queue.submit(process_bored_async)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    print(f"[Thinking] Error processing bored message: {e}", flush=True)

            if sub_socket in events:
                # 接收 Multipart: [topic, payload]
                parts = sub_socket.recv_multipart()
                if len(parts) < 2: continue
                
                topic_str = parts[0].decode('utf-8')
                msg_bytes = parts[1]
                
                try:
                    data = json.loads(msg_bytes.decode('utf-8'))
                    
                    if topic_str == "qq_log":
                        try:
                            _record_qq_background_message(data, executor)
                        except Exception as e:
                            print(f"[Thinking] Error processing qq_log: {e}", flush=True)
                        continue

                    # [修改] 处理系统命令（如来自模块管理的开关控制）
                    sys_cmd = data.get("system_command")
                    if sys_cmd:
                        if sys_cmd == "enable_game_mode":
                            interval = data.get("interval", 0.1)
                            from tools.game_mode_tool import enable_game_mode
                            res = enable_game_mode(interval)
                            # Update global state in handle_zmq.py
                            GAME_MODE_ENABLED = True
                            GAME_MODE_INTERVAL = interval
                            print(f"[Thinking] {res}", flush=True)
                        elif sys_cmd == "disable_game_mode":
                            from tools.game_mode_tool import disable_game_mode
                            res = disable_game_mode()
                            # Update global state in handle_zmq.py
                            GAME_MODE_ENABLED = False
                            print(f"[Thinking] {res}", flush=True)
                        elif sys_cmd == "update_presence":
                            is_online = data.get("user_online", True)
                            state_str = "上线" if is_online else "下线"
                            print(f"[Thinking] 收到用户状态变更通知: 用户已{state_str}", flush=True)
                            # 如果用户下线，可以考虑清空未处理的任务或暂停某些活动
                            # 目前仅作为状态同步和日志记录
                        continue  # 处理完系统命令后跳过后续用户输入处理

                    user_input = data.get("user_input", "").strip()
                    img_descr = data.get("img_descr", "").strip()
                    source = data.get("source")
                    if str(source).lower() == "qq":
                        # Prevent legacy producers from routing QQ into desktop interrupts.
                        continue
                    qq_context = data.get("qq_context", {})

                    if user_input:
                        # Reset idle timer
                        last_interaction_time = time.time()
                        has_triggered_idle = False
                        
                        # 【QQ 消息合并】QQ消息进入合并缓冲，等待窗口到期后再提交处理
                        if source == "QQ":
                            _qq_merge_enqueue(user_input, img_descr, source, qq_context,
                                              executor, processing_lock, processing_state)
                            continue
                        
                        dialogue_queue.submit(
                            process_dialogue, user_input, img_descr, source, qq_context, interrupt=True,
                        )
                except json.JSONDecodeError:
                    pass
            
            else:
                # Timeout / Idle：无用户输入时，若启用实时屏幕分析且分析文件有更新，将分析内容单独发给大模型
                now = time.time()
                if should_run_idle_check(now, last_realtime_screen_check_time, REALTIME_SCREEN_IDLE_CHECK_INTERVAL):
                    is_processing = dialogue_queue.busy

                    # [修改说明] 实时屏幕分析主动推送逻辑：
                    # 1. 只要内容变化就推送
                    # 2. 若正在处理(is_processing)或正在播放(is_player_busy)，则跳过本次推送（等待空闲时再试，或者等下一次内容更新）
                    
                    try:
                        screen_content = read_realtime_screen_analysis_if_enabled(REALTIME_SCREEN_PATHS)

                        update = evaluate_idle_realtime_push(
                            now=now,
                            last_check_time=last_realtime_screen_check_time,
                            last_pushed_content=last_pushed_screen_content,
                            last_push_mtime=last_realtime_screen_push_mtime,
                            screen_content=screen_content,
                            game_mode_enabled=deps.game_mode_enabled,
                            is_processing=is_processing,
                            is_player_busy=is_player_busy,
                        )

                        last_realtime_screen_check_time = float(update["last_check_time"])
                        last_pushed_screen_content = str(update["last_pushed_content"] or "")
                        last_realtime_screen_push_mtime = float(update["last_push_mtime"] or 0.0)

                        if update.get("should_trigger"):
                            synthetic_prompt = str(update.get("synthetic_prompt") or "")

                            dialogue_queue.submit(process_dialogue, synthetic_prompt, "", background=True)
                            print(f"[Thinking] 已推送实时屏幕分析（内容变化，游戏模式: ON）", flush=True)

                    except Exception as e:
                        print(f"[Thinking] 检查/推送实时屏幕分析失败: {e}", flush=True)
                pass 

        except KeyboardInterrupt:
            print("[Thinking] 收到中断信号，停止监听...", flush=True)
            break
        except Exception as e:
            print(f"[Thinking] ZMQ监听错误: {e}", flush=True)
            traceback.print_exc()
            time.sleep(1)  # 避免错误循环

if __name__ == "__main__":
    try:
        print("[Thinking] ========================================", flush=True)
        print("[Thinking] Thinking 进程已启动，等待其他模块就绪...", flush=True)
        print("[Thinking] ========================================", flush=True)
        
        # 等待 Classification 通知「所有模块已就绪」后再正式启动（上线通知、摘要/日记、监听）
        from common.service_ready import managed, mark_ready
        start_port = None if managed() else ZMQ_PORTS["START_THINKING_REP"]
        if start_port:
            start_rep = context.socket(zmq.REP)
            start_rep.setsockopt(zmq.LINGER, 2000)
            try:
                start_rep.bind(f"tcp://*:{start_port}")
                print(f"[Thinking] 等待 Classification 发送正式启动信号 (端口 {start_port})...", flush=True)
                start_rep.recv_string()
                start_rep.send_string("ok")
                print("[Thinking] 已收到正式启动信号，开始初始化记忆系统并上线", flush=True)
                # 建立到 Moments 的 SSE 连接，由后端维护 online 状态
                ensure_status_stream_started(MOMENTS_API_BASE_URL, TOGGLE_STATUS_TOKEN)
                # 实时屏幕分析：在其他模块启动完成后根据保存的配置决定是否启动
                try:
                    manager_url = f"http://{HTTP_HOST}:{HTTP_PORTS['MODULE_MANAGER']}"
                    r = requests.get(f"{manager_url}/api/realtime_screen_ensure_from_config", timeout=3)
                    if r.ok:
                        j = r.json()
                        if j.get("started"):
                            print("[Thinking] 已根据配置启动实时屏幕分析", flush=True)
                        elif j.get("message"):
                            print(f"[Thinking] 实时屏幕分析: {j.get('message')}", flush=True)
                    else:
                        print("[Thinking] 调用模块管理「根据配置确保实时屏幕分析」未成功，可能未开管理页", flush=True)
                except Exception as e:
                    print(f"[Thinking] 调用模块管理确保实时屏幕分析失败（可忽略）: {e}", flush=True)
            except Exception as e:
                print(f"[Thinking] 等待启动信号异常: {e}", flush=True)
            finally:
                try:
                    start_rep.close()
                except Exception:
                    pass
        else:
            print("[Thinking] START_THINKING_REP 未配置，将直接启动", flush=True)
            # 未使用 Classification 启动握手时，同样建立状态 SSE 连接
            ensure_status_stream_started(MOMENTS_API_BASE_URL, TOGGLE_STATUS_TOKEN)
        
        # 正式启动：确认 Memory Server 可用，注册依赖，再启动监听。
        if not wait_for_server(memory_client, timeout_seconds=120):
            print("[Thinking] ⚠ Memory Server 暂不可用：对话可继续，但记忆读写会失败直到其恢复", flush=True)
        deps.register_memory_client(memory_client)
        # The QQ Thinking worker consolidates all channels in both runtime profiles.

        print("[Thinking] ✓ Memory client registered; QQ Thinking owns consolidation", flush=True)
        print("[Thinking] ✓ Tools registered successfully (from tools package)", flush=True)
        print("[Thinking] ✓ PUB/SUB sockets connected to local bus", flush=True)
        print("[Thinking] Starting ZMQ listener...", flush=True)
        print("[Thinking] ========================================", flush=True)
        start_zmq_listener()
    except KeyboardInterrupt:
        print("[Thinking] Interrupted by user", flush=True)
    except Exception as e:
        print(f"[Thinking] CRITICAL ERROR: {e}", flush=True)
        traceback.print_exc()
        input("Press Enter to exit...")
    # print("DEBUG MODE ENABLED: Running single request for '喂喂喂'")
    # process_message("喂喂喂", "Visual context disabled")
