"""Account for complete model requests without network tokenizer downloads."""
from __future__ import annotations

import json
import hashlib
import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4)
def _local_tokenizer(path, modified):
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(path)
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


class ContextTokenCounter:
    """Exact local content tokens, or explicitly labelled UTF-8 byte upper bound.

    Provider chat framing is not public: reserve 16 units per message and count
    tool schemas/call metadata separately. Neither mode claims provider-exact use.
    """
    def __init__(self):
        self.encoder = None
        self.method = "utf8_byte_upper_bound"
        self.tokenizer_error = ""
        path = os.environ.get("BINES_CONTEXT_TOKENIZER_PATH", "").strip()
        if path:
            try:
                file = Path(path).expanduser()
                self.encoder = _local_tokenizer(str(file), file.stat().st_mtime_ns)
                self.method = "local_tokenizer"
            except Exception as exc:
                self.tokenizer_error = type(exc).__name__

    def count(self, text):
        text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
        if self.encoder is not None:
            return len(self.encoder.encode(text, add_special_tokens=False).ids)
        return len(text.encode("utf-8", errors="replace"))

    def messages(self, messages):
        return sum(self.count(m.get("content", "")) + 16 +
                   (self.count({k: v for k, v in m.items() if k not in {"role", "content"}})
                    if any(k not in {"role", "content"} for k in m) else 0) for m in messages)

    def prefix(self, text, limit):
        if self.count(text) <= limit:
            return text
        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self.count(text[:mid]) <= limit:
                low = mid
            else:
                high = mid - 1
        return text[:low]


def context_limit():
    """Soft context-selection target, including legacy explicit 32768 settings."""
    try:
        return max(256, min(262144, int(os.environ.get("BINES_CONTEXT_MAX_TOKENS", "65536"))))
    except ValueError:
        return 65536


def diagnostic(stage, data):
    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    if journal and hasattr(journal, "record_memory_diagnostic"):
        journal.record_memory_diagnostic(stage, data)


class ContextBudgetExceeded(ValueError):
    """Required request material exceeds an explicitly configured model window."""

    code = "input_budget_exceeded"
    scope = "request"

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


def bound_request(messages, *, tools=None, response_format=None, max_output_tokens=0):
    """Manage each request toward a soft target and respect explicit hard windows.

    Historical dialogue may be removed. Tool replies remain attached to their
    calls, with an explicit excerpt marker and durable receipt reference when
    available. Required current input/instructions are never silently cut.
    BINES_CONTEXT_MAX_TOKENS was originally a prompt-selection target, not a
    model limit. In particular, exceeding its UTF-8 byte estimate must not block
    an otherwise valid request. Only BINES_MODEL_CONTEXT_TOKENS opts into a hard
    model-window guard (conservative when a local tokenizer is unavailable).
    """
    counter, configured_limit = ContextTokenCounter(), context_limit()
    window = os.environ.get("BINES_MODEL_CONTEXT_TOKENS", "").strip()
    window = int(window) if window else None
    if window is not None and window <= 0:
        raise ValueError("BINES_MODEL_CONTEXT_TOKENS must be positive")
    output_reserve = max(0, int(max_output_tokens or 0))
    hard_limit = max(0, window - output_reserve - 256) if window else None
    limit = min(configured_limit, hard_limit) if hard_limit is not None else configured_limit
    result = [dict(message) for message in messages]
    schemas = {k: v for k, v in {"tools": tools, "response_format": response_format}.items() if v}
    schema_cost = counter.count(schemas) + 16 if schemas else 0
    cost = lambda: counter.messages(result) + schema_cost
    before, trimmed, removed = cost(), [], 0
    original_contents = [m.get("content") or "" for m in result]
    current_user = next((i for i in range(len(result) - 1, -1, -1)
                         if result[i].get("role") == "user"), None)

    def optional_history(index, message):
        content = str(message.get("content") or "")
        if (index == current_user or message.get("tool_calls") or message.get("role") == "tool"
                or (message.get("role") == "assistant" and content.lstrip().startswith("[[SKILL_CALL]]"))):
            return False
        return message.get("role") in {"user", "assistant"} or (
            message.get("role") == "system" and content.startswith(
                ("<historical_assistant_message>", "<memory_recall>")))

    from tools.context import CURRENT_CONTEXT
    journal = CURRENT_CONTEXT.get().journal
    refs = getattr(journal, "tool_result_refs", {}) if journal else {}
    # First trim verbose tool payloads, preserving their identities and outcomes
    # as excerpts, not manufacturing a replacement success receipt.
    for cap, target in ((min(2048, limit // 8), limit), (256, hard_limit), (64, hard_limit)):
        # A soft target below fixed instructions/schemas is unreachable. Do not
        # reduce useful answers to 64 bytes merely trying to meet that target.
        if target is None:
            continue
        if cost() <= target:
            break
        for i, message in enumerate(result):
            content = original_contents[i]
            is_result = message.get("role") == "tool" or (
                message.get("role") == "system" and isinstance(content, str) and content.startswith("<skill_result>"))
            if not is_result or not isinstance(content, str) or counter.count(content) <= cap:
                continue
            legacy = content.startswith("<skill_result>")
            raw = content.partition(">")[2].split("</skill_result>", 1)[0].strip() if legacy else content
            ref = (refs.get(message.get("tool_call_id")) or
                   refs.get("result:" + hashlib.sha256(raw.strip().encode()).hexdigest(), "")) if isinstance(refs, dict) else ""
            marker = "\n[工具结果节选；" + ("完整操作回执=" + ref if ref else "完整结果未随本次请求发送") + "]"
            outcome = ""
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    fields = {key: payload[key] for key in ("status", "ok", "success", "operation_id", "action", "error")
                              if key in payload and isinstance(payload[key], (str, bool, int, float, type(None)))}
                    if fields:
                        outcome = counter.prefix(json.dumps(fields, ensure_ascii=False), 256) + "\n"
            except (TypeError, ValueError):
                pass
            excerpt = outcome + counter.prefix(raw, cap) + marker
            message["content"] = "<skill_result>\n" + excerpt + "\n</skill_result>" if legacy else excerpt
            if i not in trimmed:
                trimmed.append(i)
            if cost() <= target:
                break
    protected_frame = set()
    preserved_frame = False
    if cost() > limit:
        # Re-budget the original memory material *after* the complete request's
        # schema/current-input/tool overhead is known. This prevents second-stage
        # trimming from erasing a source already deduplicated against working.
        frames = getattr(journal, "prompt_frames", []) if journal else []
        for frame in reversed(frames if isinstance(frames, list) else []):
            start = next((i for i, m in enumerate(result) if m.get("content") == frame["first"]), None)
            if start is None:
                continue
            end = next((i for i in range(start + 1, len(result))
                        if result[i].get("content") == frame["last"]), None)
            if end is None:
                continue
            outside = [*result[:start], *result[end + 1:]]
            required_outside = [m for i, m in enumerate(result)
                                if not start <= i <= end and not optional_history(i, m)]
            frame_budget = frame["context"].get("context_budget") or {}
            frame_cost = counter.messages(result[start:end + 1])
            already_bounded = (frame_budget.get("counting_method") == counter.method
                               and 0 < frame_budget.get("limit", 0) <= configured_limit
                               and frame_cost <= configured_limit
                               and isinstance(frame_budget.get("fixed_tokens"), int))
            unreachable_soft_target = (already_bounded and schema_cost + counter.messages(required_outside)
                                       + frame_budget["fixed_tokens"] > configured_limit)
            if unreachable_soft_target and (hard_limit is None or
                    frame_cost + schema_cost + counter.messages(required_outside) <= hard_limit):
                # This frame already respected the original prompt budget. Tool
                # declarations must not wipe its evidence simply because their
                # unavoidable cost alone makes the soft request target impossible.
                protected_frame = set(range(start, end + 1))
                preserved_frame = True
                break
            target = hard_limit if unreachable_soft_target else limit
            outside_cost = counter.messages(required_outside if unreachable_soft_target else outside)
            available = max(0, target - schema_cost - outside_cost)
            if unreachable_soft_target:
                available = min(available, configured_limit)
            from common.clock import CLOCK_MARKER
            from prompt_builder import build_conversation_messages
            # outside already includes this request's actual current input.
            # Reserving it again would discard memory despite unused capacity.
            rebuilt = build_conversation_messages(frame["context"], budget_limit=available,
                                                   reserve_current_input=False)
            if any(str(m.get("content") or "").startswith(CLOCK_MARKER) for m in outside):
                rebuilt = [m for m in rebuilt if not str(m.get("content") or "").startswith(CLOCK_MARKER)]
            result = [*result[:start], *rebuilt, *result[end + 1:]]
            if unreachable_soft_target:
                protected_frame = set(range(start, start + len(rebuilt)))
            current_user = next((i for i in range(len(result) - 1, -1, -1)
                                 if result[i].get("role") == "user"), None)
            break
    if cost() > limit:
        # Native call-bearing messages and tool results are inseparable. Keeping
        # both is safer than forgetting an already-executed external operation.
        discard = []
        order = sorted(range(len(result)), key=lambda i: str(result[i].get("content") or "").startswith("<memory_recall>"))
        for i in order:
            message = result[i]
            if i in protected_frame:
                continue
            if optional_history(i, message):
                discard.append(i)
                message["content"] = ""
                if cost() - 16 * len(discard) <= limit:
                    break
        removed = len(discard)
        result = [message for i, message in enumerate(result) if i not in discard]
    after = counter.messages(result) + schema_cost
    # Keep the original over_budget/limit fields for diagnostic consumers. They
    # describe the cleanup target; only hard_window_exceeded prohibits sending.
    roles = {role: counter.messages([m for m in result if m.get("role") == role])
             for role in ("system", "user", "assistant", "tool")}
    current_input = next((m for m in reversed(result) if m.get("role") == "user"), None)
    hard_exceeded = hard_limit is not None and after > hard_limit
    trace = {"limit": limit, "counting_method": counter.method, "tokenizer_error": counter.tokenizer_error,
             "configured_input_limit": configured_limit, "model_context_window": window,
             "soft_target": configured_limit, "soft_target_exceeded": after > configured_limit,
             "hard_input_limit": hard_limit, "hard_window_exceeded": hard_exceeded,
             "count_is_upper_bound": counter.encoder is None,
             "output_reserve": output_reserve, "framing_reserve": 256 if window else None,
             "input_units_before": before, "input_units": after, "schema_units": schema_cost,
             "message_units_by_role": roles,
             "current_input_units": counter.messages([current_input]) if current_input else 0,
             "bounded_frame_preserved": preserved_frame,
             "tool_excerpts": len(trimmed), "history_removed": removed, "over_budget": after > limit}
    diagnostic("request", trace)
    if hard_exceeded:
        raise ContextBudgetExceeded(
            f"模型请求超过显式模型输入窗口（请求计数={after}，输入上限={hard_limit}，"
            f"工具声明={schema_cost}，输出预留={output_reserve}，计数方式={counter.method}）；未发送请求",
            diagnostic=trace)
    return result
