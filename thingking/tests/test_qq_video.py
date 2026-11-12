import base64
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import helpers
from PIL import Image
from chatBot.media import describe_videos, resolve_replies
from chatBot.video_decode import decode_video, _format
from tools.context import ToolContext, tool_context
from verification import napcat_integration as fixtures

event = fixtures.event
QQThinking = fixtures.QQThinking


def make_video(path, frames=50):
    import av
    with av.open(str(path), "w", format="mp4") as output:
        stream = output.add_stream("mpeg4", rate=25)
        stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
        for index in range(frames):
            image = Image.new("RGB", (160, 90), "red" if index < frames // 2 else "blue")
            frame = av.VideoFrame.from_image(image)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class DecodeTests(unittest.TestCase):
    def test_real_mp4_sampling_covers_start_end_and_keeps_timestamps(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.mp4"
            make_video(path)
            result = decode_video(path)
        self.assertEqual(len(result["frames"]), 6)
        self.assertAlmostEqual(result["duration_seconds"], 2, places=1)
        self.assertEqual(result["sample_times"][0], 0)
        self.assertGreater(result["sample_times"][-1], 1.8)
        self.assertEqual(result["sample_times"], sorted(result["sample_times"]))
        self.assertFalse(result["audio_observed"])
        pixels = [Image.open(BytesIO(base64.b64decode(frame.split(",", 1)[1]))).getpixel((40, 40))
                  for frame in (result["frames"][0], result["frames"][-1])]
        self.assertGreater(pixels[0][0], pixels[0][2] + 150)
        self.assertGreater(pixels[1][2], pixels[1][0] + 150)

    def test_one_frame_video_is_not_presented_as_six_distinct_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "short.mp4"
            make_video(path, frames=1)
            self.assertEqual(len(decode_video(path)["frames"]), 1)

    def test_corrupt_or_playlist_video_is_rejected(self):
        with self.assertRaises(ValueError):
            _format(b"#EXTM3U\nfile:///etc/passwd")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.mp4"
            path.write_bytes(b"\0\0\0\x20ftyp" + b"broken" * 100)
            with self.assertRaises(ValueError):
                decode_video(path)

    def test_decoder_timeout_is_bounded(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x"
            path.write_bytes(b"data")
            with patch("chatBot.video_decode.subprocess.run",
                       side_effect=subprocess.TimeoutExpired("decoder", 30)):
                with self.assertRaisesRegex(ValueError, "超时"):
                    decode_video(path)


class VideoTests(unittest.TestCase):
    setUp = fixtures.Integration.setUp
    tearDown = fixtures.Integration.tearDown
    insert = fixtures.Integration.insert

    def source(self, mid=30, group=None, **data):
        if not data:
            path = Path(self.temp.name) / "sample.mp4"
            make_video(path)
            data = {"file": "base64://" + base64.b64encode(path.read_bytes()).decode()}
        return event("2", group=group, msg_id=mid, message=[{"type": "video", "data": data}])

    def read(self, current, mid=None, method="frames", **params):
        eid = self.insert(current)
        with tool_context(ToolContext(self.store.identity(current), eid, eid)):
            return self.client.request("/video", {"method": method, "params": {
                "message_id": str(mid or current["message_id"]), **params}})

    def test_direct_video_reaches_real_decoder_and_vision(self):
        current = self.source()
        eid = self.insert(current)
        with tool_context(ToolContext(self.store.identity(current), eid, eid)), patch(
            "config.DEEPSEEK_API_KEY", "test"
        ), patch("vlm_client.call_vision", return_value="先红色，后蓝色") as vision:
            text = describe_videos([{**current, "event_id": eid}], self.client)
        self.assertIn("先红色，后蓝色", text)
        self.assertIn("未分析声音", text)
        self.assertEqual(len(vision.call_args.args[0]), 6)
        self.assertIn("1.96 秒", vision.call_args.kwargs["image_labels"][-1])

    def test_quoted_video_uses_the_referring_event_authority(self):
        original = self.source(mid=95, group="10")
        self.api.quoted["95"] = original
        current = event("2", group="10", msg_id=96, message=[
            {"type": "reply", "data": {"id": "95"}}, {"type": "at", "data": {"qq": "99"}}])
        eid = self.insert(current)
        identity = self.store.identity(current)
        with tool_context(ToolContext(identity, eid, eid)):
            _, quoted = resolve_replies([{**current, "event_id": eid}], self.client)
            # Mirror QQThinking's authorized source persistence.
            self.store.ingest(original)
            with patch("config.DEEPSEEK_API_KEY", "test"), patch(
                "vlm_client.call_vision", return_value="引用的视频由红变蓝"
            ) as vision:
                text = describe_videos(quoted, self.client)
        self.assertEqual(quoted[0]["source_event_id"], eid)
        self.assertIn("引用的视频由红变蓝", text)
        vision.assert_called_once()

    def test_unreferenced_or_foreign_peer_and_model_urls_are_denied(self):
        current = self.source()
        for mid, params in (("1234", {}), ("30", {"url": "http://127.0.0.1/secrets"})):
            with self.subTest(mid=mid), self.assertRaises(RuntimeError):
                self.read(current, mid=mid, **params)
        self.api.quoted["30"] = self.source(group="20")
        with self.assertRaises(RuntimeError):
            self.read(current)

    def test_current_or_source_recall_blocks_video(self):
        for recalled in (30, 31):
            with self.subTest(recalled=recalled):
                original = self.source()
                self.insert(original)
                current = event("2", msg_id=31, message=[{"type": "reply", "data": {"id": "30"}}])
                eid = self.insert(current)
                with self.store.connection() as db:
                    db.execute("UPDATE messages SET recalled=1 WHERE external_id=?",
                               (f"99:p:2:{recalled}",))
                with tool_context(ToolContext(self.store.identity(current), eid, eid)):
                    with self.assertRaises(RuntimeError):
                        self.client.request("/video", {"method": "frames", "params": {"message_id": "30"}})

    def test_recall_during_vision_discards_description(self):
        current = self.source()
        eid = self.insert(current)

        def vision(*_, **__):
            with self.store.connection() as db:
                db.execute("UPDATE messages SET recalled=1 WHERE external_id='99:p:2:30'")
            return "不得进入提示词的已撤回内容"

        with tool_context(ToolContext(self.store.identity(current), eid, eid)), patch(
            "config.DEEPSEEK_API_KEY", "test"
        ), patch("vlm_client.call_vision", side_effect=vision):
            text = describe_videos([{**current, "event_id": eid}], self.client)
        self.assertNotIn("不得进入提示词", text)
        self.assertIn("未看清", text)

    def test_private_download_arbitrary_path_and_oversized_base64_are_denied(self):
        from chatBot.video import MAX_BYTES
        from chatBot.stickers import download_image
        service = self.server.videos
        with patch("chatBot.stickers._public_addresses", side_effect=ValueError("private")):
            with self.assertRaises(ValueError):
                download_image("http://127.0.0.1/video", max_bytes=MAX_BYTES)
        with self.assertRaises(ValueError):
            service._bytes({"file": "/etc/passwd"})
        with self.assertRaises(ValueError):
            service._bytes({"file": "base64://" + "A" * (((MAX_BYTES + 2) // 3 * 4) + 1)})
        self.assertIsNone(service._local_path("/bines-media/../../../etc/passwd"))

    def test_native_file_id_resolution_and_mounted_video_path(self):
        from chatBot.config import ROOT
        data = base64.b64encode(b"test-video").decode()
        with patch.object(self.api, "raw_api", return_value={"status": "ok", "data": {"base64": data}}) as api:
            self.assertEqual(self.server.videos._bytes({"file": "opaque.mp4"}), b"test-video")
            api.assert_called_once_with("get_file", {"file_id": "opaque.mp4"}, timeout=15)
        self.assertEqual(self.server.videos._local_path("/app/.config/QQ/Video/x.mp4"),
                         Path(ROOT) / "deploy/napcat/data/qq/Video/x.mp4")

    def test_video_failure_leaves_image_analysis_and_user_text_in_prompt(self):
        current = self.source(file="invalid-video-id")
        current["message"].extend([
            {"type": "image", "data": {"url": "https://qq.example/image.png"}},
            {"type": "text", "data": {"text": "这张图片也看看"}}])
        self.insert(current)
        rows = self.store.claim("worker", 0, 0)

        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                text = str(conversation)
                self.assertIn("这张图片也看看", text)
                self.assertIn("图片里的猫", text)
                self.assertIn("未看清", text)
                return "看到了猫", [], []

        with patch("config.DEEPSEEK_API_KEY", "test"), patch(
            "vlm_client.call_vision", return_value="图片里的猫"
        ), patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            QQThinking(self.store, self.client).process(rows, Model())

    def test_full_qq_turn_includes_real_video_analysis(self):
        self.insert(self.source())
        rows = self.store.claim("worker", 0, 0)

        class Model:
            def run_skill_protocol(inner, conversation, runner, **kwargs):
                self.assertIn("视频观察结果", str(conversation))
                self.assertIn("先红色后蓝色", str(conversation))
                self.assertIn("1.96s", str(conversation))
                return "画面颜色变了", [], []

        with patch("config.DEEPSEEK_API_KEY", "test"), patch(
            "vlm_client.call_vision", return_value="先红色后蓝色"
        ), patch("tools.napcat_tool.GatewayClient", return_value=self.client):
            QQThinking(self.store, self.client).process(rows, Model())

    def test_uncertain_validation_never_passes_stale_video_to_vision(self):
        from unittest.mock import Mock
        client = Mock()
        client.request.side_effect = [{"frames": ["frame"], "sample_times": [0], "duration_seconds": 1},
                                      {"status": "uncertain"}]
        with patch("config.DEEPSEEK_API_KEY", "test"), patch("vlm_client.call_vision") as vision:
            text = describe_videos([self.source()], client)
        vision.assert_not_called()
        self.assertIn("未看清", text)


if __name__ == "__main__":
    unittest.main()
