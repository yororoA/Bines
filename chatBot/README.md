# Bines ChatBot 模块

该模块封装了 NapCatQQ (OneBot V11) 的 API 调用，用于 Bines 系统与 QQ 进行交互。

## 依赖
需要安装 `requests` 库:
```bash
pip install requests
```

## 使用方法

```python
from chatBot.napcat_client import NapCatClient

# 初始化
# 请修改为你的实际地址和Token
client = NapCatClient(base_url="http://127.0.0.1:6100", token="bD0fkzc3CsaAC-70")

# 1. 发送私聊消息
client.send_private_msg(user_id=123456789, message="你好，这是一条测试消息")

# 2. 发送群聊消息
client.send_group_msg(group_id=987654321, message="大家好")

# 3. 发送群聊消息并艾特某人
client.send_group_msg(group_id=987654321, message="请查收文件", at_user_id=123456789)

# 4. 发送群聊消息并艾特全体
client.send_group_msg(group_id=987654321, message="重要通知", at_user_id="all")

# 5. 调用其他任意 API
client.raw_api("get_status", {})
```

## 目录结构
- `napcat_client.py`: 核心客户端类实现。
