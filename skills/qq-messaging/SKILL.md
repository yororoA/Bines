---
name: qq-messaging
description: 查询 QQ 好友或群，并发送或广播 QQ 消息。
actions: send_qq_private_msg, send_qq_group_msg, get_qq_group_list, get_qq_friend_list, broadcast_to_all_friends, broadcast_to_all_groups, at_each_group_member
---
主人请求或可信自主活动可以发送消息，自主活动根据实际目标选择会话和消息组合。不能刷屏、催回复、泄露私聊或向无关收件人群发；contact_allowed=false时不发消息。成功回执不可重复发送，结果不确定先查询原编号。
