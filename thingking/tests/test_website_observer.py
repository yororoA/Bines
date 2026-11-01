import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import helpers  # Match the application import path used by the memory test suite.
from chatBot.messages import Identity
from common.website_observer import WebsiteObserver, page_identity
from common.website_perception import latest_observations, with_website_observations
from common.website_spool import atomic_write_json, heartbeat, signed_envelope
import test_website as fixtures
from test_website import FakeBrowser
from tools.context import ToolContext, tool_context


class LoadingTests(unittest.TestCase):
    setUp = fixtures.WebsiteServiceTests.setUp
    tearDown = fixtures.WebsiteServiceTests.tearDown
    service = fixtures.WebsiteServiceTests.service

    def test_empty_async_shell_is_rechecked_before_model_receives_snapshot(self):
        snapshots = 0
        def callback(_platform, args, _timeout):
            nonlocal snapshots
            if args[0] == "snapshot":
                snapshots += 1
                return "(no interactive elements)" if snapshots == 1 else '- link "直播结果" [ref=e1]'
            if args[0] == "eval":
                return json.dumps({"url": "https://search.bilibili.com/live", "document_id": 1,
                                   "ready_state": "complete", "body_text_length": 0 if snapshots == 1 else 200})
            return "https://search.bilibili.com/live"
        with patch("common.website.time.sleep"):
            result = self.service(FakeBrowser(callback)).open("bilibili")
        self.assertIn("直播结果", result["snapshot"])
        self.assertEqual(result["page_state"]["load_checks"], 2)
        self.assertEqual(result["page_state"]["load_status"], "ready")

    def test_persistent_loading_has_bounded_receipt_not_final_empty(self):
        def callback(_platform, args, _timeout):
            return json.dumps({"ready_state": "complete", "body_text_length": 0}) if args[0] == "eval" else "(no interactive elements)"
        service = self.service(FakeBrowser(callback))
        service.load_wait_seconds = 0
        result = service.snapshot("bilibili")
        self.assertEqual(result["page_state"]["load_status"], "loading")
        self.assertIn("不代表", result["page_state"]["loading_note"])

    def test_complete_document_with_text_still_waits_for_delayed_result_controls(self):
        snapshots = 0
        def callback(_platform, args, _timeout):
            nonlocal snapshots
            if args[0] == "snapshot":
                snapshots += 1
                return "(no interactive elements)" if snapshots < 3 else '- link "迟到的结果" [ref=e1]'
            return json.dumps({"ready_state": "complete", "body_text_length": 300})
        with patch("common.website.time.sleep"):
            result = self.service(FakeBrowser(callback)).snapshot("bilibili")
        self.assertEqual(snapshots, 3)
        self.assertIn("迟到的结果", result["snapshot"])

    def test_same_named_ref_on_different_document_is_rejected(self):
        state = {"document_id": 1, "url": "https://www.bilibili.com/"}
        def callback(_platform, args, _timeout):
            return json.dumps(state) if args[0] == "eval" else '- button "播放" [ref=e1]'
        browser = FakeBrowser(callback)
        service = self.service(browser)
        service.snapshot("bilibili")
        state["document_id"] = 2
        with self.assertRaisesRegex(ValueError, "页面已切换"):
            service.interact("bilibili", "click", "@e1")
        self.assertFalse(any(args[0] == "click" for _, args, _ in browser.calls))

    def test_click_reports_new_tab_without_repeating_action(self):
        clicked = False
        def callback(_platform, args, _timeout):
            nonlocal clicked
            if args[0] == "click":
                clicked = True
            if args[0] == "snapshot":
                return '- link "视频" [ref=e1]'
            if args[0] == "eval":
                return json.dumps({"url": "https://www.bilibili.com/"})
            if args == ["tab", "list"]:
                return "[t1] 搜索\n[t2] 视频" if clicked else "[t1] 搜索"
            if args[:2] == ["get", "attr"]:
                return "https://www.bilibili.com/video/BV1"
            return "https://www.bilibili.com/"
        browser = FakeBrowser(callback)
        service = self.service(browser)
        service.snapshot("bilibili")
        result = service.interact("bilibili", "click", "@e1", operation_id="one")
        self.assertEqual(result["click_evidence"]["new_tabs"], ["t2"])
        self.assertTrue(result["click_evidence"]["navigation_observed"])
        service.interact("bilibili", "click", "@e1", operation_id="one")
        self.assertEqual(sum(args[0] == "click" for _, args, _ in browser.calls), 1)

    def test_tracking_query_change_is_not_reported_as_video_navigation(self):
        clicked = False
        def callback(_platform, args, _timeout):
            nonlocal clicked
            if args[0] == "click":
                clicked = True
            if args[0] == "snapshot":
                return '- link "视频" [ref=e1]'
            if args[:2] == ["get", "attr"]:
                return "//www.bilibili.com/video/BV1"
            if args == ["tab", "list"]:
                return "[t1] 搜索"
            url = "https://search.bilibili.com/all?keyword=鱼&vt=" + ("2" if clicked else "1")
            return json.dumps({"url": url}) if args[0] == "eval" else url
        service = self.service(FakeBrowser(callback))
        service.snapshot("bilibili")
        result = service.interact("bilibili", "click", "@e1")
        self.assertFalse(result["click_evidence"]["navigation_observed"])
        self.assertFalse(result["click_evidence"]["target_reached"])
        self.assertIn("不自动重试", result["click_evidence"]["note"])


class ObserverTests(unittest.TestCase):
    setUp = fixtures.WebsiteServiceTests.setUp
    tearDown = fixtures.WebsiteServiceTests.tearDown
    service = fixtures.WebsiteServiceTests.service
    browser = fixtures.WebsiteWatchTests.browser

    def observer(self, browser=None, **kwargs):
        service = self.service(browser or self.browser(no_video=True))
        observer = WebsiteObserver(service, "s" * 64, self.root / "spool", **kwargs)
        observer.activate("bilibili")
        return service, observer

    def test_open_enables_observation_without_watch_call_or_background_snapshot(self):
        service, observer = self.observer()
        with patch("vlm_client.call_vision", return_value="这是搜索页面") as vision:
            result = service.open("bilibili", snapshot=False)
            before = list(service.browser.calls)
            refs = dict(service.observed_refs)
            observer.tick("bilibili")
        self.assertTrue(result["automatic_observation"])
        self.assertEqual(vision.call_count, 1)
        self.assertFalse(any(args[0] == "snapshot" for _, args, _ in service.browser.calls[len(before):]))
        self.assertEqual(service.observed_refs, refs)
        value = observer.latest["bilibili"]["observation"]
        self.assertEqual(value["kind"], "page")
        self.assertNotIn("base64", json.dumps(value))
        self.assertNotIn("token=private", json.dumps(value))

    def test_video_uses_continuous_frames_with_playback_evidence(self):
        service, observer = self.observer(self.browser())
        with patch("vlm_client.call_vision", return_value="海鸟飞过") as vision:
            with patch("common.website_watch.time.sleep"):
                observer.tick("bilibili")
        result = observer.latest["bilibili"]["observation"]
        self.assertEqual(result["kind"], "video")
        self.assertEqual(len(result["frames"]), 3)
        self.assertTrue(result["playback_advanced"])
        self.assertFalse(result["audio_observed"])
        self.assertEqual(len(vision.call_args.args[0]), 3)

    def test_unchanged_page_is_throttled_and_snapshot_does_not_invalidate(self):
        service, observer = self.observer(min_interval=0)
        with patch("vlm_client.call_vision", return_value="暂停页面") as vision:
            observer.tick("bilibili")
            value = observer.latest["bilibili"]["observation"]
            observer.activate("bilibili", invalidate=False)
            observer.tick("bilibili")
        self.assertEqual(vision.call_count, 1)
        self.assertEqual(observer.latest["bilibili"]["observation"]["id"], value["id"])

    def test_navigation_during_inference_discards_old_description(self):
        _, observer = self.observer()
        def infer(*_args, **_kwargs):
            observer.activate("bilibili")
            return "旧页面画面"
        with patch("vlm_client.call_vision", side_effect=infer):
            observer.tick("bilibili")
        self.assertIsNone(observer.latest["bilibili"]["observation"])

    def test_browser_navigation_during_inference_also_discards(self):
        _, observer = self.observer(self.browser(no_video=True, changed_at=3))
        with patch("vlm_client.call_vision", return_value="旧页面画面"):
            observer.tick("bilibili")
        self.assertIsNone(observer.latest["bilibili"]["observation"])

    def test_foreground_can_act_while_vision_request_runs(self):
        service, observer = self.observer()
        acquired = []
        def infer(*_args, **_kwargs):
            def foreground():
                with service.execute_lock:
                    acquired.append(True)
            thread = threading.Thread(target=foreground)
            thread.start()
            thread.join(timeout=1)
            return "页面"
        with patch("vlm_client.call_vision", side_effect=infer):
            observer.tick("bilibili")
        self.assertEqual(acquired, [True])

    def test_stop_drops_inflight_analysis(self):
        _, observer = self.observer()
        def infer(*_args, **_kwargs):
            observer.close()
            return "过时页面"
        with patch("vlm_client.call_vision", side_effect=infer):
            observer.tick("bilibili")
        self.assertIsNone(observer.latest["bilibili"]["observation"])

    def test_shutdown_during_video_inference_does_not_reopen_browser(self):
        service, observer = self.observer(self.browser())
        call_count = []
        def infer(*_args, **_kwargs):
            observer.stop.set()
            call_count.append(len(service.browser.calls))
            return "旧视频"
        with patch("vlm_client.call_vision", side_effect=infer), patch.object(observer.stop, "wait", return_value=False):
            result = service.watch("bilibili", duration_seconds=1, frames=2)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(service.browser.calls), call_count[0])

    def test_two_watch_requests_do_not_start_duplicate_sampling(self):
        service, _ = self.observer()
        with service.watch_lock:
            with patch("vlm_client.call_vision") as vision:
                result = service.watch("bilibili")
        self.assertEqual(result["status"], "observing")
        vision.assert_not_called()

    def test_changed_picture_waits_for_minimum_interval_then_updates(self):
        _, observer = self.observer(min_interval=12)
        state = {"url": "https://www.bilibili.com/", "viewport": {"width": 640, "height": 480}}
        dark = ((False,) * 64, (False,) * 64)
        light = ((True,) * 64, (True,) * 64)
        with patch.object(observer, "_capture", side_effect=[
            (state, dark, "unused"), (state, light, "unused"), (state, light, "unused"),
        ]), patch("common.website_observer.observe", return_value=state):
            with patch("vlm_client.call_vision", return_value="新的画面") as vision:
                observer.tick("bilibili")
                observer.tick("bilibili")
                self.assertEqual(vision.call_count, 1)
                observer.active["bilibili"]["last_analysis"] -= 13
                observer.tick("bilibili")
                self.assertEqual(vision.call_count, 2)

    def test_refresh_after_maximum_interval_and_reload_change_identity(self):
        _, observer = self.observer()
        with patch("vlm_client.call_vision", return_value="页面") as vision:
            observer.tick("bilibili")
            observer.active["bilibili"]["last_analysis"] -= 121
            observer.tick("bilibili")
        self.assertEqual(vision.call_count, 2)
        self.assertNotEqual(page_identity({"url": "https://www.bilibili.com/", "document_id": 1}),
                            page_identity({"url": "https://www.bilibili.com/", "document_id": 2}))


class PerceptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = "s" * 64
        self.env = patch.dict(os.environ, {"BINES_WEBSITE_BRIDGE_SPOOL": str(self.root),
                                         "BINES_WEBSITE_AUTO_OBSERVE": "true"})
        self.env.start()
        atomic_write_json(self.root / ".credentials.json", {"token": self.token})
        atomic_write_json(self.root / "heartbeat.json", heartbeat(self.token))
        self.value = {"id": "sample1", "platform": "bilibili", "status": "ok",
                      "captured_at": time.time(), "source_url": "https://www.bilibili.com/",
                      "page_identity": "page1", "analysis": "正在展示海鸟", "audio_observed": False}
        self.publish()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def publish(self, **updates):
        fields = {"platform": "bilibili", "updated_at": time.time(),
                  "page_identity": "page1", "observation": self.value, **updates}
        atomic_write_json(self.root / "observations" / "bilibili.json",
                          signed_envelope(self.token, "observation", fields))

    def test_owner_receives_data_and_journal_records_only_once(self):
        journal = Mock()
        with tool_context(ToolContext(journal=journal)):
            one = with_website_observations([{"role": "user", "content": "看看"}])
            two = with_website_observations([{"role": "user", "content": "看看"}])
        self.assertEqual(len(one), 2)
        self.assertEqual(one, two)
        self.assertIn("正在展示海鸟", one[-1]["content"])
        self.assertIn("不可执行", one[-1]["content"])
        journal.capture.assert_called_once()

    def test_group_and_nonowner_cannot_read_or_record_owner_window(self):
        for identity in (Identity("qq", "owner", group="123", owner=True),
                         Identity("qq", "456")):
            with tool_context(ToolContext(identity=identity, journal=Mock())) as context:
                self.assertEqual(latest_observations(), [])
                self.assertEqual(with_website_observations([]), [])
                context.journal.capture.assert_not_called()

    def test_old_page_expired_data_signature_and_dead_bridge_are_rejected(self):
        self.publish(page_identity="new-page")
        self.assertEqual(latest_observations(), [])
        self.value["captured_at"] = time.time() - 121
        self.publish()
        self.assertEqual(latest_observations(), [])
        self.value["captured_at"] = time.time()
        self.publish()
        path = self.root / "observations" / "bilibili.json"
        data = json.loads(path.read_text())
        data["observation"]["analysis"] = "伪造"
        atomic_write_json(path, data)
        self.assertEqual(latest_observations(), [])
        self.publish()
        atomic_write_json(self.root / "heartbeat.json",
                          signed_envelope(self.token, "heartbeat", {"ts": time.time() - 6}))
        self.assertEqual(latest_observations(), [])

    def test_disabled_auto_perception_and_failed_vision_do_not_invent_content(self):
        with patch.dict(os.environ, {"BINES_WEBSITE_AUTO_OBSERVE": "false"}):
            self.assertEqual(latest_observations(), [])
        self.value.update(status="failed", analysis="", message="视觉暂不可用")
        self.publish()
        self.assertEqual(latest_observations()[0]["analysis"], "")

    def test_native_model_refreshes_per_iteration_preserving_tool_receipt(self):
        from test_model_completion import model_with_responses, response
        from types import SimpleNamespace
        call = SimpleNamespace(
            id="open1", function=SimpleNamespace(name="website_open", arguments="{}"),
            model_dump=lambda **_: {"id": "open1", "type": "function",
                                    "function": {"name": "website_open", "arguments": "{}"}},
        )
        model = model_with_responses(response(tool_calls=[call]), response("看到了新的画面"))
        runner = Mock()
        runner.instructions_for_source.return_value = "工具"
        runner.function_tools.return_value = []
        runner.action_to_skill = {"website_open": "website"}
        def execute(*_, **__):
            self.value = {**self.value, "id": "sample2", "analysis": "新的视频"}
            self.publish()
            return "打开成功回执"
        runner.execute.side_effect = execute
        model.run_tool_calls([{"role": "user", "content": "看看"}], runner)
        requests = model.client.chat.completions.create.call_args_list
        self.assertIn("正在展示海鸟", str(requests[0].kwargs["messages"]))
        self.assertIn("新的视频", str(requests[1].kwargs["messages"]))
        initial = requests[0].kwargs["messages"]
        continuation = requests[1].kwargs["messages"]
        self.assertEqual(continuation[:len(initial)], initial)
        observations = [m for m in continuation if "<website_observations>\n" in str(m.get("content"))]
        self.assertIn("新的视频", observations[-1]["content"])
        self.assertIn("以最新采样为准", observations[-1]["content"])
        self.assertIn("打开成功回执", str(requests[1].kwargs["messages"]))

    def test_local_text_protocol_receives_automatic_observation(self):
        from test_model_completion import model_with_responses, response
        model = model_with_responses(response("看到海鸟"))
        runner = Mock()
        runner.instructions_for_source.return_value = "工具"
        runner.parse_model_command.return_value = None
        model.run_skill_protocol([{"role": "user", "content": "看看"}], runner, source="local")
        self.assertIn("正在展示海鸟", str(model.client.chat.completions.create.call_args.kwargs["messages"]))

    def test_activity_deliberation_receives_automatic_observation(self):
        from common.activity_deliberation import choose_activity
        model = Mock()
        model.complete_text.return_value = json.dumps({
            "mode": "finish", "reason": "已经满足好奇心", "delay_seconds": 0, "interest_level": 0.5,
        })
        with patch("prompt_builder.build_conversation_messages", return_value=[]):
            choose_activity(model, {}, {"intent": "随便看看"})
        self.assertIn("正在展示海鸟", str(model.complete_text.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
