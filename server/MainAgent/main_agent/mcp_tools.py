import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from dotenv import dotenv_values
from mcp import ClientSession, MCPError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .core import AgentError, ToolSpec, UnavailableToolError
from .reliability.models import ToolResult


MAIN_AGENT_MCP_URL = "http://127.0.0.1:8001/mcp"
LOCAL_MCP_ENV = Path(__file__).resolve().parents[2] / "mcpServer" / ".env"
RAG_SEARCH_DESCRIPTION = "查已入库的校园规章、学籍制度与校园资料，返回可引用的原文片段和来源。"
RAG_QUERY_DESCRIPTION = (
    "写明要查的对象和条款，保留已知文件标题、文号、版次和适用人群。"
    "同一资料库内的几个证据缺口可用分号分隔。"
)


class MCPToolError(AgentError):
    pass


class MCPProtocolError(MCPToolError):
    pass


class MCPRequestError(MCPToolError):
    pass


class MCPToolClient:
    def __init__(self, session: ClientSession):
        self._session = session

    async def list_tools(self) -> list[ToolSpec]:
        specs = []
        cursor = None
        seen_cursors = set()
        seen_tool_names = set()

        while True:
            params = (
                types.PaginatedRequestParams(cursor=cursor)
                if cursor is not None
                else None
            )
            try:
                page = await self._session.list_tools(params=params)
            except httpx2.TimeoutException as exc:
                raise TimeoutError("MCP 请求超时") from exc
            except (MCPError, httpx2.HTTPError) as exc:
                raise MCPRequestError("MCP 请求失败") from exc
            for tool in page.tools:
                if tool.name in seen_tool_names:
                    raise MCPProtocolError(f"MCP 返回了重复工具：{tool.name}")
                seen_tool_names.add(tool.name)
                specs.append(
                    ToolSpec(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=tool.input_schema,
                    )
                )
            cursor = page.next_cursor
            if cursor is None:
                return specs
            if cursor in seen_cursors:
                raise MCPProtocolError("MCP 工具列表返回了重复分页游标")
            seen_cursors.add(cursor)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            result = await self._session.call_tool(name, arguments)
        except httpx2.TimeoutException as exc:
            raise TimeoutError("MCP 请求超时") from exc
        except (MCPError, httpx2.HTTPError) as exc:
            raise MCPRequestError("MCP 请求失败") from exc
        if not isinstance(result, types.CallToolResult):
            raise MCPProtocolError(
                f"MCP 工具返回了不支持的结果类型：{type(result).__name__}"
            )
        if result.structured_content is not None:
            content = json.dumps(
                result.structured_content,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            text_parts = []
            for item in result.content:
                if not isinstance(item, types.TextContent):
                    raise MCPProtocolError(
                        f"MCP 工具返回了不支持的内容类型：{item.type}"
                    )
                text_parts.append(item.text)
            content = "\n".join(text_parts)
        if result.is_error:
            return f"工具执行失败：\n{content}"
        return content


@asynccontextmanager
async def connect_mcp_http(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    trust_env: bool = True,
) -> AsyncIterator[MCPToolClient]:
    async with AsyncExitStack() as stack:
        if trust_env:
            http_client = create_mcp_http_client(headers=headers, timeout=timeout)
        else:
            http_client = httpx2.AsyncClient(
                headers=headers, timeout=timeout or httpx2.Timeout(30, read=300),
                trust_env=False, follow_redirects=False,
            )
        await stack.enter_async_context(http_client)
        read_stream, write_stream = await stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        yield MCPToolClient(session)


@asynccontextmanager
async def connect_main_agent_mcp(
    *, url: str = MAIN_AGENT_MCP_URL, token: str | None = None,
) -> AsyncIterator[MCPToolClient]:
    """Connect the main orchestrator to its authenticated MCP tools."""
    if token is None:
        token = os.getenv("MAIN_AGENT_MCP_TOKEN")
    if token is None and url == MAIN_AGENT_MCP_URL and LOCAL_MCP_ENV.is_file():
        token = dotenv_values(LOCAL_MCP_ENV, encoding="utf-8-sig").get("MAIN_AGENT_MCP_TOKEN")
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise MCPRequestError("请设置有效的 MAIN_AGENT_MCP_TOKEN")
    async with connect_mcp_http(
        url, headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(45, connect=3), trust_env=False,
    ) as client:
        yield client


async def load_subagent_capabilities(local_agents: list[dict]) -> list[dict]:
    """Read the local MCP catalog, using the active registries if it is stale or unavailable."""
    try:
        async with asyncio.timeout(2):
            async with connect_main_agent_mcp() as client:
                receipt = await client.call_tool("get_subagent_capabilities", {})
        if len(receipt) <= 128 * 1024:
            catalog = json.loads(receipt)
            if isinstance(catalog, dict) and catalog.get("agents") == local_agents:
                return catalog["agents"]
    except (TimeoutError, MCPError, MCPToolError, httpx2.HTTPError, ValueError, RecursionError, ExceptionGroup):
        pass
    return local_agents


class MainAgentRAGClient:
    """Expose the MCP server's scoped campus search to the chat orchestrator."""

    async def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec("search_rag", RAG_SEARCH_DESCRIPTION, {
            "type": "object",
            "properties": {"query": {
                "type": "string", "minLength": 1, "maxLength": 4000, "description": RAG_QUERY_DESCRIPTION,
            }},
            "required": ["query"],
            "additionalProperties": False,
        })]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name != "search_rag":
            raise UnavailableToolError("该工具未接入聊天流程")
        query = arguments.get("query")
        if set(arguments) != {"query"} or not isinstance(query, str) or not 1 <= len(query.strip()) <= 4000:
            return ToolResult("请提供有效的资料检索问题。", "error")
        try:
            async with asyncio.timeout(20):
                async with connect_main_agent_mcp() as client:
                    receipt = await client.call_tool("search_rag", {"query": query.strip(), "top_k": 5})
            if len(receipt) <= 24000 and not receipt.startswith("工具执行失败："):
                data = json.loads(receipt)
                if isinstance(data, dict) and isinstance(data.get("hits"), list):
                    return ToolResult(receipt, "success", reuse_for=60 if data["hits"] else 0)
        except (TimeoutError, MCPError, MCPToolError, httpx2.HTTPError, ValueError, RecursionError, ExceptionGroup):
            pass
        return ToolResult("校园资料库暂时无法检索。", "error")
