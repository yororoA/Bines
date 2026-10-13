"""Real MCP/HTTP smoke test, only for arkserver's disposable local fixture."""
import argparse
import asyncio
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import uuid4

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "tools")]

from ark_mcp_client import ArkMcpError, call_ark, mcp_config
from ark_tool import get_ark_chat_history, get_ark_conversations, get_ark_operation, get_ark_profile, send_ark_chat
import moments_tool as moments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-fixture", action="store_true", required=True)
    parser.parse_args()
    url = urlsplit(os.environ.get("ARK_MCP_URL", ""))
    assert url.hostname in {"127.0.0.1", "::1", "localhost"}, "Only a disposable local fixture is allowed"
    identity = get_ark_profile()["data"]
    assert identity["uid"] == "bines-fixture", "Expected arkserver test fixture"
    saved_uid = os.environ["ARK_MCP_UID"]
    try:
        os.environ["ARK_MCP_UID"] = "other-fixture"
        try:
            get_ark_profile()
            raise AssertionError("Wrong UID accepted with valid token")
        except ArkMcpError as error:
            assert error.code == "TRANSPORT_ERROR"
    finally:
        os.environ["ARK_MCP_UID"] = saved_uid
    print("PASS UID and token must identify the same fixed account")
    os.environ["MOMENTS_API_BASE_URL"] = "http://127.0.0.1:1"
    moments._check_requests = lambda: (_ for _ in ()).throw(AssertionError("MCP fell back to REST"))

    page = moments.get_moments_simple(page=1, limit=2)
    assert len(page["data"]) == 2 and page["pagination"]["next_page"] == 2
    assert "\n" in page["data"][0]["content"]
    print("PASS SDK identity, pagination and preserved newlines")

    request_id = str(uuid4())
    args = {"title": "Python fixture", "content": "Line one\nLine two", "request_id": request_id}
    first = moments.add_moment(**args)
    second = moments.add_moment(**args)
    assert first["data"]["id"] == second["data"]["id"]
    assert first["request_id"] == request_id
    operation = get_ark_operation(request_id)["data"]
    assert operation["status"] == "completed"
    try:
        moments.add_moment(**{**args, "content": "Changed"})
        raise AssertionError("Request fingerprint was not checked")
    except ArkMcpError as error:
        assert error.code == "REQUEST_ID_CONFLICT"
        assert error.request_id == request_id
    print("PASS publish replay, recovery and conflicting arguments")

    moment_id = first["data"]["id"]
    comment = moments.comment_moment(moment_id, "Python comment", request_id=str(uuid4()))
    comments = moments.get_comments(moment_id=moment_id, limit=1)
    assert comments["data"][0]["_id"] == comment["data"]["id"]
    assert moments.like_moment(moment_id, True)["data"]["likes"] == 1
    assert moments.like_comment(comment["data"]["id"], True)["data"]["likes"] == 1
    assert moments.like_moment(moment_id, False)["data"]["likes"] == 0
    print("PASS comments and desired-state likes")

    assert len(get_ark_conversations()["data"]["items"]) == 2
    chat_id = str(uuid4())
    chat = send_ark_chat("admin", "Private fixture\nsecond line", request_id=chat_id)
    replay = send_ark_chat("admin", "Private fixture\nsecond line", request_id=chat_id)
    assert chat["data"]["id"] == replay["data"]["id"]
    assert get_ark_chat_history("admin")["data"]["items"][0]["text"].endswith("second line")
    print("PASS explicit chat and chat replay")

    instance = str(uuid4())
    call_ark("ark_heartbeat", {"instance_id": instance, "online": True})
    assert get_ark_profile()["data"]["presence"]["online"]
    call_ark("ark_heartbeat", {"instance_id": instance, "online": False})
    assert not get_ark_profile()["data"]["presence"]["online"]

    async def synchronous_action_inside_loop():
        return get_ark_profile()
    assert asyncio.run(synchronous_action_inside_loop())["data"]["uid"] == identity["uid"]
    print("PASS independent presence and synchronous action inside async runtime")

    for name in ("ARK_MCP_UID", "ARK_MCP_TOKEN"):
        saved = os.environ.pop(name)
        try:
            try:
                mcp_config()
                raise AssertionError("Partial config was accepted")
            except ArkMcpError as error:
                assert error.code == "CONFIG_ERROR"
        finally:
            os.environ[name] = saved
    print("PASS incomplete config rejects without fallback")


if __name__ == "__main__":
    main()
