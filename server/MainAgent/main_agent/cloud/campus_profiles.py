"""Personal profiles and access tied to a verified campus account."""

import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import audit, user_view
from .errors import ServiceError, unauthorized


GRADES = {
    "ug1": "大一", "ug2": "大二", "ug3": "大三", "ug4": "大四", "ug5": "大五及以上", "ug_grad": "本科毕业",
    "master1": "硕一", "master2": "硕二", "master3": "硕三", "master4": "硕四及以上", "master_grad": "硕士毕业",
    "phd1": "博一", "phd2": "博二", "phd3": "博三", "phd4": "博四", "phd5": "博五", "phd6": "博六及以上",
    "phd_grad": "博士毕业",
}
PROFILE_FIELDS = ("full_name", "campus_number", "college", "grade")
LINK_REQUIRED = "campus_link_required"
PROFILE_REQUIRED = "campus_profile_required"


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    display_name: Annotated[str, Field(max_length=50)] = ""
    full_name: Annotated[str, Field(min_length=1, max_length=80)]
    campus_number: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")] | None = None
    college: Annotated[str, Field(min_length=1, max_length=100)]
    grade: Annotated[str, Field(max_length=24)] | None = None

    @field_validator("display_name", "full_name", "college")
    @classmethod
    def plain_text(cls, value):
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("资料不能包含控制字符")
        return value


def profile_error(status):
    if status == "link_required":
        return ServiceError(403, LINK_REQUIRED, "请先绑定有效的校园统一身份")
    return ServiceError(403, PROFILE_REQUIRED, "请先登记校园个人资料")


class CampusProfiles:
    def __init__(self, database, provider, *, required, check_local_status=False):
        self.db, self.provider, self.required = database, provider, required
        self.check_local_status = check_local_status

    def _identity(self, connection, user_id, role):
        if self.provider is None:
            return None
        return connection.execute(
            """SELECT i.id,i.subject,p.full_name,p.campus_number,p.college,p.grade,p.reviewed_at
            FROM external_identities i LEFT JOIN campus_profiles p ON p.identity_id=i.id AND p.role=%s
            WHERE i.user_id=%s AND i.protocol=%s AND i.issuer=%s
            ORDER BY p.identity_id IS NULL,i.created_at,i.id LIMIT 1""",
            (role, user_id, self.provider.protocol, self.provider.issuer),
        ).fetchone()

    async def _load(self, principal):
        def read(connection):
            user = connection.execute("SELECT * FROM users WHERE id=%s", (principal.user_id,)).fetchone()
            if not user or not user["is_active"]:
                raise unauthorized()
            identity = self._identity(connection, user["id"], user["role"]) if user["verified_at"] else None
            return user, identity

        user, identity = await self.db.run(read)
        if identity and self.check_local_status:
            number = await self.provider.active_campus_number(identity["subject"])
            identity = {**identity, "verified_campus_number": number} if number else None
        return user, identity

    @staticmethod
    def _status(identity):
        if not identity:
            return "link_required"
        number = identity.get("verified_campus_number")
        if not identity["full_name"] or (number and (identity["campus_number"] or "").lower() != number):
            return "profile_required"
        return "ready"

    async def require_ready(self, principal, *, load_optional=False):
        if not self.required and not load_optional:
            return None
        _, identity = await self._load(principal)
        status = self._status(identity)
        if status == "ready":
            return identity
        if self.required:
            raise profile_error(status)

    async def account(self, principal):
        user, identity = await self._load(principal)
        status = self._status(identity) if self.required else "ready"
        return {**user_view(user), "campus_status": status, "full_name": identity["full_name"] if identity else None}

    async def profile(self, principal):
        user, identity = await self._load(principal)
        data = {field: identity[field] for field in PROFILE_FIELDS} if identity and identity["full_name"] else None
        status = self._status(identity)
        verified_number = identity.get("verified_campus_number") if identity else None
        if data and verified_number:
            data["campus_number"] = verified_number
        return {"campus_status": status, "display_name": user["display_name"], "role": user["role"], "profile": data,
                "verified_campus_number": verified_number,
                "reviewed": status == "ready" and bool(identity["reviewed_at"]), "grades": GRADES}

    async def save(self, principal, data):
        _, identity = await self._load(principal)
        if not identity:
            raise profile_error("link_required")
        verified_number = identity.get("verified_campus_number")
        if verified_number and data.campus_number is not None and data.campus_number.lower() != verified_number:
            raise ServiceError(422, "campus_number_mismatch", "学工号须与 CAS 验证的学工号一致")
        number = verified_number or data.campus_number
        if not number:
            raise ServiceError(422, "campus_number_required", "请填写学号或工号")

        def update(connection):
            user = connection.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (principal.user_id,)).fetchone()
            session = connection.execute(
                "SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s AND revoked_at IS NULL AND expires_at>now() FOR UPDATE",
                (principal.session_id, principal.user_id),
            ).fetchone()
            if not user or not user["is_active"] or not user["verified_at"] or not session:
                raise unauthorized()
            current = self._identity(connection, user["id"], user["role"])
            if not current or current["id"] != identity["id"]:
                raise profile_error("link_required")
            role = user["role"]
            if (role == "student" and data.grade not in GRADES) or (role == "teacher" and data.grade is not None):
                raise ServiceError(422, "invalid_grade", "请填写与校园身份相符的年级")
            values = data.full_name, number, data.college, data.grade
            previous = tuple(current[field] for field in PROFILE_FIELDS)
            if verified_number and previous[1]:
                previous = previous[0], previous[1].lower(), previous[2], previous[3]
            if values != previous:
                connection.execute(
                    """INSERT INTO campus_profiles(identity_id,role,full_name,campus_number,college,grade)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(identity_id) DO UPDATE SET
                    role=excluded.role,full_name=excluded.full_name,campus_number=excluded.campus_number,
                    college=excluded.college,grade=excluded.grade,revision=gen_random_uuid(),reviewed_at=NULL,updated_at=now()""",
                    (current["id"], role, *values),
                )
            user = connection.execute("UPDATE users SET display_name=%s,updated_at=now() WHERE id=%s RETURNING *",
                                      (data.display_name or None, user["id"])).fetchone()
            audit(connection, user["id"], "profile.update", "completed")
            return {**user_view(user), "campus_status": "ready", "full_name": data.full_name}

        return await self.db.run(update)
