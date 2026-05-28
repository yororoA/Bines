from ..status import GraphStatus, _RESET


def StatusTrimNode(state: GraphStatus) -> dict:
    return {
        "tasks_done": _RESET,
        "thoughts": _RESET,
        "iteration_count": 0,
        "persona_snapshot": {},
        "rag_recall": {},
        "soul_prompt": "",
        "already_said": _RESET,
    }