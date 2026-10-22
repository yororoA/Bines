"""Run: python -m unittest verification.napcat_integration -v (no QQ writes)."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from chatBot.messages import Identity, LOCAL_IDENTITY, segments
from chatBot.store import MemoryStore
from chatBot.capabilities import CapabilityService, SchemaCatalog
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.napcat_client import NapCatClient
from tools.context import ToolContext, tool_context
from thingking.qq_thinking import QQThinking


def event(user="1", group=None, msg_id=1, text="你好", **extra):
    return {"self_id": 99, "post_type": "message", "message_type": "group" if group else "private",
            "group_id": group, "user_id": user, "message_id": msg_id, "time": time.time(),
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": user, "nickname": "同名", "role": "admin"}, **extra}


class FakeAPI:
    is_connected = True
    def __init__(self):
        self.calls = []
        self.history = []
        self.send_status = "ok"
        self.custom_faces = [
            {
                "url": f"https://p.qpic.cn/qq_expression/test/{index}",
                "md5": f"FACE{index}", "desc": f"收藏表情{index}",
            }
            for index in range(3)
        ]
    def raw_api(self, action, params, timeout=30, **kwargs):
        self.calls.append((action, params))
        if action.endswith("msg_history"):
            return {"status": "ok", "retcode": 0, "data": {"messages": self.history}}
        if action == "fetch_custom_face":
            return {"status": "ok", "retcode": 0,
                    "data": [row["url"] for row in self.custom_faces[:int(params.get("count", 20))]]}
        if action == "fetch_custom_face_detail":
            return {"status": "ok", "retcode": 0,
                    "data": self.custom_faces[:int(params.get("count", 20))]}
        return {"status": self.send_status, "retcode": 0,
                "data": {"message_id": 1000+len(self.calls)}}


class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.db", owners={"1"})
        self.api = FakeAPI()
        self.service = CapabilityService(self.api, self.store)
        from chatBot.memory_hub import MemoryHub
        from memory.settings import MemorySettings
        self.memory_hub = MemoryHub(self.store, profile="chatbot", settings=MemorySettings(
            data_dir=Path(self.temp.name) / "central", owner_qq_ids=frozenset({"1"}),
        ))
        self.server = GatewayServer(self.service, ("127.0.0.1", 0), token="test-secret",
                                    memory_hub=self.memory_hub)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="test-secret")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.memory_hub.close()
        self.temp.cleanup()

    def insert(self, ev):
        self.store.ingest(ev)
        with self.store.connection() as db:
            return db.execute("SELECT id FROM events ORDER BY received DESC LIMIT 1").fetchone()[0]

    def test_owner_private_shared_group_and_user_isolation(self):
        self.store.add(LOCAL_IDENTITY, "user", "桌面秘密")
        self.insert(event("1", text="主人私聊"))
        self.insert(event("2", text="其他用户"))
        self.insert(event("1", group="10", text="本群公开"))
        self.insert(event("3", group="20", text="另一群"))
        owner = self.store.identity(event("1"))
        stranger = self.store.identity(event("2"))
        group = self.store.identity(event("1", group="10"))
        self.assertIn("桌面秘密", str(self.store.context(owner)))
        self.assertNotIn("桌面秘密", str(self.store.context(group)))
        self.assertNotIn("另一群", str(self.store.context(group)))
        self.assertNotIn("主人私聊", str(self.store.context(stranger)))
        self.assertFalse(self.store.identity(event("2")).owner)
        self.assertFalse(self.store.identity(event("1", group="10")).privileged)
        changed_name = event("2")
        changed_name["sender"]["nickname"] = "改名"
        self.assertEqual(stranger.conversation, self.store.identity(changed_name).conversation)

    def test_atomic_claim_dedup_batch_restart(self):
        ev = event("2")
        self.assertTrue(self.store.ingest(ev))
        self.assertFalse(self.store.ingest(ev))
        self.store.ingest(event("2", msg_id=2))
        self.store.ingest(event("3", msg_id=3))
        batches = []
        threads = [threading.Thread(target=lambda index=i: batches.append(
            self.store.claim(str(index), 0, 0, lease=0.05))) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        batches = [batch for batch in batches if batch]
        self.assertEqual(len(batches), 2)
        self.assertEqual(len({row["id"] for batch in batches for row in batch}), 3)
        first = next(batch for batch in batches if len(batch) == 2)
        self.store.ingest(event("2", msg_id=4))
        time.sleep(0.06)
        replay = self.store.claim("new-worker", 0, 0)
        self.assertEqual([row["id"] for row in replay], [row["id"] for row in first])
        self.assertEqual(replay[0]["batch"], first[0]["batch"])

    def test_capabilities_scope_and_uncertain_write(self):
        identity = self.store.identity(event("2", group="10"))
        with self.assertRaises(PermissionError):
            self.service.call(identity, "get_group_msg_history", {"group_id": 20})
        with self.assertRaises(PermissionError):
            self.service.call(identity, "set_group_admin", {"group_id": 10, "user_id": 2})
        self.service.call(identity, "get_group_msg_history", {"group_id": 10})
        with self.assertRaises(PermissionError):
            self.service.call(identity, "get_group_msg_history", {"group_id": 10, "message_seq": "foreign-id"})
        self.api.send_status = "uncertain"
        params = {"user_id": 1, "message": "test"}
        first = self.service.call(LOCAL_IDENTITY, "send_private_msg", params, "stable-id")
        second = self.service.call(LOCAL_IDENTITY, "send_private_msg", params, "stable-id")
        self.assertEqual(first, second)
        self.assertEqual(sum(action == "send_private_msg" for action, _ in self.api.calls), 1)
        with self.assertRaises(ValueError):
            self.service.call(LOCAL_IDENTITY, "send_private_msg", {"user_id": 2, "message": "changed"}, "stable-id")
        self.service.call(LOCAL_IDENTITY, "new_napcat_extension", {}, "extension-id")

    def test_http_auth_and_model_cannot_forge_owner(self):
        ev = event("2")
        event_id = self.insert(ev)
        with tool_context(ToolContext(self.store.identity(ev), event_id, "turn")):
            with self.assertRaises(RuntimeError):
                self.client.raw_api("get_friend_list", {})
            result = self.client.raw_api("send_private_msg", {"user_id": 2, "message": "allowed"})
            self.assertEqual(result["status"], "ok")
            with self.assertRaises(RuntimeError):
                self.client.request("/action", {"action": "get_friend_list", "identity": {"owner": True}})
        invalid = GatewayClient(self.client.url, token="wrong")
        with self.assertRaises(RuntimeError):
            invalid.request("/health", {})
        self.api.send_status = "uncertain"
        with tool_context(ToolContext(self.store.identity(ev), event_id, "uncertain-turn")):
            first = self.client.raw_api("send_private_msg", {"user_id": 2, "message": "maybe delivered"})
            self.assertEqual(first["status"], "uncertain")
            with self.assertRaises(RuntimeError):
                self.client.raw_api("send_private_msg", {"user_id": 2, "message": "retry"})

    def test_notice_request_recall_never_become_chat(self):
        ev = event("2", text="to recall")
        self.insert(ev)
        self.store.ingest({"post_type": "notice", "notice_type": "friend_recall", "self_id": 99,
                           "user_id": 2, "message_id": 1})
        self.store.ingest({"post_type": "request", "request_type": "friend", "self_id": 99,
                           "user_id": 2, "flag": "request-flag", "comment": "add"})
        context = self.store.context(self.store.identity(ev))
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["content"], "[消息已撤回]")
        bot = QQThinking(self.store, self.client)
        self.assertFalse(bot.should_reply({"post_type": "request"}, self.store.identity(ev)))
        self.assertFalse(bot.should_reply(ev, self.store.identity(ev)))

    def test_qq_reply_history_and_no_false_memory(self):
        ev = event("2")
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        foreign = event("3", msg_id=8, text="foreign private")
        self.api.history = [foreign, event("2", msg_id=9, text="own history")]
        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                inner.context = str(conversation)
                return "回复", [], []
        model = Model()
        bot = QQThinking(self.store, self.client)
        bot.process(rows, model)
        self.assertIn("own history", model.context)
        self.assertNotIn("foreign private", model.context)
        self.assertIn("回复", str(self.store.context(self.store.identity(ev))))
        self.insert(event("3", msg_id=10))
        self.store.finish([row["id"] for row in rows], "worker")
        rows = self.store.claim("worker", 0, 0)
        self.api.send_status = "uncertain"
        with self.assertRaises(RuntimeError):
            bot.process(rows, Model())
        self.assertNotIn("'role': 'assistant'", str(self.store.context(self.store.identity(event("3")))))

    def test_long_group_reply_is_split_into_short_messages(self):
        from chatBot.messages import split_qq_text

        self.assertEqual(split_qq_text("短回复"), ["短回复"])
        long_text = (
            "这是一段比较长的说明。它会被拆成多条短消息发送。"
            "每条只讲一个意思。这样群里看起来不会太累。"
        ) * 3
        chunks = split_qq_text(long_text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 15 for chunk in chunks))
        # Words must not be cut in half: split only at whitespace or punctuation.
        self.assertEqual(
            split_qq_text("知道啊 黑白简笔画少女闭眼举双手 头顶写着投降两字"),
            ["知道啊", "黑白简笔画少女闭眼举双手", "头顶写着投降两字"],
        )

        for group in ("10", None):
            with self.subTest(group=group):
                ev = event("2", group=group, msg_id=80, message=[
                    {"type": "at", "data": {"qq": "99"}},
                    {"type": "text", "data": {"text": "讲个长的"}},
                ])
                self.insert(ev)
                rows = self.store.claim("worker", 0, 0)
                class Model:
                    def run_skill_protocol(inner, conversation, runner, **kwargs):
                        return long_text, [], []
                bot = QQThinking(self.store, self.client)
                bot.process(rows, Model())
                expected = "send_group_msg" if group else "send_private_msg"
                sends = [params for action, params in self.api.calls if action == expected]
                self.assertGreater(len(sends), 1)
                self.assertTrue(all(len(params["message"][-1]["data"]["text"]) <= 15 for params in sends))
                if group:
                    self.assertEqual(sum(
                        any(part["type"] == "at" for part in params["message"]) for params in sends
                    ), 1)
                    self.assertTrue(all(
                        not any(part["type"] == "at" for part in params["message"]) for params in sends[1:]
                    ))
                self.store.finish([row["id"] for row in rows], "worker")

    def test_cq_and_streaming_transport_nonblocking(self):
        self.assertEqual(segments("[CQ:at,qq=99]a&amp;b")[1]["data"]["text"], "a&b")
        holder = {}
        class Socket:
            def __init__(inner, url, **callbacks):
                inner.callbacks = callbacks
                inner.stop = threading.Event()
                holder["socket"] = inner
            def run_forever(inner, **kwargs):
                inner.callbacks["on_open"](inner)
                inner.stop.wait()
            def close(inner):
                inner.stop.set()
            def send(inner, payload):
                echo = json.loads(payload)["echo"]
                callback = inner.callbacks["on_message"]
                for kind in ("stream", "reset", "response"):
                    callback(inner, json.dumps({"echo": echo, "status": "ok", "retcode": 0,
                                               "stream": "stream-action", "data": {"type": kind}}))
        client = NapCatClient(factory=Socket)
        blocker = threading.Event()
        entered = threading.Event()
        client.add_event_listener(lambda ev: (entered.set(), blocker.wait(2)))
        client.connect()
        client.connect()
        try:
            self.assertTrue(client._connected.wait(1))
            client._on_message(client.ws, json.dumps(event()))
            self.assertTrue(entered.wait(1))
            result = client.raw_api("download_file_stream", {})
            self.assertEqual(result["data"]["type"], "response")
            self.assertEqual(len(result["stream_chunks"]), 2)
            self.assertEqual(len(client._pending), 0)
        finally:
            blocker.set()
            client.disconnect()

    def test_full_and_light_profiles_lazy_import(self):
        code = """
import sys
from process_registry import get_processes
from tools import TOOLS_REGISTRY
assert [p['name'] for p in get_processes(profile='chatbot')] == ['ChatBot','QQ Thinking','Memory Worker']
assert 'Thinking' in [p['name'] for p in get_processes(profile='full')]
assert 'Memory Worker' in [p['name'] for p in get_processes(profile='full')]
assert all(p['interpreter'] for p in get_processes(profile='full'))
assert not any(m in sys.modules for m in ['cv2','torch','zmq','selenium','pyautogui'])
"""
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True)

    def test_schema_auth_and_new_action(self):
        catalog = SchemaCatalog(token="secret")
        import hashlib
        seen = []
        def request(path, payload=None, credential=None):
            seen.append((path, payload))
            if path.endswith("login"):
                return {"Credential": "credential"}
            return {"new_napcat_extension": {"description": "new feature", "payload": {"type": "object"}}}
        catalog._request = request
        exposed = {entry["action"] for entry in catalog.discover(LOCAL_IDENTITY)}
        self.assertIn("new_napcat_extension", exposed)
        self.assertIn("send_group_msg", exposed)
        self.assertTrue(catalog.discover(LOCAL_IDENTITY, action="new_napcat_extension")["runtime_supported"])
        self.assertEqual(seen[0][1]["hash"], hashlib.sha256(b"secret.napcat").hexdigest())

    def test_mcp_stdio_protocol(self):
        async def check():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            env = dict(os.environ)
            env["BINES_QQ_GATEWAY_PORT"] = str(self.server.server_port)
            env["BINES_QQ_GATEWAY_TOKEN"] = "test-secret"
            async with stdio_client(StdioServerParameters(
                command=sys.executable, args=[str(ROOT / "chatBot" / "mcp_server.py")], env=env)) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.assertIn("napcat_call", [tool.name for tool in listing.tools])
                    result = await session.call_tool("napcat_call", {"action": "get_login_info", "params": {}})
                    payload = json.loads(result.content[0].text)
                    self.assertEqual(payload["status"], "ok")
        asyncio.run(check())
        from unittest.mock import patch
        import mcp_runtime
        servers = {"napcat": {
            "command": sys.executable, "args": [str(ROOT / "chatBot" / "mcp_server.py")],
            "env": {"BINES_QQ_GATEWAY_PORT": str(self.server.server_port), "BINES_QQ_GATEWAY_TOKEN": "test-secret"},
        }}
        with patch.object(mcp_runtime, "configurations", return_value=servers):
            tools = mcp_runtime.run("napcat")
            self.assertIn("napcat_call", [tool["name"] for tool in tools])
            result = mcp_runtime.run("napcat", "napcat_call", {"action": "get_login_info", "params": {}})
            self.assertEqual(json.loads(result["content"][0]["text"])["status"], "ok")

    def test_scoped_state_version_and_legacy_migration(self):
        a = self.store.identity(event("2", group="10"))
        b = self.store.identity(event("3", group="10"))
        self.store.update_state(a, {"important_thing": "用户2"})
        self.assertNotIn("important_thing", self.store.state(b))
        with self.assertRaises(ValueError):
            self.store.update_state(a, {"important_thing": "过时"}, expected_version=0)
        from chatBot.migrate_memory import migrate
        path = Path(self.temp.name)
        short = path / "memory_short.json"
        short.write_text(json.dumps([{"role": "user", "content": "旧记忆"}]))
        original = short.read_bytes()
        self.assertEqual(migrate(path, self.store), 1)
        self.assertEqual(migrate(path, self.store), 0)
        self.assertEqual(short.read_bytes(), original)
        self.assertIn("旧记忆", str(self.store.context(LOCAL_IDENTITY)))

    def test_crash_after_send_does_not_rerun_model(self):
        ev = event("2")
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        turn = rows[0]["batch"]
        params = {"user_id": "2", "message": "persisted reply"}
        self.store.begin_turn(turn)
        self.store.save_turn(turn, "ready", {"action": "send_private_msg", "params": params})
        context = ToolContext(self.store.identity(ev), rows[0]["id"], turn)
        with tool_context(context):
            self.client.raw_api("send_private_msg", params, operation_id=turn+":reply")
        class Model:
            def run_skill_protocol(*args, **kwargs):
                raise AssertionError("恢复时不应重新调用模型")
        bot = QQThinking(self.store, self.client)
        bot.process(rows, Model())
        self.assertEqual(sum(action == "send_private_msg" for action, _ in self.api.calls), 1)
        self.assertIn("persisted reply", str(self.store.context(self.store.identity(ev))))

    def test_interrupted_started_turn_without_writes_reruns_model(self):
        ev = event("2")
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        turn = rows[0]["batch"]
        # A process died after starting the turn but before any Skill write.
        self.store.begin_turn(turn)
        class Model:
            called = False
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                inner.called = True
                return "重试后的回复", [], []
        model = Model()
        QQThinking(self.store, self.client).process(rows, model)
        self.assertTrue(model.called)
        self.assertIn("重试后的回复", str(self.store.context(self.store.identity(ev))))

    def test_interrupted_turn_with_write_receipt_does_not_rerun_model(self):
        ev = event("2")
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        turn = rows[0]["batch"]
        params = {"user_id": "2", "message": [{"type": "text", "data": {"text": "半条回复"}}]}
        self.store.begin_turn(turn)
        self.store.begin_operation(f"{turn}:reply:0", "send_private_msg", params)
        class Model:
            def run_skill_protocol(*args, **kwargs):
                raise AssertionError("已有写回执时不应重新调用模型")
        bot = QQThinking(self.store, self.client)
        with self.assertRaises(RuntimeError):
            bot.process(rows, Model())
        self.assertEqual(self.store.turn_writes(turn), 1)

    def test_actual_light_launcher(self):
        env = dict(os.environ)
        # Keep the real installation's .env (including explicit empty secrets)
        # out of this subprocess; injected fixture values are local to the test.
        isolated_env = Path(self.temp.name) / "launcher.env"
        isolated_env.write_text("# isolated launcher verification\n")
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            gateway_port = listener.getsockname()[1]
        env.update({
            "BINES_ENV_FILE": str(isolated_env),
            "BINES_MEMORY_DIR": str(Path(self.temp.name) / "launcher"),
            "BINES_QQ_GATEWAY_PORT": str(gateway_port),
            "BINES_NAPCAT_WS_URL": "ws://127.0.0.1:1",
            "BINES_NAPCAT_DEPLOY": "external",
            "DEEPSEEK_API_KEY": "verification-only",
        })
        process = subprocess.Popen([sys.executable, "-u", str(ROOT / "start_modules.py"),
                                    "--profile", "chatbot", "--external-napcat"],
                                   cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines, ready = [], threading.Event()
        def read():
            for line in process.stdout:
                lines.append(line)
                if ("ChatBot · 已就绪" in "".join(lines)
                        and "QQ Thinking · 已就绪" in "".join(lines)
                        and "Memory Worker · 已就绪" in "".join(lines)):
                    ready.set()
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            self.assertTrue(ready.wait(30), "".join(lines))
        finally:
            import signal
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
            thread.join(timeout=1)
            process.stdout.close()
        self.assertEqual(process.returncode, 0, "".join(lines))

    def test_disconnect_inflight_no_replay_and_single_reconnect(self):
        count = []
        class Socket:
            def __init__(inner, url, **callbacks):
                inner.callbacks = callbacks
                inner.stop = threading.Event()
                count.append(inner)
            def run_forever(inner, **kwargs):
                inner.callbacks["on_open"](inner)
                inner.stop.wait()
            def close(inner):
                inner.stop.set()
            def send(inner, payload):
                inner.close()
        client = NapCatClient(factory=Socket)
        client.connect()
        self.assertTrue(client._connected.wait(1))
        try:
            result = client.raw_api("send_private_msg", {}, timeout=2)
            self.assertEqual(result["status"], "uncertain")
            deadline = time.monotonic()+3
            while len(count) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
                client.connect()
            self.assertEqual(len(count), 2)
            self.assertEqual(len(client._pending), 0)
        finally:
            client.disconnect()

    def test_docker_init_preserves_tokens_and_isolation(self):
        from unittest.mock import patch
        import napcat
        path = Path(self.temp.name)
        with patch.object(napcat, "ROOT", path), patch.object(napcat, "DEPLOY", path / "deploy"), patch("builtins.print"):
            napcat.initialize()
            token_bytes = (path / "deploy" / ".env").read_bytes()
            settings = json.loads((path / "deploy" / "data" / "config" / "onebot11.json").read_text())
            self.assertEqual(settings["network"]["websocketServers"][0]["port"], 3001)
            napcat.initialize()
            self.assertEqual((path / "deploy" / ".env").read_bytes(), token_bytes)
            self.assertNotEqual((path / ".env").stat().st_mode & 0o777, 0o644)
        import yaml
        compose = yaml.safe_load((ROOT / "deploy" / "napcat" / "compose.yaml").read_text())
        service = compose["services"]["napcat"]
        self.assertTrue(all(port.startswith("127.0.0.1:") for port in service["ports"]))
        self.assertNotIn("privileged", service)
        self.assertNotIn("network_mode", service)
        self.assertFalse(any("docker.sock" in volume for volume in service["volumes"]))

    def test_private_group_reply_and_mixed_message_single_send(self):
        from unittest.mock import patch
        from tools.napcat_tool import napcat_send
        import base64
        image = "base64://"+base64.b64encode(b"GIF89a").decode()
        for group in (None, "10"):
            with self.subTest(group=group):
                ev = event("2", group=group, msg_id=21)
                event_id = self.insert(ev)
                identity = self.store.identity(ev)
                chain = [{"type": "text", "data": {"text": "图片和说明"}},
                         {"type": "image", "data": {"file": image}},
                         {"type": "text", "data": {"text": "放在同一条消息"}}]
                before = len(self.api.calls)
                with tool_context(ToolContext(identity, event_id, "mixed:"+str(group))):
                    with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
                        result = napcat_send(chain, reply_to="21")
                self.assertEqual(result["status"], "ok")
                self.assertEqual(len(self.api.calls), before+1)
                action, payload = self.api.calls[-1]
                self.assertEqual(action, "send_group_msg" if group else "send_private_msg")
                self.assertEqual(payload["message"], [{"type": "reply", "data": {"id": "21"}}, *chain])
                self.assertEqual(len(self.store.context(identity, include_ids=True)), 1)
                self.assertIn("message_id=21", str(self.store.context(identity, include_ids=True)))

    def test_custom_image_sticker_and_market_face(self):
        for group in (None, "10"):
            ev = event("2", group=group, msg_id=30, message=[
                {"type": "image", "data": {"file": "observed-image-token", "url": "https://qq.example/sticker.gif"}}])
            event_id = self.insert(ev)
            action = "send_group_msg" if group else "send_private_msg"
            target = {"group_id": group} if group else {"user_id": "2"}
            sticker = {"type": "image", "data": {
                "file": "https://qq.example/sticker.gif", "sub_type": 1, "summary": "[自定义表情]"}}
            market_face = {"type": "mface", "data": {
                "emoji_package_id": 1, "emoji_id": "test-emoji", "key": "test-key", "summary": "[商城表情]"}}
            with tool_context(ToolContext(self.store.identity(ev), event_id, "sticker:"+str(group))):
                for message in (sticker, [market_face], [sticker, {"type": "text", "data": {"text": "好耶"}}]):
                    self.assertEqual(self.client.raw_api(action, {**target, "message": message})["status"], "ok")
                    self.assertEqual(self.api.calls[-1][1]["message"], message if isinstance(message, list) else [message])

    def test_owner_group_can_list_and_send_account_custom_face(self):
        from unittest.mock import patch
        from tools.napcat_tool import napcat_custom_faces, napcat_send_custom_face
        ev = event("1", group="10", msg_id=31)
        event_id = self.insert(ev)
        identity = self.store.identity(ev)
        self.assertTrue(identity.owner)
        with tool_context(ToolContext(identity, event_id, "custom-face")):
            with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
                rows = napcat_custom_faces(3)
                result = napcat_send_custom_face(1)
        self.assertEqual([row["index"] for row in rows], [0, 1, 2])
        self.assertEqual(result["status"], "ok")
        action, params = self.api.calls[-1]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(params["message"][0]["data"]["file"], self.api.custom_faces[1]["url"])
        self.assertEqual(params["message"][0]["data"]["sub_type"], 1)

        stranger = event("2", group="10", msg_id=32)
        stranger_id = self.insert(stranger)
        with tool_context(ToolContext(self.store.identity(stranger), stranger_id, "no-custom-face")):
            with self.assertRaises(PermissionError):
                napcat_custom_faces()

    def test_stale_observed_image_url_is_not_reused(self):
        ev = event("2", group="10", msg_id=33, time=self.service.started_at - 60, message=[
            {"type": "image", "data": {
                "file": "old-image", "url": "https://qq.example/expired.gif",
            }},
        ])
        self.insert(ev)
        identity = self.store.identity(ev)
        with self.assertRaises(PermissionError):
            self.service.call(identity, "send_group_msg", {
                "group_id": "10",
                "message": {"type": "image", "data": {
                    "file": "https://qq.example/expired.gif", "sub_type": 1,
                }},
            }, "stale-image")

    def test_forward_selection_order_and_current_scope(self):
        from unittest.mock import patch
        from tools.napcat_tool import napcat_forward
        for group in (None, "10"):
            for message_id in (41, 42, 43):
                ev = event("2", group=group, msg_id=message_id)
                event_id = self.insert(ev)
            with tool_context(ToolContext(self.store.identity(ev), event_id, "forward:"+str(group))):
                with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
                    result = napcat_forward([43, 41, 42])
            action, params = self.api.calls[-1]
            self.assertEqual(action, "send_group_forward_msg" if group else "send_private_forward_msg")
            self.assertEqual([part["data"]["id"] for part in params["messages"]], ["43", "41", "42"])
            self.assertIn("message_id", result["data"])
        identity = self.store.identity(event("2", group="10"))
        self.insert(event("3", group="20", msg_id=50))
        with self.assertRaises(PermissionError):
            self.service.call(identity, "send_group_forward_msg", {
                "group_id": "10", "messages": [{"type": "node", "data": {"id": "50"}}]}, "foreign-node")
        self.assertEqual(self.service.call(LOCAL_IDENTITY, "send_group_forward_msg", {
            "group_id": "10", "messages": [{"type": "node", "data": {"id": "50"}}]}, "owner-forward")["status"], "ok")
        # Both native aliases are supported and inspected.
        for field in ("message", "messages"):
            self.assertEqual(self.service.call(identity, "send_forward_msg", {
                "group_id": "10", field: [{"type": "node", "data": {"id": "41"}}]}, "alias:"+field)["status"], "ok")

    def test_rich_message_permission_bypasses_rejected(self):
        ev = event("2", group="10", msg_id=60)
        self.insert(ev)
        identity = self.store.identity(ev)
        bad = [
            {"type": "image", "data": {"file": "/app/napcat/config/webui.json"}},
            {"type": "image", "data": {"file": "base64://R0lG", "url": "file:///private"}},
            {"type": "image", "data": {"file": "/bines-media/public/../secret.png"}},
            {"type": "image", "data": {"file": "https://arbitrary.example/private.png"}},
            {"type": "reply", "data": {"id": "%"}},
            {"type": "reply", "data": {"id": "60", "seq": 123}},
            {"type": "node", "data": {"id": "60", "content": [{"type": "image", "data": {"file": "/secret"}}]}},
        ]
        for index, message in enumerate(bad):
            with self.subTest(message=message):
                with self.assertRaises((PermissionError, ValueError)):
                    self.service.call(identity, "send_group_msg", {"group_id": "10", "message": message}, f"bad:{index}")
        with self.assertRaises(PermissionError):
            self.service.call(identity, "send_group_msg", {
                "group_id": "10", "user_id": "2", "message_type": "private",
                "message": "private pivot"}, "bad-target")
        with self.assertRaises(ValueError):
            self.service.call(identity, "send_group_msg", {"group_id": "10", "message": [
                {"type": "node", "data": {"id": "60"}}, {"type": "text", "data": {"text": "mixed"}}]}, "mixed-node")
        self.assertFalse(self.api.calls)

    def test_forward_receipt_suppresses_duplicate_reply(self):
        from unittest.mock import patch
        from tools.napcat_tool import napcat_forward
        ev = event("2", msg_id=70)
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                self.assertIn("napcat_forward", runner.actions_for_source("QQ"))
                napcat_forward([70])
                return "已合并转发", [], []
        bot = QQThinking(self.store, self.client)
        with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            bot.process(rows, Model())
        sends = [call for call in self.api.calls if call[0].startswith("send_")]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0][0], "send_private_forward_msg")
        context = self.store.context(self.store.identity(ev))
        self.assertEqual(len([message for message in context if message["role"] == "assistant"]), 1)
        self.assertIn("node", context[-1]["content"])

    def test_uncertain_forward_not_saved_or_followed_by_text(self):
        from unittest.mock import patch
        from tools.napcat_tool import napcat_forward
        ev = event("2", msg_id=80)
        self.insert(ev)
        rows = self.store.claim("worker", 0, 0)
        self.api.send_status = "uncertain"
        class Model:
            def run_skill_protocol(inner, *args, **kwargs):
                napcat_forward([80])
                return "再发一遍", [], []
        with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            with self.assertRaises(RuntimeError):
                QQThinking(self.store, self.client).process(rows, Model())
        self.assertEqual(sum(call[0].startswith("send_") for call in self.api.calls), 1)
        self.assertFalse(any(message["role"] == "assistant"
                             for message in self.store.context(self.store.identity(ev))))


class DockerLifecycle(unittest.TestCase):
    def test_source_hash_ignores_timestamps_and_runtime_data(self):
        import docker_launcher as docker
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deploy = root / "deploy" / "docker"
            deploy.mkdir(parents=True)
            for name in ("Dockerfile.launcher", "Dockerfile.thinking"):
                (deploy / name).write_text("FROM python:3.12-slim\n")
            source = root / "bines.py"
            source.write_text("print('你好')\n")
            api = root / "chatBot" / "api" / "catalog.json"
            api.parent.mkdir(parents=True)
            api.write_text('{"version": 1}')
            with patch.object(docker, "ROOT", root), patch.object(docker, "COMPOSE_FILE", deploy / "compose.yaml"):
                expected = docker.source_hashes()
                os.utime(source, (time.time() + 86400, time.time() + 86400))
                (root / "presence_state.json").write_text('{"active": true}')
                self.assertEqual(docker.source_hashes(), expected)
                api.write_text('{"version": 2}')
                updated = docker.source_hashes()
                self.assertNotEqual(updated["chatbot"], expected["chatbot"])
                self.assertEqual(updated["memory-server"], expected["memory-server"])
                expected = updated
                (root / "chatBot" / "gateway.py").write_text("# 网关更新\n")
                updated = docker.source_hashes()
                self.assertNotEqual(updated["chatbot"], expected["chatbot"])
                self.assertEqual(updated["memory-server"], expected["memory-server"])
                expected = updated
                (root / "chatBot" / "store.py").write_text("# 共享数据库迁移更新\n")
                updated = docker.source_hashes()
                self.assertNotEqual(updated["chatbot"], expected["chatbot"])
                self.assertNotEqual(updated["memory-server"], expected["memory-server"])
                expected = updated
                (deploy / "Dockerfile.thinking").write_text("FROM python:3.12-slim\n# 更新\n")
                updated = docker.source_hashes()
                self.assertEqual(updated["chatbot"], expected["chatbot"])
                self.assertNotEqual(updated["memory-server"], expected["memory-server"])

    def test_unchanged_start_does_not_build(self):
        import docker_launcher as docker
        from unittest.mock import patch
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(docker, "_stale_services", return_value=[]), \
                patch.object(docker, "run", return_value=ok) as run, \
                patch.object(docker, "ensure_builder") as builder, \
                patch.object(docker, "cleanup_images"):
            docker.up("chatbot")
            run.assert_called_once_with("chatbot", "up", "-d", "--no-build")
            builder.assert_not_called()

    def test_down_preserves_existing_service_but_removes_new_services(self):
        import docker_launcher as docker
        from unittest.mock import patch
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(docker, "run", return_value=ok) as run, \
                patch.object(docker, "cleanup_containers"), \
                patch.object(docker, "cleanup_images"), \
                patch.object(docker, "cleanup_build_cache"):
            docker.down(previously_running={"napcat"})
            self.assertEqual([call.args[:2] for call in run.call_args_list], [("full", "stop"), ("full", "rm")])
            for call in run.call_args_list:
                self.assertNotIn("napcat", call.args)
                self.assertIn("chatbot", call.args)
                self.assertIn("memory-server", call.args)

    def test_cleanup_is_scoped_to_bines_and_keeps_tagged_images_and_volumes(self):
        import docker_launcher as docker
        from unittest.mock import patch
        ok = subprocess.CompletedProcess([], 0, "Driver: docker-container\n", "")
        with patch.object(docker, "_docker", return_value=ok) as command:
            docker.cleanup_containers()
            docker.cleanup_images()
            docker.cleanup_build_cache()
            calls = [call.args for call in command.call_args_list]
            self.assertIn(("container", "prune", "--force", "--filter", "label=com.docker.compose.project=bines"), calls)
            self.assertIn(("image", "prune", "--force", "--filter", "label=com.docker.compose.project=bines"), calls)
            self.assertIn(("buildx", "rm", "--force", "bines"), calls)
            self.assertFalse(any("volume" in args or "system" in args for args in calls))

    def test_failed_build_releases_its_cache(self):
        import docker_launcher as docker
        from unittest.mock import patch
        failed = subprocess.CompletedProcess([], 1, "", "构建失败")
        with patch.object(docker, "_stale_services", return_value=["chatbot"]), \
                patch.object(docker, "ensure_builder"), \
                patch.object(docker, "run", return_value=failed), \
                patch.object(docker, "cleanup_build_cache") as cleanup:
            with self.assertRaises(docker.DockerError):
                docker.up("chatbot")
            cleanup.assert_called_once()

    def test_startup_failure_and_interrupt_still_clean_new_containers(self):
        import docker_launcher as docker
        import start_modules
        from types import SimpleNamespace
        from unittest.mock import patch
        for failure, code in ((RuntimeError("等待超时"), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(failure=type(failure).__name__), \
                    patch.dict(os.environ), \
                    patch.object(docker, "ensure_engine"), \
                    patch.object(docker, "running_services", return_value={"napcat"}), \
                    patch.object(docker, "up"), \
                    patch.object(docker, "wait_ready", side_effect=failure), \
                    patch.object(docker, "down") as down:
                self.assertEqual(start_modules._run_docker(SimpleNamespace(profile="chatbot")), code)
                down.assert_called_once_with(previously_running={"napcat"})

    def test_windows_docker_process_isolated_from_console_interrupt(self):
        import docker_launcher as docker
        from unittest.mock import patch
        with patch.object(os, "name", "nt"), patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True):
            self.assertEqual(docker._process_options(), {"creationflags": 512})

    def test_ps_handles_json_array_and_refuses_failed_status_lookup(self):
        import docker_launcher as docker
        from unittest.mock import patch
        row = {"Service": "napcat", "State": "running"}
        with patch.object(docker, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps([row]), "")):
            self.assertEqual(docker.running_services("full"), {"napcat"})
        with patch.object(docker, "run", return_value=subprocess.CompletedProcess([], 1, "", "连接失败")):
            with self.assertRaises(docker.DockerError):
                docker.running_services("full")


if __name__ == "__main__":
    unittest.main()
