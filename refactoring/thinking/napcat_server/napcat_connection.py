from __future__ import annotations

import json
import uuid
import random
import logging

import asyncio
import websockets

from thinking_settings import thinking_settings

logger = logging.getLogger(__name__)

_workflow = None


def _get_workflow():
    global _workflow
    if _workflow is None:
        from workflow import Workflow
        _workflow = Workflow()
    return _workflow


class NapCatClient:
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.websocket = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._connection_task: asyncio.Task | None = None

    async def process_messages(self):
        self._connection_task = asyncio.create_task(self._connect())
        await self._process_loop()

    async def close(self):
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
            logger.info("NapCat connection closed")

    async def _connect(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        backoff = thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT
        max_backoff = 60

        while True:
            try:
                async with websockets.connect(
                    self.uri, extra_headers=headers
                ) as websocket:
                    self.websocket = websocket
                    backoff = thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT
                    logger.info("NapCat connection established: %s", self.uri)
                    await self._listen()
                    self.websocket = None
            except asyncio.CancelledError:
                logger.info("NapCat connection task cancelled")
                self.websocket = None
                break
            except websockets.InvalidStatusCode as e:
                self.websocket = None
                if e.status_code in (401, 403):
                    logger.error(
                        "NapCat auth failed (status %d), stopping", e.status_code
                    )
                    break
                jitter = backoff * random.uniform(0.5, 1.5)
                logger.warning(
                    "NapCat error (status %d), retry in %.1fs", e.status_code, jitter
                )
                await asyncio.sleep(jitter)
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:
                self.websocket = None
                jitter = backoff * random.uniform(0.5, 1.5)
                logger.warning(
                    "NapCat connection error: %s, retry in %.1fs", e, jitter
                )
                await asyncio.sleep(jitter)
                backoff = min(backoff * 2, max_backoff)

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
                    await self._message_queue.put(data)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            logger.info("NapCat websocket closed")
        finally:
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(
                        asyncio.CancelledError("Connection closed before response")
                    )
            self._pending_requests.clear()

    async def _process_loop(self):
        while True:
            data = await self._message_queue.get()
            try:
                await self._process_event(data)
            except Exception:
                logger.exception("Error processing queued message")

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
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: workflow.invoke(content, thread_id=thread_id),
            )
        except Exception:
            logger.exception("Error in workflow (thread=%s)", thread_id)

    async def _wait_for_connection(self, timeout: float = 30.0) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.websocket:
                return True
            await asyncio.sleep(0.1)
        return False

    async def call_api(self, *, action, params=None):
        if not self.websocket:
            connected = await self._wait_for_connection(
                timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT
            )
            if not connected:
                return {
                    "action": action,
                    "params": params,
                    "error": "No active websocket connection",
                }

        request_id = uuid.uuid4().hex
        payload = {"action": action, "params": params or {}, "echo": request_id}
        future = asyncio.Future()
        self._pending_requests[request_id] = future
        await self.websocket.send(json.dumps(payload))
        logger.debug("Requesting %s with params: %s", action, params)

        try:
            response = await asyncio.wait_for(
                future, timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT
            )
            return response
        except asyncio.TimeoutError:
            logger.warning("Timeout for %s with params: %s", action, params)
            self._pending_requests.pop(request_id, None)
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
