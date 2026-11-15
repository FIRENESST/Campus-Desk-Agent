import asyncio
import importlib
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime
from enum import Enum
from functools import partial
from hmac import compare_digest
from ipaddress import ip_address
from uuid import UUID
from urllib.parse import parse_qsl, urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from ..campus_cluster import AgentId, RoutingFeedback
from ..campus_cluster.capabilities import default_capability_catalog, publish_default_capabilities
from ..campus_cluster.library_catalog import LibraryCatalogQuery
from ..campus_cluster.routing_similarity import embed_locally
from ..campus_cluster.subskills.campus_street_view import (
    CAMPUS_VIEW_PATH, StreetViewInput, StreetViewUnavailable, load_street_view, resolve_street_view, street_view_media,
)
from ..campus_cluster.subskills.campus_timetable import CampusTimetableInput
from ..campus_cluster.subskills.minta_campus import GymSlotsQuery
from ..campus_cluster.subskills.walking_routes import walking_places
from ..llm_server import connect_llm_server
from ..teacher_credentials import TeacherCredentialClient
from .auth import AuthService
from .calendar_sync import CalendarSubscriptions, SubscriptionInput, calendar_document, personal_calendar
from .campus_access import CampusAccess
from .campus_photos import MAX_PHOTO_UPLOAD, CampusPhotos, check_place, page_offset
from .campus_profiles import GRADES, CampusProfiles, ProfileInput
from .campus_timetable import CampusTimetable
from .config import Settings
from .database import Database
from .errors import ServiceError, not_found, unauthorized
from .mail import DisabledMailer, SMTPMailer
from .microsoft_graph import MicrosoftGraph
from .microsoft_todo import AuthorizationFinish, MicrosoftTodo
from .personal_schedule import EventUpdate, NewEvent, PersonalEvents, calendar_window, classroom_event
from .wechat_following import WeChatFollowInput, change_wechat_follow, following_feed
from .minta_clubs import ClubCommand, PurgeArchive, PurgeArchives, change_club, change_club_archive, list_clubs, purge_club_archives
from .minta_chat import CampusChat
from .minta_operations import CampusOperations, OperationInput, campus_catalog
from .runner import CampusRunner
from .security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from .semester_report import semester_report
from .store import PostgresStore
from .turn_withdrawal import TurnWithdrawal
from .worker import WorkerPool
from .sso import SSOService, create_provider


def encode(value):
    if isinstance(value, (UUID, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError("无法序列化响应")


def _runtime_access(request):
    key = request.app.state.config.runtime_status_key
    headers = request.headers.getlist("x-campus-runtime-key")
    try:
        local = bool(request.client and ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    return bool(local and key and len(headers) == 1 and compare_digest(key.encode(), headers[0].encode()))


class JSON(JSONResponse):
    def render(self, content):
        return json.dumps(content, default=encode, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class Boundary(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            if not request.app.state.config.development and request.url.scheme != "https" and request.url.path != "/health":
                if not request.url.path.startswith("/internal/status/") or not _runtime_access(request):
                    raise ServiceError(426, "https_required", "请使用 HTTPS")
            response = await call_next(request)
        except ServiceError as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
            if exc.retry_after is not None:
                headers["Retry-After"] = str(exc.retry_after)
            response = JSON({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                            headers=headers)
        except Exception as exc:
            logging.getLogger(__name__).error("API operation failed (%s)", type(exc).__name__)
            response = JSON({"error": {"code": "internal_error", "message": "服务暂不可用"}}, status_code=500)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
        if not request.app.state.config.development:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmailInput(Input):
    email: str = Field(min_length=3, max_length=320)


class Registration(EmailInput):
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    display_name: str | None = Field(default=None, max_length=50)


class Login(EmailInput):
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = False


class Refresh(Input):
    refresh_token: str = Field(min_length=1, max_length=256)


class Redemption(Input):
    token: str = Field(min_length=1, max_length=256)


class PasswordReset(Redemption):
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class PasswordChangeRequest(Input):
    old_password: str = Field(min_length=1, max_length=128)


class PasswordChange(PasswordChangeRequest):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class InstanceCreate(Input):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("名称不能为空")
        return value.strip()


class InstanceUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    status: str | None = None
    is_default: bool | None = None

    @field_validator("name", "status")
    @classmethod
    def valid(cls, value, info):
        if value is None or not value.strip() or "\x00" in value or (
            info.field_name == "status" and value not in {"active", "paused"}
        ):
            raise ValueError("实例更新字段无效")
        return value.strip()

    @field_validator("is_default")
    @classmethod
    def assign_default(cls, value):
        if value is not True:
            raise ValueError("设为默认时 is_default 必须为 true")
        return value


class ConversationCreate(Input):
    title: str = Field(default="新会话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("标题不能为空")
        return value.strip()


class Turn(Input):
    query: str = Field(min_length=1, max_length=12000)
    targets: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value

    @field_validator("targets")
    @classmethod
    def known_targets(cls, value):
        if len(value) != len(set(value)) or any(item not in {agent.value for agent in AgentId} for item in value):
            raise ValueError("目标智能体无效或重复")
        return value


class RetryTurn(Input):
    attempt: int = Field(ge=1, le=2_147_483_647)


class ConfirmWithdrawal(RetryTurn):
    approvals: list[str] = Field(max_length=32)


class SSOStart(Input):
    mode: str = "login"
    code_challenge: str = Field(min_length=43, max_length=43)
    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    website_origin: str | None = Field(default=None, min_length=1, max_length=255)


class SSOFinish(Input):
    completion_code: str = Field(min_length=1, max_length=256)
    code_verifier: str = Field(min_length=43, max_length=128)


class SSOUnlink(Input):
    current_password: str = Field(min_length=1, max_length=128)


async def request_bytes(request, maximum):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise ServiceError(413, "body_too_large", "请求内容过大")
        chunks.append(chunk)
    return b"".join(chunks)


async def body(request, model):
    raw = await request_bytes(request, 65536)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("重复字段")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非法数字")))
        return model.model_validate(value)
    except (ValueError, UnicodeError, ValidationError):
        # ValidationError includes input values by default; do not return it.
        raise ServiceError(422, "invalid_request", "请求字段、格式或长度无效") from None


def uuid_arg(value):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(422, "invalid_id", "资源 ID 无效") from None


def idempotency_key(request):
    key = request.headers.get("idempotency-key", "")
    if not 1 <= len(key) <= 128 or not key.isascii() or any(char.isspace() for char in key):
        raise ServiceError(422, "idempotency_key_required", "需要 1 到 128 位非空白 ASCII Idempotency-Key")
    return key


def page_limit(request):
    try:
        value = int(request.query_params.get("limit", "50"))
        if not 1 <= value <= 100:
            raise ValueError
        return value
    except ValueError:
        raise ServiceError(422, "invalid_limit", "limit 必须在 1 到 100 之间") from None


def client_ip(request):
    # Proxy headers are handled only by Uvicorn's explicitly trusted proxy list.
    return request.client.host if request.client else "unknown"


async def principal(request, *, onboarding=False):
    scheme, _, secret = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secret or len(secret) > 256:
        raise unauthorized()
    who = await request.app.state.auth.authenticate(secret)
    if not onboarding:
        await request.app.state.profiles.require_ready(who)
    return who


ACCEPTED = {"message": "如果该邮箱符合条件，你将收到后续操作邮件。"}


def create_app(settings=None, *, database=None, runner=None, mailer=None, sso_provider=None,
               minta_gateway=None, teacher_credentials=None, todo_transport=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        db = database or Database(config.database_url, config.pool_size)
        delivery = mailer
        if delivery is None:
            if config.development and SMTPMailer.missing_configuration(config):
                delivery = DisabledMailer()
            else:
                delivery = SMTPMailer(config)
        engine = runner
        if engine is None and config.runner_factory:
            module, separator, name = config.runner_factory.partition(":")
            if not separator:
                raise ValueError("CLOUD_RUNNER_FACTORY 必须为 module:factory")
            engine = getattr(importlib.import_module(module), name)(config)
        async with AsyncExitStack() as resources:
            await db.open()
            resources.push_async_callback(db.close)
            app.state.auth = AuthService(db, config, delivery)
            provider = sso_provider or create_provider(config)
            if provider:
                resources.push_async_callback(provider.aclose)
            app.state.sso = SSOService(db, app.state.auth, config, provider)
            app.state.profiles = CampusProfiles(db, provider, required=config.require_campus_profile,
                                                check_local_status=bool(config.cas_local and provider and provider.protocol == "cas"))
            app.state.campus_access = CampusAccess(db, app.state.profiles)
            app.state.campus_photos = CampusPhotos(db)
            app.state.calendar_subscriptions = CalendarSubscriptions(db, config.require_verified_email)
            credential_http = await resources.enter_async_context(httpx2.AsyncClient(trust_env=False, follow_redirects=False))
            app.state.campus_ops = CampusOperations(db, teacher_credentials or TeacherCredentialClient(credential_http),
                                                    minta_gateway, access=app.state.campus_access)
            app.state.turn_withdrawal = TurnWithdrawal(app.state.campus_ops)
            app.state.microsoft_todo = None
            if config.todo_client_id:
                todo_http = await resources.enter_async_context(httpx2.AsyncClient(
                    timeout=httpx2.Timeout(15.0, connect=5.0), follow_redirects=False, trust_env=False,
                    transport=todo_transport,
                ))
                ready = await db.run(lambda c: c.execute(
                    """SELECT to_regclass('public.microsoft_todo_authorizations') IS NOT NULL
                       AND to_regclass('public.microsoft_todo_connections') IS NOT NULL AS ready""",
                ).fetchone())
                if ready["ready"]:
                    todo = MicrosoftTodo(db, app.state.profiles, app.state.campus_ops, app.state.campus_access,
                                         config, MicrosoftGraph(todo_http, config))
                    app.state.microsoft_todo = todo
                    todo_worker = asyncio.create_task(todo.run(), name="microsoft-todo-sync")
                    resources.push_async_callback(todo.close, todo_worker)
                else:
                    logging.getLogger(__name__).warning("Microsoft To Do 需要先执行 PostgreSQL 迁移 016_microsoft_todo.sql")
            if engine is None:
                llm = await resources.enter_async_context(connect_llm_server())
                feedback = None
                if config.routing_data_dir:
                    port = config.routing_embedding_port
                    feedback = RoutingFeedback(config.routing_data_dir, embed=partial(embed_locally, port) if port else None)
                    await asyncio.to_thread(feedback.initialize)
                campus_chat = CampusChat(app.state.campus_ops, app.state.auth)
                registrations = await campus_chat.registered_actions()
                logging.getLogger(__name__).info("启动时发现 %d 项已登记的 MINTA 办理动作", len(registrations))
                engine = CampusRunner.with_model(llm, routing_feedback=feedback, campus_chat=campus_chat)
                await asyncio.to_thread(publish_default_capabilities)
            elif hasattr(engine, "aclose"):
                resources.push_async_callback(engine.aclose)
            await db.run(lambda c: c.execute("SELECT id, is_default FROM agent_instances LIMIT 0").fetchall())
            await db.run(lambda c: c.execute("SELECT session_id FROM account_tokens LIMIT 0").fetchall())
            await db.run(lambda c: c.execute("SELECT role, academic_title FROM users LIMIT 0").fetchall())
            await db.run(lambda c: c.execute("SELECT initiator_session_id FROM runs LIMIT 0").fetchall())
            turn_schema = await db.run(lambda c: c.execute(
                """SELECT to_regclass('chat_turn_effects') IS NOT NULL AS turns,
                to_regclass('campus_profiles') IS NOT NULL AND to_regclass('campus_access_rules') IS NOT NULL AS campus,
                (SELECT count(*)=2 FROM pg_attribute WHERE attrelid=to_regclass('campus_minta_operations')
                 AND attname IN ('loan_days','loan_borrowed_at') AND NOT attisdropped) AS library_loans""",
            ).fetchone())
            if not turn_schema["turns"]:
                raise RuntimeError("会话重来与撤回需要先执行 PostgreSQL 迁移 007_chat_turn_actions.sql")
            if not turn_schema["campus"]:
                raise RuntimeError("校园身份登记和服务权限需要先执行 PostgreSQL 迁移 009 和 010")
            if not turn_schema["library_loans"]:
                raise RuntimeError("图书借期需要先执行 PostgreSQL 迁移 012_library_loan_periods.sql")
            store = PostgresStore(db, config)
            app.state.config, app.state.db, app.state.store = config, db, store
            workers = WorkerPool(store, engine, config)
            resources.push_async_callback(workers.close)
            await workers.start()
            yield

    async def health(request):
        return JSON({"status": "ok"})

    async def ready(request):
        await request.app.state.db.run(lambda c: c.execute("SELECT 1").fetchone())
        return JSON({"status": "ready"})

    async def runtime_skills(request):
        if not _runtime_access(request):
            raise not_found()
        catalog, live = await asyncio.gather(asyncio.to_thread(default_capability_catalog),
                                             request.app.state.campus_ops.registered_actions())
        return JSON({**catalog, "minta_actions": sorted(live)})

    async def runtime_campus(request):
        if not _runtime_access(request):
            raise not_found()
        service = request.path_params["service"]
        if service == "map":
            _, catalog = await asyncio.to_thread(load_street_view)
            return JSON(catalog.summary())
        return JSON(await campus_catalog(request.app.state.campus_ops.gateway, service))

    async def auth_providers(request):
        items = [{"id": "password", "type": "password", "name": "邮箱密码"}]
        provider = request.app.state.sso.provider
        if provider:
            items.append({"id": "campus", "type": provider.protocol, "name": "校园统一认证"})
        return JSON({"providers": items, "supported_sso_protocols": ["oidc", "cas", "saml2"]})

    async def sso_start(request):
        data = await body(request, SSOStart)
        who = await principal(request, onboarding=True) if data.mode == "link" else None
        sso = request.app.state.sso
        result, browser = await sso.start(data.mode, data.code_challenge, client_ip(request), who, data.current_password,
                                          website_origin=data.website_origin)
        config = request.app.state.config
        published_cas = bool(result.get("local_cas") and data.website_origin in config.cas_public_sites)
        response = JSON(result)
        response.set_cookie(sso.cookie_name, browser, max_age=600, httponly=True, path="/",
                            secure=published_cas or urlsplit(config.public_url).scheme == "https",
                            samesite="none" if sso.provider.protocol == "saml2" else "lax")
        return response

    async def sso_callback(request):
        sso = request.app.state.sso
        provider = sso.require_provider()
        if provider.protocol == "saml2":
            if request.method != "POST" or request.query_params:
                raise ServiceError(400, "invalid_sso_binding", "SAML 回调需要 HTTP POST")
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 262144:
                    raise ServiceError(413, "body_too_large", "请求内容过大")
                chunks.append(chunk)
            try:
                pairs = parse_qsl(b"".join(chunks).decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=8)
            except (ValueError, UnicodeError):
                raise ServiceError(400, "invalid_sso_response", "校园认证响应无效") from None
            state_key = "RelayState"
        else:
            if request.method != "GET":
                raise ServiceError(400, "invalid_sso_binding", "此 SSO 协议需要 GET 回调")
            pairs, state_key = list(request.query_params.multi_items()), "state"
        parameters = dict(pairs)
        if len(pairs) != len(parameters) or len(pairs) > 8:
            raise ServiceError(400, "invalid_sso_response", "校园认证响应无效")
        result = await sso.callback(parameters.get(state_key, ""), request.cookies.get(sso.cookie_name, ""), parameters, client_ip(request))
        response = JSON(result)
        response.delete_cookie(sso.cookie_name, path="/", secure=urlsplit(request.app.state.config.public_url).scheme == "https",
                               httponly=True, samesite="none" if provider.protocol == "saml2" else "lax")
        return response

    async def sso_finish(request):
        data = await body(request, SSOFinish)
        return JSON(await request.app.state.sso.finish(data.completion_code, data.code_verifier, client_ip(request),
                                                     request.headers.get("user-agent", "")[:200]))

    async def identities(request):
        return JSON({"items": await request.app.state.sso.identities(await principal(request, onboarding=True))})

    async def unlink_identity(request):
        who = await principal(request)
        data = await body(request, SSOUnlink)
        await request.app.state.auth.rate_limit("sso.unlink.user", str(who.user_id), 10, 900)
        await request.app.state.sso.unlink(who, uuid_arg(request.path_params["identity_id"]), data.current_password)
        return Response(status_code=204)

    async def saml_metadata(request):
        provider = request.app.state.sso.require_provider()
        if provider.protocol != "saml2":
            raise ServiceError(404, "not_found", "资源不存在")
        return Response(provider.metadata(), media_type="application/samlmetadata+xml")

    async def register(request):
        data = await body(request, Registration)
        await request.app.state.auth.register(data.email, data.password, data.display_name, client_ip(request))
        return JSON(ACCEPTED, status_code=202)

    async def login(request):
        data = await body(request, Login)
        return JSON(await request.app.state.auth.login(
            data.email, data.password, client_ip(request), request.headers.get("user-agent", "")[:200],
            remember_me=data.remember_me,
        ))

    async def refresh(request):
        data = await body(request, Refresh)
        return JSON(await request.app.state.auth.refresh(data.refresh_token, client_ip(request)))

    async def remember(request):
        data = await body(request, Redemption)
        return JSON(await request.app.state.auth.remember(data.token, client_ip(request)))

    async def verification_request(request):
        data = await body(request, EmailInput)
        await request.app.state.auth.request_challenge(data.email, "verify", client_ip(request))
        return JSON(ACCEPTED, status_code=202)

    async def password_request(request):
        data = await body(request, EmailInput)
        await request.app.state.auth.request_challenge(data.email, "reset", client_ip(request))
        return JSON(ACCEPTED, status_code=202)

    async def verify(request):
        data = await body(request, Redemption)
        await request.app.state.auth.rate_limit("redeem.ip", client_ip(request), 30, 900)
        await request.app.state.auth.redeem_challenge(data.token, "verify")
        return Response(status_code=204)

    async def reset(request):
        data = await body(request, PasswordReset)
        await request.app.state.auth.rate_limit("redeem.ip", client_ip(request), 30, 900)
        await request.app.state.auth.redeem_challenge(data.token, "reset", data.new_password)
        return Response(status_code=204)

    async def me(request):
        return JSON(await request.app.state.profiles.account(await principal(request, onboarding=True)))

    async def profile(request):
        who = await principal(request, onboarding=True)
        profiles = request.app.state.profiles
        if request.method == "GET":
            return JSON(await profiles.profile(who))
        await request.app.state.auth.rate_limit("profile.update", str(who.user_id), 30, 900)
        return JSON(await profiles.save(who, await body(request, ProfileInput)))

    async def campus_operations(request):
        who = await principal(request)
        operations = request.app.state.campus_ops
        if request.method == "GET":
            return JSON(await operations.list(who, request.query_params.get("service")))
        key, data = idempotency_key(request), await body(request, OperationInput)
        auth = request.app.state.auth
        await auth.rate_limit("minta.operation", str(who.user_id), 30, 60)
        if data.teacher is not None:
            await auth.rate_limit("minta.teacher_contact.user", str(who.user_id), 8, 600)
            await auth.rate_limit("minta.teacher_contact.ip", client_ip(request), 50, 600)
        result = await operations.start(who, key, data)
        return JSON(result, status_code=202 if result["state"] in {"pending", "uncertain"} else 200)

    async def close_campus_operation(request):
        who = await principal(request)
        await request.app.state.auth.rate_limit("minta.operation", str(who.user_id), 30, 60)
        result = await request.app.state.campus_ops.close(who, uuid_arg(request.path_params["operation_id"]))
        return JSON(result, status_code=202 if result["state"] in {"closing", "close_uncertain"} else 200)

    async def minta_timetable(request):
        who = await principal(request)
        params = request.query_params
        try:
            if len(params.multi_items()) != len(params):
                raise ValueError
            query = CampusTimetableInput.model_validate_strings(dict(params))
        except ValueError:
            raise ServiceError(422, "invalid_timetable_query", "时段查询条件无效，请核对服务、日期和时间范围。") from None
        result = await CampusTimetable(request.app.state.campus_ops).read(who, query)
        return JSON(result, headers={"Cache-Control": "no-store"})

    async def wechat_following(request):
        who = await principal(request)
        state = request.app.state
        if request.method == "PUT":
            if request.query_params:
                raise ServiceError(422, "invalid_wechat_query", "修改关注需要提交公众号选择。")
            data = await change_wechat_follow(state.db, who.user_id, await body(request, WeChatFollowInput))
        else:
            data = await following_feed(state.db, who.user_id, request.query_params)
        return JSON(data, headers={"Cache-Control": "no-store"})

    async def minta_catalog(request):
        who = await principal(request, onboarding=True)
        service, params = request.path_params["service"], request.query_params
        query = None
        if service == "library" and params:
            values = dict(params)
            page = values.pop("page", "1")
            if len(params.multi_items()) != len(params) or len(page) > 5 or not (page.isascii() and page.isdecimal()):
                raise ServiceError(422, "invalid_query", "馆藏查询的参数或页码无效")
            try:
                query = LibraryCatalogQuery.model_validate({**values, "page": int(page)})
            except ValidationError:
                raise ServiceError(422, "invalid_query", "馆藏查询的参数或页码无效") from None
        elif service == "gym" and params:
            if len(params.multi_items()) != len(params):
                raise ServiceError(422, "invalid_query", "场馆或日期参数重复")
            try:
                day = date.fromisoformat(params["date"])
                if day.isoformat() != params["date"]:
                    raise ValueError
                query = GymSlotsQuery.model_validate(dict(params))
            except (KeyError, ValueError):
                raise ServiceError(422, "invalid_query", "请提供场馆编号与有效日期") from None
        elif service == "classroom" and params:
            try:
                if len(params.multi_items()) != 1:
                    raise ValueError
                query = date.fromisoformat(params["date"])
                if query.isoformat() != params["date"] or not date.min < query < date.max:
                    raise ValueError
            except (KeyError, ValueError):
                raise ServiceError(422, "invalid_query", "请提供一个有效的查询日期") from None
        return JSON(await request.app.state.campus_ops.catalog(who, service, query=query))

    async def minta_directory(request):
        if request.query_params:
            raise ServiceError(422, "invalid_query", "此查询无需额外参数")
        who = await principal(request, onboarding=True)
        classroom = request.url.path.endswith("/classrooms")
        result = await request.app.state.campus_ops.catalog(who, "classroom" if classroom else "club")
        if classroom:
            for item in result["items"]:
                item["room_id"] = item.pop("id")
        else:
            for item in result["items"]:
                if item.get("club_id") is None:
                    item.pop("club_id", None)
        return JSON(result)

    async def campus_service_access(request):
        who = await principal(request, onboarding=True)
        service = request.path_params.get("service")
        headers = {}
        if service == "schedule":
            number = await request.app.state.campus_access.schedule_number(who)
            headers["X-Campus-Schedule-Role"] = who.role.value
            if number:
                headers["X-Campus-Schedule-Number"] = number
        elif service:
            await request.app.state.campus_access.check(who, service)
        else:
            await request.app.state.profiles.require_ready(who)
        return Response(status_code=204, headers=headers)

    async def minta_semester_report(request):
        who = await principal(request)
        if request.query_params:
            raise ServiceError(422, "invalid_query", "学期报告无需额外参数")
        state = request.app.state
        return JSON(await semester_report(who, state.campus_ops, state.campus_access))

    async def calendar_events(request):
        who = await principal(request)
        state = request.app.state
        events = PersonalEvents(state.campus_ops.db)
        if request.method == "POST":
            await state.auth.rate_limit("calendar.event_write", str(who.user_id), 30, 60)
            return JSON(await events.create(who.user_id, await body(request, NewEvent)), status_code=201)
        window = calendar_window(request.query_params)
        custom, (classroom, verified) = await asyncio.gather(
            events.list(who.user_id, window), state.campus_ops.classroom_calendar(who, window),
        )
        return JSON({"items": custom + list(map(classroom_event, classroom)), "classroom_unavailable": not verified},
                    headers={"Cache-Control": "no-store"})

    async def change_calendar_event(request):
        who = await principal(request)
        event_id = uuid_arg(request.path_params["event_id"])
        state = request.app.state
        events = PersonalEvents(state.campus_ops.db)
        await state.auth.rate_limit("calendar.event_write", str(who.user_id), 30, 60)
        if request.method == "PUT":
            return JSON(await events.update(who.user_id, event_id, await body(request, EventUpdate)))
        value = request.query_params.get("revision", "")
        if (len(request.query_params.multi_items()) != 1 or not 1 <= len(value) <= 10
                or not value.isascii() or not value.isdecimal() or not 1 <= int(value) < 2_147_483_647):
            raise ServiceError(422, "invalid_revision", "请提供有效的日程版本")
        await events.delete(who.user_id, event_id, int(value))
        return JSON({"deleted": True})

    async def calendar_subscriptions(request):
        who = await principal(request, onboarding=request.method == "GET")
        state = request.app.state
        subscriptions = state.calendar_subscriptions
        if request.method == "POST":
            data = await body(request, SubscriptionInput)
            await state.auth.rate_limit("calendar.create", str(who.user_id), 10, 60)
            return JSON(await subscriptions.create(who.user_id, data.label), status_code=201)
        return JSON({"items": await subscriptions.list(who.user_id)})

    async def revoke_calendar_subscription(request):
        who = await principal(request, onboarding=True)
        await request.app.state.calendar_subscriptions.revoke(who.user_id, uuid_arg(request.path_params["subscription_id"]))
        return Response(status_code=204)

    async def calendar_export(request):
        return await schedule_calendar(request, await principal(request), download=True)

    async def calendar_feed(request):
        authorization = request.headers.getlist("authorization")
        scheme, _, secret = authorization[0].partition(" ") if len(authorization) == 1 else ("", "", "")
        if scheme.lower() != "bearer" or not secret or len(secret) > 256:
            raise not_found()
        state = request.app.state
        who = await state.calendar_subscriptions.authenticate(secret)
        try:
            await state.profiles.require_ready(who)
        except ServiceError as exc:
            if exc.code in {"invalid_credentials", "campus_link_required", "campus_profile_required"}:
                raise not_found() from None
            raise
        return await schedule_calendar(request, who)

    async def schedule_calendar(request, who, *, download=False):
        if request.query_params:
            raise ServiceError(422, "invalid_query", "日历接口无需额外参数")
        state = request.app.state
        events = await personal_calendar(who, state.campus_ops, state.campus_access)
        payload, etag = calendar_document(events)
        headers = {"ETag": etag, "X-Robots-Tag": "noindex, nofollow, noarchive"}
        if download:
            headers["Content-Disposition"] = 'attachment; filename="campus-schedule.ics"'
        tags = {tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match", "").split(",")}
        if "*" in tags or etag.removeprefix("W/") in tags:
            return Response(status_code=304, headers=headers)
        headers["Content-Length"] = str(len(payload))
        return Response(payload if request.method == "GET" else b"", media_type="text/calendar", headers=headers)

    async def microsoft_todo_status(request):
        who = await principal(request, onboarding=True)
        todo = request.app.state.microsoft_todo
        if not todo:
            return JSON({"configured": False, "connection": None})
        data = await todo.status(who.user_id)
        data["redirect_uri"] = todo.settings.todo_redirect_uri
        return JSON(data)

    async def microsoft_todo_command(request):
        state = request.app.state
        who = await principal(request, onboarding=request.method == "DELETE")
        todo = state.microsoft_todo
        if not todo:
            raise ServiceError(503, "todo_unconfigured", "Microsoft To Do 尚未完成服务配置")
        if request.method == "DELETE":
            await todo.disconnect(who)
            return Response(status_code=204)
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == "start":
            await state.auth.rate_limit("todo.start", str(who.user_id), 10, 60)
            return JSON(await todo.start(who))
        if operation == "finish":
            await state.auth.rate_limit("todo.finish", str(who.user_id), 10, 60)
            data = await body(request, AuthorizationFinish)
            await todo.finish(who, data.state, data.code)
            return Response(status_code=204)
        await state.auth.rate_limit("todo.sync", str(who.user_id), 10, 60)
        await todo.queue(who)
        return JSON({"queued": True}, status_code=202)

    async def teacher_credential(request):
        who = await principal(request)
        if who.role != "teacher":
            raise ServiceError(403, "teacher_account_required", "教师凭据只向本人开放")
        try:
            credential = await request.app.state.campus_ops.credential(who.user_id)
        except ServiceError as exc:
            if exc.code != "teacher_credential_unverified":
                raise
            credential = None
        return JSON({"credential": credential})

    async def minta_clubs(request):
        who = await principal(request, onboarding=True)
        ops = request.app.state.campus_ops
        viewer = await ops.viewer(who, "club")
        if request.method == "GET":
            clubs = await list_clubs(ops.gateway, who.user_id, viewer, include_archive_summary=True)
            return JSON({**clubs, "grades": GRADES})
        key, command = idempotency_key(request), await body(request, ClubCommand)
        auth = request.app.state.auth
        await auth.rate_limit("minta.club_write", str(who.user_id), 30, 60)
        if isinstance(command.root, PurgeArchives):
            return JSON(await purge_club_archives(ops.gateway, who.user_id, command.root.snapshot))
        if isinstance(command.root, PurgeArchive):
            return JSON(await change_club_archive(ops.gateway, command.root.id, command.root, owner=who.user_id))
        account = await request.app.state.profiles.account(who)
        return JSON(await change_club(ops.gateway, who.user_id, account["display_name"] or account["full_name"], key, command))

    async def password_change_request(request):
        who = await principal(request, onboarding=True)
        data = await body(request, PasswordChangeRequest)
        await request.app.state.auth.request_password_change(who, data.old_password, client_ip(request))
        return JSON(ACCEPTED, status_code=202)

    async def change_password(request):
        who = await principal(request, onboarding=True)
        data = await body(request, PasswordChange)
        await request.app.state.auth.rate_limit("change.user", str(who.user_id), 10, 900)
        await request.app.state.auth.change_password(who, data.old_password, data.new_password, data.token)
        return Response(status_code=204)

    async def sessions(request):
        return JSON({"items": await request.app.state.auth.sessions(await principal(request, onboarding=True))})

    async def logout(request):
        await request.app.state.auth.revoke(await principal(request, onboarding=True))
        return Response(status_code=204)

    async def logout_all(request):
        await request.app.state.auth.revoke(await principal(request, onboarding=True), all_sessions=True)
        return Response(status_code=204)

    async def revoke_session(request):
        await request.app.state.auth.revoke(await principal(request, onboarding=True), uuid_arg(request.path_params["session_id"]))
        return Response(status_code=204)

    async def instances(request):
        who, store = await principal(request), request.app.state.store
        if request.method == "POST":
            data = await body(request, InstanceCreate)
            return JSON(await store.create_instance(who.user_id, data.name), status_code=201)
        return JSON({"items": await store.instances(who.user_id)})

    async def instance(request):
        who, store = await principal(request), request.app.state.store
        instance_id = uuid_arg(request.path_params["instance_id"])
        if request.method == "DELETE":
            await store.delete_instance(who.user_id, instance_id)
            return Response(status_code=204)
        if request.method == "PATCH":
            data = await body(request, InstanceUpdate)
            if not data.model_fields_set:
                raise ServiceError(422, "empty_update", "至少提供一个更新字段")
            return JSON(await store.update_instance(
                who.user_id, instance_id, data.name, data.status, make_default=data.is_default is True,
            ))
        return JSON(await store.instance(who.user_id, instance_id))

    async def conversations(request):
        who, store = await principal(request), request.app.state.store
        instance_id = uuid_arg(request.path_params["instance_id"])
        if request.method == "POST":
            data = await body(request, ConversationCreate)
            return JSON(await store.create_conversation(who.user_id, instance_id, data.title), status_code=201)
        cursor = request.query_params.get("cursor")
        search = request.query_params.get("q", "").strip()
        if len(search) > 200 or "\x00" in search:
            raise ServiceError(422, "invalid_query", "标题搜索词最多 200 个字符")
        return JSON(await store.conversations(
            who.user_id, instance_id, page_limit(request), uuid_arg(cursor) if cursor else None, search=search,
        ))

    async def conversation(request):
        who, store = await principal(request), request.app.state.store
        conversation_id = uuid_arg(request.path_params["conversation_id"])
        if request.method == "DELETE":
            await store.delete_conversation(who.user_id, conversation_id)
            return Response(status_code=204)
        data = await body(request, ConversationCreate)
        if "title" not in data.model_fields_set:
            raise ServiceError(422, "empty_update", "请提供新标题")
        return JSON(await store.update_conversation(who.user_id, conversation_id, data.title))

    async def messages(request):
        who = await principal(request)
        cursor = request.query_params.get("cursor")
        try:
            before = int(cursor) if cursor else None
            if before is not None and (before < 1 or before > 9223372036854775807):
                raise ValueError
        except ValueError:
            raise ServiceError(422, "invalid_cursor", "分页游标无效") from None
        return JSON(await request.app.state.store.messages(
            who.user_id, uuid_arg(request.path_params["conversation_id"]), page_limit(request), before))

    async def submit(request):
        who = await principal(request)
        data = await body(request, Turn)
        return JSON(await request.app.state.store.begin_turn(
            who.user_id, uuid_arg(request.path_params["conversation_id"]), data.query, data.targets,
            idempotency_key(request), session_id=who.session_id), status_code=202)

    async def run(request):
        who = await principal(request)
        return JSON(await request.app.state.store.run(who.user_id, uuid_arg(request.path_params["run_id"])))

    async def cancel(request):
        who = await principal(request)
        return JSON(await request.app.state.store.cancel(who.user_id, uuid_arg(request.path_params["run_id"])))

    async def retry_turn(request):
        who = await principal(request)
        data = await body(request, RetryTurn)
        return JSON(await request.app.state.store.retry_turn(
            who.user_id, uuid_arg(request.path_params["run_id"]), data.attempt, idempotency_key(request),
            session_id=who.session_id,
        ), status_code=202)

    async def withdraw_turn(request):
        who = await principal(request)
        run_id = uuid_arg(request.path_params["run_id"])
        withdrawal = request.app.state.turn_withdrawal
        if request.method == "GET":
            return JSON(await withdrawal.preview(who, run_id))
        data = await body(request, ConfirmWithdrawal)
        approvals = [uuid_arg(value) for value in data.approvals]
        await request.app.state.auth.rate_limit("run.withdraw", str(who.user_id), 30, 60)
        await withdrawal.withdraw(who, run_id, data.attempt, approvals)
        return Response(status_code=204)

    async def map_principal(request):
        who = await principal(request, onboarding=True)
        await request.app.state.campus_access.check(who, "map")
        return who

    async def campus_view_data(request):
        await map_principal(request)
        try:
            return await asyncio.to_thread(load_street_view)
        except StreetViewUnavailable as exc:
            raise ServiceError(503, "campus_view_unavailable", str(exc)) from None

    async def campus_view_index(request):
        root, catalog = await campus_view_data(request)
        places = await asyncio.to_thread(walking_places, root, catalog.campus_id)
        return JSON({**catalog.summary(), "walking_places": places})

    async def campus_view_route(request):
        _, catalog = await campus_view_data(request)
        route = catalog.route(request.path_params["route_id"])
        if route is None:
            raise not_found()
        return JSON({**route.summary(), "coordinate_system": "WGS84", "path": route.path,
                     "points": [point.summary() for point in catalog.ordered_points(route)]})

    async def campus_view_point(request):
        _, catalog = await campus_view_data(request)
        point = next((point for point in catalog.points if point.id == request.path_params["point_id"]), None)
        if point is None:
            raise not_found()
        return JSON({**point.detail(), "coordinate_system": "WGS84"})

    async def campus_view_lookup(request):
        root, catalog = await campus_view_data(request)
        query = await body(request, StreetViewInput)
        return JSON(await asyncio.to_thread(resolve_street_view, catalog, query, root))

    async def campus_view_asset(request):
        root, catalog = await campus_view_data(request)
        found = await asyncio.to_thread(street_view_media, root, catalog, request.path_params["name"])
        if found is None:
            raise not_found()
        path, media_type = found
        return FileResponse(path, media_type=media_type)

    async def campus_photo_places(request):
        await map_principal(request)
        return JSON(await request.app.state.campus_photos.places())

    async def campus_photo_gallery(request):
        who = await map_principal(request)
        photos = request.app.state.campus_photos
        place = check_place(request.path_params["place"])
        fields = request.query_params.multi_items()
        if request.method in {"GET", "HEAD"}:
            if fields and (len(fields) != 1 or fields[0][0] != "offset"):
                raise ServiceError(422, "invalid_photo_page", "分页参数无效")
            return JSON(await photos.gallery(place, page_offset(fields[0][1]) if fields else 0))
        if fields or request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/octet-stream":
            raise ServiceError(415, "invalid_photo_upload", "请直接上传照片文件")
        key = idempotency_key(request)
        await request.app.state.auth.rate_limit("campus_photo.upload", str(who.user_id), 20, 3600)
        raw = await request_bytes(request, MAX_PHOTO_UPLOAD)
        return JSON(await photos.submit(who.user_id, place, key, raw), status_code=201)

    async def campus_photo_image(request):
        await map_principal(request)
        photo = await request.app.state.campus_photos.image(uuid_arg(request.path_params["photo_id"]))
        return Response(photo, media_type="image/jpeg")

    routes = [
        Route("/health", health), Route("/ready", ready),
        Route(CAMPUS_VIEW_PATH, campus_view_index),
        Route(f"{CAMPUS_VIEW_PATH}/routes/{{route_id}}", campus_view_route),
        Route(f"{CAMPUS_VIEW_PATH}/points/{{point_id}}", campus_view_point),
        Route(f"{CAMPUS_VIEW_PATH}/resolve", campus_view_lookup, methods=["POST"]),
        Route(f"{CAMPUS_VIEW_PATH}/media/{{name}}", campus_view_asset),
        Route(f"{CAMPUS_VIEW_PATH}/photo-places", campus_photo_places),
        Route(f"{CAMPUS_VIEW_PATH}/photo-places/{{place}}/photos", campus_photo_gallery, methods=["GET", "POST"]),
        Route(f"{CAMPUS_VIEW_PATH}/photos/{{photo_id}}/image", campus_photo_image),
        Route("/v1/auth/providers", auth_providers),
        Route("/internal/status/skills", runtime_skills),
        Route("/internal/status/campus/{service}", runtime_campus),
        Route("/v1/auth/sso/campus/start", sso_start, methods=["POST"]),
        Route("/v1/auth/sso/campus/callback", sso_callback, methods=["GET", "POST"]),
        Route("/v1/auth/sso/campus/finish", sso_finish, methods=["POST"]),
        Route("/v1/auth/sso/campus/metadata", saml_metadata),
        Route("/v1/auth/identities", identities),
        Route("/v1/auth/identities/{identity_id}", unlink_identity, methods=["DELETE"]),
        Route("/v1/auth/register", register, methods=["POST"]),
        Route("/v1/auth/login", login, methods=["POST"]),
        Route("/v1/auth/refresh", refresh, methods=["POST"]),
        Route("/v1/auth/remember", remember, methods=["POST"]),
        Route("/v1/auth/verification/request", verification_request, methods=["POST"]),
        Route("/v1/auth/verification/confirm", verify, methods=["POST"]),
        Route("/v1/auth/password/request", password_request, methods=["POST"]),
        Route("/v1/auth/password/reset", reset, methods=["POST"]),
        Route("/v1/auth/password/change/request", password_change_request, methods=["POST"]),
        Route("/v1/auth/password/change", change_password, methods=["POST"]),
        Route("/v1/auth/logout", logout, methods=["POST"]),
        Route("/v1/auth/logout-all", logout_all, methods=["POST"]),
        Route("/v1/auth/sessions", sessions),
        Route("/v1/auth/sessions/{session_id}", revoke_session, methods=["DELETE"]),
        Route("/v1/me", me), Route("/v1/profile", profile, methods=["GET", "PUT"]),
        Route("/v1/auth/access", campus_service_access), Route("/v1/auth/access/{service}", campus_service_access),
        Route("/v1/instances", instances, methods=["GET", "POST"]),
        Route("/v1/minta/classrooms", minta_directory), Route("/v1/minta/activities", minta_directory),
        Route("/v1/minta/semester-report", minta_semester_report),
        Route("/v1/calendar/events", calendar_events, methods=["GET", "POST"]),
        Route("/v1/calendar/events/{event_id}", change_calendar_event, methods=["PUT", "DELETE"]),
        Route("/v1/calendar/subscriptions", calendar_subscriptions, methods=["GET", "POST"]),
        Route("/v1/calendar/subscriptions/{subscription_id}", revoke_calendar_subscription, methods=["DELETE"]),
        Route("/v1/calendar/export.ics", calendar_export),
        Route("/v1/calendar/feed.ics", calendar_feed),
        Route("/v1/calendar/microsoft-todo", microsoft_todo_status),
        Route("/v1/calendar/microsoft-todo", microsoft_todo_command, methods=["DELETE"]),
        Route("/v1/calendar/microsoft-todo/start", microsoft_todo_command, methods=["POST"]),
        Route("/v1/calendar/microsoft-todo/finish", microsoft_todo_command, methods=["POST"]),
        Route("/v1/calendar/microsoft-todo/sync", microsoft_todo_command, methods=["POST"]),
        Route("/v1/minta/operations", campus_operations, methods=["GET", "POST"]),
        Route("/v1/minta/operations/{operation_id}", close_campus_operation, methods=["DELETE"]),
        Route("/v1/minta/catalog/{service}", minta_catalog),
        Route("/v1/minta/timetable", minta_timetable),
        Route("/v1/campus-digest/wechat/following", wechat_following, methods=["GET", "PUT"]),
        Route("/v1/minta/teacher-credential", teacher_credential),
        Route("/v1/minta/clubs", minta_clubs, methods=["GET", "POST"]),
        Route("/v1/instances/{instance_id}", instance, methods=["GET", "PATCH", "DELETE"]),
        Route("/v1/instances/{instance_id}/conversations", conversations, methods=["GET", "POST"]),
        Route("/v1/conversations/{conversation_id}", conversation, methods=["PATCH", "DELETE"]),
        Route("/v1/conversations/{conversation_id}/messages", messages),
        Route("/v1/conversations/{conversation_id}/runs", submit, methods=["POST"]),
        Route("/v1/runs/{run_id}", run), Route("/v1/runs/{run_id}/cancel", cancel, methods=["POST"]),
        Route("/v1/runs/{run_id}/retry", retry_turn, methods=["POST"]),
        Route("/v1/runs/{run_id}/withdrawal", withdraw_turn, methods=["GET", "POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(Boundary)])


app = create_app()
