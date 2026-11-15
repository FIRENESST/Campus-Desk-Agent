"""Authenticated operations against the campus simulation gateway."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import uuid4

import httpx2
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from ..campus_cluster.activity_time import ActivityTimestamp, campus_minute, validate_activity_range
from ..campus_cluster.library_catalog import (
    DEFAULT_LOAN_DAYS, LIBRARY_PAGE_SIZE, LibraryLoanWindow, LoanDays, ResourceId, library_catalog,
)
from ..campus_cluster.subskills.minta_campus import GymSlotsQuery
from ..campus_cluster.subskills.minta_gateway import _connection_settings
from ..minta import MintaGatewayClient, MintaProtocolError, MintaRequestError
from ..teacher_credentials import Contact, contact_values, credential_digest, read_key
from .chat_effects import add_effect, lock_effect_run, resolve_effect
from .errors import ServiceError, not_found


ServiceName = Literal["classroom", "gym", "club", "library"]
STALE_AFTER = timedelta(seconds=30)
CLUB_KEY = Path(__file__).resolve().parents[4] / "simulation" / "Club" / ".data" / "management.key"
ACTIVE_STATES = ("pending", "active", "uncertain", "closing", "close_uncertain")
CLUB_REGISTRATION_BATCH = 128
ERROR_MESSAGES = {
    "NOT_FOUND": "该场地、活动或图书不存在",
    "CONFLICT": "该资源已被预约、借出或报名",
    "CAPACITY_FULL": "当前没有剩余名额或可借副本",
    "SLOT_UNAVAILABLE": "该场地时段已关闭或已开始，请重新选择",
    "INVALID_ARGUMENT": "业务参数未通过校验",
    "FORBIDDEN": "当前账号没有这项管理权限",
    "CLUB_REVIEW_REQUIRED": "社团通过审核后才能发布活动",
    "GATEWAY_UNAVAILABLE": "MINTA 网关暂不可用，请稍后重试",
    "UPSTREAM_REJECTED": "校园服务暂时无法完成该操作",
}

GymSlotId = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[1-9][0-9]?_[0-9]{4}$")]


class GymSlotDetails(BaseModel):
    slot_id: GymSlotId
    court_id: str = Field(pattern=r"^[1-9][0-9]?$")
    court_name: str = Field(min_length=1, max_length=80)
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def check_slot(self):
        start = self.starts_at.astimezone(CAMPUS_TIME_ZONE)
        if self.slot_id != f"{start:%Y-%m-%d}_{self.court_id}_{start:%H%M}" or self.ends_at <= self.starts_at:
            raise ValueError("场地时段和回执不一致")
        return self


class GymSlot(GymSlotDetails):
    capacity: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def check_capacity(self):
        if self.remaining > self.capacity:
            raise ValueError("场地时段余量超过容量")
        return self


class GymBookingWindow(BaseModel):
    first_date: date
    last_date: date


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    service: ServiceName
    resource_id: ResourceId
    teacher: Contact | None = None
    duration_days: LoanDays | None = None
    slot_id: GymSlotId | None = None
    identity_confirmed: bool = False
    starts_at: ActivityTimestamp | None = None
    ends_at: ActivityTimestamp | None = None


def classroom_period(starts_at, ends_at):
    if not starts_at or not ends_at:
        raise ServiceError(422, "classroom_period_required", "请填写教室预约的开始和结束时间（北京时间）")
    try:
        start, end = campus_minute(starts_at), campus_minute(ends_at)
        if end <= start:
            raise ValueError
    except (OverflowError, ValueError):
        raise ServiceError(422, "classroom_invalid_period", "请选择有效的起止时间，结束时间须晚于开始时间") from None
    return start, end


def merge_periods(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True)
class OperationSpec:
    create: str
    close: str
    records: str
    records_key: str
    receipt: str | None
    resource_arg: str
    actor_arg: str
    close_arg: str
    resource_field: str
    actor_field: str
    id_field: str
    name_field: str = ""


OPERATIONS = {
    "classroom": OperationSpec("classroom_reserve_room", "classroom_cancel_reservation", "/api/reservations",
                               "reservations", "reservation", "room_id", "user_name", "reservation_id",
                               "roomId", "userName", "reservationId", "roomName"),
    "gym": OperationSpec("gym_reserve_venue", "gym_cancel_reservation", "/api/reservations",
                         "reservations", None, "venue_id", "user_name", "reservation_id",
                         "venue_id", "user_name", "reservation_id", "venue_name"),
    "club": OperationSpec("club_register_activity", "club_cancel_registration", "/managed/registrations",
                          "registrations", "registration", "activity_id", "participant_name", "registration_id",
                          "activity_id", "participant_name", "id", "activity_name"),
    "library": OperationSpec("library_borrow_book", "library_return_book", "/api/loans",
                             "loans", "loan", "book_id", "borrower_name", "loan_id",
                             "book_id", "borrower_name", "loan_id", "book_title"),
}


class GatewayUncertain(Exception):
    """The upstream may have completed an operation without delivering a receipt."""


class GatewayRejected(Exception):
    def __init__(self, code, status=409):
        self.code = code if code in ERROR_MESSAGES else "UPSTREAM_REJECTED"
        self.status = status

    def as_service_error(self):
        return ServiceError(self.status, self.code.lower(), ERROR_MESSAGES[self.code])


class CampusGateway:
    async def request(self, method, path, payload=None):
        try:
            address, headers = _connection_settings()
        except (ValueError, httpx2.InvalidURL):
            raise GatewayRejected("GATEWAY_UNAVAILABLE", 503) from None
        options = {"json": payload} if payload is not None else {}
        if path.startswith("/club/managed/"):
            try:
                def read_club_key():
                    with CLUB_KEY.open("rb") as stream:
                        return stream.read(33)
                key = await asyncio.to_thread(read_club_key)
                if len(key) != 32:
                    raise ValueError
                token = hmac.digest(key, b"minta-club-management-v1", "sha256").hex()
                options["headers"] = {"authorization": f"Bearer {token}"}
            except (OSError, ValueError):
                raise GatewayRejected("GATEWAY_UNAVAILABLE", 503) from None
        try:
            async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(8, connect=2),
                                         trust_env=False, follow_redirects=False) as http:
                response = await MintaGatewayClient(address, http).call(method, path, timeout_ms=5000, **options)
        except MintaRequestError as exc:
            if isinstance(exc.__cause__, (httpx2.ConnectError, httpx2.ConnectTimeout)):
                raise GatewayRejected("GATEWAY_UNAVAILABLE", 503) from None
            raise GatewayUncertain from None
        except TimeoutError as exc:
            if isinstance(exc.__cause__, httpx2.ConnectTimeout):
                raise GatewayRejected("GATEWAY_UNAVAILABLE", 503) from None
            raise GatewayUncertain from None
        except MintaProtocolError:
            raise GatewayUncertain from None

        if response.status >= 500 or len(response.body) > 2 * 1024 * 1024:
            raise GatewayUncertain
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if response.status in {400, 403, 404, 409, 422}:
                error = data.get("error", data.get("detail", data))
                code = error.get("code") if isinstance(error, dict) else None
                raise GatewayRejected(code, response.status)
            if not 200 <= response.status < 300:
                raise GatewayUncertain
            return data
        except (ValueError, TypeError, RecursionError):
            raise GatewayUncertain from None

    async def tool(self, service, name, arguments):
        data = await self.request("POST", f"/{service}/agent/call", {"name": name, "arguments": arguments})
        try:
            result = json.loads(data["content"])
            if not isinstance(result, dict):
                raise ValueError
            if data.get("isError") is True:
                error = result.get("error", result)
                raise GatewayRejected(error.get("code") if isinstance(error, dict) else None)
            return result
        except (KeyError, ValueError, TypeError, RecursionError):
            raise GatewayUncertain from None


def public_operation(row):
    result = {"id": str(row["id"]), "service": row["service"], "resource_id": row["resource_id"],
              "resource_name": row["resource_name"],
              "state": row["state"], "created_at": row["created_at"].isoformat(),
              "message": ERROR_MESSAGES.get(row["error_code"])}
    if row["service"] == "library":
        days, borrowed_at = row["loan_days"], row["loan_borrowed_at"]
        due_at = borrowed_at.astimezone(UTC) + timedelta(days=days) if borrowed_at else None
        result.update(duration_days=days, borrowed_at=borrowed_at.isoformat() if borrowed_at else None,
                      due_at=due_at.isoformat() if due_at else None)
    if row["service"] == "gym" and row.get("gym_slot_id"):
        result.update(row["gym_slot_details"] or {"slot_id": row["gym_slot_id"]})
    if row.get("classroom_starts_at"):
        result.update(starts_at=row["classroom_starts_at"].isoformat(), ends_at=row["classroom_ends_at"].isoformat())
    return result


def upstream_actor(row):
    return f"web-{row['id'].hex}"


def valid_receipt(spec, row, receipt):
    remote_id = receipt.get(spec.id_field)
    return (isinstance(remote_id, str) and 1 <= len(remote_id) <= 128
            and receipt.get(spec.actor_field) == upstream_actor(row)
            and receipt.get(spec.resource_field) == row["resource_id"]
            and (not row.get("gym_slot_id") or row["gym_slot_id"] == receipt.get("slot_id"))
            and valid_classroom_period(row, receipt))


def valid_classroom_period(row, receipt):
    if not row.get("classroom_starts_at"):
        return True
    try:
        start, end = (datetime.fromisoformat(receipt[key]) for key in ("startsAt", "endsAt"))
    except (KeyError, TypeError, ValueError):
        return False
    return start == row["classroom_starts_at"] and end == row["classroom_ends_at"]


def receipt_name(spec, receipt):
    name = receipt.get(spec.name_field)
    return name[:180] if isinstance(name, str) and name.strip() and "\x00" not in name else None


def receipt_loan_window(row, receipt):
    if row["service"] != "library":
        return None
    days = row["loan_days"]
    if days is None and not LibraryLoanWindow.model_fields.keys() & receipt.keys():
        return None
    try:
        window = LibraryLoanWindow.model_validate(receipt)
    except ValidationError:
        raise GatewayUncertain from None
    if days is not None and days != window.duration_days:
        raise GatewayUncertain
    return window


def receipt_gym_slot(row, receipt):
    if not row.get("gym_slot_id"):
        return None
    try:
        return GymSlotDetails.model_validate(receipt).model_dump(mode="json")
    except ValidationError:
        raise GatewayUncertain from None


class CampusOperations:
    def __init__(self, database, credentials, gateway=None, *, access=None):
        self.db, self.credentials = database, credentials
        self.gateway = gateway or CampusGateway()
        self.access = access

    async def viewer(self, who, service):
        if self.access:
            viewer, _ = await self.access.check(who, service)
            if self.access.profiles.required:
                return viewer
        return None

    async def catalog(self, who, service, *, query=None, page_size=LIBRARY_PAGE_SIZE):
        if service == "gym" and query is not None:
            if self.access:
                await self.access.check(who, service, query.venue_id)
            return await campus_catalog(self.gateway, service, query=query)
        viewer, rules = await self.access.check(who, service) if self.access else (None, {})
        audience = viewer if self.access and self.access.profiles.required else None
        result = await campus_catalog(self.gateway, service, viewer=audience, query=query, page_size=page_size)
        if service == "classroom":
            if self.access:
                result["items"] = self.access.rooms(result["items"], viewer, rules)
            if query is not None:
                result["date"] = query.isoformat()
                await self._classroom_occupancy(result["items"], query)
        return result

    async def _classroom_occupancy(self, rooms, day):
        if not rooms:
            return
        start = datetime.combine(day, time.min, CAMPUS_TIME_ZONE)
        end = start + timedelta(days=1)
        intervals = await self.classroom_periods(rooms, start, end)
        for room in rooms:
            if intervals is None:
                room["busy_slots"] = None
                continue
            merged = []
            for lower, upper in merge_periods(intervals[room["id"]]):
                room_start = (lower - start) // timedelta(minutes=1)
                room_end = (upper - start) // timedelta(minutes=1)
                merged.append({"start_minute": room_start, "end_minute": room_end})
            room["busy_slots"] = merged

    async def classroom_periods(self, rooms, start, end):
        intervals = {room["id"]: [] for room in rooms}
        if not intervals:
            return intervals
        try:
            records, _ = await self._records("classroom", window=(start, end))
            for record in records:
                slots = intervals.get(record["roomId"])
                if slots is None:
                    continue
                left, right = record.get("startsAt"), record.get("endsAt")
                if left is None and right is None:
                    slots.append((start, end))  # Legacy reservations block all times until cancelled.
                    continue
                begins, finishes = campus_minute(left), campus_minute(right)
                if begins >= finishes:
                    raise ValueError("预约时段无效")
                lower, upper = max(begins, start), min(finishes, end)
                if lower < upper:
                    slots.append((lower, upper))
        except (GatewayRejected, GatewayUncertain, ValueError, OverflowError):
            return None
        return intervals

    async def registered_actions(self) -> frozenset[str]:
        """Read the tool registrations of the currently reachable MINTA services."""
        async def listing(path, field):
            try:
                async with asyncio.timeout(2):
                    payload = await self.gateway.request("GET", path)
                entries = payload.get(field) if isinstance(payload, dict) else None
                return entries if isinstance(entries, list) else []
            except (GatewayRejected, GatewayUncertain, TimeoutError):
                return []

        async def service_actions(service, spec):
            tools = await listing(f"/{service}/agent/tools", "tools")
            names = {tool["name"] for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
            fields = {"slot_id"} if service == "gym" else {"starts_at", "ends_at"} if service == "classroom" else set()
            if fields:
                definition = next((tool.get("parameters") for tool in tools
                                   if isinstance(tool, dict) and tool.get("name") == spec.create), None)
                properties = definition.get("properties") if isinstance(definition, dict) else None
                if not isinstance(properties, dict) or not fields <= properties.keys():
                    names.discard(spec.create)
            return {f"{service}.{action}" for action, name in (("start", spec.create), ("close", spec.close)) if name in names}

        async def managed_club_actions():
            actions = await listing("/club/managed/capabilities", "actions")
            return {f"clubs.{action}" for action in actions if isinstance(action, str)}

        groups = await asyncio.gather(*(service_actions(service, spec) for service, spec in OPERATIONS.items()),
                                      managed_club_actions())
        return frozenset().union(*groups)

    async def credential(self, user_id):
        return await self.credentials.verify("account", {"user_id": str(user_id)})

    async def _request_digest(self, data):
        exclude = set()
        if data.starts_at is None:
            exclude.update(("starts_at", "ends_at"))
        if data.duration_days is None:
            exclude.add("duration_days")
        if data.slot_id is None:
            exclude.update(("slot_id", "identity_confirmed"))
        material = data.model_dump_json(exclude=exclude)
        if data.teacher is None:
            return hashlib.sha256(material.encode()).hexdigest()
        try:
            key = await asyncio.to_thread(read_key, self.credentials.directory)
            return credential_digest(key, "classroom-request", material)
        except (OSError, ValueError):
            raise ServiceError(503, "credential_service_unavailable", "教师凭据服务暂不可用") from None

    @staticmethod
    def _check_request(row, digest):
        if row and row["request_digest"] != digest:
            raise ServiceError(409, "idempotency_conflict", "该请求编号已用于另一项操作")
        return row

    async def start(self, who, key, data: OperationInput, *, effect=None):
        needs_teacher = data.service == "classroom" and who.role == "student"
        if needs_teacher and data.teacher is None:
            raise ServiceError(422, "teacher_contact_required", "学生预约教室需要填写老师姓名和电话号码")
        if not needs_teacher and data.teacher is not None:
            raise ServiceError(422, "unexpected_teacher_contact", "当前操作无需填写老师联系信息")
        if data.service != "library" and data.duration_days is not None:
            raise ServiceError(422, "unexpected_loan_days", "借阅天数仅适用于图书馆")
        if data.service == "gym" and (not data.slot_id or not data.identity_confirmed):
            raise ServiceError(422, "gym_confirmation_required", "请先选择场地和时段并确认当前登录身份")
        if data.service != "gym" and (data.slot_id or data.identity_confirmed):
            raise ServiceError(422, "unexpected_gym_slot", "场地时段与身份确认仅适用于体育馆预约")
        period = (None, None)
        has_period = data.starts_at is not None or data.ends_at is not None
        if data.service == "classroom" and has_period:
            period = classroom_period(data.starts_at, data.ends_at)
            data = data.model_copy(update={"starts_at": period[0].isoformat(), "ends_at": period[1].isoformat()})
        elif has_period:
            raise ServiceError(422, "unexpected_classroom_period", "此起止时段仅适用于教室预约")
        if data.teacher is not None:
            name, phone = contact_values(data.teacher.name, data.teacher.phone)
            data = data.model_copy(update={"teacher": Contact(name=name, phone=phone)})
        digest = await self._request_digest(data)

        async def verify():
            if needs_teacher:
                return (await self.credentials.verify("contact", data.teacher.model_dump()))["id"]
            if data.service == "classroom":
                return (await self.credential(who.user_id))["id"]
            return None

        loan_days = (data.duration_days or DEFAULT_LOAN_DAYS) if data.service == "library" else None
        return await self._open_operation(
            who, key, digest, data.service, data.resource_id, verify, effect=effect, loan_days=loan_days,
            gym_slot_id=data.slot_id or "", classroom_period=period,
        )

    async def _open_operation(self, who, key, digest, service, resource_id, verify, *,
                              effect=None, restores=None, loan_days=None, gym_slot_id="", classroom_period=(None, None)):
        def previous(connection):
            return self._check_request(connection.execute(
                "SELECT * FROM campus_minta_operations WHERE user_id=%s AND request_key=%s", (who.user_id, key),
            ).fetchone(), digest)

        existing = await self.db.run(previous)
        if existing and (restores is None or existing["state"] != "failed"):
            return public_operation(existing)
        start, end = classroom_period
        if service == "classroom" and start is None and restores is None:
            raise ServiceError(422, "classroom_period_required", "请填写教室预约的开始和结束时间（北京时间）")
        if start is not None and start <= datetime.now(UTC):
            raise ServiceError(409, "SLOT_UNAVAILABLE", "该教室预约时段已开始，请重新选择")
        viewer = None
        if self.access:
            viewer, _ = await self.access.check(who, service, resource_id)
        credential_id = await verify()

        def claim(connection):
            if effect:
                lock_effect_run(connection, who, effect)
            if restores is not None and (existing := previous(connection)):
                row = connection.execute(
                    """UPDATE campus_minta_operations SET state='pending',error_code=NULL,loan_days=%s,updated_at=clock_timestamp()
                    WHERE id=%s AND state='failed' AND updated_at=%s RETURNING *""",
                    (loan_days, existing["id"], existing["updated_at"]),
                ).fetchone()
                return (row, True, None) if row else (previous(connection), False, None)
            row = connection.execute(
                """INSERT INTO campus_minta_operations(id,user_id,request_key,request_digest,service,resource_id,
                   teacher_credential_id,restores_operation_id,loan_days,gym_slot_id,classroom_starts_at,classroom_ends_at,state)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                   ON CONFLICT DO NOTHING RETURNING *""",
                (uuid4(), who.user_id, key, digest, service, resource_id, credential_id, restores, loan_days, gym_slot_id, start, end),
            ).fetchone()
            if row:
                effect_id = add_effect(connection, effect, "start", row["id"]) if effect else None
                return row, True, effect_id
            if existing := previous(connection):
                return existing, False, None
            raise ServiceError(409, "operation_already_open", "你已有这项业务的活跃记录，请查看我的操作")

        row, claimed, effect_id = await self.db.run(claim)
        if not claimed:
            return public_operation(row)
        spec = OPERATIONS[service]
        try:
            arguments = {
                spec.resource_arg: resource_id, spec.actor_arg: upstream_actor(row),
            }
            if service == "library":
                arguments["duration_days"] = loan_days
            if gym_slot_id:
                arguments["slot_id"] = gym_slot_id
            if start is not None:
                arguments.update(starts_at=start.isoformat(), ends_at=end.isoformat())
            if service == "club" and viewer is not None and self.access.profiles.required:
                result = await self.gateway.request("POST", "/club/managed/register", {**arguments, "viewer": academic_viewer(viewer)})
            else:
                result = await self.gateway.tool(service, spec.create, arguments)
            receipt = result if spec.receipt is None else result.get(spec.receipt)
            if not isinstance(receipt, dict) or not valid_receipt(spec, row, receipt):
                raise GatewayUncertain
            window = receipt_loan_window(row, receipt)
            slot = receipt_gym_slot(row, receipt)
        except GatewayRejected as exc:
            return public_operation(await self._transition(
                row, "failed", error=exc.code, effect_id=effect_id, effect_state="no_effect",
            ))
        except GatewayUncertain:
            return public_operation(await self._transition(row, "uncertain"))
        return public_operation(await self._transition(
            row, "active", remote=receipt[spec.id_field], name=receipt_name(spec, receipt),
            loan=window, gym_slot=slot, effect_id=effect_id, effect_state="applied",
        ))

    async def _transition(self, row, state, *, remote=None, error=None, name=None, loan=None, gym_slot=None,
                          effect_id=None, effect_state=None):
        def save(connection):
            updated = connection.execute(
                """UPDATE campus_minta_operations SET state=%s, upstream_id=COALESCE(%s,upstream_id),
                   resource_name=COALESCE(%s,resource_name),error_code=%s,
                   loan_days=COALESCE(%s,loan_days),loan_borrowed_at=COALESCE(%s,loan_borrowed_at),
                   gym_slot_details=COALESCE(%s,gym_slot_details),
                   updated_at=clock_timestamp()
                   WHERE id=%s AND state=%s AND updated_at=%s RETURNING *""",
                (state, remote, name, error, loan.duration_days if loan else None, loan.borrowed_at if loan else None,
                 Jsonb(gym_slot) if gym_slot else None,
                 row["id"], row["state"], row["updated_at"]),
            ).fetchone()
            if updated and effect_id:
                resolve_effect(connection, effect_id, effect_state)
            return updated or connection.execute("SELECT * FROM campus_minta_operations WHERE id=%s", (row["id"],)).fetchone()
        return await self.db.run(save)

    async def _records(self, service, *, check_archives=(), window=None):
        spec = OPERATIONS[service]
        path = f"/{service}{spec.records}"
        if check_archives:
            path += "?" + urlencode({"check_archives": ",".join(sorted(check_archives))})
        if window is not None:
            start, end = window
            path += "?" + urlencode({"ends_after": start.isoformat(), "starts_before": end.isoformat()})
        result = await self.gateway.request("GET", path)
        records = result.get(spec.records_key)
        if not isinstance(records, list) or any(not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field] for field in (spec.id_field, spec.resource_field, spec.actor_field)
        ) for item in records):
            raise GatewayUncertain
        purged = result.get("purged_activity_ids", []) if check_archives else []
        if not isinstance(purged, list) or any(not isinstance(item, str) or item not in check_archives for item in purged):
            raise GatewayUncertain
        return records, set(purged)

    async def list(self, who, service):
        if service not in OPERATIONS:
            raise not_found()
        rows = await self.db.run(lambda c: c.execute(
            """SELECT * FROM campus_minta_operations WHERE user_id=%s AND service=%s
               ORDER BY (state=ANY(%s)) DESC, created_at DESC LIMIT 100""", (who.user_id, service, list(ACTIVE_STATES)),
        ).fetchall())
        rows, _ = await self._refresh_rows(service, rows)
        return {"items": [public_operation(row) for row in rows]}

    async def classroom_calendar(self, who, window=None):
        time_filter = ""
        parameters = [who.user_id, list(ACTIVE_STATES)]
        if window:
            time_filter = " AND classroom_ends_at>%s AND classroom_starts_at<%s"
            parameters.extend(window)
        rows = await self.db.run(lambda c: c.execute(
            """SELECT * FROM campus_minta_operations WHERE user_id=%s AND service='classroom'
               AND classroom_starts_at IS NOT NULL AND state=ANY(%s)""" + time_filter, parameters,
        ).fetchall())
        rows, verified = await self._refresh_rows("classroom", rows, window=window)
        events = [row for row in rows if row["state"] in {"active", "closing", "close_uncertain"} and row["upstream_id"]]
        return events, verified

    async def _calendar_operations(self, who, service):
        await self.viewer(who, service)
        rows = await self.db.run(lambda c: c.execute(
            """SELECT * FROM campus_minta_operations WHERE user_id=%s AND service=%s AND state=ANY(%s)""",
            (who.user_id, service, list(ACTIVE_STATES)),
        ).fetchall())
        rows, verified = await self._refresh_rows(service, rows)
        return [row for row in rows if row["state"] in {"active", "closing", "close_uncertain"} and row["upstream_id"]], verified

    async def gym_calendar(self, who):
        rows, verified = await self._calendar_operations(who, "gym")
        return [public_operation(row) for row in rows if row["gym_slot_details"]], verified

    async def library_calendar(self, who):
        rows, verified = await self._calendar_operations(who, "library")
        return [public_operation(row) for row in rows], verified

    async def confirmed_club_registrations(self, who):
        await self.viewer(who, "club")
        rows = await self.db.run(lambda c: c.execute(
            """SELECT id, resource_id, upstream_id FROM campus_minta_operations
               WHERE user_id=%s AND service='club' AND state=ANY(%s)""",
            (who.user_id, list(ACTIVE_STATES)),
        ).fetchall())
        spec = OPERATIONS["club"]
        confirmed = []
        for offset in range(0, len(rows), CLUB_REGISTRATION_BATCH):
            batch = rows[offset:offset + CLUB_REGISTRATION_BATCH]
            by_actor = {upstream_actor(row): row for row in batch}
            reply = await self.gateway.request("POST", "/club/managed/registrations", {"participants": list(by_actor)})
            records = reply.get(spec.records_key)
            if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
                raise GatewayUncertain
            for record in records:
                actor = record.get(spec.actor_field)
                row = by_actor.get(actor) if isinstance(actor, str) else None
                if row and valid_receipt(spec, row, record) and row["upstream_id"] in (None, record[spec.id_field]):
                    confirmed.append(record)
        return confirmed

    async def reconcile_operation(self, who, operation_id):
        row = await self.db.run(lambda c: c.execute(
            "SELECT * FROM campus_minta_operations WHERE id=%s AND user_id=%s", (operation_id, who.user_id),
        ).fetchone())
        if not row:
            raise not_found()
        rows, _ = await self._refresh_rows(row["service"], [row])
        return public_operation(rows[0])

    async def _refresh_rows(self, service, rows, *, window=None):
        stale = datetime.now(UTC) - STALE_AFTER
        to_check = [row for row in rows if row["state"] in {"active", "uncertain", "close_uncertain"}
                    or (row["state"] in {"pending", "closing"} and row["updated_at"] < stale)]
        if to_check:
            try:
                spec = OPERATIONS[service]
                check_archives = {row["resource_id"] for row in to_check
                                  if service == "club" and row["state"] in {"pending", "uncertain"}}
                records, purged = await self._records(service, check_archives=check_archives, window=window)
                by_actor = {}
                for item in records:
                    by_actor.setdefault(item[spec.actor_field], []).append(item)
                replacements = {}
                for row in to_check:
                    match = next((item for item in by_actor.get(upstream_actor(row), ()) if valid_receipt(spec, row, item)
                                  and row["upstream_id"] in (None, item[spec.id_field])), None)
                    unconfirmed = row["state"] in {"pending", "uncertain"}
                    needs_period = service == "library" and row["state"] == "active" and not row["loan_borrowed_at"]
                    if match and (unconfirmed or needs_period):
                        try:
                            window = receipt_loan_window(row, match)
                            slot = receipt_gym_slot(row, match)
                        except GatewayUncertain:
                            if row["state"] == "pending":
                                replacements[row["id"]] = await self._transition(row, "uncertain")
                            continue
                        if unconfirmed or window:
                            replacements[row["id"]] = await self._transition(
                                row, "active", remote=match[spec.id_field], name=receipt_name(spec, match),
                                loan=window, gym_slot=slot,
                            )
                    elif not match and (row["resource_id"] in purged or row["state"] in {"active", "closing", "close_uncertain"}):
                        replacements[row["id"]] = await self._transition(row, "closed")
                    elif row["state"] == "pending":
                        replacements[row["id"]] = await self._transition(row, "uncertain")
                    elif row["state"] == "closing":
                        replacements[row["id"]] = await self._transition(row, "close_uncertain")
                rows = [replacements.get(row["id"], row) for row in rows]
            except (GatewayRejected, GatewayUncertain):
                return rows, False
        return rows, True

    async def close(self, who, operation_id, *, effect=None):
        def claim(connection):
            if effect:
                lock_effect_run(connection, who, effect)
            row = connection.execute("SELECT * FROM campus_minta_operations WHERE id=%s AND user_id=%s FOR UPDATE",
                                     (operation_id, who.user_id)).fetchone()
            if not row:
                raise not_found()
            if row["state"] in {"closed", "closing"}:
                return row, False, None
            if row["state"] not in {"active", "close_uncertain"}:
                raise ServiceError(409, "operation_not_active", "该记录尚未确认成功，暂时无法取消或归还")
            effect_id = add_effect(connection, effect, "close", row["id"]) if effect else None
            return connection.execute(
                "UPDATE campus_minta_operations SET state='closing',updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                (operation_id,),
            ).fetchone(), True, effect_id

        row, claimed, effect_id = await self.db.run(claim)
        if not claimed:
            return public_operation(row)
        spec = OPERATIONS[row["service"]]
        try:
            records, _ = await self._records(row["service"])
            owned = any(valid_receipt(spec, row, item) and item[spec.id_field] == row["upstream_id"] for item in records)
            if owned:
                arguments = {spec.close_arg: row["upstream_id"]}
                if row["service"] == "club" and (self.access is None or self.access.profiles.required):
                    response = await self.gateway.request("POST", "/club/managed/cancel", {**arguments, "participant_name": upstream_actor(row)})
                else:
                    response = await self.gateway.tool(row["service"], spec.close, arguments)
                receipt = response.get(spec.receipt or "reservation")
                if (not isinstance(receipt, dict) or not valid_receipt(spec, row, receipt)
                        or receipt[spec.id_field] != row["upstream_id"]):
                    raise GatewayUncertain
        except GatewayRejected as exc:
            return public_operation(await self._transition(
                row, "close_uncertain", error=exc.code, effect_id=effect_id, effect_state="no_effect",
            ))
        except GatewayUncertain:
            return public_operation(await self._transition(row, "close_uncertain"))
        return public_operation(await self._transition(
            row, "closed", effect_id=effect_id, effect_state="applied" if owned else "no_effect",
        ))

    async def undo_started(self, who, operation_id):
        rows = await self.db.run(lambda c: c.execute(
            """WITH RECURSIVE related AS (
                   SELECT * FROM campus_minta_operations WHERE id=%s AND user_id=%s
                   UNION ALL
                   SELECT o.* FROM campus_minta_operations o JOIN related r ON o.restores_operation_id=r.id
                   WHERE o.user_id=%s
               ) SELECT * FROM related ORDER BY created_at,id""",
            (operation_id, who.user_id, who.user_id),
        ).fetchall())
        if not rows:
            raise not_found()
        # A previously withdrawn return may have restored this loan or reservation under a new upstream ID.
        current = next((row for row in rows if row["state"] in ACTIVE_STATES), rows[0])
        result = public_operation(current)
        if result["state"] in {"pending", "uncertain"}:
            result = await self.reconcile_operation(who, current["id"])
        if result["state"] in {"active", "close_uncertain"}:
            result = await self.close(who, current["id"])
        if result["state"] in {"closing", "close_uncertain"}:
            result = await self.reconcile_operation(who, current["id"])
        return result

    async def restore_closed(self, who, operation_id, effect_id):
        original = await self.db.run(lambda c: c.execute(
            "SELECT * FROM campus_minta_operations WHERE id=%s AND user_id=%s AND state='closed'",
            (operation_id, who.user_id),
        ).fetchone())
        if not original:
            raise ServiceError(409, "operation_restore_conflict", "原校务记录已变化，暂时无法恢复。")
        undone = await self.db.run(lambda c: c.execute(
            """WITH RECURSIVE related AS (
                   SELECT id,restores_operation_id FROM campus_minta_operations WHERE id=%s AND user_id=%s
                   UNION ALL
                   SELECT o.id,o.restores_operation_id FROM campus_minta_operations o
                   JOIN related r ON o.id=r.restores_operation_id WHERE o.user_id=%s
               ) SELECT 1 FROM related r JOIN chat_turn_effects e ON e.operation_id=r.id
               WHERE e.kind='start' AND e.state='undone' LIMIT 1""",
            (operation_id, who.user_id, who.user_id),
        ).fetchone())
        if undone:
            raise ServiceError(409, "operation_restore_conflict", "原办理操作已撤回，无法重新恢复这条校务记录。")

        async def verify():
            if original["service"] != "classroom":
                return None
            credential_id = str(original["teacher_credential_id"] or "")
            if not credential_id:
                raise ServiceError(409, "credential_required", "原预约缺少有效的教师凭据。")
            if who.role == "teacher":
                credential = await self.credential(who.user_id)
            else:
                credential = await self.credentials.verify("credential", {"credential_id": credential_id})
            if credential["id"] != credential_id:
                raise ServiceError(409, "credential_changed", "原预约的教师凭据已更新，请在校园应用重新预约。")
            return credential_id

        key = f"undo-{effect_id.hex}"
        digest = hashlib.sha256(f"restore:{operation_id}".encode()).hexdigest()
        loan_days = (original["loan_days"] or DEFAULT_LOAN_DAYS) if original["service"] == "library" else None
        result = await self._open_operation(
            who, key, digest, original["service"], original["resource_id"], verify, restores=operation_id, loan_days=loan_days,
            gym_slot_id=original["gym_slot_id"], classroom_period=(original["classroom_starts_at"], original["classroom_ends_at"]),
        )
        if result["state"] in {"pending", "uncertain"}:
            return await self.reconcile_operation(who, result["id"])
        return result


class CatalogItem(BaseModel):
    model_config = ConfigDict(strict=True)
    id: ResourceId


class GymVenue(CatalogItem):
    name: str
    capacity: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]
    court_count: Annotated[int, Field(ge=1)] | None = None


class ClassroomRoom(CatalogItem):
    id: ResourceId = Field(validation_alias="roomId")
    name: str
    capacity: Annotated[int, Field(ge=0)]
    reserved: bool


class ClubActivity(CatalogItem):
    name: str
    description: str
    location: str
    starts_at: ActivityTimestamp
    ends_at: ActivityTimestamp
    capacity: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]
    points: Annotated[int, Field(ge=1, le=4)] | None = None
    club_id: str | None = None

    @model_validator(mode="after")
    def ordered_times(self):
        validate_activity_range(self.starts_at, self.ends_at)
        return self


CATALOGS = {
    "classroom": ("rooms", "/classroom/api/availability", ClassroomRoom),
    "gym": ("venues", "/gym/api/venues", GymVenue),
    "library": ("books", "/library/api/books", None),
    "club": ("activities", None, ClubActivity),
}


def academic_viewer(viewer):
    return {key: viewer[key] for key in ("reviewed", "grade", "college")}


async def campus_catalog(gateway, service, *, viewer=None, query=None, page_size=LIBRARY_PAGE_SIZE):
    if service not in CATALOGS:
        raise not_found()
    name, path, model = CATALOGS[service]
    try:
        if service == "gym" and query is not None:
            path = "/gym/api/slots?" + urlencode(query.model_dump(mode="json"))
            slots = (await gateway.request("GET", path))["slots"]
            if not isinstance(slots, list):
                raise ValueError
            return {"source": "minta_simulation", "items": [GymSlot.model_validate(slot).model_dump(mode="json") for slot in slots]}
        if path:
            payload = await gateway.request("GET", path)
        elif viewer is None:
            payload = await gateway.tool("club", "club_list_activities", {})
        else:
            payload = await gateway.request("POST", "/club/managed/activities", academic_viewer(viewer))
        if not isinstance(payload.get(name), list):
            raise ValueError
        if service == "library":
            return {"source": "minta_simulation", **library_catalog(payload[name], query, page_size=page_size)}
        items = [model.model_validate(item).model_dump(mode="json") for item in payload[name]]
        result = {"source": "minta_simulation", "items": items}
        if service == "gym" and "booking_window" in payload:
            result["booking_window"] = GymBookingWindow.model_validate(payload["booking_window"]).model_dump(mode="json")
        return result
    except GatewayRejected as exc:
        raise exc.as_service_error() from None
    except (GatewayUncertain, ValueError, TypeError, KeyError, ValidationError):
        raise ServiceError(502, "minta_unavailable", "暂时无法读取校园服务状态") from None
