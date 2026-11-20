"""Select whole shared highlights for model input without shortening stored state."""
from heapq import nlargest


def bounded_highlight_state(state, query=""):
    if not isinstance(state, dict) or "memory_highlights" not in state:
        return state
    source = state["memory_highlights"]
    if isinstance(source, str):
        source = [source]
    elif not isinstance(source, (list, tuple)):
        source = []
    items = list(dict.fromkeys(item for item in source if isinstance(item, str) and item))
    priority = []
    if query:
        from memory.lexical_index import query_terms
        terms = {term for term in query_terms(query[:512]) if len(term) > 1}
        scored = []
        for item in reversed(items):
            if len(item) > 1800:
                continue
            # Exact old quotes matter for a correction or removal operation.
            lower = item.lower()
            score = (item in query, sum(term in lower for term in terms))
            if any(score):
                scored.append((score, item))
        priority = [item for _, item in nlargest(3, scored, key=lambda entry: entry[0])]

    matched, recent, remaining = [], [], 1800
    for item in dict.fromkeys([*priority, *reversed(items[-30:])]):
        if len(item) <= remaining:
            (matched if item in priority else recent).append(item)
            remaining -= len(item)
            if len(matched) + len(recent) == 20 or not remaining:
                break
    return {**state, "memory_highlights": [*matched, *reversed(recent)]}
