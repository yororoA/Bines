"""Opt-in real-model/CFT validation, isolated from production accounts and memory."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "thingking" / "src")]
from chatBot.proactive import QQProactive
from common.proactive import ProactiveConfig
from common.website import AgentBrowser, PlanStore, WebsiteService, platform_config
from thingking.qq_thinking import QQThinking
from thinking_model_helper import ThinkingModelHelper
from verification.napcat_integration import Integration, event
from website_bridge import BridgeServer, BridgeHandler
from website_runtime import installed_chrome_for_testing


class ValidationBrowser(AgentBrowser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = "bv-" + uuid.uuid4().hex[:6]

    def _argv(self, platform, args):
        argv = super()._argv(platform, args)
        argv[argv.index("--session") + 1] = self.session + "-" + platform.key
        return argv

    def close(self):
        for key in ("bilibili", "douyin", "xiaoheihe", "qzone"):
            if (self.profile_root / key).exists():
                try:
                    self.run(platform_config(key), ["close"], timeout=10)
                except Exception:
                    pass


class ObservationService(WebsiteService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.actions = []

    def open(self, platform, url="", snapshot=True):
        result = super().open(platform, url, snapshot)
        self.actions.append({"action": "open", "url": result["page_state"].get("url")})
        return result

    def scroll(self, *args, **kwargs):
        result = super().scroll(*args, **kwargs)
        self.actions.append({"action": "scroll", "before": result["page_state_before"].get("scroll_y"),
                             "after": result["page_state"].get("scroll_y")})
        return result

    def interact(self, platform, action, ref="", value="", operation_id="", contact_allowed=True):
        result = super().interact(platform, action, ref, value, operation_id, contact_allowed=False)
        self.actions.append({"action": action, "status": result["status"],
                             "url": result.get("url")})
        return result

    def execute(self, *args, **kwargs):
        raise PermissionError("Live validation has no public write authority")


def emit(kind, payload):
    print(kind, json.dumps(payload, ensure_ascii=False), flush=True)


def main():
    logging.basicConfig(level=logging.WARNING)
    os.environ["BINES_LIFE_SLEEP_START"] = "0"
    os.environ["BINES_LIFE_WAKE_HOUR"] = "0"
    fixture = Integration()
    fixture.setUp()
    root = Path(fixture.temp.name)
    browser = ValidationBrowser(
        [str(ROOT / ".tools/agent-browser/node_modules/.bin/agent-browser")],
        profile_root=root / "browser", executable_path=installed_chrome_for_testing(), headed=True,
    )
    service = ObservationService(PlanStore(root / "website.db"), browser, root=root)
    token = uuid.uuid4().hex
    server = BridgeServer(("127.0.0.1", 0), BridgeHandler, token=token, service=service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch.dict(os.environ, {
            "BINES_WEBSITE_BRIDGE_URL": f"http://127.0.0.1:{server.server_port}",
            "BINES_WEBSITE_BRIDGE_TOKEN": token, "BINES_MEMORY_DIR": str(root / "control"),
            "AGENT_BROWSER_SOCKET_DIR": str(root / "sockets"),
            "BINES_LIFE_SLEEP_START": "0", "BINES_LIFE_WAKE_HOUR": "0",
        }):
            os.environ.pop("BINES_WEBSITE_BRIDGE_SPOOL", None)
            original = event("1", msg_id=8890, text=(
                "你可以打开B站随意逛逛。看什么、换什么方向、什么时候看够了，都按你自己的兴趣来。"
                "可以一直自主地逛下去，也可以休息或结束，不用等我每次叫你继续。"
                "这次只浏览、搜索和看视频，不发帖评论，不操作账号，也不用给我发消息。"
            ))
            fixture.insert(original)
            bot = QQThinking(fixture.store, fixture.client)
            model = ThinkingModelHelper()
            rows = fixture.store.claim("live-test", 0, 0)
            bot.process(rows, model)
            fixture.store.finish([row["id"] for row in rows], "live-test")
            fixture.memory_hub.sync()
            conversation = fixture.store.identity(original).conversation
            emit("owner_handoff", fixture.store.current_activity(conversation))
            scheduler = QQProactive(
                fixture.store, bot.memory, fixture.client,
                config=ProactiveConfig(quiet_start=0, quiet_end=0),
            )
            started, batches, decisions = time.monotonic(), 0, []
            # Bound the validation harness, never the agent's chosen activity.
            while time.monotonic() - started < 300 and batches < 4:
                scheduler.tick()
                rows = fixture.store.claim("live-test", 0, 0)
                if not rows:
                    policy = scheduler.policies.get(conversation)
                    if policy and not policy.state.activity and batches:
                        break
                    if policy and policy.state.activity.get("mode") == "pause":
                        if policy.state.activity.get("delay_seconds", 0) > 60:
                            emit("chosen_wait", policy.state.activity)
                            break
                    threading.Event().wait(0.5)
                    continue
                bot.process(rows, model)
                fixture.store.finish([row["id"] for row in rows], "live-test")
                batches += 1
                with fixture.store.connection() as db:
                    turn = db.execute("SELECT payload FROM turns WHERE id=?", (rows[0]["id"],)).fetchone()
                    checkpoint = json.loads(turn["payload"]).get("activity") if turn else None
                emit("background_choice", checkpoint)
                if checkpoint and checkpoint.get("decision"):
                    decisions.append(checkpoint["decision"])
                scheduler.tick()
                scheduler = QQProactive(
                    fixture.store, bot.memory, fixture.client,
                    config=ProactiveConfig(quiet_start=0, quiet_end=0),
                )
                if checkpoint and checkpoint.get("mode") in {"finish", "pause"}:
                    break
            with fixture.store.connection() as db:
                humans = db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0]
            assert humans == 1, humans
            assert service.actions, "No actual browser action"
            emit("autonomy_evidence", {
                "background_batches": batches, "model_decisions": decisions,
                "actions": service.actions, "human_messages": humans,
                "real_outbound_messages": 0, "scheduler_reload": True,
                "activity": fixture.store.current_activity(conversation),
            })
            # Independently verify actual video pixels + the real shared vision endpoint.
            service.open("bilibili", "https://www.bilibili.com/video/BV1GJ411x7h7/")
            from common.website_watch import observe
            state = observe(service, platform_config("bilibili"))
            if (state.get("video") or {}).get("paused"):
                tree = service.snapshot("bilibili")["snapshot"]
                import re
                line = next((line for line in tree.splitlines() if
                             re.search(r'button.*(?:播放|Play).*ref=e\d+', line)), "")
                match = re.search(r"ref=(e\d+)", line)
                if match:
                    service.interact("bilibili", "click", "@" + match.group(1))
            video = service.watch("bilibili", "这几秒画面实际展示了什么？", duration_seconds=6, frames=3)
            emit("video_evidence", video)
            assert video["status"] == "ok", video
            assert video["audio_observed"] is False
            assert len(video["frames"]) == 3 and video["analysis"]
            assert video["playback_advanced"], "Need a genuinely playing video"
            emit("PASS", {"video_frames": 3, "real_vision": True, "real_browser": True,
                          "human_messages": humans, "background_batches": batches})
    finally:
        server.shutdown()
        server.server_close()
        with patch.dict(os.environ, {"AGENT_BROWSER_SOCKET_DIR": str(root / "sockets")}):
            browser.close()
        fixture.tearDown()


if __name__ == "__main__":
    main()
