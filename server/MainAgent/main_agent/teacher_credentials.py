"""Loopback credential service for classroom reservations and local administration."""

import asyncio
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .cloud.config import Settings
from .cloud.database import Database
from .cloud.errors import ServiceError


DATA_DIR = Path(__file__).resolve().parents[3] / "DataBase" / "TeacherCredentials"
PORT = 8192
BASE_URL = f"http://127.0.0.1:{PORT}"
MAX_BODY = 8192
ACCOUNT = """SELECT u.id, u.email, u.role, u.is_active, u.verified_at,
    (SELECT COALESCE(jsonb_agg(jsonb_build_array(e.id, e.protocol, e.issuer, e.subject) ORDER BY e.id), '[]'::jsonb)
     FROM external_identities e WHERE e.user_id=u.id) AS campus_links FROM users u"""


def read_key(directory: Path = DATA_DIR, *, create: bool = False) -> bytes:
    path = directory / "credential.key"
    if create and not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
    with path.open("rb") as stream:
        key = stream.read(33)
    if len(key) != 32:
        raise ValueError("教师凭据密钥格式无效")
    return key


def credential_digest(key: bytes, domain: str, *values) -> str:
    payload = json.dumps([domain, *values], ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.digest(key, payload, "sha256").hex()


def contact_values(name: str, phone: str) -> tuple[str, str]:
    name = " ".join(unicodedata.normalize("NFKC", name).split())
    phone = re.sub(r"[\s()\-]", "", unicodedata.normalize("NFKC", phone))
    if phone.startswith("+86"):
        phone = phone[3:]
    if (not 1 <= len(name) <= 80 or any(unicodedata.category(char).startswith("C") for char in name)
            or not re.fullmatch(r"\+?[0-9]{7,15}", phone)):
        raise ServiceError(422, "teacher_contact_invalid", "请填写老师姓名和有效的电话号码")
    return name, phone


def unverified() -> ServiceError:
    return ServiceError(403, "teacher_credential_unverified", "教师信息未通过凭据核验，请核对资料或联系管理员")


class CredentialStore:
    def __init__(self, directory: Path, key: bytes):
        self.path, self.key = directory / "teachers.sqlite3", key

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def initialize(self):
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript((DATA_DIR / "schema.sql").read_text(encoding="utf-8"))

    def get(self, *, user_id: str | None = None, contact: tuple[str, str] | None = None, credential_id: str | None = None):
        with self.connect() as connection:
            if credential_id is not None:
                return connection.execute("SELECT * FROM teacher_credentials WHERE credential_id=?", (credential_id,)).fetchone()
            if contact is None:
                return connection.execute("SELECT * FROM teacher_credentials WHERE user_id=?", (user_id,)).fetchone()
            digest = credential_digest(self.key, "contact", contact[0].casefold(), contact[1])
            return connection.execute("SELECT * FROM teacher_credentials WHERE contact_digest=? AND revoked_at IS NULL",
                                      (digest,)).fetchone()

    def binding(self, account) -> str:
        return credential_digest(self.key, "identity", account["email"].casefold(), account["campus_links"])

    def valid(self, row, account) -> bool:
        return bool(row and account and account["role"] == "teacher" and account["is_active"]
                    and account["verified_at"] is not None and row["revoked_at"] is None
                    and row["expires_at"] > time.time()
                    and hmac.compare_digest(row["identity_digest"], self.binding(account)))

    def change(self, action, account, actor, *, name=None, phone=None, days=None):
        user_id, now = str(account["id"]), int(time.time())
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM teacher_credentials WHERE user_id=?", (user_id,)).fetchone()
            if action == "revoke":
                if not row:
                    raise ServiceError(404, "credential_not_found", "该账号尚无教师凭据")
                if row["revoked_at"] is None:
                    connection.execute("UPDATE teacher_credentials SET revoked_at=? WHERE user_id=?", (now, user_id))
                    self.event(connection, row["credential_id"], user_id, actor, action, now)
            else:
                if action == "issue" and self.valid(row, account):
                    raise ServiceError(409, "credential_exists", "该账号已有有效凭据，请使用修改")
                if action == "update" and (not row or row["revoked_at"] is not None):
                    raise ServiceError(404, "credential_not_found", "请先为该账号签发凭据")
                name, phone = contact_values(name, phone)
                serial = str(uuid4())
                connection.execute(
                    """INSERT INTO teacher_credentials VALUES(?,?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(user_id) DO UPDATE SET credential_id=excluded.credential_id,
                    full_name=excluded.full_name, contact_digest=excluded.contact_digest, phone_tail=excluded.phone_tail,
                    identity_digest=excluded.identity_digest, campus_linked=excluded.campus_linked,
                    issued_at=excluded.issued_at, expires_at=excluded.expires_at, revoked_at=NULL""",
                    (user_id, serial, name, credential_digest(self.key, "contact", name.casefold(), phone),
                     phone[-4:], self.binding(account), int(bool(account["campus_links"])), now, now + days * 86400),
                )
                self.event(connection, serial, user_id, actor, action, now)
            return connection.execute("SELECT * FROM teacher_credentials WHERE user_id=?", (user_id,)).fetchone()

    @staticmethod
    def event(connection, serial, user_id, actor, action, now):
        connection.execute("INSERT INTO credential_events(credential_id,user_id,actor,action,created_at) VALUES(?,?,?,?,?)",
                           (serial, user_id, actor, action, now))


def credential_view(row) -> dict:
    return {
        "id": row["credential_id"], "campus_linked": bool(row["campus_linked"]),
        "valid_until": datetime.fromtimestamp(row["expires_at"], UTC).isoformat(),
    }


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1, max_length=120)]
    phone: Annotated[str, Field(min_length=1, max_length=40)]


class AccountCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: UUID


class CredentialCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    credential_id: UUID


class AdminInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    email: Annotated[str, Field(min_length=3, max_length=254)]
    actor: Annotated[str, Field(min_length=1, max_length=40)]
    contact: Contact | None = None
    days: Annotated[int, Field(ge=1, le=730)] = 365


async def checked_input(request, model, domain):
    headers = request.headers.getlist("authorization")
    key = request.app.state.credentials.key
    expected = ("Bearer " + credential_digest(key, domain)).encode()
    if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("utf-8"), expected):
        raise ServiceError(401, "credential_access_denied", "教师凭据服务需要本机授权")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY:
            raise ServiceError(413, "credential_request_too_large", "教师凭据请求过大")
        data.extend(chunk)
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise ServiceError(422, "credential_request_invalid", "教师凭据请求格式无效") from None


async def get_account(database, *, user_id=None, email=None):
    def query(connection):
        connection.execute("SET TRANSACTION READ ONLY")
        where = "u.id=%s" if user_id is not None else "u.email=%s"
        return connection.execute(f"{ACCOUNT} WHERE {where}", (user_id if user_id is not None else email,)).fetchone()
    return await database.run(query)


def create_app(settings=None, *, database=None, directory: Path = DATA_DIR):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        key = await asyncio.to_thread(read_key, directory, create=True)
        store = CredentialStore(directory, key)
        await asyncio.to_thread(store.initialize)
        db = database or Database(config.database_url, 2)
        await db.open()
        try:
            app.state.credentials, app.state.db = store, db
            yield
        finally:
            await db.close()

    async def health(_request):
        return JSONResponse({"status": "ok"})

    async def verify(request):
        store = request.app.state.credentials
        if request.path_params["by"] == "account":
            data = await checked_input(request, AccountCheck, "verifier")
            row = await asyncio.to_thread(store.get, user_id=str(data.user_id))
        elif request.path_params["by"] == "contact":
            data = await checked_input(request, Contact, "verifier")
            row = await asyncio.to_thread(store.get, contact=contact_values(data.name, data.phone))
        elif request.path_params["by"] == "credential":
            data = await checked_input(request, CredentialCheck, "verifier")
            row = await asyncio.to_thread(store.get, credential_id=str(data.credential_id))
        else:
            raise ServiceError(404, "credential_route_unknown", "凭据核验方式不存在")
        if not row or row["revoked_at"] is not None or row["expires_at"] <= time.time():
            raise unverified()
        account = await get_account(request.app.state.db, user_id=UUID(row["user_id"]))
        if not store.valid(row, account):
            raise unverified()
        # A revoke or update concurrent with the account check invalidates this snapshot.
        current = await asyncio.to_thread(store.get, user_id=row["user_id"])
        if not current or current["credential_id"] != row["credential_id"] or current["revoked_at"] is not None:
            raise unverified()
        return JSONResponse(credential_view(row), headers={"Cache-Control": "no-store"})

    async def manage(request):
        data = await checked_input(request, AdminInput, "operator")
        action = request.path_params["action"]
        account = await get_account(request.app.state.db, email=data.email.casefold())
        if not account:
            raise ServiceError(404, "account_not_found", "未找到该邮箱对应的账号")
        store = request.app.state.credentials
        if action == "inspect":
            row = await asyncio.to_thread(store.get, user_id=str(account["id"]))
            if not row:
                raise ServiceError(404, "credential_not_found", "该账号尚无教师凭据")
        elif action in {"issue", "update", "revoke"}:
            if action != "revoke" and (not data.contact or account["role"] != "teacher"
                                       or not account["is_active"] or account["verified_at"] is None):
                raise ServiceError(422, "teacher_account_required", "签发或修改需要有效的教师账号、已验证邮箱和联系资料")
            row = await asyncio.to_thread(store.change, action, account, data.actor,
                                          name=data.contact.name if data.contact else None,
                                          phone=data.contact.phone if data.contact else None, days=data.days)
        else:
            raise ServiceError(404, "credential_route_unknown", "凭据管理操作不存在")
        return JSONResponse({**credential_view(row), "name": row["full_name"], "phone_tail": row["phone_tail"],
                             "valid": store.valid(row, account), "revoked": row["revoked_at"] is not None},
                            headers={"Cache-Control": "no-store"})

    async def error(_request, exc):
        if isinstance(exc, sqlite3.IntegrityError):
            exc = ServiceError(409, "teacher_contact_conflict", "这组老师姓名和电话已登记")
        if not isinstance(exc, ServiceError):
            exc = ServiceError(503, "credential_service_unavailable", "教师凭据服务暂不可用")
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route("/healthz", health), Route("/verify/{by}", verify, methods=["POST"]),
                             Route("/manage/{action}", manage, methods=["POST"])], lifespan=lifespan,
                     exception_handlers={ServiceError: error, sqlite3.Error: error})


class TeacherCredentialClient:
    def __init__(self, http: httpx2.AsyncClient, *, url: str = BASE_URL, directory: Path = DATA_DIR):
        self.http, self.url, self.directory = http, url, directory

    async def verify(self, by, payload):
        try:
            key = await asyncio.to_thread(read_key, self.directory)
            bearer = credential_digest(key, "verifier")
            response = await self.http.post(f"{self.url}/verify/{by}", json=payload,
                                            headers={"Authorization": f"Bearer {bearer}"}, timeout=5)
            if response.status_code == 403:
                raise unverified()
            response.raise_for_status()
            result = response.json()
            return {"id": str(UUID(result["id"])), "valid_until": result["valid_until"],
                    "campus_linked": result["campus_linked"] is True}
        except (OSError, ValueError, TypeError, KeyError, httpx2.HTTPError):
            raise ServiceError(503, "credential_service_unavailable", "教师凭据服务暂不可用") from None


if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env.cloud", override=False, encoding="utf-8-sig")
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, access_log=False)
