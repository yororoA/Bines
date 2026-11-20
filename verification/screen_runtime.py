"""Screen worker behavior checks, using isolated state and generated images."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PIL import Image

from common.screen_runtime import ScreenWorker, build_sheet_prompt, fit_frame
from common.screen_observation import read_observation
from realtime_screen_config import normalize_realtime_screen_config, save_realtime_screen_config


class ScreenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root / "config.json"
        self.context = self.root / "context.json"
        self.observation = self.root / "observation.json"
        self.stop = threading.Event()
        self.capture_count = 0
        self.calls = []
        self.errors = []
        self.thread = None
        save_realtime_screen_config(self.config, {"enabled": True, "capture_fps": 4,
                                                 "interval_sec": 3, "quality": "low"})

    def tearDown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(8)
            self.assertFalse(self.thread.is_alive(), "worker did not stop")
        self.directory.cleanup()

    def capture(self):
        self.capture_count += 1
        return Image.new("RGB", (320, 240), "navy")

    def vision(self, encoded, prompt):
        self.calls.append((encoded, prompt))
        return "测试页面为蓝色背景。"

    def start(self, vision=None, capture=None):
        worker = ScreenWorker(config_path=self.config, context_path=self.context,
                              flush_path=self.root / "flush", observation_path=self.observation,
                              capture=capture or self.capture, vision=vision or self.vision, stop=self.stop)
        def run():
            try:
                worker.run()
            except Exception as exc:
                self.errors.append(exc)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait_for(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.errors:
                raise self.errors[0]
            time.sleep(0.03)
        self.fail("condition did not become true")

    def test_real_node_publishes_fresh_observation_and_forces_only_once(self):
        self.context.write_text(json.dumps({"request_id": "old", "updated_at": time.time(),
                                            "user_input": "旧问题"}))
        self.start()
        self.wait_for(lambda: read_observation(self.observation).get("fresh"))
        observation = read_observation(self.observation)
        self.assertEqual(observation["source"], "see_now")
        self.assertEqual(observation["analysis"], "测试页面为蓝色背景。")
        self.assertTrue(observation["frames"])
        self.assertIn("cell_pixels", self.calls[0][1])
        self.context.write_text(json.dumps({"request_id": "new", "updated_at": time.time(),
                                            "user_input": "现在屏幕是什么"}))
        self.wait_for(lambda: len(self.calls) == 2)
        time.sleep(0.6)
        self.assertEqual(len(self.calls), 2, "persisted context must not force every capture")

    def test_capture_continues_during_inference_and_disable_discards_result(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_vision(*args):
            entered.set()
            release.wait(5)
            return "不应复活的旧描述"
        self.start(vision=blocked_vision)
        self.assertTrue(entered.wait(5))
        before = self.capture_count
        self.wait_for(lambda: self.capture_count >= before + 3)
        save_realtime_screen_config(self.config, {"enabled": False})
        release.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(read_observation(self.observation)["status"], "stopped")
        self.assertEqual(read_observation(self.observation)["analysis"], "")

    def test_failed_vision_is_not_published_as_screen_content(self):
        self.start(vision=lambda *_: "[VLM 未配置]")
        self.wait_for(lambda: read_observation(self.observation).get("status") == "failed")
        self.assertEqual(read_observation(self.observation)["analysis"], "")

    def test_refresh_waits_for_capture_after_the_request(self):
        save_realtime_screen_config(self.config, {"enabled": True, "backend": "legacy"})
        requested, capturing, release = threading.Event(), threading.Event(), threading.Event()
        def capture():
            if requested.is_set():
                capturing.set()
                release.wait(5)
                return Image.new("RGB", (320, 240), "red")
            return Image.new("RGB", (320, 240), "blue")
        self.start(capture=capture)
        self.wait_for(lambda: len(self.calls) == 1)
        requested.set()
        self.context.write_text(json.dumps({"request_id": "refresh", "updated_at": time.time()}))
        self.assertTrue(capturing.wait(3))
        try:
            time.sleep(0.2)
            self.assertEqual(len(self.calls), 1, "refresh consumed before a new capture")
        finally:
            release.set()
        self.wait_for(lambda: len(self.calls) == 2)

    def test_capture_error_marks_failure_and_stops(self):
        self.start(capture=lambda: None)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.errors)
        self.assertEqual(read_observation(self.observation)["status"], "failed")

    def test_capture_failure_invalidates_old_success_while_vision_is_pending(self):
        blocked, release, fail_capture = threading.Event(), threading.Event(), threading.Event()
        def vision(*args):
            self.calls.append(args)
            if len(self.calls) == 2:
                blocked.set()
                release.wait(5)
            return "旧画面"
        def capture():
            return None if fail_capture.is_set() else self.capture()
        self.start(vision=vision, capture=capture)
        self.wait_for(lambda: read_observation(self.observation).get("fresh"))
        self.context.write_text(json.dumps({"request_id": "refresh", "updated_at": time.time()}))
        self.assertTrue(blocked.wait(3))
        fail_capture.set()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if read_observation(self.observation).get("status") == "failed":
                    break
                time.sleep(0.03)
            self.assertEqual(read_observation(self.observation)["status"], "failed")
            self.assertFalse(read_observation(self.observation)["fresh"])
        finally:
            release.set()
        self.thread.join(5)

    def test_legacy_rollback_publishes_timestamped_result(self):
        save_realtime_screen_config(self.config, {"enabled": True, "backend": "legacy"})
        self.start()
        self.wait_for(lambda: read_observation(self.observation).get("fresh"))
        self.assertEqual(read_observation(self.observation)["source"], "legacy_screen")

    def test_stuck_capture_cannot_write_into_a_replacement_session(self):
        capture_waiting, release_capture = threading.Event(), threading.Event()
        vision_waiting, release_vision = threading.Event(), threading.Event()
        def capture():
            self.capture_count += 1
            if self.capture_count == 2:
                capture_waiting.set()
                release_capture.wait(8)
            return Image.new("RGB", (320, 240), "blue")
        def vision(*args):
            self.calls.append(args)
            vision_waiting.set()
            release_vision.wait(8)
            return "原会话观察"
        self.start(capture=capture, vision=vision)
        self.assertTrue(vision_waiting.wait(4))
        self.assertTrue(capture_waiting.wait(4))
        save_realtime_screen_config(self.config, {"enabled": True, "interval_sec": 20})
        release_vision.set()
        try:
            self.thread.join(4)
            self.assertFalse(self.thread.is_alive())
            self.assertTrue(self.errors)
            self.assertEqual(len(self.calls), 1, "must not launch a second session beside stuck capture")
            before = self.observation.read_bytes()
            release_capture.set()
            time.sleep(0.2)
            self.assertEqual(self.observation.read_bytes(), before, "late collector modified state")
        finally:
            release_capture.set()
            release_vision.set()

    def test_portrait_screen_keeps_all_edges(self):
        image = Image.new("RGB", (100, 200), "red")
        result = fit_frame(image, (200, 100))
        self.assertEqual(result.getpixel((100, 0)), (255, 0, 0))
        self.assertEqual(result.getpixel((100, 99)), (255, 0, 0))
        self.assertEqual(result.getpixel((0, 50)), (0, 0, 0))

    def test_config_normalization_rejects_nonfinite_and_malformed_values(self):
        config = normalize_realtime_screen_config({"enabled": "false", "capture_fps": float("nan"),
                                                   "quality": [], "backend": {},
                                                   "interval_sec": float("inf")})
        self.assertFalse(config["enabled"])
        self.assertEqual(config["capture_fps"], 4)
        self.assertEqual(config["quality"], "high")
        self.assertEqual(config["backend"], "see_now")
        self.assertEqual(config["interval_sec"], 10)


if __name__ == "__main__":
    unittest.main()
