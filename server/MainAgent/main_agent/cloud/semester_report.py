"""Private semester results for the current MINTA simulation."""

import asyncio
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from ..campus_cluster.activity_time import ActivityTimestamp
from ..campus_cluster.library_catalog import ResourceId
from .errors import ServiceError
from .minta_operations import GatewayRejected, GatewayUncertain
from .minta_schedule import configured_term, read_term_schedule

Points = Annotated[int, Field(ge=1, le=4)]


class CourseResult(BaseModel):
    model_config = ConfigDict(strict=True)
    course_id: ResourceId
    course_name: str
    points: Points


class CourseList(BaseModel):
    courses: list[CourseResult]


class ActivityResult(BaseModel):
    model_config = ConfigDict(strict=True)
    activity_id: ResourceId
    activity_name: str
    starts_at: ActivityTimestamp
    points: Points


def course_grade(term_id, user_id, course_id):
    digest = sha256(f"simulated-course-grade:{term_id}:{user_id}:{course_id}".encode()).digest()
    return 60 + int.from_bytes(digest[:4], "big") % 41


async def _courses(who, gateway, access, term):
    data = await read_term_schedule(who, gateway, access)
    courses = {}
    for course in CourseList.model_validate(data).courses:
        courses.setdefault(course.course_id, course)
    return [
        {**course.model_dump(), "grade": course_grade(term.id, who.user_id, course.course_id)}
        for course in sorted(courses.values(), key=lambda course: course.course_id)
    ]


async def _activities(who, operations, term):
    activities = {}
    for record in await operations.confirmed_club_registrations(who):
        activity = ActivityResult.model_validate(record)
        start = datetime.fromisoformat(activity.starts_at)
        start = start.replace(tzinfo=start.tzinfo or CAMPUS_TIME_ZONE).astimezone(CAMPUS_TIME_ZONE)
        if term.start <= start.date() < term.end:
            activities[activity.activity_id] = {
                "id": activity.activity_id, "name": activity.activity_name,
                "starts_at": start.isoformat(), "points": activity.points,
            }
    return sorted(activities.values(), key=lambda activity: (activity["starts_at"], activity["id"]))


async def _section(read):
    status = "ok"
    try:
        items = await read
    except ServiceError as exc:
        status = {"campus_access_denied": "restricted", "schedule_identity_unverified": "unverified"}.get(exc.code)
        if status is None:
            raise
        items = []
    except (GatewayRejected, GatewayUncertain, ValueError):
        items, status = [], "unavailable"
    return {"status": status, "items": items, "points": sum(item["points"] for item in items) if status == "ok" else None}


async def semester_report(who, operations, access):
    term = configured_term()
    if term is None:
        term_view = {"id": None, "name": "未配置学期", "starts_on": None, "ends_on": None}
        courses = activities = {"status": "unavailable", "items": [], "points": None}
    else:
        term_view = {"id": term.id, "name": term.name, "starts_on": term.start.isoformat(),
                     "ends_on": (term.end - timedelta(days=1)).isoformat()}
        courses, activities = await asyncio.gather(
            _section(_courses(who, operations.gateway, access, term)),
            _section(_activities(who, operations, term)),
        )
    points = [courses["points"], activities["points"]]
    return {
        "source": "minta_simulation",
        "term": term_view,
        "audience": who.role.value,
        "courses": courses,
        "activities": activities,
        "total_points": None if None in points else sum(points),
    }
