"""Account-scoped chat adapter for the same operations exposed in campus apps."""

import asyncio
import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime
from time import monotonic

from ..campus_cluster.action_plan import CAMPUS_ACTION_CONFIRMATION, CAMPUS_TIME_ZONE, CampusActionPlan
from ..campus_cluster.activity_time import campus_minute
from ..campus_cluster.agents.campus_services import CampusServicesAgent
from ..campus_cluster.capabilities import scope_actions
from ..campus_cluster.contracts import Status
from ..campus_cluster.library_catalog import (
    DEFAULT_LOAN_DAYS, LIBRARY_TOOL_PAGE_SIZE, MAX_LOAN_DAYS, MIN_LOAN_DAYS, LibraryCatalogQuery,
    matches_search, page_slice, search_terms,
)
from ..campus_cluster.subskills.minta_campus import (
    CAMPUS_ACTION, CAMPUS_STATUS, CLASSROOM_CONFIRMATION, CLASSROOM_END_FIELD, CLASSROOM_PENDING, CLASSROOM_START_FIELD,
    CampusIntent, GymSlotsQuery, LibraryStatusQuery, ROOM_FIELD, TEACHER_NAME_FIELD, TEACHER_PHONE_FIELD, name_key,
)
from ..campus_cluster.subskills.campus_street_view import CAMPUS_STREET_VIEW
from ..campus_cluster.subskills.campus_timetable import CAMPUS_TIMETABLE
from ..campus_cluster.subskills.minta_gateway import MAX_OUTPUT_CHARS, MINTA_GATEWAY
from ..reliability.models import ToolResult
from .auth import Principal
from .campus_profiles import GRADES
from .campus_timetable import CampusTimetable
from .campus_planning import pending_campus_action
from .chat_effects import ChatEffect, add_effect, lock_effect_run, resolve_effect
from .errors import ServiceError
from .minta_clubs import ClubCommand, change_club, list_clubs
from .minta_operations import GatewayRejected, GatewayUncertain, OperationInput, classroom_period
from .minta_schedule import read_term_schedule
from .roles import UserRole


OPEN_RECORD = {"active", "close_uncertain", "closing", "pending", "uncertain"}
LABELS = {"classroom": ("预约", "取消预约"), "gym": ("预约", "取消预约"),
          "library": ("借阅", "归还"), "club": ("报名", "取消报名")}
CLUB_LABELS = {"register": "登记社团", "update": "更新社团资料", "archive": "归档社团",
               "publish_activity": "发布活动", "update_activity": "更新活动", "archive_activity": "归档活动",
               "restore": "恢复归档社团", "restore_activity": "恢复归档活动"}
CLUB_UNDO = {"register": "撤销社团登记", "update": "恢复社团原资料", "archive": "恢复社团",
             "publish_activity": "撤销活动发布", "update_activity": "恢复活动原资料", "archive_activity": "恢复活动",
             "restore": "重新归档社团", "restore_activity": "重新归档活动"}
CLUB_FIELDS = ("name", "category", "description")
ACTIVITY_FIELDS = ("name", "description", "location", "starts_at", "ends_at", "capacity")
ACTION_REGISTRATIONS = {action.id: action for action in CAMPUS_ACTION.actions}
REGISTRATION_TTL = 5.0
CHINESE_LOAN_DAYS = {label: day for day, label in enumerate(
    ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四"), 1,
)}
CHINESE_LOAN_DAYS["两"] = 2


def library_duration(value):
    if value is None:
        return DEFAULT_LOAN_DAYS
    number = unicodedata.normalize("NFKC", value).strip().removesuffix("天").strip()
    days = int(number) if re.fullmatch(r"[0-9]{1,2}", number) else CHINESE_LOAN_DAYS.get(number, 0)
    if not MIN_LOAN_DAYS <= days <= MAX_LOAN_DAYS:
        raise ServiceError(422, "invalid_loan_days", "借期可选 1 到 14 天，请提供整天数。")
    return days


def gym_matches_slot(fields, slot_id):
    if not fields:
        return True
    if not slot_id:
        return False
    day, court, time = slot_id.split("_")
    return all({"date": day, "court_id": court, "start_time": time[:2] + ":" + time[2:]}[key] == value
               for key, value in fields.items())


def gym_slot_label(item):
    if not item.get("starts_at"):
        return ""
    start, end = (datetime.fromisoformat(item[key]).astimezone(CAMPUS_TIME_ZONE) for key in ("starts_at", "ends_at"))
    return f"{item['court_name']} · {start:%Y-%m-%d %H:%M}–{end:%H:%M}（北京时间）"


class CampusConfirmation(ServiceError):
    def __init__(self, message: str, plan: CampusActionPlan, code=CAMPUS_ACTION_CONFIRMATION):
        super().__init__(422, code, message)
        self.plan = plan


def classroom_slot_label(start, end):
    finish = f"{end:%H:%M}" if end.date() == start.date() else f"{end:%m月%d日 %H:%M}"
    return f"{start:%m月%d日 %H:%M}–{finish}"


def match_names(item):
    label = item.get("name") or item.get("title") or item.get("resource_name") or ""
    item_id = item.get("resource_id") or item["id"]
    keys = {name_key(item_id), name_key(label)}
    if item.get("id"):
        keys.add(name_key(item["id"]))
    if item_id.startswith("room-"):
        keys.add(name_key(item_id.removeprefix("room-")))
        keys.add(name_key(label.removesuffix("教室")))
    return keys - {""}


def select_one(matches, fallback):
    if len(matches) != 1:
        message = "有多个同名项目，请提供具体名称或项目编号。" if matches else fallback
        raise ServiceError(422, "minta_target_required", message)
    return matches[0]


def reserve_target(value):
    value = value.strip(" ")
    if value.startswith("体育馆") and value != "体育馆":
        return ("gym",), value.removeprefix("体育馆").strip(" :的")
    if value.startswith("教室") and value != "教室":
        return ("classroom",), value.removeprefix("教室").strip(" :的")
    if value.endswith("教室"):
        return ("classroom",), value.removesuffix("教室").strip()
    return ("classroom", "gym"), value


def details(allowed, updates, current=None):
    if updates.keys() - set(allowed):
        raise ValueError
    fields = {name: current[name] for name in allowed} if current is not None else {}
    fields.update(updates)
    if "capacity" in fields:
        fields["capacity"] = int(fields["capacity"])
    return fields


def club_details(updates, current=None):
    scope = {key: updates.pop(key) for key in ("allow_grades", "allow_colleges") if key in updates}
    fields = details(CLUB_FIELDS, updates, current)
    if scope:
        audience = dict(current.get("audience", {})) if current else {}
        for key, raw in scope.items():
            if raw == "不限":
                audience[key] = []
            else:
                audience[key] = [part.strip() for part in re.split(r"[,、]", raw) if part.strip()]
                if not audience[key]:
                    raise ValueError
        labels = {name: grade for grade, name in GRADES.items()}
        if "allow_grades" in scope:
            audience["allow_grades"] = [labels.get(value, value) for value in audience["allow_grades"]]
        fields["audience"] = audience
    return fields


class CampusChat:
    def __init__(self, operations, auth):
        self.ops = operations
        self.auth = auth
        self.timetable = CampusTimetable(operations)
        self._registrations = frozenset()
        self._registrations_until = 0.0
        self._registration_lock = asyncio.Lock()

    async def registered_actions(self):
        async with self._registration_lock:
            if monotonic() >= self._registrations_until:
                self._registrations = await self.ops.registered_actions()
                self._registrations_until = monotonic() + REGISTRATION_TTL
        return self._registrations

    async def discover(self, context, agents):
        try:
            who = await self.principal(context)
        except ServiceError:
            return scope_actions(agents, None, {})
        live = await self.registered_actions()
        return scope_actions(agents, who.role.value, {CAMPUS_ACTION.name: live & ACTION_REGISTRATIONS.keys()})

    @staticmethod
    def _access(who, service, action):
        registration = ACTION_REGISTRATIONS.get(f"{service}.{action}")
        access = registration.for_role(who.role.value) if registration else None
        if access is None:
            raise ServiceError(403, "minta_action_forbidden", "当前账号无权办理这项校园业务。")
        return registration, access

    @staticmethod
    def _classroom_details(intent, registration, access, *, live):
        if intent.action == "close":
            return None if intent.target else "请告诉我要取消的预约编号或教室。"
        supplied = {ROOM_FIELD: intent.target, TEACHER_NAME_FIELD: intent.fields.get("name"),
                    TEACHER_PHONE_FIELD: intent.fields.get("phone"),
                    CLASSROOM_START_FIELD: intent.fields.get("starts_at"), CLASSROOM_END_FIELD: intent.fields.get("ends_at")}
        missing = [field for field in access.required_fields if not supplied.get(field)]
        if not missing:
            return None
        examples = {ROOM_FIELD: "A101", TEACHER_NAME_FIELD: "老师姓名：…", TEACHER_PHONE_FIELD: "电话：…",
                    CLASSROOM_START_FIELD: "开始时间：明天 14:00", CLASSROOM_END_FIELD: "结束时间：明天 16:00"}
        example = "，".join(examples[field] for field in missing)
        availability = f"校园服务支持{registration.label}。" if live else f"{registration.label}服务暂未就绪。"
        return f"{availability}请直接在这里回复{'、'.join(missing)}。例如「{example}」。"

    async def principal(self, context):
        row = await self.ops.db.run(lambda c: c.execute(
            """SELECT s.id AS session_id,u.role FROM runs r JOIN conversations v ON v.id=r.conversation_id
            JOIN agent_instances i ON i.id=v.instance_id JOIN users u ON u.id=v.user_id
            JOIN auth_sessions s ON s.id=r.initiator_session_id AND s.user_id=u.id
            WHERE r.id=%s AND r.worker_id=%s AND r.conversation_id=%s AND i.id=%s AND u.id=%s
            AND r.status='running' AND r.lease_expires_at>now() AND r.cancel_requested_at IS NULL
            AND i.status='active' AND u.is_active AND s.revoked_at IS NULL AND s.expires_at>now()
            AND (%s=FALSE OR u.verified_at IS NOT NULL)""",
            (context.run_id, context.worker_id, context.conversation_id, context.instance_id, context.user_id,
             self.auth.settings.require_verified_email),
        ).fetchone())
        if not row:
            raise ServiceError(401, "minta_login_required", "请在当前登录账号下重新发起校务请求。")
        who = Principal(context.user_id, row["session_id"], UserRole(row["role"]))
        if self.ops.access:
            await self.ops.access.profiles.require_ready(who)
        return who

    def read_handlers(self, context):
        async def gateway(skill_context, args):
            try:
                who = await self.principal(context)
                if args.action == "list_tools":
                    await self.ops.viewer(who, args.service)
                    return await MINTA_GATEWAY.handler(skill_context, args)
                if args.service == "schedule":
                    week = args.arguments.get("week", 1)
                    if type(week) is not int or not 1 <= week <= 20:
                        raise ServiceError(422, "invalid_week", "周次必须是 1 到 20 的整数")
                    result = await read_term_schedule(who, self.ops.gateway, self.ops.access, week=week)
                elif args.service == "library":
                    result = await self.ops.catalog(who, "library", query=LibraryCatalogQuery.model_validate(args.arguments),
                                                    page_size=LIBRARY_TOOL_PAGE_SIZE)
                else:
                    result = await self.ops.catalog(who, args.service)
                content = json.dumps({"service": args.service, **result}, ensure_ascii=False)
                if len(content) > MAX_OUTPUT_CHARS:
                    return ToolResult("校园服务结果超过大小限制。", "error")
                return ToolResult(content, "success")
            except ServiceError as exc:
                return ToolResult(exc.message, "error")
            except (GatewayRejected, GatewayUncertain):
                return ToolResult("暂时无法读取该校园服务，请检查配置后重试。", "error")

        async def street_view(skill_context, args):
            try:
                await self.ops.viewer(await self.principal(context), "map")
                return await CAMPUS_STREET_VIEW.handler(skill_context, args)
            except ServiceError as exc:
                return ToolResult(exc.message, "error")

        async def timetable(_skill_context, args):
            try:
                result = await self.timetable.read(await self.principal(context), args)
                content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(content) <= MAX_OUTPUT_CHARS:
                    return ToolResult(content, "success")
                return ToolResult("时段表结果较多，请缩小查询范围。", "error")
            except ServiceError as exc:
                return ToolResult(exc.message, "error")

        return {MINTA_GATEWAY.name: gateway, CAMPUS_STREET_VIEW.name: street_view, CAMPUS_STATUS.name: self.reader(context),
                CAMPUS_TIMETABLE.name: timetable}

    async def library_status(self, who, query):
        async def catalog():
            if query.view == "my_loans":
                return {}
            return await self.ops.catalog(who, "library", query=query, page_size=LIBRARY_TOOL_PAGE_SIZE)

        async def loans():
            if query.view == "catalog":
                return {}
            records = (await self.ops.list(who, "library"))["items"]
            terms = search_terms(query.q)
            matching = [record for record in records if matches_search(
                terms, record["resource_name"] or "", record["resource_id"],
            )]
            items, page = page_slice(matching, query.page, LIBRARY_TOOL_PAGE_SIZE)
            return {"my_operations": items, "loans_page": page, "loans_total": len(matching),
                    "loans_page_size": LIBRARY_TOOL_PAGE_SIZE}

        result, personal = await asyncio.gather(catalog(), loans())
        return {**result, **personal}

    def reader(self, context):
        async def read(_skill_context, args):
            try:
                who = await self.principal(context)
                if args.service == "clubs":
                    result = await list_clubs(self.ops.gateway, who.user_id, await self.ops.viewer(who, "club"))
                elif args.service == "library":
                    result = await self.library_status(who, args.library or LibraryStatusQuery())
                elif args.service == "gym" and args.gym is not None:
                    catalog = await self.ops.catalog(who, "gym", query=args.gym)
                    result = {"venue_id": args.gym.venue_id, "date": args.gym.date, "items": [
                        {"court_id": item["court_id"], "court_name": item["court_name"],
                         "start": datetime.fromisoformat(item["starts_at"]).astimezone(CAMPUS_TIME_ZONE).strftime("%H:%M"),
                         "end": datetime.fromisoformat(item["ends_at"]).astimezone(CAMPUS_TIME_ZONE).strftime("%H:%M"),
                         "remaining": item["remaining"]}
                        for item in catalog["items"]
                    ]}
                else:
                    catalog, records = await asyncio.gather(
                        self.ops.catalog(who, args.service), self.ops.list(who, args.service),
                    )
                    result = {"items": catalog["items"], "my_operations": records["items"]}
                    if args.service == "gym":
                        result.update(booking_window=catalog.get("booking_window"), items=[
                            {key: item[key] for key in ("id", "name", "court_count")} for item in catalog["items"]
                        ])
                content = json.dumps({"source": "minta_simulation", **result}, ensure_ascii=False)
                if len(content) > MAX_OUTPUT_CHARS:
                    return ToolResult("结果较多，请缩小查询范围。", "error")
                return ToolResult(content, "success")
            except ServiceError as exc:
                return ToolResult(exc.message, "error")
        return read

    async def run_action(self, context, plan: CampusActionPlan, agent: CampusServicesAgent, *, confirm_first=False):
        async def action(_skill_context, args):
            reason, pending = None, None
            try:
                who = await self.principal(context)
                intent = CampusIntent.from_plan(args.plan)
                if intent.service is not None:
                    registration, _ = self._access(who, intent.service, intent.action)
                    if intent.fields.keys() - registration.arguments.keys():
                        raise ServiceError(422, "minta_invalid_fields", "请核对办理事项及其所需信息。")
                elif intent.action not in {"start", "close"} or intent.fields:
                    raise ServiceError(422, "minta_invalid_fields", "请写明办理对象和所需资料。")
                if intent.service == "clubs":
                    content, status = await self.manage(context, who, intent, confirm_first=confirm_first)
                else:
                    if intent.service == "gym" and intent.action == "start" and not confirm_first:
                        if pending_campus_action(context.history) != args.plan:
                            raise ServiceError(422, "gym_confirmation_required", "请先核对场馆、日期、时段、场地号和预约人。")
                    content, status = await self.operate(context, who, intent, confirm_first=confirm_first)
            except ServiceError as exc:
                if exc.code in {CLASSROOM_PENDING, CLASSROOM_CONFIRMATION, CAMPUS_ACTION_CONFIRMATION}:
                    reason = exc.code
                if isinstance(exc, CampusConfirmation):
                    pending = exc.plan.model_dump()
                if exc.code == "club_write_unconfirmed":
                    status = Status.PARTIAL
                else:
                    status = Status.NEEDS_INPUT if exc.status in {401, 403, 404, 409, 422} else Status.FAILED
                content = exc.message
            except ValueError:
                status = Status.NEEDS_INPUT
                content = "请写明办理对象和所需资料，也可以在校园应用里填写。"
            return json.dumps({"status": status.value, "message": content, "reason": reason, "pending_plan": pending},
                              ensure_ascii=False)

        return await agent.execute_action(str(context.conversation_id), plan, action)

    @staticmethod
    def _confirm_action(registration, target, fields, pending):
        details = "，".join(f"{registration.arguments[key]}为「{value}」" for key, value in fields.items())
        text = f"请确认{registration.label}「{target}」。"
        if details:
            text += details + "。"
        return CampusConfirmation(text + "回复「确认」或「确认办理」提交。", pending)

    async def matching(self, who, service, target, action):
        try:
            catalog = (await self.ops.catalog(who, service))["items"]
        except ServiceError as exc:
            if action != "close" or exc.code != "campus_access_denied":
                raise
            catalog = []
        if action == "start":
            return [(service, item) for item in catalog if target in match_names(item)]
        resource_ids = {item["id"] for item in catalog if target in match_names(item)}
        records = (await self.ops.list(who, service))["items"]
        return [(service, record) for record in records if record["state"] in OPEN_RECORD
                and (record["resource_id"] in resource_ids or target in match_names(record))]

    async def operate(self, context, who, intent, *, confirm_first=False):
        live = await self.registered_actions()
        if intent.service == "gym" and intent.action == "start" and not intent.target:
            raise ServiceError(422, "gym_venue_required",
                               "想预约哪个场馆？可以选大学生活动中心、室内篮球场或室内羽毛球场等。")
        if intent.service == "classroom":
            registration, access = self._access(who, intent.service, intent.action)
            if clarification := self._classroom_details(intent, registration, access, live=registration.id in live):
                code = CLASSROOM_PENDING if intent.action == "start" else "classroom_target_required"
                raise ServiceError(422, code, clarification)
            if context.attempt > 1 and not confirm_first:
                if not context.previous_answer:
                    return "这轮的校务回执暂未确认。可在校园应用核对当前状态。", Status.NEEDS_INPUT
                try:
                    result = Status(context.previous_result)
                except ValueError:
                    result = Status.PARTIAL
                return context.previous_answer, result
        services, target = (intent.service,), intent.target
        if intent.service is None:
            services, target = reserve_target(target)
        services = tuple(service for service in services if f"{service}.{intent.action}" in live)
        if not services:
            raise ServiceError(503, "minta_action_unavailable", "这项校园办理暂时无法提交，请稍后再试。")
        for service in services:
            self._access(who, service, intent.action)
        choices = await asyncio.gather(*(self.matching(who, service, name_key(target), intent.action) for service in services))
        empty = "没有找到匹配的个人记录。" if intent.action == "close" else "没有找到这个项目，请提供完整名称。"
        if intent.action == "start" and services == ("classroom",):
            empty = "没有找到对应的教室，请核对教室编号或名称。"
        matches = [match for group in choices for match in group]
        if intent.service == "gym" and intent.action == "close":
            matches = [match for match in matches if gym_matches_slot(intent.fields, match[1].get("slot_id"))]
            if len(matches) > 1:
                choices = "；".join(gym_slot_label(item) for _, item in matches[:4] if item.get("starts_at"))
                raise ServiceError(422, "gym_reservation_required", f"你有多条预约，请指定要取消的日期、时段和场地号。{choices}")
        if intent.action == "close" and (classrooms := [item for service, item in matches if service == "classroom"]):
            if value := intent.fields.get("starts_at"):
                start = campus_minute(value)
                matches = [(service, item) for service, item in matches if service == "classroom"
                           and item.get("starts_at") and campus_minute(item["starts_at"]) == start]
            elif len(classrooms) > 1:
                raise ServiceError(422, "classroom_reservation_required", "你有多条教室预约，请告诉我要取消哪天几点的预约。")
        service, item = select_one(matches, empty)
        who = await self.principal(context)
        registration, access = self._access(who, service, intent.action)
        if service == "classroom" and intent.service is None and intent.action == "start":
            intent = replace(intent, service=service, target=item["id"])
            if clarification := self._classroom_details(intent, registration, access, live=True):
                raise ServiceError(422, CLASSROOM_PENDING, clarification)
        loan_days = library_duration(intent.fields.get("duration_days")) if registration.id == "library.start" else None
        classroom_times = None
        classroom_label = ""
        if registration.id == "classroom.start":
            if (contact_fields := intent.fields.keys() & {"name", "phone"}) and (
                who.role != "student" or contact_fields != {"name", "phone"}
            ):
                raise ServiceError(422, "unexpected_teacher_contact", "请按当前身份提供所需的教师联系信息。")
            try:
                classroom_times = classroom_period(intent.fields.get("starts_at"), intent.fields.get("ends_at"))
            except ServiceError as exc:
                raise ServiceError(422, CLASSROOM_PENDING, exc.message) from None
            if classroom_times[0] <= datetime.now(CAMPUS_TIME_ZONE):
                raise ServiceError(422, CLASSROOM_PENDING, "该预约时段已开始，请重新提供开始和结束时间。")
            classroom_label = classroom_slot_label(*classroom_times)
        elif registration.id == "classroom.close" and item.get("starts_at"):
            classroom_label = classroom_slot_label(campus_minute(item["starts_at"]), campus_minute(item["ends_at"]))
        gym_slot_id = None
        gym_label = gym_slot_label(item) if service == "gym" else ""
        if registration.id == "gym.start":
            gym_slot_id, gym_label = await self.prepare_gym(who, item, intent.fields)
            if confirm_first:
                account = await self.ops.access.profiles.account(who) if self.ops.access else {}
                role = "教师" if who.role == "teacher" else "学生"
                name = account.get("full_name") or account.get("display_name") or ""
                identity = f"{name}（{role}）" if name else f"当前登录的{role}账号"
                pending = CampusActionPlan(action=registration.id, target=item["id"], arguments=intent.fields)
                raise CampusConfirmation(
                    f"预约人：{identity}。请核对「{item['name']} · {gym_label}」。回复「确认」或「确认预约」提交。",
                    pending,
                )
        if confirm_first:
            target_name = item.get("resource_name") or item.get("title") or item.get("name") or intent.target
            if registration.id == "classroom.start":
                contact = ""
                if TEACHER_NAME_FIELD in access.required_fields:
                    tail = re.sub(r"\D", "", intent.fields["phone"])[-4:]
                    contact = f"，老师姓名为「{intent.fields['name']}」，电话尾号 {tail}"
                pending = CampusActionPlan(action=registration.id, target=item["id"])
                raise CampusConfirmation(
                    f"你要预约「{target_name} · {classroom_label}（北京时间）」{contact}。请回复「确认」或「确认预约」提交。",
                    pending, CLASSROOM_CONFIRMATION,
                )
            if loan_days is not None:
                pending = CampusActionPlan(action=registration.id, target=item["id"],
                                           arguments={"duration_days": str(loan_days)})
                raise CampusConfirmation(
                    f"请确认借阅「{target_name}」，借期 {loan_days} 天。回复「确认」或「确认办理」提交。", pending,
                )
            if registration.id == "gym.close":
                pending = CampusActionPlan(action=registration.id, target=item["id"])
                raise CampusConfirmation(
                    f"请确认取消「{target_name}{' · ' + gym_label if gym_label else ''}」。回复「确认」或「确认办理」提交。",
                    pending,
                )
            if registration.id == "classroom.close":
                pending = CampusActionPlan(action=registration.id, target=item["id"])
                label = f"{target_name} · {classroom_label}" if classroom_label else target_name
                raise CampusConfirmation(f"请确认取消「{label}」。回复「确认」或「确认办理」提交。", pending)
            pending = CampusActionPlan(action=registration.id, target=item["id"], arguments=intent.fields)
            raise self._confirm_action(registration, target_name, intent.fields, pending)
        await self.auth.rate_limit("minta.operation", str(who.user_id), 30, 60)
        begin, finish = LABELS[service]
        if intent.action == "start":
            fields = {"service": service, "resource_id": item["id"]}
            if loan_days is not None:
                fields["duration_days"] = loan_days
            elif gym_slot_id is not None:
                fields.update(slot_id=gym_slot_id, identity_confirmed=True)
            elif classroom_times:
                fields.update(starts_at=classroom_times[0].isoformat(), ends_at=classroom_times[1].isoformat())
                if who.role == "student":
                    fields["teacher"] = {key: intent.fields[key] for key in ("name", "phone")}
                    await self.auth.rate_limit("minta.teacher_contact.user", str(who.user_id), 8, 600)
            verb, label = begin, item.get("title") or item["name"]
            if slot_label := gym_label or classroom_label:
                label += f" · {slot_label}"
            effect = ChatEffect(context, f"{finish}「{label}」")
            result = await self.ops.start(
                who, f"chat-{context.run_id.hex}", OperationInput.model_validate(fields), effect=effect,
            )
        else:
            verb, label = finish, item["resource_name"] or intent.target.strip("《》")
            if slot_label := gym_label or classroom_label:
                label += f" · {slot_label}"
            result = await self.ops.close(who, item["id"], effect=ChatEffect(context, f"重新{begin}「{label}」"))
        if result["state"] == ("active" if intent.action == "start" else "closed"):
            term = f"，借期 {result['duration_days']} 天" if loan_days is not None else ""
            schedule = "该时段已加入个人日程。" if classroom_times else "可在校园应用查看记录。"
            return f"已为你{verb}「{label}」{term}。{schedule}", Status.COMPLETED
        if result["state"] == "failed":
            return result["message"] or "这次未能完成，请查看当前状态。", Status.NEEDS_INPUT
        return f"「{label}」的{verb}结果待确认，请在校园应用刷新查看。", Status.PARTIAL

    async def prepare_gym(self, who, item, fields):
        labels = {"date": "日期", "start_time": "开始时间", "court_id": "场地号"}
        missing = [label for key, label in labels.items() if not fields.get(key)]
        day, clock, court = (fields.get(key, "") for key in labels)
        if (day and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day)
                or clock and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock)
                or court and not re.fullmatch(r"[1-9][0-9]?", court)):
            raise ServiceError(422, "gym_slot_required", "请核对预约日期、开始时间和场地号。")
        if missing:
            raise ServiceError(422, "gym_slot_required",
                               f"你想预约「{item['name']}」。请补充{'、'.join(missing)}，也可以在体育馆预约里查看可选时段。")
        slot_id = f"{day}_{court}_{clock.replace(':', '')}"
        slots = (await self.ops.catalog(who, "gym", query=GymSlotsQuery(venue_id=item["id"], date=day)))["items"]
        slot = next((slot for slot in slots if slot["slot_id"] == slot_id and slot["remaining"] > 0), None)
        if not slot:
            raise ServiceError(409, "gym_slot_unavailable", f"「{item['name']}」的这个场地时段已满或不可预约，请重新选择场地或时间。")
        return slot_id, gym_slot_label(slot)

    async def manage(self, context, who, intent, *, confirm_first=False):
        action, values = intent.action, dict(intent.fields)
        registration, _ = self._access(who, intent.service, action)
        viewer = await self.ops.viewer(who, "club")
        if f"clubs.{action}" not in await self.registered_actions():
            raise ServiceError(503, "minta_action_unavailable", "这项社团管理暂时无法提交，请稍后再试。")
        label = intent.target
        data = {"action": action}
        pending_fields = dict(intent.fields) if confirm_first else None
        if action == "register":
            data.update(club_details({**values, "name": intent.target}))
        else:
            clubs = (await list_clubs(self.ops.gateway, who.user_id, viewer))["items"]
            club = select_one([club for club in clubs if club["can_manage"] and name_key(intent.target) in match_names(club)],
                              "未找到你有权管理的社团，请核对社团名称。")
            data["id"] = club["id"]
            if action in {"update", "archive", "restore"}:
                data["version"] = club["version"]
                if action == "update":
                    data.update(club_details(values, club))
            elif action == "publish_activity":
                if club["review_status"] != "approved":
                    raise ServiceError(403, "club_review_required", "社团通过审核后才能发布活动")
                if not values.get("starts_at") or not values.get("ends_at"):
                    raise ServiceError(422, "club_activity_time_required", "请补充活动的开始和结束时间，精确到分钟。")
                data.update(details(ACTIVITY_FIELDS, values))
                label = data.get("name", label)
            else:
                title = values.pop("activity" if action == "update_activity" else "name", "")
                activity = select_one([item for item in club["activities"] if name_key(title) in match_names(item)],
                                      "请写明要管理的社团活动名称。")
                data.update(activity_id=activity["id"], version=activity["version"])
                label = activity["name"]
                if pending_fields is not None:
                    pending_fields["activity" if action == "update_activity" else "name"] = activity["id"]
                if action == "update_activity":
                    if not (values.get("ends_at") or activity["ends_at"]):
                        raise ServiceError(422, "club_activity_time_required", "请一并提供活动的结束时间，精确到分钟。")
                    data.update(details(ACTIVITY_FIELDS, values, activity))
        if action in {"archive", "archive_activity", "restore", "restore_activity"} and values:
            raise ValueError
        command = ClubCommand.model_validate(data)
        if confirm_first:
            pending = CampusActionPlan(action=registration.id, target=data.get("id", intent.target), arguments=pending_fields)
            raise self._confirm_action(registration, intent.target, intent.fields, pending)
        who = await self.principal(context)
        self._access(who, intent.service, action)
        await self.auth.rate_limit("minta.club_write", str(who.user_id), 30, 60)
        account = await self.ops.access.profiles.account(who) if self.ops.access else await self.auth.user(who)
        effect = ChatEffect(context, f"{CLUB_UNDO[action]}「{label}」")

        def record(connection):
            lock_effect_run(connection, who, effect)
            return add_effect(connection, effect, "club")

        effect_id = await self.ops.db.run(record)
        try:
            result = await change_club(self.ops.gateway, who.user_id, account["display_name"] or account.get("full_name"), f"chat-{context.run_id.hex}",
                                       command, effect_id=effect_id)
        except ServiceError as exc:
            if exc.code != "club_write_unconfirmed":
                await self.ops.db.run(lambda c: resolve_effect(c, effect_id, "no_effect"))
            raise
        await self.ops.db.run(lambda c: resolve_effect(c, effect_id, result["effect"]))
        if action == "register" and result["club"]["review_status"] == "pending":
            return f"已提交「{result['club']['name']}」的登记，管理员审核通过后会进入公开列表。", Status.COMPLETED
        return f"已{CLUB_LABELS[action]}。可在校园应用的「社团」查看「{result['club']['name']}」。", Status.COMPLETED
