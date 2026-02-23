#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通过指定 JSON 文件将 RAG 记忆库内容替换为该文件中的内容。

使用前请确保 RAG 服务已启动（例如运行 python thingking/rag_server.py）。

用法:
  python thingking/replace_rag_from_json.py <json文件路径>
  python thingking/replace_rag_from_json.py memories.json --dry-run   # 仅预览，不写入

JSON 格式支持两种:

1) 字符串列表，每条作为「剧情摘要」写入（默认重要性 7）:
   ["记忆内容1", "记忆内容2", ...]

2) 对象列表，可指定类型与重要性:
   [
     {"content": "记忆内容", "type": "episode_summary", "importance_score": 7},
     {"content": "User: xxx\\nAssistant: yyy", "type": "raw_conversation", "importance_score": 5}
   ]
   - content: 必填
   - type: 可选，"episode_summary"（默认）或 "raw_conversation"
   - importance_score: 可选，1-10，默认 7（episode_summary）/ 5（raw_conversation）
"""

import argparse
import json
import sys
from pathlib import Path

# 与 recover_from_backup.py 一致的路径设置
CURRENT_DIR = Path(__file__).resolve().parent
SRC_DIR = CURRENT_DIR / "src"
ROOT_DIR = CURRENT_DIR.parent

for p in (str(ROOT_DIR), str(SRC_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from rag_memory import RAGMemory


def load_memories_from_json(path: str):
    """从 JSON 文件加载记忆列表。返回 [(content, type, importance_score), ...]"""
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON 根节点必须是数组（list）")

    result = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            result.append((item.strip(), "episode_summary", 7))
        elif isinstance(item, dict):
            content = item.get("content")
            if content is None:
                raise ValueError(f"第 {i+1} 条缺少 'content' 字段")
            mem_type = item.get("type", "episode_summary")
            if mem_type not in ("episode_summary", "raw_conversation"):
                mem_type = "episode_summary"
            imp = item.get("importance_score")
            if imp is None:
                imp = 7 if mem_type == "episode_summary" else 5
            else:
                imp = max(1, min(10, int(imp)))
            result.append((str(content).strip(), mem_type, imp))
        else:
            raise ValueError(f"第 {i+1} 条应为字符串或对象，当前类型: {type(item).__name__}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="用指定 JSON 文件的内容替换 RAG 记忆库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "json_file",
        help="记忆内容 JSON 文件路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印将要执行的操作，不调用 RAG 服务",
    )
    args = parser.parse_args()

    try:
        memories = load_memories_from_json(args.json_file)
    except Exception as e:
        print(f"❌ 加载 JSON 失败: {e}")
        sys.exit(1)

    if not memories:
        print("⚠️ JSON 中没有任何记忆条目，操作将仅清空 RAG 记忆库。")
    else:
        print(f"📄 已从 {args.json_file} 加载 {len(memories)} 条记忆")

    if args.dry_run:
        print("\n[--dry-run] 将执行:")
        print("  1. 清空当前 RAG 记忆库")
        for i, (content, mem_type, imp) in enumerate(memories):
            preview = (content[:60] + "…") if len(content) > 60 else content
            print(f"  2.{i+1} 添加 [{mem_type}] 重要性={imp}: {preview}")
        print("\n取消 --dry-run 后将实际写入。")
        return

    print("正在连接 RAG 服务并清空记忆库…")
    rag = RAGMemory()
    rag.clear()
    print("✅ 记忆库已清空")

    if not memories:
        print("未添加新记忆，脚本结束。")
        return

    added = 0
    for content, mem_type, importance_score in memories:
        if not content:
            continue
        try:
            if mem_type == "episode_summary":
                rag.add_episode_summary(content, importance_score=importance_score)
            else:
                rag.add_raw_conversation(content, importance_score=importance_score)
            added += 1
        except Exception as e:
            print(f"⚠️ 添加记忆失败: {e}")

    print(f"✅ 已向 RAG 记忆库写入 {added} 条记忆")


if __name__ == "__main__":
    main()
