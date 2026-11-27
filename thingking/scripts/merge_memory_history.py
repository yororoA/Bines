#!/usr/bin/env python3
"""Review a historical branch recovery plan, test a copy, then apply offline.

No arguments select production implicitly. `plan` and `stage` never write to the
target. `apply` is for a stopped, backed-up database and requires an explicit path.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "thingking" / "src"))

from memory.history_merge import apply_plan, make_plan, save_private, stage_plan, validate_outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="只读生成无正文恢复计划")
    for name in ("source", "target", "manifest", "candidates", "output"):
        plan.add_argument("--" + name, type=Path, required=True)
    stage = commands.add_parser("stage", help="只读复制目标并在隔离副本恢复")
    stage.add_argument("--plan", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--report", type=Path, required=True)
    apply = commands.add_parser("apply", help="写入明确指定的已停止服务数据库")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--target", type=Path, required=True)
    apply.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))
    try:
        if args.command == "plan":
            validate_outputs([args.output], inputs=[args.manifest, args.candidates], databases=[args.source, args.target])
            result = make_plan(args.source, args.target, load(args.manifest), load(args.candidates))
            save_private(args.output, result)
            print(json.dumps(result["summary"], ensure_ascii=False))
        else:
            plan_data = load(args.plan)
            outputs = [args.output, args.report] if args.command == "stage" else [args.report]
            databases = [plan_data["source"], plan_data["target"]]
            if args.command == "apply":
                databases.append(args.target)
            validate_outputs(outputs, inputs=[args.plan], databases=databases)
            if args.command == "stage":
                validate_outputs([args.report], databases=[args.output])
            result = stage_plan(plan_data, args.output) if args.command == "stage" else apply_plan(plan_data, args.target)
            save_private(args.report, result)
            print(json.dumps({k: v for k, v in result.items() if k in (
                "messages_inserted", "memories_inserted", "memories_quarantined", "already_applied", "state_and_jobs_unchanged")}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"恢复中止：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
