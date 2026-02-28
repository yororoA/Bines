"""
工具调用通用工具函数
提取重复的工具调用逻辑
"""
import json
import threading
import re
from typing import Dict, List, Callable, Any


# 定义可以在后台执行的工具（这些工具执行后立即返回，不阻塞对话）
# 【修复】control_netease_music 使用全局快捷键，执行速度很快，应该同步执行以确保用户能立即看到结果
ASYNC_TOOLS = {
    "open_application", 
    "automate_action", 
    "automate_sequence",
    "open_netease_music",
    "search_music_in_netease",
    "play_music_in_netease",
    # control_netease_music 已移除，改为同步执行以确保立即反馈
}


def execute_tool_calls(tool_calls_list: List[Dict], tool_call_map: Dict[str, Callable], 
                       messages: List[Dict], async_tools: set = None) -> None:
    """
    执行工具调用列表，支持同步和异步工具
    
    Args:
        tool_calls_list: 工具调用列表，每个元素包含 id, type, function (name, arguments)
        tool_call_map: 工具名称到函数的映射
        messages: 消息列表，用于添加工具结果
        async_tools: 异步工具集合，如果为None则使用默认的ASYNC_TOOLS
    """
    if async_tools is None:
        async_tools = ASYNC_TOOLS
    
    for tc in tool_calls_list:
        func_name = tc["function"]["name"]
        args_str = tc["function"]["arguments"]
        call_id = tc["id"]
        
        print(f"[Tool] Calling: {func_name} args={args_str}", flush=True)
        
        # 判断是否为异步工具
        is_async = func_name in async_tools
        
        if is_async:
            # 异步工具：在后台执行，立即返回确认消息
            # 【修复】通过默认参数绑定循环变量，避免闭包捕获到最后一次迭代的值
            def run_async_tool(_fn=func_name, _args_s=args_str):
                try:
                    args = json.loads(_args_s) if _args_s else {}
                    result = tool_call_map[_fn](**args)
                    print(f"[Tool Async] {_fn} completed: {str(result)[:100]}...", flush=True)
                except Exception as e:
                    print(f"[Tool Async] {_fn} error: {e}", flush=True)
            
            # 启动后台线程
            thread = threading.Thread(target=run_async_tool, daemon=True)
            thread.start()
            
            # 立即返回确认消息，让LLM可以继续对话
            result = f"已开始执行 {func_name}"
        else:
            # 同步工具：正常执行
            try:
                args = json.loads(args_str) if args_str else {}
                result = tool_call_map[func_name](**args)
            except Exception as e:
                result = f"Error: {e}"
        
        # 【新增】对于 get_screen_info，检查是否需要过滤输出
        # 如果用户没有明确要求查看屏幕，则简化输出
        if func_name == "get_screen_info":
            try:
                result = _filter_screen_info_output(result, messages)
            except Exception as e:
                # 如果过滤失败，使用原始结果
                print(f"[Tool Utils] 过滤屏幕信息时出错: {e}", flush=True)
        
        # moments 接口返回 { message, data } 时，对「data 为空」做友好说明，避免模型误判为失败
        if func_name in ("get_moments", "add_moment", "comment_moment", "get_comments", "like_moment", "like_comment", "analyze_moment_images"):
            result = _format_moments_result(result)
        
        # 截断日志显示，但保留完整结果
        print(f"[Tool] Result: {str(result)[:100]}...", flush=True)
        
        # 添加 Tool Message
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": str(result)
        })


def has_async_tools(tool_calls_list: List[Dict], async_tools: set = None) -> bool:
    """
    检查工具调用列表中是否包含异步工具
    
    Args:
        tool_calls_list: 工具调用列表
        async_tools: 异步工具集合，如果为None则使用默认的ASYNC_TOOLS
        
    Returns:
        bool: 是否包含异步工具
    """
    if async_tools is None:
        async_tools = ASYNC_TOOLS
    
    return any(tc["function"]["name"] in async_tools for tc in tool_calls_list)


def _format_moments_result(result: Any) -> str:
    """
    moments 接口返回 { message, data } 时，将 data 为 null 的情况格式化为友好说明，
    避免模型把「无草稿/无数据」误读为「接口失败」。
    """
    if not isinstance(result, dict):
        return str(result)
    msg = result.get("message", "")
    data = result.get("data")
    if msg == "ok" and data is None:
        return "接口返回成功。当前没有相关数据（如暂无已发布动态时 data 为空，属正常情况）。"
    if msg and msg != "ok":
        return json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


def _filter_screen_info_output(screen_info: str, messages: List[Dict]) -> str:
    """
    过滤屏幕分析信息的输出
    如果用户没有明确要求查看屏幕，则返回简化版本
    
    Args:
        screen_info: 屏幕分析结果
        messages: 消息列表，用于检查用户意图
        
    Returns:
        str: 过滤后的屏幕信息
    """
    try:
        # 检查最近的用户消息中是否有明确要求查看屏幕的关键词
        # 【优化】更精确的关键词匹配，避免误判
        explicit_keywords = [
            "看屏幕", "查看屏幕", "看看屏幕", "描述屏幕", "分析屏幕", "显示屏幕", "展示屏幕",
            "屏幕内容", "画面内容", "界面内容", "告诉我屏幕", "屏幕是什么", "屏幕上有什么",
            "look at screen", "see screen", "show screen", "describe screen", "analyze screen",
            "tell me screen", "what's on screen", "what's on the screen", "screen content"
        ]
        
        # 也检查单独的关键词（但需要更严格的上下文）
        simple_keywords = ["看", "查看", "看看", "描述", "分析", "显示", "展示"]
        screen_related_words = ["屏幕", "画面", "界面", "screen", "display"]
        
        user_explicit_request = False
        
        # 检查最近的用户消息
        for msg in reversed(messages[-10:]):  # 检查最近10条消息
            if msg.get("role") == "user":
                content = msg.get("content", "").lower()
                
                # 首先检查明确的组合关键词
                if any(keyword in content for keyword in explicit_keywords):
                    user_explicit_request = True
                    break
                
                # 然后检查简单关键词+屏幕相关词的组合
                has_simple_keyword = any(keyword in content for keyword in simple_keywords)
                has_screen_word = any(word in content for word in screen_related_words)
                if has_simple_keyword and has_screen_word:
                    user_explicit_request = True
                    break
        
        # 如果用户没有明确要求，返回简化版本
        if not user_explicit_request:
            # 提取关键信息（错误、重要状态等），移除详细描述
            # 保留错误信息和关键状态
            if "Error" in screen_info or "error" in screen_info.lower():
                return screen_info  # 错误信息需要完整显示
            
            # 提取关键信息：只保留状态、错误、重要提示等
            # 移除详细的屏幕描述
            lines = screen_info.split('\n')
            filtered_lines = []
            for line in lines:
                # 保留错误、状态、重要提示等
                if any(keyword in line.lower() for keyword in ["error", "状态", "完成", "成功", "失败", 
                                                              "status", "complete", "success", "fail",
                                                              "scaling", "坐标", "coordinate"]):
                    filtered_lines.append(line)
            
            if filtered_lines:
                return "\n".join(filtered_lines)
            else:
                # 如果没有关键信息，返回简短确认
                return "屏幕分析完成（详细信息已用于工具执行）"
        
        # 用户明确要求，返回完整信息
        return screen_info
    except Exception as e:
        # 如果过滤失败，返回原始信息
        print(f"[Tool Utils] 过滤屏幕信息时出错: {e}", flush=True)
        return screen_info


def should_use_thinking_model(user_input: str, current_tools: List[Dict] = None) -> bool:
    """
    判断是否应该使用思考模式大模型
    
    这个函数可以根据用户输入和当前工具调用的复杂度来判断
    目前实现一个简单版本，可以根据需要扩展
    
    Args:
        user_input: 用户输入
        current_tools: 当前已调用的工具列表
        
    Returns:
        bool: 是否应该使用思考模式大模型
    """
    # 简单判断：如果用户输入包含复杂任务关键词，使用思考模式
    complex_keywords = [
        "复杂", "困难", "多个步骤", "需要思考", "仔细分析",
        "complex", "difficult", "multiple steps", "think carefully"
    ]
    
    user_lower = user_input.lower()
    if any(keyword in user_lower for keyword in complex_keywords):
        return True
    
    # 如果已经调用了多个工具，可能需要更深入的思考
    if current_tools and len(current_tools) >= 3:
        return True
    
    return False
