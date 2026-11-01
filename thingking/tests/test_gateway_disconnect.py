"""Exercise transport loss against a local fake gateway; never send QQ messages."""
import io
import json
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import helpers  # noqa: F401 — adds the repository and memory package to sys.path
from chatBot.gateway_client import GatewayClient
from chatBot.memory_hub import GatewayMemoryClient
from memory.client import MemoryUnavailable
from tools.context import ToolContext, tool_context


class GatewayDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.mode = "disconnect"
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append((self.path, body))
                self.close_connection = True
                if fixture.mode == "disconnect":
                    # The request has been accepted, but its acknowledgement is lost.
                    return
                payload = json.dumps({"result": {"status": "ok"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload) + (20 if fixture.mode == "partial" else 0)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.client = GatewayClient(f"http://127.0.0.1:{self.server.server_port}", token="offline-test")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_lost_write_ack_keeps_uncertain_receipt_and_blocks_a_second_write(self):
        context = ToolContext(turn_id="disconnect-test")
        with tool_context(context):
            result = self.client.raw_api("send_private_msg", {"user_id": "offline-user", "message": "test"})
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(context.receipts[0]["result"], result)
            self.assertEqual(result["operation_id"], "disconnect-test:tool:0")
            with self.assertRaisesRegex(RuntimeError, "不确定写操作"):
                self.client.raw_api("send_private_msg", {"user_id": "offline-user", "message": "retry"})
        self.assertEqual(len(self.requests), 1)

    def test_claim_disconnect_is_unavailable_without_replaying_the_claim(self):
        memory = GatewayMemoryClient(self.client)
        with self.assertRaises(MemoryUnavailable):
            memory.claim_job()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1]["method"], "claim_job")
        self.mode = "success"
        self.assertEqual(memory.get_state()["status"], "ok")
        self.assertEqual([body["method"] for _, body in self.requests], ["claim_job", "get_state"])

    def test_partial_http_ack_is_uncertain_and_never_retried(self):
        self.mode = "partial"
        result = self.client.request("/action", {"action": "send_private_msg"})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(self.requests), 1)

    def test_transport_failure_variants_are_not_retried(self):
        for error in (ConnectionResetError(), BrokenPipeError(), TimeoutError(),
                      URLError("connection refused"), IncompleteRead(b"partial")):
            with self.subTest(error=type(error).__name__):
                self.client.opener = Mock()
                self.client.opener.open.side_effect = error
                self.assertEqual(self.client.request("/action", {})["status"], "uncertain")
                self.client.opener.open.assert_called_once()

    def test_http_rejection_remains_a_definite_error(self):
        self.client.opener = Mock()
        self.client.opener.open.side_effect = HTTPError(
            self.client.url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"unauthorized"}'))
        with self.assertRaisesRegex(RuntimeError, "unauthorized"):
            self.client.request("/action", {})
        self.client.opener.open.assert_called_once()

    def test_unexpected_programming_error_is_not_hidden_as_transport_loss(self):
        self.client.opener = Mock()
        self.client.opener.open.side_effect = ValueError("invalid request")
        with self.assertRaisesRegex(ValueError, "invalid request"):
            self.client.request("/action", {})


if __name__ == "__main__":
    unittest.main()
