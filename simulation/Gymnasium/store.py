from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4


BEIJING = timezone(timedelta(hours=8))
BOOKING_DAYS = 7
START_HOURS = (8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20)
SLOT_ID = re.compile(r"(\d{4}-\d{2}-\d{2})_([1-9][0-9]?)_([0-9]{4})")


def load_venues(path):
    if not path:
        return []
    with Path(path).open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("场馆清单过大")
    records = json.loads(raw)
    if not isinstance(records, list):
        raise ValueError("场馆清单须为 JSON 数组")
    ids = set()
    for venue in records:
        if (
            not isinstance(venue, dict)
            or set(venue) != {"id", "name", "capacity", "courts", "court_type", "places"}
            or any(not isinstance(venue[key], str) or not venue[key].strip() or len(venue[key]) > 64
                   for key in ("id", "name", "court_type"))
            or venue["id"] in ids
            or any(type(venue[key]) is not int or not 1 <= venue[key] <= 99
                   for key in ("capacity", "courts", "places"))
        ):
            raise ValueError("场馆清单存在无效条目或重复 ID")
        ids.add(venue["id"])
    return records


class GymError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class GymStore:
    def __init__(self, database: str | Path = ":memory:", *, venues=(), clock=lambda: datetime.now(BEIJING)) -> None:
        self._lock = RLock()
        self._database = database
        self._db: sqlite3.Connection | None = None
        self._venues = {venue["id"]: dict(venue) for venue in venues}
        self._clock = clock

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            if isinstance(self._database, Path):
                self._database.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self._database, check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY, venue_id TEXT NOT NULL,
                    venue_name TEXT NOT NULL, user_name TEXT NOT NULL, created_at TEXT NOT NULL,
                    slot_id TEXT, court_id TEXT, court_name TEXT, starts_at TEXT, ends_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS reservations_attendee_slot
                ON reservations(venue_id, COALESCE(slot_id,''), user_name);
            """)
            self._db = db
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _now(self):
        return self._clock().astimezone(BEIJING).replace(microsecond=0)

    def booking_window(self):
        today = self._now().date()
        return {"first_date": today.isoformat(), "last_date": (today + timedelta(days=BOOKING_DAYS - 1)).isoformat()}

    def _venue(self, venue_id):
        venue = self._venues.get(self._required_text(venue_id, "venue_id", 64))
        if venue is None:
            raise GymError("NOT_FOUND", "场地不存在")
        return venue

    @staticmethod
    def _required_text(value: Any, field: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise GymError("INVALID_ARGUMENT", f"{field} 必须是非空字符串")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise GymError(
                "INVALID_ARGUMENT", f"{field} 长度不能超过 {max_length} 个字符"
            )
        return normalized

    def list_venues(self) -> list[dict[str, Any]]:
        with self._lock:
            reserved = dict(self._connection().execute(
                "SELECT venue_id,count(*) FROM reservations WHERE slot_id IS NULL GROUP BY venue_id"
            ))
            return [
                {"id": venue["id"], "name": venue["name"], "capacity": venue["capacity"],
                 "court_count": venue["courts"], "reserved": reserved.get(venue["id"], 0),
                 "remaining": venue["capacity"] - reserved.get(venue["id"], 0)}
                for venue in self._venues.values()
            ]

    def _day(self, value, now):
        try:
            day = date.fromisoformat(value)
            if day.isoformat() != value:
                raise ValueError
        except (TypeError, ValueError):
            raise GymError("INVALID_ARGUMENT", "请选择有效的预约日期") from None
        if not 0 <= (day - now.date()).days < BOOKING_DAYS:
            raise GymError("SLOT_UNAVAILABLE", "请选择未来七天内的可预约时段")
        return day

    @staticmethod
    def _slot(venue, day, court, hour):
        start = datetime.combine(day, time(hour), BEIJING)
        return {"slot_id": f"{day.isoformat()}_{court}_{hour:02d}00", "court_id": str(court),
                "court_name": f"{court} 号{venue['court_type']}", "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat()}

    def list_slots(self, venue_id, value):
        venue = self._venue(venue_id)
        now = self._now()
        day = self._day(value, now)
        with self._lock:
            used = dict(self._connection().execute(
                "SELECT slot_id,count(*) FROM reservations WHERE venue_id=? AND slot_id LIKE ? GROUP BY slot_id",
                (venue["id"], day.isoformat() + "_%"),
            ))
            slots = []
            for hour in START_HOURS:
                if datetime.combine(day, time(hour), BEIJING) <= now:
                    continue
                for court in range(1, venue["courts"] + 1):
                    slot = self._slot(venue, day, court, hour)
                    slots.append({**slot, "capacity": venue["places"],
                                  "remaining": venue["places"] - used.get(slot["slot_id"], 0)})
            return slots

    @staticmethod
    def _public_reservation(record):
        return {key: record[key] for key in record.keys() if record[key] is not None}

    def list_reservations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection().execute(
                "SELECT * FROM reservations WHERE ends_at IS NULL OR ends_at>? ORDER BY created_at,reservation_id",
                (self._now().isoformat(),),
            )
            return [self._public_reservation(row) for row in rows]

    def reserve(self, venue_id: Any, user_name: Any, slot_id: Any = None) -> dict[str, Any]:
        venue = self._venue(venue_id)
        user_name = self._required_text(user_name, "user_name", 50)
        now = self._now()
        slot = {}
        if slot_id is not None:
            match = SLOT_ID.fullmatch(self._required_text(slot_id, "slot_id", 40))
            if match is None:
                raise GymError("INVALID_ARGUMENT", "场地时段编号无效")
            day, court, clock = match.groups()
            day = self._day(day, now)
            court, hour = int(court), int(clock[:2])
            if court > venue["courts"] or clock[2:] != "00" or hour not in START_HOURS:
                raise GymError("SLOT_UNAVAILABLE", "这个场地或时段不可预约")
            slot = self._slot(venue, day, court, hour)
            if datetime.fromisoformat(slot["starts_at"]) <= now:
                raise GymError("SLOT_UNAVAILABLE", "这个时段已开始，请选择其他时段")
        with self._lock:
            db = self._connection()
            with db:
                db.execute("BEGIN IMMEDIATE")
                if slot and datetime.fromisoformat(slot["starts_at"]) <= self._now():
                    raise GymError("SLOT_UNAVAILABLE", "这个时段已开始，请选择其他时段")
                count, duplicate = db.execute(
                    "SELECT count(*),count(CASE WHEN user_name=? THEN 1 END) FROM reservations WHERE venue_id=? AND slot_id IS ?",
                    (user_name, venue["id"], slot_id),
                ).fetchone()
                if duplicate:
                    raise GymError("CONFLICT", "该用户已预约这个场地时段")
                if count >= (venue["places"] if slot else venue["capacity"]):
                    raise GymError("CAPACITY_FULL", "所选场地时段名额已满，可换一个时段", retryable=True)
                reservation = {
                    "reservation_id": f"gym-{uuid4().hex}", "venue_id": venue["id"], "venue_name": venue["name"],
                    "user_name": user_name, "created_at": now.astimezone(timezone.utc).isoformat(), **slot,
                }
                db.execute(
                    """INSERT INTO reservations(reservation_id,venue_id,venue_name,user_name,created_at,slot_id,
                        court_id,court_name,starts_at,ends_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    tuple(reservation.get(key) for key in (
                        "reservation_id", "venue_id", "venue_name", "user_name", "created_at", "slot_id",
                        "court_id", "court_name", "starts_at", "ends_at",
                    )),
                )
                return reservation

    def cancel(self, reservation_id: Any) -> dict[str, Any]:
        reservation_id = self._required_text(reservation_id, "reservation_id", 100)
        with self._lock:
            with self._connection() as db:
                reservation = db.execute("DELETE FROM reservations WHERE reservation_id=? RETURNING *",
                                         (reservation_id,)).fetchone()
                if reservation is None:
                    raise GymError("NOT_FOUND", "预约记录不存在")
                return self._public_reservation(reservation)
