from typing import Any

import httpx2

from .core import AgentError, ToolSpec


class RestToolError(AgentError):
    pass


class RestProtocolError(RestToolError):
    pass


class RestRequestError(RestToolError):
    pass


class RestToolClient:
    def __init__(self, base_url: str, client: httpx2.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def _request(self, method: str, path: str, operation: str, **kwargs):
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                **kwargs,
            )
        except httpx2.TimeoutException as exc:
            raise TimeoutError(f"REST {operation}超时") from exc
        except httpx2.HTTPError as exc:
            raise RestRequestError(f"REST {operation}请求失败") from exc
        if response.status_code >= 500:
            raise RestRequestError(f"REST {operation}请求失败")
        return response

    @staticmethod
    def _json(response, operation: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise RestProtocolError(f"REST {operation}响应格式无效") from exc

    async def list_tools(self) -> list[ToolSpec]:
        response = await self._request("GET", "/agent/tools", "工具清单")
        if response.status_code != 200:
            raise RestRequestError("REST 工具清单请求失败")
        payload = self._json(response, "工具清单")
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
            raise RestProtocolError("REST 工具清单响应格式无效")

        specs = []
        for tool in payload["tools"]:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("description"), str)
                or not isinstance(tool.get("parameters"), dict)
            ):
                raise RestProtocolError("REST 工具清单响应格式无效")
            specs.append(
                ToolSpec(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=tool["parameters"],
                )
            )
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._request(
            "POST",
            "/agent/call",
            "工具调用",
            json={"name": name, "arguments": arguments},
        )
        payload = self._json(response, "工具调用")
        if 400 <= response.status_code < 500:
            if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
                raise RestProtocolError("REST 工具错误响应格式无效")
            error = payload["error"]
            if not isinstance(error.get("code"), str) or not isinstance(
                error.get("message"), str
            ):
                raise RestProtocolError("REST 工具错误响应格式无效")
            return f"工具执行失败：\n{error['code']}: {error['message']}"
        if response.status_code != 200:
            raise RestRequestError("REST 工具调用请求失败")
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise RestProtocolError("REST 工具调用响应格式无效")
        return payload["content"]
