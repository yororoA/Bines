"""Isolated SQLite/assets and stub transport; no real model, QQ, or network calls."""
from contextlib import contextmanager
import io
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import helpers  # noqa: F401
from PIL import Image
from chatBot.messages import Identity, LOCAL_IDENTITY
from chatBot.stickers import StickerLibrary, MAX_BYTES, download_image


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def picture(format="PNG", animated=False):
    buf = io.BytesIO()
    first = Image.new("RGB", (3, 3), "red")
    if animated:
        first.save(buf, format=format, save_all=True, append_images=[Image.new("RGB", (3, 3), "blue")], duration=120, loop=0)
    else:
        first.save(buf, format=format)
    return buf.getvalue()


class StickerLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bines-stickers-test-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"BINES_ENV_FILE": "/dev/null", "BINES_MEMORY_DIR": str(self.root / "memory"),
            "BINES_GATEWAY_DIR": str(self.root / "gateway"), "BINES_HEALTH_DIR": str(self.root / "health"),
            "BINES_MODEL_USAGE_PATH": str(self.root / "usage.db"), "BINES_WEBSITE_BRIDGE_SPOOL": str(self.root / "spool")})
        self.env.start()
        self.store = Store(self.root / "gateway.db")
        self.media = self.root / "media"
        self.library = StickerLibrary(self.store, self.media)
        self.owner = Identity("bot", "owner", owner=True)
        self.group_owner = Identity("bot", "owner", group="group-a", owner=True)
        self.group_member = Identity("bot", "friend", group="group-a")
        self.other = Identity("bot", "other")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def save(self, identity=None, binary=None, **kwargs):
        return self.library.save(identity or self.owner, {"type": "image", "data": {}},
                                 kwargs.pop("description", "委屈猫猫"), kwargs.pop("tags", ["委屈", "道歉"]),
                                 image_bytes=picture() if binary is None else binary, **kwargs)

    def test_preserves_animated_gif_bytes_deduplicates_and_reopens(self):
        raw = picture("GIF", animated=True)
        first = self.save(binary=raw)
        self.assertTrue(first["animated"])
        second = self.save(binary=raw, description="猫猫道歉")
        self.assertEqual(first["id"], second["id"])
        files = list((self.media / "stickers").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), raw)
        reopened = StickerLibrary(Store(self.store.path), self.media)
        self.assertEqual(reopened.search(self.owner)[0]["description"], "猫猫道歉")
        self.assertEqual(reopened.resolve(self.owner, first["id"])["data"]["file"], "/bines-media/stickers/" + files[0].name)

    def test_private_group_and_global_scopes_enforced_even_for_owner_in_group(self):
        private = self.save()
        self.assertEqual(self.library.search(LOCAL_IDENTITY)[0]["id"], private["id"])
        for identity in (self.group_owner, self.other):
            self.assertEqual(self.library.search(identity), [])
            with self.assertRaises(PermissionError): self.library.resolve(identity, private["id"])
        group = self.save(self.group_member)
        self.assertEqual(self.library.search(self.group_owner)[0]["id"], group["id"])
        with self.assertRaises(PermissionError): self.save(self.group_owner, shared=True)
        shared = self.save(shared=True)
        self.assertEqual(self.library.search(self.other)[0]["id"], shared["id"])
        with self.assertRaises(PermissionError): self.library.update(self.other, shared["id"], description="改掉")
        with self.assertRaises(PermissionError): self.library.update_sync(self.other, shared["id"], "confirmed")

    def test_shared_requires_boolean_not_truthy_text_or_numbers(self):
        for shared in ("false", "true", 0, 1, None):
            with self.subTest(shared=shared), self.assertRaises(ValueError): self.save(shared=shared)
        self.assertEqual(self.library.search(self.owner), [])

    def test_source_revoked_during_download_is_rechecked_before_any_write(self):
        state = {"allowed": True}

        def download(*_args, **_kwargs):
            state["allowed"] = False
            return picture()

        def revalidate():
            if not state["allowed"]:
                raise PermissionError("source recalled")

        library = StickerLibrary(self.store, self.media, downloader=download)
        with self.assertRaises(PermissionError):
            library.save(self.owner, {"type": "image", "data": {"url": "https://authorized.example/image"}},
                         "图", [], before_save=revalidate)
        denied = Mock(return_value=False)
        with self.assertRaises(PermissionError):
            library.save(self.owner, {"type": "face", "data": {"id": 14}}, "表情", [], before_save=denied)
        denied.assert_called_once()
        self.assertEqual(library.search(self.owner), [])
        self.assertEqual(list((self.media / "stickers").iterdir()), [])

    def test_source_is_rechecked_under_write_lock_before_asset_and_record(self):
        callback = Mock(side_effect=[True, False])
        with self.assertRaises(PermissionError): self.save(before_save=callback)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(self.library.search(self.owner), [])
        self.assertEqual(list((self.media / "stickers").iterdir()), [])
        calls = []

        def check():
            with self.store.connection() as db:
                db.execute("SELECT COUNT(*) FROM sticker_library").fetchone()
            if calls:
                writer = sqlite3.connect(self.store.path, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError): writer.execute("BEGIN IMMEDIATE")
                finally:
                    writer.close()
            calls.append(True)
            return True

        self.save(before_save=check)
        self.assertEqual(len(calls), 2)

    def test_guessed_file_path_does_not_bypass_scope_or_tamper_checks(self):
        item = self.save()
        source = self.library.resolve(self.owner, item["id"])["data"]["file"]
        self.assertTrue(self.library.authorize_path(self.owner, source))
        self.assertFalse(self.library.authorize_path(self.other, source))
        self.assertFalse(self.library.authorize_path(self.owner, source + "/../secret"))
        path = self.media / "stickers" / source.rsplit("/", 1)[1]
        path.write_bytes(b"altered")
        self.assertFalse(self.library.authorize_path(self.owner, source))
        with self.assertRaises(ValueError): self.library.resolve(self.owner, item["id"])

    def test_signature_damage_and_size_rejected_before_storage(self):
        for raw in (b"<svg></svg>", b"GIF89aBAD", b"\x89PNG\r\n\x1a\nBAD", b"x" * (MAX_BYTES + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError): self.save(binary=raw)
        self.assertEqual(self.library.search(self.owner), [])
        self.assertEqual(list((self.media / "stickers").iterdir()), [])

    def test_damaged_later_animation_frame_and_decode_budget_are_rejected(self):
        raw = picture("GIF", animated=True)
        with self.assertRaises(ValueError): self.save(binary=raw[:-3])
        with patch("chatBot.stickers.MAX_FRAMES", 1):
            with self.assertRaises(ValueError): self.save(binary=raw)
        with patch("chatBot.stickers.MAX_DECODED_PIXELS", 10):
            with self.assertRaises(ValueError): self.save(binary=raw)
        self.assertEqual(self.library.search(self.owner), [])
        self.assertEqual(list((self.media / "stickers").iterdir()), [])

    def test_native_faces_keep_send_structure_but_hide_keys_in_metadata(self):
        for segment in ({"type": "face", "data": {"id": 14, "resultId": 1}},
                        {"type": "mface", "data": {"emoji_package_id": "10", "emoji_id": "abc", "key": "secret", "url": "https://example.invalid/private"}}):
            item = self.library.save(self.owner, segment, "开心", ["笑"])
            result = self.library.resolve(self.owner, item["id"])
            self.assertEqual(result["type"], segment["type"])
            self.assertNotIn("url", result["data"])
            self.assertNotIn("secret", json.dumps(self.library.search(self.owner)))
            self.assertEqual(item["qq_sync"], "not_applicable")
        for segment in ({"type": "face", "data": {"id": None}}, {"type": "mface", "data": {"key": "missing"}}):
            with self.assertRaises(ValueError): self.library.save(self.owner, segment, "坏", [])

    def test_metadata_updates_search_chinese_substrings_and_limit(self):
        wanted = self.save()
        for n in range(7):
            self.library.save(self.owner, {"type": "face", "data": {"id": n}}, "开心笑脸", ["笑"])
        self.assertEqual(self.library.search(self.owner, "委屈的猫猫")[0]["id"], wanted["id"])
        self.assertEqual(len(self.library.search(self.owner, limit=99)), 5)
        self.library.update(self.owner, wanted["id"], description="低头认错", tags=["道歉"])
        synced = self.library.update_sync(self.owner, wanted["id"], "confirmed", "https://example.invalid/private?k=secret")
        self.assertEqual(synced["description"], "低头认错")
        self.assertEqual(synced["qq_sync"], "confirmed")
        self.assertNotIn("https://", json.dumps(synced))
        self.assertEqual(self.save()["qq_sync"], "confirmed")
        self.assertEqual(self.library.search(self.owner, "不存在"), [])

    def test_labels_and_internal_source_provenance_never_expose_urls(self):
        for kwargs in ({"description": "字" * 161}, {"tags": ["a"] * 9}, {"tags": ["a" * 33]},
                       {"tags": "abc"}, {"description": "https://example.invalid/x"}):
            with self.assertRaises(ValueError): self.save(**kwargs)
        item = self.save(source_message_id="source-1")
        self.assertNotIn("source_message_id", item)
        with self.store.connection() as db:
            meta = json.loads(db.execute("SELECT metadata FROM sticker_library").fetchone()[0])
        self.assertEqual(meta["source_message_id"], "source-1")
        self.assertEqual(meta["source_scope"], self.owner.scope)

    def test_favorite_md5_checked_before_file_or_metadata_write(self):
        raw = picture()
        with self.assertRaisesRegex(ValueError, "MD5"):
            self.save(binary=raw, expected_md5="0" * 32)
        self.assertEqual(self.library.search(self.owner), [])
        self.assertEqual(list((self.media / "stickers").iterdir()), [])
        saved = self.save(binary=raw, expected_md5=hashlib.md5(raw).hexdigest().upper())
        self.assertEqual(saved["md5"], hashlib.md5(raw).hexdigest())

    def test_download_injection_does_not_persist_url_and_keeps_original_bytes(self):
        download = Mock(return_value=picture("GIF", animated=True))
        library = StickerLibrary(self.store, self.media, downloader=download)
        item = library.save(self.owner, {"type": "image", "data": {"url": "https://authorized.example/image?signature=private"}}, "图", [])
        self.assertTrue(item["animated"])
        with self.store.connection() as db:
            row = dict(db.execute("SELECT * FROM sticker_library").fetchone())
        self.assertNotIn("signature", json.dumps(row))
        self.assertEqual(download.call_args.kwargs["max_bytes"], MAX_BYTES)

    def test_local_path_requires_resolved_approved_media_path(self):
        outside = self.root / "outside.png"; outside.write_bytes(picture())
        inside = self.media / "approved.png"; inside.write_bytes(picture())
        for source in (outside, str(inside), inside.as_uri()):
            with self.assertRaises(PermissionError):
                self.library.save(self.owner, {"type": "image", "data": {"file": source}}, "图", [])
        link = self.media / "link.png"; link.symlink_to(outside)
        with self.assertRaises(PermissionError):
            self.library.save(self.owner, {"type": "image", "data": {"file": link}}, "图", [])
        self.library.save(self.owner, {"type": "image", "data": {"file": inside.resolve()}}, "图", [])


class StickerDownloadTests(unittest.TestCase):
    @staticmethod
    def addr(ip):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    def test_private_dns_and_mixed_answers_never_connect(self):
        for answers in (self.addr("127.0.0.1"), self.addr("169.254.169.254"), self.addr("224.0.0.1"),
                        self.addr("2002:7f00:1::"), self.addr("93.184.216.34") + self.addr("10.0.0.1")):
            with patch("chatBot.stickers.socket.getaddrinfo", return_value=answers), patch("chatBot.stickers.socket.create_connection") as connect:
                with self.assertRaises(ValueError): download_image("http://example.test/a")
                connect.assert_not_called()

    def test_redirect_is_rechecked_before_private_connection(self):
        response = Mock(status=302); response.getheader.return_value = "http://127.0.0.1/secret"
        connection = Mock(); connection.getresponse.return_value = response
        with patch("chatBot.stickers.socket.getaddrinfo", side_effect=[self.addr("93.184.216.34"), self.addr("127.0.0.1")]), patch("chatBot.stickers.socket.create_connection") as connect, patch("chatBot.stickers.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(ValueError): download_image("http://public.test/a")
            self.assertEqual(connect.call_count, 1)
            self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 80))

    def test_checked_ip_is_pinned_and_body_limit_is_enforced(self):
        response = Mock(status=200); response.getheader.return_value = None; response.read.side_effect = [b"a" * 5]
        connection = Mock(); connection.getresponse.return_value = response
        with patch("chatBot.stickers.socket.getaddrinfo", return_value=self.addr("93.184.216.34")) as dns, patch("chatBot.stickers.socket.create_connection") as connect, patch("chatBot.stickers.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(ValueError): download_image("http://public.test/a", max_bytes=4)
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 80))
            connection.close.assert_called_once()

    def test_non_http_credentials_and_control_characters_rejected(self):
        for url in ("file:///etc/passwd", "http://user:secret@example.test/a", "http://example.test/\r\nX-Test:yes"):
            with patch("chatBot.stickers.socket.getaddrinfo") as dns:
                with self.assertRaises(ValueError): download_image(url)
                dns.assert_not_called()

    def test_total_deadline_interrupts_slow_drip_body(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.025)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            # Only this test bypasses DNS/IP filtering to reach its own fixture server.
            with patch("chatBot.stickers._public_addresses", return_value=["127.0.0.1"]):
                with self.assertRaises(TimeoutError):
                    download_image(f"http://fixture.test:{server.server_port}/image", timeout=.15)
            self.assertLess(time.monotonic() - started, .8)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
