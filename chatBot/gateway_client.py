"""Skills and MCP share the gateway, never another OneBot socket."""
import json
import uuid
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from chatBot.config import GATEWAY_URL, gateway_token


class GatewayClient:
    def __init__(self, url=GATEWAY_URL, token=None):
        self.url, self.token = url.rstrip("/"), token or gateway_token()
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data, timeout=30):
        from tools.context import CURRENT_CONTEXT
        context = CURRENT_CONTEXT.get()
        payload = dict(data)
        if context.event_id:
            payload["event_id"] = context.event_id
        request = Request(self.url+path, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
        })
        try:
            with self.opener.open(request, timeout=timeout+15) as response:
                return json.load(response)["result"]
        except HTTPError as exc:
            with exc:
                error = json.load(exc).get("error", "gateway error")
            raise RuntimeError(error) from None
        except (URLError, TimeoutError):
            # An HTTP timeout after dispatch can also be a successful QQ write.
            return {"status": "uncertain", "msg": "网关断开或超时；写操作不能自动重发"}

    def raw_api(self, action, params=None, timeout=30, operation_id=None):
        from tools.context import CURRENT_CONTEXT
        from chatBot.capabilities import is_read
        context = CURRENT_CONTEXT.get()
        receipt = None
        if not is_read(action):
            if any((item.get("result") or {}).get("status") == "uncertain" for item in context.receipts):
                raise RuntimeError("本轮已有不确定写操作；先核查原回执，禁止继续发送或重试")
            if operation_id is None:
                index = len(context.receipts)
                operation_id = f"{context.turn_id}:tool:{index}" if context.turn_id else str(uuid.uuid4())
            elif context.turn_id and not operation_id.startswith(context.turn_id+":"):
                operation_id = context.turn_id+":tool:"+operation_id
            if context.turn_id:
                receipt = {"action": action, "params": params or {}, "operation_id": operation_id}
                context.receipts.append(receipt)
        result = self.request("/action", {
            "action": action, "params": params or {}, "timeout": timeout, "operation_id": operation_id,
        }, timeout)
        if operation_id and isinstance(result, dict):
            result.setdefault("operation_id", operation_id)
        if receipt is not None:
            receipt["result"] = result
        return result

    def discover(self, query="", action=None):
        return self.request("/discover", {"query": query, "action": action})

    def send_private_msg(self, user_id, message):
        return self.raw_api("send_private_msg", {"user_id": user_id, "message": message})

    def send_group_msg(self, group_id, message, at_user_id=None):
        from chatBot.messages import segments
        chain = segments(message)
        if at_user_id:
            chain.insert(0, {"type": "at", "data": {"qq": str(at_user_id)}})
        return self.raw_api("send_group_msg", {"group_id": group_id, "message": chain})

    def get_group_msg_history(self, group_id, message_seq=None, count=20):
        params = {"group_id": str(group_id), "count": count, "reverse_order": False}
        if message_seq is not None:
            params["message_seq"] = str(message_seq)
        return self.raw_api("get_group_msg_history", params)

    def get_group_member_list(self, group_id):
        return self.raw_api("get_group_member_list", {"group_id": group_id})
