from typing import Any, Optional, Tuple
from collections import deque
import logging
import threading


class DialogueQueue:
    """One FIFO drain, even with a multi-worker executor; human input is never dropped."""

    def __init__(self, executor, *, on_start=None, on_interrupt=None):
        self.executor = executor
        self.on_start = on_start or (lambda: None)
        self.on_interrupt = on_interrupt or (lambda: None)
        self._lock = threading.Lock()
        self._pending = deque()
        self._running = False
        self._active = False

    @property
    def busy(self):
        with self._lock:
            return self._running

    def submit(self, callback, *args, interrupt=False, background=False, **kwargs):
        with self._lock:
            if background and self._running:
                return False  # Only dispensable automatic observations may be skipped.
            self._pending.append((callback, args, kwargs))
            if self._running:
                if interrupt and self._active:
                    self.on_interrupt()
                return True
            self._running = True
            try:
                self.executor.submit(self._drain)
            except BaseException:
                self._running = False
                self._pending.pop()
                raise
        return True

    def _drain(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._active = self._running = False
                    return
                callback, args, kwargs = self._pending.popleft()
                self._active = True
                # Reset cancellation under the same lock as admission, so an
                # incoming message cannot be lost between reset and turn start.
                self.on_start()
            try:
                callback(*args, **kwargs)
            except Exception:
                logging.getLogger("bines.thinking.queue").exception("桌面对话任务失败，继续处理已接收的输入")
            finally:
                with self._lock:
                    self._active = False


def submit_pending_interrupt(
    *,
    pending: Optional[Tuple[Any, ...]],
    executor,
    processing_lock,
    processing_state,
    process_message,
) -> Optional[str]:
    """提交被打断后暂存的下一条输入，返回用于日志的输入预览。"""
    if not pending or executor is None:
        return None

    if len(pending) == 4:
        new_user_input, new_img_descr, new_source, new_extra = pending
    else:
        new_user_input, new_img_descr, new_source = pending
        new_extra = None

    def process_pending():
        with processing_lock:
            processing_state["is_processing"] = True
        try:
            process_message(new_user_input, new_img_descr, source=new_source, extra_data=new_extra)
        finally:
            with processing_lock:
                processing_state["is_processing"] = False

    # Legacy callers share a serial drain instead of silently dropping a pending
    # input when another worker has already acquired the busy flag.
    with processing_lock:
        dispatcher = processing_state.get("_dialogue_queue")
        if dispatcher is None:
            dispatcher = DialogueQueue(executor)
            processing_state["_dialogue_queue"] = dispatcher
    dispatcher.submit(process_pending)
    return str(new_user_input)
