from typing import Any, Optional, Tuple


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

    with processing_lock:
        processing_state["is_processing"] = False

    if len(pending) == 4:
        new_user_input, new_img_descr, new_source, new_extra = pending
    else:
        new_user_input, new_img_descr, new_source = pending
        new_extra = None

    def process_pending():
        with processing_lock:
            if processing_state["is_processing"]:
                return
            processing_state["is_processing"] = True
        try:
            process_message(new_user_input, new_img_descr, source=new_source, extra_data=new_extra)
        finally:
            with processing_lock:
                processing_state["is_processing"] = False

    executor.submit(process_pending)
    return str(new_user_input)
