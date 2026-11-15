
from __future__ import annotations

import json
import logging
import math
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path

import anyio
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

logger = logging.getLogger("uvicorn.error")

FORWARDED_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "openai-organization",
    "openai-project",
    "openai-beta",
    "idempotency-key",
    "x-request-id",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    base_url: str
    default_model: str = ""
    port: int = 8000
    timeout: float = 300.0
    local_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        # 路径相对于脚本，已有的系统环境变量优先于 .env。
        load_dotenv(Path(__file__).with_name(".env"), encoding="utf-8-sig")
        api_key = os.getenv("LLM_API_KEY", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
        if not api_key:
            raise ValueError("请在 .env 或环境变量中设置 LLM_API_KEY")
        if not api_key.isascii() or any(char.isspace() for char in api_key):
            raise ValueError("LLM_API_KEY 必须是服务商提供的 API Key，不能包含空白字符")
        try:
            url = httpx.URL(base_url)
        except httpx.InvalidURL:
            raise ValueError("LLM_BASE_URL 不是有效的 HTTP/HTTPS 地址") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("LLM_BASE_URL 必须是 HTTP/HTTPS 基础地址，不含凭据、查询参数或片段")
        try:
            port = int(os.getenv("LOCAL_PORT", "8000"))
            timeout = float(os.getenv("LLM_TIMEOUT", "300"))
        except ValueError:
            raise ValueError("LOCAL_PORT 必须是整数，LLM_TIMEOUT 必须是秒数") from None
        if not 1 <= port <= 65535:
            raise ValueError("LOCAL_PORT 必须在 1 到 65535 之间")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("LLM_TIMEOUT 必须是大于 0 的有限秒数")
        return cls(
            api_key=api_key,
            base_url=str(url).rstrip("/"),
            default_model=os.getenv("LLM_MODEL", "").strip(),
            port=port,
            timeout=timeout,
            local_api_key=os.getenv("LOCAL_API_KEY", "").strip(),
        )


def error_response(status: int, message: str, code: str) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={"error": {"message": message, "type": "proxy_error", "code": code}},
    )


class ProxyStreamingResponse(StreamingResponse):
    """保留上游原始字节，并在结束、断开或出错时释放上游连接。"""

    def __init__(self, upstream: httpx.Response) -> None:
        self.upstream = upstream
        connection_headers = {
            name.strip().lower()
            for name in upstream.headers.get("connection", "").split(",")
            if name.strip()
        }
        excluded = HOP_BY_HOP_HEADERS | connection_headers | {
            "content-length", "authorization", "set-cookie"
        }
        headers = {
            name: value for name, value in upstream.headers.items() if name not in excluded
        }
        if "text/event-stream" in headers.get("content-type", "").lower():
            headers["cache-control"] = "no-cache"
            headers["x-accel-buffering"] = "no"
        super().__init__(
            content=upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=headers,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 客户端断开会取消响应任务；关闭连接时避免被同一取消信号打断。
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings.from_env()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(config.timeout, connect=10.0, pool=10.0),
            follow_redirects=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
        ) as client:
            app.state.settings = config
            app.state.client = client
            yield

    app = FastAPI(title="本地大模型转发服务", lifespan=lifespan)

    @app.get("/health")
    async def health():
        # 仅检查本地服务，不调用上游，也不泄露配置或 API Key。
        return {"status": "ok"}

    @app.get("/v1/models")
    @app.post("/v1/chat/completions")
    @app.post("/v1/completions")
    @app.post("/v1/embeddings")
    @app.post("/v1/responses")
    async def proxy(request: Request):
        config: Settings = request.app.state.settings
        client: httpx.AsyncClient = request.app.state.client
        if config.local_api_key:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                token.encode("utf-8"), config.local_api_key.encode("utf-8")
            ):
                return error_response(401, "本地 API Key 无效", "invalid_local_api_key")

        body = b""
        if request.method == "POST":
            try:
                payload = await request.json()
                if not isinstance(payload, dict):
                    return error_response(400, "请求 JSON 必须是对象", "invalid_request")
                if payload.get("model") in (None, ""):
                    if not config.default_model:
                        return error_response(
                            400, "请提供 model 或设置 LLM_MODEL", "missing_model"
                        )
                    payload["model"] = config.default_model
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return error_response(400, "请求体必须是有效的 JSON", "invalid_json")

        headers = {
            name: value
            for name, value in request.headers.items()
            if name in FORWARDED_REQUEST_HEADERS
        }
        headers["authorization"] = f"Bearer {config.api_key}"
        headers["accept-encoding"] = "identity"
        if request.method == "POST":
            headers["content-type"] = "application/json"
        # 本地 /v1 是客户端接口前缀；上游基础地址是否带 /v1 由配置决定。
        suffix = request.url.path.removeprefix("/v1")
        url = httpx.URL(config.base_url + suffix).copy_with(
            query=request.scope["query_string"] or None
        )
        upstream_request = client.build_request(
            request.method, url, headers=headers, content=body
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return error_response(504, "上游服务响应超时", "upstream_timeout")
        except httpx.RequestError as exc:
            logger.warning("上游连接失败: %s", type(exc).__name__)
            return error_response(502, "无法连接上游服务", "upstream_unavailable")
        return ProxyStreamingResponse(upstream)

    return app


app = create_app()


if __name__ == "__main__":
    try:
        config = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(f"配置错误: {exc}") from None
    uvicorn.run(
        create_app(config), host="127.0.0.1", port=config.port, proxy_headers=False
    )
