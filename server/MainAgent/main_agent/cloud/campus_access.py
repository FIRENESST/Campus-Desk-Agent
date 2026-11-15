"""Shared campus-service eligibility for the website and assistant."""

import unicodedata
from typing import Annotated
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .campus_profiles import GRADES
from .errors import ServiceError, not_found
from .roles import UserRole


SERVICES = frozenset({"classroom", "club", "gym", "library", "schedule", "map"})
AudienceList = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=100)]


def college_key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


class AcademicAudience(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    allow_grades: AudienceList = Field(default_factory=list)
    allow_colleges: AudienceList = Field(default_factory=list)

    @field_validator("allow_grades", "deny_grades", check_fields=False)
    @classmethod
    def known_grades(cls, values):
        if set(values) - GRADES.keys():
            raise ValueError("请选择有效的年级")
        return list(dict.fromkeys(values))

    @field_validator("allow_colleges", "deny_colleges", check_fields=False)
    @classmethod
    def normalized_colleges(cls, values):
        if any(not college_key(value) or any(unicodedata.category(ch).startswith("C") for ch in value) for value in values):
            raise ValueError("学院名称不能为空或包含控制字符")
        return list(dict.fromkeys(college_key(value) for value in values))


class Audience(AcademicAudience):
    allow_users: AudienceList = Field(default_factory=list)
    deny_users: AudienceList = Field(default_factory=list)
    deny_grades: AudienceList = Field(default_factory=list)
    deny_colleges: AudienceList = Field(default_factory=list)

    @field_validator("allow_users", "deny_users")
    @classmethod
    def user_uuids(cls, values):
        return list(dict.fromkeys(str(UUID(value)) for value in values))


class RuleInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    audience: Audience
    blocked: bool = False


def permitted(audience, viewer):
    academic = ("grades", "colleges")
    if not viewer["reviewed"] and any(audience.get(prefix + field) for prefix in ("allow_", "deny_") for field in academic):
        return False
    for field, value in (("users", viewer["user_id"]), ("grades", viewer["grade"]), ("colleges", viewer["college"])):
        if value in audience.get("deny_" + field, ()):
            return False
        allowed = audience.get("allow_" + field)
        if allowed and value not in allowed:
            return False
    return True


def service_rules(connection, service):
    if service not in SERVICES:
        raise not_found()
    rows = connection.execute("SELECT resource_id,audience,blocked FROM campus_access_rules WHERE service=%s ORDER BY resource_id",
                              (service,)).fetchall()
    return {row["resource_id"]: row for row in rows}


def save_rule(connection, service, resource_id, rule):
    if service not in SERVICES or (resource_id and service != "classroom") or (not resource_id and rule.blocked):
        raise not_found()
    audience = rule.audience.model_dump(exclude_defaults=True)
    if audience or rule.blocked:
        connection.execute(
            """INSERT INTO campus_access_rules(service,resource_id,audience,blocked) VALUES (%s,%s,%s,%s)
            ON CONFLICT(service,resource_id) DO UPDATE SET audience=excluded.audience,blocked=excluded.blocked""",
            (service, resource_id, Jsonb(audience), rule.blocked),
        )
    else:
        connection.execute("DELETE FROM campus_access_rules WHERE service=%s AND resource_id=%s", (service, resource_id))
    return {"resource_id": resource_id, "audience": audience, "blocked": rule.blocked}


def denied():
    return ServiceError(403, "campus_access_denied", "此校务服务的开放范围不包含当前校园身份")


class CampusAccess:
    def __init__(self, database, profiles):
        self.db, self.profiles = database, profiles

    async def check(self, principal, service, resource_id=None):
        identity = await self.profiles.require_ready(principal)
        return await self._check_rules(principal, identity, service, resource_id)

    async def schedule_number(self, principal):
        identity = await self.profiles.require_ready(principal, load_optional=True)
        await self._check_rules(principal, identity, "schedule")
        if identity:
            return identity.get("verified_campus_number") or (
                identity["campus_number"] if identity["reviewed_at"] else None
            )

    async def _check_rules(self, principal, identity, service, resource_id=None):
        reviewed = bool(identity and identity["reviewed_at"])
        viewer = {"user_id": str(principal.user_id), "reviewed": reviewed,
                  "grade": identity["grade"] if reviewed else None,
                  "college": college_key(identity["college"]) if reviewed else None}
        rules = await self.db.run(lambda connection: service_rules(connection, service))
        if not permitted(rules.get("", {}).get("audience", {}), viewer):
            raise denied()
        if resource_id:
            rule = rules.get(resource_id, {})
            if not permitted(rule.get("audience", {}), viewer):
                raise denied()
            if rule.get("blocked"):
                raise ServiceError(409, "classroom_unavailable", "该教室已由管理员设为占用或暂停预约")
        return viewer, rules

    @staticmethod
    def rooms(items, viewer, rules):
        visible = []
        for room in items:
            rule = rules.get(room["id"], {})
            if permitted(rule.get("audience", {}), viewer):
                blocked = rule.get("blocked", False)
                visible.append({**room, "reserved": room["reserved"] or blocked, "booking_blocked": blocked})
        return visible
