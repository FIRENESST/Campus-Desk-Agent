import json
import uuid
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .core import AgentError, ToolSpec


class WebSocketToolError(AgentError):
    pass


class WebSocketProtocolError(WebSocketToolError):
    pass


class WebSocketRequestError(WebSocketToolError):
    pass


class WebSocketToolClient:
    def __init__(self, url: str, *, connect=connect):
        self._url = url
        self._connect = connect

    async def _exchange(self, message_type: str, **fields) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "type": message_type, **fields}
        try:
            async with self._connect(self._url) as connection:
                await connection.send(json.dumps(request, ensure_ascii=False))
                message = await connection.recv()
        except TimeoutError as exc:
            raise TimeoutError("WebSocket 工具请求超时") from exc
        except (WebSocketException, OSError) as exc:
            raise WebSocketRequestError("WebSocket 工具请求失败") from exc
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError) as exc:
            raise WebSocketProtocolError("WebSocket 工具响应不是合法 JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("id") != request_id
            or not isinstance(payload.get("type"), str)
        ):
            raise WebSocketProtocolError("WebSocket 工具响应关联信息无效")
        return payload

    async def list_tools(self) -> list[ToolSpec]:
        payload = await self._exchange("list_tools")
        tools = payload.get("tools")
        if payload["type"] != "tools" or not isinstance(tools, list):
            raise WebSocketProtocolError("WebSocket 工具清单响应格式无效")
        specs = []
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("description"), str)
                or not isinstance(tool.get("parameters"), dict)
            ):
                raise WebSocketProtocolError("WebSocket 工具清单响应格式无效")
            specs.append(
                ToolSpec(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool["parameters"],
                )
            )
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        payload = await self._exchange(
            "call_tool",
            name=name,
            arguments=arguments,
        )
        if (
            payload["type"] != "tool_result"
            or not isinstance(payload.get("content"), str)
            or not isinstance(payload.get("isError"), bool)
        ):
            raise WebSocketProtocolError("WebSocket 工具调用响应格式无效")
        if payload["isError"]:
            return f"工具执行失败：\n{payload['content']}"
        return payload["content"]
