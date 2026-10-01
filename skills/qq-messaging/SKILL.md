---
name: qq-messaging
description: 查询 QQ 好友或群，并发送或广播 QQ 消息。
actions: send_qq_private_msg, send_qq_group_msg, get_qq_group_list, get_qq_friend_list, broadcast_to_all_friends, broadcast_to_all_groups, at_each_group_member
---
只有在用户明确要求发送消息时才发送。批量广播或逐个 @ 成员属于高影响操作，应先确认收件范围和完整消息；不得自行扩大范围。
