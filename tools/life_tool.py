"""Bounded lifecycle self-regulation through the central Memory Server."""
from .dependencies import deps


def regulate_life(mode: str, reason: str, duration_minutes: int = 60):
    """Temporarily rest, stay awake, or return to automatic lifecycle control."""
    from tools.context import CURRENT_CONTEXT

    context = CURRENT_CONTEXT.get()
    identity = context.identity
    if not identity.privileged:
        raise PermissionError("生命周期自我调节仅允许 Bines 本机或主人私聊")
    if not context.turn_id:
        raise PermissionError("生命周期调节必须来自可信对话或自主轮次")
    if identity.local:
        client = deps.memory_client
        source_ref = f"local-life-regulation:{context.turn_id}"
    else:
        from chatBot.memory_hub import GatewayMemoryClient
        client = GatewayMemoryClient()
        # The gateway replaces this with its own event-bound source.
        source_ref = f"qq-life-regulation:{context.turn_id}"
    if client is None:
        raise RuntimeError("记忆服务未就绪")
    result = client.regulate_life(
        "owner", mode, duration_minutes, reason, source_ref
    )
    life = result.get("life") or {}
    regulation = life.get("regulation") or {}
    return {
        "status": result.get("status"),
        "phase": life.get("phase"),
        "phase_label": life.get("phase_label"),
        "energy": life.get("energy"),
        "sleep_pressure": life.get("sleep_pressure"),
        "regulation": {
            key: regulation.get(key)
            for key in ("mode", "reason", "until")
            if regulation.get(key) not in (None, "")
        },
    }
