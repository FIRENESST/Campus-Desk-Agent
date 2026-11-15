import json
from typing import Any

import httpx2

from .core import AgentError, ToolSpec


class GraphQLToolError(AgentError):
    pass


class GraphQLProtocolError(GraphQLToolError):
    pass


class GraphQLRequestError(GraphQLToolError):
    pass


class GraphQLToolClient:
    def __init__(self, endpoint: str, client: httpx2.AsyncClient):
        self._endpoint = endpoint
        self._client = client

    async def _execute(
        self,
        query: str,
        variables: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._endpoint,
                json={"query": query, "variables": variables or {}},
            )
        except httpx2.TimeoutException as exc:
            raise TimeoutError(f"GraphQL {operation}超时") from exc
        except httpx2.HTTPError as exc:
            raise GraphQLRequestError(f"GraphQL {operation}请求失败") from exc
        if response.status_code != 200:
            raise GraphQLRequestError(f"GraphQL {operation}请求失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GraphQLProtocolError(f"GraphQL {operation}响应格式无效") from exc
        if not isinstance(payload, dict):
            raise GraphQLProtocolError(f"GraphQL {operation}响应格式无效")
        if payload.get("errors"):
            raise GraphQLRequestError(f"GraphQL {operation}请求失败")
        if not isinstance(payload.get("data"), dict):
            raise GraphQLProtocolError(f"GraphQL {operation}响应格式无效")
        return payload["data"]

    async def list_tools(self) -> list[ToolSpec]:
        data = await self._execute(
            "query AgentTools { agentTools { name description parameters } }",
            None,
            "工具清单",
        )
        tools = data.get("agentTools")
        if not isinstance(tools, list):
            raise GraphQLProtocolError("GraphQL 工具清单响应格式无效")

        specs = []
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("description"), str)
                or not isinstance(tool.get("parameters"), str)
            ):
                raise GraphQLProtocolError("GraphQL 工具清单响应格式无效")
            try:
                parameters = json.loads(tool["parameters"])
            except json.JSONDecodeError as exc:
                raise GraphQLProtocolError("GraphQL 工具参数 Schema 无效") from exc
            if not isinstance(parameters, dict):
                raise GraphQLProtocolError("GraphQL 工具参数 Schema 无效")
            specs.append(
                ToolSpec(
                    name=tool["name"],
                    description=tool["description"],
                    parameters=parameters,
                )
            )
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        data = await self._execute(
            "mutation CallTool($name: String!, $argumentsJson: String!) { "
            "callTool(name: $name, argumentsJson: $argumentsJson) { content isError } }",
            {
                "name": name,
                "argumentsJson": json.dumps(
                    arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            "工具调用",
        )
        result = data.get("callTool")
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("content"), str)
            or not isinstance(result.get("isError"), bool)
        ):
            raise GraphQLProtocolError("GraphQL 工具调用响应格式无效")
        if result["isError"]:
            return f"工具执行失败：\n{result['content']}"
        return result["content"]
