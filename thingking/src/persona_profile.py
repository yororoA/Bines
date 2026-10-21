"""Load ``thingking/persona/persona.yaml`` (the only persona source) and render it for prompts and docs."""
from __future__ import annotations

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
        output_rules(p),
        "```",
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
