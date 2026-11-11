"""Verbatim external text eligible for expression learning, never tool instructions."""
from __future__ import annotations

import json


def _at(value, path):
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value[key]
        elif isinstance(value, list) and type(key) is int and key >= 0:
            value = value[key]
        else:
            raise ValueError("表达来源字段无效")
    return value


def experience_text_fields(record):
    """Return allowlisted text leaves; paths are relative to record['result'].

    A successful observation is evidence that text was obtained, not that it was
    presented. The journal separately confirms the actual model request.
    """
    if (not isinstance(record, dict) or record.get("category") not in {"tool", "observation"}
            or record.get("status") != "succeeded"):
        return []
    result, action = record.get("result"), record.get("action")
    if not isinstance(result, dict) or result.get("human_action_required"):
        return []
    paths = []
    if action in {"web_fetch", "web_fetch_full"}:
        if result.get("status") not in (None, "ok", "success", "succeeded"):
            return []
        paths = [["content"]]
    elif action in {"website_snapshot", "website_watch", "website_auto_observation"}:
        if result.get("status") != "ok":
            return []
        if action == "website_snapshot":
            paths = [["snapshot"]]
        if action == "website_auto_observation":
            paths.append(["visible_text"])
        if action in {"website_watch", "website_auto_observation"}:
            frames = result.get("frames")
            if isinstance(frames, list):
                for i, frame in enumerate(frames[:6]):
                    captions = frame.get("captions") if isinstance(frame, dict) else None
                    if isinstance(captions, list):
                        paths.extend(["frames", i, "captions", j] for j in range(min(12, len(captions))))
    fields = []
    for path in paths:
        try:
            value = _at(result, path)
        except (KeyError, IndexError, ValueError):
            continue
        if isinstance(value, str) and value.strip():
            fields.append({"path": path, "text": value})
    return fields


def extract_experience_text(record, path):
    """Resolve an exact permitted field or reject; never fall back to a summary."""
    if (not isinstance(path, list) or not path or len(path) > 4
            or any(not isinstance(key, str) and type(key) is not int for key in path)):
        raise ValueError("表达来源需要明确的原文字段路径")
    for field in experience_text_fields(record):
        if field["path"] == path and all(type(a) is type(b) for a, b in zip(field["path"], path)):
            return field["text"]
    raise ValueError("该字段不是可学习的网页原文、可见文字或字幕")


def _string_spans(raw):
    """Locate JSON string values, retaining encoded offsets for a cut-off result."""
    decoder = json.JSONDecoder()

    def space(pos):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        return pos

    def scan(pos, path):
        pos = space(pos)
        if raw[pos] == "{":
            pos = space(pos + 1)
            while raw[pos] != "}":
                key, pos = decoder.raw_decode(raw, pos)
                pos = space(pos)
                if not isinstance(key, str) or raw[pos] != ":":
                    raise ValueError("invalid JSON object")
                pos = yield from scan(pos + 1, [*path, key])
                pos = space(pos)
                if raw[pos] != ",":
                    break
                pos = space(pos + 1)
            if raw[pos] != "}":
                raise ValueError("invalid JSON object")
            return pos + 1
        if raw[pos] == "[":
            pos, index = space(pos + 1), 0
            while raw[pos] != "]":
                pos = yield from scan(pos, [*path, index])
                index += 1
                pos = space(pos)
                if raw[pos] != ",":
                    break
                pos = space(pos + 1)
            if raw[pos] != "]":
                raise ValueError("invalid JSON list")
            return pos + 1
        value, end = decoder.raw_decode(raw, pos)
        if isinstance(value, str):
            yield path, value, pos + 1, end - 1
        return end

    yield from scan(0, [])


def visible_experience_fields(record, rendered, visible):
    """Recover only text shown from this known tool receipt, including JSON cuts.

    The caller must bind the receipt using a runtime tool-call ID or its exact
    result hash. A page-supplied source URL/ID never grants that authority.
    """
    fields = {tuple(field["path"]): field["text"] for field in experience_text_fields(record)}
    if not fields or not isinstance(rendered, str) or not isinstance(visible, str):
        return []
    rendered = rendered.strip()
    if not rendered:
        return []
    # Request budgeting prepends outcome metadata and keeps a prefix of the
    # original JSON. Match the actual serialization, not text elsewhere in args.
    start = visible.find(rendered[:min(8, len(rendered))])
    if start < 0:
        return []
    end = 0
    for original, shown in zip(rendered, visible[start:]):
        if original != shown:
            break
        end += 1
    result = []
    try:
        for path, value, left, right in _string_spans(rendered):
            original = fields.get(tuple(path))
            if original != value or end <= left:
                continue
            encoded = rendered[left:min(end, right)]
            # A budget may split an escape or surrogate pair. Retire that last
            # incomplete character rather than manufacturing a visible quote.
            for trim in range(min(12, len(encoded)) + 1):
                try:
                    text = json.loads('"' + (encoded[:-trim] if trim else encoded) + '"')
                except ValueError:
                    continue
                if text and original.startswith(text):
                    result.append({"path": path, "text": text})
                    break
    except (ValueError, IndexError, RecursionError):
        return []
    return result


def visible_observation_fields(record, shown):
    """A projected signed observation keeps its ID and prefixes text leaves."""
    original = record.get("result") or {}
    if (record.get("action") != "website_auto_observation" or not isinstance(shown, dict)
            or not original.get("id") or shown.get("id") != original.get("id")):
        return []
    fields = []
    for field in experience_text_fields(record):
        try:
            value = _at(shown, field["path"])
        except (KeyError, IndexError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        # The perception projector appends an ellipsis when a leaf was cut.
        if value != field["text"] and value.endswith("…"):
            value = value[:-1]
        if value and field["text"].startswith(value):
            fields.append({"path": field["path"], "text": value})
    return fields
