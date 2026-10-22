"""Verify complete model/API/MCP exposure with a simulated NapCat, never live QQ."""
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]

from chatBot.catalog import bundled_document, reference_catalog, action_tool_specs, mcp_action_name
from chatBot.capabilities import SchemaCatalog, CapabilityService, OWNER_GROUP_READS, SCOPED
from chatBot.gateway import GatewayServer
from chatBot.gateway_client import GatewayClient
from chatBot.messages import LOCAL_IDENTITY, Identity, FORWARD_ACTIONS, SEND_ACTIONS
from chatBot.store import MemoryStore
from skill_runner import SkillRunner, QQ_ALLOWED_ACTIONS
from tools import TOOLS_REGISTRY
from tools.context import ToolContext, tool_context


class SimulatedNapCat:
    is_connected = True

    def __init__(self):
        self.calls = []

    def raw_api(self, action, params, timeout=30):
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"action": action}}


class APIExposure(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = SchemaCatalog(token="")
        self.store = MemoryStore(Path(self.temp.name) / "memory.db", owners={"1"})
        self.napcat = SimulatedNapCat()
        self.server = GatewayServer(
            CapabilityService(self.napcat, self.store, self.catalog), ("127.0.0.1", 0), "exposure-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="exposure-token")
        self.runner = SkillRunner(
            TOOLS_REGISTRY, skills_dir=ROOT / "skills",
            enabled_actions_path=ROOT / "server" / "skill_actions.json",
            qq_allowed_actions=QQ_ALLOWED_ACTIONS)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_every_documented_action_reaches_bines_and_gateway(self):
        manifest, document = bundled_document()
        self.assertEqual(manifest["actions"], 178)
        expected = {path.removeprefix("/") for path in document["paths"]}
        instructions = self.runner.instructions_for_source()
        discovered = self.client.discover()
        self.assertEqual({row["action"] for row in discovered}, expected)
        self.assertTrue(all(row["runtime_supported"] is None for row in discovered))
        with patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            for index, action in enumerate(sorted(expected)):
                with self.subTest(action=action):
                    self.assertIn(f"- {action}:", instructions)
                    details = self.client.discover(action=action)
                    self.assertIn("payload", details)
                    self.assertIn("response", details)
                    self.assertEqual(details["schema_source"], "NapCatDocs")
                    params = copy.deepcopy(details.get("payloadExample") or {})
                    if action in SEND_ACTIONS:
                        params.pop("messages", None)
                        params["message"] = (
                            [{"type": "node", "data": {"id": "1"}}] if action in FORWARD_ACTIONS else "test")
                    reply = self.runner.execute({
                        "skill": "napcat", "action": "napcat_call",
                        "args": {"action": action, "params": params, "operation_id": f"coverage:{index}"},
                    })
                    self.assertNotIn("技能执行失败", reply)
                    self.assertEqual(self.napcat.calls[-1][0], action)
        self.assertEqual(len(self.napcat.calls), 178)
        status = self.client.request("/catalog", {})
        self.assertEqual(status["allowed_actions"], 178)
        self.assertFalse(status["runtime_verified"])

    def test_owner_model_has_full_index_and_remote_permissions_remain_scoped(self):
        expected = set(reference_catalog())
        owner = Identity("99", "1", owner=True)
        with tool_context(ToolContext(owner, "trusted-test-event", "owner")):
            instructions = self.runner.instructions_for_source("QQ")
            self.assertIn("napcat_catalog", self.runner.actions_for_source("QQ"))
            for action in expected:
                self.assertIn(f"- {action}:", instructions)
        stranger_group = Identity("99", "2", "10")
        with tool_context(ToolContext(stranger_group, "trusted-test-event", "remote")):
            instructions = self.runner.instructions_for_source("QQ")
            self.assertIn(f"- send_group_msg:", instructions)
            self.assertNotIn("Action `napcat_send_custom_face`", instructions)
            self.assertNotIn("- get_credentials:", instructions)
        discovered = self.catalog.discover(stranger_group)
        self.assertEqual({row["action"] for row in discovered}, expected & SCOPED)

        owner_group = Identity("99", "1", "10", owner=True)
        with tool_context(ToolContext(owner_group, "trusted-owner-group", "owner-group")):
            instructions = self.runner.instructions_for_source("QQ")
            self.assertIn("napcat_send_custom_face", instructions)
            self.assertIn("- get_credentials:", instructions)
        discovered = self.catalog.discover(owner_group)
        self.assertEqual(
            {row["action"] for row in discovered},
            expected,
        )
        self.assertEqual(
            self.server.service.call(owner_group, "fetch_custom_face", {"count": 20})["status"],
            "ok",
        )

        for identity in (Identity("99", "2"),):
            with tool_context(ToolContext(identity, "trusted-test-event", "remote")):
                instructions = self.runner.instructions_for_source("QQ")
                self.assertIn(f"- send_group_msg:", instructions)
                self.assertNotIn("- get_credentials:", instructions)
            discovered = self.catalog.discover(identity)
            self.assertEqual({row["action"] for row in discovered}, expected & SCOPED)
            self.assertFalse(self.catalog.status(identity)["all_actions_allowed"])
            with self.assertRaises(PermissionError):
                self.server.service.call(identity, "get_credentials", {})
        self.assertEqual(self.napcat.calls, [("fetch_custom_face", {"count": 20})])

    def test_deepseek_dsml_fallback_is_parsed_as_a_skill_call(self):
        text = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="napcat_discover">
<｜｜DSML｜｜ parameter name="query" string="true">自定义表情 表情列表 custom face emoji</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""
        self.assertEqual(self.runner.parse_model_command(text), {
            "skill": "napcat",
            "action": "napcat_discover",
            "args": {"query": "自定义表情 表情列表 custom face emoji"},
        })

    def test_runtime_catalog_additions_and_missing_actions_reported(self):
        runtime = {
            "send_group_msg": {
                "description": "Runtime group send",
                "payload": {"type": "object", "properties": {
                    "group_id": {"type": "string"}, "message": {"$ref": "OB11MessageMixType"}},
                    "required": ["group_id", "message"]},
            },
            "new_runtime_api": {"description": "Extension without a schema"},
        }
        catalog = SchemaCatalog(token="test-webui-token")
        catalog._request = lambda path, payload=None, credential=None: (
            {"Credential": "test-credential"} if path.endswith("/login") else runtime)
        status = catalog.status(LOCAL_IDENTITY)
        self.assertTrue(status["runtime_verified"])
        self.assertEqual(status["catalog_actions"], 179)
        self.assertEqual(status["runtime_actions"], 2)
        self.assertEqual(status["runtime_only_actions"], ["new_runtime_api"])
        self.assertIn("get_credentials", status["missing_in_runtime"])
        self.assertTrue(catalog.discover(LOCAL_IDENTITY, action="send_group_msg")["runtime_supported"])
        self.assertFalse(catalog.discover(LOCAL_IDENTITY, action="get_credentials")["runtime_supported"])
        self.assertEqual(catalog.discover(LOCAL_IDENTITY, action="send_group_msg")["schema_source"], "NapCat WebUI")
        warning = catalog.discover(LOCAL_IDENTITY, action="new_runtime_api")["payload"]["x-napcat-schema-warning"]
        self.assertIn("no schema", warning)

    def test_webui_failure_keeps_full_catalog_and_explicit_notice(self):
        catalog = SchemaCatalog(token="test-webui-token")
        def fail(*args, **kwargs):
            raise RuntimeError("simulated login failure")
        catalog._request = fail
        self.assertEqual(len(catalog.discover(LOCAL_IDENTITY)), 178)
        status = catalog.status(LOCAL_IDENTITY)
        self.assertFalse(status["runtime_verified"])
        self.assertIn("查询失败", status["notice"])

    def test_individual_mcp_tools_have_resolved_schemas_and_original_action_mapping(self):
        import jsonschema
        specs = action_tool_specs(reference_catalog())
        self.assertEqual(len(specs), 178)
        names = {spec["name"] for spec in specs}
        self.assertEqual(len(names), 178)
        for spec in specs:
            self.assertRegex(spec["name"], r"^[A-Za-z0-9_-]{1,128}$")
            schema = spec["inputSchema"]
            jsonschema.Draft202012Validator.check_schema(schema)
            # Resolve every reference, not only those exercised by a trivial example.
            def check_references(value):
                if isinstance(value, dict):
                    if "$ref" in value:
                        self.assertTrue(value["$ref"].startswith("#/$defs/"))
                        self.assertIn(value["$ref"].removeprefix("#/$defs/"), schema.get("$defs", {}))
                    for item in value.values():
                        check_references(item)
                elif isinstance(value, list):
                    for item in value:
                        check_references(item)
            check_references(schema)
            if spec["_meta"]["napcat_action"] == "send_group_msg":
                jsonschema.Draft202012Validator(schema).validate({
                    "params": {"group_id": "1", "message": [{"type": "text", "data": {"text": "hello"}}]}})
        async def check_protocol():
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            env = dict(os.environ)
            env.update({
                "BINES_ENV_FILE": str(Path(self.temp.name) / "empty.env"),
                "BINES_QQ_GATEWAY_PORT": str(self.server.server_port),
                "BINES_QQ_GATEWAY_TOKEN": "exposure-token",
            })
            async with stdio_client(StdioServerParameters(
                command=sys.executable, args=["-m", "chatBot.mcp_server"], env=env)) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    tools = {tool.name for tool in listing.tools}
                    self.assertTrue(names <= tools)
                    self.assertEqual(len(tools), 182)  # 178 individual tools + 4 general tools.
                    for action in (".ocr_image", ".handle_quick_operation", "_send_group_notice", "get_login_info"):
                        result = await session.call_tool(mcp_action_name(action), {
                            "params": {}, "operation_id": f"mcp-coverage:{action}"})
                        self.assertFalse(result.is_error)
                        self.assertEqual(json.loads(result.content[0].text)["data"]["action"], action)
        asyncio.run(check_protocol())


if __name__ == "__main__":
    unittest.main()
