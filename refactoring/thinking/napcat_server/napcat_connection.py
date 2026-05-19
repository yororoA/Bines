import websockets
import asyncio
from thinking_settings import thinking_settings
import json
import uuid


class NapCatClient:
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.websocket = None
        self._pending_requests: dict[str, asyncio.Future] = {}

    async def connect(self):
        """
        连接NapCat服务器
        """
        headers = {"Authorization": f"Bearer {self.token}"}

        while 1:  # 自动重连
            try:
                async with websockets.connect(
                    self.uri, extra_headers=headers
                ) as websocket:
                    self.websocket = websocket
                    print(f"NapCat connection succeed: {self.uri}")

                    await self._listen()
            except Exception as e:
                print(
                    f"NapCat connection error: {e}, try reconnect in {thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT} seconds"
                )
                await asyncio.sleep(thinking_settings.NAPCAT_WS_RECONNECT_TIMEOUT)

    async def close(self):
        """
        关闭NapCat连接
        """
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
            print("NapCat connection closed")

    async def _listen(self):
        """
        监听NapCat并分发所有收到的消息
        """
        try:
            async for message in self.websocket:
                data = json.loads(message)
                # todo: 处理消息
                print(f"Received message: {data}")
                # 是否为 api 响应
                request_id = data.get("echo") or data.get("request_id")
                if request_id and request_id in self._pending_requests:
                    future = self._pending_requests.pop(request_id)
                    if not future.done():
                        future.set_result(data)
                else:
                    # 其他消息
                    await self._process_other_message(data)
        except asyncio.CancelledError:
            print("NapCat connection cancelled")
        except websockets.ConnectionClosed:
            print("NapCat connection closed")
        finally:
            # 连接断开时清理所有等待中请求
            for future in self._pending_requests.values():
                future.set_exception(
                    asyncio.CancelledError(
                        "NapCat connection closed before response received"
                    )
                )
            self._pending_requests.clear()

    async def _process_other_message(self, data):
        """
        处理其他消息, qq消息入库/buffer
        """
        pass

    async def call_api(self,*, action, params=None):
        """
        调用NapCat API
        """
        if not self.websocket:
            await self.connect()

        request_id = uuid.uuid4().hex
        payload = {"action": action, "params": params or {}, "echo": request_id}
        # 创建一个 Future 来等待响应
        future = asyncio.Future()
        self._pending_requests[request_id] = future
        # 发送消息
        await self.websocket.send(json.dumps(payload))
        print(f"Requesting {action} with params: {params}")

        # 接收响应
        try:
            response = await asyncio.wait_for(
                future, timeout=thinking_settings.NAPCAT_WS_API_RESPONSE_TIMEOUT
            )
            response_data = json.loads(response)
            print(f"Received response: {response_data}")
            return response_data
        except asyncio.TimeoutError:
            print(f"Timeout waiting for response for {action} with params: {params}")
            # 清理等待中的 Future
            self._pending_requests.pop(request_id)
            return {
                "request_id": request_id,
                "action": action,
                "params": params,
                "error": "Timeout waiting for response",
            }

