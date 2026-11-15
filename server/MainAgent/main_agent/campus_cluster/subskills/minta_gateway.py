"""Expose the campus simulations' query tools through MINTA."""

import json
import os
from typing import Annotated, Literal

import httpx2
from pydantic import Field, model_validator

from ...minta import MintaGatewayClient, MintaProtocolError, MintaRequestError
from ...reliability.models import ToolResult
from ..library_catalog import LIBRARY_TOOL_PAGE_SIZE, LibraryCatalogQuery, library_catalog
from ..skills import Skill, SkillContext, SkillInput


QUERY_TOOLS = {
    "gym": "gym_list_venues",
    "club": "club_list_activities",
    "library": "library_list_books",
    "classroom": "classroom_list_rooms",
    "schedule": "schedule_query",
}
DEFAULT_URL = "http://127.0.0.1:19000"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 8_000


class MintaGatewayInput(SkillInput):
    service: Literal["gym", "club", "library", "classroom", "schedule"]
    action: Literal["list_tools", "call_tool"]
    tool: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    arguments: dict[str, str | int] = Field(default_factory=dict, max_length=8)

    @model_validator(mode="after")
    def validate_call(self):
        if self.action == "list_tools":
            if self.tool is not None or self.arguments:
                raise ValueError("查看工具清单时只需指定服务和 action")
        elif self.tool != QUERY_TOOLS[self.service]:
            raise ValueError("请选择该服务已开放的查询工具")
        elif self.service == "library":
            LibraryCatalogQuery.model_validate(self.arguments)
        if len(json.dumps(self.arguments, ensure_ascii=False, allow_nan=False).encode()) > 4_096:
            raise ValueError("MINTA 工具参数超过大小限制")
        return self


def _connection_settings():
    address = os.getenv("MAIN_AGENT_MINTA_URL", "").strip() or DEFAULT_URL
    url = httpx2.URL(address)
    token = os.getenv("MAIN_AGENT_MINTA_TOKEN", "").strip()
    if (
        url.scheme not in {"http", "https"} or not url.host or url.port == 0
        or url.userinfo or url.query or url.fragment or any(char.isspace() or char == "\\" for char in address)
        or any(not 33 <= ord(char) <= 126 for char in token)
        or (token and url.scheme == "http" and url.host not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError("MINTA 地址或凭据配置无效")
    return address, {"Authorization": f"Bearer {token}"} if token else {}


def _error(service: str, code: str, message: str, **details) -> ToolResult:
    return ToolResult(json.dumps({
        "service": service, "error": {"code": code, "message": message, **details},
    }, ensure_ascii=False), status="error")


def _response_data(args: MintaGatewayInput, payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("MINTA 响应不是 JSON 对象")
    if args.action == "list_tools":
        tools = payload.get("tools")
        if not isinstance(tools, list):
            raise ValueError("MINTA 工具清单无效")
        permitted = QUERY_TOOLS[args.service]
        matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == permitted]
        if len(matches) != 1:
            raise ValueError("MINTA 查询工具未就绪")
        tool = matches[0]
        if not isinstance(tool.get("description"), str) or not isinstance(tool.get("parameters"), dict):
            raise ValueError("MINTA 工具定义无效")
        parameters = LibraryCatalogQuery.model_json_schema() if args.service == "library" else tool["parameters"]
        return {"tools": [{"name": permitted, "description": tool["description"], "parameters": parameters}]}
    content = payload.get("content")
    if payload.get("isError") is True or not isinstance(content, str) or not content.strip():
        raise ValueError("MINTA 工具回执无效")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = content
    if args.service == "library":
        if not isinstance(data, dict) or not isinstance(data.get("books"), list):
            raise ValueError("馆藏工具未返回图书列表")
        data = library_catalog(data["books"], LibraryCatalogQuery.model_validate(args.arguments),
                               page_size=LIBRARY_TOOL_PAGE_SIZE)
        data["books"] = data.pop("items")
    return {"tool": args.tool, "data": data}


async def call_minta_gateway(_context: SkillContext, args: MintaGatewayInput) -> ToolResult:
    try:
        address, headers = _connection_settings()
    except (httpx2.InvalidURL, ValueError):
        return _error(args.service, "invalid_configuration", "MINTA 网关配置无效")
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(8.0, connect=3.0),
                                     trust_env=False, follow_redirects=False) as http:
            gateway = MintaGatewayClient(address, client=http)
            if args.action == "list_tools":
                response = await gateway.call("GET", f"/{args.service}/agent/tools", timeout_ms=5_000)
            else:
                arguments = {} if args.service == "library" else args.arguments
                response = await gateway.call("POST", f"/{args.service}/agent/call", timeout_ms=5_000,
                                              json={"name": args.tool, "arguments": arguments})
    except TimeoutError:
        return _error(args.service, "gateway_timeout", "MINTA 网关响应超时")
    except MintaRequestError as exc:
        return _error(args.service, "gateway_error", "MINTA 网关暂不可用", http_status=exc.status_code)
    except MintaProtocolError:
        return _error(args.service, "invalid_response", "MINTA 网关响应格式无效")

    if not 200 <= response.status < 300:
        return _error(args.service, "backend_error", "MINTA 后端拒绝或未完成请求", http_status=response.status)
    if len(response.body) > MAX_RESPONSE_BYTES:
        return _error(args.service, "response_too_large", "MINTA 查询结果超过大小限制")
    try:
        data = _response_data(args, response.json())
        content = json.dumps({"source": "minta_simulation", "service": args.service, **data},
                             ensure_ascii=False, allow_nan=False)
        content.encode("utf-8")
    except (ValueError, RecursionError):
        return _error(args.service, "invalid_response", "MINTA 后端响应格式无效")
    if len(content) > MAX_OUTPUT_CHARS:
        return _error(args.service, "response_too_large", "MINTA 查询结果超过大小限制")
    return ToolResult(content, status="success")


MINTA_GATEWAY = Skill(
    "minta_gateway",
    "经 MINTA 网关查询校园模拟服务的场馆、社团活动、馆藏、教室和课表。"
    "使用 list_tools 查看查询工具及参数，再用 call_tool 获取结构化结果。",
    MintaGatewayInput, call_minta_gateway, read_only=True,
)
