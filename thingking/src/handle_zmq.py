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
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from layered_memory import LayeredMemorySystem
from thinking_model_helper import ThinkingModelHelper
from tool_call_utils import execute_tool_calls, has_async_tools, should_use_thinking_model
from agents import MainAgent, ToolAgent, SummaryAgent, DynamicMemoryToolAgent
from tool_agent_schema import get_tool_agent_schema_filtered
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
from runtime_wiring import register_tool_dependencies, bind_pub_socket_with_retry

# 确保可以从项目根目录导入 config（无论当前工作目录在哪里）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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

# 将项目根目录加入 path 以便导入 tools
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
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
_QQ_MERGE_LOCK = threading.Lock()
# key = (group_id, user_id) 的元组
# value = {"texts": [str], "timer": Timer|None, "context": (img_descr, source, qq_context)|None}
_QQ_MERGE_SLOTS: dict = {}

# 【双 Chat】模型 B 任务 ID：若用户打断则取消当前 B，丢弃其输出
_CURRENT_B_TASK_ID = None
_CANCELLED_B_TASK_IDS = set()
_B_TASK_LOCK = threading.Lock()

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

# 工具定义
# 【三代理架构】主模型可见：get_time、open_application、automate_action、automate_sequence、call_tool_agent（browser_search 已移至工具模型）
# 浏览器相关请求由主模型通过 call_tool_agent 交给工具模型，由工具模型调用 browser_search
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前系统时间和日期 (Get current system time)",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "打开应用程序或文件（不含浏览器）。当用户要求打开应用程序或文件时（如'打开记事本'、'打开计算器'、'打开 VSCode'），使用此工具。与浏览器相关的操作（打开浏览器、网页搜索、查资料）必须使用 call_tool_agent，不要用本工具。Open an application or file (NOT browser). For browser or web search use call_tool_agent. This tool runs in background.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name_or_path": {
                        "type": "string",
                        "description": "应用程序名称（如 'notepad', 'calc', 'vscode'）或完整路径。打开浏览器/搜索请用 call_tool_agent。Application name or full path. Use call_tool_agent for browser."
                    },
                    "arguments": {
                        "type": "string",
                        "description": "可选。传递给应用程序的参数，例如要打开的文件路径。Optional. Arguments to pass to the application, e.g. a file path to open."
                    }
                },
                "required": ["app_name_or_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "automate_action",
            "description": "在非浏览器类应用程序中执行自动化操作（打字、按键、点击、写入、画图、等待、移动等）。禁止用于浏览器：任何浏览器相关操作（打开、搜索、点击网页等）仅使用 browser_search，不得使用本工具或屏幕坐标操作浏览器。仅当用户要求在记事本/画图/计算器等非浏览器应用中操作时使用本工具。Execute automation in non-browser apps (type, key, click, write, draw, wait, move). NEVER use for browser; use browser_search only for any browser operation. Do not use coordinates to operate browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_type": {
                        "type": "string",
                        "description": "操作类型。支持：'type'、'key'、'click'（仅限非浏览器窗口，不可用坐标操作浏览器）、'write'、'draw'、'wait'、'move'。浏览器相关一律使用 browser_search，禁止用本工具。Action type: type, key, click (non-browser only), write, draw, wait, move. Browser: use browser_search only."
                    },
                    "content": {
                        "type": "string",
                        "description": "操作内容。type/write 为文本，key 为按键名，draw 为图形类型，move 为 'x,y'，wait 为秒数。Content: text for type/write, key name for key, shape for draw, 'x,y' for move, seconds for wait."
                    },
                    "target_window": {
                        "type": "string",
                        "description": "可选。目标窗口标题，用于先激活窗口，如 '记事本'、'Chrome'。Optional. Target window title to activate first, e.g. 'Notepad', 'Chrome'."
                    },
                    "delay": {
                        "type": "number",
                        "description": "可选。操作间延迟（秒），默认 0.5。Optional. Delay between actions in seconds, default 0.5."
                    },
                    "coordinates": {
                        "type": "string",
                        "description": "可选。点击坐标 'x,y'，仅用于非浏览器窗口的 click。禁止用于浏览器；浏览器操作仅使用 browser_search。Optional. Click coordinates 'x,y' for non-browser only. Never for browser; use browser_search."
                    }
                },
                "required": ["action_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "automate_sequence",
            "description": "在非浏览器类应用中执行一系列自动化操作。禁止用于浏览器：浏览器相关操作仅使用 browser_search，不得用本工具或屏幕坐标操作浏览器。按顺序执行多个操作，返回每步结果拼接的字符串。Execute a sequence of automation actions in non-browser apps only. NEVER use for browser; use browser_search for any browser operation. Do not use coordinates for browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "description": "操作列表，每项包含 action_type, content, target_window, delay 等。List of actions, each with action_type, content, target_window, delay.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action_type": {"type": "string"},
                                "content": {"type": "string"},
                                "target_window": {"type": "string"},
                                "delay": {"type": "number"}
                            },
                            "required": ["action_type"]
                        }
                    }
                },
                "required": ["actions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_tool_agent",
            "description": "调用工具代理执行其他操作任务。必须使用场景：用户要求「用浏览器搜索」「在网上搜一下」「查一下」等时，必须调用本工具（工具代理会使用 browser_search 完成）。屏幕分析、视觉识别、音乐控制、打开非浏览器应用等也通过本工具。Call tool agent for browser search, screen analysis, visual recognition, music control, opening apps. For browser/search requests you MUST call this tool; the agent will use browser_search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "需要执行的任务描述，包括目标、约束条件等。Task description including goals and constraints."
                    },
                    "context": {
                        "type": "string",
                        "description": "可选。当前上下文信息，如对话历史、用户情绪等。Optional. Current context information."
                    }
                },
                "required": ["task_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_summary_agent",
            "description": "当对话中发生重要状态变化时（如好感度明显变化、获得/失去物品、地点变化、关键记忆亮点等），立即调用本工具更新动态记忆，避免角色在后续多轮对话中“忘记”刚发生的变化。Call summary agent to update dynamic memory when significant state changes occur (relationship, inventory, location, memory highlights).",
            "parameters": {
                "type": "object",
                "properties": {
                    "state_update_description": {
                        "type": "string",
                        "description": "需要更新的状态描述，包括发生了什么变化、新的状态值等。State update description including what changed and new values."
                    },
                    "context": {
                        "type": "string",
                        "description": "可选。当前上下文信息，如对话内容、工具执行结果等。Optional. Current context information."
                    }
                },
                "required": ["state_update_description"]
            }
        }
    },
    # 以下为历史注释：update_status 等由 SummaryAgent 内部使用
    # 以下工具已移除，只能通过 call_tool_agent 间接调用
    # update_status, get_visual_info, get_screen_info, open_application 等
    # 保留以下注释作为参考：
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "update_status",
    #         "description": "【已废弃，应使用 call_summary_agent】更新当前角色扮演世界中的动态状态，包括地点、好感度（关系）、背包物品、当前任务、用户状态、NPC状态和记忆亮点。只有在对话中明确发生这些变化时才调用此工具。",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {
    #                 "current_time": {
    #                     "type": "string",
    #                     "description": "当前时间戳（ISO格式），例如：'2026-01-20T20:00:00+08:00'。通常不需要手动设置，系统会自动更新。"
    #                 },
    #                 "current_location": {
    #                     "type": "string",
    #                     "description": "当前所在地点或场景，例如：'碧波的房间（电脑屏幕前）'、'寝室'、'地铁上' 等。"
    #                 },
    #                 "relationship_level": {
    #                     "type": "string",
    #                     "description": "直接设置当前关系等级，例如：'冷淡'、'普通'、'亲近'、'非常亲密'、'敌对' 等。通常使用 relationship_delta 更合适。"
    #                 },
    #                 "relationship_delta": {
    #                     "type": "integer",
    #                     "description": "关系增量（正数提升好感，负数降低好感）。例如告白成功可 +3，发生冲突可 -2。大多数时候在 -1 ~ +1 之间，稍重要事件用 -2 ~ +2，重大事件再更大。"
    #                 },
    #                 "add_item": {
    #                     "type": "string",
    #                     "description": "获得的新物品名称，将被加入 inventory 中，例如：'视觉传感器修复工具'、'情书'。"
    #                 },
    #                 "remove_item": {
    #                     "type": "string",
    #                     "description": "失去或消耗的物品名称，将从 inventory 中移除。"
    #                 },
    #                 "active_quest": {
    #                     "type": "string",
    #                     "description": "当前正在进行的任务或目标描述，例如：'日常陪伴：聊聊关于冰和螃蟹的爱好'、'帮用户完成论文'。"
    #                 },
    #                 "user_name": {
    #                     "type": "string",
    #                     "description": "更新用户的名称，例如：'YororoIce'。"
    #                 },
    #                 "user_appearance": {
    #                     "type": "string",
    #                     "description": "更新用户的外观描述，例如：'长发男性，戴着白色耳机和眼镜'。"
    #                 },
    #                 "user_mood": {
    #                     "type": "string",
    #                     "description": "更新用户的当前情绪状态，例如：'闲适（刚睡醒不久）'、'开心'、'疲惫' 等。"
    #                 },
    #                 "npc_name": {
    #                     "type": "string",
    #                     "description": "更新NPC（碧波）的名称，例如：'碧波 (Bines)'。"
    #                 },
    #                 "npc_attire": {
    #                     "type": "string",
    #                     "description": "更新NPC的服装描述，例如：'米白色针织开衫(带螃蟹刺绣) + 浅卡其色阔腿短裤 + 螃蟹刺绣拖鞋'。"
    #                 },
    #                 "npc_visual_status": {
    #                     "type": "string",
    #                     "description": "更新视觉模块的状态，例如：'Online (已修复)'、'Offline' 等。"
    #                 },
    #                 "npc_activity": {
    #                     "type": "string",
    #                     "description": "更新NPC当前正在进行的活动，例如：'正在和管理员闲聊，刚才在讨论唱见和冰兔'、'正在听音乐' 等。"
    #                 },
    #                 "add_memory_highlight": {
    #                     "type": "string",
    #                     "description": "添加一条记忆亮点（重要事件或里程碑），例如：'生日确立: 1月15日'、'视觉修复: 1月18日 (重大信任节点)'。这些是长期记忆中的重要节点。"
    #                 },
    #                 "remove_memory_highlight": {
    #                     "type": "string",
    #                     "description": "移除一条记忆亮点（如果不再重要或需要修正）。"
    #                 }
    #             },
    #             "required": []
    #         }
    #     }
    # }
    # 以下所有操作工具已移除，只能通过 call_tool_agent 间接调用：
    # - get_visual_info
    # - get_screen_info
    # - open_application
    # - enable_game_mode / disable_game_mode
    # - fast_screen_analysis
    # - find_color_region
    # - template_match
    # - control_netease_music
    # - call_thinking_model
    # 这些工具现在由 ToolAgent（外援大模型）负责执行
]

# QQ 消息场景下 ToolAgent 允许使用的工具白名单
_QQ_ALLOWED_TOOLS = {
    "get_time", "task_complete",
    "send_qq_group_msg", "send_qq_private_msg",
    "get_qq_group_list", "get_qq_friend_list",
    "broadcast_to_all_friends", "broadcast_to_all_groups",
    "at_each_group_member",
    "browser_search",
    "get_moments", "add_moment", "comment_moment",
    "get_comments", "like_moment", "like_comment", "analyze_moment_images",
    "sing",
}

# 主模型路由层：仅挂载 get_time + call_tool_agent + call_summary_agent，弃用 <ACTION>，用原生 Tool Calling + tool_calls 判断
ROUTER_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前系统时间和日期 (Get current system time)",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_tool_agent",
            "description": "调用工具代理执行操作（浏览器搜索、屏幕分析、视觉识别、打开应用、音乐控制等）。用户需要执行任何操作时必须调用本工具，不要编造结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {"type": "string", "description": "需要执行的任务描述，包括目标与约束。"},
                    "context": {"type": "string", "description": "可选。当前上下文或已对用户说的场面话。"}
                },
                "required": ["task_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_summary_agent",
            "description": "当对话中发生重要状态变化（好感度、物品、地点、记忆亮点等）时调用，更新动态记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "state_update_description": {"type": "string", "description": "需要更新的状态描述。"},
                    "context": {"type": "string", "description": "可选。当前上下文。"}
                },
                "required": ["state_update_description"]
            }
        }
    },
]

# 工具模型可见的工具（含 browser_search；排除 update_status、call_summary_agent）
ALL_TOOLS_SCHEMA_FOR_AGENT = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "获取当前系统时间和日期 (Get current system time)",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_visual_info",
            "description": "观察周围环境，获取视觉信息。调用时必须由调用方提供 focus 作为分析提示（例如要回答的问题或关注点）；视觉模块会按该提示进行分析并返回答案。Look around; you must provide focus as the analysis prompt (e.g. question or what to look for).",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": "必填（工具模型调用时）。分析提示：要回答的问题或关注点，例如 'Does the user wear glasses?', 'What is the user's expression?', '描述画面中的主要物体'。Required when calling: the analysis prompt (question or instruction)."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_screen_info",
            "description": "查看用户【当前】屏幕实时画面（仅限实时屏幕内容）。调用时由调用方提供 focus_description 作为屏幕分析提示（要关注或描述的内容）；未提供时使用默认通用描述。注意：本工具仅用于查看用户此刻的屏幕画面，绝不可用于获取历史对话、聊天记录或记忆信息。历史信息由系统自动注入主模型上下文，无需工具获取。Look at CURRENT screen only; NEVER use for retrieving past conversations, chat history, or memories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "simple_recognition": {
                        "type": "boolean",
                        "description": "Whether to just perform simple screen recognition (True) or include mouse context (False). Default is True."
                    },
                    "only_mouse_area": {
                        "type": "boolean",
                        "description": "If simple_recognition is False, this determines whether to crop and focus only on the area around the mouse (True) or view the full screen with mouse marked (False). Default is False."
                    },
                    "focus_description": {
                        "type": "string",
                        "description": "屏幕分析提示（工具模型调用时必填）：要关注或描述的内容，如 '搜索结果的标题与摘要'、'错误信息与按钮'、'当前窗口标题与主要文字'。Analysis prompt (required when tool agent calls): what to look for or describe."
                    },
                    "fast_mode": {
                        "type": "boolean",
                        "description": "Optional: Enable fast mode for quicker response. This reduces image quality and size, which speeds up processing but may slightly reduce recognition accuracy. Default is False."
                    }
                },
                "required": ["simple_recognition"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_search",
            "description": "与浏览器相关的唯一入口：仅允许通过本工具（浏览器自动化/Selenium）进行浏览器操作，不依赖屏幕坐标。启动浏览器并执行网页搜索，返回搜索结果文本。当任务涉及「打开浏览器」「用浏览器搜索」「在网上搜一下」「查一下」等时必须调用本工具。禁止用 automate_action/automate_sequence 或屏幕坐标操作浏览器。Execute web search via browser (Selenium) only; no screen coordinates. MUST use for any browser-related task. Do NOT use automate_action, automate_sequence or coordinates for browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。Search query."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enable_game_mode",
            "description": "启用游戏模式。游戏模式会大幅降低屏幕监控和分析的延迟，使用本地快速识别替代API调用，适合需要实时响应的游戏场景。Enable game mode for low-latency screen monitoring and analysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interval": {
                        "type": "number",
                        "description": "监控间隔（秒），建议0.05-0.2秒。默认0.1秒。Monitoring interval in seconds, recommended 0.05-0.2s. Default 0.1s."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "disable_game_mode",
            "description": "禁用游戏模式，恢复到普通模式。Disable game mode and return to normal mode.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_qq_group_msg",
            "description": "发送QQ群聊消息。需要传入 group_id, message, 可选 at_user_id (填 'all' 为全体)。Send QQ group message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号 (Group ID)"},
                    "message": {"type": "string", "description": "消息内容 (Message content)"},
                    "at_user_id": {"type": "string", "description": "可选。要艾特的用户QQ号，'all' 为全体成员 (Optional. User ID to @, 'all' for everyone)"}
                },
                "required": ["group_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_qq_private_msg",
            "description": "发送QQ私聊消息。需要传入 recipient_qq (user_id) 和 message。Send QQ private message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "对方QQ号 (User ID)"},
                    "message": {"type": "string", "description": "消息内容 (Message content)"}
                },
                "required": ["user_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_qq_group_list",
            "description": "获取当前登录账号加入的QQ群列表。Get QQ group list.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_qq_friend_list",
            "description": "获取当前登录账号的QQ好友列表。Get QQ friend list.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_to_all_friends",
            "description": "向所有QQ好友批量群发私聊消息。自动获取好友列表并逐个发送，支持排除指定QQ号。消息内容支持模板变量：{昵称}或{nickname}=好友昵称，{qq}或{qq号}=QQ号码，{备注}或{remark}=好友备注名。例如 message='你好{昵称}，你的QQ是{qq}' 会自动替换为每个好友的实际信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "消息内容，支持模板变量 {昵称} {qq} {备注}（自动替换为每个好友的实际信息）"},
                    "exclude_user_ids": {"type": "string", "description": "可选。要排除的QQ号，逗号分隔。例如 '12345,67890' (Optional. Comma-separated user IDs to exclude)"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_to_all_groups",
            "description": "向所有已加入的QQ群批量群发消息。自动获取群列表并逐个发送，支持排除指定群号。消息内容支持模板变量：{群名}或{group_name}=群名称，{群号}或{group_id}=群号码。例如 message='通知{群名}的各位' 会自动替换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "消息内容，支持模板变量 {群名} {群号}（自动替换为每个群的实际信息）"},
                    "exclude_group_ids": {"type": "string", "description": "可选。要排除的群号，逗号分隔。例如 '12345,67890' (Optional. Comma-separated group IDs to exclude)"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "at_each_group_member",
            "description": "在指定QQ群内依次艾特每个成员并发送个性化消息。自动获取群成员列表，逐个@并发送消息，自动跳过机器人自身。支持模板变量：{昵称}或{nickname}=群昵称，{qq}或{qq号}=QQ号，{群名片}或{card}=群名片。例如 message='新年快乐{昵称}！你的QQ是{qq}' 会为每个成员替换为实际信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "integer", "description": "群号 (Group ID)"},
                    "message": {"type": "string", "description": "消息内容，支持模板变量 {昵称} {qq} {群名片}（自动替换为每个成员的实际信息）"},
                    "exclude_user_ids": {"type": "string", "description": "可选。要排除的QQ号，逗号分隔。例如 '12345,67890'（机器人自身会自动排除）"}
                },
                "required": ["group_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sing",
            "description": "播放 static/music/cover 目录下的本地音频（如翻唱、BGM）。必须从下方 filename 枚举中直接选择一项，不要自行输入。Play a local audio from static/music/cover; choose one from the filename enum.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "从枚举中选择要播放的文件名（由系统扫描 cover 目录生成）。"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fast_screen_analysis",
            "description": "快速屏幕分析（游戏模式专用）。使用本地OCR和图像处理，延迟极低（<100ms），适合实时游戏场景。Fast screen analysis for game mode. Uses local OCR and image processing with very low latency (<100ms).",
            "parameters": {
                "type": "object",
                "properties": {
                    "focus_area": {
                        "type": "string",
                        "description": "关注区域，格式 'x,y,width,height'，例如 '100,100,200,200'。Focus area in format 'x,y,width,height'."
                    },
                    "use_ocr": {
                        "type": "boolean",
                        "description": "是否使用OCR识别文字。默认True。Whether to use OCR for text recognition. Default True."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_color_region",
            "description": "在屏幕中查找特定颜色区域（用于游戏UI元素定位，如血条、能量条等）。Find color region in screen for game UI elements (health bar, energy bar, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_color": {
                        "type": "string",
                        "description": "目标颜色，格式 'R,G,B'，例如 '255,0,0' 表示红色。Target color in format 'R,G,B'."
                    },
                    "tolerance": {
                        "type": "number",
                        "description": "颜色容差，默认30。Color tolerance, default 30."
                    },
                    "focus_area": {
                        "type": "string",
                        "description": "搜索区域，格式 'x,y,width,height'。Search area in format 'x,y,width,height'."
                    }
                },
                "required": ["target_color"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "template_match",
            "description": "模板匹配（用于快速识别游戏中的固定UI元素，如按钮、图标等）。Template matching for game UI elements (buttons, icons, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {
                        "type": "string",
                        "description": "模板图片路径。Path to template image."
                    },
                    "threshold": {
                        "type": "number",
                        "description": "匹配阈值，默认0.8。Matching threshold, default 0.8."
                    }
                },
                "required": ["template_path"]
            }
        }
    },
    # 网易云音乐工具（使用uiautomation控件操作，无需视觉分析验收）
    {
        "type": "function",
        "function": {
            "name": "open_netease_music",
            "description": "打开网易云音乐应用。Open NetEase CloudMusic application.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_music_in_netease",
            "description": "在网易云音乐中搜索音乐。Search music in NetEase CloudMusic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（歌曲名、歌手名等）。Search keyword (song name, artist name, etc.)."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_music_in_netease",
            "description": "在网易云音乐中搜索并播放音乐。Search and play music in NetEase CloudMusic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（歌曲名、歌手名等）。Search keyword (song name, artist name, etc.)."
                    },
                    "play_first_result": {
                        "type": "boolean",
                        "description": "是否播放搜索结果，默认True。Whether to play the first search result. Default True."
                    },
                    "result_index": {
                        "type": "integer",
                        "description": "播放第几个搜索结果（0表示第一首），默认0。Index of search result to play (0 for first). Default 0."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_netease_music",
            "description": "控制网易云音乐播放（播放/暂停/上一首/下一首/音量等）。使用全局快捷键，无需激活窗口，无需屏幕分析。Control NetEase CloudMusic playback (play/pause/next/previous/volume). Uses global hotkeys, no window activation or screen analysis needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "控制动作。支持: 'play'（播放）、'pause'（暂停）、'play_pause'（播放/暂停切换）、'next'（下一首）、'previous'（上一首）、'volume_up'（音量增加）、'volume_down'（音量减少）。Control action. Supported: 'play', 'pause', 'play_pause', 'next', 'previous', 'volume_up', 'volume_down'."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_playlist_name_in_netease",
            "description": "获取网易云音乐中所有歌单名称列表。Get all playlist names from NetEase CloudMusic.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "switch_to_playlist",
            "description": "切换到指定的网易云音乐歌单。Switch to a specific playlist in NetEase CloudMusic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist_name": {
                        "type": "string",
                        "description": "歌单名称。Playlist name."
                    }
                },
                "required": ["playlist_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_song_from_playlist",
            "description": "从指定歌单中播放特定歌曲。Play a specific song from a playlist in NetEase CloudMusic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist_name": {
                        "type": "string",
                        "description": "歌单名称。Playlist name."
                    },
                    "song_name": {
                        "type": "string",
                        "description": "歌曲名称。Song name."
                    },
                    "song_index": {
                        "type": "integer",
                        "description": "歌曲在歌单中的索引（从0开始）。Index of song in playlist (0-based)."
                    }
                },
                "required": []
            }
        }
    },
    # 键鼠操作工具已移除；call_thinking_model 不挂在工具模型上，避免“工具模型再调思考模型”的递归，复杂任务由主模型直接 call_tool_agent 交给工具模型多轮执行即可
    # 【动态工具·Buffer 约定】get_moments 调用一次后结果会写入模块缓冲区；comment_moment / get_comments / like_moment / like_comment / analyze_moment_images 必须使用该次返回的 data 中的 _id，禁止为获取 _id 重复调用 get_moments。
    {
        "type": "function",
        "function": {
            "name": "get_moments",
            "description": "【仅需调用一次】获取已发布动态列表并写入缓冲区。返回 data 中每条含 _id, title, content, filenames, comments, likes, views, createdAt, username。后续评论/点赞/看评论/看图片时必须使用本次返回的 _id，禁止再次调用 get_moments 只为拿 _id。Call once to fetch moments; use returned _id for comment/like/get_comments/analyze_moment_images; do NOT call get_moments again for each operation.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_moment",
            "description": "发布一条新动态（标题必填，正文可选）。发布后会自动写入缓冲区，无需再调 get_moments 刷新。Publish a moment (title required, content optional); new moment is auto-cached.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "动态标题。Moment title."},
                    "content": {"type": "string", "description": "动态正文，可选。Moment content, optional."}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "comment_moment",
            "description": "对某条动态或某条评论发表评论。moment_id 必须使用【上一次 get_moments 返回的 data 中】对应条目的 _id，禁止为获取 moment_id 再次调用 get_moments。回复某条评论时，belong 填该评论的 _id（可从 get_comments 返回或当前上下文获得）。Comment on a moment; moment_id must come from previous get_moments result; do NOT call get_moments again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "moment_id": {"type": "string", "description": "源动态的 _id（必须来自已缓存的 get_moments 结果）。Moment _id from buffer/previous get_moments."},
                    "comment": {"type": "string", "description": "评论内容。Comment content."},
                    "belong": {"type": "string", "description": "可选。回复某条评论时填该评论的 _id。Optional: parent comment _id when replying to a comment."}
                },
                "required": ["moment_id", "comment"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_comments",
            "description": "根据评论 id 列表批量拉取评论详情。comment_ids 应来自【已缓存的 get_moments 结果】中某条的 comments 字段（id 列表）。拉取后缓冲区会更新该条动态的评论映射，便于后续 comment_moment(..., belong=评论_id)。Do NOT call get_moments again to get comment ids; use comments from previous get_moments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "评论 id 列表，来自 get_moments 返回的 data[].comments。Comment id list from buffer."
                    }
                },
                "required": ["comment_ids"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "like_moment",
            "description": "点赞或取消点赞某条动态。moment_id 必须使用【上一次 get_moments 返回的 data 中】对应条目的 _id，禁止再次调用 get_moments。Like/unlike a moment; moment_id from buffer only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "moment_id": {"type": "string", "description": "动态 _id（来自缓存的 get_moments 结果）。Moment _id from buffer."},
                    "like": {"type": "boolean", "description": "True 点赞，False 取消。True to like, False to unlike."}
                },
                "required": ["moment_id", "like"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "like_comment",
            "description": "点赞或取消点赞某条评论。comment_id 使用 get_comments 返回的 _id 或上下文中已有的评论 id，禁止为获取 id 调用 get_moments。Like/unlike a comment; comment_id from get_comments or context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "comment_id": {"type": "string", "description": "评论 _id（来自 get_comments 或缓冲）。Comment _id from buffer."},
                    "like": {"type": "boolean", "description": "True 点赞，False 取消。True to like, False to unlike."}
                },
                "required": ["comment_id", "like"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_moment_images",
            "description": "识别某条动态中的图片并返回文字描述。moment_id 必须使用【上一次 get_moments 返回的 data 中】对应条目的 _id（且该条 filenames 非空），禁止再次调用 get_moments。Analyze moment images; moment_id from buffer only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "moment_id": {"type": "string", "description": "动态 _id（来自缓存的 get_moments 结果，且该条有图片）。Moment _id from buffer."},
                    "max_images": {"type": "integer", "description": "可选，最多分析几张图，默认 3。Optional, default 3."},
                    "timeout": {"type": "integer", "description": "可选，单张超时秒数，默认 25。Optional, default 25."}
                },
                "required": ["moment_id"]
            }
        }
    },
    # 指针与键盘工具集（坐标均为屏幕像素）
    {
        "type": "function",
        "function": {
            "name": "left_click",
            "description": "在指定坐标左键单击，可连续多次。坐标为屏幕像素 (x, y)。Left click at (x,y), optional multiple times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标（像素）。X coordinate in pixels."},
                    "y": {"type": "integer", "description": "纵坐标（像素）。Y coordinate in pixels."},
                    "times": {"type": "integer", "description": "连续点击次数，默认 1。Number of clicks, default 1."}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "left_double_click",
            "description": "在指定坐标左键双击，可连续多次。坐标为屏幕像素。Left double-click at (x,y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标（像素）。"},
                    "y": {"type": "integer", "description": "纵坐标（像素）。"},
                    "times": {"type": "integer", "description": "连续双击次数，默认 1。"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "right_click",
            "description": "在指定坐标右键单击，可连续多次。坐标为屏幕像素。Right click at (x,y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标（像素）。"},
                    "y": {"type": "integer", "description": "纵坐标（像素）。"},
                    "times": {"type": "integer", "description": "连续点击次数，默认 1。"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "left_drag",
            "description": "左键按下从起点拖拽到终点。参数为起点与终点坐标（屏幕像素）。Left-button drag from (start_x,start_y) to (end_x,end_y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "起点横坐标（像素）。"},
                    "start_y": {"type": "integer", "description": "起点纵坐标（像素）。"},
                    "end_x": {"type": "integer", "description": "终点横坐标（像素）。"},
                    "end_y": {"type": "integer", "description": "终点纵坐标（像素）。"}
                },
                "required": ["start_x", "start_y", "end_x", "end_y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "right_drag",
            "description": "右键按下从起点拖拽到终点。参数为起点与终点坐标（屏幕像素）。Right-button drag from start to end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "起点横坐标（像素）。"},
                    "start_y": {"type": "integer", "description": "起点纵坐标（像素）。"},
                    "end_x": {"type": "integer", "description": "终点横坐标（像素）。"},
                    "end_y": {"type": "integer", "description": "终点纵坐标（像素）。"}
                },
                "required": ["start_x", "start_y", "end_x", "end_y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "在当前焦点位置模拟键盘输入字符串。Type the given string at current focus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的字符串。String to type."}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "一次性按下组合键，如 Ctrl+C、Alt+Tab。参数为按键列表，按顺序按下后同时释放。Press a key combination, e.g. ['ctrl','c'] for Ctrl+C.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按键列表，如 ['ctrl','c'] 表示 Ctrl+C。List of keys, e.g. ['ctrl','c']."
                    }
                },
                "required": ["keys"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "任务完成后必须调用本工具提交最终结果。只有调用 task_complete 后任务才算结束；请先完成所有操作，再调用本工具并传入对用户的最终回复或结果摘要。You MUST call this tool when the task is done to submit the final result. Task is only considered complete after calling task_complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "对用户的最终回复或结果摘要（将作为任务执行结果返回）。Final reply or result summary for the user."
                    }
                },
                "required": ["result"]
            }
        }
    }
]

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


def _get_tool_agent_schema_filtered():
    """读取工具 schema 并返回可挂载到工具代理的过滤结果。"""
    return get_tool_agent_schema_filtered(TOOL_AGENT_SCHEMA_PATH, ALL_TOOLS_SCHEMA_FOR_AGENT)


main_agent.set_tools_schema(ROUTER_TOOLS_SCHEMA)
tool_agent.set_tools_schema(_get_tool_agent_schema_filtered())

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
                full_schema = _get_tool_agent_schema_filtered()
                # 【QQ 工具过滤】QQ 消息来源时，仅允许 QQ 相关工具，禁止屏幕/视觉/游戏等本地工具
                if source in ("QQ", "qq"):
                    qq_schema = [t for t in full_schema if t.get("function", {}).get("name") in _QQ_ALLOWED_TOOLS]
                    tool_agent.set_tools_schema(qq_schema)
                    print(f"[Thinking] QQ 来源，工具已过滤为 QQ 白名单 ({len(qq_schema)}/{len(full_schema)} 个工具)", flush=True)
                else:
                    tool_agent.set_tools_schema(full_schema)
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

def _qq_merge_source_key(qq_context: dict) -> tuple:
    """根据 qq_context 生成合并缓冲的来源 key：(group_id, user_id)。同一群/同一用户私聊的消息才合并。"""
    inner = qq_context.get("qq_context", qq_context) if qq_context else {}
    gid = inner.get("group_id")
    uid = inner.get("user_id")
    return (gid, uid)


def _qq_merge_enqueue(user_input: str, img_descr: str, source: str, qq_context: dict,
                      executor, processing_lock, processing_state):
    """
    将一条 QQ 消息放入**按来源分离**的合并缓冲区。
    - 每个 (group_id, user_id) 拥有独立的缓冲槽和定时器
    - 同一来源的连续消息合并，不同来源互不干扰、独立提交
    """
    key = _qq_merge_source_key(qq_context)
    
    with _QQ_MERGE_LOCK:
        slot = _QQ_MERGE_SLOTS.get(key)
        if slot is None:
            slot = {"texts": [], "timer": None, "context": None}
            _QQ_MERGE_SLOTS[key] = slot
        
        slot["texts"].append(user_input)
        
        # 保存第一条消息的上下文，后续消息复用
        if slot["context"] is None:
            slot["context"] = (img_descr, source, qq_context)
        else:
            # 如果后续消息也有图片，合并图片URL
            if qq_context and qq_context.get("qq_context", {}).get("image_urls"):
                prev_qq = slot["context"][2] or {}
                prev_inner = prev_qq.get("qq_context", prev_qq)
                existing_urls = prev_inner.get("image_urls", [])
                new_urls = qq_context.get("qq_context", qq_context).get("image_urls", [])
                if new_urls:
                    combined_urls = existing_urls + new_urls
                    if "qq_context" in prev_qq:
                        prev_qq["qq_context"]["image_urls"] = combined_urls
                    else:
                        prev_qq["image_urls"] = combined_urls
        
        # 取消之前的定时器并重新设置（滑动窗口）
        if slot["timer"] is not None:
            slot["timer"].cancel()
        
        buf_count = len(slot["texts"])
        print(f"[QQ Merge] [{key}] 消息入缓冲 ({buf_count} 条待合并)，{_QQ_MERGE_WINDOW_SEC}s 后提交: {user_input[:40]}...", flush=True)
        
        slot["timer"] = threading.Timer(
            _QQ_MERGE_WINDOW_SEC,
            _qq_merge_flush,
            args=(key, executor, processing_lock, processing_state)
        )
        slot["timer"].daemon = True
        slot["timer"].start()


def _qq_merge_flush(key: tuple, executor, processing_lock, processing_state):
    """
    某个来源的合并窗口到期：将该来源缓冲区内的所有消息合并为一条，提交到 process_message。
    如果当前正在处理中，则作为 PENDING_INTERRUPT_INPUT 暂存（不会丢失）。
    每个来源独立 flush，互不干扰。
    """
    global INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
    
    with _QQ_MERGE_LOCK:
        slot = _QQ_MERGE_SLOTS.pop(key, None)
        if not slot or not slot["texts"]:
            return
        
        merged_texts = list(slot["texts"])
        ctx = slot["context"]
    
    # 合并消息文本：多条消息用换行连接
    if len(merged_texts) == 1:
        merged_input = merged_texts[0]
    else:
        merged_input = "\n".join(merged_texts)
        print(f"[QQ Merge] [{key}] 已合并 {len(merged_texts)} 条消息: {merged_input[:60]}...", flush=True)
    
    img_descr = ctx[0] if ctx else ""
    source = ctx[1] if ctx else "QQ"
    qq_context = ctx[2] if ctx else {}
    
    # 检查是否正在处理中
    with IS_PROCESSING_DIALOGUE_LOCK:
        is_processing = IS_PROCESSING_DIALOGUE
    
    if is_processing:
        # 当前正在处理另一条消息，作为 pending 暂存（打断当前处理）
        with INTERRUPT_LOCK:
            INTERRUPT_REQUESTED = True
            if PENDING_INTERRUPT_INPUT is not None:
                # 已有 pending，追加到已有文本后面
                prev = PENDING_INTERRUPT_INPUT
                prev_text = prev[0] if len(prev) >= 1 else ""
                PENDING_INTERRUPT_INPUT = (prev_text + "\n" + merged_input, img_descr or "", source, qq_context)
            else:
                PENDING_INTERRUPT_INPUT = (merged_input, img_descr or "", source, qq_context)
        with _B_TASK_LOCK:
            if _CURRENT_B_TASK_ID:
                _CANCELLED_B_TASK_IDS.add(_CURRENT_B_TASK_ID)
        print(f"[QQ Merge] [{key}] 当前正在处理中，合并消息已暂存为 Pending: {merged_input[:40]}...", flush=True)
    else:
        # 当前空闲，直接提交处理
        def process_qq_merged(u=merged_input, i=img_descr, s=source, e=qq_context):
            audio_flag = (s not in ["QQ", "qq"])
            print(f"[QQ Merge] [{key}] 提交合并后的QQ消息处理: source={s}, enable_audio={audio_flag}", flush=True)
            with processing_lock:
                if processing_state["is_processing"]:
                    # 极端竞态：刚好有另一个消息开始处理了，改为 pending
                    _qq_merge_set_pending(u, i, s, e)
                    print(f"[QQ Merge] [{key}] 竞态：转为 Pending", flush=True)
                    return
                processing_state["is_processing"] = True
            try:
                process_message(u, i, source=s, extra_data=e)
            finally:
                with processing_lock:
                    processing_state["is_processing"] = False
        
        executor.submit(process_qq_merged)


def process_message(user_input, img_descr, source=None, extra_data=None):
    # 【新增】设置全局状态标签：开始处理对话
    global IS_PROCESSING_DIALOGUE, INTERRUPT_REQUESTED, PENDING_INTERRUPT_INPUT
    global _CURRENT_B_TASK_ID, _CANCELLED_B_TASK_IDS
    with IS_PROCESSING_DIALOGUE_LOCK:
        IS_PROCESSING_DIALOGUE = True
    
    exited_due_to_interrupt = False  # 是否因用户打断而退出本轮
    
    # QQ消息不发送TTS语音，同时支持 "QQ" 和 "qq"
    # 如果 source 是 QQ/qq，则 disable audio
    # 【修复】增加更健壮的判断，去除可能的空格，并显式处理 None
    source_str = str(source).strip() if source else ""
    enable_audio = (source_str not in ["QQ", "qq"])
    # 强制修正：如果 source 为 ASR/MANUAL，必须开启 audio
    if source_str in ["ASR", "MANUAL"]:
        enable_audio = True

    # 定义中断检查函数，供 ToolAgent 和 MainAgent 在长时间运行时回调
    def _interrupt_check():
        with INTERRUPT_LOCK:
            return INTERRUPT_REQUESTED
    
    try:
        # [修复] 增加 flush=True 确保日志即时输出
        # [安全修复] 清洗用户输入，防止 Prompt 注入和系统事件伪造
        # 移除用户输入中的系统事件标记，防止恶意用户伪造系统事件
        cleaned_user_input = user_input
        if user_input:
            # 移除系统事件标记（不区分大小写）
            system_markers = [
            "[system event:",
            "[system event :",
            "system event:",
            "[screen monitor]",
            "[screen monitor:",
            "screen monitor:",
            ]
            for marker in system_markers:
                # 不区分大小写地移除标记
                import re
                cleaned_user_input = re.sub(re.escape(marker), "", cleaned_user_input, flags=re.IGNORECASE)
                cleaned_user_input = cleaned_user_input.strip()
            
            # 如果清洗后内容为空，使用原始输入（避免完全丢失用户消息）
            if not cleaned_user_input:
                cleaned_user_input = user_input
                print(f"[Security] 警告：用户输入包含系统事件标记，已清洗但保留原始内容", flush=True)
            elif cleaned_user_input != user_input:
                print(f"[Security] 已清洗用户输入中的系统事件标记", flush=True)
        
        user_input = cleaned_user_input  # 使用清洗后的输入
        
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
        
        time_gap_instruction = ""
        try:
            history = memory_system.short_term.get_messages()
            last_date_obj = None
            
            for msg in reversed(history):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    match = re.search(r"\[(\d{4}/\d{1,2}/\d{1,2})\]:", content)
                    if match:
                        try:
                            last_date_obj = datetime.datetime.strptime(match.group(1), "%Y/%m/%d")
                            break
                        except ValueError:
                            pass
            
            if last_date_obj:
                delta_days = (current_date_obj - last_date_obj).days
                wakeup_words = ["你好", "在吗", "醒醒", "启动", "喂", "hi", "hello"]
                is_wakeup = any(w in user_input.lower() for w in wakeup_words)
                
                if delta_days >= 1 and (is_wakeup or random.random() < 0.3):
                    # (完全保留原提示词)
                    time_gap_instruction = (
                        f"\nSystem Context: The last conversation was on {last_date_obj.strftime('%Y/%m/%d')}, "
                        f"which is {delta_days} days ago. "
                        "Since the user has been gone for a while, you should playfully complain "
                        "about their long absence or the gap in time based on your persona."
                    )
        except Exception as e:
            print(f"Date check error: {e}", flush=True)

        # 【修复】检查用户是否明确拒绝屏幕监控
        global SCREEN_MONITOR_USER_REJECTED, SCREEN_MONITOR_REJECT_EXPIRE_TIME
        reject_keywords = ["不看", "不要看", "别看了", "不用看", "不需要看", "ignore", "don't look", "stop watching", 
                          "不要分析", "不用分析", "别分析", "不需要分析"]
        user_input_lower = user_input.lower()
        if any(keyword in user_input_lower for keyword in reject_keywords):
            SCREEN_MONITOR_USER_REJECTED = True
            SCREEN_MONITOR_REJECT_EXPIRE_TIME = time.time() + 300  # 5分钟后自动恢复
            print(f"[Screen Monitor] 检测到用户拒绝屏幕监控，将在5分钟后自动恢复", flush=True)
        
        # 检查拒绝状态是否过期
        if SCREEN_MONITOR_USER_REJECTED and time.time() > SCREEN_MONITOR_REJECT_EXPIRE_TIME:
            SCREEN_MONITOR_USER_REJECTED = False
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

        full_raw_response = ""  # 用于打断时判断是否有完整输出
        # === 原生 Tool Calling：主模型挂载 call_tool_agent / call_summary_agent / get_time，根据 tool_calls 执行并循环 ===
        def _interrupt_check():
            with INTERRUPT_LOCK:
                return INTERRUPT_REQUESTED

        MAX_TOOL_ROUNDS = 5
        round_idx = 0
        while round_idx < MAX_TOOL_ROUNDS:
            round_idx += 1
            print(f"[Thinking] 主模型流式请求 (round {round_idx})...", flush=True)
            content_gen = None
            holder = {}
            for attempt in range(3):
                content_gen, holder = main_agent.run_one_turn_streaming(messages, interrupt_check=_interrupt_check)
                if not holder.get("error"):
                    break
                err = holder.get("error", "")
                if any(x in str(err) for x in ("500", "502", "503")) and attempt < 2:
                    print(f"[Thinking] DeepSeek 服务端异常，3 秒后重试 ({attempt + 1}/3)...", flush=True)
                    time.sleep(3)
                    continue
                print(f"[Thinking] [Error] {err}", flush=True)
                if "500" in str(err) or "502" in str(err) or "503" in str(err):
                    zmq_send("DeepSeek 服务暂时异常，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
                else:
                    zmq_send("请求出错，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
                break
            if holder.get("error"):
                break
            if not content_gen:
                zmq_send("系统错误，无法连接大脑。", lang="zh", cough="end", enable_audio=enable_audio)
                break

            full_raw_response = ""
            content_buffer = ""
            is_first_packet = True
            line_queue = queue.Queue()

            def _stream_producer():
                try:
                    for chunk in content_gen:
                        line_queue.put(chunk)
                finally:
                    line_queue.put(None)

            threading.Thread(target=_stream_producer, daemon=True).start()
            stream_done = False
            while not stream_done:
                try:
                    chunk = line_queue.get(timeout=0.35)
                except queue.Empty:
                    with INTERRUPT_LOCK:
                        if INTERRUPT_REQUESTED:
                            exited_due_to_interrupt = True
                            stream_done = True
                            break
                    continue
                if chunk is None:
                    stream_done = True
                    break
                full_raw_response += chunk
                if holder.get("has_tool_calls"):
                    continue
                for c in chunk:
                    # [修改] 改为检测双空格分句
                    # 缓冲区逻辑：
                    # 如果遇到空格：
                    # 1. 检查是否上一个也是空格 (content_buffer 最后一个字符是空格)
                    # 2. 如果是，说明遇到了 "  "，触发分句
                    # 3. 如果不是，仅追加空格，等待下一个
                    
                    if c == " ":
                        if content_buffer.endswith(" "):
                            # 已有一个空格在 buffer 末尾，现在又来一个，构成双空格
                            seg = content_buffer[:-1].strip() # 去掉那个暂存的空格
                            if seg:
                                if not _is_only_action_or_empty(seg):
                                    cough_val = "start" if is_first_packet else None
                                    zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
                                    is_first_packet = False
                            content_buffer = "" # 清空
                        else:
                            content_buffer += c # 第一个空格，暂存
                    else:
                        content_buffer += c

            if holder.get("interrupted"):
                exited_due_to_interrupt = True
            if exited_due_to_interrupt:
                if content_buffer.strip():
                    seg = content_buffer.strip()
                    if not _is_only_action_or_empty(seg):
                        cough_val = "start" if is_first_packet else None
                        zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
                break
            if content_buffer.strip():
                seg = content_buffer.strip()
                if not _is_only_action_or_empty(seg):
                    cough_val = "start" if is_first_packet else None
                    zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
            print("", flush=True)

            tool_calls = (holder.get("message") or {}).get("tool_calls")
            if not tool_calls or not list(tool_calls):
                break
            messages.append(holder["message"])
            _execute_router_tool_calls_and_append(messages, tool_calls, messages, interrupt_callback=_interrupt_check, source=source)
            full_raw_response = (full_raw_response or "").strip()

        to_save = full_raw_response or ""
        if not _is_only_action_or_empty(to_save):
            if source not in ["QQ", "qq"]:
                memory_system.add_interaction(formatted_user_input, to_save)
            else:
                print(f"[Thinking] QQ 消息不写入主短期记忆 (已由 QQ Buffer 管理)", flush=True)

        # [QQ回复] 独立于 _is_only_action_or_empty 判断，确保 QQ 消息始终被发送
        # 但如果是被打断的截断回复，则不发送（避免发送不完整的半句话）
        if source == "QQ" and extra_data and to_save and to_save.strip() and not exited_due_to_interrupt:
            try:
                qq_ctx = extra_data.get("qq_context") or extra_data
                reply_msg = send_qq_reply(to_save, qq_ctx)
                if reply_msg:
                    group_id = qq_ctx.get("group_id")

                    # [新增] 将 bot 自己的 QQ 回复也存入 Buffer
                    try:
                        buf_mgr = get_qq_buffer_manager()
                        bot_meta = {
                            "timestamp": time.time(),
                            "sender": "Self(Bot)",
                            "group_id": qq_ctx.get("group_id"),
                            "user_id": qq_ctx.get("user_id"),
                            "is_group": bool(qq_ctx.get("group_id"))
                        }
                        if group_id:
                            bot_content = f"[QQ群回复][Bot]: {reply_msg}"
                        else:
                            bot_content = f"[QQ私聊回复][Bot]: {reply_msg}"
                        buf_mgr.add_message(bot_content, bot_meta)
                        print(f"[Thinking] Bot QQ 回复已存入 Buffer", flush=True)
                    except Exception as buf_err:
                        print(f"[Thinking] Bot 回复存入 Buffer 失败: {buf_err}", flush=True)

            except Exception as e:
                print(f"[Thinking] Failed to send QQ reply: {e}", flush=True)
                traceback.print_exc()

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
        if pending and _EXECUTOR_FOR_INTERRUPT is not None:
            with PROCESSING_LOCK:
                PROCESSING_STATE["is_processing"] = False
            if len(pending) == 4:
                new_user_input, new_img_descr, new_source, new_extra = pending
            else:
                new_user_input, new_img_descr, new_source = pending
                new_extra = None
            def process_pending():
                with PROCESSING_LOCK:
                    if PROCESSING_STATE["is_processing"]:
                        return
                    PROCESSING_STATE["is_processing"] = True
                try:
                    process_message(new_user_input, new_img_descr, source=new_source, extra_data=new_extra)
                finally:
                    with PROCESSING_LOCK:
                        PROCESSING_STATE["is_processing"] = False
            _EXECUTOR_FOR_INTERRUPT.submit(process_pending)
            print(f"[Thinking] 已提交打断后的新输入: {new_user_input[:30]}...", flush=True)


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


class RAGServerClient:
    """Simple ZMQ Client for RAG Server"""
    def __init__(self, zmq_context, host, port):
        self.ctx = zmq_context
        self.host = host
        self.port = port
        print(f"[RAGClient] Initialized: {host}:{port}", flush=True)

    def _req(self, method, **params):
        try:
            # 使用上下文管理器创建 socket，确保正确关闭
            s = self.ctx.socket(zmq.REQ)
            # 增加超时以支持 LLM 调用，30s
            s.setsockopt(zmq.RCVTIMEO, 30000) 
            s.setsockopt(zmq.SNDTIMEO, 5000)
            s.setsockopt(zmq.LINGER, 0)
            
            s.connect(f"tcp://{self.host}:{self.port}")
            s.send_json({"method": method, "params": params})
            res = s.recv_json()
            s.close()
            return res
        except zmq.error.Again:
             print(f"[RAGClient] Timeout calling {method}", flush=True)
             try: s.close()
             except: pass
             return None
        except Exception as e:
            print(f"[RAGClient] Request failed ({method}): {e}", flush=True)
            try: s.close()
            except: pass
            return None

    def add_to_summary_buffer(self, content, meta=None, importance_score=7):
        return self._req("add_to_summary_buffer", content=content, meta=meta, importance_score=importance_score)

    def add_qq_log(self, content, meta=None):
        return self._req("add_qq_log", content=content, meta=meta)

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