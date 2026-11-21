"""Browser-only management API for supervised websites."""

import base64
import hmac
import re
import sqlite3
import sys
from dataclasses import dataclass
from functools import partial
from ipaddress import ip_address
from urllib.parse import urlsplit

import anyio
import httpx2
from starlette.responses import JSONResponse, Response

from minta_shortcuts import read_shortcut
from protocol import InterfaceError, parse_json, read_body
from request_auth import RequestAuthorizationUnavailable, runtime_request
from rumor import admin_request as rumor_admin_request
from settings import SERVER_DIR

if str(SERVER_DIR.parent) not in sys.path:
    sys.path.append(str(SERVER_DIR.parent))
from MainProcessManager.online_admin_store import (
    IDLE_SECONDS, SESSION_SECONDS, OnlineAdminStore, admin_user_id, csrf_token,
)
from server.CASserver.store import CASStore, STATE_SUBDIR
from server.CampusDigest.weread_admin import MAX_QR_BASE64, SCAN_PHASES, SESSION_STATES


LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
CREDENTIAL_KEY = SERVER_DIR.parent / "DataBase" / "TeacherCredentials" / "credential.key"
JSON_HEADERS = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class Browser:
    origin: str
    address: str
    secure: bool
    cookie: str
    token: str
    site_session: str


def _equal(configured, supplied):
    return hmac.compare_digest(configured.encode("utf-8"), supplied.encode("utf-8"))


def _response(data, status=200, headers=None):
    return JSONResponse(data, status, headers={**JSON_HEADERS, **(headers or {})})


async def _object(request):
    raw = await read_body(request, 16_384)
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise InterfaceError(415, "unsupported_media_type", "请使用 application/json")
    try:
        value = parse_json(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, RecursionError):
        pass
    raise InterfaceError(422, "invalid_request", "请提交有效的 JSON 对象")


class OnlineAdmin:
    def __init__(self, settings):
        self.settings = settings
        self.bridge = settings.web_admin_bridge
        self.store = None
        if settings.web_admin_state:
            try:
                self.store = OnlineAdminStore(settings.web_admin_state)
            except (OSError, sqlite3.Error):
                pass

    def _browser(self, request):
        if not self.store:
            raise InterfaceError(503, "admin_unavailable", "请通过主管理器启动网站和统一接口")
        bridges = request.headers.getlist("x-campus-admin-bridge")
        if len(bridges) != 1 or not _equal(self.bridge, bridges[0]):
            raise InterfaceError(403, "admin_bridge_denied", "请通过网站管理入口访问")
        site_sessions = request.headers.getlist("x-campus-admin-site-session")
        if len(site_sessions) != 1 or not re.fullmatch(r"[a-f0-9]{64}", site_sessions[0]):
            raise InterfaceError(403, "admin_bridge_denied", "请通过网站管理入口访问")
        origin = request.headers.get("x-campus-admin-origin", "")
        try:
            site = urlsplit(origin)
            peer = ip_address(request.headers.get("x-campus-admin-ip", ""))
            secure = site.scheme == "https"
            if (not site.hostname or site.username is not None or site.path or site.query or site.fragment
                or not (secure or site.scheme == "http" and site.hostname in LOCAL_HOSTS and peer.is_loopback)):
                raise ValueError
        except ValueError:
            raise InterfaceError(403, "invalid_admin_origin", "在线管理入口需要 HTTPS，本机允许回环地址") from None
        if request.method not in {"GET", "HEAD"} and request.headers.getlist("origin") != [origin]:
            raise InterfaceError(403, "admin_origin_denied", "请求来源与管理页面不一致")
        cookie = "__Host-campus-admin" if secure else "campus-admin-dev"
        return Browser(origin, str(peer), secure, cookie, request.cookies.get(cookie, ""), site_sessions[0])

    @staticmethod
    def _session(user_id, email, token):
        return {"name": email, "user_id": user_id, "csrf": csrf_token(token), "idle_seconds": IDLE_SECONDS}

    @staticmethod
    def _cookie(response, browser, value, age):
        response.set_cookie(browser.cookie, value, max_age=age, secure=browser.secure, httponly=True, samesite="strict")

    async def _identity(self, request, browser, client):
        credentials = request.headers.getlist("authorization")
        expired = InterfaceError(401, "admin_site_session_invalid", "邮箱登录已失效，请重新登录")
        if len(credentials) != 1 or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{1,256}", credentials[0]):
            raise expired
        base = httpx2.URL(self.settings.main_url)
        url = base.copy_with(path=base.path.rstrip("/") + "/v1/me")
        try:
            reply = await client.get(url, headers={
                "Authorization": credentials[0], "X-Forwarded-For": browser.address,
                "X-Forwarded-Proto": "https" if browser.secure else "http",
            }, timeout=8)
            if reply.status_code in {401, 403}:
                raise expired
            if reply.status_code != 200:
                raise ValueError
            data = reply.json()
            user_id, email = admin_user_id(data["id"]), data["email"]
            if not isinstance(email, str) or "@" not in email or len(email) > 320:
                raise ValueError
        except (httpx2.HTTPError, ValueError, TypeError, KeyError):
            raise InterfaceError(502, "admin_identity_unavailable", "暂时无法核验邮箱账号") from None
        if not data.get("verified_at"):
            raise InterfaceError(403, "admin_email_unverified", "请先完成该邮箱的验证")
        if data.get("campus_status") != "ready":
            raise InterfaceError(403, "admin_campus_required", "请先在校园助手完成统一身份绑定和个人资料登记")
        return user_id, email

    async def _login(self, request, browser, client):
        await read_body(request, 0)
        user_id, email = await self._identity(request, browser, client)
        token = await anyio.to_thread.run_sync(
            self.store.issue, user_id, browser.origin, browser.site_session, browser.token,
        )
        if not token:
            raise InterfaceError(403, "admin_permission_required", "该邮箱对应的用户尚未获授管理员权限")
        response = _response(self._session(user_id, email, token))
        self._cookie(response, browser, token, SESSION_SECONDS)
        return response

    async def handle(self, request, interface, dispatch, workers, client):
        try:
            return await self._handle(request, interface, dispatch, workers, client)
        except (sqlite3.Error, OSError):
            raise InterfaceError(503, "admin_state_unavailable", "管理员状态库暂不可用") from None

    async def _handle(self, request, interface, dispatch, workers, client):
        browser = self._browser(request)
        if interface.service == "web_auth" and interface.target == "login":
            return await self._login(request, browser, client)
        rumor_path = request.path_params.get("tail", "")
        passive = request.method == "GET" and (interface.service == "web_weread" or (
            interface.service == "web_rumor" and (rumor_path.startswith("static/") or rumor_path == "api/collection_status")
        ))
        user_id = await anyio.to_thread.run_sync(partial(
            self.store.authenticate, browser.token, browser.origin, browser.site_session, touch=not passive,
        ))
        if not user_id:
            raise InterfaceError(401, "admin_login_required", "请先登录在线管理台")
        session_read = interface.service == "web_auth" and interface.target == "session"
        rumor_write = interface.service == "web_rumor" and request.method != "GET"
        if rumor_write and request.headers.getlist("x-campus-request") != ["1"]:
            raise InterfaceError(403, "admin_csrf_denied", "管理请求校验失败，请刷新页面。")
        if not session_read and interface.service != "web_rumor":
            supplied = request.headers.getlist("x-campus-admin-csrf")
            if len(supplied) != 1 or not _equal(csrf_token(browser.token), supplied[0]):
                raise InterfaceError(403, "admin_csrf_denied", "管理会话校验失败，请重新载入管理页面")
        current_id, email = await self._identity(request, browser, client)
        if current_id != user_id:
            raise InterfaceError(401, "admin_login_required", "邮箱账号已切换，请重新登录在线管理台")
        if passive and interface.service == "web_weread":
            return await self._weread(request, interface.target, client)
        if interface.service == "web_rumor" and request.method == "GET":
            return await rumor_admin_request(request, client, self.settings, user_id)
        if interface.service == "web_auth":
            if interface.target == "session":
                return _response(self._session(user_id, email, browser.token))
            if interface.target == "audit":
                return _response({"items": await anyio.to_thread.run_sync(self.store.audit)})
            await read_body(request, 0)
            await anyio.to_thread.run_sync(self.store.logout, browser.token, browser.origin, browser.site_session)
            response = Response(status_code=204, headers=JSON_HEADERS)
            self._cookie(response, browser, "", 0)
            return response

        target = request.url.path
        parent = request.query_params.get("parent", "")
        if re.fullmatch(r"[A-Fa-f0-9-]{36}", parent):
            target += "/" + parent
        event = await anyio.to_thread.run_sync(self.store.event, user_id, request.method, target, "pending")
        try:
            if interface.service == "web_data":
                response = await self._data(request, interface.target, user_id, workers)
            elif interface.service == "web_campus_profiles":
                response = await self._profiles(request, interface.target, user_id, workers)
            elif interface.service == "web_campus_controls":
                response = await self._campus_controls(request, interface.target, user_id, workers)
            elif interface.service == "web_campus_photos":
                response = await self._campus_photos(request, interface.target, user_id, workers)
            elif interface.service == "web_credentials":
                response = await self._credential(request, user_id, client)
            elif interface.service == "web_runtime":
                response = await self._runtime(request, interface.target, user_id)
            elif interface.service == "web_inventory":
                response = await self._inventory(request, interface.target, client)
            elif interface.service == "web_weread":
                response = await self._weread(request, interface.target, client)
            elif interface.service == "web_rumor":
                response = await rumor_admin_request(request, client, self.settings, user_id)
            else:
                response = await dispatch()
        except InterfaceError as exc:
            await anyio.to_thread.run_sync(self.store.finish_event, event, f"http_{exc.status}")
            raise
        if response.status_code == 202:
            outcome = "accepted"
        else:
            outcome = "completed" if response.status_code < 300 else f"http_{response.status_code}"
        await anyio.to_thread.run_sync(self.store.finish_event, event, outcome)
        return response

    async def _weread(self, request, action, client):
        if request.query_params:
            raise InterfaceError(422, "invalid_query", "此入口无需查询参数")
        await read_body(request, 0)
        key = self.settings.runtime_status_key
        if not key:
            raise InterfaceError(503, "weread_unavailable", "请通过主管理器启动校园日报。")
        url = f"http://127.0.0.1:{self.settings.campus_digest_port}/internal/weread/{action}"
        raw = bytearray()
        try:
            async with client.stream(request.method, url, headers={"X-Campus-Runtime-Key": key}, timeout=25) as reply:
                if reply.status_code not in {200, 202, 503}:
                    raise ValueError
                async for chunk in reply.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_QR_BASE64 + 1024:
                        raise ValueError
                payload = parse_json(raw)
            if reply.status_code == 503:
                if isinstance(payload, dict) and payload.get("error") == "digest_package_missing":
                    raise InterfaceError(503, "digest_package_missing", "校园日报采集包缺失，公众号总成暂时使用缓存。")
                raise ValueError
            state, phase = payload["session"], payload["phase"]
            if state not in SESSION_STATES or phase not in SCAN_PHASES:
                raise ValueError
            result = {"session": state, "phase": phase}
            encoded = payload.get("qr_png_base64")
            if action == "login" and phase == "waiting_scan" and isinstance(encoded, str) and len(encoded) <= MAX_QR_BASE64:
                if base64.b64decode(encoded, validate=True).startswith(b"\x89PNG\r\n\x1a\n"):
                    result["qr_png_base64"] = encoded
        except (httpx2.HTTPError, ValueError, KeyError, TypeError, RecursionError):
            raise InterfaceError(503, "weread_unavailable", "微信读书暂不可用，请检查校园日报进程。") from None
        return _response(result, reply.status_code)

    async def _data(self, request, action, user_id, workers):
        arguments = {"actor": "web:" + user_id, "resource": request.path_params.get("resource", ""),
                     "id": request.path_params.get("record_id")}
        if action in {"minta_close", "minta_reconcile"}:
            arguments["resource"] = "minta"
        query = request.query_params.multi_items()
        allowed = {"q", "parent", "offset"} if action == "list" else {"parent"} if action == "create" else set()
        options = dict(query)
        if len(query) != len(options) or options.keys() - allowed:
            raise InterfaceError(422, "invalid_query", "查询参数无效")
        offset = options.get("offset", "0")
        if not re.fullmatch(r"[0-9]{1,6}", offset) or int(offset) > 100_000 or len(options.get("q", "")) > 160:
            raise InterfaceError(422, "invalid_query", "搜索或分页参数无效")
        arguments.update(options, offset=int(offset))
        if action in {"update", "role", "create"}:
            arguments["payload"] = await _object(request)
        else:
            await read_body(request, 0)
            arguments["payload"] = {}
        status, data = await workers.call("postgres_worker", action, arguments)
        return _response(data, status)

    async def _profiles(self, request, action, user_id, workers):
        arguments = {"actor": "web:" + user_id, "id": request.path_params.get("record_id")}
        fields = request.query_params.multi_items()
        options = dict(fields)
        allowed = {"q", "offset"} if action == "campus_profiles" else set()
        offset = options.get("offset", "0")
        if len(fields) != len(options) or options.keys() - allowed or len(options.get("q", "")) > 160 or not re.fullmatch(r"[0-9]{1,5}", offset):
            raise InterfaceError(422, "invalid_query", "查询参数无效")
        arguments.update(options, offset=int(offset))
        if action in {"campus_profile_review", "campus_profiles_bulk_review"}:
            arguments["payload"] = await _object(request)
        else:
            await read_body(request, 0)
        directory = self.settings.web_admin_state.parent / STATE_SUBDIR
        if action == "campus_profiles_bulk_review":
            arguments["cas_directory"] = str(directory)
        status, data = await workers.call("postgres_worker", action, arguments)
        if action == "campus_profiles" and status == 200:
            local = "urn:campus-desk:cas:local"
            subjects = [row["subject"] for row in data["items"] if row["issuer"] == local]
            if subjects:
                accounts = await anyio.to_thread.run_sync(lambda: CASStore(directory).identity_accounts(subjects))
            else:
                accounts = {}
            for row in data["items"]:
                issuer, subject = row.pop("issuer"), row.pop("subject")
                account = accounts.get(subject) if issuer == local else None
                row["username"] = account["username"] if account else None
                if issuer == local:
                    verified_number = row["username"] if account and account["is_campus_number"] else None
                    row["verified_campus_number"] = verified_number
                    row["campus_number_current"] = bool(verified_number and (row["campus_number"] or "").lower() == verified_number)
                row["batch_reviewable"] = bool(
                    account and row["protocol"] == "cas" and account["enabled"]
                    and row["campus_number_current"] and row["is_active"]
                    and row["email_verified"] and row["revision"] and not row["reviewed_at"]
                )
        return _response(data, status)

    async def _campus_controls(self, request, action, user_id, workers):
        fields = request.query_params.multi_items()
        offset = fields[0][1] if len(fields) == 1 and fields[0][0] == "offset" else "0"
        if fields and (action not in {"club_reviews", "club_archives"} or fields != [("offset", offset)]
                       or not re.fullmatch(r"[0-9]{1,5}", offset)):
            raise InterfaceError(422, "invalid_query", "分页参数无效")
        record = request.path_params.get("record_id")
        arguments = {"actor": "web:" + user_id, "id": record, "offset": int(offset),
                     "service": request.path_params.get("service", "classroom")}
        if request.method in {"PUT", "PATCH"}:
            arguments["payload"] = await _object(request)
        else:
            await read_body(request, 0)
        status, data = await workers.call("postgres_worker", action, arguments)
        return _response(data, status)

    async def _campus_photos(self, request, action, user_id, workers):
        fields = request.query_params.multi_items()
        arguments = {"actor": "web:" + user_id, "id": request.path_params.get("record_id")}
        query = dict(fields)
        if action in {"photo_review_places", "photo_reviews"}:
            status, offset = query.get("status", "pending"), query.get("offset", "0")
            allowed = {"status", "offset", "place"} if action == "photo_reviews" else {"status", "offset"}
            place = query.get("place")
            if (len(query) != len(fields) or query.keys() - allowed
                    or status not in {"all", "pending", "approved", "rejected"}
                    or not re.fullmatch(r"[0-9]{1,6}", offset)
                    or (place is not None and not re.fullmatch(r"[0-9]{7}_[0-9]{6}", place))):
                raise InterfaceError(422, "invalid_query", "照片审核查询参数无效")
            arguments.update(status=status, offset=int(offset))
            if place is not None:
                arguments["place"] = place
        elif action == "photo_delete":
            version = query.get("version", "")
            if len(fields) != 1 or not re.fullmatch(r"[0-9]{1,10}", version) or not 1 <= int(version) <= 2_147_483_647:
                raise InterfaceError(422, "invalid_query", "永久删除照片需要有效的照片版本")
            arguments["version"] = int(version)
        elif fields:
            raise InterfaceError(422, "invalid_query", "此入口无需查询参数")
        if action == "photo_review":
            arguments["payload"] = await _object(request)
        else:
            await read_body(request, 0)
        status, data = await workers.call("postgres_worker", action, arguments)
        if action == "photo_preview" and status == 200:
            try:
                image = base64.b64decode(data["image_base64"], validate=True)
            except (KeyError, ValueError, TypeError):
                raise InterfaceError(502, "photo_preview_invalid", "照片预览不可用") from None
            if not image.startswith(b"\xff\xd8") or len(image) > 2 * 1024 * 1024:
                raise InterfaceError(502, "photo_preview_invalid", "照片预览不可用")
            return Response(image, media_type="image/jpeg", headers=JSON_HEADERS)
        return _response(data, status)

    async def _runtime(self, request, target, user_id):
        command = {"operation": "status"}
        if target == "process_action":
            data = await _object(request)
            if data.keys() != {"action"} or data["action"] not in ("start", "stop", "restart"):
                raise InterfaceError(422, "invalid_process_action", "进程操作须为启动、停止或重启")
            command = {"operation": "submit", "service": request.path_params["service"],
                       "actor": user_id, "action": data["action"]}
        try:
            result = await anyio.to_thread.run_sync(runtime_request, *self.settings.mpm_auth, command)
        except RequestAuthorizationUnavailable:
            raise InterfaceError(503, "supervisor_unavailable", "本机主管理器暂不可用") from None
        if not result.pop("allowed"):
            raise InterfaceError(403, "process_action_denied", "主管理器未授权该操作")
        if result.get("busy"):
            raise InterfaceError(409, "process_busy", "已有进程操作在执行")
        return _response(result, 202 if target == "process_action" else 200)

    async def _inventory(self, request, target, client):
        if target == "minta-health":
            if request.query_params or not self.settings.minta_url:
                raise InterfaceError(503, "minta_unconfigured", "MINTA 校务服务尚未配置")
            base = httpx2.URL(self.settings.minta_url)
            url = base.copy_with(path=base.path.rstrip("/") + "/readyz")
            try:
                reply = await client.get(url, timeout=8)
                if reply.status_code not in {200, 503} or len(reply.content) > 65_536:
                    raise ValueError
                result = reply.json()
                items = [{"name": name, "connected": value.get("connected") is True}
                         for name, value in result["upstreams"].items()]
                return _response({"status": result["status"], "items": items})
            except (httpx2.HTTPError, ValueError, TypeError, KeyError, AttributeError):
                raise InterfaceError(502, "minta_unavailable", "暂时无法读取 MINTA 的后端连接状态") from None
        if target == "campus":
            service = request.path_params["service"]
            if service == "schedule":
                if not self.settings.minta_url:
                    raise InterfaceError(503, "minta_unconfigured", "MINTA 校务服务尚未配置")
                base = httpx2.URL(self.settings.minta_url)
                url = base.copy_with(path=base.path.rstrip("/") + "/v1/call")
                return _response(await read_shortcut(client, url, "schedule", request.query_params))
            if service not in {"gym", "club", "classroom", "library", "map"}:
                raise InterfaceError(404, "unknown_campus_service", "校园服务不存在")
            path = "/internal/status/campus/" + service
        else:
            path = "/internal/status/" + target
        if request.query_params:
            raise InterfaceError(422, "invalid_query", "此入口无需查询参数")
        base = httpx2.URL(self.settings.mcp_url if target == "tools" else self.settings.main_url)
        key = self.settings.runtime_status_key
        if not key or base.host not in LOCAL_HOSTS:
            raise InterfaceError(503, "inventory_unconfigured", "运行时清单需要受管的本机服务")
        url = base.copy_with(path=path)
        raw = bytearray()
        try:
            async with client.stream("GET", url, headers={"X-Campus-Runtime-Key": key}, timeout=8) as reply:
                if reply.status_code == 404:
                    raise InterfaceError(503, "inventory_unconfigured", "请通过主管理器重新启动对应服务")
                async for chunk in reply.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError
                result = parse_json(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return _response(result, reply.status_code)
        except (httpx2.HTTPError, ValueError):
            raise InterfaceError(502, "inventory_unavailable", "暂时无法读取该服务的状态") from None

    async def _credential(self, request, user_id, client):
        action = request.path_params["action"]
        if action not in {"inspect", "issue", "update", "revoke"}:
            raise InterfaceError(404, "credential_route_unknown", "凭据管理操作不存在")
        data = await _object(request)
        if data.keys() - {"email", "contact", "days"}:
            raise InterfaceError(422, "invalid_request", "凭据字段无效")

        def read_key():
            try:
                with CREDENTIAL_KEY.open("rb") as handle:
                    return handle.read(33)
            except OSError:
                return b""

        key = await anyio.to_thread.run_sync(read_key)
        if len(key) != 32:
            raise InterfaceError(503, "credential_service_unavailable", "请先启动教师凭据服务")
        operator = hmac.digest(key, b'["operator"]', "sha256").hex()
        try:
            response = await client.post(
                f"http://127.0.0.1:8192/manage/{action}", json={**data, "actor": "web:" + user_id},
                headers={"Authorization": f"Bearer {operator}"}, timeout=10,
            )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
        except (httpx2.HTTPError, ValueError):
            raise InterfaceError(502, "credential_service_unavailable", "教师凭据服务暂不可用") from None
        return _response(result, response.status_code)
