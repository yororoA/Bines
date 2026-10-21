"""End-to-end over real ZMQ: Thinking-side client + worker against an in-process Memory Server."""
import json
import socket
import threading
import unittest

import zmq

from helpers import OWNER_QQ, STRANGER_QQ, MemoryFixture, local_env, qq_group_env, qq_private_env

import prompt_builder
from memory.client import MemoryClient
from memory.server import dispatch
from memory.worker import MemoryWorker


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class InProcessServer:
    def __init__(self, service):
        self.service = service
        self.port = _free_port()
        self.context = zmq.Context()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._ready.wait(5)

    def _run(self):
        rep = self.context.socket(zmq.REP)
        rep.bind(f"tcp://127.0.0.1:{self.port}")
        self._ready.set()
        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)
        while not self._stop.is_set():
            if not dict(poller.poll(50)):
                continue
            request = json.loads(rep.recv().decode("utf-8"))
            rep.send(json.dumps(dispatch(self.service, request), ensure_ascii=False, default=str).encode("utf-8"))
        rep.close(linger=0)

    def close(self):
        self._stop.set()
        self.thread.join(2)
        self.context.term()


class FakeLLM:
    """Returns a deterministic JSON summary that mentions the last user line."""

    def __init__(self):
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        prompt = messages[-1]["content"]
        if "日记" in messages[0]["content"] or "日期：" in prompt:
            return "今天和管理员聊得很开心。"
        subject = "owner" if "owner" in prompt else f"qq:{STRANGER_QQ}"
        return json.dumps({
            "summary": "管理员告诉碧波，下周三要去医院复查。",
            "tags": ["Daily"],
            "importance": 8,
            "facts": [{"subject": subject, "content": "管理员下周三要去医院复查", "importance": 8}],
        }, ensure_ascii=False)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.fx = MemoryFixture(summarize_trigger=6, summarize_chunk=6, working_window=4)
        self.server = InProcessServer(self.fx.service)
        self.client = MemoryClient("127.0.0.1", self.server.port, timeout_ms=5000)
        self.llm = FakeLLM()
        self.state_updates = []
        self.worker = MemoryWorker(self.client, self.llm, lambda: "写日记的人格", on_owner_summary=self.state_updates.append)

    def tearDown(self):
        self.server.close()
        self.fx.close()

    def _talk_locally(self, rounds):
        for i in range(rounds):
            self.fx.clock.advance(60)
            self.client.record_turn(local_env(ts=self.fx.clock()), f"本机第{i}句：下周三我要去医院复查", f"知道了{i}")

    def _visible_text(self, envelope, query):
        context = self.client.build_context(envelope, query)
        return "\n".join(m["content"] for m in prompt_builder.build_conversation_messages(context))

    def test_ping_and_unknown_method(self):
        self.assertTrue(self.client.ping())
        with self.assertRaises(RuntimeError):
            self.client.call("drop_everything")

    def test_local_to_owner_private_continuity_and_isolation(self):
        self._talk_locally(3)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.state_updates, ["管理员告诉碧波，下周三要去医院复查。"])
        self._talk_locally(2)

        owner_qq = self._visible_text(qq_private_env(OWNER_QQ), "医院复查是哪天")
        self.assertIn("下周三要去医院复查", owner_qq)
        self.assertIn("[其他渠道的近期对话]", owner_qq)

        group = self._visible_text(qq_group_env(OWNER_QQ), "医院复查是哪天")
        stranger = self._visible_text(qq_private_env(STRANGER_QQ), "医院复查是哪天")
        for leaked in (group, stranger):
            self.assertNotIn("医院复查", leaked)

    def test_group_member_only_recalls_group_context(self):
        for i in range(3):
            self.fx.clock.advance(60)
            env = qq_group_env(STRANGER_QQ, ts=self.fx.clock(), name="小明")
            self.client.record_message(env, "observed", f"小明在群里说第{i}件事")
            self.client.record_turn(env, f"@碧波 帮我记一下第{i}件事", f"好的{i}")
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.state_updates, [], "group summaries must not touch the owner's state")

        other_group = self._visible_text(qq_group_env(STRANGER_QQ, group_id="99999"), "医院复查")
        self.assertNotIn("复查", other_group)
        same_group = self._visible_text(qq_group_env(STRANGER_QQ, name="小明"), "医院复查")
        self.assertIn("复查", same_group)
        owner_local = self._visible_text(local_env(), "医院复查")
        self.assertNotIn("小明", owner_local)

    def test_worker_failure_is_retried(self):
        self._talk_locally(3)
        broken = MemoryWorker(self.client, lambda messages: "not json", lambda: "")
        self.assertTrue(broken.run_once())
        jobs = self.fx.repo.job_counts()
        self.assertEqual(jobs.get("pending"), 1)
        self.fx.clock.advance(3600)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.fx.repo.job_counts().get("done"), 1)

    def test_diary_is_written_in_persona_voice(self):
        self._talk_locally(3)
        self.worker.run_once()
        self.fx.clock.advance(2 * 86400)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.llm.calls[-1][0]["content"], "写日记的人格")
        diaries = self.client.list_memories(kinds=["diary"])
        self.assertEqual([d["content"] for d in diaries["items"]], ["今天和管理员聊得很开心。"])

    def test_update_status_tool_uses_versioned_state(self):
        from tools.dependencies import deps
        from tools.memory_tool import update_status

        deps.memory_client = self.client
        try:
            self.client.put_state("owner", {"relationship_score": 10, "inventory": []})
            result = update_status(add_item="耳机", relationship_delta=2, npc_activity="听歌")
            state = self.client.get_state("owner")["data"]
        finally:
            deps.memory_client = None
        self.assertIn("动态状态已更新", result)
        self.assertEqual(state["relationship_score"], 12)
        self.assertEqual(state["inventory"], ["耳机"])
        self.assertEqual(state["npc_state"]["current_activity"], "听歌")


if __name__ == "__main__":
    unittest.main()
