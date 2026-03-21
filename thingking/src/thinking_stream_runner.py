import queue
import threading
import time
from typing import Any, Callable, Dict


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

        tool_calls = (holder.get("message") or {}).get("tool_calls")
        if not tool_calls or not list(tool_calls):
            break

        messages.append(holder["message"])
        execute_router_tool_calls(messages, tool_calls, interrupt_requested, source)
        full_raw_response = (full_raw_response or "").strip()

    return {
        "full_raw_response": full_raw_response,
        "exited_due_to_interrupt": exited_due_to_interrupt,
    }
