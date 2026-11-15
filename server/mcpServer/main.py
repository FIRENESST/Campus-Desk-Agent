import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

import httpx2
import uvicorn
from dotenv import dotenv_values
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

# Import the shared reader without loading MainAgent or its model dependencies.
sys.path.append(str(Path(__file__).resolve().parents[1] / "MainAgent" / "main_agent" / "campus_cluster" / "subskills"))
from domain_page_reader import DomainPageError, DomainPageResult, PublicDomain, fetch_domain_page

if __package__:
    from .ocr_tool import ocr_document
else:
    from ocr_tool import ocr_document


HOST = "127.0.0.1"
PORT = 8001
DEFAULT_RAG_SEARCH_URL = "http://127.0.0.1:8002/search"
LOCAL_ENV = Path(__file__).with_name(".env")
CAPABILITY_CATALOG_PATH = Path(__file__).resolve().parents[1] / "MainAgent" / ".runtime" / "subagent-capabilities.json"
MAX_CATALOG_BYTES = 128 * 1024
TOOL_STATUS_PATH = "/internal/status/tools"


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    rag_search_url: str = DEFAULT_RAG_SEARCH_URL
    rag_scope: str = "campus"
    runtime_status_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token):
            raise ValueError("MAIN_AGENT_MCP_TOKEN 需要 32 到 256 个 URL 安全字符")
        if self.runtime_status_key and (
            self.runtime_status_key == self.token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.runtime_status_key)
        ):
            raise ValueError("运行状态凭据格式无效")
        try:
            url = httpx2.URL(self.rag_search_url)
        except httpx2.InvalidURL:
            raise ValueError("RAG_SEARCH_URL 无效") from None
        if (
            url.scheme not in {"http", "https"} or not url.host
            or url.userinfo or url.query or url.fragment or not url.path.endswith("/search")
        ):
            raise ValueError("RAG_SEARCH_URL 需要填写完整的 HTTP/HTTPS 检索地址，以 /search 结尾")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.rag_scope):
            raise ValueError("MCP_RAG_SCOPE 需要 1 到 64 个英文字母、数字、下划线或连字符")

    @classmethod
    def from_env(cls) -> "Settings":
        local = dotenv_values(LOCAL_ENV, encoding="utf-8-sig") if LOCAL_ENV.is_file() else {}
        return cls(
            token=os.getenv("MAIN_AGENT_MCP_TOKEN", local.get("MAIN_AGENT_MCP_TOKEN") or "").strip(),
            rag_search_url=os.getenv("RAG_SEARCH_URL", local.get("RAG_SEARCH_URL") or DEFAULT_RAG_SEARCH_URL).strip(),
            rag_scope=os.getenv("MCP_RAG_SCOPE", local.get("MCP_RAG_SCOPE") or "campus").strip(),
            runtime_status_key=os.getenv("MPM_RUNTIME_STATUS_KEY", ""),
        )


class RequireMainAgent:
    def __init__(self, app: ASGIApp, *, token: str):
        self.app = app
        self.token = token.encode("ascii")

    def _authorized(self, scope: Scope) -> bool:
        headers = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
        if len(headers) != 1:
            return False
        scheme, _, token = headers[0].partition(b" ")
        return scheme.lower() == b"bearer" and compare_digest(token, self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != TOOL_STATUS_PATH and not self._authorized(scope):
            response = JSONResponse(
                {"error": "需要有效的 MainAgent 凭据"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RagHit(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    chunk_id: str
    document_id: str
    chunk_index: int
    source: str
    text: str
    score: float


class RagSearchResult(BaseModel):
    scope: str
    hits: list[RagHit] = Field(max_length=50)


class ActionAccess(BaseModel):
    roles: list[str]
    required_fields: list[str]
    checks: list[str]
    example: str


class ActionCapability(BaseModel):
    id: str
    label: str
    access: list[ActionAccess]
    arguments: dict[str, str] = Field(default_factory=dict)


class SkillCapability(BaseModel):
    name: str
    description: str
    read_only: bool
    actions: list[ActionCapability] = Field(default_factory=list)


class RegisteredSkill(SkillCapability):
    enabled: bool


class SubagentCapability(BaseModel):
    id: str
    name: str
    responsibility: str
    skills: list[RegisteredSkill]


class SkillAssignment(BaseModel):
    id: str
    name: str
    enabled: bool


class SubskillCapability(SkillCapability):
    registered_agents: list[SkillAssignment]


class SubagentCatalog(BaseModel):
    agents: list[SubagentCapability]
    skills: list[SubskillCapability]


def read_capability_catalog() -> SubagentCatalog:
    try:
        with CAPABILITY_CATALOG_PATH.open("rb") as handle:
            content = handle.read(MAX_CATALOG_BYTES + 1)
        if len(content) > MAX_CATALOG_BYTES:
            raise ValueError("清单超出大小限制")
        return SubagentCatalog.model_validate_json(content, strict=True)
    except FileNotFoundError:
        raise ToolError("MainAgent 尚未发布子智能体能力清单") from None
    except (OSError, ValueError):
        raise ToolError("无法读取子智能体能力清单") from None


def test_tool() -> str:
    """get current time"""
    return datetime.now().astimezone().isoformat()


def create_app(settings: Settings) -> Starlette:
    mcp = MCPServer("Simple-MCP-Server")
    mcp.add_tool(test_tool)
    mcp.add_tool(ocr_document, annotations=types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
    ))

    @mcp.tool(annotations=types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    async def get_subagent_capabilities() -> SubagentCatalog:
        """读取校园子智能体、已登记技能及各办理动作的角色和前置条件。"""
        return await asyncio.to_thread(read_capability_catalog)

    @mcp.tool(annotations=types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
    ))
    async def read_domain_page(domain: PublicDomain) -> DomainPageResult:
        """给定公开域名，读取 HTTPS 首页并返回标题、最终来源 URL 和静态正文。"""
        try:
            return await fetch_domain_page(domain)
        except DomainPageError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True,
    ))
    async def search_rag(
        query: Annotated[str, Field(min_length=1, max_length=4000)],
        top_k: Annotated[int, Field(strict=True, ge=1, le=50)] = 5,
    ) -> RagSearchResult:
        """检索指定校园资料库，返回片段原文、来源、片段 ID 和相似度。"""
        query = query.strip()
        if not query:
            raise ToolError("请填写检索问题")
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(40, connect=3), trust_env=False, follow_redirects=False,
            ) as client:
                response = await client.post(
                    settings.rag_search_url,
                    json={"query": query, "scope": settings.rag_scope, "top_k": top_k},
                )
                response.raise_for_status()
        except httpx2.TimeoutException:
            raise ToolError("RAG 检索超时，请稍后重试") from None
        except httpx2.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (400, 422):
                raise ToolError("RAG 拒绝了检索参数，请检查查询内容和长度") from None
            raise ToolError(f"RAG 检索失败（HTTP {status}）") from None
        except httpx2.RequestError:
            raise ToolError("无法连接 RAG 查询服务，请检查服务和 RAG_SEARCH_URL") from None
        try:
            return RagSearchResult(scope=settings.rag_scope, hits=response.json()["hits"])
        except (ValueError, KeyError, TypeError):
            raise ToolError("RAG 返回的检索结果格式无效") from None

    async def tool_status(request):
        try:
            local = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        headers = request.headers.getlist("x-campus-runtime-key")
        if not (local and settings.runtime_status_key and len(headers) == 1
                and compare_digest(settings.runtime_status_key.encode(), headers[0].encode())):
            return JSONResponse({"error": "运行状态入口未开放"}, status_code=404, headers={"Cache-Control": "no-store"})
        tools = await mcp.list_tools()
        return JSONResponse({"items": [
            {"name": tool.name, "description": tool.description,
             "read_only": tool.annotations.model_dump(by_alias=True).get("readOnlyHint") if tool.annotations else None}
            for tool in tools
        ]}, headers={"Cache-Control": "no-store"})

    app = mcp.streamable_http_app(host=HOST, streamable_http_path="/mcp")
    app.router.routes.insert(0, Route(TOOL_STATUS_PATH, tool_status))
    app.add_middleware(RequireMainAgent, token=settings.token)
    return app


if __name__ == "__main__":
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    uvicorn.run(create_app(settings), host=HOST, port=PORT, access_log=False)
