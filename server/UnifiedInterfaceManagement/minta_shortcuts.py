"""Read-only views over selected MINTA simulation services."""

import re

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import QueryParams

from protocol import InterfaceError, parse_json


MAX_RESPONSE = 64 * 1024
STAFF_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
CLOCK_TIME = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"


class Course(BaseModel):
    model_config = ConfigDict(strict=True)

    course_id: str
    course_name: str
    teacher: str
    classroom: str
    day: str
    start_period: int
    end_period: int
    points: int | None = Field(default=None, ge=1, le=4)


class Activity(BaseModel):
    model_config = ConfigDict(strict=True)

    id: str
    name: str
    description: str
    location: str
    starts_at: str
    ends_at: str
    capacity: int
    remaining: int
    club_id: str | None = None


class Classroom(BaseModel):
    model_config = ConfigDict(strict=True)

    room_id: str = Field(validation_alias="roomId")
    name: str
    capacity: int
    reserved: bool


class PeriodTime(BaseModel):
    model_config = ConfigDict(strict=True)

    period: int = Field(ge=1, le=255)
    start_time: str = Field(pattern=CLOCK_TIME)
    end_time: str = Field(pattern=CLOCK_TIME)


class Schedule(BaseModel):
    courses: list[Course]
    periods: list[PeriodTime] = Field(default_factory=list, max_length=255)


class Activities(BaseModel):
    activities: list[Activity]


class Classrooms(BaseModel):
    rooms: list[Classroom]


async def _call(
    client: httpx2.AsyncClient, url: httpx2.URL, path: str, name: str | None = None, arguments: dict | None = None,
):
    payload = {"method": "GET", "target": path, "timeout_ms": 5000}
    if name is not None:
        payload.update(method="POST", json={"name": name, "arguments": arguments})
    raw = bytearray()
    try:
        with anyio.fail_after(8):
            async with client.stream(
                "POST", url, json=payload, headers={"Accept-Encoding": "identity"},
                timeout=httpx2.Timeout(8.0, connect=3.0),
            ) as response:
                if response.status_code == 504:
                    raise TimeoutError
                response.raise_for_status()
                async for chunk in response.aiter_bytes(8192):
                    if len(raw) + len(chunk) > MAX_RESPONSE:
                        raise ValueError
                    raw.extend(chunk)
        envelope = parse_json(raw)
        if envelope["status"] == 504:
            raise TimeoutError
        if envelope["status"] != 200:
            raise ValueError
        reply = parse_json(envelope["body"].encode("utf-8"))
        if name is None:
            return reply
        if reply.get("isError"):
            raise ValueError
        return parse_json(reply["content"].encode("utf-8"))
    except (TimeoutError, httpx2.TimeoutException):
        raise InterfaceError(504, "minta_timeout", "MINTA 校务服务响应超时，请重试") from None
    except (httpx2.HTTPError, AttributeError, KeyError, TypeError, ValueError, RecursionError):
        raise InterfaceError(502, "minta_unavailable", "暂时无法读取 MINTA 校务数据，请稍后重试") from None


def _week(query: QueryParams) -> int:
    params = query.multi_items()
    if not params:
        return 1
    if len(params) == 1:
        name, value = params[0]
        if name == "week" and re.fullmatch(r"[1-9]|1[0-9]|20", value):
            return int(value)
    raise InterfaceError(422, "invalid_query", "课表只接受一个 1 到 20 的 week 参数")


async def _read_schedule(client, url, week, role, schedule_number):
    if role not in {"student", "teacher"}:
        raise InterfaceError(502, "schedule_identity_unavailable", "暂时无法核验日程使用的校园身份")
    result = {"week": week, "audience": role}
    if not schedule_number:
        result["staff_verified" if role == "teacher" else "student_verified"] = False
        return {**result, "periods": [], "items": []}
    if not STAFF_NUMBER.fullmatch(schedule_number):
        raise InterfaceError(502, "schedule_identity_unavailable", "暂时无法核验日程使用的校园身份")
    if role == "teacher":
        payload = await _call(client, url, f"/schedule/api/teaching/{schedule_number}?week={week}")
        result["staff_verified"] = True
    else:
        payload = await _call(
            client, url, "/schedule/agent/call", "schedule_query", {"student_id": schedule_number, "week": week},
        )

    schedule = Schedule.model_validate(payload)
    periods = {period.period: period for period in schedule.periods}
    items = []
    for course in schedule.courses:
        item = course.model_dump(exclude_none=True)
        start, end = periods.get(course.start_period), periods.get(course.end_period)
        if start and end and start.start_time < end.end_time:
            item.update(start_time=start.start_time, end_time=end.end_time)
        items.append(item)
    return {**result, "periods": [period.model_dump() for period in periods.values()], "items": items}


async def read_shortcut(
    client: httpx2.AsyncClient, url: httpx2.URL, target: str, query: QueryParams, *,
    schedule_role: str | None = "student", schedule_number: str | None = None,
) -> dict:
    result = {"source": "minta_simulation"}
    if target != "schedule" and query:
        raise InterfaceError(422, "invalid_query", "此查询无需额外参数")
    try:
        if target == "schedule":
            return {**result, **await _read_schedule(client, url, _week(query), schedule_role, schedule_number)}
        elif target == "activities":
            payload = await _call(client, url, "/club/agent/call", "club_list_activities", {})
            items = Activities.model_validate(payload).activities
        elif target == "classrooms":
            payload = await _call(client, url, "/classroom/api/availability")
            items = Classrooms.model_validate(payload).rooms
        else:
            raise InterfaceError(404, "interface_not_found", "校务接口不存在")
    except ValidationError:
        raise InterfaceError(502, "minta_invalid_response", "MINTA 校务数据暂不可用") from None
    result["items"] = [item.model_dump(exclude_none=True) for item in items]
    return result
