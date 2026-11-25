"""Autonomous appraisal/decision requests, without models or persistent state."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]


class CaptureModel:
    def __init__(self):
        self.requests = []

    def complete_text(self, messages, **options):
        self.requests.append(copy.deepcopy(messages))
        return '{"should_act":false,"reason":"fixture","delay_seconds":60}'


class DeliberationContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bines-deliberation-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        empty_env = root / "empty.env"
        empty_env.write_text("")
        env = patch.dict(os.environ, {
            "BINES_ENV_FILE": str(empty_env), "BINES_MEMORY_DIR": str(root / "memory"),
            "BINES_GATEWAY_DIR": str(root / "gateway"), "BINES_HEALTH_DIR": str(root / "health"),
            "BINES_MODEL_USAGE_PATH": str(root / "usage.sqlite"),
            "BINES_WEBSITE_BRIDGE_SPOOL": str(root / "website"),
            "BINES_WEBSITE_AUTO_OBSERVE": "false", "BINES_CONTEXT_TOKENIZER_PATH": "",
            "BINES_MODEL_CONTEXT_TOKENS": "", "BINES_CONTEXT_MAX_TOKENS": "65536",
            "BINES_DELIBERATION_MAX_TOKENS": "65536",
        })
        env.start()
        self.addCleanup(env.stop)
        self.model = CaptureModel()

    def data(self):
        fact = "Alice plans to visit on Friday."
        return {
            "context": {
                "envelope": {"scope": "owner", "actor_id": "owner", "channel_type": "qq_private"},
                "recent": [{"id": 1, "role": "user", "actor_id": "owner", "ts": 100,
                            "content": "Please remember the museum."}],
                "memories": [{"id": "fact-1", "kind": "semantic", "content": fact,
                              "scope": "owner", "subject_id": "owner", "context_id": "internal-context",
                              "score": .99, "node_digest": "internal-digest", "reason_shares": {"x": .5},
                              "source_family": ["internal-family"],
                              "evidence": [{"source_ref": "message:1", "role": "user", "actor_id": "owner",
                                            "content": fact, "ts": 100}],
                              "temporal": {"valid_from": 100, "valid_to": 200, "is_current": False},
                              "corrections": [{"quote": "Friday", "replacement": "Saturday", "kind": "fact"}]}],
                "profile": [], "bridge": None,
                "state": {"current_location": "home", "active_quest": "museum"},
                "life": {"phase": "resting", "energy": .3},
                "activity": {"mode": "pause", "note": "waiting for login", "delay_seconds": 0},
                "contact_allowed": False, "owner_contact_allowed": False, "planning_only": True,
                "available_skills": "get_time, search_memory, activity_pause",
                "recent_outcomes": [{"topic": "outcome-fixture", "result": "actually finished"}],
            },
            "emotion": {"social": .5}, "willingness": {"connection": .4},
            "allowed_kinds": ["curiosity"], "capabilities": "QQ", "now": 1000,
            "recent_outcomes": [{"topic": "outcome-fixture", "result": "actually finished"}],
        }

    def request(self, data=None):
        from common.proactive import _complete
        _complete("fixture persona and decision rules", data or self.data(), self.model, max_tokens=256)
        return self.model.requests[-1]

    def test_sources_precede_changing_state_and_clock_without_changing_prefix(self):
        first = self.request()
        data = self.data()
        data["now"] += 60
        data["emotion"] = {"social": .1}
        data["context"]["state"]["current_location"] = "outside"
        second = self.request(data)
        a = next(i for i, m in enumerate(first) if "Alice plans" in m["content"])
        b = next(i for i, m in enumerate(first) if '"current_location"' in m["content"])
        self.assertLess(a, b)
        self.assertEqual(first[:a + 1], second[:a + 1])
        self.assertIn("当前真实时间", first[-1]["content"])

    def test_projection_deduplicates_evidence_and_keeps_identity_validity_corrections(self):
        data = self.data()
        original = copy.deepcopy(data)
        text = json.dumps(self.request(data), ensure_ascii=False)
        self.assertEqual(text.count("Alice plans to visit on Friday."), 1)
        for removed in ("internal-context", "internal-digest", "internal-family", "reason_shares"):
            self.assertNotIn(removed, text)
        for retained in ("message:1", "owner", "valid_from", "valid_to", "is_current", "Saturday"):
            self.assertIn(retained, text)
        self.assertEqual(data, original)

    def test_recent_outcomes_have_one_copy_and_current_permissions_survive(self):
        messages = self.request()
        text = "\n".join(m["content"] for m in messages)
        self.assertEqual(text.count("outcome-fixture"), 1)
        for value in ('"contact_allowed":false', '"owner_contact_allowed":false',
                      '"planning_only":true', '"mode":"pause"', "waiting for login", "resting"):
            self.assertIn(value, text.replace(": ", ":"))

    def test_finished_contacts_and_explorations_do_not_replay_full_reflections(self):
        from common.proactive import ProactiveConfig, ProactivePolicy
        policy = ProactivePolicy(100, ProactiveConfig())
        chat = "这里是之前发出的整段问候。" * 40
        finding = "观测记录里，燕鸥在清晨五点离巢。"
        reflection = "整理一下今天的收获。" * 50 + finding + "期待下次还有机会继续探索。" * 40
        for kind, topic, result in (("connection", "问候", chat), ("curiosity", "燕鸥离巢", reflection)):
            policy.reserve({"kind": kind, "topic": topic, "intent": topic, "based_on_user": "turn-1", "id": kind})
            policy.resolve(102, delivered=True, reflection=result,
                           activity={"mode": "finish", "decision_source": "model",
                                     "decision": {"interest_level": .7, "reason": "还有想确认的细节"}})
        snapshot = copy.deepcopy(policy.state.recent_outcomes)
        data = {**self.data(), "recent_outcomes": policy.state.recent_outcomes}
        messages = self.request(data)
        section = next(m["content"] for m in messages if m["content"].startswith("<deliberation_recent_outcomes>"))
        contact, exploration = json.loads(section.splitlines()[1])["records"]
        from common.clock import now as local_datetime
        self.assertEqual(contact, {**{k: v for k, v in snapshot[0].items() if k != "result"},
                                   "at_local": local_datetime(102).isoformat(timespec="seconds")})
        self.assertNotIn(chat[:12], section)
        self.assertIn(finding, exploration["result"])
        self.assertLessEqual(len(exploration["result"]), 183)
        self.assertEqual(exploration["interest_level"], .7)
        self.assertEqual(policy.state.recent_outcomes, snapshot)
        self.assertEqual(snapshot[0]["result"], chat)

    def test_structured_receipts_and_unfinished_checkpoints_are_intact_when_selected(self):
        from common.context_budget import ContextTokenCounter
        from common.deliberation_context import _fit_section
        checkpoint = "等待操作继续，核验这个字段。" * 120
        rows = [{"kind": "connection", "outcome": "completed", "activity_mode": "pause", "result": checkpoint},
                {"kind": "connection", "outcome": "completed", "activity_mode": "finish",
                 "result": {"status": "sent", "operation_id": "send-14", "detail": checkpoint}}]
        messages = self.request({**self.data(), "recent_outcomes": rows})
        section = next(m["content"] for m in messages if m["content"].startswith("<deliberation_recent_outcomes>"))
        self.assertEqual(json.loads(section.splitlines()[1])["records"], rows)
        constrained = _fit_section("recent_outcomes", rows, 400, ContextTokenCounter(), newest=True)
        self.assertEqual(json.loads(constrained["content"].splitlines()[1]), {"records": [], "omitted": 2})

    def test_historical_timestamps_stay_local_and_stable_across_midnight(self):
        from datetime import datetime
        def stamp(iso):
            return datetime.fromisoformat(iso).timestamp()

        old = "2026-11-24T23:55:08+08:00"
        later = "2026-11-24T23:59:00+08:00"
        rows = [{"outcome": "completed", "at": stamp(later)}, {"outcome": "reply", "at": stamp(old)},
                {"outcome": "uncertain", "at": stamp(old)}, {"outcome": "legacy", "at": "unknown"}]
        data = self.data()
        data["recent_outcomes"] = rows
        data["context"]["recent"][0]["ts"] = stamp(old)
        data["context"]["memories"][0]["evidence"][0]["ts"] = stamp(old)
        data["now"] = stamp(later)
        with patch.dict(os.environ, {"BINES_MEMORY_TIMEZONE": "Asia/Shanghai"}):
            first = self.request(data)
            data["now"] += 3600
            second = self.request(data)
        self.assertEqual(first[:-1], second[:-1])
        sections = {m["content"].splitlines()[0]: json.loads(m["content"].splitlines()[1])
                    for m in second if m["content"].startswith("<deliberation_")}
        records = sections["<deliberation_recent_outcomes>"]["records"]
        self.assertEqual(records, [{**rows[0], "at_local": later}, {**rows[1], "at_local": old}, rows[3]])
        self.assertEqual(sections["<deliberation_current>"]["uncertain_outcomes"], [{**rows[2], "at_local": old}])
        self.assertEqual(sections["<deliberation_recent>"]["records"][0]["ts_local"], old)
        self.assertEqual(sections["<deliberation_memories>"]["records"][0]["evidence"][0]["ts_local"], old)
        self.assertNotIn("at_local", rows[0])

    def test_decision_separates_completed_plans_and_live_goals(self):
        data = self.data()
        data["recent_outcomes"] = [{"kind": "curiosity", "outcome": "completed", "activity_mode": "finish",
                                    "topic": "旧书图例", "intent": "先用工具翻旧索引再换词重查", "result": "那张图缺了右半边"},
                                   {"kind": "initiative", "outcome": "completed", "topic": "送票",
                                    "intent": "给小张送周五的票", "result": "送到了"}]
        data["previous_topics"] = ["更早的旧书书单", "旧书图例", "更早的旧书书单"]
        data["context"].update(
            ongoing_intention={"kind": "curiosity", "topic": "新书", "intent": "看看封面上的鸟"},
            queued_intentions=[{"kind": "initiative", "topic": "画画", "intent": "试试新画笔",
                                "reason": "模型上回写的一串缘由", "created_at": 80}],
        )
        original = copy.deepcopy(data)
        sections = {m["content"].splitlines()[0]: json.loads(m["content"].splitlines()[1])
                    for m in self.request(data) if m["content"].startswith("<deliberation_")}
        current = sections["<deliberation_current>"]
        self.assertNotIn("previous_topics", current)
        self.assertEqual(current["ongoing_intention"]["intent"], "看看封面上的鸟")
        self.assertEqual(current["queued_intentions"], [{"kind": "initiative", "topic": "画画",
                                                         "intent": "试试新画笔", "created_at": 80}])
        self.assertFalse(current["owner_contact_allowed"])
        self.assertEqual(sections["<deliberation_recent_outcomes>"]["records"], [
            {"previous_topic": "更早的旧书书单"},
            {"kind": "curiosity", "outcome": "completed", "activity_mode": "finish", "topic": "旧书图例",
             "result": "那张图缺了右半边"},
            {"kind": "initiative", "outcome": "completed", "topic": "送票",
             "intent": "给小张送周五的票", "result": "送到了"},
        ])
        self.assertEqual(data, original)

    def test_source_budget_keeps_body_evidence_and_required_current_state(self):
        from common.context_budget import ContextTokenCounter
        data = self.data()
        data["context"]["recent"] *= 12
        data["context"]["memories"] *= 5
        for row in data["context"]["recent"]:
            row["content"] = "Recent source body. " * 1000
        for row in data["context"]["memories"]:
            row["content"] = "Memory source body. " * 1000
        with patch.dict(os.environ, {"BINES_DELIBERATION_MAX_TOKENS": "8000"}):
            messages = self.request(data)
        self.assertLessEqual(ContextTokenCounter().messages(messages), 8000)
        text = "\n".join(m["content"] for m in messages)
        for value in ("Recent source body.", "Memory source body.", "Saturday", "valid_to",
                      "waiting for login", "get_time, search_memory", "resting"):
            self.assertIn(value, text)
        self.assertIn("节选", text)

    def test_budget_keeps_matched_fact_in_middle_of_long_source(self):
        data = self.data()
        row = data["context"]["memories"][0]
        fact = "The museum is closed on Saturday; go on Sunday instead."
        row["content"] = "Unrelated opening. " * 1000 + fact + " Unrelated ending." * 1000
        row["matched_spans"] = [fact, "not in the source"]
        with patch.dict(os.environ, {"BINES_DELIBERATION_MAX_TOKENS": "6500"}):
            text = "\n".join(m["content"] for m in self.request(data))
        self.assertIn(fact, text)
        self.assertNotIn("not in the source", text)
        self.assertNotIn("_matched_spans", text)

    def test_distinct_evidence_is_not_replaced_by_a_source_id(self):
        data = self.data()
        evidence = data["context"]["memories"][0]["evidence"][0]
        evidence["content"] = "The quoted user explicitly corrected the visit to Saturday."
        text = "\n".join(m["content"] for m in self.request(data))
        self.assertIn(evidence["content"], text)
        self.assertIn("message:1", text)

    def test_tiny_source_budget_preserves_uncertain_write_and_current_permissions(self):
        data = self.data()
        data["recent_outcomes"] = [{"topic": "sent museum invitation", "outcome": "uncertain",
                                    "result": {"status": "uncertain", "operation_id": "receipt-fixture"}}]
        with patch.dict(os.environ, {"BINES_DELIBERATION_MAX_TOKENS": "256"}):
            text = "\n".join(m["content"] for m in self.request(data))
        self.assertEqual(text.count("sent museum invitation"), 1)
        for value in ("receipt-fixture", '"owner_contact_allowed":false', "waiting for login", "resting"):
            self.assertIn(value, text)

    def test_selected_sources_also_fit_explicit_model_window(self):
        from common.context_budget import bound_request, ContextTokenCounter
        data = self.data()
        data["context"]["memories"][0]["content"] *= 1000
        with patch.dict(os.environ, {"BINES_MODEL_CONTEXT_TOKENS": "6500"}):
            messages = self.request(data)
            bounded = bound_request(messages, response_format={"type": "json_object"}, max_output_tokens=256)
        self.assertEqual(messages, bounded)
        self.assertLess(ContextTokenCounter().messages(messages), 6500 - 256 - 256)

    def test_both_appraisal_and_decision_use_shared_projection(self):
        from common.proactive import ProactiveState, appraise_history, draft_decision
        data = self.data()
        state = ProactiveState(recent_outcomes=data["recent_outcomes"])
        appraise_history(data["context"], state, data["now"], model=self.model)
        draft_decision(data["context"], state, data["now"], model=self.model, capabilities="desktop")
        self.assertEqual(len(self.model.requests), 2)
        for messages in self.model.requests:
            text = "\n".join(m["content"] for m in messages)
            self.assertEqual(text.count("outcome-fixture"), 1)
            self.assertEqual(text.count("Alice plans to visit on Friday."), 1)
            self.assertIn("<deliberation_current>", text)
            self.assertIn("当前真实时间", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
