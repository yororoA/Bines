import json
import threading
import time
import uuid
from typing import Optional, List, Dict, Union, Any, Callable
import websocket
from .config import ONEBOT_WS_URL, ONEBOT_ACCESS_TOKEN

class NapCatClient:
    """
    NapCatQQ (OneBot V11) WebSocket 客户端 wrapper
    支持双向通信：API 调用 + 事件监听
    """
    def __init__(self, ws_url: str = ONEBOT_WS_URL, token: Optional[str] = ONEBOT_ACCESS_TOKEN):
        """
        初始化客户端
        :param ws_url: NapCatQQ WebSocket 服务地址，例如 ws://127.0.0.1:6101
        :param token: Access Token
        """
        self.ws_url = ws_url
        self.token = token
        self.ws: Optional[websocket.WebSocketApp] = None
        self.is_connected = False
        self._shutdown_event = threading.Event()
        
        # 存储 API 响应的回调 {echo_id: {"event": threading.Event, "result": None}}
        self._api_callbacks: Dict[str, Dict] = {}
        # 存储事件监听器列表
        self._event_listeners: List[Callable[[Dict], None]] = []
        
        # 线程锁
        self._lock = threading.Lock()
        
        # 线程引用
        self._thread: Optional[threading.Thread] = None

    def connect(self, background: bool = True):
        """
        连接 WebSocket 服务器
        :param background: 是否在后台线程运行
        """
        if self.is_connected:
            return

        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # 关键配置：on_open, on_message 等
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )

        if background:
            self._thread = threading.Thread(target=self.ws.run_forever)
            self._thread.daemon = True # 设置为守护线程，主程序退出时自动退出
            self._thread.start()
        else:
            self.ws.run_forever()

    def disconnect(self):
        """
        断开连接
        """
        self._shutdown_event.set()
        if self.ws:
            self.ws.close()
        self.is_connected = False

    def _on_open(self, ws):
        print(f"[NapCatClient] Connected to {self.ws_url}")
        self.is_connected = True

    def _on_message(self, ws, message):
        """
        处理接收到的 WebSocket 消息
        """
        try:
            data = json.loads(message)
            
            # 1. 检查是否是 API 响应 (通过 echo 字段判断)
            # 假设 echo 字段是我们发送请求时带上的 ID
            if "echo" in data:
                echo_id = data["echo"]
                # 这是一个 API 响应
                with self._lock:
                    if echo_id in self._api_callbacks:
                        callback_info = self._api_callbacks[echo_id]
                        callback_info["result"] = data
                        callback_info["event"].set() # 通知等待线程
                        # 注意：这里不删除 callback，由调用方获取结果后删除，或者超时删除
                return

            # 2. 如果不是 API 响应，那就是 OneBot 事件 (Event)
            # 例如：私聊消息、群消息、心跳包等
            post_type = data.get("post_type")
            if post_type == "meta_event":
                # 心跳包通常不需要太多处理，除非你需要监控状态
                return 

            # 分发给所有已注册的监听器
            for listener in self._event_listeners:
                try:
                    # 在独立线程或者当前线程执行？为了安全，最好不要阻塞 on_message
                    # 这里简化处理，直接执行
                    listener(data)
                except Exception as e:
                    print(f"[NapCatClient] Listener Error: {e}")

        except json.JSONDecodeError:
            print(f"[NapCatClient] Received invalid JSON")
        except Exception as e:
            print(f"[NapCatClient] Message Error: {e}")

    def _on_error(self, ws, error):
        print(f"[NapCatClient] WebSocket Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[NapCatClient] WebSocket Closed: {close_status_code} - {close_msg}")
        self.is_connected = False

    def on(self, event_type: str = None):
        """
        装饰器：注册事件监听器
        :param event_type: 指定监听的消息类型 'message', 'notice', 'request'，如果为 None 则监听所有
        """
        def decorator(func):
            def wrapper(event):
                if event_type is None or event.get("post_type") == event_type:
                    func(event)
            self.add_event_listener(wrapper)
            return func
        return decorator

    def add_event_listener(self, callback: Callable[[Dict], None]):
        """
        添加事件监听器函数
        """
        self._event_listeners.append(callback)

    def raw_api(self, action: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """
        [公开 API] 调用任意 OneBot API
        """
        return self._call_api(action, params, timeout)

    def _call_api(self, action: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        """
        通过 WebSocket 调用 API
        """
        if not self.is_connected:
            # 简单的自动重连等待
            start_wait = time.time()
            while not self.is_connected:
                if time.time() - start_wait > 5:
                    return {"status": "failed", "retcode": -1, "msg": "Not connected to WebSocket"}
                time.sleep(0.1)

        echo_id = str(uuid.uuid4())
        payload = {
            "action": action,
            "params": params,
            "echo": echo_id
        }
        
        # 注册回调
        response_event = threading.Event()
        callback_info = {"event": response_event, "result": None}
        
        with self._lock:
            self._api_callbacks[echo_id] = callback_info
        
        # 发送
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            with self._lock:
                if echo_id in self._api_callbacks:
                    del self._api_callbacks[echo_id]
            return {"status": "failed", "retcode": -1, "msg": f"Send error: {str(e)}"}
            
        # 等待响应
        if response_event.wait(timeout):
            result = callback_info["result"]
            with self._lock:
                if echo_id in self._api_callbacks:
                    del self._api_callbacks[echo_id]
            return result
        else:
            # 超时
            with self._lock:
                if echo_id in self._api_callbacks:
                    del self._api_callbacks[echo_id]
            return {"status": "failed", "retcode": -1, "msg": "API Timeout"}

    # --- 常用 API 封装 ---

    def send_private_msg(self, user_id: int, message: str) -> Dict[str, Any]:
        return self._call_api("send_private_msg", {
            "user_id": user_id,
            "message": [{"type": "text", "data": {"text": message}}]
        })

    def send_group_msg(self, group_id: Union[int, str], message: str, at_user_id: Optional[Union[int, str]] = None) -> Dict[str, Any]:
        """
        发送群消息
        :param group_id: 群号
        :param message: 消息内容
        :param at_user_id: 要艾特的用户 QQ 号，如果为 'all' 则艾特全体成员。如果不艾特则为 None。
        """
        msg_chain = []
        if at_user_id:
            if str(at_user_id).lower() == 'all':
                msg_chain.append({"type": "at", "data": {"qq": "all"}})
            else:
                msg_chain.append({"type": "at", "data": {"qq": str(at_user_id)}})
            message = " " + message
        
        msg_chain.append({"type": "text", "data": {"text": message}})
        
        return self._call_api("send_group_msg", {
            "group_id": int(group_id),
            "message": msg_chain
        })

    def send_like(self, user_id: int, times: int = 1) -> Dict[str, Any]:
        """
        发送好友赞
        :param user_id: 对方 QQ 号
        :param times: 赞的次数，每个好友每天最多 10 次
        """
        return self._call_api("send_like", {"user_id": user_id, "times": times})

    def set_group_kick(self, group_id: int, user_id: int, reject_add_request: bool = False) -> Dict[str, Any]:
        """
        群组踢人
        :param group_id: 群号
        :param user_id: 要踢的 QQ 号
        :param reject_add_request: 拒绝此人的加群请求
        """
        return self._call_api("set_group_kick", {
            "group_id": group_id,
            "user_id": user_id,
            "reject_add_request": reject_add_request
        })

    def get_group_msg_history(self, group_id: int, message_seq: Optional[int] = None) -> Dict[str, Any]:
        """
        [扩展 API] 获取群消息历史记录
        注意：这是非标准 OneBot V11 API，仅 NapCat/Go-CQHTTP 支持
        :param group_id: 群号
        :param message_seq: 起始消息序号，可选。留空则获取最新消息。
        """
        params = {"group_id": group_id}
        if message_seq:
            params["message_seq"] = message_seq
        return self._call_api("get_group_msg_history", params)

    def set_group_ban(self, group_id: int, user_id: int, duration: int = 30 * 60) -> Dict[str, Any]:
        """
        群组单人禁言
        :param group_id: 群号
        :param user_id: 要禁言的 QQ 号
        :param duration: 禁言时长，单位秒，0 表示取消禁言
        """
        return self._call_api("set_group_ban", {
            "group_id": group_id,
            "user_id": user_id,
            "duration": duration
        })

    def set_group_whole_ban(self, group_id: int, enable: bool = True) -> Dict[str, Any]:
        """
        群组全员禁言
        :param group_id: 群号
        :param enable: 是否开启
        """
        return self._call_api("set_group_whole_ban", {
            "group_id": group_id,
            "enable": enable
        })

    def set_group_admin(self, group_id: int, user_id: int, enable: bool = True) -> Dict[str, Any]:
        """
        群组设置管理员
        :param group_id: 群号
        :param user_id: 要设置的 QQ 号
        :param enable: true 为设置，false 为取消
        """
        return self._call_api("set_group_admin", {
            "group_id": group_id,
            "user_id": user_id,
            "enable": enable
        })

    def set_group_card(self, group_id: int, user_id: int, card: str = "") -> Dict[str, Any]:
        """
        设置群名片 (群备注)
        :param group_id: 群号
        :param user_id: 要设置的 QQ 号
        :param card: 群名片内容，不填或空字符串表示删除群名片
        """
        return self._call_api("set_group_card", {
            "group_id": group_id,
            "user_id": user_id,
            "card": card
        })

    def set_group_name(self, group_id: int, group_name: str) -> Dict[str, Any]:
        """
        设置群名
        :param group_id: 群号
        :param group_name: 新群名
        """
        return self._call_api("set_group_name", {
            "group_id": group_id,
            "group_name": group_name
        })

    def set_group_leave(self, group_id: int, is_dismiss: bool = False) -> Dict[str, Any]:
        """
        退出群组
        :param group_id: 群号
        :param is_dismiss: 是否解散，如果登录号是群主，则仅在此项为 true 时能够解散
        """
        return self._call_api("set_group_leave", {
            "group_id": group_id,
            "is_dismiss": is_dismiss
        })

    def set_group_special_title(self, group_id: int, user_id: int, special_title: str = "", duration: int = -1) -> Dict[str, Any]:
        """
        设置群组专属头衔
        :param group_id: 群号
        :param user_id: 要设置的 QQ 号
        :param special_title: 头衔，不填或空字符串表示删除专属头衔
        :param duration: 有效时长，单位秒，-1 表示永久
        """
        return self._call_api("set_group_special_title", {
            "group_id": group_id,
            "user_id": user_id,
            "special_title": special_title,
            "duration": duration
        })

    def get_stranger_info(self, user_id: int, no_cache: bool = False) -> Dict[str, Any]:
        """
        获取陌生人信息
        :param user_id: QQ 号
        :param no_cache: 是否不使用缓存
        """
        return self._call_api("get_stranger_info", {
            "user_id": user_id,
            "no_cache": no_cache
        })

    def get_group_info(self, group_id: int, no_cache: bool = False) -> Dict[str, Any]:
        """
        获取群信息
        :param group_id: 群号
        :param no_cache: 是否不使用缓存
        """
        return self._call_api("get_group_info", {
            "group_id": group_id,
            "no_cache": no_cache
        })

    def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = False) -> Dict[str, Any]:
        """
        获取群成员信息
        :param group_id: 群号
        :param user_id: QQ 号
        :param no_cache: 是否不使用缓存
        """
        return self._call_api("get_group_member_info", {
            "group_id": group_id,
            "user_id": user_id,
            "no_cache": no_cache
        })

    def get_group_member_list(self, group_id: int) -> Dict[str, Any]:
        """
        获取群成员列表
        :param group_id: 群号
        """
        return self._call_api("get_group_member_list", {"group_id": group_id})
    
    def get_group_honor_info(self, group_id: int, type: str) -> Dict[str, Any]:
        """
        获取群荣誉信息
        :param group_id: 群号
        :param type: 要获取的群荣誉类型，可传入 talkative (龙王), performer (群聊之火), legend (群聊炽焰), strong_newbie (冒尖小春笋), emotion (快乐源泉)
        """
        return self._call_api("get_group_honor_info", {
            "group_id": group_id,
            "type": type
        })

    def delete_msg(self, message_id: int) -> Dict[str, Any]:
        """
        撤回消息
        :param message_id: 消息 ID
        """
        return self._call_api("delete_msg", {"message_id": message_id})

    def get_msg(self, message_id: int) -> Dict[str, Any]:
        """
        获取消息详情
        :param message_id: 消息 ID
        """
        return self._call_api("get_msg", {"message_id": message_id})

    def get_forward_msg(self, id: str) -> Dict[str, Any]:
        """
        获取合并转发消息
        :param id: 合并转发 ID
        """
        return self._call_api("get_forward_msg", {"id": id})

    def get_image(self, file: str) -> Dict[str, Any]:
        """
        获取图片信息
        :param file: 图片缓存文件名
        """
        return self._call_api("get_image", {"file": file})

    def can_send_image(self) -> Dict[str, Any]:
        """
        检查是否可以发送图片
        """
        return self._call_api("can_send_image", {})

    def can_send_record(self) -> Dict[str, Any]:
        """
        检查是否可以发送语音
        """
        return self._call_api("can_send_record", {})
    
    def get_version_info(self) -> Dict[str, Any]:
        """
        获取版本信息
        """
        return self._call_api("get_version_info", {})
        
    def set_restart(self, delay: int = 0) -> Dict[str, Any]:
        """
        重启 OneBot 实现
        :param delay: 延迟毫秒数
        """
        return self._call_api("set_restart", {"delay": delay})
    
    def clean_cache(self) -> Dict[str, Any]:
        """
        清理缓存
        """
        return self._call_api("clean_cache", {})

    def send_msg(self, message_type: str, user_id: Optional[int] = None, group_id: Optional[int] = None, message: str = "", auto_escape: bool = False) -> Dict[str, Any]:
        """
        发送消息 (综合接口)
        :param message_type: 消息类型，支持 private、group，分别对应私聊、群组，如不传入，则根据传入的 *_id 参数判断
        :param user_id: 对方 QQ 号 (仅在 private 类型时需要)
        :param group_id: 群号 (仅在 group 类型时需要)
        :param message: 消息内容
        :param auto_escape: 消息内容是否作为纯文本发送 (即不解析 CQ 码)
        """
        return self._call_api("send_msg", {
            "message_type": message_type,
            "user_id": user_id,
            "group_id": group_id,
            "message": message,
            "auto_escape": auto_escape
        })

    def set_group_anonymous_ban(self, group_id: int, anonymous: Dict[str, Any] = None, anonymous_flag: str = None, duration: int = 30 * 60) -> Dict[str, Any]:
        """
        群组匿名用户禁言
        :param group_id: 群号
        :param anonymous: 可选，要禁言的匿名用户对象（群消息上报的 anonymous 字段）
        :param anonymous_flag: 可选，要禁言的匿名用户的 flag（需从群消息上报的数据中获得）
        :param duration: 禁言时长，单位秒
        """
        params = {"group_id": group_id, "duration": duration}
        if anonymous:
            params["anonymous"] = anonymous
        if anonymous_flag:
            params["anonymous_flag"] = anonymous_flag
        return self._call_api("set_group_anonymous_ban", params)

    def set_group_anonymous(self, group_id: int, enable: bool = True) -> Dict[str, Any]:
        """
        群组设置匿名
        :param group_id: 群号
        :param enable: 是否允许匿名聊天
        """
        return self._call_api("set_group_anonymous", {"group_id": group_id, "enable": enable})

    def set_friend_add_request(self, flag: str, approve: bool = True, remark: str = "") -> Dict[str, Any]:
        """
        处理加好友请求
        :param flag: 加好友请求的 flag（需从上报的数据中获得）
        :param approve: 是否同意请求
        :param remark: 添加后的好友备注（仅在同意时有效）
        """
        return self._call_api("set_friend_add_request", {
            "flag": flag,
            "approve": approve,
            "remark": remark
        })

    def set_group_add_request(self, flag: str, sub_type: str, approve: bool = True, reason: str = "") -> Dict[str, Any]:
        """
        处理加群请求／邀请
        :param flag: 加群请求的 flag（需从上报的数据中获得）
        :param sub_type: add 或 invite，请求类型
        :param approve: 是否同意请求／邀请
        :param reason: 拒绝理由（仅在拒绝时有效）
        """
        return self._call_api("set_group_add_request", {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
            "reason": reason
        })

    def get_cookies(self, domain: str = "") -> Dict[str, Any]:
        """
        获取 Cookies
        :param domain: 需要获取 cookies 的域名
        """
        return self._call_api("get_cookies", {"domain": domain})

    def get_csrf_token(self) -> Dict[str, Any]:
        """
        获取 CSRF Token
        """
        return self._call_api("get_csrf_token", {})

    def get_credentials(self, domain: str = "") -> Dict[str, Any]:
        """
        获取 QQ 相关接口凭证
        :param domain: 需要获取 cookies 的域名
        """
        return self._call_api("get_credentials", {"domain": domain})

    def get_record(self, file: str, out_format: str) -> Dict[str, Any]:
        """
        获取语音
        :param file: 收到的语音文件名
        :param out_format: 要转换到的格式
        """
        return self._call_api("get_record", {"file": file, "out_format": out_format})

    def get_status(self) -> Dict[str, Any]:
        """
        获取运行状态
        """
        return self._call_api("get_status", {})

# 测试代码
if __name__ == "__main__":
    import time
    client = NapCatClient()

    @client.on("message")
    def handle_msg(event):
        try:
            print(f"\n[收到消息] {event.get('raw_message')}\n")
        except Exception as e:
            print(f"Error handling message: {e}")

    print("启动连接...")
    client.connect() # 默认后台运行

    # 等待连接成功
    time.sleep(2) 
    
    if client.is_connected:
        print("获取登录信息:")
        print(client.get_login_info())
        
        # 只有在主线程不需要做其他事的时候才需要这样 Loop
        # 如果你的主程序是 GUI 或者有自己的循环，这里就不需要了
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            client.disconnect()
    else:
        print("连接失败。")
