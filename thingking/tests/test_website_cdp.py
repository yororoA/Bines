import base64
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import helpers
import test_website as fixtures
from common.website import AgentBrowser, BrowserCommandError, PlanStore, WebsiteService, platform_config
from common.website_cdp import ExistingBrowserUnavailable, ExistingPageReader, VideoPixelsUnavailable
from common.website_observer import WebsiteObserver


class ExistingReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reader = ExistingPageReader(self.root)
        self.config = platform_config("bilibili")

    def tearDown(self):
        self.temp.cleanup()

    def page(self, target, *, visible=True, focused=True, url="https://www.bilibili.com/", document_id=1000):
        return {"id": target, "type": "page", "url": url,
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/" + target,
                "visible": visible, "focused": focused, "document_id": document_id}

    @staticmethod
    def call(page, method, params=None, **_kwargs):
        if method == "Runtime.evaluate":
            value = {"url": page["url"], "document_id": page["document_id"]} if "performance.timeOrigin" in params["expression"] else "observed"
            return {"result": {"value": value}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"image").decode()}
        raise AssertionError("Unexpected CDP method: " + method)

    def test_missing_browser_never_calls_browser_command_or_creates_page(self):
        runner = Mock(side_effect=AssertionError("Must never launch CLI"))
        browser = AgentBrowser(["forbidden-agent-browser"], profile_root=self.root, runner=runner)
        with patch.object(browser.page_reader, "_ports", return_value=[]):
            for _ in range(4):
                with self.assertRaises(ExistingBrowserUnavailable):
                    browser.read_existing(self.config, ["eval", "location.href"])
        runner.assert_not_called()

    def test_bound_hidden_target_is_read_even_when_another_tab_is_foreground(self):
        pages = [self.page("background", visible=False, focused=False),
                 self.page("visible")]
        self.reader.bound_targets["bilibili"] = "background"
        with patch.object(self.reader, "_pages", return_value=pages), patch.object(
            self.reader, "_call", side_effect=self.call,
        ) as calls:
            self.assertEqual(self.reader.run(self.config, ["eval", "document.title"]), "observed")
            self.reader.run(self.config, ["screenshot", str(self.root / "shot.png")])
        self.assertEqual((self.root / "shot.png").read_bytes(), b"image")
        self.assertEqual(self.reader.targets["bilibili"], "background")
        self.assertEqual({call.args[1] for call in calls.call_args_list},
                         {"Runtime.evaluate", "Page.captureScreenshot"})
        for call in calls.call_args_list:
            if call.args[1] == "Page.captureScreenshot":
                self.assertEqual(call.args[0]["id"], "background")
                self.assertFalse(call.args[2]["fromSurface"])

    def test_video_pixels_use_evaluate_without_compositor_capture(self):
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (320, 180), "green").save(image, "JPEG")
        payload = "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode()
        target = self.page("visible")
        with patch.object(self.reader, "_target", return_value=target), patch.object(
            self.reader, "_call", return_value={"result": {"value": {"data": payload, "media_time": 32.5}}},
        ) as call:
            path = self.root / "video.jpg"
            self.assertEqual(self.reader.capture_video(self.config, 0, path), 32.5)
        self.assertEqual(path.read_bytes(), image.getvalue())
        self.assertEqual(call.call_args.args[1], "Runtime.evaluate")
        self.assertIn("drawImage", call.call_args.args[2]["expression"])
        self.assertNotIn("appendChild", call.call_args.args[2]["expression"])

    def test_tainted_video_can_use_viewport_fallback_but_timeout_does_not_retry(self):
        service = WebsiteService(PlanStore(self.root / "outbox.db"),
                                 AgentBrowser(["test"], profile_root=self.root), root=self.root)
        state = {"video": {"index": 0}}
        with patch.object(service.browser, "capture_video", side_effect=VideoPixelsUnavailable("cross_origin")), patch.object(
            service, "read_browser",
        ) as read:
            self.assertEqual(service.capture_video_frame(self.config, state, self.root / "frame"), "browser_viewport")
            self.assertEqual(read.call_count, 1)
        with patch.object(service.browser, "capture_video", side_effect=ExistingBrowserUnavailable("timeout", stage="Runtime.evaluate")), patch.object(
            service, "read_browser",
        ) as read:
            with self.assertRaises(ExistingBrowserUnavailable):
                service.capture_video_frame(self.config, state, self.root / "frame")
            read.assert_not_called()

    def test_transport_receives_have_remaining_deadline_even_with_event_flood(self):
        import websocket
        connection = Mock()
        connection.recv.side_effect = [
            json.dumps({"method": "event"}), json.dumps({"id": 1, "result": {"result": {"value": 1}}}),
        ]
        with patch.object(websocket, "create_connection", return_value=connection), patch(
            "common.website_cdp.time.monotonic", side_effect=[10, 10.2, 10.2, 11.8, 11.8],
        ):
            result = self.reader._call(self.page("a"), "Runtime.evaluate", timeout=2)
        self.assertEqual(result["result"]["value"], 1)
        timeouts = [round(call.args[0], 2) for call in connection.settimeout.call_args_list]
        self.assertEqual(timeouts, [1.8, 0.2])
        connection.close.assert_called_once()

    def test_unique_hidden_page_is_observable_without_focusing_it(self):
        with patch.object(self.reader, "_pages", return_value=[self.page("a", visible=False, focused=False)]), patch.object(
            self.reader, "_call", side_effect=self.call,
        ) as calls:
            self.assertEqual(self.reader.run(self.config, ["eval", "document.title"]), "observed")
            self.assertEqual(self.reader.run(self.config, ["tab", "list"]), "observed_target=a")
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(calls.call_args.args[1], "Runtime.evaluate")

    def test_ambiguous_unbound_pages_do_not_activate_or_open_tabs(self):
        for pages in ([self.page("a"), self.page("b", visible=False, focused=False)],
                      [self.page("a", focused=False), self.page("b", focused=False)]):
            with patch.object(self.reader, "_pages", return_value=pages), patch.object(
                self.reader, "_call", side_effect=self.call,
            ) as calls:
                with self.assertRaises(ExistingBrowserUnavailable):
                    self.reader.run(self.config, ["eval", "location.href"])
                calls.assert_not_called()

    def test_binding_disambiguates_identical_urls_by_document_identity(self):
        pages = [self.page("a", document_id=1000), self.page("b", document_id=2000, visible=False)]
        with patch.object(self.reader, "_call", side_effect=self.call):
            self.reader.bind(self.config, {"url": pages[1]["url"], "document_id": 2000}, pages)
        self.assertEqual(self.reader.bound_targets["bilibili"], "b")

    def test_failed_binding_closed_target_and_foreign_navigation_never_fall_back(self):
        for bound, pages in (
            (None, [self.page("other")]),
            ("closed", [self.page("other")]),
            ("a", [self.page("a", url="https://example.com"), self.page("other")]),
            ("a", [self.page("other")]),
        ):
            self.reader.bound_targets["bilibili"] = bound
            with patch.object(self.reader, "_pages", return_value=pages), patch.object(self.reader, "_call") as calls:
                with self.assertRaises(ExistingBrowserUnavailable):
                    self.reader.run(self.config, ["eval", "document.title"])
                calls.assert_not_called()

    def test_explicit_tab_selection_and_background_read_share_one_document(self):
        pages = [self.page("a", document_id=1000), self.page("b", document_id=2000, visible=False)]
        identity = {"url": pages[1]["url"], "document_id": 2000}
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, stdout="Selected tab 1"),
            subprocess.CompletedProcess([], 0, stdout=(
                "--- AGENT_BROWSER_PAGE_CONTENT nonce=test origin=https://www.bilibili.com/ ---\n"
                + json.dumps(identity) + "\n--- END_AGENT_BROWSER_PAGE_CONTENT nonce=test ---"
            )),
        ])
        browser = AgentBrowser(["test"], profile_root=self.root, runner=runner)
        with patch.object(browser.page_reader, "_pages", return_value=pages), patch.object(
            browser.page_reader, "_call", side_effect=self.call,
        ) as calls:
            self.assertEqual(browser.run(self.config, ["tab", "t1"]), "Selected tab 1")
            self.assertEqual(browser.read_existing(self.config, ["eval", "document.title"]), "observed")
        self.assertEqual(calls.call_args.args[0]["id"], "b")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.args[0][-2], "eval")

    def test_successful_write_receipt_survives_binding_failure_without_stale_reads(self):
        runner = Mock(side_effect=[
            subprocess.CompletedProcess([], 0, stdout="Clicked"),
            subprocess.CompletedProcess([], 0, stdout="not a document identity"),
        ])
        browser = AgentBrowser(["test"], profile_root=self.root, runner=runner)
        browser.page_reader.bound_targets["bilibili"] = "old"
        with patch.object(browser.page_reader, "_pages", return_value=[self.page("old")]), patch.object(
            browser.page_reader, "_call",
        ) as calls:
            self.assertEqual(browser.run(self.config, ["click", "@e1"]), "Clicked")
            with self.assertRaises(ExistingBrowserUnavailable):
                browser.read_existing(self.config, ["eval", "document.title"])
        calls.assert_not_called()

    def test_foreign_domain_and_remote_debugger_are_not_read(self):
        with patch.object(self.reader, "_pages", return_value=[self.page("a", url="https://example.com")]), patch.object(
            self.reader, "_call",
        ) as calls:
            with self.assertRaises(ExistingBrowserUnavailable):
                self.reader.run(self.config, ["eval", "location.href"])
            calls.assert_not_called()
        with self.assertRaises(ExistingBrowserUnavailable):
            self.reader._call({"webSocketDebuggerUrl": "ws://remote.example:9222/devtools/page/a"},
                              "Runtime.evaluate", {"expression": "1"})

    def test_closed_browser_invalidates_port_and_does_not_reconnect_by_launching(self):
        self.reader.ports["bilibili"] = 9222
        with patch.object(self.reader, "_json", side_effect=OSError("closed")):
            with self.assertRaises(ExistingBrowserUnavailable):
                self.reader._pages("bilibili")
        self.assertNotIn("bilibili", self.reader.ports)

    def test_video_watch_preserves_pixel_frames_progress_and_original_dimensions(self):
        from PIL import Image
        browser = fixtures.WebsiteWatchTests.browser(self)
        capture_count = 0
        def capture(_config, _index, path, **_kwargs):
            nonlocal capture_count
            capture_count += 1
            Image.new("RGB", (320, 180), "green").save(path, "JPEG")
            return 12 + capture_count * 2
        browser.capture_video = capture
        service = WebsiteService(PlanStore(self.root / "outbox.db"), browser, root=self.root)
        with patch("vlm_client.call_vision", return_value="可见海鸟") as vision, patch("common.website_watch.time.sleep"):
            result = service.watch("bilibili", duration_seconds=1, frames=3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([frame["media_time"] for frame in result["frames"]], [14, 16, 18])
        self.assertEqual(Image.open(BytesIO(base64.b64decode(vision.call_args.args[0][0]))).size, (320, 180))
        self.assertTrue(all(frame["capture_method"] == "video_pixels" for frame in result["frames"]))
        self.assertTrue(result["playback_advanced"])
        self.assertFalse(any(args[0] == "screenshot" for _, args, _ in browser.calls))
        self.assertNotIn("data:image", json.dumps(result))

    def test_capture_timeout_returns_named_stage_without_calling_vision(self):
        browser = fixtures.WebsiteWatchTests.browser(self)
        browser.capture_video = Mock(side_effect=ExistingBrowserUnavailable("timeout", stage="Runtime.evaluate"))
        service = WebsiteService(PlanStore(self.root / "outbox.db"), browser, root=self.root)
        with patch("vlm_client.call_vision") as vision:
            result = service.watch("bilibili", duration_seconds=1, frames=3)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "Runtime.evaluate")
        vision.assert_not_called()
        self.assertFalse(any(args[0] == "screenshot" for _, args, _ in browser.calls))


class BrowserLaunchFailureTests(unittest.TestCase):
    def test_bridge_launch_enables_attach_only_reads_and_background_playback(self):
        import os
        import website_runtime as runtime
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ):
            root = Path(temp)
            bridge = runtime.WebsiteBridgeProcess()
            opener = Mock()
            opener.open.return_value = BytesIO(b'{"status":"ok"}')
            process = Mock()
            process.poll.return_value = None
            with patch.object(runtime, "ROOT", root), patch.object(
                runtime, "AGENT_BROWSER_SOCKET_DIR", root / "runtime",
            ), patch.object(runtime, "AGENT_BROWSER_RUNTIME_HOME", root / "home"), patch.object(
                runtime, "AGENT_BROWSER_CRASH_DIR", root / "crashes",
            ), patch.object(runtime, "ensure_agent_browser", return_value=["test"]), patch.object(
                runtime, "ensure_browser_node", return_value="/test/node",
            ), patch.object(runtime, "ensure_chrome_for_testing", return_value="/test/chrome"), patch.object(
                runtime, "close_stale_bines_sessions",
            ), patch.object(runtime.subprocess, "Popen", return_value=process) as launch, patch.object(
                runtime, "build_opener", return_value=opener,
            ):
                try:
                    self.assertTrue(bridge._start())
                    args = launch.call_args.kwargs["env"]["AGENT_BROWSER_ARGS"].split(",")
                    for required in ("--remote-debugging-port=0", "--disable-background-timer-throttling",
                                     "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"):
                        self.assertIn(required, args)
                finally:
                    bridge.close()

    def test_status_reports_degraded_browser_and_read_only_tools_remain_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = Mock(return_value=subprocess.CompletedProcess([], 1, stderr="Chrome exited early", stdout=""))
            browser = AgentBrowser(["test-cli"], profile_root=root, runner=runner)
            service = WebsiteService(PlanStore(root / "outbox.db"), browser, root=root)
            with self.assertRaises(BrowserCommandError):
                browser.run(platform_config("bilibili"), ["open", "https://www.bilibili.com/"])
            status = service.status("bilibili")
            self.assertEqual(status["runtime"], "degraded")
            self.assertTrue(status["bridge_ready"])
            self.assertEqual(status["browser_failures"]["bilibili"]["stage"], "open")
            with patch.object(browser, "read_existing", return_value='[{"kind":"video","current_time":22}]'):
                self.assertEqual(service.media_status("bilibili")["media_state"][0]["current_time"], 22)
            self.assertEqual(runner.call_count, 1)

    def test_paused_bridge_does_not_dispatch_browsing_or_watch(self):
        from website_bridge import dispatch
        service = Mock()
        service.paused = True
        for route in ("/v1/open", "/v1/watch", "/v1/snapshot", "/v1/interact"):
            result = dispatch(service, route, {"platform": "bilibili"})
            self.assertEqual(result["status"], "paused")
            self.assertFalse(result["retry_allowed"])
        service.open.assert_not_called()
        service.watch.assert_not_called()
        service.interact.assert_not_called()

    def test_failed_launch_is_not_repeated_by_later_model_actions(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Chrome exited early without writing DevToolsActivePort"))
        with tempfile.TemporaryDirectory() as temp:
            browser = AgentBrowser(["test-cli"], profile_root=Path(temp), runner=runner)
            config = platform_config("bilibili")
            with self.assertRaises(BrowserCommandError):
                browser.run(config, ["open", config.entry_url])
            for args in (["open", config.entry_url], ["snapshot"], ["eval", "location.href"]):
                with self.assertRaisesRegex(BrowserCommandError, "暂停自动重试"):
                    browser.run(config, args)
        self.assertEqual(runner.call_count, 1)

    def test_observer_failure_suspends_platform_until_explicit_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = Mock()
            browser.existing_platforms.return_value = ["bilibili"]
            browser.read_existing.side_effect = ExistingBrowserUnavailable("closed")
            service = WebsiteService(PlanStore(Path(temp) / "outbox.db"), browser, root=Path(temp))
            observer = WebsiteObserver(service, "t" * 64, interval=0.01)
            observer.start()
            try:
                for _ in range(50):
                    if not observer.active:
                        break
                    observer.stop.wait(0.005)
                self.assertNotIn("bilibili", observer.active)
                observer.stop.wait(0.03)
                self.assertEqual(browser.read_existing.call_count, 1)
                browser.run.assert_not_called()
            finally:
                observer.close()

    def test_video_capture_failure_is_suspended_and_busy_watch_keeps_previous_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = WebsiteService(PlanStore(root / "outbox.db"), fixtures.FakeBrowser(), root=root)
            observer = WebsiteObserver(service, "t" * 64, min_interval=0)
            observer.activate("bilibili")
            state = {"url": "https://www.bilibili.com/", "viewport": {},
                     "video": {"ready_state": 4, "index": 0}}
            fingerprint = ((False,) * 64, (False,) * 64)
            with patch.object(observer, "_capture", return_value=(state, fingerprint, "")), patch.object(
                service, "watch", return_value={"status": "failed", "reason": "observation_failed",
                                               "failed_stage": "Page.captureScreenshot"},
            ):
                with self.assertRaises(ExistingBrowserUnavailable) as caught:
                    observer.tick("bilibili")
            self.assertEqual(caught.exception.stage, "Page.captureScreenshot")
            previous = {"id": "valid", "status": "ok"}
            observer.latest["bilibili"]["observation"] = previous
            observer.active["bilibili"]["last_analysis"] -= 31
            with patch.object(observer, "_capture", return_value=(state, fingerprint, "")), patch.object(
                service, "watch", return_value={"status": "observing"},
            ):
                observer.tick("bilibili")
            self.assertEqual(observer.latest["bilibili"]["observation"], previous)

    def test_bundled_runtime_uses_native_binary_without_node_child(self):
        from website_runtime import _bundled_command
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            shim = folder / "agent-browser.js"
            shim.write_text("shim")
            native = folder / "agent-browser-darwin-arm64"
            native.write_text("native")
            with patch("website_runtime.sys.platform", "darwin"), patch(
                "website_runtime.platform.machine", return_value="arm64",
            ):
                self.assertEqual(_bundled_command(shim), [str(native.resolve())])


if __name__ == "__main__":
    unittest.main()
