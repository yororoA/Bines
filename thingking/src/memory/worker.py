"""Consolidation worker (runs in Thinking): claims jobs from the Memory Server and does the LLM work."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import MemoryClient
from .models import OWNER_SCOPE, ROLE_ASSISTANT, ROLE_OBSERVED

logger = logging.getLogger("bines.memory.worker")

CompleteText = Callable[[List[Dict[str, str]]], str]

_SUMMARY_SYSTEM = (
    "你负责把一段聊天记录压缩成长期记忆。只依据记录内容，不要编造。输出严格 JSON，不要输出其他文字。"
)

_SUMMARY_INSTRUCTIONS = """请阅读下面的聊天记录，输出 JSON：
{{
  "summary": "第三人称中文叙述的剧情摘要，保留情感变化、关键事件、约定、冲突与和解；闲聊或测试用一两句概括",
  "tags": ["英文标签，如 Daily, Work, Game, Emotion, Hobby, User_Profile，最多 5 个"],
  "importance": 1到10的整数，表示这段经历的重要程度,
  "facts": [
    {{"subject": "参与者编号（必须来自下方参与者列表）", "content": "关于该参与者的长期稳定事实或偏好", "importance": 1到10}}
  ]
}}
facts 只写长期稳定的信息（身份、偏好、习惯、重要约定、生日等），临时话题不要写；没有就输出空数组。
{scene}
参与者：
{participants}

聊天记录：
{transcript}"""

_DIARY_INSTRUCTIONS = """日期：{day}

【当日经历摘要】
{episodes}

【当前状态（写日记时可参考）】
{state}

请以第一人称写一段当天的日记，保留关键事件与情感变化，语气自然像在写日记。不要逐条列举，输出一整段连贯的日记正文。"""


def _format_ts(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M") if ts else ""


def _speaker(message: Dict[str, Any]) -> str:
    if message["role"] == ROLE_ASSISTANT:
        return "助手(你)"
    return message.get("actor_name") or message.get("actor_id") or "用户"


def build_summary_prompt(materials: Dict[str, Any]) -> List[Dict[str, str]]:
    messages = materials["messages"]
    participants: Dict[str, str] = {}
    for message in messages:
        if message["role"] != ROLE_ASSISTANT:
            participants.setdefault(message["actor_id"], _speaker(message))
    transcript = "\n".join(
        f"[{_format_ts(m['ts'])}] {_speaker(m)}{'(旁听)' if m['role'] == ROLE_OBSERVED else ''}: {m['content']}"
        for m in messages
    )
    if materials["scope"] == OWNER_SCOPE:
        scene = "场景：助手与其管理员（用户）的私人对话。关于管理员的事实 subject 填 owner。"
    elif str(materials["scope"]).startswith("qq_group:") or ":group:" in str(materials["scope"]):
        scene = "场景：QQ 群聊，可能有多位群成员，摘要中注明是谁说的。"
    else:
        scene = "场景：QQ 私聊。"
    participant_lines = "\n".join(f"- {actor_id}: {name}" for actor_id, name in participants.items()) or "- 无"
    return [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": _SUMMARY_INSTRUCTIONS.format(
            scene=scene, participants=participant_lines, transcript=transcript)},
    ]


def parse_summary(raw: str) -> Tuple[str, List[str], List[Dict[str, Any]], float]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError(f"summary output is not JSON: {raw[:200]!r}")
    data = json.loads(match.group(0))
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary output has empty summary")
    tags = [str(t).strip() for t in data.get("tags") or [] if str(t).strip()][:5]
    facts = [
        {"subject": str(f.get("subject") or ""), "content": str(f.get("content") or "").strip(),
         "importance": f.get("importance", 6)}
        for f in data.get("facts") or [] if isinstance(f, dict) and str(f.get("content") or "").strip()
    ]
    try:
        importance = float(data.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5.0
    return summary, tags, facts, importance


def build_diary_prompt(materials: Dict[str, Any], diary_system_prompt: str) -> List[Dict[str, str]]:
    state = json.dumps(materials.get("state") or {}, ensure_ascii=False, indent=1)
    return [
        {"role": "system", "content": diary_system_prompt},
        {"role": "user", "content": _DIARY_INSTRUCTIONS.format(
            day=materials["day_key"], episodes="\n\n".join(materials["episodes"]), state=state[:3000])},
    ]


class MemoryWorker:
    def __init__(
        self,
        client: MemoryClient,
        complete_text: CompleteText,
        diary_system_prompt: Callable[[], str],
        on_owner_summary: Optional[Callable[[str], None]] = None,
        idle_seconds: float = 30.0,
    ):
        self.client = client
        self.complete_text = complete_text
        self.diary_system_prompt = diary_system_prompt
        self.on_owner_summary = on_owner_summary
        self.idle_seconds = idle_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="MemoryWorker", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("memory worker iteration failed")
                worked = False
            self._stop.wait(1.0 if worked else self.idle_seconds)

    def run_once(self) -> bool:
        claimed = self.client.claim_job()
        if not claimed:
            return False
        job, materials = claimed["job"], claimed["materials"]
        try:
            if job["kind"] == "summarize":
                self._summarize(job, materials)
            elif job["kind"] == "diary":
                self._diary(job, materials)
            else:
                raise ValueError(f"unknown job kind {job['kind']}")
        except Exception as exc:
            logger.warning("memory job %s (%s) failed: %s", job["id"], job["kind"], exc)
            self.client.fail_job(job["id"], f"{type(exc).__name__}: {exc}")
        return True

    def _summarize(self, job: Dict[str, Any], materials: Dict[str, Any]) -> None:
        raw = self.complete_text(build_summary_prompt(materials))
        summary, tags, facts, importance = parse_summary(raw)
        result = self.client.complete_summary(job["id"], summary, tags, facts, importance)
        logger.info("memory job %s summarized %d messages -> %s", job["id"], len(materials["messages"]), result.get("episode_id"))
        if result.get("status") == "done" and materials["scope"] == OWNER_SCOPE and self.on_owner_summary:
            try:
                self.on_owner_summary(summary)
            except Exception:
                logger.exception("owner state update after summary failed")

    def _diary(self, job: Dict[str, Any], materials: Dict[str, Any]) -> None:
        text = self.complete_text(build_diary_prompt(materials, self.diary_system_prompt())).strip()
        if not text:
            raise ValueError("diary model returned empty text")
        self.client.complete_diary(job["id"], text)
        logger.info("memory job %s wrote diary for %s", job["id"], materials["day_key"])


def wait_for_server(client: MemoryClient, timeout_seconds: float = 120.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if client.ping():
            return True
        time.sleep(2)
    return False
