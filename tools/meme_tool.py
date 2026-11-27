"""Read-only CNMeme Wiki lookup for a named expression in the current turn."""
from functools import lru_cache
import re
from threading import BoundedSemaphore
import time
from urllib.parse import quote

import requests

from common.memory_journal import tool_failure
from tools.context import CURRENT_CONTEXT


_API = "https://cnmeme.wiki/api/v1/public/memes"
_SLOTS = BoundedSemaphore(4)
# A bare private message is not a search request. These public expressions
# can also be recognised without a surrounding question or meme cue.
_PUBLIC_TERMS = frozenset({"退退退", "尊嘟假嘟", "鸡你太美", "你干嘛哎哟", "一眼丁真",
                           "绷不住了", "遥遥领先", "好活当赏", "栓Q", "yyds", "awsl"})


def _safe_term(term):
    return bool(re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9 ·!！?？]{0,28}", term)
                and not re.search(r"\d{5}|[A-Za-z0-9]{20}|密码|验证码|手机号|身份证|门牌号|电话|住址|住在|地址|账号|邮箱", term))


def _mentioned_term(term, text):
    if term not in text:
        return False
    if term in _PUBLIC_TERMS:
        return True
    # Require a cue next to this specific term. A stray “梗” elsewhere in a
    # conversation must not allow arbitrary snippets from that conversation.
    keyword = re.escape(term)
    quoted = rf"[「“『‘\"']{keyword}[」”』’\"']"
    return bool(re.search(
        rf"(?:接(?:一下|个)?梗|查(?:一下|查)?梗|搜(?:一下)?梗|梗名)[\s：:「“『‘\"']*{keyword}"
        rf"|{keyword}[」”』’\"']?\s*(?:这个|那个|是个|是|到底是)?(?:什么梗|啥梗|什么意思|这个梗|的梗)"
        rf"|{quoted}[吗啊呀呢，,。！？!?\s]*[^\n，。！？]{{0,10}}梗",
        text, flags=re.IGNORECASE))


def quoted_meme_terms(events, resolved_quotes):
    """Extract candidate names only from the quote attached to an explicit meme question."""
    from chatBot.messages import segments

    def plain_text(event):
        parts = segments(event.get("message", event.get("raw_message", "")))
        return "".join(str((p.get("data") or {}).get("text", "")) for p in parts if p.get("type") == "text").strip()

    asking_events = {e["event_id"] for e in events
                     if re.search(r"什么梗|啥梗|接(?:一下|个|这个)?梗|查(?:一下|查)?梗", plain_text(e))}
    terms = set()
    for quote_event in resolved_quotes:
        if quote_event.get("source_event_id") not in asking_events:
            continue
        body = plain_text(quote_event)
        marked = re.findall(r"[「“『‘\"']([^「」“”『』‘’\"'\n]{1,29})[」”』’\"']", body)
        candidates = [term for term in marked if body[1:-1] == term or _mentioned_term(term, body)]
        if not marked and 1 < len(body) <= 8 and not re.search(r"\s|我家|我叫|医院|病历|银行卡|我明天", body):
            candidates.append(body)
        sender = quote_event.get("sender") or {}
        for term in candidates:
            if _safe_term(term) and not term.isdigit() and term not in (sender.get("nickname"), sender.get("card")):
                terms.add(term)
    return terms


@lru_cache(maxsize=128)
def _search(term, minute):
    # The public search is cached for 60 seconds by the provider.
    if not _SLOTS.acquire(blocking=False):
        return tool_failure("梗 Wiki 暂忙，这轮先按原话聊。", code="wiki_busy")
    try:
        with requests.get(_API, params={"q": term, "limit": 3},
                          headers={"User-Agent": "CNMemeWikiPlugin/1.0 (Bines)", "Accept": "application/json"},
                          timeout=(3, 4), allow_redirects=False) as response:
            if response.status_code != 200:
                return tool_failure(f"梗 Wiki 暂不可用（HTTP {response.status_code}），本轮不再查询。",
                                    code="wiki_unavailable")
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Invalid items")
    except (requests.RequestException, ValueError):
        return tool_failure("没读到梗 Wiki 的结果，本轮不再查询。", code="wiki_unavailable")
    finally:
        _SLOTS.release()

    items = []
    for row in data["items"][:3]:
        if not isinstance(row, dict):
            continue
        title, slug, summary = (row.get(key) for key in ("title", "slug", "summary"))
        if not all(isinstance(value, str) and value.strip() for value in (title, slug, summary)):
            continue
        aliases = row.get("aliases")
        names = [title, *(aliases if isinstance(aliases, list) else [])]
        if any(term.casefold() == name.casefold() for name in names if isinstance(name, str)):
            items.append({"title": title[:200], "summary": summary[:500],
                          "url": f"{_API}/{quote(slug, safe='')}"})
    return {"status": "ok", "items": items}


def search_meme(term: str) -> dict:
    """按本轮提到的短梗词或别名查询梗 Wiki，用于接梗或解释；仅传梗词。"""
    context = CURRENT_CONTEXT.get()
    term = term.strip() if isinstance(term, str) else ""
    if not _safe_term(term) or not (_mentioned_term(term, context.user_text) or term in context.quoted_meme_terms):
        return tool_failure("只查询本轮点名的梗词。梗名不明确时直接接话或问对方指哪个梗。",
                            code="invalid_meme_term")
    return _search(term, int(time.monotonic() // 60))
