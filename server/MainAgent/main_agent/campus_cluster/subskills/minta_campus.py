"""User-scoped MINTA capabilities bound by the authenticated cloud runner."""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, model_validator

from ..action_plan import CampusActionPlan
from ..library_catalog import LibraryCatalogQuery, ResourceId
from ..skills import ActionAccess, Skill, SkillAction, SkillInput


class LibraryStatusQuery(LibraryCatalogQuery):
    view: Literal["both", "catalog", "my_loans"] = Field(
        default="both", description="catalog 查馆藏和匿名借期，my_loans 查本人借还记录及到期时间，both 一起查",
    )


class GymSlotsQuery(SkillInput):
    venue_id: ResourceId
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="北京时间日期 YYYY-MM-DD")


class CampusStatusInput(SkillInput):
    service: Literal["classroom", "gym", "club", "clubs", "library"]
    library: LibraryStatusQuery | None = Field(default=None, description="图书馆查询的书名、作者、编号、分类、页码和范围")
    gym: GymSlotsQuery | None = Field(default=None, description="体育馆场馆编号及北京时间日期 YYYY-MM-DD")

    @model_validator(mode="after")
    def validate_library(self):
        if self.library is not None and self.service != "library":
            raise ValueError("馆藏筛选仅适用于图书馆")
        if self.gym is not None and self.service != "gym":
            raise ValueError("场地时段筛选仅适用于体育馆")
        return self


class CampusActionInput(SkillInput):
    """Write arguments come from user messages in the authenticated conversation."""

    plan: CampusActionPlan


async def _unbound(_context, _args):
    return "校务操作需要当前登录账号和明确的本轮指令。请写明动作和具体名称，或打开对应校园应用。"


ROOM_FIELD = "教室编号或名称"
TEACHER_NAME_FIELD = "老师姓名"
TEACHER_PHONE_FIELD = "老师电话号码"
CLASSROOM_START_FIELD = "开始时间（北京时间）"
CLASSROOM_END_FIELD = "结束时间（北京时间）"
CLASSROOM_TIME_ARGUMENTS = {"starts_at": CLASSROOM_START_FIELD, "ends_at": CLASSROOM_END_FIELD}
CLUB_EDIT_ARGUMENTS = {"name": "新社团名称", "category": "类别", "description": "简介",
                       "allow_grades": "开放年级", "allow_colleges": "开放学院"}
ACTIVITY_ARGUMENTS = {"name": "活动名称", "description": "简介", "location": "地点",
                      "starts_at": "开始时间（北京时间 YYYY-MM-DDTHH:MM）",
                      "ends_at": "结束时间（北京时间 YYYY-MM-DDTHH:MM）", "capacity": "名额（数字字符串）"}
GYM_ARGUMENTS = {"date": "日期（YYYY-MM-DD）", "start_time": "北京时间（HH:MM）", "court_id": "场地号码"}
CLASSROOM_RESERVE = SkillAction(id="classroom.start", label="预约教室", access=(
    ActionAccess(roles=("student",),
                 required_fields=(ROOM_FIELD, CLASSROOM_START_FIELD, CLASSROOM_END_FIELD, TEACHER_NAME_FIELD, TEACHER_PHONE_FIELD),
                 checks=("教室在所选时段可预约", "用所填姓名和电话核验老师凭据"),
                 example="帮我预约明天 14:00 到 16:00 的 A101，老师姓名：指导老师的姓名，电话：老师的电话号码"),
    ActionAccess(roles=("teacher",), required_fields=(ROOM_FIELD, CLASSROOM_START_FIELD, CLASSROOM_END_FIELD),
                 checks=("教室在所选时段可预约", "本人电子凭据有效"),
                 example="帮我预约明天 14:00 到 16:00 的 A101 教室"),
), arguments={"name": TEACHER_NAME_FIELD, "phone": TEACHER_PHONE_FIELD, **CLASSROOM_TIME_ARGUMENTS})


def _action(action_id, label, required_fields, checks=(), *, arguments=None):
    return SkillAction(id=action_id, label=label, access=(
        ActionAccess(roles=("student", "teacher"), required_fields=required_fields, checks=checks),
    ), arguments=arguments or {})


CAMPUS_STATUS = Skill(
    "minta_campus_status", "按当前登录账号查询教室、体育馆、社团活动、社团目录和图书馆。"
    "查书用 service=library 和 library.view=catalog，可按书名、作者、馆藏编号或分类筛选，结果带可借数和匿名进度。"
    "查看本人借阅、到期时间和已归还记录用 library.view=my_loans。"
    "校园活动按 starts_at 和 ends_at 说明完整的北京时间时段。"
    "查询体育馆某天的场地号、时段和余量时填写 gym.venue_id 和 gym.date。",
    CampusStatusInput, _unbound, read_only=True,
)
CAMPUS_ACTION = Skill(
    "minta_campus_action", "办理教室和体育馆预约或取消、社团登记管理及活动报名、图书借还。按实际回执回答。"
    "教室预约填写北京时间的起止时段。学生需提供老师姓名和电话，教师需本人有效凭据。可在同一会话补齐资料。",
    CampusActionInput, _unbound, actions=(
        CLASSROOM_RESERVE,
        _action("classroom.close", "取消教室预约", ("预约记录编号或可定位唯一记录的教室及时段",),
                ("本人有对应预约",), arguments={"starts_at": CLASSROOM_START_FIELD}),
        _action("gym.start", "预约体育馆场地", ("场馆", "日期", "开始时间", "场地号"),
                ("按场地时段核对余量", "使用本人校园身份并确认预约明细"), arguments=GYM_ARGUMENTS),
        _action("gym.close", "取消体育馆预约", ("预约记录编号或可定位唯一记录的场馆及时段",),
                ("本人有对应预约",), arguments=GYM_ARGUMENTS),
        _action("library.start", "借阅图书", ("书名或馆藏编号",), ("有可借副本",),
                arguments={"duration_days": "借阅天数（1–14 天，未填写时默认 7 天）"}),
        _action("library.close", "归还图书", ("书名或馆藏编号",), ("本人有对应借阅记录，可提前归还",)),
        _action("club.start", "报名社团活动", ("活动编号或名称",), ("活动开放且有名额",)),
        _action("club.close", "取消活动报名", ("活动编号或名称",), ("本人有对应报名记录",)),
        _action("clubs.register", "登记社团", ("社团名称", "类别", "简介"),
                arguments={key: label for key, label in CLUB_EDIT_ARGUMENTS.items() if key != "name"}),
        _action("clubs.update", "修改社团资料", ("社团名称", "要修改的资料（可含开放年级、开放学院）"),
                ("本人是社团登记人",), arguments=CLUB_EDIT_ARGUMENTS),
        _action("clubs.archive", "归档社团", ("社团名称",), ("本人是社团登记人",)),
        _action("clubs.restore", "恢复归档社团", ("社团名称",), ("本人是社团登记人", "社团已归档")),
        _action("clubs.publish_activity", "发布社团活动", ("社团名称", "活动名称", "简介", "地点", "开始时间", "结束时间", "名额"),
                ("本人是社团登记人", "社团已通过审核"), arguments=ACTIVITY_ARGUMENTS),
        _action("clubs.update_activity", "修改社团活动", ("社团名称", "原活动名称", "要修改的资料"),
                ("本人是社团登记人",), arguments={"activity": "原活动名称", **ACTIVITY_ARGUMENTS}),
        _action("clubs.archive_activity", "归档社团活动", ("社团名称", "活动名称"), ("本人是社团登记人",),
                arguments={"name": "活动名称"}),
        _action("clubs.restore_activity", "恢复归档活动", ("社团名称", "活动名称"),
                ("本人是社团登记人", "活动已归档"), arguments={"name": "活动名称"}),
    ),
)


@dataclass(frozen=True)
class CampusIntent:
    action: str
    service: str | None
    target: str
    fields: dict[str, str] = field(default_factory=dict)

    def to_plan(self) -> CampusActionPlan:
        return CampusActionPlan(action=f"{self.service or 'reservation'}.{self.action}",
                                target=self.target, arguments=self.fields)

    @classmethod
    def from_plan(cls, plan: CampusActionPlan) -> "CampusIntent":
        service, action = plan.action.split(".")
        return cls(action, None if service == "reservation" else service, plan.target, plan.arguments)


CLUB_ACTIONS = {
    "修改社团活动": "update_activity", "编辑社团活动": "update_activity", "归档社团活动": "archive_activity",
    "恢复归档活动": "restore_activity", "恢复社团活动": "restore_activity", "恢复归档社团": "restore",
    "发布社团活动": "publish_activity", "登记社团": "register", "注册社团": "register",
    "修改社团": "update", "编辑社团": "update", "归档社团": "archive", "恢复社团": "restore",
}
CLUB_FIELDS = {
    "名称": "name", "活动": "name", "活动名称": "name", "原活动": "activity",
    "类别": "category", "简介": "description", "地点": "location", "时间": "starts_at",
    "开始时间": "starts_at", "结束时间": "ends_at", "名额": "capacity",
    "开放年级": "allow_grades", "开放学院": "allow_colleges",
}
TEACHER_FIELDS = {"老师": "name", "老师姓名": "name", "姓名": "name", "电话": "phone", "老师电话": "phone",
                  "电话号码": "phone", "老师电话号码": "phone"}
LIBRARY_FIELDS = {"借阅天数": "duration_days", "借期": "duration_days"}
BOOK_WITH_DURATION = re.compile(r"(《[^》]+》)\s*([0-9一二两三四五六七八九十]+\s*天)")
COMMANDS = (
    ("取消报名", "close", "club"), ("取消预约", "close", None), ("取消预订", "close", None),
    ("取消预定", "close", None), ("取消", "close", None),
    ("预约", "start", None), ("预订", "start", None), ("预定", "start", None), ("订", "start", None),
    ("报名", "start", "club"),
    ("借阅", "start", "library"), ("借书", "start", "library"), ("借", "start", "library"),
    ("提前归还", "close", "library"), ("归还", "close", "library"), ("还书", "close", "library"),
)
PREFIX = re.compile(r"^(?:(?:请(?:你)?|帮我|替我|给我|我要|我想(?:要)?|麻烦(?:你)?)\s*)+")
UNSPECIFIED_CLASSROOM = re.compile(r"(?:(?:任意|随便)\s*)?(?:[一1]?\s*(?:个|间)\s*)?(?:空(?:闲)?\s*)?教室")
CONTACT_PHONE = re.compile(r"((?:联系电话|电话号码|电话|手机号|手机)\s*[:=]?\s*)\+?\d[\d ()-]{5,}\d")
CLASSROOM_PENDING = "classroom_details_pending"
CLASSROOM_CONFIRMATION = "classroom_confirmation"
REPLY_FIELDS = {"教室": "room", "教室编号": "room", "教室名称": "room",
                **TEACHER_FIELDS, "联系电话": "phone", "手机号": "phone",
                "开始时间": "starts_at", "结束时间": "ends_at"}
REPLY_FIELD = re.compile(r"(" + "|".join(REPLY_FIELDS) + r")\s*[:=]\s*(.+)")
REPLY_SEPARATOR = re.compile(r"[,;、/\r\n]+")
ROOM_REPLY = re.compile(r"(?:[\u3400-\u9fff]{1,20}\s*)?(?=[A-Z0-9_-]*[0-9])[A-Z0-9][A-Z0-9_-]{0,31}", re.I)
PHONE_REPLY = re.compile(r"\+?(?:[0-9][ ()-]*){7,15}")
PHONE_IN_TEXT = re.compile(r"(?<![a-zA-Z0-9+])\+?[0-9][0-9 ()-]{5,28}[0-9](?![a-zA-Z0-9])")
CONTACT_OR_DATE = re.compile(
    r"(?P<date>(?<![0-9])[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"(?:(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:Z|[+-][0-9]{2}:[0-9]{2})?)"
    r"(?![0-9])|(?![0-9T-]| +[0-9])))|" + PHONE_IN_TEXT.pattern
)
NAME_REPLY = re.compile(r"[^\W\d_]+(?:[ .·'\-][^\W\d_]+)*")


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).strip(" \t《》「」\"“”").split()).casefold()


def _split_fields(text, names, *, separator_required=True):
    if not names:
        return text.strip(" :"), {}
    labels = "|".join(sorted(names, key=len, reverse=True))
    separator = r"\s*[:=]\s*" if separator_required else r"\s*[:=]?\s*"
    field = re.compile(rf"({labels}){separator}(.+)")
    head, *fields = re.split(rf"[,;]\s*(?=(?:{labels}){separator}.+)", text)
    result = {}
    for part in fields:
        match = field.fullmatch(part)
        if match is None:
            return "", {}
        key, value = match.groups()
        field_name = names[key]
        if field_name in result or not value.strip():
            return "", {}
        result[field_name] = value.strip()
    return head.strip(" :"), result


def _classroom_target(target):
    if UNSPECIFIED_CLASSROOM.fullmatch(target):
        return ""
    if target.startswith("教室"):
        return target.removeprefix("教室").strip(" :").removesuffix("教室").strip()
    if target.endswith("教室"):
        return target.removesuffix("教室").strip()
    if name_key(target).startswith("room-"):
        return target
    return None


def campus_intent(query: str) -> CampusIntent | None:
    line = unicodedata.normalize("NFKC", query).strip().rstrip("。!").strip()
    if not line or len(line) > 1500 or any(char in line for char in "?\n\r"):
        return None
    line, prefixed = PREFIX.subn("", line)
    if line.endswith(("吗", "么", "多少钱", "怎么操作", "怎么办")):
        return None
    for verb, action in CLUB_ACTIONS.items():
        if line.startswith(verb):
            target, fields = _split_fields(line[len(verb):], CLUB_FIELDS)
            return CampusIntent(action, "clubs", target, fields) if target else None
    for verb, action, service in COMMANDS:
        if not line.startswith(verb):
            continue
        argument = re.sub(r"^\s*(?:一下|一本)\s*", "", line[len(verb):]).strip(" :")
        if verb == "借" and not (prefixed or argument.startswith("《")):
            return None
        target, fields = _split_fields(argument, TEACHER_FIELDS if action == "start" and service is None else {},
                                       separator_required=False)
        if action == "start" and service is None and any(label in argument for label in ("开始时间", "结束时间")):
            target, fields = _split_fields(argument, {**TEACHER_FIELDS, "开始时间": "starts_at", "结束时间": "ends_at"},
                                           separator_required=False)
        if service == "library" and action == "start":
            target, fields = _split_fields(argument, LIBRARY_FIELDS)
            if not fields and (match := BOOK_WITH_DURATION.fullmatch(target)):
                target, duration = match.groups()
                fields = {"duration_days": duration}
        target = target.removesuffix("的预约") if action == "close" and service is None else target
        target = target.strip()
        if service is None and target:
            room = _classroom_target(target)
            if room is not None or fields:
                return CampusIntent(action, "classroom", target if room is None else room, fields)
        return CampusIntent(action, service, target, fields) if target else None
    return None


def classroom_fields(query: str) -> dict[str, str]:
    line = unicodedata.normalize("NFKC", query).strip().rstrip("。").strip()
    if not line or len(line) > 240 or any(char in line for char in "?!"):
        return {}
    parts = [part.strip() for part in REPLY_SEPARATOR.split(line)]
    if len(parts) > 5:
        return {}
    fields = {}
    bare_name = False
    for part in parts:
        if match := REPLY_FIELD.fullmatch(part):
            label, value = match.groups()
            field_name = REPLY_FIELDS[label]
        elif PHONE_REPLY.fullmatch(part):
            field_name, value = "phone", part
        elif ROOM_REPLY.fullmatch(part.removesuffix("教室").strip()):
            field_name, value = "room", part.removesuffix("教室").strip()
        elif len(parts) > 1 and 2 <= len(part) <= 24 and NAME_REPLY.fullmatch(part):
            field_name, value = "name", part
            bare_name = True
        else:
            return {}
        if (field_name in fields or not value or len(value) > 80
                or field_name == "phone" and not PHONE_REPLY.fullmatch(value)):
            return {}
        fields[field_name] = value
    return {} if bare_name and "phone" not in fields else fields


def classroom_command_needs_model(intent: CampusIntent | None) -> bool:
    if not intent or intent.action != "start" or intent.service not in {None, "classroom"} or not intent.target:
        return False
    head, *remaining = REPLY_SEPARATOR.split(intent.target, maxsplit=1)
    if remaining:
        room = head.strip().removesuffix("教室").strip()
        return (intent.service == "classroom" or ROOM_REPLY.fullmatch(room) is not None
                or PHONE_IN_TEXT.search(intent.target) is not None)
    if intent.service != "classroom":
        return False
    if intent.fields:
        return any(REPLY_SEPARATOR.search(value) for value in intent.fields.values())
    return ROOM_REPLY.fullmatch(intent.target) is None


def redact_classroom_contact(query: str, *, pending=False) -> str:
    intent = campus_intent(query)
    if intent and intent.action == "start" and intent.service == "classroom":
        phone = intent.fields.get("phone")
        if phone and PHONE_REPLY.fullmatch(phone):
            return unicodedata.normalize("NFKC", query).replace(phone, "[电话已隐藏]")
    fields = classroom_fields(query)
    if "phone" in fields and (pending or len(fields) > 1):
        return unicodedata.normalize("NFKC", query).replace(fields["phone"], "[电话已隐藏]")
    if pending or classroom_command_needs_model(intent) or any(term in query for term in ("教室", "课室", "老师", "教师")):
        text = unicodedata.normalize("NFKC", query)
        redacted = CONTACT_OR_DATE.sub(
            lambda match: match[0] if match["date"] else "[电话已隐藏]",
            CONTACT_PHONE.sub(r"\1[电话已隐藏]", text),
        )
        if redacted != text:
            return redacted
    return query
