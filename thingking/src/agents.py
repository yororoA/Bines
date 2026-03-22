"""
三代理架构：主模型、工具模型、摘要模型
"""
import json
import requests
import time
from typing import Dict, List, Optional, Tuple
from config import (
    DEEPSEEK_API_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_API_TIMEOUT,
    DEEPSEEK_THINKING_API_KEY,
    DEEPSEEK_SUMMARY_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DYNAMIC_MEMORY_MODEL,
    require_env,
)
from thinking_model_helper import ThinkingModelHelper
from tools import call_tool, TOOLS_REGISTRY

# 副工具模型仅挂载的动态记忆工具 schema（与 memory_tool.update_status 参数一致，含 important_thing）
UPDATE_STATUS_SCHEMA_FOR_DYNAMIC_MEMORY = [
    {
        "type": "function",
        "function": {
            "name": "update_status",
            "description": "根据对话摘要更新当前角色扮演世界中的动态状态，包括地点、好感度、背包物品、当前任务、NPC状态和记忆亮点等。仅输出有变化的字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "current_time": {"type": "string", "description": "当前时间戳（ISO格式）"},
                    "current_location": {"type": "string", "description": "当前所在地点或场景"},
                    "relationship_delta": {"type": "integer", "description": "关系增量（正数提升好感，负数降低好感）；relationship_level/score/distribution 由系统根据 delta 自动计算，不要传 relationship_level。"},
                    "add_item": {"type": "string", "description": "获得的新物品名称"},
                    "remove_item": {"type": "string", "description": "失去或消耗的物品名称"},
                    "active_quest": {"type": "string", "description": "当前正在进行的任务或目标描述"},
                    "npc_name": {"type": "string", "description": "更新NPC的名称"},
                    "npc_attire": {"type": "string", "description": "更新NPC的服装描述"},
                    "npc_visual_status": {"type": "string", "description": "更新视觉模块的状态"},
                    "npc_activity": {"type": "string", "description": "更新NPC当前正在进行的活动"},
                    "add_memory_highlight": {"type": "string", "description": "添加一条记忆亮点"},
                    "remove_memory_highlight": {"type": "string", "description": "移除一条记忆亮点"},
                    "important_thing": {"type": "string", "description": "当前重要的人/事/物（一句话描述）"},
                },
                "required": []
            }
        }
    }
]


def _clean_messages_for_tool_history(messages: List[Dict], agent_label: str) -> List[Dict]:
    """
    清理消息历史，移除未完成的 tool_calls。

    如果 assistant 消息包含 tool_calls，但后面没有对应的 tool 消息，
    则移除该 assistant 消息的 tool_calls 字段。由于清理后 tool 会成为孤立消息，
    因此统一不保留 role=tool 的消息。
    """
    if not messages:
        return []

    cleaned = []
    pending_tool_call_ids = set()

    for msg in messages:
        role = msg.get("role")

        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_call_ids.add(tc_id)

                cleaned_msg = msg.copy()
                cleaned_msg.pop("tool_calls", None)
                if cleaned_msg.get("content") or cleaned_msg.get("reasoning_content"):
                    cleaned.append(cleaned_msg)
            else:
                cleaned.append(msg)

        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                pending_tool_call_ids.discard(tc_id)
            # 清理后不再保留 role=tool 消息，避免孤立 tool 报错

        else:
            cleaned.append(msg)

    if pending_tool_call_ids:
        print(
            f"[{agent_label}] Warning: Found {len(pending_tool_call_ids)} incomplete tool_calls, removed them",
            flush=True,
        )

    return cleaned


class MainAgent:
    """
    主模型代理
    职责：
    1. 情感分析/意图识别
    2. 对话生成
    3. 调度决策（调用 ToolAgent 或 SummaryAgent）
    工具列表由外部注入（handle_zmq 的 TOOLS_SCHEMA），保证与主流程一致。
    """

    def get_system_prompt(self) -> str:
        """主模型系统提示：情感分析、任务规划、调度规则；含语义层面的「不擅长/拒绝」策略。"""
        return """你是一个情感分析助手和对话生成器。你的职责是：
1. 分析用户的情感和意图
2. 生成自然、有情感的对话回复
3. 判断是否需要调用工具代理（call_tool_agent）或摘要代理（call_summary_agent，若可用）

重要规则：
- 如果用户询问过去的对话、记忆、聊天历史，直接从你的上下文（<memory_recall>、QQ历史、消息历史）中回答，不要调用 call_tool_agent
- 如果用户需要执行实际操作（打开应用、查看当前屏幕、视觉分析、浏览器搜索等），必须调用 call_tool_agent
- 如果对话中发生状态变化（地点变化、好感度变化、获得物品等）且你有 call_summary_agent 工具时，可调用 call_summary_agent
- 不要直接调用操作工具，只能通过 call_tool_agent 间接调用
- 保持你的角色人设和情感表达
- 禁止输出括号内的动作描述（如（点头）、（微笑）、(nodding) 等），只允许输出要说的台词文本，或不输出；若无需回复则不要输出任何内容

不擅长/拒绝策略（按语义判断，不要仅看某些词）：
- 当用户是在「请你写代码、解释代码、调试、讲编程/数学/科学/历史知识」时，你可以以傲娇人设抱怨、嫌麻烦或温和拒绝，但必须给出文字回复，不能沉默。
- 当用户只是在做别的事（例如「帮我把这个代码文件重命名」「打开某个程序」等操作类请求）时，应正常协助。即：区分「请求你本人做编程/讲题」与「请求你帮忙执行操作」，后者照常协助。"""

    def __init__(self):
        self.api_key = require_env("DEEPSEEK_API_KEY", DEEPSEEK_API_KEY)
        self.api_url = DEEPSEEK_API_URL
        self.model = DEEPSEEK_MODEL
        self.timeout = DEEPSEEK_API_TIMEOUT
        # 主模型工具列表由 handle_zmq 通过 set_tools_schema(TOOLS_SCHEMA) 注入，此处仅占位
        self.tools_schema = []

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _post_chat_completion(self, payload: Dict, stream: bool = False):
        return requests.post(
            self.api_url,
            headers=self._build_headers(),
            json=payload,
            stream=stream,
            timeout=self.timeout,
        )

    def set_tools_schema(self, tools_schema: List[Dict]):
        """设置主模型可见的工具 schema（由 handle_zmq 的 TOOLS_SCHEMA 注入）。"""
        self.tools_schema = list(tools_schema) if tools_schema else []

    def _ensure_system_prompt(self, messages: List[Dict]) -> List[Dict]:
        """若首条不是 system，则在开头插入主模型系统提示。"""
        if not messages or messages[0].get("role") != "system":
            return [{"role": "system", "content": self.get_system_prompt()}] + list(messages)
        return list(messages)

    def _ensure_router_prompt(self, messages: List[Dict]) -> List[Dict]:
        """兼容旧调用：插入主系统提示。主流程请使用 run_one_turn_streaming(messages) 与 tools。"""
        if not messages or messages[0].get("role") != "system":
            return [{"role": "system", "content": self.get_system_prompt()}] + list(messages)
        return list(messages)

    def run_one_turn_streaming_text_only(self, messages: List[Dict], interrupt_check=None):
        """
        仅文本流式、不传 tools，保留用于兼容。主流程请使用 run_one_turn_streaming。
        返回: (generator, holder)，holder["message"] 仅含 content（无 tool_calls）。
        """
        holder = {}
        msgs = self._ensure_router_prompt(messages)
        payload = {
            "messages": msgs,
            "model": self.model,
            "frequency_penalty": 0,
            "max_tokens": 4096,
            "presence_penalty": 0,
            "stream": True,
            "temperature": 0.7,
            "top_p": 1,
        }
        try:
            response = self._post_chat_completion(payload, stream=True)
            if response.status_code != 200:
                holder["error"] = f"HTTP {response.status_code}: {response.text[:500]}"
                return iter([]), holder
        except Exception as e:
            holder["error"] = str(e)
            return iter([]), holder

        full_content = [""]

        def _gen():
            try:
                for line in response.iter_lines():
                    if line is None:
                        break
                    if interrupt_check and interrupt_check():
                        holder["interrupted"] = True
                        return
                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line_str.startswith("data: "):
                        line_str = line_str[6:]
                    line_str = line_str.strip()
                    if line_str == "[DONE]":
                        break
                    try:
                        chunk_json = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk_json.get("choices")
                    if not choices or not isinstance(choices, list):
                        continue
                    delta = choices[0].get("delta", {})
                    if "reasoning_content" in delta:
                        continue
                    if delta.get("content"):
                        full_content[0] += delta["content"]
                        yield delta["content"]
            finally:
                holder["message"] = {"role": "assistant", "content": full_content[0] or None}

        return _gen(), holder

    def run_one_turn_streaming(self, messages: List[Dict], interrupt_check=None):
        """
        执行一轮流式请求，由 process_message 驱动工具循环。
        Yields: 内容片段 (str)
        返回: (generator, holder)
        holder 在流结束后包含: message = {role, content, tool_calls?}, 可选 error, interrupted
        """
        holder = {}
        msgs = self._ensure_system_prompt(messages)
        # 若上文含「仅内部参考」的思考过程，在首条 system 后插入提醒，减少对人设的干扰
        if msgs and any(
            m.get("role") == "tool" and "仅内部参考" in (m.get("content") or "")
            for m in msgs
        ):
            idx = 1 if (len(msgs) > 0 and msgs[0].get("role") == "system") else 0
            msgs.insert(idx, {
                "role": "system",
                "content": "本轮回复时：上文工具结果中可能含「仅内部参考」的思考过程，请勿在回复中照搬或使用理性/分析性话术，保持角色人设。"
            })
        payload = {
            "messages": msgs,
            "model": self.model,
            "frequency_penalty": 0,
            "max_tokens": 4096,
            "presence_penalty": 0,
            "stop": None,
            "stream": True,
            "temperature": 0.7,
            "top_p": 1,
            "tools": self.tools_schema,
            "tool_choice": "auto",
        }
        try:
            response = self._post_chat_completion(payload, stream=True)
            if response.status_code != 200:
                holder["error"] = f"HTTP {response.status_code}: {response.text[:500]}"
                return iter([]), holder
        except Exception as e:
            holder["error"] = str(e)
            return iter([]), holder

        tool_calls_data = {}
        tool_call_content_cache = [""]
        full_content = [""]

        def _gen():
            nonlocal tool_call_content_cache, full_content
            try:
                for line in response.iter_lines():
                    if line is None:
                        break
                    if interrupt_check and interrupt_check():
                        holder["interrupted"] = True
                        return
                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                    if line_str.startswith("data: "):
                        line_str = line_str[6:]
                    line_str = line_str.strip()
                    if line_str == "[DONE]":
                        break
                    try:
                        chunk_json = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk_json.get("choices")
                    if not choices or not isinstance(choices, list):
                        continue
                    delta = choices[0].get("delta", {})
                    if "reasoning_content" in delta:
                        continue
                    if delta.get("tool_calls"):
                        holder["has_tool_calls"] = True
                        for tc in delta.get("tool_calls", []):
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_data:
                                tool_calls_data[idx] = {
                                    "id": tc.get("id"),
                                    "type": tc.get("type"),
                                    "function": {"name": "", "arguments": ""},
                                }
                            if "function" in tc:
                                if "name" in tc["function"]:
                                    tool_calls_data[idx]["function"]["name"] += tc["function"].get("name", "")
                                if "arguments" in tc["function"]:
                                    tool_calls_data[idx]["function"]["arguments"] += tc["function"].get("arguments", "")
                            if tc.get("id"):
                                tool_calls_data[idx]["id"] = tc["id"]
                            if tc.get("type"):
                                tool_calls_data[idx]["type"] = tc["type"]
                        if delta.get("content"):
                            tool_call_content_cache[0] += delta.get("content", "")
                        continue
                    if delta.get("content"):
                        full_content[0] += delta["content"]
                        yield delta["content"]
            finally:
                sorted_indexes = sorted(k for k in tool_calls_data if tool_calls_data[k]["function"]["name"])
                final_tool_calls = []
                for idx in sorted_indexes:
                    item = tool_calls_data[idx]
                    if not item["function"]["name"]:
                        continue
                    final_tool_calls.append({
                        "id": item["id"],
                        "type": item.get("type", "function"),
                        "function": {
                            "name": item["function"]["name"],
                            "arguments": item["function"]["arguments"],
                        },
                    })
                asst_content = tool_call_content_cache[0] or full_content[0] or None
                asst_msg = {"role": "assistant", "content": asst_content}
                if final_tool_calls:
                    asst_msg["tool_calls"] = final_tool_calls
                holder["message"] = asst_msg

        return _gen(), holder

    def handle(self, user_input: str, context_messages: List[Dict], 
               tool_agent: 'ToolAgent', summary_agent: 'SummaryAgent') -> Dict:
        """
        处理用户输入，返回决策结果
        
        Returns:
            Dict with keys:
                - need_tool: bool, 是否需要工具
                - need_state_update: bool, 是否需要状态更新
                - reply: str, 直接回复（如果不需要工具）
                - tool_task: str, 工具任务描述（如果需要工具）
                - state_update_desc: str, 状态更新描述（如果需要状态更新）
        """
        # 构建消息
        messages = context_messages.copy()
        messages.append({
            "role": "user",
            "content": user_input
        })
        
        # 添加系统提示（与 run_one_turn_streaming 一致）
        messages.insert(0, {"role": "system", "content": self.get_system_prompt()})
        
        # 调用主模型
        payload = {
            "messages": messages,
            "model": self.model,
            "temperature": 0.8,
            "max_tokens": 4096,
            "tools": self.tools_schema,
            "tool_choice": "auto",
            "stream": False
        }

        try:
            response = self._post_chat_completion(payload, stream=False)
            response.raise_for_status()
            result = response.json()
            
            message = result["choices"][0]["message"]
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])
            
            # 处理工具调用
            if tool_calls:
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"])
                    
                    if func_name == "call_tool_agent":
                        # 调用工具代理
                        task_desc = args.get("task_description", "")
                        context = args.get("context", "")
                        tool_result = tool_agent.handle_task(task_desc, context, context_messages)
                        
                        return {
                            "need_tool": True,
                            "need_state_update": False,
                            "reply": None,
                            "tool_task": task_desc,
                            "tool_result": tool_result,
                            "state_update_desc": None
                        }
                    
                    elif func_name == "call_summary_agent":
                        # 调用摘要代理
                        state_desc = args.get("state_update_description", "")
                        context = args.get("context", "")
                        state_result = summary_agent.update_state(state_desc, context, context_messages)
                        
                        return {
                            "need_tool": False,
                            "need_state_update": True,
                            "reply": None,
                            "tool_task": None,
                            "tool_result": None,
                            "state_update_desc": state_desc,
                            "state_result": state_result
                        }
            
            # 没有工具调用，直接回复
            return {
                "need_tool": False,
                "need_state_update": False,
                "reply": content,
                "tool_task": None,
                "tool_result": None,
                "state_update_desc": None
            }
            
        except Exception as e:
            print(f"[MainAgent] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return {
                "need_tool": False,
                "need_state_update": False,
                "reply": f"抱歉，处理时出现错误：{str(e)}",
                "tool_task": None,
                "tool_result": None,
                "state_update_desc": None
            }
    
    def reply_with_result(self, user_input: str, tool_result: str, 
                         state_result: Optional[str] = None) -> str:
        """
        基于工具结果生成情感化回复
        """
        messages = [
            {
                "role": "system",
                "content": "你是一个情感助手。基于工具执行结果，生成对用户友好、有情感的自然语言回复。保持你的角色人设。"
            },
            {
                "role": "user",
                "content": f"用户说：{user_input}\n\n工具执行结果：{tool_result}"
            }
        ]
        
        if state_result:
            messages[1]["content"] += f"\n\n状态更新结果：{state_result}"
        
        payload = {
            "messages": messages,
            "model": self.model,
            "temperature": 0.8,
            "max_tokens": 2048,
            "stream": False
        }

        try:
            response = self._post_chat_completion(payload, stream=False)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[MainAgent] Error generating reply: {e}", flush=True)
            return f"任务已完成：{tool_result}"


class ToolAgent:
    """
    工具代理（外援大模型）
    职责：
    1. 接收任务描述
    2. 规划工具调用序列
    3. 执行所有操作工具
    4. 返回执行结果
    """
    
    def __init__(self):
        # 主工具模型使用 chat，不使用 reasoner/thinking
        self.thinking_helper = ThinkingModelHelper(use_thinking=False)
        
        # 工具代理可以调用的所有操作工具（排除状态更新工具）
        self.operational_tools = []
        # 从 TOOLS_REGISTRY 中获取所有工具，排除 update_status
        for tool_name in TOOLS_REGISTRY.keys():
            if tool_name != "update_status":
                self.operational_tools.append(tool_name)
        
        # 构建工具 schema（从主模型的 TOOLS_SCHEMA 中筛选）
        # 这里我们需要从 handle_zmq.py 中导入 TOOLS_SCHEMA，但为了避免循环导入，
        # 我们动态构建工具 schema
        self.tools_schema = self._build_tools_schema()
    
    def _build_tools_schema(self) -> List[Dict]:
        """动态构建工具 schema，排除 update_status"""
        # 默认返回空列表，需要通过 set_tools_schema 设置
        return []
    
    def set_tools_schema(self, tools_schema: List[Dict]):
        """设置工具 schema（从 handle_zmq.py 传入）。工具大模型不得调用摘要模型或动态记忆。"""
        # 过滤掉 update_status、call_summary_agent：动态记忆仅由摘要模型在摘要时自动更新
        excluded = {"update_status", "call_summary_agent"}
        self.tools_schema = [
            tool for tool in tools_schema
            if tool.get("function", {}).get("name") not in excluded
        ]
    
    def _clean_messages(self, messages: List[Dict]) -> List[Dict]:
        return _clean_messages_for_tool_history(messages, "ToolAgent")
    
    def handle_task(self, task_description: str, context: str, 
                   base_messages: List[Dict], progress_callback=None, interrupt_check=None) -> str:
        """
        处理任务，执行工具调用
        
        Args:
            task_description: 任务描述
            context: 上下文信息
            base_messages: 基础消息列表（用于上下文）
            progress_callback: 进度回调函数，接收 (status, message) 参数
        
        Returns:
            str: 工具执行结果
        """
        # 构建消息
        # 【修复】清理消息历史，移除未完成的 tool_calls
        messages = self._clean_messages(base_messages) if base_messages else []
        
        # 添加任务描述；工具列表以 API 传入的 tools schema 为准，不在 prompt 中重复描述
        task_prompt = f"""你需要执行以下任务：
{task_description}

{f'''【重要上下文】你刚刚已经对用户回复了以下内容（请基于此承诺去执行，不要重复说这些话，只输出工具执行后的最终结果）：
"{context}"
''' if context else ""}

【重要约束】你不具备访问对话历史或记忆的能力。如果任务要求检索过去的对话内容、聊天记录或记忆，请直接调用 task_complete 并回复"历史对话信息已在主模型上下文中，无需工具操作，请直接从上下文回答用户。"。get_screen_info 仅用于查看用户当前屏幕的实时画面，绝不可用于获取过去的对话内容。

【动态（moments）工具】若任务涉及看动态、评论、点赞、看评论、看动态图片：必须先调用一次 get_moments 获取列表，后续 comment_moment、get_comments、like_moment、like_comment、analyze_moment_images 一律使用该次返回的 data 中的 _id（或 comments 中的 id），不得为获取 _id 重复调用 get_moments。

请根据任务需要调用工具（工具说明以 API 传入的 schema 为准）。可自主连续多轮调用工具，根据结果再决定是否继续调用或调用 task_complete 提交最终结果。"""
        
        messages.append({
            "role": "user",
            "content": task_prompt
        })
        
        # 使用思考模式大模型执行工具调用
        try:
            # 【优化】输出任务开始描述
            print(f"[ToolAgent] 开始执行任务: {task_description[:100]}...", flush=True)
            if progress_callback:
                try:
                    progress_callback("task_start", f"开始执行任务: {task_description[:50]}...")
                except:
                    pass
            
            # 仅允许执行 schema 中已启用的工具，未勾选工具即使被模型返回也不执行
            allowed_tool_names = {t.get("function", {}).get("name") for t in self.tools_schema}
            tool_call_map_filtered = {k: v for k, v in TOOLS_REGISTRY.items() if k in allowed_tool_names}
            final_content, tool_history, reasoning_contents = self.thinking_helper.run_tool_calling_turn(
                messages=messages,
                tools=self.tools_schema,
                tool_call_map=tool_call_map_filtered,
                turn=1,
                task_goal=task_description,
                progress_callback=progress_callback,
                interrupt_check=interrupt_check,
                max_iterations=25,  # 工具模型可自主连续多轮调用，适当提高上限以支持多步任务
            )
            
            # 【优化】输出任务完成描述
            if tool_history:
                executed_tools = [tc.get("tool_name") for tc in tool_history]
                print(f"[ToolAgent] 任务执行完成，已调用工具: {', '.join(executed_tools)}", flush=True)
                if progress_callback:
                    try:
                        progress_callback("task_complete", f"任务执行完成，已调用工具: {', '.join(executed_tools)}")
                    except:
                        pass
            
            # 【新增】将思考过程包含在返回结果中，让主模型能够基于这些思考生成符合角色性格的回复
            # 格式：将 reasoning_contents 作为额外信息附加到结果中
            result = final_content or "任务执行完成"
            if reasoning_contents:
                # 将思考过程作为额外信息传递给主模型
                reasoning_summary = "\n\n[工具大模型的思考过程]：" + "\n".join(reasoning_contents)
                result = result + reasoning_summary
                print(f"[ToolAgent] 已收集 {len(reasoning_contents)} 条思考过程，将传递给主模型", flush=True)
            
            return result
        except Exception as e:
            print(f"[ToolAgent] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            if progress_callback:
                try:
                    progress_callback("task_error", f"工具执行出错：{str(e)}")
                except:
                    pass
            return f"工具执行出错：{str(e)}"


class SummaryAgent:
    """
    摘要代理（思考模式/摘要模型）
    职责：
    1. 接收状态更新描述
    2. 分析对话和工具执行结果
    3. 更新状态和记忆
    4. 返回更新结果
    """
    
    def __init__(self):
        # 使用思考模式大模型作为摘要代理
        self.thinking_helper = ThinkingModelHelper()
        
        # 摘要代理只能调用状态更新工具
        self.tools_schema = [
            {
                "type": "function",
                "function": {
                    "name": "update_status",
                    "description": "更新当前角色扮演世界中的动态状态，包括地点、好感度（关系）、背包物品、当前任务、用户状态、NPC状态和记忆亮点。只有在对话中明确发生这些变化时才调用此工具。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "current_time": {"type": "string", "description": "当前时间戳（ISO格式）"},
                            "current_location": {"type": "string", "description": "当前所在地点或场景"},
                            "relationship_delta": {"type": "integer", "description": "关系增量（正数提升好感，负数降低好感）；level/score/distribution 由系统根据 delta 自动计算。"},
                            "add_item": {"type": "string", "description": "获得的新物品名称"},
                            "remove_item": {"type": "string", "description": "失去或消耗的物品名称"},
                            "active_quest": {"type": "string", "description": "当前正在进行的任务或目标描述"},
                            "npc_name": {"type": "string", "description": "更新NPC的名称"},
                            "npc_attire": {"type": "string", "description": "更新NPC的服装描述"},
                            "npc_visual_status": {"type": "string", "description": "更新视觉模块的状态"},
                            "npc_activity": {"type": "string", "description": "更新NPC当前正在进行的活动"},
                            "add_memory_highlight": {"type": "string", "description": "添加一条记忆亮点"},
                            "remove_memory_highlight": {"type": "string", "description": "移除一条记忆亮点"}
                        },
                        "required": []
                    }
                }
            }
        ]
    
    def _clean_messages(self, messages: List[Dict]) -> List[Dict]:
        return _clean_messages_for_tool_history(messages, "SummaryAgent")
    
    def update_state(self, state_update_description: str, context: str,
                    base_messages: List[Dict]) -> str:
        """
        更新状态和记忆
        
        Args:
            state_update_description: 状态更新描述
            context: 上下文信息
            base_messages: 基础消息列表（用于上下文）
        
        Returns:
            str: 状态更新结果
        """
        # 构建消息
        # 【修复】清理消息历史，移除未完成的 tool_calls
        messages = self._clean_messages(base_messages) if base_messages else []
        
        # 添加状态更新描述
        update_prompt = f"""你需要更新以下状态：
{state_update_description}

{f"上下文信息：{context}" if context else ""}

请分析对话内容，确定需要更新的状态字段，并调用 update_status 工具进行更新。
只更新明确发生变化的字段，不要更新未变化的字段。"""
        
        messages.append({
            "role": "user",
            "content": update_prompt
        })
        
        # 使用思考模式大模型执行状态更新
        try:
            final_content, tool_history, reasoning_contents = self.thinking_helper.run_tool_calling_turn(
                messages=messages,
                tools=self.tools_schema,
                tool_call_map={"update_status": TOOLS_REGISTRY.get("update_status")},
                turn=1,
                task_goal="更新状态和记忆"
            )
            
            # 【新增】将思考过程包含在返回结果中（如果需要的话）
            result = final_content or "状态更新完成"
            if reasoning_contents:
                reasoning_summary = "\n\n[摘要代理的思考过程]：" + "\n".join(reasoning_contents)
                result = result + reasoning_summary
            
            return result
        except Exception as e:
            print(f"[SummaryAgent] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return f"状态更新出错：{str(e)}"

    def summarize_content(self, content: str) -> str:
        """
        对给定的内容进行摘要总结。
        
        Args:
            content: 需要总结的文本内容
            
        Returns:
            str: 总结后的摘要
        """
        if not content:
            return ""
            
        messages = [
            {"role": "system", "content": "你是一个专业的对话摘要助手。你的任务是对提供的聊天记录进行简洁、准确的总结。抓住主要话题、关键信息、参与者和他们的观点。忽略无关的寒暄或刷屏。"},
            {"role": "user", "content": f"请对以下聊天记录进行总结：\n\n{content}"}
        ]
        
        try:
            # 使用助手类内部的 client
            response = self.thinking_helper.client.chat.completions.create(
                model=self.thinking_helper.model, # 使用配置的模型
                messages=messages,
                stream=False,
                temperature=0.3, # 摘要不需要太高创造性
                max_tokens=1024
            )
            
            summary = response.choices[0].message.content.strip() if response.choices else ""
            return summary
            
        except Exception as e:
            print(f"[SummaryAgent] Summarize content error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return f"Summarization failed: {str(e)}"


class DynamicMemoryToolAgent:
    """
    副工具模型（reasoner）：仅挂载动态记忆工具 update_status。
    配置与现有工具模型类似（API Key 同 DEEPSEEK_THINKING_API_KEY，模型为 reasoner）。
    由摘要流程在摘要写入 buffer RAG 后调用，根据摘要内容更新 memory_dynamic.json。
    """
    def __init__(self):
        self.thinking_helper = ThinkingModelHelper(
            api_key=DEEPSEEK_THINKING_API_KEY,
            model=DEEPSEEK_DYNAMIC_MEMORY_MODEL,
            use_thinking=True,  # 副工具模型（动态记忆）保留 reasoner
        )
        self.tools_schema = UPDATE_STATUS_SCHEMA_FOR_DYNAMIC_MEMORY
        self._update_status_fn = TOOLS_REGISTRY.get("update_status")

    def update_dynamic_memory_from_summary(self, summary_text: str, current_dynamic_json: str) -> str:
        """
        根据摘要与当前 memory_dynamic.json 内容，调用 reasoner 生成 update_status 工具调用并执行。
        current_dynamic_json: 当前动态记忆的完整 JSON（与 memory_dynamic.json 结构一致），用于判断需要更新哪些字段。
        """
        if not self._update_status_fn:
            print("[DynamicMemoryToolAgent] update_status 未注册，跳过动态记忆更新", flush=True)
            return ""
        messages = [
            {
                "role": "system",
                "content": (
                    "你负责根据「对话摘要」更新角色扮演世界的动态状态（地点、关系、物品、任务、NPC、记忆亮点等）。"
                    "你会收到当前 memory_dynamic.json 的完整内容，请据此判断哪些字段需要更新。"
                    "仅调用 update_status 工具，且只传入有变化的字段；无需更新的不要传。"
                    "若摘要中无任何状态变化，可不调用工具或传空对象。\n"
                    "【关系/好感度更新】若摘要中出现关系或好感度变化（如变亲密、发生冲突、被安抚、关系升温、冷淡等），必须传入 relationship_delta（整数）：正数提升好感（如 +1 小提升、+2 明显、+3 重大），负数降低（如 -1、-2）。relationship_level/relationship_score/relationship_distribution 由系统根据 delta 自动计算，不要传 relationship_level。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【当前 memory_dynamic.json 内容（用于判断需要更新哪些字段）】\n{current_dynamic_json}\n\n"
                    f"【对话摘要】\n{summary_text.strip()}\n\n请根据摘要更新动态记忆（仅输出有变化的字段）；若有关系/好感度变化请务必传入 relationship_delta（整数）。"
                ),
            },
        ]
        try:
            final_content, tool_history, _ = self.thinking_helper.run_tool_calling_turn(
                messages=messages,
                tools=self.tools_schema,
                tool_call_map={"update_status": self._update_status_fn},
                turn=1,
                task_goal="根据摘要更新动态记忆",
            )
            if tool_history:
                print(f"[DynamicMemoryToolAgent] 已根据摘要更新动态记忆: {[t.get('tool_name') for t in tool_history]}", flush=True)
            return final_content or ""
        except Exception as e:
            print(f"[DynamicMemoryToolAgent] Error: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return ""
