import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from thinking_settings import thinking_settings


class ThinkingModelHelper:
    def __init__(self, api_key=None, base_url=None, model=None, use_thinking=False):
        resolved_api_key = api_key or thinking_settings.DEEPSEEK_THINKING_API_KEY
        self.api_key = resolved_api_key
        resolved_base_url = base_url or thinking_settings.DEEPSEEK_API_URL
        self.base_url = resolved_base_url
        self.model = model or thinking_settings.DEEPSEEK_THINKING_MODEL
        self.use_thinking = use_thinking
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def run_tool_calling_turn(
        self,
        messages,
        tools,
        tool_call_map,
        turn=1,
        task_goal=None,
        progress_callback=None,
        max_iterations=20,
        interrupt_check=None,
    ):
        sub_turn = 1
        tool_call_history = []
        reasoning_contents = []
        task_complete_result = None

        while sub_turn <= max_iterations:
            if interrupt_check and interrupt_check():
                print(f"[ThinkingModel] Tool execution interrupted during turn {sub_turn}", flush=True)
                return "（任务已被用户打断）", tool_call_history, reasoning_contents

            try:
                kwargs = dict(model=self.model, messages=messages, tools=tools)
                if self.use_thinking:
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                response = self.client.chat.completions.create(**kwargs)

                message = response.choices[0].message
                message_dict = {"role": "assistant", "content": message.content}

                reasoning_content = getattr(message, "reasoning_content", None)
                if reasoning_content:
                    message_dict["reasoning_content"] = reasoning_content
                    reasoning_contents.append(reasoning_content)

                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    tool_calls_dict = []
                    for tc in tool_calls:
                        tc_dict = {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        tool_calls_dict.append(tc_dict)
                    message_dict["tool_calls"] = tool_calls_dict

                messages.append(message_dict)

                content = message.content
                print(f"[ThinkingModel] Turn {turn}.{sub_turn}")
                print(f"  content: {content[:100] if content else None}...")
                print(f"  tool_calls: {len(tool_calls) if tool_calls else 0}")

                if not tool_calls:
                    break

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

                for (_, _, func_name, _, args) in tc_list:
                    if func_name == "task_complete":
                        task_complete_result = args.get("result", "")
                        break

                def _run_one_tool(item):
                    idx, call_id, func_name, args_str, args = item
                    tool_function = tool_call_map.get(func_name)
                    if not tool_function:
                        return idx, call_id, func_name, args_str, f"Error: Tool '{func_name}' not found"
                    try:
                        tool_result = tool_function(**args)
                        result = str(tool_result)
                        return idx, call_id, func_name, args_str, result
                    except Exception as e:
                        return idx, call_id, func_name, args_str, f"Error executing '{func_name}': {e}"

                results_by_idx = {}
                with ThreadPoolExecutor(max_workers=min(len(tc_list), 8)) as executor:
                    futures = {executor.submit(_run_one_tool, item): item[0] for item in tc_list}
                    for future in as_completed(futures):
                        idx, call_id, func_name, args_str, result = future.result()
                        results_by_idx[idx] = (call_id, func_name, args_str, result)
                        print(f"[ThinkingModel] Tool done: {func_name}, result len: {len(result)}", flush=True)

                for idx in sorted(results_by_idx.keys()):
                    call_id, func_name, args_str, result = results_by_idx[idx]
                    tool_call_history.append({"tool_name": func_name, "arguments": args_str, "result": result})
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                    if progress_callback:
                        try:
                            progress_callback("tool_complete", result[:100])
                        except Exception:
                            pass

                if task_complete_result is not None:
                    print(f"[ThinkingModel] task_complete called, ending", flush=True)
                    break

                sub_turn += 1

            except Exception as e:
                print(f"[ThinkingModel] Error in turn {turn}.{sub_turn}: {e}")
                import traceback
                traceback.print_exc()
                return f"Error: {e}", tool_call_history, []

        if task_complete_result is not None:
            final_content = task_complete_result
        elif messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                final_content = last_message.get("content", "") or ""
            else:
                final_content = getattr(last_message, "content", None) or ""
        else:
            final_content = content or ""

        if sub_turn > max_iterations and task_complete_result is None:
            progress_parts = []
            for tc in tool_call_history[-5:]:
                name = tc.get("tool_name", "")
                res = (tc.get("result") or "")[:80]
                progress_parts.append(f"{name}: {res}")
            progress_str = "；".join(progress_parts) if progress_parts else "无"
            final_content = f"由于步骤过多已强制终止，当前进度为：{progress_str}。请下次通过 task_complete 显式提交结果。"
            print(f"[ThinkingModel] Max iterations {max_iterations} reached", flush=True)

        return final_content, tool_call_history, reasoning_contents
