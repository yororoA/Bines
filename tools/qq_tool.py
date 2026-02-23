import json
from typing import Dict, Any, List, Optional
from chatBot.napcat_client import NapCatClient
from .dependencies import deps

# 全局客户端实例（延迟初始化）
_qq_client: Optional[NapCatClient] = None

def get_qq_client() -> NapCatClient:
    global _qq_client
    if _qq_client is None:
        _qq_client = NapCatClient()
        # 注意：这里我们只用它来发送请求。
        # 虽然主要监听是在独立的 chatBot 进程中，但 NapCatClient 是基于 WebSocket 的，
        # 必须连接才能发送请求。连接后会有心跳和消息接收，但我们不注册任何监听器即可。
        _qq_client.connect(background=True)
        # 等待连接成功
        import time
        for _ in range(50):
            if _qq_client.is_connected:
                break
            time.sleep(0.1)
    return _qq_client

def send_qq_private_msg(user_id: int, message: str) -> str:
    """
    发送QQ私聊消息
    """
    client = get_qq_client()
    res = client.send_private_msg(user_id, message)
    return json.dumps(res, ensure_ascii=False)

def send_qq_group_msg(group_id: int, message: str, at_user_id: str = None) -> str:
    """
    发送QQ群聊消息
    """
    client = get_qq_client()
    # 处理 at_user_id 类型转换
    at_uid = None
    if at_user_id:
        if str(at_user_id).lower() == "all":
            at_uid = "all"
        else:
            # 兼容 int 类型传入
            try:
                at_uid = int(at_user_id)
            except:
                at_uid = None
    
    # 调用客户端发送
    res = client.send_group_msg(group_id, message, at_user_id=at_uid)
    return f"发送结果: {res}" if res else "发送失败"

def get_qq_group_list() -> str:
    """
    获取QQ群列表
    """
    client = get_qq_client()
    # 这是一个新增加的方法，我们需要在 NapCatClient 确认是否有
    # 查看 NapCatClient 代码，发现并没有直接暴露 get_group_list，但有 raw_api
    res = client.raw_api("get_group_list", {})
    # 简化返回结果，只返回群号和群名
    if res.get("status") == "ok":
        groups = []
        for g in res.get("data", []):
            groups.append(f"{g.get('group_name')}({g.get('group_id')})")
        return "\n".join(groups)
    return str(res)

def get_qq_friend_list() -> str:
    """
    获取QQ好友列表（包含备注名）
    """
    client = get_qq_client()
    res = client.raw_api("get_friend_list", {})
    if res.get("status") == "ok":
        friends = []
        for f in res.get("data", []):
            nickname = f.get('nickname', '未知')
            user_id = f.get('user_id')
            remark = f.get('remark', '')
            # 如果有备注则显示「备注(昵称)(QQ号)」，否则「昵称(QQ号)」
            if remark and remark != nickname:
                friends.append(f"{remark}[昵称:{nickname}]({user_id})")
            else:
                friends.append(f"{nickname}({user_id})")
        return "\n".join(friends)
    return str(res)

def broadcast_to_all_friends(message: str, exclude_user_ids: str = "") -> str:
    """
    向所有QQ好友批量发送私聊消息（群发）。
    
    Args:
        message: 要发送的消息内容
        exclude_user_ids: 可选。要排除的QQ号列表，逗号分隔。例如 "12345,67890"
    
    Returns:
        str: 发送结果摘要
    """
    import time as _time
    client = get_qq_client()
    
    # 1. 获取好友列表
    res = client.raw_api("get_friend_list", {})
    if res.get("status") != "ok":
        return f"获取好友列表失败: {res}"
    
    friends = res.get("data", [])
    if not friends:
        return "好友列表为空，没有可发送的对象。"
    
    # 2. 解析排除列表
    exclude_set = set()
    if exclude_user_ids:
        for uid_str in exclude_user_ids.split(","):
            uid_str = uid_str.strip()
            if uid_str:
                try:
                    exclude_set.add(int(uid_str))
                except ValueError:
                    pass
    
    # 3. 逐个发送
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total = len(friends)
    
    for friend in friends:
        uid = friend.get("user_id")
        nickname = friend.get("nickname", "未知")
        remark = friend.get("remark", "")
        display_name = remark if remark else nickname
        
        if not uid:
            continue
        
        if uid in exclude_set:
            skipped_count += 1
            continue
        
        try:
            # 模板变量替换：支持 {昵称}/{nickname}, {qq}/{qq号}, {备注}/{remark}
            final_msg = message
            final_msg = final_msg.replace("{昵称}", nickname).replace("{nickname}", nickname)
            final_msg = final_msg.replace("{qq}", str(uid)).replace("{qq号}", str(uid))
            final_msg = final_msg.replace("{备注}", remark or nickname).replace("{remark}", remark or nickname)
            
            client.send_private_msg(int(uid), final_msg)
            success_count += 1
            print(f"[QQ Broadcast] 已发送给 {display_name}({uid}) ({success_count}/{total})", flush=True)
        except Exception as e:
            fail_count += 1
            print(f"[QQ Broadcast] 发送给 {display_name}({uid}) 失败: {e}", flush=True)
        
        # 每条消息间隔 1 秒，避免被 QQ 风控
        _time.sleep(1.0)
    
    return f"群发完成。总好友数: {total}, 成功: {success_count}, 失败: {fail_count}, 跳过: {skipped_count}"


def broadcast_to_all_groups(message: str, exclude_group_ids: str = "") -> str:
    """
    向所有已加入的QQ群批量发送消息（群发）。
    
    Args:
        message: 要发送的消息内容
        exclude_group_ids: 可选。要排除的群号列表，逗号分隔。例如 "12345,67890"
    
    Returns:
        str: 发送结果摘要
    """
    import time as _time
    client = get_qq_client()
    
    # 1. 获取群列表
    res = client.raw_api("get_group_list", {})
    if res.get("status") != "ok":
        return f"获取群列表失败: {res}"
    
    groups = res.get("data", [])
    if not groups:
        return "群列表为空，没有可发送的对象。"
    
    # 2. 解析排除列表
    exclude_set = set()
    if exclude_group_ids:
        for gid_str in exclude_group_ids.split(","):
            gid_str = gid_str.strip()
            if gid_str:
                try:
                    exclude_set.add(int(gid_str))
                except ValueError:
                    pass
    
    # 3. 逐个发送
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total = len(groups)
    
    for group in groups:
        gid = group.get("group_id")
        gname = group.get("group_name", "未知群")
        
        if not gid:
            continue
        
        if gid in exclude_set:
            skipped_count += 1
            continue
        
        try:
            # 模板变量替换：支持 {群名}/{group_name}, {群号}/{group_id}
            final_msg = message
            final_msg = final_msg.replace("{群名}", gname).replace("{group_name}", gname)
            final_msg = final_msg.replace("{群号}", str(gid)).replace("{group_id}", str(gid))
            
            client.send_group_msg(int(gid), final_msg)
            success_count += 1
            print(f"[QQ Broadcast] 已发送到群 {gname}({gid}) ({success_count}/{total})", flush=True)
        except Exception as e:
            fail_count += 1
            print(f"[QQ Broadcast] 发送到群 {gname}({gid}) 失败: {e}", flush=True)
        
        # 每条消息间隔 1.5 秒，群消息风控更严格
        _time.sleep(1.5)
    
    return f"群发完成。总群数: {total}, 成功: {success_count}, 失败: {fail_count}, 跳过: {skipped_count}"


def at_each_group_member(group_id: int, message: str, exclude_user_ids: str = "") -> str:
    """
    在指定QQ群内依次艾特每个成员并发送个性化消息。
    支持模板变量：{昵称}/{nickname}=群昵称, {qq}/{qq号}=QQ号, {群名片}/{card}=群名片
    
    Args:
        group_id: 群号
        message: 消息内容，支持模板变量
        exclude_user_ids: 可选。要排除的QQ号列表，逗号分隔
    
    Returns:
        str: 发送结果摘要
    """
    import time as _time
    client = get_qq_client()
    
    # 1. 获取群成员列表
    res = client.get_group_member_list(group_id)
    if res.get("status") != "ok":
        return f"获取群 {group_id} 成员列表失败: {res}"
    
    members = res.get("data", [])
    if not members:
        return f"群 {group_id} 成员列表为空。"
    
    # 2. 获取机器人自身QQ号，避免艾特自己
    try:
        login_info = client.raw_api("get_login_info", {})
        self_id = login_info.get("data", {}).get("user_id")
    except Exception:
        self_id = None
    
    # 3. 解析排除列表
    exclude_set = set()
    if exclude_user_ids:
        for uid_str in exclude_user_ids.split(","):
            uid_str = uid_str.strip()
            if uid_str:
                try:
                    exclude_set.add(int(uid_str))
                except ValueError:
                    pass
    if self_id:
        exclude_set.add(int(self_id))
    
    # 4. 逐个艾特并发送
    success_count = 0
    fail_count = 0
    skipped_count = 0
    total = len(members)
    
    for member in members:
        uid = member.get("user_id")
        nickname = member.get("nickname", "未知")
        card = member.get("card", "") or nickname  # 群名片，没有则用昵称
        
        if not uid:
            continue
        
        if uid in exclude_set:
            skipped_count += 1
            continue
        
        try:
            # 模板变量替换
            final_msg = message
            final_msg = final_msg.replace("{昵称}", nickname).replace("{nickname}", nickname)
            final_msg = final_msg.replace("{qq}", str(uid)).replace("{qq号}", str(uid))
            final_msg = final_msg.replace("{群名片}", card).replace("{card}", card)
            
            client.send_group_msg(int(group_id), final_msg, at_user_id=uid)
            success_count += 1
            print(f"[QQ @Each] 已艾特 {card}({uid}) ({success_count}/{total})", flush=True)
        except Exception as e:
            fail_count += 1
            print(f"[QQ @Each] 艾特 {card}({uid}) 失败: {e}", flush=True)
        
        # 每条消息间隔 2 秒，避免风控（群内连续艾特风控更严）
        _time.sleep(2.0)
    
    return f"群内逐个艾特完成。群号: {group_id}, 总成员: {total}, 成功: {success_count}, 失败: {fail_count}, 跳过(含自身): {skipped_count}"


def get_group_history(group_id: int) -> str:
    """
    获取最近的QQ群消息历史（依赖 NapCat/Go-CQHTTP 扩展 API）
    """
    client = get_qq_client()
    # 需要先确认 client 是否包含 get_group_msg_history 方法（已动态注入）
    if not hasattr(client, "get_group_msg_history"):
        return "错误：当前 QQ 客户端不支持获取历史消息 API"
        
    # 获取最近的 20 条
    try:
        result = client.get_group_msg_history(group_id)
        if result.get("status") == "ok":
            messages = result.get("data", {}).get("messages", [])
            # 格式化一下返回给大模型
            summary = []
            if not messages:
                return "暂无历史消息"
                
            for msg in messages[-15:]: # 只取最近15条，避免 Token 爆炸
                sender = msg.get("sender", {}).get("nickname", "Unknown")
                content = msg.get("raw_message", "") or msg.get("message", "")
                # 简单过滤空消息或纯图片（如果需要）
                if content:
                    summary.append(f"{sender}: {content}")
            return "\n".join(summary)
        return f"获取失败: {result}"
    except Exception as e:
        return f"获取历史消息异常: {e}"
