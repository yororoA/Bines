from typing import Any, Dict, Tuple


# 好感度数值范围
SCORE_MIN = -200
SCORE_MAX = 200

# 模糊状态定义：中心点 + 软半径（Softness）
# 这里可以理解为在好感度轴上的“画笔中心”，单位与 score 相同
RELATIONSHIP_STATES = [
    {"name": "敌对", "center": -160},
    {"name": "冷淡", "center": -80},
    {"name": "普通", "center": 0},
    {"name": "亲近", "center": 80},
    {"name": "非常亲密", "center": 160},
]

# 软半径：越大则状态之间重叠越多，“忽远忽近”的概率就越明显
DEFAULT_SOFTNESS = 80.0


def _triangular_membership(score: float, center: float, softness: float) -> float:
    """
    简单的三角形隶属度函数：
    - 在 center 处为 1
    - 在距离 center 超过 softness 时为 0
    - 中间线性下降
    """
    dist = abs(score - center)
    if dist >= softness:
        return 0.0
    return 1.0 - dist / softness


def compute_relationship_weights(
    score: float,
    softness: float = DEFAULT_SOFTNESS,
) -> Dict[str, float]:
    """
    基于当前 relationship_score 计算每个关系等级的模糊权重（概率分布）。

    返回值示例：{"普通": 0.6, "亲近": 0.35, "冷淡": 0.05}
    """
    # 裁剪 score，防止越界
    s = max(SCORE_MIN, min(SCORE_MAX, score))

    raw_weights: Dict[str, float] = {}
    for state in RELATIONSHIP_STATES:
        w = _triangular_membership(s, state["center"], softness)
        if w > 0:
            raw_weights[state["name"]] = w

    # 如果所有状态都为 0（极端边缘情况），选择最近的中心点给一个权重 1
    if not raw_weights:
        closest = min(
            RELATIONSHIP_STATES,
            key=lambda st: abs(s - st["center"]),
        )
        return {closest["name"]: 1.0}

    total = sum(raw_weights.values())
    if total <= 0:
        # 理论上不应该发生，上面已经处理，兜底
        closest = min(
            RELATIONSHIP_STATES,
            key=lambda st: abs(s - st["center"]),
        )
        return {closest["name"]: 1.0}

    # 归一化为概率分布
    return {k: v / total for k, v in raw_weights.items()}


def collapse_relationship_level(
    weights: Dict[str, float],
) -> str:
    """
    从模糊权重中“坍缩”出一个具体的关系等级。
    默认使用加权随机采样，保证多次交互中会出现“忽远忽近”的效果。
    """
    if not weights:
        return "普通"

    # 按权重随机抽样
    import random

    r = random.random()
    cumulative = 0.0
    for name, w in sorted(weights.items(), key=lambda kv: kv[0]):
        cumulative += w
        if r <= cumulative:
            return name

    # 理论上上面应该已经 return，这里兜底返回权重最大的等级
    return max(weights.items(), key=lambda kv: kv[1])[0]


def apply_relationship_delta(
    current_score: int,
    delta: int,
    softness: float = DEFAULT_SOFTNESS,
) -> Tuple[int, str, Dict[str, float]]:
    """
    应用一次 relationship_delta，返回：
    - 更新后的 score（裁剪到 [SCORE_MIN, SCORE_MAX]）
    - 本次坍缩后的离散关系等级名称
    - 当前的完整权重分布（方便调试或注入到 prompt 中）
    """
    # 建议：大多数时候 delta 在 -1 ~ +1 之间，稍重要事件用 -2 ~ +2，重大事件再更大
    new_score = current_score + delta
    new_score = max(SCORE_MIN, min(SCORE_MAX, new_score))

    weights = compute_relationship_weights(new_score, softness=softness)
    level = collapse_relationship_level(weights)

    return new_score, level, weights


def compute_state_from_score(
    score: int,
    softness: float = DEFAULT_SOFTNESS,
) -> Dict[str, Any]:
    """
    根据单一的 relationship_score 生成与 memory_dynamic.json
    中相同结构的关系状态字段：
    - relationship_score
    - relationship_level
    - relationship_distribution

    这样在初始化或从磁盘加载旧数据时，可以方便地重建
    与当前打分模型一致的状态。
    """
    clamped_score = max(SCORE_MIN, min(SCORE_MAX, int(score)))
    weights = compute_relationship_weights(clamped_score, softness=softness)
    level = collapse_relationship_level(weights)

    return {
        "relationship_score": clamped_score,
        "relationship_level": level,
        "relationship_distribution": weights,
    }


