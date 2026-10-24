"""Offline compatibility checks; no real HTTP requests or desktop imports."""
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tools")]
import moments_tool as moments
from ark_mcp_client import ArkMcpError


def response(status=200, headers=None):
    item = Mock()
    item.status_code = status
    item.headers = headers or {}
    item.json.return_value = {"success": True, "data": []}
    if status >= 400:
        item.raise_for_status.side_effect = moments.requests.HTTPError(response=item)
    return item


with patch.dict(os.environ, {"ARK_TRANSPORT": "legacy", "MOMENTS_API_BASE_URL": "http://127.0.0.1:1", "MOMENTS_UID": "fixture", "MOMENTS_TOKEN": "original"}):
    with patch.object(moments.requests, "get", return_value=response(headers={"X-Refreshed-Token": "refreshed"})) as get:
        moments.get_moments(limit=2)
        assert get.call_args.kwargs["params"]["limit"] == 2
        assert moments._headers()["Authorization"] == "X-Token refreshed"
    with patch.object(moments.requests, "get", return_value=response(401)) as get:
        try:
            moments.get_moments()
            raise AssertionError("401 accepted")
        except ArkMcpError:
            assert get.call_count == 1
    with patch.object(moments.requests, "get", side_effect=[moments.requests.Timeout(), response()]) as get:
        moments.get_moments()
        assert get.call_count == 2
    with patch.object(moments.requests, "post", side_effect=moments.requests.Timeout()) as post:
        try:
            moments.add_moment("fixture", request_id="legacy-fixture-key")
            raise AssertionError("Uncertain write accepted")
        except RuntimeError as error:
            assert "legacy-fixture-key" in str(error)
            assert post.call_count == 1
            assert post.call_args.kwargs["headers"]["X-Idempotency-Key"] == "legacy-fixture-key"
    with patch.dict(os.environ, {"ARK_TRANSPORT": "mcp", "ARK_MCP_URL": "", "ARK_MCP_TOKEN": "partial"}):
        with patch.object(moments.requests, "get") as get:
            try:
                moments.get_moments()
                raise AssertionError("Incomplete config accepted")
            except ArkMcpError as error:
                assert error.code == "CONFIG_ERROR"
                assert get.call_count == 0
print("PASS legacy pagination, token refresh, bounded reads, no write replay, no MCP fallback")
