"""Streaming conversation loop with a local SKILL_CALL text protocol."""
import queue
import threading
import time
from typing import Any, Callable, Dict

from skill_runner import CALL_END, CALL_START, SkillProtocolError, SkillRunner


def run_main_agent_rounds(
    *,
    messages: list,
    main_agent,
    skill_runner: SkillRunner,
    zmq_send: Callable[..., bool],
    is_only_action_or_empty: Callable[[str], bool],
    enable_audio: bool,
    source: str,
    interrupt_requested: Callable[[], bool],
    max_skill_rounds: int = 12,
) -> Dict[str, Any]:
    """Stream normal replies; buffer protocol directives so they never reach TTS/UI."""
    final_text = ""
    exited_due_to_interrupt = False
    last_skill_result = ""
    state_updated = False

    def send_text(text: str, segment_state: dict) -> None:
        for char in text:
            if char == " " and segment_state["buffer"].endswith(" "):
                segment = segment_state["buffer"][:-1].strip()
                if segment and not is_only_action_or_empty(segment):
                    cough = "start" if segment_state["first"] else None
                    zmq_send(segment, lang="zh", cough=cough, enable_audio=enable_audio)
                    segment_state["first"] = False
                segment_state["buffer"] = ""
            else:
                segment_state["buffer"] += char

    for round_index in range(1, max_skill_rounds + 1):
        print(f"[Thinking] 主模型流式请求 (round {round_index})...", flush=True)
        content_gen, holder = main_agent.run_one_turn_streaming(messages, interrupt_check=interrupt_requested)
        if holder.get("error"):
            error = str(holder["error"])
            print(f"[Thinking] [Error] {error}", flush=True)
            if any(code in error for code in ("500", "502", "503")):
                zmq_send("DeepSeek 服务暂时异常，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
            else:
                zmq_send("请求出错，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
            break
        if not content_gen:
            zmq_send("系统错误，无法连接大脑。", lang="zh", cough="end", enable_audio=enable_audio)
            break

        chunk_queue = queue.Queue()

        def produce():
            try:
                for item in content_gen:
                    chunk_queue.put(item)
            finally:
                chunk_queue.put(None)

        threading.Thread(target=produce, name="MainAgentStream", daemon=True).start()
        full_response = []
        pending_prefix = ""
        directive_mode = False
        normal_mode = False
        directive_text = ""
        segment_state = {"buffer": "", "first": True}
        stream_done = False

        while not stream_done:
            try:
                chunk = chunk_queue.get(timeout=0.35)
            except queue.Empty:
                if interrupt_requested():
                    exited_due_to_interrupt = True
                    break
                continue
            if chunk is None:
                stream_done = True
                break

            full_response.append(chunk)
            if directive_mode:
                directive_text += chunk
            elif normal_mode:
                send_text(chunk, segment_state)
            else:
                pending_prefix += chunk
                trimmed = pending_prefix.lstrip()
                if trimmed.startswith(CALL_START):
                    directive_mode = True
                    directive_text = trimmed
                elif CALL_START.startswith(trimmed):
                    # Wait until we know whether this is protocol or ordinary prose.
                    continue
                else:
                    normal_mode = True
                    send_text(pending_prefix, segment_state)
                    pending_prefix = ""

        if exited_due_to_interrupt:
            if normal_mode and segment_state["buffer"].strip():
                segment = segment_state["buffer"].strip()
                if not is_only_action_or_empty(segment):
                    cough = "start" if segment_state["first"] else None
                    zmq_send(segment, lang="zh", cough=cough, enable_audio=enable_audio)
            break

        raw_response = "".join(full_response)
        if not normal_mode and not directive_mode:
            # Short response was a prefix of the marker, but never completed it.
            normal_mode = True
            send_text(pending_prefix, segment_state)

        if directive_mode:
            try:
                command = skill_runner.parse_command(directive_text)
                if command is None:
                    raise SkillProtocolError("未识别到技能调用块")
                skill_result = skill_runner.execute(command, source=source)
                if command["action"] == "update_status" and str(skill_result).startswith("动态状态已更新"):
                    state_updated = True
                messages.append({"role": "assistant", "content": directive_text})
                messages.append({
                    "role": "system",
                    "content": f"<skill_result>\n{skill_result}\n</skill_result>\n请根据技能结果继续调用技能或直接回复用户。",
                })
                last_skill_result = skill_result
                final_text = ""
                print(f"[Thinking] Skill 执行: {command['skill']}.{command['action']}", flush=True)
                continue
            except SkillProtocolError as exc:
                skill_result = f"技能协议错误：{exc}。请按协议重试。"
                messages.append({"role": "assistant", "content": raw_response})
                messages.append({"role": "system", "content": f"<skill_result>{skill_result}</skill_result>"})
                last_skill_result = skill_result
                continue

        if segment_state["buffer"].strip():
            segment = segment_state["buffer"].strip()
            if not is_only_action_or_empty(segment):
                cough = "start" if segment_state["first"] else None
                zmq_send(segment, lang="zh", cough=cough, enable_audio=enable_audio)
        final_text = raw_response.strip()
        if final_text:
            break

    if not final_text and last_skill_result and not exited_due_to_interrupt:
        final_text = f"[技能执行结果] {last_skill_result}"
    return {
        "full_raw_response": final_text,
        "exited_due_to_interrupt": exited_due_to_interrupt,
        "state_updated": state_updated,
    }
