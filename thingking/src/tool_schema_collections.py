from tool_schema_defs import get_time_tool_schema, browser_search_tool_schema


# 工具定义
# 【三代理架构】主模型可见：get_time、open_application、automate_action、automate_sequence、call_tool_agent（browser_search 已移至工具模型）
# 浏览器相关请求由主模型通过 call_tool_agent 交给工具模型，由工具模型调用 browser_search
TOOLS_SCHEMA = [
    get_time_tool_schema(),
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

# 主模型路由层：仅保留 get_time + call_summary_agent
ROUTER_TOOLS_SCHEMA = [
    get_time_tool_schema(),
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
    get_time_tool_schema(),
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
    browser_search_tool_schema(),
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
