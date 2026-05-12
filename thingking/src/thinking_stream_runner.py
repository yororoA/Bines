import json
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, List

import requests


AI_SDK_GATEWAY_BASE = os.getenv("AI_SDK_GATEWAY_BASE", "http://127.0.0.1:3100").rstrip("/")
TOOL_SELECTION_MODE = os.getenv("MAIN_TOOL_SELECTION_MODE", "filter").strip().lower()


def _normalize_tool_choice(raw_tool_choice: Any, allowed_tools: List[str]) -> List[str]:
    """归一化并过滤 toolChoice，仅保留允许调用的工具名。"""
    if not isinstance(raw_tool_choice, list):
        return []
    allowed = set(allowed_tools or [])
    normalized: List[str] = []
    for item in raw_tool_choice:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        if name in allowed and name not in normalized:
            normalized.append(name)
    return normalized


def _select_tools_after_main_output(messages: list, main_output: str, allowed_tools: List[str]) -> List[str]:
    """
    主模型输出后进行工具选择：
    - filter 模式：由筛选模型输出 toolChoice
    - main 模式：由主模型输出 {text, toolChoice}
    """
    if not (main_output or "").strip():
        return []

    mode = TOOL_SELECTION_MODE if TOOL_SELECTION_MODE in {"filter", "main"} else "filter"
    payload = {
        "mode": mode,
        "main_output": main_output,
        "messages": messages,
        "allowed_tools": allowed_tools,
    }

    try:
        resp = requests.post(
            f"{AI_SDK_GATEWAY_BASE}/api/tool_choice",
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json() if resp.content else {}
            return _normalize_tool_choice(data.get("toolChoice"), allowed_tools)
        print(f"[Thinking] /api/tool_choice 失败: HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[Thinking] /api/tool_choice 调用异常: {e}", flush=True)

    # 兼容老网关（仅 filter_tools）
    if mode == "filter":
        try:
            fallback_payload = {
                "main_output": main_output,
                "messages": messages,
                "allowed_tools": allowed_tools,
            }
            resp = requests.post(
                f"{AI_SDK_GATEWAY_BASE}/api/filter_tools",
                json=fallback_payload,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                return _normalize_tool_choice(data.get("selected_tools"), allowed_tools)
            print(f"[Thinking] /api/filter_tools 回退失败: HTTP {resp.status_code}", flush=True)
        except Exception as e:
            print(f"[Thinking] /api/filter_tools 回退异常: {e}", flush=True)
    return []


def run_main_agent_rounds(
    *,
    messages: list,
    main_agent,
    execute_router_tool_calls: Callable[[list, list, Callable[[], bool], str], None],
    zmq_send: Callable[..., bool],
    is_only_action_or_empty: Callable[[str], bool],
    enable_audio: bool,
    source: str,
    interrupt_requested: Callable[[], bool],
    max_tool_rounds: int = 5,
) -> Dict[str, Any]:
    """运行主模型多轮工具调用与流式输出循环。"""
    full_raw_response = ""
    exited_due_to_interrupt = False
    last_selected_tools: List[str] = []
    same_toolchoice_streak = 0

    round_idx = 0
    while round_idx < max_tool_rounds:
        round_idx += 1
        print(f"[Thinking] 主模型流式请求 (round {round_idx})...", flush=True)
        content_gen = None
        holder = {}

        for attempt in range(3):
            content_gen, holder = main_agent.run_one_turn_streaming(messages, interrupt_check=interrupt_requested)
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
                if interrupt_requested():
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
                if c == " ":
                    if content_buffer.endswith(" "):
                        seg = content_buffer[:-1].strip()
                        if seg and not is_only_action_or_empty(seg):
                            cough_val = "start" if is_first_packet else None
                            zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
                            is_first_packet = False
                        content_buffer = ""
                    else:
                        content_buffer += c
                else:
                    content_buffer += c

        if holder.get("interrupted"):
            exited_due_to_interrupt = True

        if exited_due_to_interrupt:
            if content_buffer.strip():
                seg = content_buffer.strip()
                if not is_only_action_or_empty(seg):
                    cough_val = "start" if is_first_packet else None
                    zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
            break

        if content_buffer.strip():
            seg = content_buffer.strip()
            if not is_only_action_or_empty(seg):
                cough_val = "start" if is_first_packet else None
                zmq_send(seg, lang="zh", cough=cough_val, enable_audio=enable_audio)
        print("", flush=True)

        holder_message = holder.get("message") or {"role": "assistant", "content": full_raw_response or None}
        tool_calls = holder_message.get("tool_calls")
        if not tool_calls or not list(tool_calls):
            # 主模型本轮没有直接返回 tool_calls，走 toolChoice 选择流程
            from tool_schema_collections import ALL_TOOLS_SCHEMA_FOR_AGENT

            allowed_tools = [t["function"]["name"] for t in ALL_TOOLS_SCHEMA_FOR_AGENT]
            selected_tools = _select_tools_after_main_output(messages, full_raw_response, allowed_tools)

            if selected_tools:
                if selected_tools == last_selected_tools:
                    same_toolchoice_streak += 1
                else:
                    last_selected_tools = selected_tools[:]
                    same_toolchoice_streak = 1

                # 防止 toolChoice 自激循环：连续两轮命中完全相同的工具集时，不再继续调工具
                if same_toolchoice_streak >= 2:
                    print(
                        f"[Thinking] 检测到重复 toolChoice 循环，跳过本轮工具调用: {selected_tools}",
                        flush=True,
                    )
                    messages.append({"role": "assistant", "content": full_raw_response or None})
                    break

                print(f"[Thinking] toolChoice 结果: {selected_tools}", flush=True)
                synthetic_tool_call = {
                    "id": f"tool_choice_{round_idx}_{int(time.time() * 1000)}",
                    "type": "function",
                    "function": {
                        "name": "call_tool_agent",
                        "arguments": json.dumps(
                            {
                                "task_description": full_raw_response or "",
                                "context": full_raw_response or "",
                                "toolChoice": selected_tools,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
                synthetic_message = {
                    "role": "assistant",
                    "content": full_raw_response or None,
                    "tool_calls": [synthetic_tool_call],
                }
                messages.append(synthetic_message)
                execute_router_tool_calls(messages, [synthetic_tool_call], interrupt_requested, source)
                full_raw_response = (full_raw_response or "").strip()
                continue

            # 既无 tool_calls 又无 toolChoice，本轮结束
            same_toolchoice_streak = 0
            last_selected_tools = []
            messages.append({"role": "assistant", "content": full_raw_response or None})
            break

        same_toolchoice_streak = 0
        last_selected_tools = []
        messages.append(holder_message)
        execute_router_tool_calls(messages, list(tool_calls), interrupt_requested, source)
        full_raw_response = (full_raw_response or "").strip()

    return {
        "full_raw_response": full_raw_response,
        "exited_due_to_interrupt": exited_due_to_interrupt,
    }
