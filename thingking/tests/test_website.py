import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from chatBot.messages import Identity, LOCAL_IDENTITY
from common.website import (
    AgentBrowser,
    BrowserCommandError,
    PlanStore,
    WebsiteService,
    normalize_steps,
    platform_config,
    validate_url,
)
from tools.context import ToolContext, tool_context


class FakeBrowser:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def run(self, platform, args, timeout=60):
        args = list(args)
        self.calls.append((platform.key, args, timeout))
        if self.callback:
            return self.callback(platform, args, timeout)
        if args[:2] == ["get", "url"]:
            return f"https://www.{platform.key}.example/result"
        return "ok"

    def close(self):
        pass

    def read_existing(self, platform, args, timeout=60):
        return self.run(platform, args, timeout=timeout)

    def existing_platforms(self):
        return []


class WebsiteServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "static").mkdir()
        self.db = self.root / "website.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def service(self, browser=None):
        return WebsiteService(
            PlanStore(self.db),
            browser or FakeBrowser(),
            root=self.root,
            media_roots=[self.root / "static"],
        )

    @staticmethod
    def steps():
        return [
            {"op": "open", "url": "https://t.bilibili.com/"},
            {
                "op": "fill",
                "target": {"by": "placeholder", "value": "有什么想和大家分享的？"},
                "field": "content",
            },
            {
                "op": "click",
                "target": {"by": "role", "role": "button", "name": "发布"},
                "commit": True,
            },
            {"op": "snapshot"},
        ]

    def create(self, service=None):
        service = service or self.service()
        result = service.create_plan(
            "bilibili", "post", "离线测试草稿", steps=self.steps(),
        )
        return service, result["plan"]["id"]

    def test_urls_are_https_and_platform_scoped(self):
        platform = platform_config("bilibili")
        self.assertEqual(
            validate_url(platform, "https://www.bilibili.com/video/BV1"),
            "https://www.bilibili.com/video/BV1",
        )
        for value in (
            "http://www.bilibili.com/",
            "https://bilibili.com@evil.example/",
            "https://evil.example/",
        ):
            with self.assertRaises(ValueError):
                validate_url(platform, value)

    def test_steps_require_one_allowlisted_commit(self):
        platform = platform_config("bilibili")
        with self.assertRaisesRegex(ValueError, "只能有一个提交"):
            normalize_steps(platform, "post", "", "x", [], "", [
                {"op": "open", "url": "https://t.bilibili.com/"},
            ])
        with self.assertRaisesRegex(ValueError, "提交按钮仅允许"):
            normalize_steps(platform, "post", "", "x", [], "", [
                {
                    "op": "click",
                    "target": {"by": "role", "role": "button", "name": "删除账号"},
                    "commit": True,
                },
            ])

    def test_create_plan_does_not_touch_browser(self):
        browser = FakeBrowser()
        service, plan_id = self.create(self.service(browser))
        self.assertEqual(browser.calls, [])
        plan = service.store.get(plan_id)
        self.assertEqual(plan["status"], "pending_approval")
        self.assertEqual(plan["content"], "离线测试草稿")

    def test_approved_plan_executes_once_and_returns_receipt(self):
        browser = FakeBrowser()
        service, plan_id = self.create(self.service(browser))
        service.approve(plan_id)
        first = service.execute(plan_id)
        calls_after_first = list(browser.calls)
        second = service.execute(plan_id)
        self.assertEqual(first["plan_status"], "completed")
        self.assertEqual(second["plan_status"], "completed")
        self.assertEqual(browser.calls, calls_after_first)
        self.assertEqual(service.store.get(plan_id)["next_step"], len(self.steps()))

    def test_autonomous_bridge_execute_reuses_completed_plan(self):
        from website_bridge import dispatch
        browser = FakeBrowser()
        service, plan_id = self.create(self.service(browser))
        body = {"plan_id": plan_id, "autonomous": True}
        first = dispatch(service, "/v1/plans/execute", body)
        calls = len(browser.calls)
        second = dispatch(service, "/v1/plans/execute", body)
        self.assertEqual(first["plan_status"], "completed")
        self.assertEqual(second["plan_status"], "completed")
        self.assertEqual(len(browser.calls), calls)

    def test_commit_timeout_is_uncertain_and_never_replayed(self):
        def callback(_platform, args, _timeout):
            if args and args[0] == "find" and "click" in args:
                raise BrowserCommandError("timeout", timeout=True)
            return "ok"

        browser = FakeBrowser(callback)
        service, plan_id = self.create(self.service(browser))
        service.approve(plan_id)
        result = service.execute(plan_id)
        self.assertEqual(result["plan_status"], "uncertain")
        before = list(browser.calls)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            service.execute(plan_id)
        self.assertEqual(browser.calls, before)

    def test_interrupted_execution_is_uncertain_after_restart(self):
        service, plan_id = self.create()
        service.approve(plan_id)
        service.store.begin(plan_id)
        recovered = PlanStore(self.db).get(plan_id)
        self.assertEqual(recovered["status"], "uncertain")
        self.assertIn("禁止自动重放", recovered["error"])

    def test_challenge_before_commit_pauses_for_human(self):
        state = {"blocked": True}

        def callback(_platform, args, _timeout):
            if state["blocked"] and args and args[0] == "open":
                return "请先登录并完成验证码"
            return "ok"

        browser = FakeBrowser(callback)
        service, plan_id = self.create(self.service(browser))
        service.approve(plan_id)
        blocked = service.execute(plan_id)
        self.assertEqual(blocked["plan_status"], "blocked_human")
        self.assertTrue(blocked["human_action_required"])
        state["blocked"] = False
        completed = service.execute(plan_id)
        self.assertEqual(completed["plan_status"], "completed")

    def test_media_must_be_in_allowlisted_directory(self):
        service = self.service()
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"video")
        with self.assertRaisesRegex(ValueError, "媒体文件必须"):
            WebsiteService(
                PlanStore(self.db),
                FakeBrowser(),
                root=self.root,
                media_roots=[self.root / "static"],
            ).create_plan(
                "bilibili", "video", "demo", media_paths=[str(outside)],
            )
        inside = self.root / "static" / "inside.mp4"
        inside.write_bytes(b"video")
        result = service.create_plan(
            "bilibili", "video", "demo", media_paths=[str(inside)],
        )
        self.assertEqual(result["plan_status"], "pending_approval")
        invalid = self.root / "static" / "secret.txt"
        invalid.write_text("not media")
        with self.assertRaisesRegex(ValueError, "文件类型"):
            service.create_plan(
                "bilibili", "video", "demo", media_paths=[str(invalid)],
            )

    def test_container_media_path_maps_to_host_shared_directory(self):
        shared = self.root / "deploy" / "napcat" / "data" / "media" / "public"
        shared.mkdir(parents=True)
        video = shared / "clip.mp4"
        video.write_bytes(b"video")
        service = WebsiteService(
            PlanStore(self.db),
            FakeBrowser(),
            root=self.root,
            media_roots=[self.root / "deploy" / "napcat" / "data" / "media"],
        )
        result = service.create_plan(
            "bilibili", "video", "demo", media_paths=["/bines-media/public/clip.mp4"],
        )
        self.assertEqual(result["plan"]["media"], ["clip.mp4"])

    def test_interact_uses_observed_ref_and_replays_receipt_without_second_click(self):
        tree = '- button "播放" [ref=e1]\n- textbox "搜索" [ref=e2]'

        def callback(_platform, args, _timeout):
            return tree if args[0] == "snapshot" else "https://www.bilibili.com/"

        browser = FakeBrowser(callback)
        service = self.service(browser)
        service.snapshot("bilibili")
        first = service.interact("bilibili", "click", "@e1", operation_id="one")
        tree = '- button "登录" [ref=e1]'
        calls = len(browser.calls)
        second = service.interact("bilibili", "click", "@e1", operation_id="one")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first, second)
        self.assertEqual(len(browser.calls), calls)
        self.assertEqual(sum(args[0] == "click" for _, args, _ in browser.calls), 1)

    def test_stale_ref_is_rejected_before_dispatch_and_fresh_snapshot_recovers(self):
        tree = '- textbox "搜索" [ref=e1]'
        browser = FakeBrowser(lambda _platform, args, _timeout: tree if args[0] == "snapshot" else "ok")
        service = self.service(browser)
        service.snapshot("bilibili")
        tree = '- textbox "搜索" [ref=e2]\n- button "评论" [ref=e1]'
        with self.assertRaisesRegex(ValueError, "含义已变化"):
            service.interact("bilibili", "fill", "@e1", "螃蟹", operation_id="stale")
        self.assertFalse(any(args[0] == "fill" for _, args, _ in browser.calls))
        service.snapshot("bilibili")
        result = service.interact("bilibili", "fill", "@e2", "螃蟹", operation_id="fresh")
        self.assertEqual(result["status"], "ok")

    def test_navigation_error_requires_observation_but_public_click_is_uncertain(self):
        def callback(_platform, args, _timeout):
            if args[0] == "snapshot":
                return '- button "发表评论" [ref=e1]'
            if args[0] in {"back", "click"}:
                raise BrowserCommandError("browser timeout", timeout=True)
            return "ok"
        service = self.service(FakeBrowser(callback))
        service.snapshot("bilibili")
        navigation = service.interact("bilibili", "back", operation_id="nav")
        self.assertEqual(navigation["status"], "failed")
        self.assertTrue(navigation["reobserve_required"])
        public = service.interact("bilibili", "click", "@e1", operation_id="public")
        self.assertEqual(public["status"], "uncertain")
        self.assertFalse(public["retry_allowed"])

    def test_successful_write_receipt_survives_later_snapshot_failure(self):
        clicks = []
        def callback(_platform, args, _timeout):
            if args[0] == "snapshot":
                if clicks:
                    raise BrowserCommandError("observation timeout", timeout=True)
                return '- button "发表评论" [ref=e1]'
            if args[0] == "click":
                clicks.append(1)
            return "ok"
        service = self.service(FakeBrowser(callback))
        service.snapshot("bilibili")
        result = service.interact("bilibili", "click", "@e1", operation_id="public")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["reobserve_required"])
        self.assertEqual(service.interact("bilibili", "click", "@e1", operation_id="public"), result)
        self.assertEqual(len(clicks), 1)

    def test_silent_activity_can_search_but_cannot_fill_comment(self):
        tree = '- textbox "搜索" [ref=e1]\n- textbox "发表评论" [ref=e2]'
        browser = FakeBrowser(lambda _platform, args, _timeout: tree if args[0] == "snapshot" else "ok")
        service = self.service(browser)
        service.snapshot("bilibili")
        result = service.interact("bilibili", "fill", "@e1", "螃蟹", contact_allowed=False)
        self.assertEqual(result["status"], "ok")
        result = service.interact("bilibili", "press", "@e1", "Enter", contact_allowed=False)
        self.assertEqual(result["status"], "ok")
        with self.assertRaises(PermissionError):
            service.interact("bilibili", "fill", "@e2", "评论", contact_allowed=False)

    def test_verification_controls_are_not_operated(self):
        browser = FakeBrowser(lambda *_: '- textbox "验证码" [ref=e1]')
        service = self.service(browser)
        result = service.interact("bilibili", "fill", "@e1", "1234")
        self.assertTrue(result["human_action_required"])
        self.assertFalse(any(args[0] == "fill" for _, args, _ in browser.calls))

    def test_silent_activity_cannot_click_publication(self):
        browser = FakeBrowser(lambda *_: '- button "发表评论" [ref=e1]')
        service = self.service(browser)
        service.snapshot("bilibili")
        with self.assertRaises(PermissionError):
            service.interact("bilibili", "click", "@e1", contact_allowed=False)

    def test_silent_search_recognizes_trending_placeholder(self):
        tree = '- textbox "今日热门话题" [ref=e1]'
        def callback(_platform, args, _timeout):
            if args[0] == "snapshot":
                return tree
            if args[:2] == ["get", "attr"]:
                return "nav-search-input" if args[-1] == "class" else "text"
            return "ok"
        service = self.service(FakeBrowser(callback))
        service.snapshot("bilibili")
        result = service.interact("bilibili", "fill", "@e1", "螃蟹", contact_allowed=False)
        self.assertEqual(result["status"], "ok")

    def test_media_state_decodes_browser_content_boundary(self):
        state = [{"kind": "video", "paused": False, "current_time": 12, "duration": 94}]
        output = (
            "--- AGENT_BROWSER_PAGE_CONTENT nonce=test origin=https://www.bilibili.com/ ---\n"
            + json.dumps(json.dumps(state)) + "\n--- END_AGENT_BROWSER_PAGE_CONTENT nonce=test ---"
        )
        service = self.service(FakeBrowser(lambda *_: output))
        self.assertEqual(service.media_status("bilibili")["media_state"], state)

    def test_switch_from_video_tab_to_search_results(self):
        state = {"tab": "t3"}
        def callback(_platform, args, _timeout):
            if args == ["tab", "list"]:
                return "[t2] 搜索结果\n→ [t3] 视频"
            if args == ["tab", "t2"]:
                state["tab"] = "t2"
            if args[:2] == ["get", "url"]:
                return "https://search.bilibili.com/" if state["tab"] == "t2" else "https://www.bilibili.com/video/BV1"
            if args[0] == "snapshot":
                return '- textbox "搜索" [ref=e1]'
            return "ok"
        service = self.service(FakeBrowser(callback))
        self.assertIn("t2", service.tabs("bilibili")["tabs"])
        result = service.interact("bilibili", "switch_tab", value="t2", contact_allowed=False)
        self.assertEqual(result["url"], "https://search.bilibili.com/")
        with self.assertRaises(ValueError):
            service.interact("bilibili", "switch_tab", value="t2; exec")

    def test_same_content_returns_same_plan_including_uncertain_status(self):
        service, plan_id = self.create()
        service.store.finish(plan_id, "uncertain")
        _, duplicate_id = self.create(service)
        self.assertEqual(plan_id, duplicate_id)
        self.assertEqual(service.get_plan(duplicate_id)["plan_status"], "uncertain")


class WebsiteWatchTests(unittest.TestCase):
    setUp = WebsiteServiceTests.setUp
    tearDown = WebsiteServiceTests.tearDown
    service = WebsiteServiceTests.service

    def browser(self, *, paused=False, changed_at=0, challenge="", no_video=False):
        count = 0
        def callback(_platform, args, _timeout):
            nonlocal count
            if args[0] == "screenshot":
                from PIL import Image
                Image.new("RGB", (640, 480), "green").save(args[1])
                return "captured"
            if args[0] == "eval":
                count += 1
                changed = changed_at and count >= changed_at
                return json.dumps({
                    "url": "https://www.bilibili.com/video/" + ("BV2" if changed else "BV1"),
                    "title": "观察片段", "challenge": challenge,
                    "viewport": {"width": 640, "height": 480},
                    "video": None if no_video else {
                        "index": 0, "source": "https://media.example/video?token=private",
                        "paused": paused, "ended": False, "ready_state": 4, "duration": 100,
                        "current_time": 12 if paused else 12 + count,
                        "rect": {"left": 0, "top": 0, "right": 640, "bottom": 360},
                    },
                    "captions": ["海鸟正在捕食", "海鸟正在捕食"],
                })
            return "ok"
        return FakeBrowser(callback)

    def test_continuous_frames_preserve_captions_progress_and_no_raw_media(self):
        from common.activity import activity_checkpoint
        browser = self.browser()
        with patch("vlm_client.call_vision", return_value="画面展示海鸟俯冲捕食") as vision:
            with patch("common.website_watch.time.sleep"):
                result = self.service(browser).watch("bilibili", "看到了什么", 1, 3)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["audio_observed"])
        self.assertTrue(result["playback_advanced"])
        self.assertEqual(len(result["frames"]), 3)
        self.assertEqual(result["frames"][0]["media_time"], 13)
        self.assertIn("海鸟正在捕食", result["frames"][0]["captions"])
        self.assertEqual(len(vision.call_args.args[0]), 3)
        self.assertIn("播放13.00秒", vision.call_args.kwargs["image_labels"][0])
        self.assertNotIn("base64", json.dumps(result))
        self.assertNotIn("token=private", json.dumps(result))
        paths = [Path(args[1]) for _, args, _ in browser.calls if args[0] == "screenshot"]
        self.assertTrue(all(not path.exists() for path in paths))
        checkpoint = activity_checkpoint({"intent": "看视频"}, {}, [{
            "action": "website_watch", "result": json.dumps(result), "status": "succeeded",
        }])
        self.assertIn("海鸟俯冲捕食", str(checkpoint))
        self.assertIn("audio_observed", str(checkpoint))

    def test_paused_video_is_observed_as_still_frames(self):
        with patch("vlm_client.call_vision", return_value="视频暂停，画面重复") as vision:
            with patch("common.website_watch.time.sleep"):
                result = self.service(self.browser(paused=True)).watch("bilibili", frames=2)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["playback_advanced"])
        self.assertTrue(all(row["paused"] for row in result["frames"]))
        self.assertIn("paused=True", vision.call_args.kwargs["image_labels"][0])

    def test_switching_video_stops_sampling_and_never_mixes_content(self):
        for changed_at in (2, 3):
            with patch("vlm_client.call_vision") as vision:
                with patch("common.website_watch.time.sleep"):
                    result = self.service(self.browser(changed_at=changed_at)).watch("bilibili", frames=2)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["source_url"], "https://www.bilibili.com/video/BV1")
            self.assertNotIn("analysis", result)
            vision.assert_not_called()

    def test_navigation_during_inference_preserves_original_source(self):
        with patch("vlm_client.call_vision", return_value="原视频中的海鸟"):
            with patch("common.website_watch.time.sleep"):
                result = self.service(self.browser(changed_at=5)).watch("bilibili", frames=2)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["page_changed"])
        self.assertEqual(result["source_url"], "https://www.bilibili.com/video/BV1")

    def test_no_video_challenge_and_vision_failure_are_not_success(self):
        with patch("vlm_client.call_vision") as vision:
            absent = self.service(self.browser(no_video=True)).watch("bilibili")
            blocked = self.service(self.browser(challenge="请先登录并完成验证码")).watch("bilibili")
            vision.assert_not_called()
        self.assertEqual(absent["reason"], "no_media")
        self.assertEqual(blocked["status"], "blocked_human")
        with patch("vlm_client.call_vision", return_value="[VLM 请求失败 HTTP 500]"):
            with patch("common.website_watch.time.sleep"):
                failed = self.service(self.browser()).watch("bilibili", frames=2)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], "vision_failed")
        self.assertNotIn("analysis", failed)

    def test_sampling_arguments_are_bounded(self):
        service = self.service(self.browser())
        for kwargs in ({"frames": 20}, {"frames": True}, {"duration_seconds": float("nan")},
                       {"duration_seconds": 31}, {"question": "a" * 1001}):
            with self.assertRaises(ValueError):
                service.watch("bilibili", **kwargs)


class AgentBrowserTests(unittest.TestCase):
    def test_runner_receives_argv_not_shell_and_domain_policy(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="snapshot", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            browser = AgentBrowser(
                ["/tmp/agent-browser"],
                profile_root=Path(temp),
                executable_path="/tmp/Google Chrome for Testing",
                runner=runner,
            )
            output = browser.run(platform_config("bilibili"), ["snapshot", "-i"])
        self.assertEqual(output, "snapshot")
        self.assertIsInstance(calls[0], list)
        self.assertIn("--allowed-domains", calls[0])
        self.assertIn("*.bilibili.com", calls[0][calls[0].index("--allowed-domains") + 1])

    def test_close_does_not_start_inactive_bines_sessions(self):
        calls = []
        outputs = iter([
            "Active sessions:\n  personal\n  bines-bilibili\n",
            "Browser closed",
            "No active sessions",
        ])

        def runner(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=next(outputs), stderr="")

        with tempfile.TemporaryDirectory() as temp:
            browser = AgentBrowser(
                ["/tmp/agent-browser"],
                profile_root=Path(temp),
                executable_path="/tmp/Google Chrome for Testing",
                runner=runner,
            )
            browser.close()
        close_calls = [argv for argv in calls if argv[-1:] == ["close"]]
        self.assertEqual(len(close_calls), 1)
        self.assertIn("bines-bilibili", close_calls[0])
        self.assertNotIn("bines-douyin", close_calls[0])

    def test_platforms_use_independent_persistent_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = AgentBrowser(["test"], profile_root=Path(temp))
            args_b = browser._argv(platform_config("bilibili"), ["snapshot"])
            args_d = browser._argv(platform_config("douyin"), ["snapshot"])
            self.assertNotEqual(args_b[args_b.index("--profile") + 1], args_d[args_d.index("--profile") + 1])


class WebsiteRuntimeTests(unittest.TestCase):
    def test_compose_update_reads_live_token_without_launcher_environment(self):
        import docker_launcher
        from common.website_spool import BRIDGE_CREDENTIALS_FILE, atomic_write_json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spool = root / "thingking" / "memory_data" / "website_bridge"
            atomic_write_json(spool / BRIDGE_CREDENTIALS_FILE, {"token": "live-token"})
            with patch.object(docker_launcher, "ROOT", root), patch.object(
                docker_launcher, "source_hashes", return_value={},
            ), patch.dict(os.environ, {"BINES_WEBSITE_BRIDGE_TOKEN": ""}):
                env = docker_launcher._environment("chatbot")
            self.assertEqual(env["BINES_WEBSITE_BRIDGE_TOKEN"], "live-token")

    def test_newest_installed_chrome_for_testing_is_selected(self):
        from website_runtime import _chrome_for_testing_binary, installed_chrome_for_testing

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older = _chrome_for_testing_binary(root / "chrome-120.0.1.2")
            newer = _chrome_for_testing_binary(root / "chrome-156.0.9.1")
            for binary in (older, newer):
                binary.parent.mkdir(parents=True)
                binary.write_text("binary")
            self.assertEqual(installed_chrome_for_testing(root), str(newer))

    def test_missing_chrome_for_testing_uses_agent_browser_installer(self):
        import website_runtime

        with patch.dict(os.environ, {"BINES_CHROME_FOR_TESTING_EXECUTABLE": ""}):
            with patch.object(
                website_runtime,
                "installed_chrome_for_testing",
                side_effect=["", "/tmp/Chrome for Testing"],
            ), patch.object(
                website_runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="installed", stderr=""),
            ) as run:
                result = website_runtime.ensure_chrome_for_testing(["agent-browser"])
        self.assertEqual(result, "/tmp/Chrome for Testing")
        self.assertEqual(run.call_args.args[0], ["agent-browser", "install"])

    def test_stale_cleanup_closes_only_bines_sessions(self):
        import website_runtime

        listing = subprocess.CompletedProcess(
            [], 0,
            stdout="Active sessions:\n  personal\n  bines-douyin\n  bines-bilibili\n",
            stderr="",
        )
        closed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        empty = subprocess.CompletedProcess([], 0, stdout="No active sessions\n", stderr="")
        with patch.object(
            website_runtime.subprocess,
            "run",
            side_effect=[listing, closed, closed, empty],
        ) as run:
            website_runtime.close_stale_bines_sessions(["agent-browser"])
        commands = [call.args[0] for call in run.call_args_list[1:3]]
        self.assertEqual(len(commands), 2)
        self.assertTrue(all("personal" not in command for command in commands))
        self.assertEqual(
            {command[2] for command in commands},
            {"bines-douyin", "bines-bilibili"},
        )

    def test_bridge_refuses_implicit_system_chrome_fallback(self):
        import website_bridge

        with patch.dict(
            os.environ,
            {"BINES_AGENT_BROWSER_COMMAND": "agent-browser"},
            clear=False,
        ):
            os.environ.pop("BINES_CHROME_FOR_TESTING_EXECUTABLE", None)
            with self.assertRaisesRegex(ValueError, "Chrome for Testing"):
                website_bridge.create_service()


class WebsiteSpoolTests(unittest.TestCase):
    def test_recreated_container_connects_without_environment_token(self):
        from common.website_spool import BRIDGE_CREDENTIALS_FILE
        from website_bridge import SpoolWorker
        from tools import website_tool

        class Service:
            def status(self, platform=""):
                return {"status": "ok", "platform": platform}

        with tempfile.TemporaryDirectory() as temp:
            worker = SpoolWorker(Service(), temp, "live-token")
            worker.start()
            try:
                with patch.dict(os.environ, {
                    "BINES_WEBSITE_BRIDGE_SPOOL": temp, "BINES_WEBSITE_BRIDGE_TOKEN": "",
                }):
                    result = json.loads(website_tool._request(
                        "/v1/status", {"platform": "bilibili"}, timeout=2,
                    ))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["platform"], "bilibili")
                if os.name != "nt":
                    self.assertEqual((Path(temp).stat().st_mode & 0o777), 0o700)
                    self.assertEqual(
                        (Path(temp) / BRIDGE_CREDENTIALS_FILE).stat().st_mode & 0o777, 0o600,
                    )
            finally:
                worker.close()
            self.assertFalse((Path(temp) / BRIDGE_CREDENTIALS_FILE).exists())

    def test_bridge_restart_reconnects_with_frozen_old_environment_token(self):
        from website_bridge import SpoolWorker
        from tools import website_tool

        class Service:
            def __init__(self, generation):
                self.generation = generation
            def status(self, platform=""):
                return {"status": "ok", "generation": self.generation}

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "BINES_WEBSITE_BRIDGE_SPOOL": temp, "BINES_WEBSITE_BRIDGE_TOKEN": "old-token",
        }):
            for token, generation in (("old-token", 1), ("new-token", 2)):
                worker = SpoolWorker(Service(generation), temp, token)
                worker.start()
                try:
                    result = json.loads(website_tool._request("/v1/status", {}, timeout=2))
                    self.assertEqual(result["generation"], generation)
                    self.assertEqual(os.environ["BINES_WEBSITE_BRIDGE_TOKEN"], "old-token")
                finally:
                    worker.close()

    def test_invalid_published_credentials_do_not_fall_back_or_send_requests(self):
        from common.website_spool import BRIDGE_CREDENTIALS_FILE, atomic_write_json, heartbeat
        from tools import website_tool

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "BINES_WEBSITE_BRIDGE_SPOOL": temp, "BINES_WEBSITE_BRIDGE_TOKEN": "env-token",
        }):
            root = Path(temp)
            atomic_write_json(root / "heartbeat.json", heartbeat("env-token"))
            for token in ("", None, ["env-token"], "wrong-token"):
                with self.subTest(token=token):
                    atomic_write_json(root / BRIDGE_CREDENTIALS_FILE, {"token": token})
                    with self.assertRaises(website_tool.WebsiteRequestError) as caught:
                        website_tool._request("/v1/status", {}, timeout=0.1)
                    self.assertTrue(caught.exception.rejected)
                    self.assertEqual(list((root / "requests").glob("*.json")), [])

    def test_missing_credentials_report_startup_problem_before_enqueueing(self):
        from tools import website_tool

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "BINES_WEBSITE_BRIDGE_SPOOL": temp, "BINES_WEBSITE_BRIDGE_TOKEN": "",
        }):
            with self.assertRaisesRegex(website_tool.WebsiteRequestError, "尚未提供连接令牌"):
                website_tool._request("/v1/status", {}, timeout=0.1)
            self.assertEqual(list((Path(temp) / "requests").glob("*.json")), [])

    def test_owner_request_can_run_during_video_observation(self):
        from website_bridge import SpoolWorker
        from tools import website_tool
        entered, release = threading.Event(), threading.Event()
        class Service:
            def watch(self, *args):
                entered.set()
                release.wait(3)
                return {"status": "ok", "analysis": "测试帧"}
            def status(self, *args):
                return {"status": "ok", "runtime": "ready"}
        with tempfile.TemporaryDirectory() as temp:
            worker = SpoolWorker(Service(), temp, "test-token")
            worker.start()
            with patch.dict(os.environ, {
                "BINES_WEBSITE_BRIDGE_SPOOL": temp, "BINES_WEBSITE_BRIDGE_TOKEN": "test-token",
            }):
                results = []
                observer = threading.Thread(target=lambda: results.append(
                    website_tool._request("/v1/watch", {"platform": "bilibili"}, timeout=3)))
                observer.start()
                try:
                    self.assertTrue(entered.wait(1))
                    status = json.loads(website_tool._request("/v1/status", {}, timeout=1))
                    self.assertEqual(status["runtime"], "ready")
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    observer.join(3)
                    worker.close()
                self.assertEqual(json.loads(results[0])["analysis"], "测试帧")

    def test_second_bridge_cannot_replace_active_heartbeat(self):
        from website_bridge import SpoolWorker
        from common.website_spool import read_bridge_token
        with tempfile.TemporaryDirectory() as temp:
            first = SpoolWorker(None, temp, "first")
            second = SpoolWorker(None, temp, "second")
            first.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "已经运行"):
                    second.start()
                second.close()
                self.assertTrue((Path(temp) / "heartbeat.json").exists())
                self.assertEqual(read_bridge_token(temp), "first")
            finally:
                first.close()
            second.start()
            second.close()

    def test_heartbeat_keeps_updating_while_browser_request_is_blocked(self):
        import website_bridge
        from common.website_spool import read_json, signed_envelope, atomic_write_json

        entered, release = threading.Event(), threading.Event()
        class Service:
            def status(self, platform=""):
                entered.set()
                release.wait(3)
                return {"status": "ok"}

        with tempfile.TemporaryDirectory() as temp:
            root, token = Path(temp), "test-token"
            worker = website_bridge.SpoolWorker(Service(), root, token)
            worker.start()
            try:
                initial = read_json(root / "heartbeat.json")["ts"]
                request_id = "a" * 32
                atomic_write_json(root / "requests" / f"{request_id}.json", signed_envelope(token, "request", {
                    "id": request_id, "route": "/v1/status", "payload": {}, "created_at": time.time(),
                }))
                self.assertTrue(entered.wait(1))
                deadline = time.monotonic() + 2
                while read_json(root / "heartbeat.json")["ts"] == initial and time.monotonic() < deadline:
                    release.wait(0.02)
                self.assertGreater(read_json(root / "heartbeat.json")["ts"], initial)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                worker.close()

    def test_signed_spool_round_trip(self):
        import website_bridge
        from tools import website_tool

        class Service:
            def status(self, platform=""):
                return {"status": "ok", "runtime": "ready", "platform": platform}

        with tempfile.TemporaryDirectory() as temp:
            root, token = Path(temp), "test-token"
            worker = website_bridge.SpoolWorker(Service(), root, token)
            worker.start()
            try:
                deadline = time.time() + 2
                while not (root / "heartbeat.json").exists() and time.time() < deadline:
                    time.sleep(0.01)
                with patch.dict(os.environ, {
                    "BINES_WEBSITE_BRIDGE_SPOOL": str(root),
                    "BINES_WEBSITE_BRIDGE_TOKEN": token,
                }):
                    result = json.loads(
                        website_tool._request("/v1/status", {"platform": "bilibili"}, timeout=2)
                    )
                self.assertEqual(result["runtime"], "ready")
                self.assertEqual(result["platform"], "bilibili")
            finally:
                worker.close()

    def test_spool_rejects_tampered_heartbeat(self):
        from common.website_spool import atomic_write_json, heartbeat
        from tools import website_tool

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            atomic_write_json(root / "heartbeat.json", heartbeat("wrong-token"))
            with patch.dict(os.environ, {
                "BINES_WEBSITE_BRIDGE_SPOOL": str(root),
                "BINES_WEBSITE_BRIDGE_TOKEN": "expected-token",
            }):
                with self.assertRaisesRegex(RuntimeError, "未运行"):
                    website_tool._request("/v1/status", {}, timeout=0.1)

    def test_execute_timeout_is_reported_as_uncertain(self):
        from common.website_spool import atomic_write_json, heartbeat
        from tools import website_tool

        with tempfile.TemporaryDirectory() as temp:
            root, token = Path(temp), "test-token"
            atomic_write_json(root / "heartbeat.json", heartbeat(token))
            with patch.dict(os.environ, {
                "BINES_WEBSITE_BRIDGE_SPOOL": str(root),
                "BINES_WEBSITE_BRIDGE_TOKEN": token,
            }):
                result = json.loads(website_tool._request("/v1/plans/execute", {"plan_id": "A"}, timeout=0.05))
                self.assertEqual(result["status"], "uncertain")
                self.assertFalse(result["retry_allowed"])
                self.assertEqual(list((root / "requests").glob("*.json")), [])


class WebsiteToolAuthorityTests(unittest.TestCase):
    def setUp(self):
        from tools import website_tool

        self.tool = website_tool
        self.fake_result = json.dumps({"status": "ok"})

    def test_stranger_and_owner_group_cannot_use_account(self):
        stranger = Identity("bot", "stranger")
        owner_group = Identity("bot", "owner", group="group", owner=True)
        for identity in (stranger, owner_group):
            with tool_context(ToolContext(identity, "event", "turn")):
                with self.assertRaises(PermissionError):
                    self.tool.website_status()
                with self.assertRaises(PermissionError):
                    self.tool.website_watch("bilibili")

    def test_video_observation_checks_owner_interruption_before_and_after(self):
        from unittest.mock import Mock
        check = Mock(side_effect=["", "主人发来新消息"])
        context = ToolContext(LOCAL_IDENTITY, autonomous=True, check=check)
        with patch.object(self.tool, "_request", return_value=self.fake_result) as request:
            with tool_context(context), self.assertRaisesRegex(RuntimeError, "新消息"):
                self.tool.website_watch("bilibili")
        self.assertEqual(request.call_args.args[0], "/v1/watch")
        check = Mock(return_value="已暂停")
        with patch.object(self.tool, "_request") as request:
            with tool_context(ToolContext(LOCAL_IDENTITY, check=check)):
                with self.assertRaisesRegex(RuntimeError, "已暂停"):
                    self.tool.website_watch("bilibili")
        request.assert_not_called()

    def test_authorized_autonomous_turn_can_draft_and_publish(self):
        context = ToolContext(
            LOCAL_IDENTITY,
            "event",
            "turn",
            autonomous=True,
            user_text="批准 ABC123",
        )
        with patch.object(self.tool, "_request", return_value=self.fake_result):
            with tool_context(context):
                self.tool.website_create_draft("bilibili", "post", "draft")
                self.tool.website_approve("ABC123")
                self.tool.website_execute("ABC123")
        with patch.object(self.tool, "_request", return_value=self.fake_result):
            with tool_context(ToolContext(LOCAL_IDENTITY, autonomous=True, contact_allowed=False)):
                with self.assertRaises(PermissionError):
                    self.tool.website_execute("ABC123")

    def test_approval_and_execution_require_id_and_explicit_current_words(self):
        with patch.object(self.tool, "_request", return_value=self.fake_result) as request:
            with tool_context(ToolContext(
                LOCAL_IDENTITY,
                "event",
                "turn",
                user_text="批准 ABC123 并执行 ABC123",
            )):
                self.tool.website_approve("ABC123")
                self.tool.website_execute("ABC123")
            self.assertEqual(request.call_count, 2)
        with patch.dict(os.environ, {"BINES_WEBSITE_REQUIRE_APPROVAL": "true"}), tool_context(ToolContext(
            LOCAL_IDENTITY,
            "event",
            "turn",
            user_text="发吧",
        )):
            with self.assertRaisesRegex(PermissionError, "准确计划编号"):
                self.tool.website_execute("ABC123")

    def test_negated_approval_or_execution_is_rejected(self):
        with patch.object(self.tool, "_request", return_value=self.fake_result):
            for text, action in (
                ("不要批准 ABC123", self.tool.website_approve),
                ("批准 ABC123，但不要执行 ABC123", self.tool.website_execute),
            ):
                with tool_context(ToolContext(
                    LOCAL_IDENTITY, "event", "turn", user_text=text,
                )):
                    with self.assertRaisesRegex(PermissionError, "没有明确"):
                        action("ABC123")

    def test_owner_request_uses_existing_authority_without_plan_number(self):
        with patch.dict(os.environ, {"BINES_WEBSITE_REQUIRE_APPROVAL": "false"}):
            with patch.object(self.tool, "_request", return_value=self.fake_result) as request:
                with tool_context(ToolContext(LOCAL_IDENTITY, user_text="发吧")):
                    self.tool.website_execute("ABC123")
                self.assertTrue(request.call_args.args[1]["autonomous"])

    def test_transport_failure_retains_uncertain_receipt(self):
        context = ToolContext(LOCAL_IDENTITY, turn_id="test", autonomous=True)
        with patch.object(self.tool, "_request", side_effect=RuntimeError("connection lost")):
            with tool_context(context):
                result = json.loads(self.tool.website_interact("bilibili", "click", "@e1"))
        self.assertEqual(result["status"], "uncertain")
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(context.receipts[0]["result"], result)

    def test_known_predispatch_rejection_is_recoverable_and_execute_has_params(self):
        context = ToolContext(LOCAL_IDENTITY, turn_id="test", autonomous=True)
        error = self.tool.WebsiteRequestError("元素含义已变化，请刷新 snapshot", rejected=True)
        with patch.object(self.tool, "_request", side_effect=error), tool_context(context):
            result = json.loads(self.tool.website_interact("bilibili", "fill", "@e1", "螃蟹"))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["reobserve_required"])
        with patch.object(self.tool, "_request", return_value=self.fake_result), tool_context(context):
            self.tool.website_execute("ABC123")
        self.assertEqual(context.receipts[-1]["params"], {"plan_id": "ABC123"})


class WebsiteSkillRunnerTests(unittest.TestCase):
    def test_qq_exposes_website_only_to_owner_private_chat(self):
        from thingking.src.skill_runner import SkillRunner
        from tools import TOOLS_REGISTRY

        runner = SkillRunner(
            TOOLS_REGISTRY,
            skills_dir=Path(__file__).resolve().parents[2] / "skills",
            enabled_actions_path=Path(__file__).resolve().parents[2] / "server" / "skill_actions.json",
            qq_allowed_actions={"get_time"},
        )
        owner_private = Identity("bot", "owner", owner=True)
        owner_group = Identity("bot", "owner", group="group", owner=True)
        stranger = Identity("bot", "stranger")
        with tool_context(ToolContext(owner_private, "event", "turn")):
            actions = runner.actions_for_source("QQ")
            self.assertIn("website_open", actions)
            self.assertIn("website_execute", actions)
            self.assertIn("website_watch", actions)
        for identity in (owner_group, stranger):
            with tool_context(ToolContext(identity, "event", "turn")):
                actions = runner.actions_for_source("QQ")
                self.assertNotIn("website_open", actions)
                self.assertNotIn("website_execute", actions)
                self.assertNotIn("website_watch", actions)


if __name__ == "__main__":
    unittest.main()
