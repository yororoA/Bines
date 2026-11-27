"""Read-time views of past dialogue and generated prose. Stored sources stay verbatim."""
from __future__ import annotations

import re
from itertools import groupby


def relevant_scene_state(state, query=""):
    """Present recorded scene details when asked, keeping stored state intact."""
    from memory.lexical_index import query_terms

    if not query or re.search(r"(?:你|当前|现在|完整|全部)的?(?:动态)?状态|状态[栏页]|人物面板", query):
        return state
    terms = {word for word in query_terms(query[:512]) if len(word) > 1}

    def relevant(value, topic):
        return bool(re.search(topic, query, re.I) or any(word in str(value).lower() for word in terms))

    view = dict(state)
    npc = dict(view.get("npc_state") or {})
    for fields, key, topic in (
        (view, "current_location", r"在哪|哪[儿里]|什么地方|位置|地点|where"),
        (view, "inventory", r"物品|道具|背包|库存|持有|带了什么|有什么东西|inventory|carrying"),
        (npc, "attire", r"穿|打扮|衣服|衣着|装扮|服装|衣裳|wearing|outfit"),
    ):
        if key in fields and not relevant(fields[key], topic):
            fields.pop(key)
    if not relevant(npc.get("current_activity", ""), r"[干做忙]什么|在[干做忙][嘛啥]|在忙|活动|近况|最近怎么样"):
        npc.pop("current_activity", None)
    if not relevant(view.get("important_thing", ""), r"重要的?事|近期背景|近况|最近怎么样"):
        view.pop("important_thing", None)
    if npc:
        view["npc_state"] = npc
    else:
        view.pop("npc_state", None)
    return view


_SENTENCE_BREAK = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+|[\r\n]+")
# A diary's speculation about someone else is weak evidence even when it happens
# to match the topic of the new question. Original diary pages remain readable.
_DIARY_INFERENCE = re.compile(r"我(?:猜|估计|想必|琢磨着|寻思着)(?:测)?[，, ]*(?:他|她|对方|主人|管理员)")


def wants_narrative_original(query, kind):
    query = quote_request_text(query)
    if kind not in {"diary", "episode"}:
        return False
    names = ("日记",) if kind == "diary" else ("摘要", "小结")
    return (any(name in query for name in names)
            and any(word in query for word in (
                "原文", "逐字", "原句", "全文", "原样", "怎么写", "写了什么", "读给", "念给", "读一下", "念一下",
            )))


def narrative_original_spans(item, _query, limit):
    """Start at the beginning; the source reader can continue from this offset."""
    content = str(item.get("content") or "")
    return [content[:limit]] if limit > 0 and content.strip() else []


_TURN_ACTIONS = {"dialogue_turn", "autonomous_turn"}
_MEMORY_READ_ACTIONS = {"search_memory", "read_memory_source"}


def wants_turn_record(query):
    return bool(re.search(
        r"autonomous_turn|dialogue_turn|(?:行动|自主|轮次|运行|操作|历史)(?:记录|日志)|"
        r"(?:自主|行动).{0,12}(?:理由|原因)|"
        r"(?:你|Bines)(?:当时|上次|那(?:次|天|时|会儿)).{0,8}(?:为什么|为何|怎么想|想了什么)|"
        r"(?:当时|上次|那次)(?:你|Bines).{0,8}(?:为什么|为何|怎么想|想了什么)|"
        r"(?<![他她们])(?:当时|上次|那次)(?:为什么|为何)(?:没[有]?|会|要|决定|选择)?"
        r"(?:自主|主动|联系|发消息|说|回|做|跳过)", quote_request_text(query), re.I))


def hide_turn_prose(item, query=""):
    action = (item.get("meta") or {}).get("action") or item.get("action")
    if action in _MEMORY_READ_ACTIONS:
        return not wants_turn_record(query)
    return action in _TURN_ACTIONS and not (wants_turn_record(query) or wants_narrative_original(query, "episode"))


def turn_record_label(item):
    meta = item.get("meta") or {}
    return " · ".join(str(meta.get(key) or item[key]) for key in ("action", "status") if meta.get(key) or item.get(key))


def is_firsthand_experience(row):
    action = row.get("action")
    return (row.get("source_type") == "experience" and bool(action) and
            action not in _TURN_ACTIONS and action not in _MEMORY_READ_ACTIONS)


def supported_evidence(item, query=""):
    """A generated summary or a memory lookup is not a firsthand observation."""
    include_turns = wants_turn_record(query)
    return [row for row in item.get("evidence") or [] if isinstance(row, dict) and row.get("content") and (
        row.get("source_type") == "message" or is_firsthand_experience(row) or
        (include_turns and row.get("source_type") == "experience" and row.get("action") in _TURN_ACTIONS)
    )]


def diary_witnesses(item):
    """Use scoped firsthand sources already authorized and ranked by retrieval."""
    if item.get("kind") != "diary":
        return []
    return [row for row in item.get("evidence") or [] if isinstance(row, dict) and row.get("content")
            and (is_firsthand_experience(row) or
                 (row.get("source_type") == "message" and row.get("role") in {"user", "observed"}))]


def narrative_spans(item, query="", limit=220):
    """Select at most two source substrings, in source order, for automatic recall."""
    from memory.evidence import _short_evidence, _tokens

    limit = min(220, limit)
    content = str(item.get("content") or "")
    if not content.strip() or limit <= 0:
        return []
    diary = item.get("kind") == "diary"
    if diary and diary_witnesses(item):
        return []
    matches = [span for span in item.get("matched_spans") or [] if isinstance(span, str) and span and span in content]
    # Decide whether a statement is evidence using its full sentence. A lexical
    # hit can start after “我猜他” and make a guess look like a witnessed fact.
    match_ranges = [match.span() for span in dict.fromkeys(matches)
                    for match in re.finditer(re.escape(span), content)]
    pieces = []
    cursor = 0
    for sentence in _SENTENCE_BREAK.split(content):
        if not sentence.strip():
            continue
        start = content.index(sentence, cursor)
        cursor = start + len(sentence)
        if diary and _DIARY_INFERENCE.search(sentence):
            continue
        if not match_ranges or any(start < right and cursor > left for left, right in match_ranges):
            pieces.append(sentence.strip())
    terms = {word for word in _tokens(query) if len(word) > 1}
    ranked = []
    for order, sentence in enumerate(dict.fromkeys(pieces)):
        if not sentence:
            continue
        score = len(terms.intersection(_tokens(sentence))) if terms else 0
        # Source-less diaries are retrospective accounts. Require a topic match
        # or an explicit diary question before including even a short passage.
        if diary and not score and not matches and "日记" not in query:
            continue
        ranked.append((-score, order, sentence))
    selected = []
    for _, order, sentence in sorted(ranked)[:2]:
        if limit <= 0:
            break
        span = _short_evidence(sentence, query, limit)
        selected.append((order, span))
        limit -= len(span)
    return [span for _, span in sorted(selected)]


_DECLINES_QUOTE = re.compile(
    r"(?:不用|不必|无需|不需要|不要|别|用不着|不想|不是(?:要|想|让你|问))"
    r"(?:(?:再|去|给我|跟我|把|看|念|读|说|贴|发|翻|找|讲|听|引用|复述|重复|照搬|告诉我|"
    r"那天的?|之前的?|当时的?|过去的?|以前的?|你的?|我的?|这段|一遍|一下|完整版?)\s*)*"
    r"(?:原话|原文|原句|逐字|一字不差|原样|聊天记录|日记|摘要|小结|"
    r"(?:行动|自主|轮次|运行|操作|历史)(?:记录|日志)|怎么(?:说|回|答)|说了什么)")


def quote_request_text(query):
    """Consider affirmative clauses when deciding whether to replay original wording."""
    clauses = re.split(r"[，,。；;！？!?\n]|但是|不过|而是|(?=我(?:现在)?(?:想|要)|只(?:想|要))", query)
    return "，".join(clause for clause in clauses if clause and not _DECLINES_QUOTE.search(clause))


def wants_verbatim(context):
    query = quote_request_text(str(context.get("current_query", context.get("recall_query")) or ""))
    if any(mark in query for mark in ("原话", "逐字", "原文", "原句", "哪句话", "原样", "一字不差", "聊天记录")):
        return True
    # “这条消息怎么回” asks for a new reply. Preserve historical wording when
    # the question actually refers to what someone said or wrote before.
    return bool(re.search(
        r"说了什[么麼]|说的什[么麼]|怎么(?:说|回[复覆]?|答[复覆]?)的|"
        r"(?:刚才|刚刚|之前|先前|那时|那天|当时|上次|昨天|前面|刚)[^。！？\n]{0,24}怎么(?:说|回|答)", query))


def grouped_message_offset(hit, evidence):
    """Locate a ranked speaker in a graph transcript, including pre-offset server responses."""
    if hit.get("kind") != "message" or evidence.get("source_type") != "message":
        return None
    if "content_offset" in evidence:
        return evidence["content_offset"]
    if "citation_offset" in hit:  # Direct search already points to an individual chunk.
        return None
    source = hit.get("content") or ""
    header = f"[{evidence.get('source_id')}][{evidence.get('role')}][{evidence.get('actor_id')}] "
    if source.startswith("[") and (start := source.find(header)) >= 0:
        offset = source.find(evidence.get("content") or "", start + len(header))
        if offset >= 0:
            return offset
    return None


_HISTORY_ACKS = {
    "这话我记下了": "表示记下了", "这句话我记下了": "表示记下了",
    "这话我心里有数": "表示已了解", "这句话我心里有数": "表示已了解",
    "你说得对": "表示赞同", "你说的对": "表示赞同",
    "这句话接得漂亮": "称赞了对方的接话",
    "感谢你的反馈": "致谢", "感谢您的反馈": "致谢", "谢谢你的分享": "致谢", "感谢你的分享": "致谢",
    "希望对你有所帮助": "祝愿能帮到对方", "希望以上内容对你有所帮助": "祝愿能帮到对方",
    "如果还有其他问题欢迎随时提问": "邀请继续提问", "如果还有其他问题欢迎随时问我": "邀请继续提问",
}

_HISTORY_LITERAL = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"|<[^>\n]*>')


def history_wording(text, *, verbatim):
    """Render a historical reply as evidence, retaining the precise spans still visible."""
    if verbatim or not text:
        return text, [text]
    masked = _HISTORY_LITERAL.sub(lambda match: "x" * len(match[0]), text)
    if any(char in masked for char in "`“”‘’「」『』\"<>"):
        return text, [text]  # Truncated quotation or code. Keep its exact wording.
    sentences = (text[m.start():m.end()] for m in re.finditer(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+", masked))
    rendered, spans = [], []
    for _, repeated in groupby(sentences, key=str.strip):
        copies = list(repeated)
        first = copies[0]
        note = _HISTORY_ACKS.get(first.strip().rstrip("。！!").replace("，", "").replace(",", ""))
        if note:
            rendered.append(f"〔{note}〕")
        else:
            rendered.append(first)
            spans.append(first)
        if len(copies) > 1:
            rendered.append(f"〔同句共{len(copies)}次〕")
    view = "".join(rendered)
    return view, [text] if view == text else spans
