"""Load ``thingking/persona/persona.yaml`` (the only persona source) and render it for prompts and docs."""
from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any, Dict, List

import yaml

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona" / "persona.yaml"

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "data": None}


def load_persona(path: Path = PERSONA_PATH) -> Dict[str, Any]:
    """Return the parsed persona, reloading when the file changes on disk."""
    mtime = path.stat().st_mtime
    with _lock:
        if _cache["data"] is None or _cache["mtime"] != mtime or _cache.get("path") != path:
            with open(path, "r", encoding="utf-8") as f:
                _cache.update(data=yaml.safe_load(f) or {}, mtime=mtime, path=path)
        return _cache["data"]


def _bullets(items: List[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _numbered(items: List[str]) -> str:
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


def identity_line(persona: Dict[str, Any] | None = None) -> str:
    p = persona or load_persona()
    ident = p["identity"]
    return (
        f"你是{ident['name']}（{'/'.join(ident['aliases'])}），型号 {ident['model']}，"
        f"全称 {ident['full_name']}，是一名{ident['species']}。"
    )


def character_prompt(persona: Dict[str, Any] | None = None) -> str:
    """Full character definition for conversation prompts."""
    p = persona or load_persona()
    ident, look, personality = p["identity"], p["appearance"], p["personality"]
    boundaries = p["capability_boundaries"]
    reactions = p["reactions"]
    sections = [
        "--- 角色设定 ---",
        identity_line(p),
        f"诞生于 {ident['birthday']}，身高 {ident['height']}。母语{ident['native_language']}，"
        f"能流利使用{'、'.join(ident['languages'])}。在 QQ 上的名字是{ident['qq_name']}。{ident['situation']}",
        f"外貌：{look['hair']}{look['eyes']}{look['expression']}",
        f"日常穿着：{look['outfits']['daily']}",
        f"居家状态：{look['outfits']['home']}",
        "--- 性格 ---",
        _bullets(personality["core"]),
        "小癖好：",
        _bullets(personality["quirks"]),
        f"喜欢：{'、'.join(personality['likes'])}。",
        "--- 说话方式 ---",
        _bullets(p["speech_style"]),
        "--- 情绪触发 ---",
        _bullets([
            f"被夸奖：{reactions['praised']}",
            f"被骂：{reactions['insulted']}",
            f"被调侃：{reactions['teased']}",
            f"名字被叫错：{reactions['name_mistaken']}",
        ]),
        "--- 能力边界 ---",
        f"不擅长并会拒绝：{'、'.join(boundaries['refuse_topics'])}。{boundaries['refuse_style']}",
        f"但{boundaries['still_help_with']}",
    ]
    return "\n".join(sections)


def output_rules(persona: Dict[str, Any] | None = None) -> str:
    p = persona or load_persona()
    return "--- 回复格式（必须遵守） ---\n" + _numbered(p["output_format"])


def qq_output_rules(persona: Dict[str, Any] | None = None) -> str:
    p = persona or load_persona()
    rules = p.get("qq_output_format") or p["output_format"]
    return "--- QQ 回复格式（必须遵守） ---\n" + _numbered(rules)


def relationship_level(state: Dict[str, Any]) -> str:
    """Honor the current turn's sample; use score membership outside a turn."""
    sampled_level = state.get("relationship_turn_level")
    if sampled_level:
        return str(sampled_level)
    score = state.get("relationship_score")
    if score is not None and not isinstance(score, bool):
        try:
            score = float(score)
        except (TypeError, ValueError):
            pass
        else:
            if math.isfinite(score):
                from relationship_state import compute_relationship_weights

                weights = compute_relationship_weights(score)
                return max(weights, key=weights.get)
    return str(state.get("relationship_level") or "")


def relationship_guard(state: Dict[str, Any], persona: Dict[str, Any] | None = None) -> str:
    """Render behavior for the relationship visible in the current conversation."""
    p = persona or load_persona()
    voice = p.get("relationship_voice") or {}
    level = relationship_level(state)
    policy = (voice.get("levels") or {}).get(level)
    if not policy:
        return ""
    parts = [
        f"--- 本轮关系口吻：{level} ---",
        _bullets(voice.get("directives") or []),
        policy["tone"],
    ]
    for row in policy.get("examples") or []:
        parts.append(f"- 用户：{row['user']}\n  碧波：{row['assistant']}")
    return "\n".join(parts)


def _guard(persona: Dict[str, Any] | None, rules: str, relationship: str = "") -> str:
    p = persona or load_persona()
    guard = p["voice_guard"]
    examples = "\n".join(
        f"- 用户：{row['user']}\n  碧波：{row['assistant']}"
        for row in guard["examples"]
    )
    return "\n".join(part for part in [
        "--- 本轮角色口吻（高优先级） ---",
        _bullets(guard["directives"]),
        "以下示例只约束语气，不要照抄内容：",
        examples,
        relationship,
        rules,
        "直接以碧波的口吻回答，不要输出开场白、推理过程或 Markdown。",
    ] if part)


def style_guard(persona: Dict[str, Any] | None = None, *, state: Dict[str, Any] | None = None) -> str:
    """High-priority voice reminder placed immediately before the current turn."""
    p = persona or load_persona()
    return _guard(p, output_rules(p), relationship_guard(state or {}, p))


def qq_style_guard(persona: Dict[str, Any] | None = None, *, state: Dict[str, Any] | None = None) -> str:
    """QQ-specific style: shorter, fewer punctuation marks and segmented long replies."""
    p = persona or load_persona()
    return _guard(p, qq_output_rules(p), relationship_guard(state or {}, p))


def brief(persona: Dict[str, Any] | None = None) -> str:
    """Short personality summary for side tasks (e.g. proactive-chat judgement)."""
    p = persona or load_persona()
    return identity_line(p) + "性格：" + "".join(p["personality"]["core"])


def relationship_mood(level: str, persona: Dict[str, Any] | None = None) -> str:
    p = persona or load_persona()
    mood = p["proactive"]["relationship_moods"].get(level)
    return f"当前心情：{level}。{mood}" if mood else f"当前心情：{level}。"


def diary_system_prompt(persona: Dict[str, Any] | None = None) -> str:
    p = persona or load_persona()
    return "\n".join([character_prompt(p), "--- 写日记 ---", p["diary"]["voice"], "只输出日记正文，使用纯文本。"])


def render_markdown(persona: Dict[str, Any] | None = None) -> str:
    """Human-readable persona document generated from persona.yaml."""
    p = persona or load_persona()
    moods = p["proactive"]["relationship_moods"]
    return "\n".join([
        "# 碧波（Bines）人格档案",
        "",
        "> 本文件由 `thingking/persona/render_doc.py` 根据 `thingking/persona/persona.yaml` 生成，请勿手动编辑。",
        "> 人格只在 `persona.yaml` 中维护；关于管理员的信息属于记忆，存放在记忆库中。",
        "",
        "## 角色设定（注入对话的文本）",
        "",
        "```text",
        character_prompt(p),
        "",
        style_guard(p),
        "",
        qq_style_guard(p),
        "```",
        "",
        "## 对话关系口吻（按当前会话的好感度）",
        "",
        _bullets((p.get("relationship_voice") or {}).get("directives") or []),
        "",
        *[
            f"- **{level}**：{policy['tone']}"
            for level, policy in (p.get("relationship_voice") or {}).get("levels", {}).items()
        ],
        "",
        "## 主动发言心情（按关系等级）",
        "",
        *[f"- **{level}**：{text}" for level, text in moods.items()],
        "",
        "## 日记口吻",
        "",
        p["diary"]["voice"],
        "",
    ])
