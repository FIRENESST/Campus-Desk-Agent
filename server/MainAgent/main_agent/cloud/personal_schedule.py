"""Account-owned events in the campus calendar."""

from datetime import date, datetime, time, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from ..campus_cluster.activity_time import ActivityTimestamp, campus_minute
from .errors import ServiceError, not_found


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f]+$")
    location: str = Field(default="", max_length=180, pattern=r"^[^\x00-\x1f\x7f]*$")
    starts_at: ActivityTimestamp
    ends_at: ActivityTimestamp

    @model_validator(mode="after")
    def ordered_times(self):
        start, end = self.period
        if end <= start:
            raise ValueError("结束时间须晚于开始时间")
        return self

    @property
    def period(self):
        try:
            return campus_minute(self.starts_at), campus_minute(self.ends_at)
        except OverflowError as exc:
            raise ValueError("日期超出支持范围") from exc


class NewEvent(EventInput):
    id: str = Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")


class EventUpdate(EventInput):
    revision: int = Field(ge=1, lt=2_147_483_647)


def calendar_window(params):
    try:
        first, last = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        if (len(params.multi_items()) != 2 or first.isoformat() != params["start"]
                or last.isoformat() != params["end"] or not timedelta(0) < last - first <= timedelta(days=31)):
            raise ValueError
    except (KeyError, ValueError):
        raise ServiceError(422, "calendar_invalid_window", "请选择有效的日程范围，最多 31 天") from None
    return tuple(datetime.combine(day, time.min, CAMPUS_TIME_ZONE) for day in (first, last))


def classroom_event(row):
    room = row["resource_name"] or row["resource_id"]
    return {"id": row["id"], "kind": "classroom", "title": f"教室预约 · {room}", "location": room,
            "starts_at": row["classroom_starts_at"], "ends_at": row["classroom_ends_at"]}


class PersonalEvents:
    def __init__(self, db):
        self.db = db

    async def list(self, user_id, window=None):
        time_filter = " AND ends_at>%s AND starts_at<%s" if window else ""
        return await self.db.run(lambda c: c.execute(
            """SELECT id,title,location,starts_at,ends_at,revision,'custom' AS kind
               FROM personal_calendar_events WHERE user_id=%s""" + time_filter + " ORDER BY starts_at,id",
            (user_id, *(window or ())),
        ).fetchall())

    async def create(self, user_id, data):
        event_id = UUID(data.id)
        fields = data.title, data.location, *data.period

        def save(connection):
            row = connection.execute(
                """INSERT INTO personal_calendar_events (user_id,id,title,location,starts_at,ends_at)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                   RETURNING id,title,location,starts_at,ends_at,revision,'custom' AS kind""",
                (user_id, event_id, *fields),
            ).fetchone()
            if row is not None:
                return row
            row = connection.execute(
                """SELECT id,title,location,starts_at,ends_at,revision,'custom' AS kind
                   FROM personal_calendar_events WHERE user_id=%s AND id=%s""", (user_id, event_id),
            ).fetchone()
            if row and tuple(row[key] for key in ("title", "location", "starts_at", "ends_at")) == fields:
                return row
            raise ServiceError(409, "calendar_event_changed", "日程已变化，请重新打开对应事件")

        return await self.db.run(save)

    async def update(self, user_id, event_id, data):
        return await self._change(user_id, event_id, data.revision,
            """UPDATE personal_calendar_events SET title=%s,location=%s,starts_at=%s,ends_at=%s,revision=revision+1""",
            (data.title, data.location, *data.period))

    async def delete(self, user_id, event_id, revision):
        await self._change(user_id, event_id, revision, "DELETE FROM personal_calendar_events", ())

    async def _change(self, user_id, event_id, revision, statement, fields):
        def save(connection):
            row = connection.execute(
                statement + " WHERE user_id=%s AND id=%s AND revision=%s"
                " RETURNING id,title,location,starts_at,ends_at,revision,'custom' AS kind",
                (*fields, user_id, event_id, revision),
            ).fetchone()
            if row is not None:
                return row
            if connection.execute("SELECT 1 FROM personal_calendar_events WHERE user_id=%s AND id=%s",
                                  (user_id, event_id)).fetchone():
                raise ServiceError(409, "calendar_event_changed", "日程已变化，请重新打开对应事件")
            raise not_found()

        return await self.db.run(save)
