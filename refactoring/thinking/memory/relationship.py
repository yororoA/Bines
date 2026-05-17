from typing import Any, Dict, Tuple

SCORE_MIN = -200
SCORE_MAX = 200

RELATIONSHIP_STATES = [
    {"name": "敌对", "center": -160},
    {"name": "冷淡", "center": -80},
    {"name": "普通", "center": 0},
    {"name": "亲近", "center": 80},
    {"name": "非常亲密", "center": 160},
]

DEFAULT_SOFTNESS = 80.0


def _triangular_membership(score: float, center: float, softness: float) -> float:
    dist = abs(score - center)
    if dist >= softness:
        return 0.0
    return 1.0 - dist / softness


def compute_relationship_weights(
    score: float,
    softness: float = DEFAULT_SOFTNESS,
) -> Dict[str, float]:
    s = max(SCORE_MIN, min(SCORE_MAX, score))
    raw_weights: Dict[str, float] = {}
    for state in RELATIONSHIP_STATES:
        w = _triangular_membership(s, state["center"], softness)
        if w > 0:
            raw_weights[state["name"]] = w
    if not raw_weights:
        closest = min(RELATIONSHIP_STATES, key=lambda st: abs(s - st["center"]))
        return {closest["name"]: 1.0}
    total = sum(raw_weights.values())
    if total <= 0:
        closest = min(RELATIONSHIP_STATES, key=lambda st: abs(s - st["center"]))
        return {closest["name"]: 1.0}
    return {k: v / total for k, v in raw_weights.items()}


def collapse_relationship_level(weights: Dict[str, float]) -> str:
    if not weights:
        return "普通"
    import random
    r = random.random()
    cumulative = 0.0
    for name, w in sorted(weights.items(), key=lambda kv: kv[0]):
        cumulative += w
        if r <= cumulative:
            return name
    return max(weights.items(), key=lambda kv: kv[1])[0]


def apply_relationship_delta(
    current_score: int,
    delta: int,
    softness: float = DEFAULT_SOFTNESS,
) -> Tuple[int, str, Dict[str, float]]:
    new_score = current_score + delta
    new_score = max(SCORE_MIN, min(SCORE_MAX, new_score))
    weights = compute_relationship_weights(new_score, softness=softness)
    level = collapse_relationship_level(weights)
    return new_score, level, weights


def compute_state_from_score(
    score: int,
    softness: float = DEFAULT_SOFTNESS,
) -> Dict[str, Any]:
    clamped_score = max(SCORE_MIN, min(SCORE_MAX, int(score)))
    weights = compute_relationship_weights(clamped_score, softness=softness)
    level = collapse_relationship_level(weights)
    return {
        "relationship_score": clamped_score,
        "relationship_level": level,
        "relationship_distribution": weights,
    }
