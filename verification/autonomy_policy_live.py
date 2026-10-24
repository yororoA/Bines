"""Real-model judgment checks with synthetic material and no executable tools."""
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from common.activity_deliberation import choose_activity
from common.proactive import ProactivePolicy, appraise_history, capability_summary, draft_decision
from thinking_model_helper import ThinkingModelHelper
from tools.context import ToolContext, tool_context


def main():
    model = ThinkingModelHelper()
    now = datetime(2026, 10, 24, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    evidence = {"real_model": True, "synthetic_material": True, "external_tool_calls": 0,
                "real_outbound_messages": 0, "cases": []}
    with patch("common.activity.activity_control", return_value={}):
        policy = ProactivePolicy(now-121, state={
            "user_ref": "", "drives": {"connection": .01, "curiosity": .01, "initiative": .01}})
        assert policy.tick(now, None, life={"phase": "waking", "energy": .6})
        materials = {"recent": [], "memories": [], "profile": {},
                     "life": {"phase": "waking", "energy": .6, "sleep_pressure": .3},
                     "available_skills": capability_summary("QQ"), "planning_only": False,
                     **policy.contact_permissions(now, None)}
        appraisal = appraise_history(materials, policy.state, now, model=model)
        assert appraisal, "Real appraisal did not satisfy its schema"
        policy.appraise(appraisal, "", now)
        decision = draft_decision(materials, policy.state, now, model=model, capabilities="QQ",
                                  allowed_kinds=("curiosity", "initiative"))
        assert policy.state.last_decision["status"] in {"selected", "quiet"}, policy.state.last_decision
        if decision:
            assert decision["kind"] in {"curiosity", "initiative"}, decision
        evidence["cases"].append({"case": "waking_low_drive_no_user_history",
                                  "appraisal": appraisal, "judgment": policy.state.last_decision})
        print("真实模型：低意愿、无新用户任务的发起取舍已完成", flush=True)

        policy.state.interests = ["海鸟行为"]
        policy.state.recent_outcomes = [{"topic": "海鸟行为", "outcome": "completed",
                                         "result": "验证构造材料：上一段兴趣已结束；当前没有待执行任务"}]
        policy.state.planning_only = True
        resting = {**materials, "planning_only": True, "contact_allowed": False,
                   "owner_contact_allowed": False, "life": {
                       "phase": "resting", "energy": .5, "sleep_pressure": .45}}
        draft_decision(resting, policy.state, now+1800, model=model, capabilities="QQ",
                       allowed_kinds=("curiosity", "initiative"))
        assert policy.state.last_decision["status"] in {"selected", "quiet"}, policy.state.last_decision
        evidence["cases"].append({"case": "resting_internal_judgment",
                                  "judgment": policy.state.last_decision})
        print("真实模型：休息期间内部取舍已完成", flush=True)

        candidate = {"id": "synthetic-review", "kind": "curiosity", "topic": "海鸟行为",
                     "intent": "考虑下一段兴趣", "reason": "验证自主换方向、暂停或结束",
                     "planning_only": True, "contact_allowed": False, "owner_contact_allowed": False,
                     "activity": {"mode": "review", "id": "synthetic-review", "observations": []},
                     "life": resting["life"], "emotion": policy.state.emotion}
        with tool_context(ToolContext(turn_id="synthetic-review", autonomous=True, planning_only=True,
                                      contact_allowed=False, owner_contact_allowed=False)):
            choice = choose_activity(model, {"working": [], "state": {}, "life": resting["life"]}, candidate)
        evidence["cases"].append({"case": "model_owns_next_direction", "choice": choice})
        print(f"真实模型：活动取舍选择 {choice['mode']}，原因：{choice['reason']}", flush=True)

        policy.state.planning_only = False
        policy.state.drives["curiosity"] = .85
        policy.state.emotion["curiosity"] = .85
        previous = {"id": "synthetic-finished", "kind": "curiosity", "topic": "海鸟行为",
                    "intent": "查询海鸟基本种类", "reason": "自身兴趣", "based_on_user": "",
                    "timestamp": now+1801}
        policy.reserve(previous)
        policy.resolve(now+1801, delivered=True, reflection="验证构造：基础种类问题已完成，协作行为尚未探索")
        active = {**materials, "life": {"phase": "active", "energy": .75, "sleep_pressure": .3},
                  "recent_outcomes": policy.state.recent_outcomes}
        decision = draft_decision(active, policy.state, now+2101, model=model, capabilities="QQ",
                                  allowed_kinds=("curiosity",))
        assert policy.state.last_decision["status"] in {"selected", "quiet"}, policy.state.last_decision
        if decision:
            assert decision["kind"] == "curiosity" and decision["intent"] != previous["intent"], decision
        evidence["cases"].append({"case": "new_question_after_finished_interest",
                                  "judgment": policy.state.last_decision})
        print("真实模型：上一段兴趣完成后的新方向取舍已完成", flush=True)

    evidence["recorded_at"] = time.time()
    path = ROOT / "verification" / "autonomy_policy_live_result.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PASS · 4组真实模型取舍 · 未提供可执行外部工具", flush=True)


if __name__ == "__main__":
    main()
