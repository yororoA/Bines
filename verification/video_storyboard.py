"""Real Node nine-cell selection, temporal metadata and bounded finite replay."""
import io
import unittest
from unittest.mock import patch

from PIL import Image

from common.see_now import SeeNowClient, SeeNowError
from common.video_storyboard import iter_storyboards, sheet_data_url, sheet_label


SELECT_ALL = {"cutAbs": 0, "cutSpike": 0.1, "dupMin": 0,
              "integralSeconds": 0.1, "markMinGap": 0.01}


def source(count, offset=0):
    for i in range(count):
        color = ((i * 59) % 256, (i * 101) % 256, (i * 149) % 256)
        yield offset + i / 2, Image.new("RGB", (126, 72), color)


class StoryboardTests(unittest.TestCase):
    def sheets(self, count, offset=0):
        return list(iter_storyboards(source(count, offset), width=126, height=72,
                                     options=SELECT_ALL))

    def test_nine_cell_layout_and_multiple_pages_keep_all_selected_frames(self):
        sheets = self.sheets(20)
        self.assertEqual([len(s["frames"]) for s in sheets], [9, 9, 2])
        self.assertEqual([f["t"] for s in sheets for f in s["frames"]], [i / 2 for i in range(20)])
        for sheet in sheets:
            self.assertEqual((sheet["layout"], sheet["rows"], sheet["columns"]), ("grid3", 3, 3))
            image = Image.open(io.BytesIO(sheet["png"]))
            self.assertEqual(image.size, (126, 72))
            for i, (frame, cell) in enumerate(zip(sheet["frames"], sheet["cells"])):
                self.assertEqual((cell["px"], cell["py"], cell["pw"], cell["ph"]),
                                 (i % 3 * 42, i // 3 * 24, 42, 24))
                n = int(frame["t"] * 2)
                self.assertEqual(image.getpixel((cell["px"] + 36, cell["py"] + 18)),
                                 ((n * 59) % 256, (n * 101) % 256, (n * 149) % 256))

    def test_partial_tail_is_empty_padding_not_repeated_frames(self):
        sheets = self.sheets(11)
        self.assertEqual([len(s["frames"]) for s in sheets], [9, 2])
        image = Image.open(io.BytesIO(sheets[-1]["png"]))
        self.assertEqual(image.getpixel((105, 60)), (42, 42, 46))
        self.assertIn("实际2帧", sheet_label(sheets[-1], "片尾"))
        self.assertTrue(sheet_data_url(sheets[-1]).startswith("data:image/png;base64,"))

    def test_full_last_sheet_is_not_duplicated_at_finish(self):
        self.assertEqual([len(s["frames"]) for s in self.sheets(18)], [9, 9])

    def test_sparse_samples_do_not_repeat_across_time_cutoffs(self):
        for gap in (31, 121):
            frames = [(i * gap, image) for i, (_, image) in enumerate(source(15))]
            sheets = list(iter_storyboards(frames, width=126, height=72, options=SELECT_ALL))
            self.assertEqual([f['t'] for s in sheets for f in s['frames']], [t for t, _ in frames])

    def test_numbered_badges_render_different_glyphs_only_in_occupied_cells(self):
        # Compare yellow pixel masks with the background excluded, rather than
        # merely checking layout metadata. All nine badges must be distinct.
        full, partial = self.sheets(10)
        def masks(sheet):
            img = Image.open(io.BytesIO(sheet['png']))
            shapes = []
            for i in range(9):
                x, y = i % 3 * 42, i // 3 * 24
                shapes.append(tuple(j for j,(r,g,b) in enumerate(img.crop((x,y,x+16,y+20)).getdata())
                                    if r >= 240 and g >= 200 and b < 110))
            return shapes
        glyphs = masks(full)
        self.assertTrue(all(glyphs))
        self.assertEqual(len(set(glyphs)), 9)
        self.assertEqual(masks(partial)[1:], [()] * 8)

    def test_source_offsets_and_precise_wall_clock_are_preserved(self):
        sheets = self.sheets(3, offset=73.123456)
        self.assertEqual([f["t"] for s in sheets for f in s["frames"]],
                         [73.123456 + i / 2 for i in range(3)])
        with SeeNowClient(126, 72, 4, {"queueDepth": 0}) as client:
            client.process(Image.new("RGB", (126, 72)), sample_time=0.001, captured_at=1001)
            client.process(Image.new("RGB", (126, 72), "white"), sample_time=0.002, captured_at=1002)
            sheet = client.flush()
            self.assertEqual(sheet["frames"][-1]["t"], 0.002)
            self.assertEqual(sheet["captured_at"], 1002)

    def test_static_sources_are_filtered_not_blindly_tiled(self):
        sheets = list(iter_storyboards(((i / 4, Image.new("RGB", (126, 72), "navy"))
                                       for i in range(30)), width=126, height=72))
        self.assertLess(sum(len(s["frames"]) for s in sheets), 30)

    def test_portrait_edges_are_preserved_with_letterboxing(self):
        portrait = Image.new("RGB", (60, 120), "red")
        sheets = list(iter_storyboards([(0, portrait)], width=126, height=72))
        image = Image.open(io.BytesIO(sheets[0]["png"]))
        self.assertEqual(image.getpixel((20, 19)), (255, 0, 0))
        self.assertEqual(image.getpixel((3, 19)), (0, 0, 0))

    def test_early_close_releases_source_and_node(self):
        clients, closed = [], []
        def factory(*args, **kwargs):
            client = SeeNowClient(*args, **kwargs)
            clients.append(client)
            return client
        def frames():
            try:
                yield from source(30)
            finally:
                closed.append(True)
        with patch("common.video_storyboard.SeeNowClient", side_effect=factory):
            iterator = iter_storyboards(frames(), width=126, height=72, options=SELECT_ALL)
            next(iterator)
            iterator.close()
        self.assertEqual(closed, [True])
        self.assertFalse(clients[0].health()["running"])

    def test_invalid_grid_capacity_is_rejected(self):
        with self.assertRaisesRegex(SeeNowError, "at most 9"):
            SeeNowClient(126, 72, 4, {"maxFrames": 10}).start()


if __name__ == "__main__":
    unittest.main()
