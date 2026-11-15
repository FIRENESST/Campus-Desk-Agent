"""Read-only iCalendar feeds for account-owned campus schedules."""

import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from ..campus_cluster.activity_time import campus_datetime
from ..campus_cluster.library_catalog import ResourceId
from .errors import ServiceError, not_found
from .minta_operations import GatewayRejected, GatewayUncertain
from .minta_schedule import TERM_WEEKS, configured_term, read_term_schedule
from .personal_schedule import PersonalEvents, classroom_event
from .roles import UserRole
from .security import digest, token, token_hash


TOKEN_PREFIX = "cal"
DAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
ClockTime = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Period = Annotated[int, Field(ge=1, le=255)]
Week = Annotated[int, Field(ge=1, le=TERM_WEEKS)]


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=60, pattern=r"^[^\x00-\x1f\x7f]+$")


@dataclass(frozen=True)
class Subscriber:
    user_id: UUID
    role: UserRole


class CalendarSubscriptions:
    def __init__(self, db, require_verified_email):
        self.db = db
        self.require_verified_email = require_verified_email

    async def list(self, user_id):
        return await self.db.run(lambda c: c.execute(
            """SELECT id,label,created_at FROM calendar_subscriptions
               WHERE user_id=%s ORDER BY created_at DESC,id""", (user_id,),
        ).fetchall())

    async def create(self, user_id, label):
        secret = token(TOKEN_PREFIX)
        record = await self.db.run(lambda c: c.execute(
            """INSERT INTO calendar_subscriptions (user_id,label,token_hash)
               VALUES (%s,%s,%s) RETURNING id,label,created_at""", (user_id, label, digest(secret)),
        ).fetchone())
        return {**record, "path": f"/calendar/{secret}.ics"}

    async def revoke(self, user_id, subscription_id):
        await self.db.run(lambda c: c.execute(
            "DELETE FROM calendar_subscriptions WHERE user_id=%s AND id=%s", (user_id, subscription_id),
        ))

    async def authenticate(self, secret):
        record = await self.db.run(lambda c: c.execute(
            """SELECT s.user_id,u.role FROM calendar_subscriptions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=%s AND u.is_active AND (%s=FALSE OR u.verified_at IS NOT NULL)""",
            (token_hash(secret, TOKEN_PREFIX), self.require_verified_email),
        ).fetchone())
        if not record:
            raise not_found()
        return Subscriber(record["user_id"], UserRole(record["role"]))


class Lesson(BaseModel):
    model_config = ConfigDict(strict=True)
    course_id: ResourceId
    course_name: str
    classroom: str
    day: Literal[*DAYS]
    start_period: Period
    end_period: Period
    weeks: list[Week]


class PeriodTime(BaseModel):
    model_config = ConfigDict(strict=True)
    period: Period
    start_time: ClockTime
    end_time: ClockTime


class TermSchedule(BaseModel):
    courses: list[Lesson]
    periods: list[PeriodTime]


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    starts_at: datetime
    ends_at: datetime
    location: str
    category: str


def event_uid(user_id, *parts):
    return digest(":".join([str(user_id), *map(str, parts)])) + "@cuc-campus-desk"


def lesson_events(who, schedule, term):
    periods = {period.period: period for period in schedule.periods}
    lessons = sorted(schedule.courses, key=lambda course: (
        DAYS.index(course.day), course.start_period, course.end_period, course.classroom, course.course_id,
    ))
    occurrences = Counter()
    scheduled = set()
    category = "授课" if who.role == UserRole.TEACHER else "课程"
    for lesson in lessons:
        start = periods[lesson.start_period].start_time
        end = periods[lesson.end_period].end_time
        if lesson.start_period > lesson.end_period or start >= end:
            raise ValueError("课程时段无效")
        weekday = DAYS.index(lesson.day)
        for week in sorted(set(lesson.weeks)):
            day = term.start + timedelta(days=weekday, weeks=week - 1)
            slot = (lesson.course_id, day, start, end, lesson.classroom)
            if slot in scheduled:
                continue
            scheduled.add(slot)
            course_week = (lesson.course_id, week)
            occurrences[course_week] += 1
            yield CalendarEvent(
                event_uid(who.user_id, "course", term.id, *course_week, occurrences[course_week]),
                lesson.course_name,
                datetime.combine(day, time.fromisoformat(start), CAMPUS_TIME_ZONE),
                datetime.combine(day, time.fromisoformat(end), CAMPUS_TIME_ZONE),
                lesson.classroom, category,
            )


async def course_events(who, operations, access):
    if (term := configured_term()) is None:
        return []
    schedule = await read_term_schedule(who, operations.gateway, access)
    return list(lesson_events(who, TermSchedule.model_validate(schedule), term))


async def _activities(who, operations):
    registrations = await operations.confirmed_club_registrations(who)
    if not registrations:
        return []
    ids = {record["activity_id"] for record in registrations}
    catalog = await operations.catalog(who, "club")
    return [
        CalendarEvent(
            event_uid(who.user_id, "activity", item["id"]), item["name"],
            campus_datetime(item["starts_at"]), campus_datetime(item["ends_at"]), item["location"], "校园活动",
        )
        for item in catalog["items"] if item["id"] in ids
    ]


async def _read_events(read):
    try:
        return await read
    except ServiceError as exc:
        if exc.code in {"campus_access_denied", "schedule_identity_unverified"}:
            return []
        if exc.code not in {"upstream_rejected", "minta_unavailable", "gateway_unavailable"}:
            raise
    except (GatewayRejected, GatewayUncertain, KeyError, ValueError):
        pass
    # A temporary outage must not look like a successful, empty calendar.
    raise ServiceError(503, "calendar_data_unavailable", "暂时无法读取完整日程，请稍后重试") from None


async def personal_calendar(who, operations, access, window=None):
    courses, activities, (reservations, verified), custom = await asyncio.gather(
        _read_events(course_events(who, operations, access)),
        _read_events(_activities(who, operations)),
        operations.classroom_calendar(who, window),
        PersonalEvents(operations.db).list(who.user_id, window),
    )
    if not verified:
        raise ServiceError(503, "calendar_data_unavailable", "暂时无法读取完整日程，请稍后重试")
    personal = [
        CalendarEvent(event_uid(who.user_id, item["kind"], item["id"]), item["title"],
                      item["starts_at"], item["ends_at"], item["location"], category)
        for items, category in ((map(classroom_event, reservations), "教室预约"), (custom, "自定义事件"))
        for item in items
    ]
    events = courses + activities + personal
    if window is not None:
        first, last = window
        events = [event for event in events if event.ends_at > first and event.starts_at < last]
    return sorted(events, key=lambda event: (event.starts_at, event.uid))


def _escape(value):
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _fold(line):
    result, current = [], bytearray()
    for character in line:
        encoded = character.encode("utf-8")
        if len(current) + len(encoded) > 75:
            result.append(bytes(current))
            current = bytearray(b" ")
        current.extend(encoded)
    result.append(bytes(current))
    return b"\r\n".join(result)


def _utc(value):
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def calendar_document(events, *, now=None):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CUC Campus Desk//Personal Calendar//ZH", "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:中传校园日程", "X-WR-TIMEZONE:Asia/Shanghai",
        "BEGIN:VTIMEZONE", "TZID:Asia/Shanghai", "BEGIN:STANDARD", "DTSTART:19920101T000000",
        "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "END:STANDARD", "END:VTIMEZONE",
    ]
    stamp = "DTSTAMP:" + _utc(now or datetime.now(UTC))
    for event in events:
        lines.extend([
            "BEGIN:VEVENT", f"UID:{event.uid}", stamp, f"DTSTART:{_utc(event.starts_at)}",
            f"DTEND:{_utc(event.ends_at)}", f"SUMMARY:{_escape(event.title)}",
            f"LOCATION:{_escape(event.location)}", f"CATEGORIES:{_escape(event.category)}", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    payload = b"\r\n".join(map(_fold, lines)) + b"\r\n"
    version = digest("\n".join(line for line in lines if line != stamp))
    return payload, f'W/"{version}"'
