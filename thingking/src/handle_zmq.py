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
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from layered_memory import LayeredMemorySystem
from thinking_model_helper import ThinkingModelHelper
from tool_call_utils import execute_tool_calls, has_async_tools, should_use_thinking_model
from agents import MainAgent, ToolAgent, SummaryAgent, DynamicMemoryToolAgent
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
)
from path_setup import ensure_project_root
from runtime_wiring import register_tool_dependencies, bind_pub_socket_with_retry
from tool_schema_sync import sync_agent_schema_with_registry
from qq_merge_coordinator import QQMergeCoordinator
from message_flow_utils import (
    compute_enable_audio,
    sanitize_user_input,
    build_time_gap_instruction,
    update_screen_monitor_rejection_state,
)
from rag_client import RAGServerClient
from thinking_stream_runner import run_main_agent_rounds
from qq_reply_flow import handle_qq_reply_if_needed
from interrupt_flow import submit_pending_interrupt
from tool_schema_collections import (
    TOOLS_SCHEMA,
    _QQ_ALLOWED_TOOLS,
    ROUTER_TOOLS_SCHEMA,
    ALL_TOOLS_SCHEMA_FOR_AGENT,
)
from tool_mounting import (
    initialize_agent_tool_mounts,
    configure_tool_agent_schema_for_source,
)

# 确保可以从项目根目录导入 config（无论当前工作目录在哪里）
PROJECT_ROOT = ensure_project_root(__file__, 2)

from config import (
    ROOT_DIR,
    ZMQ_HOST,
    ZMQ_PORTS,
    PRESENCE_STATE_PATH,
    DEEPSEEK_API_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_API_TIMEOUT,
    require_env,
    MOMENTS_API_BASE_URL,
    TOGGLE_STATUS_TOKEN,
)
# [工具迁移] relationship_state 的导入已移到 tools/memory_tool.py 中（延迟导入）

# --- [修复补丁 1] 强制设置 Windows 控制台编码，防止 print 中文卡死 ---
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
# -------------------------------------------------------------------
from tools import call_tool, TOOLS_REGISTRY
from tools.dependencies import deps
# 导入从 handle_zmq.py 迁移的工具函数（现在在 tools 包中）
from tools.game_mode_tool import enable_game_mode, disable_game_mode
from tools.memory_tool import update_status
from tools.visual_tool import get_visual_info
from tools.thinking_tool import call_thinking_model
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

# 初始化三代理架构（工具 schema 稍后设置）
main_agent = MainAgent()
tool_agent = ToolAgent()
summary_agent = SummaryAgent()

# [工具迁移] call_thinking_model 已迁移到 tools/thinking_tool.py
# 工具函数定义已移除，现在从 tools 包导入

from qq_buffer_manager import QQBufferManager

# --- 配置 ---
# 主机与端口统一来自全局 config
HOST = ZMQ_HOST

# ZMQ 端口配置（命名保持与原注释一致）
ZMQ_PUB_PORT = ZMQ_PORTS["THINKING_TTS_PUB"]       # Output to TTS (音频请求)
ZMQ_PUB_TEXT_PORT = ZMQ_PORTS["THINKING_TEXT_PUB"] # Output to Display (文本)
ZMQ_SUB_PORT = ZMQ_PORTS["CLASSIFICATION_PUB"]     # Input from Classification
ZMQ_SUB_TOPIC = "classified"
ZMQ_PUB_TOPIC = "think"
ZMQ_PUB_TEXT_TOPIC = "text"  # 文本直接发送到display

# DeepSeek 配置（API Key 仅从环境变量读取）
API_KEY = require_env("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
API_URL = DEEPSEEK_API_URL

# 用注册表补齐遗漏工具，避免 schema 与 registry 双改不一致
ALL_TOOLS_SCHEMA_FOR_AGENT = sync_agent_schema_with_registry(
    ALL_TOOLS_SCHEMA_FOR_AGENT,
    TOOLS_REGISTRY,
    excluded_names={"update_status", "call_summary_agent"},
)

# 分层记忆系统：收到 Classification「所有模块就绪」后再初始化，并执行上线摘要/日记等
memory_system = None

# 初始化 ZMQ 上下文
context = zmq.Context()

# [依赖注入] 注册工具依赖，避免循环导入（memory_system 在正式启动时注册）
register_tool_dependencies(
    deps,
    context,
    thinking_model_helper,
    get_tools_schema=lambda: TOOLS_SCHEMA,
    get_tools_registry=lambda: TOOLS_REGISTRY,
)

# 工具模型可调用工具：单一 schema 文件 server/tool_agent_schema.json，每项含 name/description/enabled；不依赖 Thinking 启动即可在模块管理页显示与修改
TOOL_AGENT_SCHEMA_PATH = PROJECT_ROOT / "server" / "tool_agent_schema.json"
# 实时屏幕分析：配置与结果文件由 realtime_screen_analysis_standalone.py 与模块管理页维护
REALTIME_SCREEN_PATHS = build_realtime_screen_paths(PROJECT_ROOT)


initialize_agent_tool_mounts(
    main_agent=main_agent,
    tool_agent=tool_agent,
    router_tools_schema=ROUTER_TOOLS_SCHEMA,
    tool_agent_schema_path=TOOL_AGENT_SCHEMA_PATH,
    all_tools_schema_for_agent=ALL_TOOLS_SCHEMA_FOR_AGENT,
)

# PUB: 发送思考结果到TTS（音频请求）
max_bind_retries = 3
bind_retry_delay = 1.0
pub_socket = bind_pub_socket_with_retry(
    context,
    ZMQ_PUB_PORT,
    "THINKING_TTS_PUB",
    max_retries=max_bind_retries,
    retry_delay=bind_retry_delay,
)

# PUB: 发送文本到Display（直接发送文本，不经过TTS）
pub_text_socket = bind_pub_socket_with_retry(
    context,
    ZMQ_PUB_TEXT_PORT,
    "THINKING_TEXT_PUB",
    max_retries=max_bind_retries,
    retry_delay=bind_retry_delay,
)

# PUB: 播放本地音频（sing 工具）发往 Display
ZMQ_AUDIO_PLAY_PUB_PORT = ZMQ_PORTS.get("AUDIO_PLAY_PUB", 5563)
audio_play_pub_socket = bind_pub_socket_with_retry(
    context,
    ZMQ_AUDIO_PLAY_PUB_PORT,
    "AUDIO_PLAY_PUB",
    max_retries=max_bind_retries,
    retry_delay=bind_retry_delay,
)
try:
    from tools.dependencies import deps
    deps.register_audio_play_pub_socket(audio_play_pub_socket)
except Exception as e:
    print(f"[Thinking] 注册 audio_play_pub_socket 失败: {e}", flush=True)

# SUB: 接收分类器结果
sub_socket = context.socket(zmq.SUB)
sub_socket.connect(f"tcp://{HOST}:{ZMQ_SUB_PORT}")
sub_socket.setsockopt_string(zmq.SUBSCRIBE, ZMQ_SUB_TOPIC)
# [新增] 订阅 QQ 后台日志话题
sub_socket.setsockopt_string(zmq.SUBSCRIBE, "qq_log")

# [新增] 连接模块管理器的控制端口 (5566)，用于接收系统命令 (如游戏模式开关)
MODULE_MANAGER_PUB_PORT = 5566
sub_socket.connect(f"tcp://{HOST}:{MODULE_MANAGER_PUB_PORT}")
sub_socket.setsockopt_string(zmq.SUBSCRIBE, "syscmd")

# SUB: 接收bored检测器的消息
BORED_SUB_PORT = ZMQ_PORTS["BORED_PUB"]
bored_sub_socket = context.socket(zmq.SUB)
bored_sub_socket.connect(f"tcp://{HOST}:{BORED_SUB_PORT}")
bored_sub_socket.setsockopt_string(zmq.SUBSCRIBE, "bored")


def _is_only_action_or_empty(text):
    """若文本为空或仅含括号内动作描述（如（点头）（微笑）），返回 True，不应发送到 TTS/展示。"""
    if not text or not str(text).strip():
        return True
    t = re.sub(r'[（(][^）)]*[）)]', '', str(text).strip())
    return not t or not t.strip()


def zmq_send(data, lang="zh", translation=None, cough=None, origin=None, enable_audio=True):
    """
    向 ZMQ 发送数据
    【重构】文本直接发送到Display，音频请求发送到TTS
    """
    try:
        # 1. 文本直接发送到Display（端口5553，topic="text"）
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
            
            # 发送文本到Display
            pub_text_socket.send_multipart([ZMQ_PUB_TEXT_TOPIC.encode('utf-8'), json.dumps(text_payload).encode('utf-8')])
            
            # 更新分段索引
            if not hasattr(zmq_send, '_segment_index'):
                zmq_send._segment_index = 0
            zmq_send._segment_index += 1
        
        # 2. 音频请求发送到TTS（端口5552，topic="think"）
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
            
            # 发送音频请求到TTS
            pub_socket.send_multipart([ZMQ_PUB_TOPIC.encode('utf-8'), json.dumps(tts_payload).encode('utf-8')])
        
        return True
    except Exception as e:
        print(f"[Thinking] ZMQ发送异常: {str(e)}", flush=True)
        traceback.print_exc()
    return False


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


def _execute_router_tool_calls_and_append(messages: list, tool_calls_list: list, base_messages: list, interrupt_callback=None, source: str = None) -> None:
    """
    执行主模型（路由层）返回的 tool_calls：仅 get_time / call_tool_agent / call_summary_agent。
    向 messages 追加每条 role=tool 消息（调用方需已追加含 tool_calls 的 assistant 消息）。
    source: 消息来源（"QQ" 时仅允许 QQ 相关工具）
    """
    for tc in tool_calls_list:
        call_id = tc.get("id") or ""
        func_name = (tc.get("function") or {}).get("name", "")
        args_str = (tc.get("function") or {}).get("arguments", "") or "{}"
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {}
        if not func_name:
            messages.append({"role": "tool", "tool_call_id": call_id, "content": "Error: 缺少工具名"})
            continue
        if func_name == "get_time":
            result = call_tool("get_time", **args)
        elif func_name == "call_tool_agent":
            task_desc = args.get("task_description", "")
            context = args.get("context", "")
            try:
                # 运行中动态生效：每次调用前按当前配置重新挂载工具列表
                active_schema, full_count, is_qq_mode = configure_tool_agent_schema_for_source(
                    tool_agent=tool_agent,
                    tool_agent_schema_path=TOOL_AGENT_SCHEMA_PATH,
                    all_tools_schema_for_agent=ALL_TOOLS_SCHEMA_FOR_AGENT,
                    source=source,
                    qq_allowed_tools=_QQ_ALLOWED_TOOLS,
                )
                if is_qq_mode:
                    print(f"[Thinking] QQ 来源，工具已过滤为 QQ 白名单 ({len(active_schema)}/{full_count} 个工具)", flush=True)
                # 【修改】传入中断检查回调，允许用户打断工具执行
                result = tool_agent.handle_task(task_desc, context, base_messages, interrupt_check=interrupt_callback)
            except Exception as e:
                result = f"工具代理执行出错：{e}"
        elif func_name == "call_summary_agent":
            state_desc = args.get("state_update_description", "")
            context = args.get("context", "")
            try:
                result = summary_agent.update_state(state_desc, context, base_messages)
            except Exception as e:
                result = f"摘要代理执行出错：{e}"
        else:
            result = f"Error: 未知工具 '{func_name}'（路由层仅支持 get_time / call_tool_agent / call_summary_agent）"
        messages.append({"role": "tool", "tool_call_id": call_id, "content": str(result)})
        print(f"[Thinking] 路由工具执行: {func_name}", flush=True)


# 重置分段索引的函数（在新对话开始时调用）
def reset_segment_index():
    """重置文本分段索引"""
    if not hasattr(zmq_send, '_segment_index'):
        zmq_send._segment_index = 0
    else:
        zmq_send._segment_index = 0


# 创建控制信号发送 socket（用于向 hearing 发送 cough 信号）
# 使用专用端口，避免与 guidisplay（CONTROL_PUB）和手动输入端口冲突
CONTROL_PORT_THINKING = ZMQ_PORTS.get("CONTROL_PUB_THINKING", 5561)
control_pub_socket = None
def get_control_socket():
    """获取或创建控制信号发送 socket（Thinking 专用控制端口）"""
    global control_pub_socket
    if control_pub_socket is None:
        try:
            control_pub_socket = context.socket(zmq.PUB)
            control_pub_socket.setsockopt(zmq.LINGER, 0)
            control_pub_socket.setsockopt(zmq.IMMEDIATE, 1)
            control_pub_socket.bind(f"tcp://*:{CONTROL_PORT_THINKING}")
            print(f"[Thinking] 控制信号 socket 已绑定到端口 {CONTROL_PORT_THINKING}", flush=True)
        except Exception as e:
            print(f"[Thinking] 创建/绑定控制 socket 失败: {e}", flush=True)
            control_pub_socket = None
    return control_pub_socket

def send_control_signal(cough_status):
    """发送控制信号到 hearing 模块"""
    socket = get_control_socket()
    if socket:
        try:
            control_data = {"cough": cough_status}
            socket.send_multipart([b"control", json.dumps(control_data).encode('utf-8')], flags=zmq.DONTWAIT)
            print(f"[Thinking] 已发送控制信号 (cough={cough_status})", flush=True)
        except Exception as e:
            print(f"[Thinking] 发送控制信号失败: {e}", flush=True)

def _analyze_qq_images(image_urls, timeout=25):
    """
    [新增] 下载并分析 QQ 消息中的图片，返回图片描述文本。
    使用 DashScope VLM (与 screen_tool 相同的模型) 进行识别。
    
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
        from config import (
            DASHSCOPE_API_URL,
            DASHSCOPE_API_KEY,
            DASHSCOPE_VISION_MODEL,
            DASHSCOPE_API_TIMEOUT,
            require_env,
        )
    except ImportError as e:
        print(f"[QQ Image] Import error: {e}", flush=True)
        return ""
    
    api_key = require_env("DASHSCOPE_API_KEY", DASHSCOPE_API_KEY)
    if not api_key:
        print("[QQ Image] No DASHSCOPE_API_KEY configured, skipping image analysis", flush=True)
        return ""
    
    descriptions = []
    
    for i, url in enumerate(image_urls[:3]):  # 最多分析 3 张图，防止 Token 爆炸
        try:
            # 尝试直接使用 URL 调用 VLM（DashScope 支持 URL 输入）
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
            
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": DASHSCOPE_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "请详细描述这张图片的内容。如果包含文字请提取出来。如果是表情包/梗图请描述其含义。简洁回复，不超过100字。"
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_content
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 200
            }
            
            resp = _requests.post(
                DASHSCOPE_API_URL, 
                headers=headers, 
                json=payload, 
                timeout=timeout,
                proxies={"http": None, "https": None}
            )
            
            if resp.status_code == 200:
                result = resp.json()
                desc = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                if desc:
                    descriptions.append(f"[图片{i+1}内容: {desc.strip()}]")
                    print(f"[QQ Image] Image {i+1} analyzed: {desc[:50]}...", flush=True)
                else:
                    descriptions.append(f"[图片{i+1}: 无法识别内容]")
            else:
                print(f"[QQ Image] VLM returned status {resp.status_code}", flush=True)
                descriptions.append(f"[图片{i+1}: 识别失败]")
                
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
    # 【新增】设置全局状态标签：开始处理对话
    global IS_PROCESSING_DIALOGUE, INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
    global _CURRENT_B_TASK_ID, _CANCELLED_B_TASK_IDS
    with IS_PROCESSING_DIALOGUE_LOCK:
        IS_PROCESSING_DIALOGUE = True
    
    exited_due_to_interrupt = False  # 是否因用户打断而退出本轮
    
    # QQ消息不发送TTS语音，同时支持 "QQ" 和 "qq"；ASR/MANUAL 强制开启
    enable_audio = compute_enable_audio(source)

    # 定义中断检查函数，供 ToolAgent 和 MainAgent 在长时间运行时回调
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
        
        # 更新全局视觉信息供工具调用
        global CURRENT_VISUAL_INFO
        CURRENT_VISUAL_INFO = img_descr if img_descr else "当前眼前一片漆黑，什么也看不见。"

        # --- 日期处理与抱怨逻辑 (完全保留原代码逻辑) ---
        current_date_obj = datetime.datetime.now()
        # [修改] 增加时分秒
        current_date_str = current_date_obj.strftime("%Y/%m/%d %H:%M")
        
        formatted_user_input = f"[{current_date_str}]: [{user_input}]"
        
        time_gap_instruction = build_time_gap_instruction(memory_system, user_input, current_date_obj)
        if not isinstance(time_gap_instruction, str):
            time_gap_instruction = ""

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
        
        # 获取上下文（不擅长/拒绝逻辑已移至 MainAgent.get_system_prompt，由模型按语义判断）
        # [修改] 传递 QQ 上下文数据以拉取相关历史
        qq_ctx = None
        if source == "QQ" and extra_data:
             qq_ctx = extra_data.get("qq_context") or extra_data
        
        context_messages = memory_system.get_full_context_messages(user_input, qq_context_data=qq_ctx)
        
        if time_gap_instruction:
            context_messages.append({"role": "system", "content": time_gap_instruction})
        
        # [新增] QQ 图片消息处理：分析图片并将描述注入上下文
        if source == "QQ" and qq_ctx:
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
            
        # [修改] 全局回复格式要求：始终使用双空格
        fmt_instruction = "\n【回复格式要求】请使用**双空格**（  ）作为句子分隔符（不要使用单空格），以便语音合成模块正确断句。"
        
        # [修改] 如果是 QQ 消息，注入简洁要求 + QQ 工具约束
        if source == "QQ":
            # 检查管理员权限
            if qq_ctx and qq_ctx.get("is_admin"):
                 context_messages.append({"role": "system", "content": "【系统提示】此消息来自管理员，请高度重视并优先响应。"})
            
            # QQ 工具约束：禁止使用与 QQ 无关的工具
            context_messages.append({"role": "system", "content": (
                "【QQ消息工具约束】当前消息来自QQ远程用户，你无法通过屏幕或摄像头看到对方。\n"
                "- 禁止调用 get_screen_info、get_visual_info 等视觉/屏幕工具（对方是远程QQ用户，看屏幕毫无意义）\n"
                "- 禁止调用 automate_action、automate_sequence、enable_game_mode 等本地操作工具\n"
                "- 允许使用的工具仅限：QQ消息工具（send_qq_private_msg、send_qq_group_msg、get_qq_friend_list、get_qq_group_list、broadcast_to_all_friends、broadcast_to_all_groups、at_each_group_member）、browser_search（搜索信息）、get_time（获取时间）、动态工具（get_moments、add_moment、comment_moment）\n"
                "- 如果QQ用户的请求不需要以上工具即可回答（如闲聊、问答），请直接回复，不要调用 call_tool_agent"
            )})
            
            # QQ回复格式要求
            fmt_instruction += "\n鉴于QQ聊天场景，回复必须**极度简洁**，废话少说，直击重点。"
        
        context_messages.append({"role": "system", "content": fmt_instruction})

        # 实时屏幕分析：若启用则把最新分析作为系统消息注入，与用户输入一起发给主模型
        is_realtime_screen_active = False
        rt_text = read_realtime_screen_analysis_if_enabled(REALTIME_SCREEN_PATHS)
        if rt_text:
            # 【修改】强化 Prompt，明确告知模型已获得最新画面，无需再次调用工具
            context_messages.append({"role": "system", "content": f"【实时屏幕分析（已是最新画面）】\n{rt_text}\n(系统提示：上述内容即为当前用户屏幕的实时画面分析结果。你已拥有最新的视觉信息，**严禁**重复调用 get_screen_info / get_visual_info / call_tool_agent(visual_tool) 等工具来获取屏幕内容，直接使用上述信息回答即可。)"})
            is_realtime_screen_active = True
        
        # 消息体构建
        # 仅当有有效的视觉描述时才附加到 Prompt 中
        # 【修改】如果实时屏幕分析已启用且有效，则忽略旧的缓存描述，避免提示词冲突导致模型困惑
        if is_realtime_screen_active:
            current_content = f"{formatted_user_input}"
        elif img_descr and len(img_descr.strip()) > 1 and "暂无" not in img_descr:
             current_content = f"{formatted_user_input}。(注意：系统提供的[用户当前环境解析]是基于历史画面的缓存，可能已过时。如果用户要求你此时观看、确认或描述，请务必调用视觉工具重新获取实时画面！\n缓存环境解析:{img_descr})"
        else:
             current_content = f"{formatted_user_input}"
             
        messages = context_messages.copy()
        messages.append({"role": "user", "content": current_content})

        # === 原生 Tool Calling：主模型挂载 call_tool_agent / call_summary_agent / get_time，根据 tool_calls 执行并循环 ===
        def _interrupt_check():
            with INTERRUPT_LOCK:
                return INTERRUPT_REQUESTED

        def _execute_router_tools_adapter(msgs, tool_calls, interrupt_cb, src):
            _execute_router_tool_calls_and_append(
                msgs,
                tool_calls,
                msgs,
                interrupt_callback=interrupt_cb,
                source=src,
            )

        stream_result = run_main_agent_rounds(
            messages=messages,
            main_agent=main_agent,
            execute_router_tool_calls=_execute_router_tools_adapter,
            zmq_send=zmq_send,
            is_only_action_or_empty=_is_only_action_or_empty,
            enable_audio=enable_audio,
            source=source,
            interrupt_requested=_interrupt_check,
            max_tool_rounds=5,
        )
        full_raw_response = stream_result.get("full_raw_response", "")
        exited_due_to_interrupt = stream_result.get("exited_due_to_interrupt", False)

        to_save = full_raw_response or ""
        if not _is_only_action_or_empty(to_save):
            if source not in ["QQ", "qq"]:
                memory_system.add_interaction(formatted_user_input, to_save)
            else:
                print(f"[Thinking] QQ 消息不写入主短期记忆 (已由 QQ Buffer 管理)", flush=True)

        # [QQ回复] 独立于 _is_only_action_or_empty 判断，确保 QQ 消息始终被发送
        # 但如果是被打断的截断回复，则不发送（避免发送不完整的半句话）
        handle_qq_reply_if_needed(
            source=source,
            extra_data=extra_data,
            to_save=to_save,
            exited_due_to_interrupt=exited_due_to_interrupt,
            send_qq_reply_func=send_qq_reply,
            get_qq_buffer_manager_func=get_qq_buffer_manager,
        )

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
        print(f"[Thinking] [Error] {str(e)}", flush=True)
        traceback.print_exc()
        # 发生错误时，务必发送结束信号
        zmq_send("", lang="zh", cough="end", enable_audio=enable_audio)
    finally:
        send_control_signal("end")
        with IS_PROCESSING_DIALOGUE_LOCK:
            IS_PROCESSING_DIALOGUE = False
        # 【打断】若有暂存的新用户输入，提交为新任务（先释放 processing 状态，再提交，避免 process_pending 误判仍为处理中）
        with INTERRUPT_LOCK:
            pending = PENDING_INTERRUPT_INPUT
            INTERRUPT_REQUESTED = False
            PENDING_INTERRUPT_INPUT = None
        submitted_input = submit_pending_interrupt(
            pending=pending,
            executor=_EXECUTOR_FOR_INTERRUPT,
            processing_lock=PROCESSING_LOCK,
            processing_state=PROCESSING_STATE,
            process_message=process_message,
        )
        if submitted_input:
            print(f"[Thinking] 已提交打断后的新输入: {submitted_input[:30]}...", flush=True)


def screen_monitor_thread():
    """
    后台屏幕监控线程：定期截取屏幕并分析，主动触发对话
    支持游戏模式：低延迟、高频率监控
    """
    global LAST_SCREEN_HASH, LAST_SCREEN_CHECK_TIME
    
    import hashlib
    
    print("[Screen Monitor] 屏幕监控线程已启动", flush=True)
    if GAME_MODE_ENABLED:
        print(f"[Screen Monitor] 游戏模式已启用，监控间隔: {GAME_MODE_INTERVAL}秒", flush=True)
    
    while True:
        try:
            if not SCREEN_MONITOR_ENABLED:
                time.sleep(5)
                continue
            
            # 根据模式选择监控间隔
            monitor_interval = GAME_MODE_INTERVAL if GAME_MODE_ENABLED else SCREEN_MONITOR_INTERVAL
            
            # 检查是否到了监控时间
            now = time.time()
            if now - LAST_SCREEN_CHECK_TIME < monitor_interval:
                # 游戏模式使用更短的休眠时间
                sleep_time = 0.01 if GAME_MODE_ENABLED else 1
                time.sleep(sleep_time)
                continue
            
            LAST_SCREEN_CHECK_TIME = now
            
            # 【修复】检查用户是否明确拒绝屏幕监控
            global SCREEN_MONITOR_USER_REJECTED, SCREEN_MONITOR_REJECT_EXPIRE_TIME
            if SCREEN_MONITOR_USER_REJECTED:
                # 检查拒绝状态是否过期
                if time.time() > SCREEN_MONITOR_REJECT_EXPIRE_TIME:
                    SCREEN_MONITOR_USER_REJECTED = False
                    print(f"[Screen Monitor] 拒绝状态已过期，恢复屏幕监控", flush=True)
                else:
                    # 用户明确拒绝，跳过本次监控
                    time.sleep(1)
                    continue
            
            # 快速截屏并计算哈希（用于检测变化）
            try:
                import pyautogui
                screenshot = pyautogui.screenshot()
                
                # 缩小图片以加快哈希计算
                screenshot.thumbnail((320, 240))
                img_bytes = screenshot.tobytes()
                current_hash = hashlib.md5(img_bytes).hexdigest()
                
                # 如果屏幕内容发生变化
                if LAST_SCREEN_HASH is not None and current_hash != LAST_SCREEN_HASH:
                    # 【修复】取消屏幕变化时向大模型发送提示词的功能，只让大模型自己调用
                    # 只记录屏幕变化，不主动触发对话，让大模型自己决定是否需要查看屏幕
                    print(f"[Screen Monitor] 检测到屏幕变化（哈希值已更新）", flush=True)
                    # 不再主动调用 process_message()，让大模型自己决定是否调用屏幕分析工具
                
                LAST_SCREEN_HASH = current_hash
                
            except ImportError:
                print("[Screen Monitor] pyautogui 未安装，屏幕监控功能不可用", flush=True)
                time.sleep(60)  # 如果依赖缺失，减少检查频率
            except Exception as e:
                print(f"[Screen Monitor] 截屏时出错: {e}", flush=True)
                time.sleep(5)
            
            time.sleep(1)  # 短暂休眠，避免占用过多CPU
            
        except Exception as e:
            print(f"[Screen Monitor] 监控线程错误: {e}", flush=True)
            traceback.print_exc()
            time.sleep(5)


# Global QQ Buffer Manager
_qq_buffer_manager_instance = None

def get_qq_buffer_manager():
    global _qq_buffer_manager_instance
    if _qq_buffer_manager_instance is None:
        # Lazy initialization
        rag_port = ZMQ_PORTS.get('RAG_SERVER_REQREP', 5560)
        # RAGClient needs global 'context' from handle_zmq.py
        rag_client = RAGServerClient(context, HOST, rag_port)
        # summary_agent is a global variable from handle_zmq.py
        _qq_buffer_manager_instance = QQBufferManager(summary_agent, rag_client)
    return _qq_buffer_manager_instance

def start_zmq_listener():
    global INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT, _EXECUTOR_FOR_INTERRUPT
    global GAME_MODE_ENABLED, GAME_MODE_INTERVAL
    print(f"[Thinking] Listening on ZMQ SUB: {ZMQ_SUB_PORT}", flush=True)
    print(f"[Thinking] Publishing to ZMQ PUB: {ZMQ_PUB_PORT}", flush=True)

    # 启动屏幕监控线程
    if SCREEN_MONITOR_ENABLED:
        monitor_thread = threading.Thread(target=screen_monitor_thread, daemon=True)
        monitor_thread.start()
        print(f"[Screen Monitor] 屏幕监控已启用，监控间隔: {SCREEN_MONITOR_INTERVAL}秒", flush=True)
    
    # RAG GC 与日记归纳由 LayeredMemorySystem.start_maintenance_services() 在初始化时启动，此处不再重复
    
    # 【新增】系统启动完成后，向主模型发送用户上线的系统提示
    def send_user_online_notification():
        """延迟发送用户上线通知，确保系统完全启动"""
        # 等待3秒，确保所有线程和系统都已完全启动
        time.sleep(3)
        try:
            # 发送用户上线的系统提示
            online_prompt = "[System Event: User Online] (The user has just come online. You should greet them naturally based on your persona and the current time. You can check the time, mention how long it's been since you last saw them, or simply greet them warmly.)"
            print(f"[Thinking] 发送用户上线系统提示...", flush=True)
            process_message(online_prompt, "")
            print(f"[Thinking] 用户上线系统提示已发送", flush=True)
        except Exception as e:
            print(f"[Thinking] 发送用户上线提示时出错: {e}", flush=True)
            traceback.print_exc()
    
    # 在后台线程中发送用户上线通知
    online_notification_thread = threading.Thread(target=send_user_online_notification, daemon=True)
    online_notification_thread.start()
    print(f"[Thinking] 用户上线通知线程已启动，将在3秒后发送", flush=True)

    # 【修复】创建线程池用于异步处理消息，避免阻塞主循环
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ThinkingWorker")
    global _EXECUTOR_FOR_INTERRUPT
    _EXECUTOR_FOR_INTERRUPT = executor  # 供打断后提交新任务
    # 【修复】使用模块级锁与状态，与 process_message 打断后提交共用
    processing_lock = PROCESSING_LOCK
    processing_state = PROCESSING_STATE

    # Control SUB: Receive play status from Display
    ZMQ_CONTROL_PUB = ZMQ_PORTS.get("CONTROL_PUB", 5565)
    control_sub_socket = context.socket(zmq.SUB)
    control_sub_socket.connect(f"tcp://{HOST}:{ZMQ_CONTROL_PUB}")
    control_sub_socket.setsockopt_string(zmq.SUBSCRIBE, "control")

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
    
    while True:
        try:
            # 【修复】减少poll超时到50ms，提高响应速度
            events = dict(poller.poll(50))

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
                            # 用户下线状态下忽略 bored，不发起主动对话
                            if not is_user_online(PRESENCE_STATE_PATH):
                                continue
                            # 检查是否正在处理对话
                            with IS_PROCESSING_DIALOGUE_LOCK:
                                is_processing = IS_PROCESSING_DIALOGUE
                            
                            if is_processing:
                                print(f"[Thinking] Received bored message but currently processing dialogue, rejecting...", flush=True)
                            else:
                                bored_log, bored_prompt = build_bored_prompt(data)
                                print(bored_log, flush=True)
                                
                                # 触发屏幕分析刷新
                                try:
                                    if send_realtime_screen_flush_signal(REALTIME_SCREEN_PATHS):
                                        print(f"[Thinking] Sent flush signal to Realtime Screen Analysis. Waiting for update...", flush=True)
                                    else:
                                        print(f"[Thinking] Failed to send flush signal", flush=True)

                                    # [优化] 等待分析脚本响应：
                                    # 分析脚本是轮询的，可能需要一点时间来感知信号并写入文件。
                                    # 给它 1~2 秒的反应时间，尽量让本次 LLM 调用能读取到最新的屏幕分析结果。
                                    time.sleep(1.5)

                                except Exception as e:
                                    print(f"[Thinking] Failed to send flush signal: {e}", flush=True)

                                # 异步处理bored消息
                                def process_bored_async():
                                    with processing_lock:
                                        if processing_state["is_processing"]:
                                            print(f"[Thinking] Another message is processing, skipping bored message", flush=True)
                                            return
                                        processing_state["is_processing"] = True
                                    try:
                                        process_message(bored_prompt, "")
                                    finally:
                                        with processing_lock:
                                            processing_state["is_processing"] = False
                                
                                executor.submit(process_bored_async)
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
                        # 处理 QQ 背景消息记录 (存储到 RAG Buffer)
                        try:
                            content = data.get("content", "")
                            meta = {
                                "timestamp": data.get("timestamp"),
                                "sender": data.get("sender"),
                                "group_id": data.get("group_id"),
                                "user_id": data.get("user_id"),
                                "is_group": data.get("is_group", False)
                            }
                            
                            # 获取或初始化 Buffer Manager
                            buf_mgr = get_qq_buffer_manager()
                            
                            should_process = buf_mgr.add_message(content, meta)
                            
                            if should_process:
                                print(f"[Thinking] QQ Buffer full, triggering processing...", flush=True)
                                
                                # 在后台线程执行总结和入库，避免阻塞主循环
                                def _run_buffer_process():
                                    try:
                                        buf_mgr.process_buffer()
                                    except Exception as e:
                                        print(f"[Thinking] Buffer processing failed: {e}", flush=True)
                                        
                                executor.submit(_run_buffer_process)

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
                    qq_context = data.get("qq_context", {})

                    if user_input:
                        # [新增逻辑] 如果是 QQ 消息，无论是否主动交互，都存入 Buffer 以便 RAG 检索
                        if source == "QQ":
                            try:
                                qq_ctx = data.get("qq_context") or {}
                                # 构造 meta 信息
                                meta = {
                                    "timestamp": time.time(), # 或从 qq_ctx 获取
                                    "sender": qq_ctx.get("sender", "User"),
                                    "group_id": qq_ctx.get("group_id"),
                                    "user_id": qq_ctx.get("user_id"),
                                    # 如果有 group_id 则视为群聊，否则私聊
                                    "is_group": bool(qq_ctx.get("group_id"))
                                }
                                # 调用 buffer manager 存储
                                buf_mgr = get_qq_buffer_manager()
                                # 注意：这里只 add 不 process，避免打断对话流，让后台线程或满阈值时处理
                                buf_mgr.add_message(user_input, meta)
                                print(f"[Thinking] 已将主动 QQ 消息存入 Buffer (Private/Group)", flush=True)
                            except Exception as e:
                                print(f"[Thinking] 旁路记录 QQ 消息失败: {e}", flush=True)

                        # [QQ消息处理] 如果是QQ消息，在 user_input 前附加来源信息，以便模型感知
                        if source == "QQ":
                            # 如果 classification server 已经构造了带 [QQ] 的 input_text，这里其实可能不需要再构造一遍
                            # 但如果有 qq_context，我们需要保留并在 process_message 中使用它来回复
                            pass

                        # Reset idle timer
                        last_interaction_time = time.time()
                        has_triggered_idle = False
                        
                        # 【QQ 消息合并】QQ消息进入合并缓冲，等待窗口到期后再提交处理
                        if source == "QQ":
                            _qq_merge_enqueue(user_input, img_descr, source, qq_context,
                                              executor, processing_lock, processing_state)
                            continue
                        
                        # 【打断机制】若上一轮仍在处理，则打断并暂存本次输入，由 process_message 收尾后再处理
                        with IS_PROCESSING_DIALOGUE_LOCK:
                            is_processing = IS_PROCESSING_DIALOGUE
                        if is_processing:
                            with INTERRUPT_LOCK:
                                INTERRUPT_REQUESTED = True
                                # 【修复】如果已有 pending 且来源相同，追加而非覆盖
                                if PENDING_INTERRUPT_INPUT is not None:
                                    prev = PENDING_INTERRUPT_INPUT
                                    prev_text = prev[0] if len(prev) >= 1 else ""
                                    PENDING_INTERRUPT_INPUT = (prev_text + "\n" + user_input, img_descr or "", source, qq_context)
                                else:
                                    PENDING_INTERRUPT_INPUT = (user_input, img_descr or "", source, qq_context)
                            with _B_TASK_LOCK:
                                if _CURRENT_B_TASK_ID:
                                    _CANCELLED_B_TASK_IDS.add(_CURRENT_B_TASK_ID)
                            print(f"[Thinking] 打断上一轮处理，新输入将在本轮收尾后处理: {user_input[:30]}...", flush=True)
                            continue
                        
                        # 【修复】闭包变量捕获问题：显式传递参数
                        def process_async(u=user_input, i=img_descr, s=source, e=qq_context):
                            # [调试日志] 打印每个任务的语音开关状态
                            audio_flag = (s not in ["QQ", "qq"])
                            print(f"[Thinking DEBUG] process_async: source={s}, enable_audio={audio_flag}", flush=True)
                            with processing_lock:
                                if processing_state["is_processing"]:
                                    print(f"[Thinking] 上一个消息正在处理中，跳过: {u[:20]}...", flush=True)
                                    return
                                processing_state["is_processing"] = True
                            try:
                                process_message(u, i, source=s, extra_data=e)
                            finally:
                                with processing_lock:
                                    processing_state["is_processing"] = False
                        
                        executor.submit(process_async)
                except json.JSONDecodeError:
                    pass
            
            else:
                # Timeout / Idle：无用户输入时，若启用实时屏幕分析且分析文件有更新，将分析内容单独发给大模型
                now = time.time()
                if should_run_idle_check(now, last_realtime_screen_check_time, REALTIME_SCREEN_IDLE_CHECK_INTERVAL):
                    with processing_lock:
                        is_processing = processing_state["is_processing"]

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

                            def process_screen_only_async():
                                with processing_lock:
                                    if processing_state["is_processing"]:
                                        return
                                    processing_state["is_processing"] = True
                                try:
                                    process_message(synthetic_prompt, "")
                                finally:
                                    with processing_lock:
                                        processing_state["is_processing"] = False

                            executor.submit(process_screen_only_async)
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
        start_port = ZMQ_PORTS.get("START_THINKING_REP")
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
                    r = requests.get("http://127.0.0.1:5000/api/realtime_screen_ensure_from_config", timeout=3)
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
        
        # 正式启动：初始化记忆系统（触发上线摘要/日记等），注册依赖，再启动监听
        # 在 __main__ 顶层赋值即更新模块级变量，无需 global
        memory_system = LayeredMemorySystem()
        deps.register_memory_system(memory_system)
        memory_system.start_maintenance_services()  # RAG GC + 日记归纳后台线程
        # 副工具模型（reasoner）：仅挂载动态记忆工具，摘要写入 buffer RAG 后由其更新 memory_dynamic
        dynamic_memory_agent = DynamicMemoryToolAgent()
        memory_system.set_dynamic_memory_agent(dynamic_memory_agent)
        
        print("[Thinking] ✓ Memory system initialized (上线摘要/日记已执行)", flush=True)
        print("[Thinking] ✓ Tools registered successfully (from tools package)", flush=True)
        print(f"[Thinking] ✓ SUB socket ready: tcp://{HOST}:{ZMQ_SUB_PORT}", flush=True)
        print(f"[Thinking] ✓ PUB socket ready: tcp://*:{ZMQ_PUB_PORT}", flush=True)
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