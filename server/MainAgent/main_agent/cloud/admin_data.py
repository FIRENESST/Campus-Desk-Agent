"""Structured operator operations for PostgreSQL, called by the local interface worker."""

import asyncio
import base64
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated
from uuid import UUID

from dotenv import load_dotenv
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import audit
from .campus_access import RuleInput, save_rule, service_rules
from .campus_photos import CampusPhotos, PhotoReview
from .campus_profiles import GRADES
from .config import Settings
from .database import Database
from .errors import ServiceError, not_found
from .minta_clubs import AdminArchiveCommand, ClubReview, change_club_archive, list_club_archives, review_clubs
from .minta_operations import CampusGateway, CampusOperations, campus_catalog
from .roles import AcademicTitle, UserRole
from .security import email_address
from .store import PostgresStore


@dataclass(frozen=True)
class DataSet:
    table: str
    columns: str
    order: str = "created_at DESC,id DESC"
    parent: str = ""


DATASETS = {
    "users": DataSet("users", "id,email,display_name,role,academic_title,is_active,verified_at,created_at"),
    "instances": DataSet("agent_instances", "id,user_id,name,status,is_default,created_at,updated_at", parent="user_id"),
    "conversations": DataSet("conversations", "id,user_id,instance_id,title,created_at,updated_at",
                             "updated_at DESC,id DESC", "user_id"),
    "messages": DataSet("messages", "id,conversation_id,role,left(content,16000) AS content,created_at", "id DESC", "conversation_id"),
    "runs": DataSet("runs", "id,conversation_id,status,result_status,attempt,created_at,started_at,finished_at,withdrawn_at",
                    parent="conversation_id"),
    "sessions": DataSet("auth_sessions", "id,user_id,auth_method,created_at,last_seen_at,expires_at,revoked_at", parent="user_id"),
    "identities": DataSet("external_identities", "id,user_id,protocol,created_at", parent="user_id"),
    "minta": DataSet("campus_minta_operations", "id,user_id,service,resource_id,resource_name,state,error_code,created_at,updated_at",
                    parent="user_id"),
    "user-audit": DataSet("audit_events", "id,user_id,action,outcome,created_at", "id DESC", "user_id"),
}


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)


class UserUpdate(Input):
    display_name: Annotated[str, Field(max_length=50)] | None = None
    is_active: bool | None = None


class RoleUpdate(Input):
    role: str
    academic_title: str | None = None


class InstanceUpdate(Input):
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    status: str | None = None
    make_default: bool = False


class ConversationUpdate(Input):
    title: Annotated[str, Field(min_length=1, max_length=200)]


class ProfileReview(Input):
    revision: str
    approved: bool


class ProfileReviewTarget(Input):
    id: str
    revision: str


class ProfileReviewBatch(Input):
    items: Annotated[list[ProfileReviewTarget], Field(min_length=1, max_length=40)]


def _model(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise ServiceError(422, "invalid_request", "字段或取值无效") from None


def _uuid(value):
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(422, "invalid_id", "请输入有效的用户、会话或记录 ID") from None


def _dataset(resource):
    if resource not in DATASETS:
        raise not_found()
    return DATASETS[resource]


def _list(connection, resource, options):
    dataset = _dataset(resource)
    parent, search = options.get("parent"), options.get("q", "").strip()
    conditions, values = [], []
    if parent:
        if not dataset.parent:
            raise ServiceError(422, "invalid_filter", "此列表不支持按关联 ID 查询")
        conditions.append(f"{dataset.parent}=%s")
        values.append(_uuid(parent))
    if resource == "messages":
        if not parent:
            raise ServiceError(422, "conversation_required", "查看消息前请填写会话 ID")
        conditions.append("role IN ('user','assistant')")
        conditions.append("NOT EXISTS (SELECT 1 FROM public.runs r WHERE r.id=messages.run_id AND r.withdrawn_at IS NOT NULL)")
    if search:
        if resource != "users":
            raise ServiceError(422, "invalid_filter", "关键词搜索适用于用户列表")
        conditions.append("(id::text=lower(%s) OR strpos(lower(email),lower(%s))>0 OR strpos(lower(coalesce(display_name,'')),lower(%s))>0)")
        values.extend((search, search, search))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    offset = options["offset"]
    # Columns, relation and ordering come exclusively from DATASETS.
    rows = connection.execute(
        f"SELECT {dataset.columns} FROM public.{dataset.table}{where} ORDER BY {dataset.order} LIMIT 41 OFFSET %s",
        (*values, offset),
    ).fetchall()
    return {"items": rows[:40], "next_offset": offset + 40 if len(rows) > 40 and offset < 100_000 else None}


def _verify(connection, email, actor):
    user = connection.execute(
        "SELECT id,is_active,verified_at FROM public.users WHERE email=%s FOR UPDATE", (email_address(email),),
    ).fetchone()
    if user is None:
        raise not_found()
    if not user["is_active"]:
        raise ServiceError(409, "inactive_account", "请先启用该用户")
    changed = user["verified_at"] is None
    if changed:
        connection.execute("UPDATE public.users SET verified_at=now(),updated_at=now() WHERE id=%s", (user["id"],))
        connection.execute("UPDATE public.account_tokens SET consumed_at=now() WHERE user_id=%s AND purpose='verify' "
                           "AND consumed_at IS NULL", (user["id"],))
    audit(connection, user["id"], "account.verify.operator", "completed" if changed else "already_verified", actor)
    return {"id": user["id"], "changed": changed}


def _admin_identity(connection, user_id):
    user = connection.execute(
        "SELECT email,is_active,verified_at FROM public.users WHERE id=%s", (user_id,),
    ).fetchone()
    if user is None:
        raise ServiceError(404, "user_not_found", "未找到该 UUID 对应的用户")
    if not user["is_active"] or user["verified_at"] is None:
        raise ServiceError(409, "admin_identity_ineligible", "在线管理员必须是已启用且已验证邮箱的用户")
    return {"id": user_id, "email": user["email"]}


def _campus_profiles(connection, args):
    search = args.get("q", "").strip().lower()
    offset = args["offset"]
    rows = connection.execute(
        """SELECT i.id,i.protocol,i.issuer,i.subject,u.email,u.role,u.is_active,
        u.verified_at IS NOT NULL AS email_verified,p.full_name,p.campus_number,
        p.college,p.grade,p.revision,p.reviewed_at FROM external_identities i JOIN users u ON u.id=i.user_id
        LEFT JOIN campus_profiles p ON p.identity_id=i.id AND p.role=u.role
        WHERE %s='' OR strpos(lower(u.email),%s)>0 OR strpos(lower(coalesce(p.full_name,'')),%s)>0
        OR strpos(lower(coalesce(p.campus_number,'')),%s)>0
        ORDER BY (p.revision IS NOT NULL AND p.reviewed_at IS NULL) DESC,i.created_at DESC,i.id LIMIT 41 OFFSET %s""",
        (search, search, search, search, offset),
    ).fetchall()
    return {"items": rows[:40], "grades": GRADES, "next_offset": offset + 40 if len(rows) > 40 else None}


def _review_profile(connection, identity_id, review, actor):
    row = connection.execute(
        """UPDATE campus_profiles p SET reviewed_at=CASE WHEN %s THEN now() ELSE NULL END,revision=gen_random_uuid()
        FROM external_identities i JOIN users u ON u.id=i.user_id
        WHERE p.identity_id=i.id AND i.id=%s AND p.revision=%s AND p.role=u.role AND u.is_active
        RETURNING i.user_id""",
        (review.approved, identity_id, _uuid(review.revision)),
    ).fetchone()
    if not row:
        raise ServiceError(409, "profile_review_stale", "资料已变化或账号已停用，请刷新后核验")
    audit(connection, row["user_id"], "campus.profile.review", "approved" if review.approved else "revoked", actor)
    return {"approved": review.approved}


def _review_profiles(connection, batch, actor, cas_accounts):
    revisions = {_uuid(item.id): _uuid(item.revision) for item in batch.items}
    if len(revisions) != len(batch.items):
        raise ServiceError(422, "duplicate_profiles", "批量核验中有重复资料")
    rows = connection.execute(
        """SELECT i.id,i.subject,p.revision,p.campus_number
        FROM external_identities i JOIN users u ON u.id=i.user_id
        JOIN campus_profiles p ON p.identity_id=i.id AND p.role=u.role
        WHERE i.id=ANY(%s) AND i.protocol='cas' AND i.issuer='urn:campus-desk:cas:local'
        AND u.is_active AND u.verified_at IS NOT NULL AND p.reviewed_at IS NULL
        ORDER BY u.id,i.id FOR UPDATE OF u""",
        (list(revisions),),
    ).fetchall()
    stale = ServiceError(409, "profile_review_stale", "部分资料或账号状态已变化，请刷新后重新核验本页")
    if len(rows) != len(revisions) or any(row["revision"] != revisions[row["id"]] for row in rows):
        raise stale

    accounts = cas_accounts([row["subject"] for row in rows])
    for row in rows:
        account = accounts.get(row["subject"])
        if (not account or not account["enabled"] or not account["is_campus_number"]
                or account["username"] != row["campus_number"].lower()):
            raise stale

    for row in rows:
        review = ProfileReview(revision=str(row["revision"]), approved=True)
        _review_profile(connection, row["id"], review, actor)
    return {"approved_count": len(rows)}


def _unbind_identity(connection, identity_id, actor):
    row = connection.execute(
        "SELECT u.id FROM users u JOIN external_identities i ON i.user_id=u.id WHERE i.id=%s FOR UPDATE OF u",
        (identity_id,),
    ).fetchone()
    if not row:
        raise not_found()
    removed = connection.execute("DELETE FROM external_identities WHERE id=%s AND user_id=%s", (identity_id, row["id"]))
    if not removed.rowcount:
        raise not_found()
    connection.execute("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (row["id"],))
    audit(connection, row["id"], "campus.identity.unbind", "completed", actor)
    return {"removed": identity_id}


def _update_user(connection, user_id, data, actor):
    changes = data.model_dump(exclude_unset=True)
    if not changes or ("is_active" in changes and data.is_active is None):
        raise ServiceError(422, "invalid_request", "请选择要更新的用户字段")
    current = connection.execute("SELECT id FROM public.users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
    if current is None:
        raise not_found()
    if "display_name" in changes:
        connection.execute("UPDATE public.users SET display_name=%s,updated_at=now() WHERE id=%s",
                           (data.display_name or None, user_id))
    if "is_active" in changes:
        connection.execute("UPDATE public.users SET is_active=%s,updated_at=now() WHERE id=%s", (data.is_active, user_id))
        if not data.is_active:
            connection.execute("UPDATE public.auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
            connection.execute("UPDATE public.account_tokens SET consumed_at=now() WHERE user_id=%s AND consumed_at IS NULL", (user_id,))
    audit(connection, user_id, "account.update.operator", "completed", actor)
    return {"updated": user_id}


def _role(connection, user_id, data, actor):
    try:
        role = UserRole(data.role)
        title = AcademicTitle(data.academic_title) if data.academic_title else None
    except ValueError:
        raise ServiceError(422, "invalid_role", "角色或教师职称无效") from None
    if title and role != UserRole.TEACHER:
        raise ServiceError(422, "invalid_role", "教师账号才能设置职称")
    row = connection.execute(
        "UPDATE public.users SET role=%s,academic_title=%s,updated_at=now() WHERE id=%s RETURNING id",
        (role.value, title.value if title else None, user_id),
    ).fetchone()
    if row is None:
        raise not_found()
    audit(connection, user_id, "account.role.operator", role.value, actor)
    return {"updated": user_id}


def _revoke_sessions(connection, resource, record, actor):
    if resource == "users":
        if connection.execute("SELECT 1 FROM public.users WHERE id=%s FOR UPDATE", (record,)).fetchone() is None:
            raise not_found()
        count = connection.execute("UPDATE public.auth_sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL",
                                   (record,)).rowcount
        audit(connection, record, "account.sessions.operator", "completed", actor)
        return {"revoked": count}
    session = connection.execute(
        "UPDATE public.auth_sessions SET revoked_at=coalesce(revoked_at,now()) WHERE id=%s RETURNING user_id",
        (record,),
    ).fetchone()
    if session is None:
        raise not_found()
    audit(connection, session["user_id"], "account.session.operator", "completed", actor)
    return {"revoked": 1}


async def _change_record(db, resource, action, record, payload):
    if resource not in {"instances", "conversations"}:
        raise not_found()
    table = DATASETS[resource].table
    owner = await db.run(lambda c: c.execute(f"SELECT user_id FROM public.{table} WHERE id=%s", (record,)).fetchone())
    if owner is None:
        raise not_found()
    user_id = owner["user_id"]
    store = PostgresStore(db, None)
    if resource == "instances":
        if action == "delete":
            await store.delete_instance(user_id, record)
        else:
            data = _model(InstanceUpdate, payload)
            if data.status not in {None, "active", "paused"} or not (data.name or data.status or data.make_default):
                raise ServiceError(422, "invalid_request", "实例名称或状态无效")
            await store.update_instance(user_id, record, **data.model_dump())
    elif action == "delete":
        await store.delete_conversation(user_id, record)
    else:
        data = _model(ConversationUpdate, payload)
        await store.update_conversation(user_id, record, data.title)
    return {"id": record, "deleted": action == "delete"}


async def _execute(dsn, action, args, *, cas_accounts=None):
    db = Database(dsn, max_size=1)
    try:
        await db.open()
        if action == "verify_user":
            return await db.run(lambda c: _verify(c, args["email"], args["actor"]))
        if action == "admin_identity":
            return await db.run(lambda c: _admin_identity(c, _uuid(args["id"])))
        if action == "campus_profiles":
            return await db.run(lambda c: _campus_profiles(c, args))
        if action == "campus_profile_review":
            return await db.run(lambda c: _review_profile(c, _uuid(args["id"]), _model(ProfileReview, args["payload"]), args["actor"]))
        if action == "campus_profiles_bulk_review":
            if cas_accounts is None:
                raise ServiceError(503, "cas_state_unavailable", "CAS 账号状态暂不可用")
            batch = _model(ProfileReviewBatch, args["payload"])
            return await db.run(lambda c: _review_profiles(c, batch, args["actor"], cas_accounts))
        if action == "campus_identity_unbind":
            return await db.run(lambda c: _unbind_identity(c, _uuid(args["id"]), args["actor"]))
        if action == "campus_rules":
            rules = await db.run(lambda c: service_rules(c, args["service"]))
            return {"rules": rules, "grades": GRADES}
        if action == "campus_rule_update":
            service, resource_id = args["service"], args["id"] or ""
            rule = _model(RuleInput, args["payload"])
            if resource_id:
                if service != "classroom":
                    raise not_found()
                items = (await campus_catalog(CampusGateway(), "classroom"))["items"]
                if not any(room["id"] == resource_id for room in items):
                    raise not_found()
            return await db.run(lambda c: save_rule(c, service, resource_id, rule))
        if action == "club_reviews":
            return await review_clubs(CampusGateway(), offset=args["offset"])
        if action == "club_review":
            return await review_clubs(CampusGateway(), club_id=str(_uuid(args["id"])), review=_model(ClubReview, args["payload"]))
        if action == "club_archives":
            return await list_club_archives(CampusGateway(), args["offset"])
        if action == "club_archive":
            return await change_club_archive(CampusGateway(), str(_uuid(args["id"])), _model(AdminArchiveCommand, args["payload"]))
        if action == "photo_review_places":
            return await CampusPhotos(db).review_places(args["status"], args["offset"])
        if action == "photo_reviews":
            return await CampusPhotos(db).review_queue(args["status"], args["offset"], args.get("place"))
        if action == "photo_review":
            return await CampusPhotos(db).review(_uuid(args["id"]), _uuid(args["actor"].removeprefix("web:")),
                                                 _model(PhotoReview, args["payload"]))
        if action == "photo_delete":
            return await CampusPhotos(db).delete(_uuid(args["id"]), args["version"])
        if action == "photo_preview":
            image = await CampusPhotos(db).image(_uuid(args["id"]), approved_only=False)
            return {"image_base64": base64.b64encode(image).decode("ascii")}
        resource = args["resource"]
        if action == "list":
            return await db.run(lambda c: _list(c, resource, args))
        if action == "create":
            owner = _uuid(args["parent"])
            if resource == "instances":
                data = _model(InstanceUpdate, args["payload"])
                if data.name is None or data.model_fields_set != {"name"}:
                    raise ServiceError(422, "invalid_request", "创建实例需要实例名称")
                if not await db.run(lambda c: c.execute("SELECT 1 FROM public.users WHERE id=%s AND is_active", (owner,)).fetchone()):
                    raise not_found()
                instance = await PostgresStore(db, Settings.from_env()).create_instance(owner, data.name)
                return {"id": instance["id"]}
            raise not_found()
        record, actor = _uuid(args["id"]), args["actor"]
        if action == "revoke" and resource in {"users", "sessions"}:
            return await db.run(lambda c: _revoke_sessions(c, resource, record, actor))
        if action == "role" and resource == "users":
            data = _model(RoleUpdate, args["payload"])
            return await db.run(lambda c: _role(c, record, data, actor))
        if action in {"minta_close", "minta_reconcile"} and resource == "minta":
            row = await db.run(lambda c: c.execute("SELECT user_id FROM public.campus_minta_operations WHERE id=%s",
                                                   (record,)).fetchone())
            if row is None:
                raise not_found()
            owner = SimpleNamespace(user_id=row["user_id"])
            operations = CampusOperations(db, None)
            handler = operations.close if action == "minta_close" else operations.reconcile_operation
            receipt = await handler(owner, record)
            await db.run(lambda c: audit(c, owner.user_id, f"{action}.operator", receipt["state"], actor))
            return receipt
        if action == "update" and resource == "users":
            data = _model(UserUpdate, args["payload"])
            return await db.run(lambda c: _update_user(c, record, data, actor))
        if action in {"delete", "update"}:
            return await _change_record(db, resource, action, record, args["payload"])
        raise not_found()
    except PoolTimeout:
        raise ServiceError(503, "database_unavailable", "PostgreSQL 暂不可用") from None
    finally:
        await db.close()


def operator_operation(action, args, *, cas_accounts=None):
    load_dotenv(Path(__file__).resolve().parents[2] / ".env.cloud", override=False, encoding="utf-8-sig")
    if action == "options":
        return {"role_editable": bool(os.getenv("CLOUD_ADMIN_ROLE_DATABASE_URL")), "titles": [title.value for title in AcademicTitle]}
    variable = "CLOUD_ADMIN_ROLE_DATABASE_URL" if action == "role" else "CLOUD_DATABASE_URL"
    dsn = os.getenv(variable)
    if not dsn:
        message = "请先配置专用的角色编辑数据库凭据" if action == "role" else "PostgreSQL 连接尚未配置"
        raise ServiceError(503, "database_unconfigured", message)
    return asyncio.run(_execute(dsn, action, args, cas_accounts=cas_accounts))
