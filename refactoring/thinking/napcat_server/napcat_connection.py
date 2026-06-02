from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
import json
import logging
import random
import threading
import uuid
from collections import OrderedDict

import websockets

from thinking_settings import thinking_settings

logger = logging.getLogger(__name__)

_workflow = None
_workflow_lock = threading.Lock()
_MAX_SEEN_MESSAGES = 1000


def _get_workflow():
    global _workflow
    if _workflow is None:
        with _workflow_lock:
            if _workflow is None:
                from workflow import Workflow
                _workflow = Workflow()
    return _workflow


def close_workflow():
    global _workflow
    w = None
    with _workflow_lock:
        if _workflow is not None:
            w = _workflow
            _workflow = None
    if w is not None:
        w.close()


def _is_at_bot(message_segments: list[dict], bot_number: str) -> bool:
    if not bot_number:
        return False
    for seg in message_segments:
        if seg.get("type") == "at" and str(seg.get("data", {}).get("qq", "")) == str(bot_number):
            return True
    return False


class NapCatClient:
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.websocket = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._connection_task: asyncio.Task | None = None
        self._seen_message_ids: OrderedDict = OrderedDict()
        self._debounce_timers: dict[str, asyncio.Task] = {}
        self._running_tasks: dict[str, asyncio.Task | ConcurrentFuture] = {}
        self._pending_buffers: dict[str, list[str]] = {}
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._task_lock = threading.Lock()

    def _is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = None
        if len(self._seen_message_ids) > _MAX_SEEN_MESSAGES:
            self._seen_message_ids.popitem(last=False)
        return False

    def _is_trigger_message(self, data: dict) -> bool:
        message_type = data.get("message_type", "")
        if message_type == "private":
            return True
        if message_type == "group":
            bot_number = thinking_settings.BOT_NUMBER
            message_segments = data.get("message", [])
            return _is_at_bot(message_segments, bot_number)
        return False

    async def _handle_debounce(self, thread_id: str, content: str):
        with self._task_lock:
            old_timer = self._debounce_timers.pop(thread_id, None)
            old_task = self._running_tasks.pop(thread_id, None)
        if old_timer is not None:
            old_timer.cancel()
            try:
                await old_timer
            except asyncio.CancelledError:
                pass
        if old_task is not None:
            from workflow.cancel import cancel_thread
            cancel_thread(thread_id)
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass

        with self._task_lock:
            self._pending_buffers.setdefault(thread_id, []).append(content)
            self._debounce_timers[thread_id] = asyncio.create_task(
                self._debounce_callback(thread_id)
            )

    async def _debounce_callback(self, thread_id: str):
        try:
            await asyncio.sleep(thinking_settings.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        with self._task_lock:
            self._debounce_timers.pop(thread_id, None)
            messages = self._pending_buffers.pop(thread_id, [])
        if not messages:
            return

        combined = "\n".join(messages)
        logger.info("Debounce triggered for %s with %d buffered message(s)", thread_id, len(messages))

        from workflow.cancel import get_thread_cancel_event, remove_thread_cancel_event
        workflow = _get_workflow()
        cancel_event = get_thread_cancel_event(thread_id)
        cancel_event.clear()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                None,
                lambda: workflow.invoke(combined, thread_id=thread_id, cancel_event=cancel_event),
            )
            with self._task_lock:
                self._running_tasks[thread_id] = future
            await future
        except asyncio.CancelledError:
            logger.info("Workflow cancelled for %s", thread_id)
        except Exception:
            logger.exception("Error in debounced workflow (thread=%s)", thread_id)
        finally:
            with self._task_lock:
                self._running_tasks.pop(thread_id, None)
            remove_thread_cancel_event(thread_id)

    async def process_messages(self):
        self._connection_task = asyncio.create_task(self._connect())
        await self._process_loop()

    async def close(self):
        from workflow.cancel import cancel_all_threads
        cancel_all_threads()
        with self._task_lock:
            timers = list(self._debounce_timers.values())
            tasks = list(self._running_tasks.values())
            self._debounce_timers.clear()
            self._running_tasks.clear()
            self._pending_buffers.clear()
        for timer in timers:
            timer.cancel()
        for task in tasks:
            task.cancel()
        for timer in timers:
            try:
                await timer
            except asyncio.CancelledError:
                pass
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

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

        self._main_loop = asyncio.get_running_loop()

        while True:
            try:
                async with websockets.connect(
                    self.uri, additional_headers=headers
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
            for attempt in range(3):
                try:
                    await self._process_event(data)
                    break
                except (ConnectionError, TimeoutError, OSError) as exc:
                    if attempt < 2:
                        logger.warning(
                            "Transient error (attempt %d/3): %s", attempt + 1, exc
                        )
                        await asyncio.sleep(1)
                    else:
                        logger.exception("Transient error after 3 attempts")
                except Exception:
                    logger.exception("Permanent error processing message, skipping retries")
                    break

    async def _process_event(self, data: dict):
        post_type = data.get("post_type")
        if post_type != "message":
            return

        message_id = data.get("message_id")
        if message_id is not None and self._is_duplicate(str(message_id)):
            logger.debug("Duplicate message_id=%s ignored", message_id)
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
                img_url = seg_data.get("url") or seg_data.get("file", "")
                if img_url:
                    content_parts.append(f"[image:{img_url}]")
                else:
                    content_parts.append("[image]")
        content = " ".join(content_parts) if content_parts else raw_message

        if len(content) > thinking_settings.MAX_INPUT_LENGTH:
            original_len = len(content)
            content = content[:thinking_settings.MAX_INPUT_LENGTH]
            logger.warning("Input message truncated from %d to %d chars", original_len, thinking_settings.MAX_INPUT_LENGTH)

        user_id = str(data.get("user_id", ""))
        group_id = str(data.get("group_id", ""))
        thread_id = _determine_thread_id(message_type, user_id=user_id, group_id=group_id)
        if not thread_id:
            return

        if self._is_trigger_message(data):
            await self._handle_debounce(thread_id, content)
        else:
            workflow = _get_workflow()
            workflow.inject_message(content, thread_id=thread_id)
            logger.debug("Injected non-trigger message into STM for %s", thread_id)

    async def _wait_for_connection(self, timeout: float = 30.0) -> bool:
        if self.websocket:
            return True
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if self.websocket:
                    return True
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("Error waiting for connection")
        return False

    def get_main_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._main_loop

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
        try:
            await self.websocket.send(json.dumps(payload))
        except Exception:
            self._pending_requests.pop(request_id, None)
            raise
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


def _determine_thread_id(message_type: str, user_id: str = "", group_id: str = "") -> str:
    if message_type == "private":
        return f"QQ_private_{user_id}" if user_id else "QQ_private"
    if message_type == "group":
        return f"QQ_group_{group_id}" if group_id else "QQ_group"
    return ""
