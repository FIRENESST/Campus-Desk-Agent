"""One route catalog and authorization boundary for both transports."""

import re
from contextlib import asynccontextmanager
from hmac import compare_digest
from http.cookiejar import CookieJar, DefaultCookiePolicy
from ipaddress import ip_address

import anyio
import httpx2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from admin import LocalWorkers, RagSearch, worker_arguments
from catalog import ADMIN_INTERFACES, CAMPUS_PHOTO_GALLERY, USER_INTERFACES, WEB_ADMIN_INTERFACES, Interface
from minta_shortcuts import read_shortcut
from online_admin import OnlineAdmin
from protocol import MAX_BODY, MAX_CAMPUS_PHOTO_UPLOAD, MAX_RAG_UPLOAD, InterfaceError, json_input, read_body
from request_auth import RequestAuthorizationUnavailable, authorize_request
from rumor import student_request
from settings import Settings


HOP_HEADERS = frozenset((
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"proxy-connection",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
))
REQUEST_HEADERS = frozenset((
    b"accept", b"accept-encoding", b"accept-language", b"content-type", b"content-encoding",
    b"if-match", b"if-none-match", b"if-modified-since", b"if-unmodified-since", b"if-range", b"range",
    b"idempotency-key", b"user-agent",
))
MCP_HEADERS = frozenset((b"mcp-session-id", b"mcp-protocol-version", b"last-event-id", b"origin"))
LLM_HEADERS = frozenset((b"openai-organization", b"openai-project", b"openai-beta", b"x-request-id"))
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
PATH_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
BEARER = re.compile(r"(?i:bearer) ([\x21-\x7e]{1,256})\Z")
STREAM_TIMEOUT = httpx2.Timeout(180.0, connect=5.0, pool=5.0, read=None)
DIGEST_TIMEOUT = httpx2.Timeout(22.0, connect=3.0, pool=3.0)


def _connection_headers(headers):
    return {
        token.strip().lower()
        for name, value in headers if name.lower() == b"connection"
        for token in value.split(b",")
    }


def _loopback(address: str) -> bool:
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


def _matches(configured: str, supplied: str | None) -> bool:
    return bool(configured and supplied and compare_digest(configured, supplied))


def _bearer(request: Request) -> str | None:
    headers = request.headers.getlist("authorization")
    if not headers:
        return None
    matched = BEARER.fullmatch(headers[0]) if len(headers) == 1 else None
    if matched is None or b"authorization" in _connection_headers(request.headers.raw):
        raise InterfaceError(401, "invalid_credentials", "请提供一个有效的 Bearer 凭据")
    return matched[1]


def _require_access(request: Request, interface: Interface, settings: Settings) -> str | None:
    peer = request.client.host if request.client else ""
    local = _loopback(peer) and request.url.hostname in LOCAL_HOSTS
    if not local and request.url.scheme != "https":
        raise InterfaceError(426, "https_required", "请使用 HTTPS")

    access = interface.access
    if access == "web_admin":
        if not local:
            raise InterfaceError(403, "admin_bridge_denied", "请通过网站管理入口访问")
        return None
    if access == "public" and interface.path != "/v1/auth/sso/campus/start":
        return None
    if access not in {"public", "user"} and not local and not settings.allow_remote_admin:
        raise InterfaceError(403, "local_admin_only", "管理入口仅允许本机访问")
    token = _bearer(request)
    if access in {"public", "user"}:
        service_secrets = (settings.admin_token, settings.mcp_token, settings.llm_token, settings.llm_upstream_key)
        if (token or access == "public") and not any(_matches(secret, token) for secret in service_secrets):
            return token
    else:
        allowed = {
            "admin": (settings.admin_token,),
            "main_agent": (settings.mcp_token,),
            "model": (settings.admin_token, settings.llm_token),
        }[access]
        if not any(allowed):
            raise InterfaceError(503, "credentials_unconfigured", "请先配置此入口的服务凭据")
        if any(_matches(secret, token) for secret in allowed):
            return token
    raise InterfaceError(401, "invalid_credentials", "此入口需要有效的 Bearer 凭据")


def _upstream_url(base: str, path: str, query: bytes = b"") -> httpx2.URL:
    url = httpx2.URL(base)
    if path:
        url = url.copy_with(path=url.path.rstrip("/") + path)
    return url.copy_with(query=query or None)


def _request_headers(request: Request, service: str, token: str | None, *, browser: bool = False) -> list:
    excluded = HOP_HEADERS | _connection_headers(request.headers.raw)
    allowed = REQUEST_HEADERS
    if browser:
        allowed |= {b"cookie"}
    elif service == "mcp":
        allowed |= MCP_HEADERS
    elif service == "llm":
        allowed |= LLM_HEADERS
    headers = [(name, value) for name, value in request.headers.raw if name in allowed - excluded]
    if not any(name == b"accept-encoding" for name, _ in headers):
        headers.append((b"accept-encoding", b"identity"))
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
    if service == "main":
        headers.append((b"x-forwarded-proto", request.url.scheme.encode("ascii")))
        try:
            peer = str(ip_address(request.client.host)) if request.client else None
        except ValueError:
            peer = None
        if peer:
            headers.append((b"x-forwarded-for", peer.encode("ascii")))
    return headers


class UpstreamResponse(StreamingResponse):
    def __init__(self, upstream: httpx2.Response, service: str):
        self.upstream = upstream
        body = self._body()
        super().__init__(body, status_code=upstream.status_code)
        excluded = HOP_HEADERS | _connection_headers(upstream.headers.raw)
        excluded |= {b"authorization", b"cache-control", b"x-accel-buffering"}
        if service != "main":
            excluded |= {b"set-cookie"}
        self.raw_headers = [
            (name.lower(), value) for name, value in upstream.headers.raw if name.lower() not in excluded
        ]
        self.headers["Cache-Control"] = "no-store"
        self.headers["X-Content-Type-Options"] = "nosniff"
        if upstream.headers.get("content-type", "").lower().startswith("text/event-stream"):
            self.headers["X-Accel-Buffering"] = "no"

    async def _body(self):
        # Mock transports may pre-read a response. HTTP responses use the raw stream.
        if self.upstream.is_stream_consumed:
            if self.upstream.content:
                yield self.upstream.content
            return
        async for part in self.upstream.aiter_raw():
            yield part

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


class Gateway:
    def __init__(self, settings: Settings, client: httpx2.AsyncClient, workers: LocalWorkers):
        self.settings = settings
        self.client = client
        self.workers = workers
        self.online_admin = OnlineAdmin(settings)

    async def _send(
        self, method: str, url: httpx2.URL, headers: list, body: bytes = b"", *, stream_events: bool = False,
        timeout: httpx2.Timeout | None = None,
    ) -> httpx2.Response:
        try:
            request_timeout = STREAM_TIMEOUT if stream_events else timeout
            options = {"timeout": request_timeout} if request_timeout else {}
            outgoing = self.client.build_request(method, url, headers=headers, content=body, **options)
            return await self.client.send(outgoing, stream=True)
        except httpx2.TimeoutException:
            raise InterfaceError(504, "upstream_timeout", "上游服务响应超时") from None
        except (httpx2.RequestError, httpx2.InvalidURL):
            raise InterfaceError(502, "upstream_unavailable", "暂时无法访问上游服务") from None

    async def _check_user(self, request: Request, token: str, service=""):
        headers = _request_headers(request, "main", token)
        headers = [(name, value) for name, value in headers if name in {
            b"authorization", b"x-forwarded-proto", b"x-forwarded-for",
        }]
        path = "/v1/auth/access" + ("/" + service if service else "")
        reply = await self._send("GET", _upstream_url(self.settings.main_url, path), headers)
        try:
            if reply.status_code == 401:
                raise InterfaceError(401, "invalid_session", "MainAgent 会话无效")
            if reply.status_code == 403:
                try:
                    await reply.aread()
                    error = reply.json()["error"]
                    if error["code"] in {"campus_link_required", "campus_profile_required", "campus_access_denied"}:
                        raise InterfaceError(403, error["code"], error["message"])
                except (KeyError, TypeError, ValueError, httpx2.HTTPError):
                    pass
                raise InterfaceError(403, "invalid_session", "当前账号无权访问")
        finally:
            with anyio.CancelScope(shield=True):
                await reply.aclose()
        if reply.status_code != 204:
            raise InterfaceError(502, "session_check_failed", "MainAgent 暂时无法验证用户会话")
        return reply.headers

    async def handle(self, request: Request, interface: Interface) -> Response:
        token = _require_access(request, interface, self.settings)
        if interface.service != "web_rumor" and not all(PATH_VALUE.fullmatch(value) for value in request.path_params.values()):
            raise InterfaceError(422, "invalid_path_parameter", "路径参数无效")

        if self.settings.mpm_auth is not None:
            try:
                allowed = await anyio.to_thread.run_sync(
                    authorize_request, *self.settings.mpm_auth, request.method, interface.path,
                )
            except RequestAuthorizationUnavailable:
                raise InterfaceError(503, "supervisor_unavailable", "本机主管理器鉴权通道暂不可用") from None
            if not allowed:
                raise InterfaceError(403, "supervisor_denied", "本次请求未通过主管理器鉴权")

        if interface.access == "web_admin":
            return await self.online_admin.handle(
                request, interface, lambda: self._dispatch(request, interface, None), self.workers, self.client,
            )
        return await self._dispatch(request, interface, token)

    async def _dispatch(self, request: Request, interface: Interface, token: str | None) -> Response:
        service, target = interface.service, interface.target
        if service == "manager":
            if target == "health":
                return JSONResponse({"status": "ok", "service": "UnifiedInterfaceManagement"})
            interfaces = USER_INTERFACES if target == "user_interfaces" else ADMIN_INTERFACES
            return JSONResponse(
                {"interfaces": [entry.describe() for entry in interfaces]}, headers={"Cache-Control": "no-store"},
            )

        if service == "minta_shortcut":
            await read_body(request, 0)
            identity = await self._check_user(request, token, target)
            if not self.settings.minta_url:
                raise InterfaceError(503, "upstream_unconfigured", "MINTA 校务服务尚未配置")
            url = _upstream_url(self.settings.minta_url, "/v1/call")
            result = await read_shortcut(
                self.client, url, target, request.query_params,
                schedule_role=identity.get("X-Campus-Schedule-Role"),
                schedule_number=identity.get("X-Campus-Schedule-Number") or identity.get("X-Campus-Staff-Number"),
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})

        if service == "digest":
            await read_body(request, 0)
            await self._check_user(request, token)
            query = request.scope["query_string"]
            if len(query) > 4096 or (query and target.endswith("/events")):
                raise InterfaceError(422, "invalid_digest_query", "校园日报查询参数无效。")
            url = _upstream_url(f"http://127.0.0.1:{self.settings.campus_digest_port}", target, query)
            upstream = await self._send("GET", url, [(b"accept-encoding", b"identity")], timeout=DIGEST_TIMEOUT)
            return UpstreamResponse(upstream, service)

        if service == "rumor":
            await self._check_user(request, token)
            return await student_request(request, target, self.client, self.settings)

        limit = MAX_BODY
        if service == "rag_worker" and target == "ingest":
            limit = MAX_RAG_UPLOAD
        elif service == "main" and target == CAMPUS_PHOTO_GALLERY and request.method == "POST":
            limit = MAX_CAMPUS_PHOTO_UPLOAD
        body = await read_body(request, limit)
        if service in {"rag_worker", "routing_worker"}:
            arguments = worker_arguments(request, interface, body, self.settings.rag_scope)
            status, result = await self.workers.call(service, target, arguments)
            return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})

        query = request.scope["query_string"]
        if service == "rag" and target == "/search":
            search = json_input(request, body, RagSearch)
            if query:
                raise InterfaceError(422, "invalid_query", "检索参数请放在 JSON 请求体")
            scope = search.scope or self.settings.rag_scope
            if interface.access == "user":
                if scope != self.settings.rag_scope:
                    raise InterfaceError(403, "scope_forbidden", "一般用户只能检索已开放的资料范围")
                await self._check_user(request, token)
            body = search.model_copy(update={"scope": scope}).model_dump_json().encode("utf-8")
            headers = [(b"content-type", b"application/json"), (b"accept-encoding", b"identity")]
        else:
            upstream_token = None
            if service == "mcp" or (service == "main" and interface.access in {"user", "public"}):
                upstream_token = token
            elif service == "llm":
                upstream_token = self.settings.llm_upstream_key
            headers = _request_headers(
                request, service, upstream_token, browser=service == "main" and target == "/v1/auth/sso/campus/callback"
            )

        base = getattr(self.settings, f"{service}_url")
        if not base:
            raise InterfaceError(503, "upstream_unconfigured", "此上游服务尚未配置")
        try:
            destination = _upstream_url(base, target.format(**request.path_params), query)
        except httpx2.InvalidURL:
            raise InterfaceError(400, "invalid_query", "请求地址或查询参数无效") from None
        upstream = await self._send(request.method, destination, headers, body, stream_events=service in {"llm", "mcp"})
        return UpstreamResponse(upstream, service)


def create_app(settings: Settings, *, transport=None, workers=None) -> Starlette:
    @asynccontextmanager
    async def lifespan(app):
        # A pooled client must never reuse one user's upstream session cookie for another.
        cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=()))
        async with httpx2.AsyncClient(
            transport=transport, cookies=cookies, trust_env=False, follow_redirects=False,
            timeout=httpx2.Timeout(180.0, connect=5.0, pool=5.0),
        ) as client:
            app.state.gateway = Gateway(settings, client, workers if workers is not None else LocalWorkers(settings))
            yield

    def endpoint(interface):
        async def dispatch(request):
            return await request.app.state.gateway.handle(request, interface)
        return dispatch

    async def interface_error(request, exc):
        headers = {"Cache-Control": "no-store"}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(exc.payload(), status_code=exc.status, headers=headers)

    async def http_error(request, exc):
        codes = {404: "interface_not_found", 405: "method_not_allowed"}
        payload = {"error": {"code": codes.get(exc.status_code, "http_error"), "message": str(exc.detail)}}
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    app = Starlette(
        routes=[Route(entry.path, endpoint(entry), methods=entry.methods)
                for entry in (*USER_INTERFACES, *ADMIN_INTERFACES, *WEB_ADMIN_INTERFACES)],
        middleware=[Middleware(
            TrustedHostMiddleware, allowed_hosts=[*LOCAL_HOSTS, httpx2.URL(settings.public_url).raw_host.decode("ascii")],
            www_redirect=False,
        )],
        exception_handlers={InterfaceError: interface_error, HTTPException: http_error},
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app
