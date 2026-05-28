from __future__ import annotations

import json
import uuid

import asyncio
import websockets

from thinking_settings import thinking_settings


_shared_workflow = None


def _get_workflow():
    # Shared Workflow instance across all QQ threads (private/group).
    # LangGraph's compiled app is thread-safe for concurrent invocations
    # as long as each invoke uses a distinct thread_id.
    global _shared_workflow
    if _shared_workflow is None:
        from workflow import Workflow
        _shared_workflow = Workflow()
    return _shared_workflow


class NapCatClient:
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.websocket = None
        self._pending_requests: dict[str, asyncio.Future] = {}

    async def connect(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        while True:
            try:
                async with websockets.connect(
                    self.uri, extra_headers=headers
                ) as websocket:
                    self.websocket = websocket
                    print(f"NapCat connection succeed: {self.uri}")
                    await self._listen()
            except Exception as e:
                print(
                    f"NapCat connection error: {e}, "
                    f"try reconnect in {thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT} seconds"
                )
                await asyncio.sleep(thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT)

    async def close(self):
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
            print("NapCat connection closed")

    async def _listen(self):
        try:
            async for message in self.websocket:
                data = json.loads(message)
                request_id = data.get("echo") or data.get("request_id")
                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests.pop(request_id)
                    if not future.done():
                        future.set_result(data)
                else:
                    await self._process_event(data)
        except asyncio.CancelledError:
            print("NapCat connection cancelled")
        except websockets.ConnectionClosed:
            print("NapCat connection closed")
        finally:
            for future in self._pending_requests.values():
                future.set_exception(
                    asyncio.CancelledError(
                        "NapCat connection closed before response received"
                    )
                )
            self._pending_requests.clear()

    async def _process_event(self, data: dict):
        post_type = data.get("post_type")
        if post_type != "message":
            return

        message_type = data.get("message_type", "")
        raw_message = data.get("raw_message", "")
        message_segments = data.get("message", [])

        content_parts = []
        for seg in message_segments:
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})
            if seg_type == "text":
                content_parts.append(seg_data.get("text", ""))
            elif seg_type == "at":
                at_qq = seg_data.get("qq", "")
                content_parts.append(f"@{at_qq}")
            elif seg_type == "image":
                content_parts.append("[image]")
        content = " ".join(content_parts) if content_parts else raw_message

        thread_id = _determine_thread_id(message_type)
        if not thread_id:
            return

        workflow = _get_workflow()
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: workflow.invoke(content, thread_id=thread_id),
        )

    async def call_api(self, *, action, params=None):
        if not self.websocket:
            await self.connect()

        request_id = uuid.uuid4().hex
        payload = {"action": action, "params": params or {}, "echo": request_id}
        future = asyncio.Future()
        self._pending_requests[request_id] = future
        await self.websocket.send(json.dumps(payload))
        print(f"Requesting {action} with params: {params}")

        try:
            response = await asyncio.wait_for(
                future, timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT
            )
            response_data = response
            print(f"Received response: {response_data}")
            return response_data
        except asyncio.TimeoutError:
            print(f"Timeout waiting for response for {action} with params: {params}")
            self._pending_requests.pop(request_id)
            return {
                "request_id": request_id,
                "action": action,
                "params": params,
                "error": "Timeout waiting for response",
            }


def _determine_thread_id(message_type: str) -> str:
    if message_type == "private":
        return "QQ_private"
    if message_type == "group":
        return "QQ_group"
    return ""