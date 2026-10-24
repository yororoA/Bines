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
from common.website_cdp import ExistingBrowserUnavailable, ExistingPageReader
from common.website_observer import WebsiteObserver


class ExistingReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reader = ExistingPageReader(self.root)
        self.config = platform_config("bilibili")

    def tearDown(self):
        self.temp.cleanup()

    def page(self, target, *, visible=True, focused=True, url="https://www.bilibili.com/"):
        return {"id": target, "type": "page", "url": url,
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/" + target,
                "visible": visible, "focused": focused}

    @staticmethod
    def call(page, method, params=None, **_kwargs):
        if method == "Runtime.evaluate":
            value = {"visible": page["visible"], "focused": page["focused"]} if "document.visibilityState" in params["expression"] else "observed"
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

    def test_only_reads_existing_visible_target_and_screenshot(self):
        pages = [self.page("background", visible=False, focused=False),
                 self.page("visible")]
        with patch.object(self.reader, "_pages", return_value=pages), patch.object(
            self.reader, "_call", side_effect=self.call,
        ) as calls:
            self.assertEqual(self.reader.run(self.config, ["eval", "document.title"]), "observed")
            self.reader.run(self.config, ["screenshot", str(self.root / "shot.png")])
        self.assertEqual((self.root / "shot.png").read_bytes(), b"image")
        self.assertEqual(self.reader.targets["bilibili"], "visible")
        self.assertEqual({call.args[1] for call in calls.call_args_list},
                         {"Runtime.evaluate", "Page.captureScreenshot"})
        for call in calls.call_args_list:
            if call.args[1] == "Page.captureScreenshot":
                self.assertEqual(call.args[0]["id"], "visible")

    def test_no_visible_or_ambiguous_pages_do_not_activate_or_open_tabs(self):
        for pages in ([self.page("a", visible=False, focused=False)],
                      [self.page("a", focused=False), self.page("b", focused=False)]):
            with patch.object(self.reader, "_pages", return_value=pages), patch.object(
                self.reader, "_call", side_effect=self.call,
            ) as calls:
                with self.assertRaises(ExistingBrowserUnavailable):
                    self.reader.run(self.config, ["eval", "location.href"])
                self.assertTrue(all(call.args[1] == "Runtime.evaluate" for call in calls.call_args_list))

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


class BrowserLaunchFailureTests(unittest.TestCase):
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
