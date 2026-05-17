import sys
import time

from memory import DynamicMemory
from tools import set_dynamic_memory
from workflow import app


def main():
    print("=" * 50)
    print("Bines Thinking - Refactored (Terminal Mode)")
    print("=" * 50)

    dynamic_memory = DynamicMemory()
    set_dynamic_memory(dynamic_memory)
    print("[Init] DynamicMemory loaded")

    print("\nType your message and press Enter. (Type 'exit' to quit)")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n[You]: ").strip()
            if not user_input:
                continue
            if user_input.lower() == "exit":
                break

            initial_state = {
                "messages": [],
                "system_prompt": "",
                "user_input": user_input,
                "source": "MANUAL",
                "dynamic_memory_text": "",
                "dynamic_memory_json": "",
                "next_node": "",
                "reasoning": "",
                "next_purpose": "",
                "final_reply": None,
                "tool_result": None,
                "tool_history": [],
                "state_update_result": None,
                "reply_text": None,
                "tool_round_count": 0,
            }

            print("[Thinking...]", end="", flush=True)
            start_time = time.time()

            try:
                result = app.invoke(initial_state)
            except Exception as e:
                print(f"\n[Error] Workflow execution failed: {e}")
                import traceback
                traceback.print_exc()
                continue

            elapsed = time.time() - start_time

            reply = result.get("reply_text") or result.get("final_reply") or "……"
            print(f"\r[Bines] ({elapsed:.1f}s): {reply}")

        except KeyboardInterrupt:
            print("\n[Interrupted]")
            break
        except Exception as e:
            print(f"\n[Error] {e}")
            import traceback
            traceback.print_exc()

    print("\nExiting...")


if __name__ == "__main__":
    main()
