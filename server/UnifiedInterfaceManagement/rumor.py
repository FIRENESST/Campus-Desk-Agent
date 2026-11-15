"""Gateway clients for the local campus rumor-control server."""

import re
import secrets
from urllib.parse import unquote, urlsplit

import httpx2
from starlette.responses import Response

from protocol import InterfaceError, parse_json, read_body


ADMIN_PREFIX = "/admin/rumor"
ADMIN_NAVIGATION = {"/", "/admin", "/posts", "/topics", "/rumors", "/trace", "/reports", "/spread",
                    "/radar", "/settings", "/chat", "/student"}
_NAVIGATION_DIRS = ("/posts/", "/topics/", "/rumors/", "/reports/", "/student/reports/")
_MAX_RESPONSE = 18 * 1024 * 1024
_PAGE_LINK = re.compile(rb'((?:href|src|action|data-href|data-job)=["\x27])(/(?!/))')
_SCRIPT_LINK = re.compile(rb'((?:\.href|\.src)\s*=\s*["\x27])(/(?!/))')
_UNSAFE_PATH = re.compile(r"[\x00-\x1f\x7f%\\]")
_JSON = {"Accept": "application/json"}
_CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self' 'nonce-{nonce}'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
_JS_PREFIX = (
    '"use strict";\nconst rumorPath = path => typeof path === "string" && path.startsWith("/") && !path.startsWith("//")'
    ' ? "/admin/rumor" + path : path;\n'
)


def _headers(settings, role, **extra):
    if not settings.campus_rumor_key:
        raise InterfaceError(503, "rumor_unavailable", "请通过主管理器启动校园传闻服务。")
    return {
        "X-Campus-Rumor-Key": settings.campus_rumor_key, "X-Campus-Rumor-Audience": role, **extra,
    }


async def _request(client, settings, method, path, headers, *, body=None, query=b""):
    url = httpx2.URL(f"http://127.0.0.1:{settings.campus_rumor_port}", path=path, query=query or None)
    try:
        async with client.stream(method, url, headers=headers, content=body, timeout=httpx2.Timeout(80, connect=2)) as upstream:
            data = bytearray()
            async for chunk in upstream.aiter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_RESPONSE:
                    raise InterfaceError(502, "rumor_response_too_large", "谣言防控服务返回内容过大。")
            return upstream.status_code, upstream.headers, bytes(data)
    except httpx2.HTTPError:
        raise InterfaceError(502, "rumor_unavailable", "校园传闻服务暂不可用，请稍后重试。") from None


async def student_request(request, target, client, settings):
    headers = _headers(settings, "student", **_JSON)
    if request.query_params:
        raise InterfaceError(422, "invalid_query", "此入口无需查询参数。")
    body = None
    if target == "tips":
        body = await read_body(request, 12 * 1024)
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            raise InterfaceError(415, "unsupported_media_type", "请提交 JSON 表单。")
        try:
            payload = parse_json(body)
            text = payload["content"]
            if set(payload) != {"content"} or not isinstance(text, str) or not 5 <= len(text.strip()) <= 2000:
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise InterfaceError(422, "invalid_tip", "反馈内容须为 5 至 2,000 字。") from None
        headers["Content-Type"] = "application/json"
    else:
        await read_body(request, 0)
    status, reply, data = await _request(client, settings, request.method,
                                         f"/internal/campus-desk/{target}", headers, body=body)
    if status not in {200, 201} or len(data) > 64 * 1024 or not reply.get("content-type", "").startswith("application/json"):
        raise InterfaceError(502, "rumor_unavailable", "校园传闻服务暂未完成请求。")
    return Response(data, status_code=status, media_type="application/json", headers={"Cache-Control": "no-store"})


def _admin_path(tail, method):
    path = "/" + tail
    if len(path) > 2048 or _UNSAFE_PATH.search(path) or any(part in {"..", "."} for part in path.split("/")) or "//" in path:
        raise InterfaceError(404, "rumor_not_found", "没有这个管理入口。")
    if path in ADMIN_NAVIGATION or path == "/xhs-shot" or path.startswith(("/static/", "/evidence/", *_NAVIGATION_DIRS)):
        if method == "GET":
            return path
    elif path.startswith("/api/") and path not in {"/api/assess"}:
        return path
    raise InterfaceError(404, "rumor_not_found", "没有这个管理入口。")


def _rewrite(body, path, media):
    if media == "text/html":
        body = _PAGE_LINK.sub(lambda value: value[1] + ADMIN_PREFIX.encode() + value[2], body)
        body = _SCRIPT_LINK.sub(lambda value: value[1] + ADMIN_PREFIX.encode() + value[2], body)
        return body.replace(
            '<a class="svc" href="/admin/rumor/login">切换账号</a>'.encode("utf-8"),
            '<a class="svc" href="/admin">返回校园助手管理台</a>'.encode("utf-8"),
        )
    if path == "/static/site.js":
        text = body.decode("utf-8").removeprefix('"use strict";').lstrip("\r\n")
        text = text.replace("fetch(path, {", "fetch(rumorPath(path), {")
        text = text.replace('return "/posts/" + encodeURIComponent(id);', 'return rumorPath("/posts/" + encodeURIComponent(id));')
        text = text.replace('link.href = "/reports/" + encodeURIComponent(name);',
                            'link.href = rumorPath("/reports/" + encodeURIComponent(name));')
        text = text.replace('source.href = evidence.attachment_url || evidence.url;',
                            'source.href = rumorPath(evidence.attachment_url || evidence.url);')
        return (_JS_PREFIX + text).encode("utf-8")
    if media in {"text/javascript", "application/javascript"}:
        return _SCRIPT_LINK.sub(lambda value: value[1] + ADMIN_PREFIX.encode() + value[2], body)
    return body


def _secure_admin_media(path, media):
    if media == "text/html":
        return path in ADMIN_NAVIGATION or path.startswith(_NAVIGATION_DIRS)
    if media in {"text/javascript", "application/javascript", "text/css"}:
        return path.startswith("/static/")
    return media in {"application/json", "image/png", "image/jpeg", "image/webp", "application/octet-stream", "text/plain", "text/markdown"}


async def admin_request(request, client, settings, user_id):
    path = _admin_path(request.path_params.get("tail", ""), request.method)
    nonce = secrets.token_urlsafe(18)
    headers = _headers(settings, "admin", **{
        "X-Campus-Rumor-Actor": "campus-desk:" + user_id,
        "X-Campus-Rumor-Nonce": nonce,
        "X-Campus-Request": "1",
    })
    query = request.scope["query_string"]
    if len(query) > 4096:
        raise InterfaceError(422, "invalid_query", "管理查询参数过长。")
    uploads = path in {"/api/upload/evidence", "/api/radar/upload", "/api/tips"}
    limit = 0 if request.method == "GET" else (17 * 1024 * 1024 if uploads else 64 * 1024)
    body = await read_body(request, limit)
    if body:
        media = request.headers.get("content-type", "")
        kinds = {"application/json", "multipart/form-data"} if uploads else {"application/json"}
        if media.partition(";")[0] not in kinds:
            raise InterfaceError(415, "unsupported_media_type", "请求内容类型不受支持。")
        headers["Content-Type"] = media
    status, upstream, result = await _request(client, settings, request.method, path, headers, body=body, query=query)
    content_type = upstream.get("content-type", "application/json")
    media = content_type.partition(";")[0].lower()
    response_headers = {"Content-Type": content_type, "Cache-Control": "no-store",
                        "Content-Security-Policy": _CSP.format(nonce=nonce), "X-Frame-Options": "DENY"}
    if status in {301, 302, 303, 307, 308}:
        location = upstream.get("location", "")
        url = urlsplit(location)
        if url.scheme or url.netloc or not location.startswith("/") or location.startswith("//"):
            raise InterfaceError(502, "rumor_bad_redirect", "管理服务返回了无效的跳转。")
        _admin_path(unquote(url.path[1:]), "GET")
        response_headers["Location"] = ADMIN_PREFIX + location
    elif not _secure_admin_media(path, media):
        raise InterfaceError(502, "rumor_bad_response", "管理服务返回了无法显示的内容。")
    if attachment := upstream.get("content-disposition"):
        response_headers["Content-Disposition"] = attachment
    return Response(_rewrite(result, path, media), status, headers=response_headers)
