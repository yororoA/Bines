"""
思考模型工具
用于调用思考模式大模型进行复杂工具调用
"""
from .dependencies import deps
from thingking.src.tool_schema_defs import browser_search_tool_schema


def _is_simple_open_task(text: str) -> bool:
    """
    粗略判断是否为“仅打开应用”的简单任务：
    - 包含“打开/启动/open”
    - 且不包含明显的后续操作词（搜索/点击/播放/输入/登录 等）
    """
    if not text:
        return False
    lowered = text.lower()
    # 触发“打开”类任务的关键词
    open_keywords = ["打开", "启动", "open "]
    # 一旦出现这些，就认为任务不再是“仅打开”
    followup_keywords = [
        "然后", "并且", "接着", "顺便", "同时",
        "搜索", "查找", "查一下", "找一下",
        "点击", "点一下", "点开",
        "播放", "放一下", "下一首", "上一首",
        "输入", "打字", "键入",
        "登录", "登陆", "log in", "sign in",
    ]
    if not any(k in lowered for k in open_keywords):
        return False
    if any(k in lowered for k in followup_keywords):
        return False
    return True


def _is_browser_only_task(text: str) -> bool:
    """
    判断是否为仅与浏览器相关的任务（应用内仅浏览器，无记事本/画图等）。
    此类任务应由主模型使用 browser_search 完成，工具代理不得使用屏幕坐标操作浏览器。
    """
    if not text or not text.strip():
        return False
    lowered = text.lower().strip()
    browser_keywords = [
        "浏览器", "browser", "网页", "网上", "上网",
        "用浏览器搜索", "打开浏览器", "在浏览器里", "在浏览器中",
        "web search", "search the web", "查一下", "搜一下", "搜一搜",
    ]
    non_browser_app_keywords = [
        "记事本", "notepad", "画图", "paint", "计算器", "calculator",
        "vscode", "代码", "code editor", "编辑器",
    ]
    has_browser = any(k in text or k in lowered for k in browser_keywords)
    has_non_browser = any(k in text or k in lowered for k in non_browser_app_keywords)
    return has_browser and not has_non_browser


def call_thinking_model(task_description, context=""):
    """
    调用思考模式大模型进行复杂工具调用
    
    Args:
        task_description: 任务描述
        context: 当前上下文信息
        
    Returns:
        str: 思考模式大模型处理后的结果
    """
    # [依赖注入] 从依赖容器获取思考模型助手
    if deps.thinking_model_helper is None:
        return "Thinking model helper is not available."
    
    # [依赖注入] 获取工具定义和注册表（需要通过回调函数获取，避免循环导入）
    # 这些依赖需要在 handle_zmq.py 中注册
    if not hasattr(deps, 'get_tools_schema') or not hasattr(deps, 'get_tools_registry'):
        return "Tools schema and registry are not available. Please register them in handle_zmq.py"
    
    get_tools_schema = deps.get_tools_schema
    get_tools_registry = deps.get_tools_registry
    
    # 检测当前任务是否属于“仅打开应用”的简单任务
    simple_open_task = _is_simple_open_task(task_description)

    # 【特殊规则】如果任务明确是“打开网易云音乐”，强制走网易云音乐专用工具，
    # 而不是通用的 open_application，避免出现日志中那种“调用打开网易云的工具使用 app_tool”的情况。
    is_open_netease_task = False
    # 【新增】检测是否为“音乐相关任务”：播放/控制音乐、歌曲、网易云等
    is_music_task = False
    if task_description:
        lowered_desc = task_description.lower()
        if ("网易云音乐" in task_description) or ("netease" in lowered_desc and "music" in lowered_desc):
            is_open_netease_task = True
            is_music_task = True
        # 其他泛化音乐关键词：音乐 / 歌曲 / 歌单 / 播放音乐 / music / song / playlist
        music_keywords_cn = ["音乐", "歌曲", "歌单", "播放音乐", "听歌"]
        music_keywords_en = ["music", "song", "songs", "playlist"]
        if any(k in task_description for k in music_keywords_cn) or any(k in lowered_desc for k in music_keywords_en):
            is_music_task = True

    # browser_search 已挂在工具模型上，纯浏览器任务由本代理直接调用 browser_search 完成，不再短路返回
    try:
        print(f"[ThinkingModel] 收到任务: {task_description}", flush=True)
        
        # 构建消息列表
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a thinking-enabled AI assistant that can call tools to help users. "
                    "You have access to all the same tools as the main model, including mouse/keyboard automation tools. "
                    "When you need to call tools, use the available tools to complete the task. "
                    "Think carefully about the steps needed and call tools in the correct order.\n\n"
                    "BROWSER OPERATIONS (STRICT): For any browser-related task (open browser, web search, look up online), you MUST use the browser_search tool only. Do NOT use automate_action, automate_sequence, find_element_and_click, find_element_and_type, analyze_and_operate, or any coordinate/screen-based automation for browser. Call browser_search with the search query to complete the task.\n\n"
                    "SCREEN/VISUAL PROMPT (MANDATORY): When you call get_screen_info or get_visual_info, you MUST provide the analysis prompt yourself based on the current task:\n"
                    "- get_screen_info: always pass focus_description with a concrete instruction (e.g. what to look for on screen, what to describe). Do not leave focus_description empty.\n"
                    "- get_visual_info: always pass focus with a concrete question or instruction (e.g. what to look for in the scene). Do not leave focus empty.\n"
                    "The API uses your prompt as-is; omitting it yields a generic result that is less useful for the task.\n\n"
                    "CRITICAL RULES FOR MOUSE/KEYBOARD OPERATIONS (non-browser only):\n"
                    "1. After each mouse/keyboard operation (automate_action, automate_sequence, find_element_and_click, etc.) on non-browser apps, "
                    "   the system will automatically re-check the screen state for you.\n"
                    "2. You should analyze the updated screen information to determine the next step.\n"
                    "3. Continue operations until the task goal is achieved.\n"
                    "4. When the task is complete, provide a summary of what was accomplished.\n\n"
                    "DYNAMIC STATUS (update_status):\n"
                    "- You can maintain structured roleplay world state (current_location, relationship_level, inventory, active_quest) via the `update_status` tool.\n"
                    "- Call `update_status` only when location, relationship, inventory items, or active quest clearly change in the story.\n"
                    "- Do NOT spam this tool; use it only when the change is explicit in the conversation."
                )
            }
        ]
        
        # 添加上下文信息（如果有）
        if context:
            messages.append({
                "role": "user",
                "content": f"Context: {context}\n\nTask: {task_description}"
            })
        else:
            messages.append({
                "role": "user",
                "content": task_description
            })
        
        # 准备工具定义和工具调用映射
        # 思考模式大模型需要使用操作类工具来完成任务
        # 【修复】不要在思考模型里暴露调度类工具（call_tool_agent / call_summary_agent），
        # 否则会出现“Tool 'call_tool_agent' not found in tool_call_map”之类的错误。
        raw_tools = get_tools_schema().copy()
        thinking_tools = []
        dispatch_tools = {"call_tool_agent", "call_summary_agent"}
        for t in raw_tools:
            func = t.get("function", {})
            name = func.get("name")
            if name in dispatch_tools:
                # 这些是主模型用来调度外援/摘要代理的工具，思考模型不应再调用
                continue
            # 【规则】对于“打开网易云音乐”的任务，不向思考模型暴露通用的 open_application，
            # 这样它就只能选择 open_netease_music 这一类专用音乐工具。
            if is_open_netease_task and name == "open_application":
                continue
            thinking_tools.append(t)

        # 【优化】对于“仅打开应用”的简单任务，不需要视觉验收，
        # 禁用屏幕/视觉相关工具，避免多余的 get_screen_info 调用导致卡顿
        if simple_open_task:
            visual_tool_names = {
                "get_screen_info",
                "get_visual_info",
                "fast_screen_analysis",
                "find_color_region",
                "template_match",
            }
            filtered_tools = []
            for t in thinking_tools:
                func = t.get("function", {})
                name = func.get("name")
                if name in visual_tool_names:
                    continue
                filtered_tools.append(t)
            thinking_tools = filtered_tools
            print("[ThinkingModel] 检测到简单“打开应用”任务，已在思考模型中禁用视觉类工具以避免不必要的屏幕分析。", flush=True)
        
        # 添加键鼠/屏幕/音乐控制等操作工具定义
        # 这些工具在主模型中已移除，但思考模式大模型需要直接调用它们来执行具体操作
        mouse_keyboard_tools_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "automate_action",
                    "description": "仅在非浏览器类应用程序中执行自动化操作（记事本、画图、计算器等）。禁止用于浏览器：浏览器相关操作由主模型通过 browser_search 完成，不得使用本工具或屏幕坐标操作浏览器。Execute automation in non-browser apps only (Notepad, Paint, Calculator, etc.). NEVER use for browser; browser operations are done by main model via browser_search. Do not use coordinates for browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action_type": {
                                "type": "string",
                                "description": "操作类型：'type'、'key'、'click'（仅限非浏览器窗口）、'write'、'draw'、'wait'、'move'。禁止 'search' 或任何浏览器操作；浏览器请用主模型 browser_search。Action type: type, key, click (non-browser only), write, draw, wait, move. No search/browser; use browser_search for browser."
                            },
                            "content": {
                                "type": "string",
                                "description": "操作内容。根据 action_type：type/write 为文本，key 为按键名，draw 为图形类型，move 为 'x,y'，wait 为秒数。Content: text for type/write, key name for key, shape for draw, 'x,y' for move, seconds for wait."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题（仅限非浏览器），如 '记事本'、'Notepad'、'画图'。禁止使用 'Chrome'、'浏览器' 等；浏览器操作由主模型 browser_search 完成。Optional. Target window (non-browser only), e.g. '记事本', 'Notepad'. Never 'Chrome' or browser; use browser_search for browser."
                            },
                            "delay": {
                                "type": "number",
                                "description": "可选。操作之间的延迟时间（秒），默认0.5秒。Optional. Delay between actions in seconds, default 0.5."
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
                    "description": "在非浏览器类应用中执行一系列自动化操作。禁止用于浏览器；浏览器相关操作由主模型通过 browser_search 完成，不得使用本工具或屏幕坐标操作浏览器。Execute a sequence of automation actions in non-browser apps only. NEVER use for browser; use browser_search for browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "actions": {
                                "type": "array",
                                "description": "操作列表，每个操作是一个对象，包含 action_type, content, target_window, delay 等字段。List of actions, each action is an object with fields like action_type, content, target_window, delay.",
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
                    "name": "find_element_and_click",
                    "description": "通过屏幕分析找到指定UI元素并点击（仅限非浏览器窗口）。禁止用于浏览器：浏览器内点击/搜索等由主模型通过 browser_search 完成，不得用本工具或屏幕坐标操作浏览器。Find element and click in non-browser apps only. NEVER use for browser; use browser_search for browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "element_description": {
                                "type": "string",
                                "description": "要查找的元素描述，例如：'确定按钮'、'搜索按钮'、'关闭按钮'、'登录链接'等。Element description, e.g. 'OK button', 'Search button', 'Close button', 'Login link', etc."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题，用于先激活窗口。Optional. Target window title to activate first."
                            },
                            "search_area": {
                                "type": "string",
                                "description": "可选。搜索区域，格式 'x,y,width,height'，如果提供则只在该区域搜索。Optional. Search area in format 'x,y,width,height'."
                            },
                            "retry_times": {
                                "type": "integer",
                                "description": "可选。重试次数，默认2次。Optional. Number of retries, default 2."
                            }
                        },
                        "required": ["element_description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "find_element_and_type",
                    "description": "通过屏幕分析找到输入框并输入文本（仅限非浏览器窗口）。禁止用于浏览器；浏览器内输入/搜索由主模型通过 browser_search 完成。Find input and type in non-browser apps only. NEVER use for browser; use browser_search for browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "element_description": {
                                "type": "string",
                                "description": "要查找的输入框描述，例如：'搜索框'、'用户名输入框'、'密码输入框'等。Input field description, e.g. 'Search box', 'Username field', 'Password field', etc."
                            },
                            "text": {
                                "type": "string",
                                "description": "要输入的文本。Text to input."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题，用于先激活窗口。Optional. Target window title to activate first."
                            },
                            "search_area": {
                                "type": "string",
                                "description": "可选。搜索区域，格式 'x,y,width,height'。Optional. Search area in format 'x,y,width,height'."
                            }
                        },
                        "required": ["element_description", "text"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "analyze_and_operate",
                    "description": "分析屏幕并执行操作（仅限非浏览器窗口）。当用户要求在非浏览器应用中执行操作但不确定具体坐标时使用。禁止用于浏览器；浏览器相关操作由主模型通过 browser_search 完成，不得用本工具或屏幕坐标操作浏览器。Analyze screen and operate in non-browser apps only. NEVER use for browser; use browser_search for browser.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "operation_description": {
                                "type": "string",
                                "description": "操作描述，例如：'点击确定按钮'、'在搜索框中输入hello'、'找到关闭按钮并点击'等。Operation description, e.g. 'Click OK button', 'Type hello in search box', 'Find and click close button', etc."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题，用于先激活窗口。Optional. Target window title to activate first."
                            }
                        },
                        "required": ["operation_description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "control_netease_music",
                    "description": "控制网易云音乐播放（播放/暂停/上一首/下一首/音量/喜欢歌曲等）。使用全局快捷键，无需激活窗口，无需屏幕分析。Control NetEase CloudMusic playback (play/pause/next/previous/volume/like). Uses global hotkeys, no window activation or screen analysis needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "控制动作。支持: 'play'（播放）、'pause'（暂停）、'play_pause'（播放/暂停切换）、'next'（下一首）、'previous'（上一首）、'volume_up'（音量增加）、'volume_down'（音量减少）、'like'（喜欢歌曲）、'toggle_lyrics'（打开/关闭歌词）、'translate_lyrics'（翻译当前歌词）。Control action. Supported: 'play', 'pause', 'play_pause', 'next', 'previous', 'volume_up', 'volume_down', 'like', 'toggle_lyrics', 'translate_lyrics'."
                            }
                        },
                        "required": ["action"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "smart_click",
                    "description": "智能点击（简化接口）。通过屏幕分析找到元素并点击。当用户要求点击某个按钮或元素时，优先使用此工具。Smart click - find element via screen analysis and click. Prefer this tool when user asks to click a button or element.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "element_description": {
                                "type": "string",
                                "description": "要查找的元素描述，例如：'确定按钮'、'搜索按钮'等。Element description, e.g. 'OK button', 'Search button', etc."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题。Optional. Target window title."
                            },
                            "search_area": {
                                "type": "string",
                                "description": "可选。搜索区域，格式 'x,y,width,height'。Optional. Search area in format 'x,y,width,height'."
                            }
                        },
                        "required": ["element_description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "smart_type",
                    "description": "智能输入（简化接口）。通过屏幕分析找到输入框并输入文本。当用户要求在某个输入框中输入文本时，优先使用此工具。Smart type - find input field via screen analysis and type text. Prefer this tool when user asks to type text in an input field.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "element_description": {
                                "type": "string",
                                "description": "要查找的输入框描述，例如：'搜索框'、'用户名输入框'等。Input field description, e.g. 'Search box', 'Username field', etc."
                            },
                            "text": {
                                "type": "string",
                                "description": "要输入的文本。Text to input."
                            },
                            "target_window": {
                                "type": "string",
                                "description": "可选。目标窗口标题。Optional. Target window title."
                            },
                            "search_area": {
                                "type": "string",
                                "description": "可选。搜索区域，格式 'x,y,width,height'。Optional. Search area in format 'x,y,width,height'."
                            }
                        },
                        "required": ["element_description", "text"]
                    }
                }
            }
        ]
        
        # 添加音乐操作工具定义（这些工具在主模型中已移除，但思考模式大模型需要）
        music_tools_definitions = [
            {
                "type": "function",
                "function": {
                    "name": "open_netease_music",
                    "description": "打开网易云音乐应用程序。如果应用未运行，会启动它。Open NetEase CloudMusic application. If not running, will start it.",
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
                    "description": "在网易云音乐中搜索音乐。可以搜索歌曲名、歌手名等。此工具会在后台执行，执行后你可以立即继续与用户对话。Search for music in NetEase CloudMusic. Can search by song name, artist name, etc. This tool runs in background, you can continue conversation immediately after calling it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索关键词，可以是歌曲名、歌手名、专辑名等。Search keyword, can be song name, artist name, album name, etc."
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
                    "description": "在网易云音乐中播放音乐。可以搜索并播放，或直接播放当前音乐。此工具会在后台执行，执行后你可以立即继续与用户对话。Play music in NetEase CloudMusic. Can search and play, or play current music. This tool runs in background, you can continue conversation immediately after calling it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "可选。搜索关键词。如果提供，会先搜索再播放。Optional. Search keyword. If provided, will search first then play."
                            },
                            "play_first_result": {
                                "type": "boolean",
                                "description": "是否播放搜索结果，默认True。Whether to play the search result, default True."
                            },
                            "result_index": {
                                "type": "integer",
                                "description": "可选。播放第几个搜索结果（0表示第一首），默认0。Optional. Index of search result to play (0 for first), default 0."
                            }
                        },
                        "required": []
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "play_song_from_playlist",
                    "description": "从网易云音乐歌单中播放特定歌曲。可以指定歌单名称、歌曲名称或歌曲索引。此工具会在后台执行，执行后你可以立即继续与用户对话。Play a specific song from a NetEase CloudMusic playlist. Can specify playlist name, song name, or song index. This tool runs in background, you can continue conversation immediately after calling it.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "playlist_name": {
                                "type": "string",
                                "description": "可选。歌单名称。如果提供，会先打开该歌单。Optional. Playlist name. If provided, will open the playlist first."
                            },
                            "song_name": {
                                "type": "string",
                                "description": "可选。歌曲名称。如果提供，会在歌单中搜索并播放该歌曲。Optional. Song name. If provided, will search and play the song in the playlist."
                            },
                            "song_index": {
                                "type": "integer",
                                "description": "可选。歌曲在歌单中的索引（从0开始，0表示第一首）。如果提供，直接播放该索引的歌曲。Optional. Song index in playlist (0-based, 0 for first song). If provided, will play the song at that index directly."
                            }
                        },
                        "required": []
                    }
                }
            }
        ]
        
        # 将键鼠操作工具和音乐操作工具添加到思考模式大模型的工具列表中
        thinking_tools.extend(mouse_keyboard_tools_definitions)
        thinking_tools.extend(music_tools_definitions)

        # 主模型 TOOLS_SCHEMA 已不包含 browser_search，call_thinking_model 内思考模型需单独加入
        thinking_tools.append(browser_search_tool_schema())

        # 【新增】对于“音乐相关任务”，完全禁用所有屏幕/视觉分析类工具，
        # 避免因为过时上下文导致频繁调用 get_screen_info / fast_screen_analysis 等，
        # 音乐控制仅依赖专用音乐工具和必要的键鼠工具即可。
        if is_music_task:
            visual_for_music = {
                "get_screen_info",
                "get_visual_info",
                "fast_screen_analysis",
                "find_color_region",
                "template_match",
                "find_element_and_click",
                "find_element_and_type",
                "analyze_and_operate",
            }
            filtered = []
            for t in thinking_tools:
                func = t.get("function", {})
                name = func.get("name")
                if name in visual_for_music:
                    continue
                filtered.append(t)
            thinking_tools = filtered
            print("[ThinkingModel] 检测到音乐相关任务，已在思考模型中禁用屏幕/视觉类工具，避免不必要的屏幕分析。", flush=True)
        
        # 移除 call_thinking_model 工具本身，避免递归调用
        thinking_tools = [t for t in thinking_tools if t["function"]["name"] != "call_thinking_model"]
        
        # 构建工具调用映射 - 使用完整的 TOOLS_REGISTRY（包含所有工具，包括键鼠操作工具）
        thinking_tool_call_map = get_tools_registry().copy()
        # 移除 call_thinking_model，避免递归
        if "call_thinking_model" in thinking_tool_call_map:
            del thinking_tool_call_map["call_thinking_model"]
        
        # 执行工具调用（传入任务目标用于完成检测）
        # 【修复】run_tool_calling_turn 现在返回三个值：(final_content, tool_call_history, reasoning_contents)，
        # 这里必须按三个值解包，否则会出现 "too many values to unpack (expected 2)" 的报错。
        final_content, tool_call_history, reasoning_contents = deps.thinking_model_helper.run_tool_calling_turn(
            messages=messages,
            tools=thinking_tools,
            tool_call_map=thinking_tool_call_map,
            turn=1,
            task_goal=task_description  # 传入任务描述作为目标检测依据
        )
        
        # 构建返回结果
        result = final_content
        if tool_call_history:
            result += f"\n\n[工具调用历史]\n"
            for i, tc in enumerate(tool_call_history, 1):
                result += f"{i}. {tc['tool_name']}: {tc['result'][:100]}...\n"
        # 【新增】将思考过程一并返回，主模型可以用这些思考内容来生成更符合人设的回复
        if reasoning_contents:
            result += "\n\n[工具大模型的思考过程]："
            for rc in reasoning_contents:
                # 为避免输出过长，只截取前一部分
                snippet = rc[:400]
                result += snippet + "\n"
        
        return result
        
    except Exception as e:
        error_msg = f"思考模式大模型调用失败: {e}"
        print(f"[ThinkingModel] {error_msg}", flush=True)
        import traceback
        traceback.print_exc()
        return error_msg
