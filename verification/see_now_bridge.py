"""Real local Node bridge checks using synthetic RGB frames; no capture or model calls."""
import io
import json
import struct
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from common.see_now import BRIDGE_PATH, DESKTOP_OPTIONS, SeeNowClient, SeeNowError, find_node_binary


class SeeNowBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = find_node_binary()

    def client(self, **options):
        return SeeNowClient(128, 72, 4, options, node_binary=self.node)

    def test_rgb_png_and_capture_time_round_trip(self):
        with self.client(maxFrames=1, strategy="single") as client:
            self.assertIsNone(client.take())
            self.assertIsNone(client.flush())
            original = Image.new("RGB", (128, 72), (23, 137, 211))
            self.assertTrue(client.push(original.tobytes(), captured_at=1000, sample_time=0))
            sheet = client.flush()
            image = Image.open(io.BytesIO(sheet["png"]))
            self.assertEqual(image.size, (128, 72))
            cell = sheet["cells"][0]
            self.assertEqual(image.getpixel((cell["px"] + cell["pw"] - 5,
                                             cell["py"] + cell["ph"] - 5)), (23, 137, 211))
            self.assertEqual(sheet["captured_at"], 1000)
            self.assertEqual(sheet["wallTo"], 1000000)
            self.assertEqual(sheet["frames"][-1]["t"], 0)
            self.assertEqual(len(sheet["frames"]), len(sheet["cells"]))
            self.assertTrue(client.health()["running"])

    def test_automatic_output_and_latest_force_frame(self):
        with self.client(maxFrames=1, strategy="single", oneShot=True) as client:
            client.push(Image.new("RGB", (128, 72), "red"), captured_at=1000, sample_time=0)
            client.push(Image.new("RGB", (128, 72), "red"), captured_at=1000.25, sample_time=0.25)
            deadline = time.monotonic() + 3
            first = None
            while first is None and time.monotonic() < deadline:
                first = client.take()
                time.sleep(0.01)
            self.assertIsNotNone(first)
            for i in range(40):
                client.push(Image.new("RGB", (128, 72), "blue"), captured_at=1001 + i / 4,
                            sample_time=1 + i / 4)
            latest = client.flush()
            self.assertEqual(latest["captured_at"], 1010.75)
            self.assertEqual(latest["frames"][-1]["t"], 10.75)
            cell = latest["cells"][-1]
            self.assertEqual(Image.open(io.BytesIO(latest["png"])).getpixel(
                (cell["px"] + cell["pw"] - 5, cell["py"] + cell["ph"] - 5)), (0, 0, 255))
            self.assertIsNone(client.take())

    def test_input_validation_and_safe_options(self):
        with self.client(audioStrip=False, queueDepth=1, maxFrames=16, layout="adaptive") as client:
            with self.assertRaises(ValueError):
                client.push(b"short")
            with self.assertRaises(ValueError):
                client.push(bytes(128 * 72 * 3), float("nan"))
            with self.assertRaises(ValueError):
                client.flush(timeout=0)
        client = self.client(queueDepth=2)
        with self.assertRaisesRegex(SeeNowError, "numeric parameter"):
            client.start()
        self.assertFalse(client.health()["running"])
        self.assertIsNotNone(client._process.poll())

    def test_close_and_child_failure_release_resources(self):
        client = self.client().start()
        process = client._process
        client.close()
        client.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(not thread.is_alive() for thread in client._threads))
        with self.assertRaises(SeeNowError):
            client.push(bytes(128 * 72 * 3))
        with self.client() as failed:
            failed._process.terminate()
            failed._process.wait(timeout=3)
            with self.assertRaises(SeeNowError):
                failed.take()

    def test_explicit_old_node_is_rejected(self):
        with patch("common.see_now._version", return_value=(18, 20, 8)):
            with self.assertRaisesRegex(SeeNowError, ">=20"):
                find_node_binary(self.node)

    def test_startup_timeout_terminates_the_waiting_child(self):
        client = SeeNowClient(128, 72, 4, node_binary=self.node, timeout=0.1)
        with patch.object(client, "_write_loop", return_value=None):
            with self.assertRaisesRegex(SeeNowError, "timed out"):
                client.start()
        self.assertIsNotNone(client._process.poll())
        self.assertTrue(all(not thread.is_alive() for thread in client._threads))

    def test_wall_clock_rollback_does_not_reverse_algorithm_time(self):
        with self.client() as client:
            client.push(bytes(128 * 72 * 3), captured_at=1000)
            first = client._last_t
            time.sleep(0.01)
            client.push(bytes(128 * 72 * 3), captured_at=999)
            self.assertGreater(client._last_t, first)
            with self.assertRaises(ValueError):
                client.push(bytes(128 * 72 * 3), captured_at=1001, sample_time=0)
            self.assertEqual(client.flush()["captured_at"], 999)

    def test_pending_batch_obeys_deadline_when_later_frames_are_static(self):
        with self.client(**{**DESKTOP_OPTIONS, "maxWaitSec": 1}) as client:
            for index in range(5):
                client.push(Image.new("RGB", (128, 72), "white"),
                            captured_at=1000 + index / 4, sample_time=index / 4)
                time.sleep(0.02)
            deadline = time.monotonic() + 2
            sheet = None
            while sheet is None and time.monotonic() < deadline:
                sheet = client.take()
                time.sleep(0.01)
            self.assertIsNotNone(sheet)
            self.assertEqual(sheet["why"], "time")
            self.assertEqual(sheet["captured_at"], 1001)

    def test_close_during_node_discovery_reaps_late_child(self):
        entered, release = threading.Event(), threading.Event()
        client = self.client()
        failures = []
        def discover(_):
            entered.set()
            release.wait(3)
            return self.node
        def start():
            try:
                client.start()
            except Exception as exc:
                failures.append(exc)
        with patch("common.see_now.find_node_binary", side_effect=discover):
            starter = threading.Thread(target=start)
            starter.start()
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=client.close)
            closer.start()
            release.set()
            starter.join(3)
            closer.join(3)
        self.assertFalse(failures)
        self.assertFalse(starter.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertIsNotNone(client._process.poll())

    def test_bridge_rejects_oversized_header_without_reading_payload(self):
        result = subprocess.run([self.node, str(BRIDGE_PATH)],
                                input=struct.pack(">I", 8193), capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        message = json.loads(result.stdout)
        self.assertEqual(message["event"], "error")
        self.assertNotIn("stack", message)

    def test_future_frame_buffer_stays_bounded_with_overdense_input(self):
        # Exercise the upstream lookahead itself: a too-fast producer must not keep
        # all raw RGB/HSV frames while waiting for half a second of source time.
        script = """
import { createPipeline } from './vendor/see_now/src/pipeline.js';
import { randomFillSync } from 'node:crypto';
const pipeline = createPipeline({width:128,height:72,fps:4,audioStrip:false});
for (let i = 0; i < 1500; i++) {
  pipeline.push({t:i * 0.000001,buf:randomFillSync(Buffer.alloc(128 * 72 * 3))});
  pipeline.log.length = 0;
}
global.gc(); global.gc();
console.log(JSON.stringify({external:process.memoryUsage().external,frames:pipeline.stats.frames}));
"""
        result = subprocess.run([self.node, "--expose-gc", "--input-type=module", "-e", script],
                                cwd=BRIDGE_PATH.parents[2], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        usage = json.loads(result.stdout)
        self.assertEqual(usage["frames"], 1500)
        self.assertLess(usage["external"], 32 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
