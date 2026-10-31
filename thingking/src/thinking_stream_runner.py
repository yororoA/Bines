"""Streaming conversation loop with a local SKILL_CALL text protocol."""
import queue
import threading
from contextvars import copy_context
from typing import Any, Callable, Dict

from skill_runner import CALL_END, CALL_START, SkillProtocolError, SkillRunner
from will_runtime import ActivityYield


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
    error = ""
    cleanup_pending = False

    def send_text(text: str, segment_state: dict) -> None:
        for char in text:
            if char == " " and segment_state["buffer"].endswith(" "):
                segment = segment_state["buffer"][:-1].strip()
                if segment and not is_only_action_or_empty(segment):
                    cough = "start" if segment_state["first"] else None
                    zmq_send(segment, lang="zh", cough=cough, enable_audio=enable_audio)
                    segment_state["sent"].append(segment)
                    segment_state["first"] = False
                segment_state["buffer"] = ""
            else:
                segment_state["buffer"] += char

    for round_index in range(1, max_skill_rounds + 1):
        if interrupt_requested():
            exited_due_to_interrupt = True
            break
        print(f"[Thinking] 主模型流式请求 (round {round_index})...", flush=True)
        try:
            content_gen, holder = main_agent.run_one_turn_streaming(messages, interrupt_check=interrupt_requested)
        except Exception as exc:
            error = str(exc)
            break
        if holder.get("error"):
            error = str(holder["error"])
            print(f"[Thinking] [Error] {error}", flush=True)
            if any(code in error for code in ("500", "502", "503")):
                zmq_send("DeepSeek 服务暂时异常，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
            else:
                zmq_send("请求出错，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
            break
        if not content_gen:
            error = "模型流未创建"
            zmq_send("系统错误，无法连接大脑。", lang="zh", cough="end", enable_audio=enable_audio)
            break

        chunk_queue = queue.Queue(maxsize=64)
        stream_stop = threading.Event()

        def produce(gen=content_gen, output=chunk_queue, stopped=stream_stop, result_holder=holder):
            def put(value):
                while not stopped.is_set():
                    try:
                        output.put(value, timeout=0.1)
                        return
                    except queue.Full:
                        pass
            try:
                for item in gen:
                    if stopped.is_set():
                        break
                    put(item)
            except Exception as exc:
                if not stopped.is_set():
                    result_holder["error"] = str(exc)
            finally:
                try:
                    close = getattr(gen, "close", None)
                    if callable(close):
                        close()
                except Exception as exc:
                    if not stopped.is_set():
                        result_holder.setdefault("error", f"关闭模型流失败：{type(exc).__name__}")
                put(None)

        # The request now opens in this thread. Preserve the turn's authority,
        # memory journal and observation de-duplication context.
        producer = threading.Thread(target=copy_context().run, args=(produce,), name="MainAgentStream", daemon=True)
        producer.start()
        full_response = []
        pending_prefix = ""
        directive_mode = False
        normal_mode = False
        directive_text = ""
        segment_state = {"buffer": "", "first": True, "sent": []}
        memory_confirmed = False
        stream_completed = False

        try:
            while True:
                if interrupt_requested() or holder.get("interrupted"):
                    exited_due_to_interrupt = True
                    break
                try:
                    chunk = chunk_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if interrupt_requested() or holder.get("interrupted"):
                    exited_due_to_interrupt = True
                    break
                if chunk is None:
                    stream_completed = True
                    break

                if chunk and not memory_confirmed:
                    from thinking_model_helper import _confirm_memory_request
                    _confirm_memory_request(messages)
                    memory_confirmed = True
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
                        continue
                    else:
                        normal_mode = True
                        send_text(pending_prefix, segment_state)
                        pending_prefix = ""
        finally:
            stream_stop.set()
            if producer.is_alive() and not stream_completed:
                cancel = holder.get("cancel")
                if callable(cancel):
                    try:
                        cancel()
                    except Exception as exc:
                        if not exited_due_to_interrupt:
                            holder.setdefault("error", f"关闭模型流失败：{type(exc).__name__}")
            producer.join(timeout=1)
            # An HTTP connection still opening has no response handle to close.
            # Its cancellation flag closes it on arrival; the SDK timeout bounds
            # that wait. Do not let its late chunks reach another turn.
            cleanup_pending = producer.is_alive()

        exited_due_to_interrupt = bool(exited_due_to_interrupt or holder.get("interrupted") or interrupt_requested())
        if exited_due_to_interrupt or holder.get("error"):
            # Preserve only already emitted sentences. Never flush a buffered
            # partial sentence or execute a directive from an incomplete stream.
            final_text = "  ".join(segment_state["sent"])
            error = "" if exited_due_to_interrupt else str(holder["error"])
            if error:
                zmq_send("请求出错，回答未完成，请稍后再试。", lang="zh", cough="end", enable_audio=enable_audio)
            break

        raw_response = "".join(full_response)
        if not normal_mode and not directive_mode:
            # Short response was a prefix of the marker, but never completed it.
            normal_mode = True
            send_text(pending_prefix, segment_state)

        if directive_mode:
            try:
                if interrupt_requested():
                    exited_due_to_interrupt = True
                    break
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
            except ActivityYield:
                # An activity checkpoint ends this batch without speaking a
                # synthetic tool receipt or starting another model request.
                final_text = ""
                last_skill_result = ""
                break
            except RuntimeError as exc:
                if interrupt_requested():
                    exited_due_to_interrupt = True
                else:
                    error = str(exc)
                break

        if segment_state["buffer"].strip():
            segment = segment_state["buffer"].strip()
            if not is_only_action_or_empty(segment):
                cough = "start" if segment_state["first"] else None
                zmq_send(segment, lang="zh", cough=cough, enable_audio=enable_audio)
        final_text = raw_response.strip()
        if final_text:
            break

    if not final_text and last_skill_result and not exited_due_to_interrupt and not error:
        error = "技能调用达到轮次上限，未生成最终回复；已执行操作保留回执"
    return {
        "full_raw_response": final_text,
        "exited_due_to_interrupt": exited_due_to_interrupt,
        "state_updated": state_updated,
        "error": error,
        "stream_cleanup_pending": cleanup_pending,
    }
