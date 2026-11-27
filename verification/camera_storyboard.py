"""Controlled camera-source/real Node/ZMQ checks; never open a physical camera."""
import ast
import base64
import io
import json
from pathlib import Path
import socket
import threading
import time
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image
import zmq

from common.camera_storyboard import CameraStoryboard, CameraStoryboardError, describe_camera


ROOT = Path(__file__).resolve().parents[1]


class Clock:
    tick = 0.0

    def monotonic(self):
        return self.tick

    def time(self):
        return 1000 + self.tick


def sample_sheet(*times):
    encoded = io.BytesIO()
    Image.new("RGB", (384, 216), "blue").save(encoded, "PNG")
    return {"png": encoded.getvalue(), "frames": [{"wall": stamp * 1000, "t": stamp - 1000}
                                                   for stamp in times],
            "cells": [{"px": index % 3 * 128, "py": index // 3 * 72, "pw": 128, "ph": 72}
                      for index in range(len(times))], "captured_at": times[-1]}


class ControlledClient:
    def __init__(self, *_, **__):
        self.waiting, self.flushed = None, []
        self.closed = False

    def start(self):
        return self

    def push(self, *_args, **_kwargs):
        return True

    def take(self):
        value, self.waiting = self.waiting, None
        return value

    def flush(self):
        return self.flushed.pop(0)

    def close(self):
        self.closed = True


class CameraStoryboardTests(unittest.TestCase):
    def controlled(self):
        clock = Clock()
        story = CameraStoryboard(width=384, height=216, client_factory=ControlledClient,
                                 monotonic=clock.monotonic, wall_time=clock.time).start()
        self.addCleanup(story.close)
        return clock, story

    def test_request_keeps_recent_history_and_deduplicates_current(self):
        clock, story = self.controlled()
        clock.tick = 8
        story.client.waiting = sample_sheet(1002, 1004, 1006)
        story.push(Image.new("RGB", (64, 48)), captured_at=1008)
        story.client.flushed = [sample_sheet(1007, 1008)]
        self.assertEqual(len(story.snapshot()), 2)
        story.client.waiting = sample_sheet(1007, 1008)
        story.client.flushed = [sample_sheet(1008)]
        self.assertEqual(len(story.snapshot()), 1)

    def test_expired_history_and_partial_batch_are_not_reported_as_current(self):
        clock, story = self.controlled()
        clock.tick = 12
        story.client.waiting = sample_sheet(1000, 1001)
        story.push(Image.new("RGB", (64, 48)), captured_at=1012)
        story.client.flushed = [sample_sheet(1000, 1012), sample_sheet(1012)]
        sheets = story.snapshot()
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0]["captured_at"], 1012)
        clock.tick = 15
        with self.assertRaisesRegex(CameraStoryboardError, "stale"):
            story.snapshot()

    def test_sampling_is_limited_and_model_errors_remain_errors(self):
        clock, story = self.controlled()
        image = Image.new("RGB", (64, 48))
        self.assertTrue(story.push(image))
        self.assertFalse(story.push(image))
        clock.tick = .25
        self.assertTrue(story.push(image))
        with self.assertRaisesRegex(CameraStoryboardError, "vision request failed"):
            describe_camera([sample_sheet(1000)], ["Known person"], lambda *_a, **_k: "[VLM failed]")

    def test_continuous_controlled_frames_reach_real_node_before_request(self):
        clock = Clock()
        with CameraStoryboard(width=384, height=216, fps=4, monotonic=clock.monotonic,
                              wall_time=clock.time) as story:
            for index in range(33):
                clock.tick = index / 4
                image = Image.new("RGB", (320, 240), ("red", "blue", "green")[(index // 2) % 3])
                story.push(image, captured_at=clock.time())
                time.sleep(.015)
            sheets = story.snapshot()
            times = [value for sheet in sheets for value in story._times(sheet)]
            self.assertGreater(len(times), 1)
            self.assertLess(min(times), max(times) - .4)
            self.assertEqual(max(times), 1008)
            self.assertTrue(all(998 <= value <= 1008 for value in times))
            self.assertLessEqual(len(sheets), 2)
            for sheet in sheets:
                self.assertEqual(Image.open(io.BytesIO(sheet["png"])).size, (384, 216))
                self.assertLessEqual(len(sheet["frames"]), 9)
                for index, cell in enumerate(sheet["cells"]):
                    self.assertEqual((cell["px"], cell["py"]), (index % 3 * 128, index // 3 * 72))
            calls = []
            def vision(images, prompt, image_labels):
                calls.append((images, prompt, image_labels))
                return "人物在移动，当前画面清晰。"
            result = describe_camera(sheets, ["Known person"], vision, "观察动作")
            self.assertEqual(result["faces"], ["Known person"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["layout"], "grid3")
            self.assertFalse(result["audio"])
            self.assertNotIn("scene_images", result)
            self.assertTrue(all(url.startswith("data:image/png;base64,") for url in calls[0][0]))
            self.assertEqual(len(calls[0][2]), len(sheets))
            self.assertTrue(base64.b64decode(result["scene_image"]).startswith(b"\x89PNG"))
        self.assertFalse(story.client.health()["running"])


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CameraProtocolTests(unittest.TestCase):
    def test_existing_reqrep_and_pub_with_controlled_capture(self):
        # Execute only the service function, excluding private face-library startup
        # and the real camera entrypoint. Its ZMQ sockets and Node bridge are real.
        source = (ROOT / "visual/0.py").read_text()
        tree = ast.parse(source)
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "camera_model"]
        stop, ready = threading.Event(), threading.Event()
        request_port, pub_port = free_port(), free_port()
        captures, stories, calls, errors = [], [], [], []

        class Capture:
            released = False
            count = 0

            def __init__(self, _device):
                captures.append(self)

            def isOpened(self):
                return True

            def set(self, *_):
                pass

            def read(self):
                time.sleep(.04)
                self.count += 1
                color = (20, 40, 230) if (self.count // 12) % 2 else (230, 60, 20)
                return True, np.full((240, 320, 3), color, dtype=np.uint8)

            def release(self):
                self.released = True

        fake_cv2 = types.SimpleNamespace(**{key: getattr(cv2, key) for key in dir(cv2)})
        fake_cv2.VideoCapture = Capture
        fake_cv2.destroyAllWindows = lambda: None

        def publisher(context, **_):
            pub = context.socket(zmq.PUB)
            pub.bind(f"tcp://127.0.0.1:{pub_port}")
            return pub

        def storyboard():
            value = CameraStoryboard(width=384, height=216)
            stories.append(value)
            return value

        def vision(images, prompt, **kwargs):
            calls.append((images, prompt, kwargs))
            return "[VLM failed]" if len(calls) == 2 else f"受控图像观察 {len(calls)}"

        namespace = {"cv2": fake_cv2, "threading": threading, "CameraStoryboard": storyboard,
                     "describe_camera": describe_camera, "_call_vision": vision,
                     "face_identify": lambda _frame: [((4, 4, 20, 20), "Known person")],
                     "ZMQ_PORTS": {"VISUAL_REQREP": request_port}, "create_publisher": publisher}
        exec(compile(tree, str(ROOT / "visual/0.py"), "exec"), namespace)
        def run():
            try:
                namespace["camera_model"](stop)
            except Exception as exc:
                errors.append(exc)
        context = zmq.Context()
        req, sub = context.socket(zmq.REQ), context.socket(zmq.SUB)
        req.setsockopt(zmq.RCVTIMEO, 5000)
        req.setsockopt(zmq.SNDTIMEO, 5000)
        sub.setsockopt(zmq.RCVTIMEO, 5000)
        sub.setsockopt(zmq.SUBSCRIBE, b"visual")
        worker = threading.Thread(target=run, daemon=True)
        try:
            with patch("common.module_ready.notify_module_ready", side_effect=lambda _: ready.set()):
                worker.start()
                self.assertTrue(ready.wait(5))
                req.connect(f"tcp://127.0.0.1:{request_port}")
                sub.connect(f"tcp://127.0.0.1:{pub_port}")
                time.sleep(1.3)
                req.send_json({"command": "look", "focus": "人物在做什么"})
                result = req.recv_json()
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(result["faces"], ["Known person"])
                self.assertEqual(result["layout"], "grid3")
                self.assertGreater(result["frame_count"], 1)
                topic, published = sub.recv_multipart()
                self.assertEqual(topic, b"visual")
                self.assertEqual(json.loads(published)["scene_caption"], result["scene_caption"])
                req.send_string("look")
                self.assertEqual(req.recv_json()["status"], "error")
                req.send_json({"command": "look"})
                self.assertEqual(req.recv_json()["status"], "success")
        finally:
            stop.set()
            worker.join(6)
            req.close(0)
            sub.close(0)
            context.term()
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(captures[0].released)
        self.assertFalse(stories[0].client.health()["running"])


if __name__ == "__main__":
    unittest.main()
