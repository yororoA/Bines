from ..status import GraphStatus, _RESET


def StatusTrimNode(state: GraphStatus) -> dict:
    return {
        "tasks_done": _RESET,
        "thoughts": _RESET,
        "persona_snapshot": {},
        "rag_recall": {},
        "already_said": _RESET,
    }