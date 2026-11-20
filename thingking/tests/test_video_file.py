"""Real controlled MP4 → PyAV → Node tests; vision is deliberately not called."""
from contextlib import closing, redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import helpers
from PIL import Image

from common.video_file import VideoFile, coverage_note


def make_changing_video(path, seconds=14, rate=8):
    import av
    colors = ["red", "blue", "lime", "yellow", "magenta", "cyan", "white", "black"]
    with av.open(str(path), "w", format="mp4") as output:
        stream = output.add_stream("mpeg4", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 160, 90, "yuv420p"
        for index in range(round(seconds * rate)):
            image = Image.new("RGB", (160, 90), colors[(index // rate) % len(colors)])
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class VideoFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "controlled.mp4"
        make_changing_video(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_real_decoder_and_node_emit_multiple_nine_cell_sheets(self):
        with VideoFile(self.path) as source:
            sheets = list(source.storyboards())
            metadata = dict(source.metadata)
        self.assertGreaterEqual(len(sheets), 2)
        self.assertTrue(all(sheet["layout"] == "grid3" for sheet in sheets))
        self.assertTrue(all(1 <= len(sheet["frames"]) <= 9 for sheet in sheets))
        self.assertTrue(any(len(sheet["frames"]) == 9 for sheet in sheets))
        self.assertTrue(metadata["sampling_complete"])
        self.assertFalse(metadata["storyboard_limit_reached"])
        self.assertGreater(metadata["processed_until_seconds"], 13.5)
        selected = [frame["t"] for sheet in sheets for frame in sheet["frames"]]
        self.assertEqual(selected, sorted(selected))
        self.assertGreater(max(selected), 12)
        self.assertIsNone(source.container)

    def test_reduced_sampling_still_visits_the_end_and_reports_density(self):
        with VideoFile(self.path, max_samples=6) as source:
            frames = list(source.frames())
            metadata = dict(source.metadata)
        self.assertEqual(len(frames), 6)
        self.assertEqual(frames[0][0], 0)
        self.assertGreater(frames[-1][0], 13.5)
        self.assertTrue(metadata["sampling_reduced"])
        self.assertIn("降低采样密度", coverage_note(metadata))

    def test_sheet_budget_marks_unanalyzed_tail_and_closes_decoder(self):
        with VideoFile(self.path) as source:
            sheets = list(source.storyboards(max_storyboards=1))
            metadata = dict(source.metadata)
        self.assertEqual(len(sheets), 1)
        self.assertTrue(metadata["storyboard_limit_reached"])
        self.assertLess(metadata["selected_until_seconds"], metadata["duration_seconds"] - 1)
        self.assertIn("后续画面未分析", metadata["coverage_note"])
        self.assertIsNone(source.container)

    def test_stream_is_lazy_and_node_is_closed_when_consumer_stops(self):
        from common.see_now import SeeNowClient
        processes = []
        original = SeeNowClient.start

        def start(client):
            result = original(client)
            processes.append(client._process)
            return result

        with patch.object(SeeNowClient, "start", start), VideoFile(self.path) as source:
            with closing(source.storyboards()) as sheets:
                next(sheets)
                self.assertLess(len(source.metadata["sample_times"]), source.metadata["planned_samples"])
            self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertIsNone(source.container)

    def test_decode_budget_and_before_decode_boundary(self):
        calls = []
        with VideoFile(self.path, max_decoded_pixels=1,
                       before_decode=lambda: calls.append("limited")) as source:
            with self.assertRaisesRegex(ValueError, "解码预算"):
                next(source.frames())
        self.assertEqual(calls, ["limited"])
        self.assertIsNone(source.container)

    def test_partial_pixel_budget_returns_a_labeled_tail_page(self):
        with VideoFile(self.path, max_decoded_pixels=160 * 90 * 12) as source:
            sheets = list(source.storyboards())
            self.assertEqual(len(sheets), 1)
            self.assertTrue(sheets[0]['frames'])
            self.assertTrue(source.metadata['decode_budget_reached'])
            self.assertFalse(source.metadata['sampling_complete'])
            self.assertIn('后续画面未分析', source.metadata['coverage_note'])
            self.assertGreater(source.metadata['processed_until_seconds'], 0)

    def test_rotation_preserves_portrait_display_dimensions(self):
        image = Image.new("RGB", (160, 90), "red")
        frame = SimpleNamespace(width=160, height=90, time=0, rotation=90,
                                to_image=lambda: image.copy())
        stream = SimpleNamespace(metadata={}, time_base=.04)
        from unittest.mock import Mock
        container = Mock()
        container.decode.return_value = iter([frame])
        source = VideoFile(self.path)
        source.container, source.stream = container, stream
        source.targets, source.start, source.rate = [0], 0, 25
        source.metadata = {"decoded_pixels": 0, "sample_times": [], "processed_from_seconds": None}
        try:
            frames = list(source.frames())
            self.assertEqual(frames[0][1].size, (90, 160))
        finally:
            source.close()
        container.close.assert_called_once()

    def test_linux_memory_limit_wraps_probe_and_decode_but_not_node_start(self):
        import sys
        if sys.platform == "win32":
            self.skipTest("POSIX resource limits")
        import resource
        from chatBot.video_decode import _decode
        from common.see_now import SeeNowClient
        events = []
        original_start = SeeNowClient.start
        original_enter = VideoFile.__enter__

        def start(client):
            events.append("node_start")
            return original_start(client)

        def enter(source):
            events.append("av_open")
            return original_enter(source)

        def limit(_kind, values):
            events.append("limited" if values[0] == 2 * 1024**3 else "restored")

        # Exercise the Linux branch on this host without changing its real
        # limits. The real decoder and Node still run; syscall ordering is tested.
        with patch("chatBot.video_decode.sys.platform", "linux"), \
                patch.object(resource, "getrlimit", return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                patch.object(resource, "setrlimit", side_effect=limit), \
                patch.object(SeeNowClient, "start", start), patch.object(VideoFile, "__enter__", enter):
            result = _decode(self.path)
        self.assertGreater(len(result["frames"]), 1)
        self.assertEqual(events[:5], ["limited", "av_open", "restored", "node_start", "limited"])

    def test_cli_streams_multiple_sheets_and_appends_scope(self):
        import video_analysis_standalone as cli
        output = Path(self.temp.name) / "analysis.txt"
        with patch.object(cli, "call_vision", return_value="受控画面变化") as vision, \
                redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()):
            metadata = cli.run(str(self.path), output_path=str(output))
        self.assertGreaterEqual(vision.call_count, 2)
        self.assertEqual(vision.call_count, metadata["storyboard_count"])
        self.assertTrue(vision.call_args.args[0].startswith("data:image/png;base64,"))
        self.assertIn("九宫格", vision.call_args.args[1])
        self.assertIn("未分析声音", output.read_text())
        self.assertIn("受控画面变化", stdout.getvalue())

    def test_cli_does_not_overwrite_input_or_accept_invalid_interval(self):
        from video_analysis_standalone import run
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "覆盖"):
            run(str(self.path), output_path=str(self.path))
        with self.assertRaisesRegex(ValueError, "间隔"):
            run(str(self.path), interval_sec=0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_later_sheet_preserves_previous_report(self):
        import video_analysis_standalone as cli
        output = self.path.with_suffix(".txt")
        output.write_text("已有报告", encoding="utf-8")
        with patch.object(cli, "call_vision", side_effect=["第一张正常", "[VLM 请求失败]"]), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(RuntimeError):
            cli.run(str(self.path), output_path=output)
        self.assertEqual(output.read_text(encoding="utf-8"), "已有报告")
        self.assertEqual(list(output.parent.glob(f".{output.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
