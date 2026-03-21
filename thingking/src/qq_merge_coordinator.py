import threading
from typing import Callable, Dict, Tuple, Any


class QQMergeCoordinator:
    """QQ 消息合并协调器：按 (group_id, user_id) 分槽合并并在窗口到期后提交。"""

    def __init__(
        self,
        merge_window_sec: float,
        set_pending_interrupt: Callable[[str, str, str, dict], None],
        is_processing_dialogue: Callable[[], bool],
        cancel_current_b_task: Callable[[], None],
    ) -> None:
        self._merge_window_sec = merge_window_sec
        self._set_pending_interrupt = set_pending_interrupt
        self._is_processing_dialogue = is_processing_dialogue
        self._cancel_current_b_task = cancel_current_b_task
        self._lock = threading.Lock()
        self._slots: Dict[Tuple[Any, Any], dict] = {}

    @staticmethod
    def _source_key(qq_context: dict) -> tuple:
        inner = qq_context.get("qq_context", qq_context) if qq_context else {}
        gid = inner.get("group_id")
        uid = inner.get("user_id")
        return (gid, uid)

    def enqueue(
        self,
        user_input: str,
        img_descr: str,
        source: str,
        qq_context: dict,
        executor,
        processing_lock,
        processing_state,
        process_message,
    ) -> None:
        key = self._source_key(qq_context)

        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = {"texts": [], "timer": None, "context": None}
                self._slots[key] = slot

            slot["texts"].append(user_input)

            if slot["context"] is None:
                slot["context"] = (img_descr, source, qq_context)
            else:
                if qq_context and qq_context.get("qq_context", {}).get("image_urls"):
                    prev_qq = slot["context"][2] or {}
                    prev_inner = prev_qq.get("qq_context", prev_qq)
                    existing_urls = prev_inner.get("image_urls", [])
                    new_urls = qq_context.get("qq_context", qq_context).get("image_urls", [])
                    if new_urls:
                        combined_urls = existing_urls + new_urls
                        if "qq_context" in prev_qq:
                            prev_qq["qq_context"]["image_urls"] = combined_urls
                        else:
                            prev_qq["image_urls"] = combined_urls

            if slot["timer"] is not None:
                slot["timer"].cancel()

            buf_count = len(slot["texts"])
            print(
                f"[QQ Merge] [{key}] 消息入缓冲 ({buf_count} 条待合并)，{self._merge_window_sec}s 后提交: {user_input[:40]}...",
                flush=True,
            )

            slot["timer"] = threading.Timer(
                self._merge_window_sec,
                self._flush,
                args=(key, executor, processing_lock, processing_state, process_message),
            )
            slot["timer"].daemon = True
            slot["timer"].start()

    def _flush(self, key: tuple, executor, processing_lock, processing_state, process_message) -> None:
        with self._lock:
            slot = self._slots.pop(key, None)
            if not slot or not slot["texts"]:
                return

            merged_texts = list(slot["texts"])
            ctx = slot["context"]

        if len(merged_texts) == 1:
            merged_input = merged_texts[0]
        else:
            merged_input = "\n".join(merged_texts)
            print(f"[QQ Merge] [{key}] 已合并 {len(merged_texts)} 条消息: {merged_input[:60]}...", flush=True)

        img_descr = ctx[0] if ctx else ""
        source = ctx[1] if ctx else "QQ"
        qq_context = ctx[2] if ctx else {}

        if self._is_processing_dialogue():
            self._set_pending_interrupt(merged_input, img_descr or "", source, qq_context)
            self._cancel_current_b_task()
            print(f"[QQ Merge] [{key}] 当前正在处理中，合并消息已暂存为 Pending: {merged_input[:40]}...", flush=True)
            return

        def process_qq_merged(u=merged_input, i=img_descr, s=source, e=qq_context):
            audio_flag = (s not in ["QQ", "qq"])
            print(f"[QQ Merge] [{key}] 提交合并后的QQ消息处理: source={s}, enable_audio={audio_flag}", flush=True)
            with processing_lock:
                if processing_state["is_processing"]:
                    self._set_pending_interrupt(u, i, s, e)
                    print(f"[QQ Merge] [{key}] 竞态：转为 Pending", flush=True)
                    return
                processing_state["is_processing"] = True
            try:
                process_message(u, i, source=s, extra_data=e)
            finally:
                with processing_lock:
                    processing_state["is_processing"] = False

        executor.submit(process_qq_merged)
