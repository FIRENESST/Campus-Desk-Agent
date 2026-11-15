"""Shared full-term schedule lookup for the report and external calendars."""

import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import ServiceError
from .roles import UserRole


TERM_WEEKS = 20
STAFF_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


@dataclass(frozen=True)
class Term:
    id: str
    name: str
    start: date

    @property
    def end(self):
        return self.start + timedelta(weeks=TERM_WEEKS)


def configured_term():
    identifier, name, starts_on = (os.getenv(key, "").strip()
                                   for key in ("CLOUD_TERM_ID", "CLOUD_TERM_NAME", "CLOUD_TERM_START"))
    if not identifier or not name or not starts_on:
        return None
    try:
        start = date.fromisoformat(starts_on)
    except ValueError:
        return None
    return Term(identifier, name, start) if start.weekday() == 0 else None


async def schedule_identity(who, access):
    number = await access.schedule_number(who)
    if not number or not STAFF_NUMBER.fullmatch(number):
        raise ServiceError(403, "schedule_identity_unverified", "请先完成学号或工号核验")
    return number


async def read_term_schedule(who, gateway, access, *, week=None):
    number = await schedule_identity(who, access)
    if who.role == UserRole.TEACHER:
        query = f"?week={int(week)}" if week is not None else ""
        return await gateway.request("GET", f"/schedule/api/teaching/{number}{query}")
    args = {"student_id": number}
    if week is not None:
        args["week"] = week
    return await gateway.tool("schedule", "schedule_query", args)
