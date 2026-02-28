"""
思考模式大模型工具调用辅助模块
使用思考模式的DeepSeek API进行工具调用
"""
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from config import (
    DEEPSEEK_THINKING_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    require_env,
)
from tool_call_utils import _format_moments_result


def _resolve_model(model_from_init):
    """Resolve model: init param > env DEEPSEEK_MODEL."""
    return model_from_init if model_from_init is not None else DEEPSEEK_MODEL

# 定义键鼠操作相关的工具名称
MOUSE_KEYBOARD_TOOLS = {
    "automate_action",
    "automate_sequence",
    "find_element_and_click",
    "find_element_and_type",
    "analyze_and_operate",
    "smart_click",
    "smart_type"
}


class ThinkingModelHelper:
    """大模型助手，用于复杂工具调用场景"""
    
    def __init__(self, api_key=None, base_url=None, model=None, use_thinking=False):
        """
        初始化   
        
        Args:
            api_key: API密钥，默认使用环境变量或提供的密钥
            base_url: API基础URL，默认使用DeepSeek官方URL
            model: 模型名称，默认使用 DEEPSEEK_MODEL；副工具模型可传入 DEEPSEEK_DYNAMIC_MEMORY_MODEL
            use_thinking: 是否启用 reasoner/thinking 模式；主工具模型用 chat 不启用，副工具（动态记忆）可启用
        """
        resolved_api_key = api_key or DEEPSEEK_THINKING_API_KEY
        self.api_key = require_env("DEEPSEEK_THINKING_API_KEY", resolved_api_key)
        resolved_base_url = base_url or DEEPSEEK_BASE_URL
        self.base_url = resolved_base_url
        self.model = _resolve_model(model)
        self.use_thinking = use_thinking
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
    
    def clear_reasoning_content(self, messages):
        """
        清除历史消息中的reasoning_content，节省网络带宽
        
        Args:
            messages: 消息列表
        """
        for message in messages:
            if hasattr(message, 'reasoning_content'):
                message.reasoning_content = None
            elif isinstance(message, dict) and 'reasoning_content' in message:
                message['reasoning_content'] = None
    
    def run_tool_calling_turn(self, messages, tools, tool_call_map, turn=1, task_goal=None, progress_callback=None, max_iterations=20, interrupt_check=None):
        """
        执行工具调用，支持模型自主连续多轮调用工具（每轮执行工具后把结果送回模型，由模型决定是否继续调用或给出最终回复）。
        
        Args:
            messages: 消息列表
            tools: 工具定义列表
            tool_call_map: 工具名称到函数的映射
            turn: 当前轮次（用于日志）
            task_goal: 任务目标描述，用于检测任务是否完成
            progress_callback: 进度回调函数，接收 (status, message) 参数，用于发送中间状态
            max_iterations: 最大工具调用轮数，防止无限循环（默认 20，工具模型可传更大值以支持更长链路）
            interrupt_check: 可选的回调函数（无参，返回bool），若返回 True，则立即中断工具调用循环
            
        Returns:
            tuple: (最终回复内容, 工具调用历史, 思考过程列表)
        """
        sub_turn = 1
        tool_call_history = []
        reasoning_contents = []  # 收集所有思考过程
        task_complete_result = None  # 模型调用 task_complete 时提交的最终结果，用于强制收敛
        
        while sub_turn <= max_iterations:
            # 每一轮开始前，检查是否被中断
            if interrupt_check and interrupt_check():
                print(f"[ThinkingModel] Tool execution interrupted by user during turn {sub_turn}", flush=True)
                return "（任务已被用户打断）", tool_call_history, reasoning_contents

            try:
                kwargs = dict(model=self.model, messages=messages, tools=tools)
                if self.use_thinking:
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                response = self.client.chat.completions.create(**kwargs)
                
                message = response.choices[0].message
                
                # 将消息转换为字典格式以便后续处理
                message_dict = {
                    "role": "assistant",
                    "content": message.content
                }
                
                # 处理 reasoning_content
                reasoning_content = getattr(message, 'reasoning_content', None)
                if reasoning_content:
                    message_dict["reasoning_content"] = reasoning_content
                    # 【新增】收集思考过程，用于传递给主模型
                    reasoning_contents.append(reasoning_content)
                
                # 处理 tool_calls
                tool_calls = getattr(message, 'tool_calls', None)
                if tool_calls:
                    # 将 tool_calls 转换为字典格式
                    tool_calls_dict = []
                    for tc in tool_calls:
                        tc_dict = {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        tool_calls_dict.append(tc_dict)
                    message_dict["tool_calls"] = tool_calls_dict
                
                messages.append(message_dict)
                
                content = message.content
                
                print(f"[ThinkingModel] Turn {turn}.{sub_turn}")
                print(f"  reasoning_content: {reasoning_content[:100] if reasoning_content else None}...")
                print(f"  content: {content[:100] if content else None}...")
                print(f"  tool_calls: {len(tool_calls) if tool_calls else 0}")
                
                # 如果没有工具调用，说明已经得到最终答案
                if not tool_calls:
                    break
                
                # 标准化：本轮 tool_calls 转为 (index, call_id, func_name, args_str, args) 列表，保证顺序与 tool_call_id 一致
                tc_list = []
                for idx, tool_call in enumerate(tool_calls):
                    if isinstance(tool_call, dict):
                        func_name = tool_call["function"]["name"]
                        args_str = tool_call["function"]["arguments"]
                        call_id = tool_call["id"]
                    else:
                        func_name = tool_call.function.name
                        args_str = tool_call.function.arguments
                        call_id = tool_call.id
                    try:
                        args = json.loads(args_str) if args_str else {}
                    except json.JSONDecodeError:
                        args = {}
                    tc_list.append((idx, call_id, func_name, args_str, args))
                
                # task_complete：显式结束，提取 result 供最终返回
                for (_, _, func_name, _, args) in tc_list:
                    if func_name == "task_complete":
                        task_complete_result = args.get("result", "")
                        break
                
                def _run_one_tool(item):
                    idx, call_id, func_name, args_str, args = item
                    tool_function = tool_call_map.get(func_name)
                    if not tool_function:
                        return idx, call_id, func_name, args_str, f"Error: Tool '{func_name}' not found in tool_call_map"
                    try:
                        tool_result = tool_function(**args)
                        if func_name in ("get_moments", "add_moment", "comment_moment", "get_comments", "like_moment", "like_comment", "analyze_moment_images"):
                            result = _format_moments_result(tool_result)
                        else:
                            result = str(tool_result)
                        return idx, call_id, func_name, args_str, result
                    except Exception as e:
                        return idx, call_id, func_name, args_str, f"Error executing '{func_name}': {e}"
                
                # 并行执行本轮所有工具调用，再按顺序回填 Tool Message
                results_by_idx = {}
                with ThreadPoolExecutor(max_workers=min(len(tc_list), 8)) as executor:
                    futures = {executor.submit(_run_one_tool, item): item[0] for item in tc_list}
                    for future in as_completed(futures):
                        idx, call_id, func_name, args_str, result = future.result()
                        results_by_idx[idx] = (call_id, func_name, args_str, result)
                        print(f"[ThinkingModel] 工具完成: {func_name}, 结果长度: {len(result)}", flush=True)
                
                MUSIC_TOOLS = {
                    "open_netease_music", "search_music_in_netease", "play_music_in_netease",
                    "control_netease_music", "play_song_from_playlist", "get_playlist_name_in_netease", "switch_to_playlist"
                }
                for idx in sorted(results_by_idx.keys()):
                    call_id, func_name, args_str, result = results_by_idx[idx]
                    if func_name in MOUSE_KEYBOARD_TOOLS and func_name not in MUSIC_TOOLS:
                        get_screen_info = tool_call_map.get("get_screen_info")
                        if get_screen_info:
                            try:
                                time.sleep(0.5)
                                screen_info = get_screen_info(
                                    simple_recognition=False, fast_mode=True,
                                    focus_description="请基于当前截图，重点描述这次键鼠操作后屏幕上发生的变化，尤其是与用户任务相关的区域和文字。"
                                )
                                result += f"\n\n[屏幕更新] 操作后的屏幕状态：\n{screen_info[:500]}"
                            except Exception as e:
                                result += f"\n[警告] 无法获取最新屏幕信息: {e}"
                    tool_call_history.append({"tool_name": func_name, "arguments": args_str, "result": result})
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                    if progress_callback:
                        try:
                            progress_callback("tool_complete", result[:100])
                        except Exception:
                            pass
                
                if task_complete_result is not None:
                    print(f"[ThinkingModel] 已调用 task_complete，任务结束", flush=True)
                    break
                
                # 检查任务是否完成（如果提供了任务目标）
                if task_goal and self._check_task_completion(task_goal, tool_call_history, messages):
                    print(f"[ThinkingModel] 检测到任务已完成: {task_goal}")
                    # 添加任务完成提示
                    messages.append({
                        "role": "system",
                        "content": f"任务目标 '{task_goal}' 已达成。请总结完成的操作并给出最终回复。"
                    })
                    # 继续执行，让模型生成最终回复
                
                sub_turn += 1
                
            except Exception as e:
                print(f"[ThinkingModel] Error in turn {turn}.{sub_turn}: {e}")
                import traceback
                traceback.print_exc()
                # 返回错误信息（包含空的思考过程列表）
                return f"Error: {e}", tool_call_history, []
        
        # 返回最终内容和工具调用历史
        if task_complete_result is not None:
            final_content = task_complete_result
        elif messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                final_content = last_message.get("content", "") or ""
            else:
                final_content = getattr(last_message, 'content', None) or ""
        else:
            final_content = content or ""
        
        # 达到最大轮数且未调用 task_complete：强制以“任务完成”形式收尾，避免返回截断或无效中间状态
        if sub_turn > max_iterations and task_complete_result is None:
            progress_parts = []
            for tc in tool_call_history[-5:]:
                name = tc.get("tool_name", "")
                res = (tc.get("result") or "")[:80]
                progress_parts.append(f"{name}: {res}")
            progress_str = "；".join(progress_parts) if progress_parts else "无"
            final_content = f"由于步骤过多已强制终止，当前进度为：{progress_str}。请下次通过 task_complete 显式提交结果。"
            print(f"[ThinkingModel] 已达最大迭代 {max_iterations}，强制 task_complete 式收尾", flush=True)
        
        # 【修改】返回思考过程列表，让主模型能够基于这些思考过程生成符合角色性格的回复
        # reasoning_contents 包含工具大模型在执行过程中的所有思考，主模型可以基于这些思考生成回复
        # 例如：如果 reasoning_content 提到"正在打开某个软件"，主模型可以生成"让我看看怎么打开"之类的回复
        return final_content, tool_call_history, reasoning_contents
    
    def _check_task_completion(self, task_goal, tool_call_history, messages):
        """
        检查任务是否完成
        
        Args:
            task_goal: 任务目标描述
            tool_call_history: 工具调用历史
            messages: 消息列表（包含工具执行结果）
            
        Returns:
            bool: 任务是否完成
        """
        # 简单的完成检测：检查最近的工具调用结果中是否包含完成相关的关键词
        completion_keywords = [
            "完成", "成功", "done", "success", "已完成",
            "已发送", "已执行", "已打开", "已播放", "finished",
            "completed", "ok", "executed"
        ]
        
        # 检查最近的工具调用结果
        if tool_call_history:
            recent_results = " ".join([str(tc.get("result", "")) for tc in tool_call_history[-3:]])
            recent_lower = recent_results.lower()
            
            # 检查是否包含完成关键词
            for keyword in completion_keywords:
                if keyword in recent_lower:
                    return True
        
        # 检查消息中是否有明确的完成指示
        for msg in messages[-5:]:
            if isinstance(msg, dict) and msg.get("role") == "tool":
                content = str(msg.get("content", "")).lower()
                if any(keyword in content for keyword in completion_keywords):
                    return True
        
        return False