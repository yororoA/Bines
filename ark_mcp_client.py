"""Ark MCP adapter for synchronous SkillRunner actions (official Python SDK v2).

No model protocol, memory or autonomous behaviour is changed here. Credentials
are loaded lazily, so the legacy runtime does not require the MCP dependency.
"""
import asyncio
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4


class ArkMcpError(RuntimeError):
    def __init__(self, code: str, message: str, request_id: str | None = None):
        self.code = code
        self.message = message
        self.request_id = request_id
        suffix = f" request_id={request_id}；请用 get_ark_operation 查询，勿换编号重发。" if request_id else ""
        super().__init__(f"{code}: {message}{suffix}")


def _setting(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name].strip()
    try:
        import config
    except ImportError:
        return default
    return str(getattr(config, name, default)).strip()


@dataclass(frozen=True)
class McpConfig:
    url: str
    uid: str
    token: str = field(repr=False)
    timeout: float = 20.0


def mcp_config() -> McpConfig | None:
    mode = _setting("ARK_TRANSPORT", "auto").lower()
    if mode not in {"auto", "mcp", "legacy"}:
        raise ArkMcpError("CONFIG_ERROR", "ARK_TRANSPORT 必须为 auto、mcp 或 legacy。")
    if mode == "legacy":
        return None
    url, uid, token = _setting("ARK_MCP_URL"), _setting("ARK_MCP_UID"), _setting("ARK_MCP_TOKEN")
    if mode == "auto" and not url and not uid and not token:
        return None
    if not url or not uid or not token:
        raise ArkMcpError("CONFIG_ERROR", "请同时配置 ARK_MCP_URL、ARK_MCP_UID 和 ARK_MCP_TOKEN。")
    if len(uid) > 128 or not uid.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in uid):
        raise ArkMcpError("CONFIG_ERROR", "ARK_MCP_UID 必须是有效的账号 UID。")
    parsed = urlsplit(url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.hostname or (parsed.scheme != "https" and not local_http) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ArkMcpError("CONFIG_ERROR", "MCP URL 必须为 HTTPS（本机允许 HTTP），且不含凭证、查询或片段。")
    try:
        timeout = float(_setting("ARK_MCP_TIMEOUT", "20"))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError()
    except ValueError:
        raise ArkMcpError("CONFIG_ERROR", "ARK_MCP_TIMEOUT 必须为 1–120 秒。") from None
    return McpConfig(url, uid, token, timeout)


def _decode_result(result) -> dict:
    payload = result.structured_content
    if payload is None:
        for content in result.content:
            if content.type == "text":
                try:
                    payload = json.loads(content.text)
                except (TypeError, ValueError):
                    continue
                break
    if not isinstance(payload, dict):
        raise ArkMcpError("INVALID_RESPONSE", "MCP 返回无效结果，请检查工具参数或服务版本。")
    if result.is_error or payload.get("ok") is not True:
        error = payload.get("error") or {}
        raise ArkMcpError(error.get("code", "TOOL_ERROR"), error.get("message", "工具调用失败。"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ArkMcpError("INVALID_RESPONSE", "MCP data 必须为对象。")
    return data


async def _call(config: McpConfig, name: str, arguments: dict) -> dict:
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        raise ArkMcpError("MISSING_DEPENDENCY", "请使用 Python 3.11+ 安装 requirements-mcp.txt。") from None
    # An overall deadline also bounds initialize/cleanup. No automatic replay
    # of writes: even a timeout may occur after a successful database commit.
    try:
        async with asyncio.timeout(config.timeout):
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {config.token}", "X-Bines-Uid": config.uid},
                timeout=config.timeout, follow_redirects=False,
            ) as client:
                async with streamable_http_client(config.url, http_client=client) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=config.timeout) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
    except Exception:
        # Transport exception groups can include URLs/headers. Do not put
        # their raw representation in model context or application logs.
        raise ArkMcpError("TRANSPORT_ERROR", "MCP 连接失败或超时，请检查网络、凭证、权限和服务状态。") from None
    return _decode_result(result)


def call_ark(name: str, arguments: dict | None = None, *, write: bool = False) -> dict:
    config = mcp_config()
    if config is None:
        raise ArkMcpError("MCP_REQUIRED", "此动作需要配置 Ark MCP；当前为 legacy REST 模式。")
    args = dict(arguments or {})
    if write:
        args["request_id"] = args.get("request_id") or str(uuid4())
    request_id = args.get("request_id") if write else None
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            data = asyncio.run(_call(config, name, args))
        else:
            # SkillRunner can be invoked by an async runtime; do not nest
            # asyncio.run on its loop or change its synchronous contract.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ark-mcp") as pool:
                data = pool.submit(lambda: asyncio.run(_call(config, name, args))).result()
    except ArkMcpError as error:
        raise ArkMcpError(error.code, error.message, request_id) from None
    return {"message": "ok", "data": data, **({"request_id": request_id} if request_id else {})}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="检查 Ark 配置和只读身份连接（不会发文或发送聊天）。")
    parser.add_argument("--check", action="store_true", help="连接 MCP 并验证身份")
    options = parser.parse_args()
    try:
        configuration = mcp_config()
        if configuration is None:
            print("Ark: legacy REST；身份使用 MOMENTS_UID / MOMENTS_TOKEN，状态使用旧 WebSocket。")
        elif options.check:
            profile = call_ark("ark_get_profile")["data"]
            print(f"Ark MCP: 已连接 · {profile['displayName']} · UID {profile['uid']}")
            print("权限: " + ", ".join(profile["scopes"]))
        else:
            print("Ark MCP: 配置有效；加 --check 可验证连接。")
    except ArkMcpError as error:
        print(str(error))
        raise SystemExit(1)
