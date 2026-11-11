"""Distinguish a proven pre-send rejection from an ambiguous native failure."""


def recipient_resolution_failed(action, params, result):
    """NapCat's private-send peer lookup failed before message construction.

    The generic 1200 handler code alone says nothing about side effects. This
    exception is deliberately limited to the exact peer-lookup failure observed
    in NapCat's private send handler; media, forward, timeout and unknown errors
    remain ambiguous. A result carrying any data is not proof of non-delivery.
    """
    if action not in {"send_private_msg", "send_msg"}:
        return False
    if not params.get("user_id") or params.get("group_id") or params.get("message_type") == "group":
        return False
    if not isinstance(result, dict) or result.get("status") != "failed" or result.get("retcode") != 1200:
        return False
    if result.get("data") not in (None, {}) or any(result.get(key) for key in ("message_id", "res_id", "forward_id")):
        return False
    if result.get("success") is True or result.get("delivery") in {"sent", "delivered"}:
        return False
    errors = [result[key] for key in ("message", "msg", "wording") if result.get(key)]
    return bool(errors) and all(error == "无法获取用户信息" for error in errors)


def classify_write_result(action, params, result):
    if not isinstance(result, dict) or result.get("status") != "failed" or result.get("retcode") not in {200, 1200}:
        return result
    if recipient_resolution_failed(action, params, result):
        return {**result, "failure_stage": "recipient_resolution", "delivery": "not_sent"}
    # NapCat's generic handler catches errors even after a write has reached QQ.
    return {**result, "status": "uncertain"}
